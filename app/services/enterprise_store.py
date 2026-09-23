"""Durable home for the Batch 01 enterprise registries (Batch 02 persistence).

Batch 01's three services keep their state in process-local dictionaries and
said so in their own docstrings ("a migration is reported at the end of the
batch"). Batch 02 provides the migration
(``alembic/versions/0012_enterprise_persistence.py``) and this module, which is
the *only* piece of new logic: it hydrates those dictionaries from the database
before an operation and flushes them back afterwards.

Why hydrate-and-flush instead of reimplementing the services against the ORM:

* **One implementation of every rule.** Validation, the idempotency key, the
  cooldown/max-per-event decision, template rendering, delivery outcomes, the
  inbox transition table and the SLA clock are all still exactly the Batch 01
  code — there is no second copy that can drift from it.
* **The registries are already tenant-keyed**, so hydration is a plain SELECT
  per tenant and flush is a plain upsert. Nothing about the service API changes,
  which keeps the domain tests meaningful.
* **The database is the source of truth.** The dictionaries are a per-request
  materialised view: they are cleared before hydration and re-cleared if the
  operation raises, so a failed request cannot leak partial state into the next
  one.

Concurrency, stated honestly: operations for one tenant are serialised by an
in-process lock, which is exactly right for the single-worker deployment this
project ships (Caddy → one API container). Two API *processes* serving the same
tenant would need row-level locking (`SELECT … FOR UPDATE`) or a queue; that is
a follow-up, not a claim made here.

Usage from a route (one line, see ``app/api/*_routes.py``)::

    async def endpoint(..., _durable: None = Depends(durable_state)):
        ...

The dependency opens the scope before the handler runs, flushes after it
returns, and clears the cache when the handler raised (so a 404/422 leaves no
half-applied state behind).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import TenantContext, get_context
from app.db.models import (
    Automation as AutomationRow,
    AutomationRun as AutomationRunRow,
    InboxThreadState as InboxRow,
    NotificationRow,
    NotificationTemplateRow,
)
from app.db.session import get_session
from app.domain.automation_models import (
    AutomationDefinition,
    AutomationRun,
    AutomationSchedule,
    AutomationStatus,
    ExecutionPolicy,
    FilterRule,
    ScheduleKind,
    TriggerEvent,
)
from app.domain.notification_models import (
    DeliveryState,
    EventSource,
    Notification,
    NotificationChannel,
    NotificationPriority,
    NotificationTemplate,
    Recipient,
)
from app.domain.workflow_models import WorkflowAction
from app.services import automation_service, inbox_service, notification_service

#: tenant_id -> per-tenant serialisation lock (see the module docstring).
_LOCKS: dict[str, asyncio.Lock] = {}
#: tenant_id -> {entity: {key: orm_row}} for the currently open scope.
_ROWS: dict[str, dict[str, dict]] = {}


def _lock(tenant_id: str) -> asyncio.Lock:
    return _LOCKS.setdefault(tenant_id, asyncio.Lock())


def _as_uuid(tenant_id: str) -> uuid.UUID:
    return tenant_id if isinstance(tenant_id, uuid.UUID) else uuid.UUID(str(tenant_id))


# ------------------------------------------------------------------ clearing ---

def _clear(tenant_id: str) -> None:
    """Drop this tenant's materialised view (never another tenant's)."""
    automation_service._REGISTRY.pop(tenant_id, None)
    automation_service._RUNS.pop(tenant_id, None)
    automation_service._LAST_RUN_AT.pop(tenant_id, None)
    notification_service._TEMPLATES.pop(tenant_id, None)
    notification_service._NOTIFICATIONS.pop(tenant_id, None)
    notification_service._DEDUPE.pop(tenant_id, None)
    notification_service._READ.pop(tenant_id, None)
    inbox_service._OVERLAY.pop(tenant_id, None)
    _ROWS.pop(tenant_id, None)


# ---------------------------------------------------------------- hydration ---

async def _load(session: AsyncSession, tenant_id: str) -> None:
    tenant = _as_uuid(tenant_id)
    rows: dict[str, dict] = {"automations": {}, "runs": {}, "templates": {},
                             "notifications": {}, "inbox": {}}

    for row in (await session.execute(
        select(AutomationRow).where(AutomationRow.tenant_id == tenant)
    )).scalars():
        rows["automations"][row.id] = row
        automation_service._REGISTRY.setdefault(tenant_id, {})[row.id] = _definition_of(row)
        if row.last_run_at:
            automation_service._LAST_RUN_AT.setdefault(tenant_id, {})[row.id] = row.last_run_at

    for row in (await session.execute(
        select(AutomationRunRow).where(AutomationRunRow.tenant_id == tenant)
    )).scalars():
        rows["runs"][row.id] = row
        automation_service._RUNS.setdefault(tenant_id, {})[row.id] = _run_of(row)

    for row in (await session.execute(
        select(NotificationTemplateRow).where(NotificationTemplateRow.tenant_id == tenant)
    )).scalars():
        rows["templates"][row.id] = row
        notification_service._TEMPLATES.setdefault(tenant_id, {})[row.id] = _template_of(row)

    for row in (await session.execute(
        select(NotificationRow).where(NotificationRow.tenant_id == tenant)
    )).scalars():
        rows["notifications"][row.id] = row
        notification_service._NOTIFICATIONS.setdefault(tenant_id, {})[row.id] = _notification_of(row)
        notification_service._DEDUPE.setdefault(tenant_id, {})[row.dedupe_key] = row.id
        if row.read:
            notification_service._READ.setdefault(tenant_id, set()).add(row.id)

    for row in (await session.execute(
        select(InboxRow).where(InboxRow.tenant_id == tenant)
    )).scalars():
        rows["inbox"][str(row.call_id)] = row
        state = _overlay_of(row)
        if state:
            inbox_service._OVERLAY.setdefault(tenant_id, {})[str(row.call_id)] = state

    _ROWS[tenant_id] = rows


# -------------------------------------------------------------------- flush ---

async def _flush(session: AsyncSession, tenant_id: str) -> None:
    tenant = _as_uuid(tenant_id)
    rows = _ROWS.setdefault(tenant_id, {"automations": {}, "runs": {}, "templates": {},
                                        "notifications": {}, "inbox": {}})

    for definition in automation_service._REGISTRY.get(tenant_id, {}).values():
        row = rows["automations"].get(definition.id)
        if row is None:
            row = AutomationRow(id=definition.id, tenant_id=tenant)
            session.add(row)
            rows["automations"][definition.id] = row
        _apply_definition(row, definition)
        row.last_run_at = automation_service._LAST_RUN_AT.get(tenant_id, {}).get(definition.id)

    for run in automation_service._RUNS.get(tenant_id, {}).values():
        row = rows["runs"].get(run.id)
        if row is None:
            row = AutomationRunRow(id=run.id, tenant_id=tenant)
            session.add(row)
            rows["runs"][run.id] = row
        _apply_run(row, run)

    for template in notification_service._TEMPLATES.get(tenant_id, {}).values():
        row = rows["templates"].get(template.id)
        if row is None:
            row = NotificationTemplateRow(id=template.id, tenant_id=tenant)
            session.add(row)
            rows["templates"][template.id] = row
        _apply_template(row, template)

    read_ids = notification_service._READ.get(tenant_id, set())
    for notification in notification_service._NOTIFICATIONS.get(tenant_id, {}).values():
        row = rows["notifications"].get(notification.id)
        if row is None:
            row = NotificationRow(id=notification.id, tenant_id=tenant,
                                  dedupe_key=notification.dedupe_key or notification.id)
            session.add(row)
            rows["notifications"][notification.id] = row
        _apply_notification(row, notification, is_read=notification.id in read_ids)

    for call_id, state in inbox_service._OVERLAY.get(tenant_id, {}).items():
        row = rows["inbox"].get(call_id)
        if row is None:
            row = InboxRow(tenant_id=tenant, call_id=_as_uuid(call_id))
            session.add(row)
            rows["inbox"][call_id] = row
        _apply_overlay(row, state)

    await session.flush()


# ------------------------------------------------------------------- scope ---

@asynccontextmanager
async def tenant_scope(session: AsyncSession, tenant_id: str) -> AsyncIterator[None]:
    """Hydrate → run (unchanged service logic) → flush, serialised per tenant."""
    async with _lock(tenant_id):
        _clear(tenant_id)
        await _load(session, tenant_id)
        try:
            yield
        except BaseException:
            # Nothing is written for a failed request, and the next one starts
            # from the database rather than from this request's leftovers.
            _clear(tenant_id)
            raise
        await _flush(session, tenant_id)
        await session.commit()


async def durable_state(
    ctx: TenantContext = Depends(get_context),
    session: AsyncSession = Depends(get_session),
) -> AsyncIterator[None]:
    """FastAPI dependency: run the handler inside the tenant's durable scope."""
    async with tenant_scope(session, str(ctx.tenant_id)):
        yield


# ------------------------------------------------------------ row mappings ---
# Every mapper is the identity on the fields the domain object already carries,
# so a round-trip cannot change meaning. Enums are stored by value.

def _definition_of(row: AutomationRow) -> AutomationDefinition:
    return AutomationDefinition(
        id=row.id,
        tenant_id=str(row.tenant_id),
        name=row.name,
        event=TriggerEvent(row.event),
        description=row.description or "",
        filters=tuple(
            FilterRule(field=item.get("field", ""), operator=item.get("operator", "eq"),
                       value=item.get("value"))
            for item in (row.filters or [])
        ),
        actions=tuple(
            WorkflowAction(name=item.get("name", ""), params=item.get("params") or {})
            for item in (row.actions or [])
        ),
        schedule=AutomationSchedule(
            kind=ScheduleKind(row.schedule_kind),
            delay_seconds=row.delay_seconds,
        ),
        policy=ExecutionPolicy(
            max_attempts=row.max_attempts,
            backoff_seconds=row.backoff_seconds,
            cooldown_seconds=row.cooldown_seconds,
            max_per_event=row.max_per_event,
        ),
        status=AutomationStatus(row.status),
    )


def _apply_definition(row: AutomationRow, definition: AutomationDefinition) -> None:
    row.name = definition.name
    row.description = definition.description
    row.event = definition.event.value
    row.status = definition.status.value
    row.filters = [
        {"field": rule.field, "operator": rule.operator, "value": rule.value}
        for rule in definition.filters
    ]
    row.actions = [
        {"name": action.name, "params": action.params} for action in definition.actions
    ]
    row.schedule_kind = definition.schedule.kind.value
    row.delay_seconds = definition.schedule.delay_seconds
    row.max_attempts = definition.policy.max_attempts
    row.backoff_seconds = definition.policy.backoff_seconds
    row.cooldown_seconds = definition.policy.cooldown_seconds
    row.max_per_event = definition.policy.max_per_event


def _run_of(row: AutomationRunRow) -> AutomationRun:
    return AutomationRun(
        id=row.id,
        automation_id=row.automation_id,
        tenant_id=str(row.tenant_id),
        idempotency_key=row.idempotency_key,
        event=TriggerEvent(row.event),
        business_event_id=row.business_event_id,
        status=row.status,
        attempts=row.attempts,
        next_attempt_at=row.next_attempt_at or "",
        last_error=row.last_error or "",
        result_summary=row.result_summary or {},
        created_at=row.created_at or "",
        finished_at=row.finished_at or "",
    )


def _apply_run(row: AutomationRunRow, run: AutomationRun) -> None:
    row.automation_id = run.automation_id
    row.idempotency_key = run.idempotency_key
    row.event = run.event.value
    row.business_event_id = run.business_event_id
    row.status = run.status
    row.attempts = run.attempts
    row.next_attempt_at = run.next_attempt_at
    row.last_error = run.last_error
    row.result_summary = run.result_summary
    row.created_at = run.created_at
    row.finished_at = run.finished_at


def _template_of(row: NotificationTemplateRow) -> NotificationTemplate:
    return NotificationTemplate(
        id=row.id,
        tenant_id=str(row.tenant_id),
        name=row.name,
        channel=NotificationChannel(row.channel),
        body=row.body or "",
        variables=tuple(row.variables or ()),
    )


def _apply_template(row: NotificationTemplateRow, template: NotificationTemplate) -> None:
    row.name = template.name
    row.channel = template.channel.value
    row.body = template.body
    row.variables = list(template.variables)


def _notification_of(row: NotificationRow) -> Notification:
    payload = row.recipient or {}
    return Notification(
        id=row.id,
        tenant_id=str(row.tenant_id),
        template_id=row.template_id,
        channel=NotificationChannel(row.channel),
        recipient=Recipient(
            kind=payload.get("kind", "user"),
            target=payload.get("target", ""),
            user_id=payload.get("user_id", ""),
        ),
        event_source=EventSource(row.event_source),
        priority=NotificationPriority(row.priority),
        dedupe_key=row.dedupe_key,
        rendered_body=row.rendered_body or "",
        delivery_state=DeliveryState(row.delivery_state),
        attempts=row.attempts,
        next_attempt_at=row.next_attempt_at or "",
        sent_at=row.sent_at or "",
        error_summary=row.error_summary or "",
    )


def _apply_notification(row: NotificationRow, notification: Notification, *,
                        is_read: bool) -> None:
    row.template_id = notification.template_id
    row.channel = notification.channel.value
    row.recipient = {
        "kind": notification.recipient.kind,
        "target": notification.recipient.target,
        "user_id": notification.recipient.user_id,
    }
    row.event_source = notification.event_source.value
    row.priority = notification.priority.value
    row.dedupe_key = notification.dedupe_key or notification.id
    row.rendered_body = notification.rendered_body
    row.delivery_state = notification.delivery_state.value
    row.attempts = notification.attempts
    row.next_attempt_at = notification.next_attempt_at
    row.sent_at = notification.sent_at
    row.error_summary = notification.error_summary
    row.read = is_read


def _overlay_of(row: InboxRow) -> dict:
    """Rebuild the overlay dict exactly as ``inbox_service`` reads it.

    Only *meaningful* keys are set: the service distinguishes "absent" from
    "empty" (``overlay.get("assignee_id")`` decides the derived thread status),
    so writing empty defaults here would silently change behaviour.
    """
    state: dict = {}
    if row.status:
        state["status"] = row.status
    if row.priority and row.priority != "normal":
        state["priority"] = row.priority
    if row.assignee_id:
        state["assignee_id"] = row.assignee_id
    if row.tags:
        state["tags"] = list(row.tags)
    if row.notes:
        state["notes"] = list(row.notes)
    if row.unread:
        state["unread"] = row.unread
    if row.opened_at:
        state["opened_at"] = row.opened_at
    if row.sla_deadline_at:
        state["sla_deadline_at"] = row.sla_deadline_at
    return state


def _apply_overlay(row: InboxRow, state: dict) -> None:
    row.status = state.get("status")
    row.priority = state.get("priority", "normal")
    row.assignee_id = state.get("assignee_id", "")
    row.tags = list(state.get("tags", []))
    row.notes = list(state.get("notes", []))
    row.unread = int(state.get("unread", 0) or 0)
    row.opened_at = state.get("opened_at", "")
    row.sla_deadline_at = state.get("sla_deadline_at", "")
