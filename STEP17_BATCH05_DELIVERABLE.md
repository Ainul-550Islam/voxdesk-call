# Step 17 — Batch 05: durable storage for the automation / notification / inbox surface

Repo: `/home/user/voxdesk` (HEAD `ccba554`)
Generated: 2026-09-21T16:46:50Z

Batch 01 shipped three services whose state lived in process-local dictionaries and said so
in their own docstrings ("a migration is reported at the end of the batch"); Batch 02 then
wired them to HTTP, which turned that gap into a user-visible one — an API restart lost every
automation, every notification and every unread badge. This batch closes it with the schema and
the smallest possible piece of new code:

* `alembic/versions/0012_enterprise_persistence.py` adds five tables (`automations`,
  `automation_runs`, `notification_templates`, `notifications`, `inbox_thread_states`), all
  tenant-scoped and all cascading from `tenants`; the inbox overlay also cascades from `calls`,
  so the retention purge takes it along.
* `app/services/enterprise_store.py` hydrates the Batch 01 registries from those rows before
  each request and flushes them back afterwards — so there is still exactly **one**
  implementation of every rule (validation, idempotency key, cooldown/max-per-event, template
  rendering, delivery states, the thread transition table, the SLA clock) and it is still the
  Batch 01 code.
* `app/db/models.py` grows the five models (appended; nothing above them is edited).
* The three route modules take a router-level `Depends(durable_state)` — one line each.
* `tests/test_enterprise_persistence.py` proves the property the gap was about: it wipes *all*
  in-process state between requests (what a fresh worker starts with) and re-reads through the
  API, so anything it sees must have come back out of the database.

The three route modules also appear in Batch 02; the versions reproduced here are the
durable-scope versions. Everything is given in full — no elision, no placeholder comments.

**Files in this batch: 9.** Every block below is the file's content byte-for-byte as it exists in the working tree. Each block header carries the line count and the SHA-256 of the whole file, so a reader can confirm the block is complete and unmodified — nothing is paraphrased, summarised, or replaced by a placeholder comment.

---


==============================================================================
===== FILE: alembic/versions/0012_enterprise_persistence.py (207 lines, sha256 2ff619ed2c0e09579460fa299245fa019a4c1b8cdc906332fc87de46f8d674ea) =====
==============================================================================
```python
"""enterprise surface persistence: automations, notifications, inbox state

Batch 02 closes the schema gap Batch 01 reported: the automation, notification
and inbox services kept their state in process-local dictionaries, so a restart
(or a second API worker) lost every automation, notification and unread badge.

Five tables, all tenant-scoped and all cascading from `tenants` (and, for the
inbox overlay, from `calls` so the retention purge takes it along):

* ``automations``            — definitions (filters/actions as JSON)
* ``automation_runs``       — one row per business event, PK = idempotency key
* ``notification_templates``— templates
* ``notifications``         — rendered notifications (holds recipient PII;
                              unique per (tenant, dedupe_key); purged by
                              ``app.core.retention.purge_expired_notifications``)
* ``inbox_thread_states``   — the inbox overlay (priority/assignee/tags/notes/
                              unread/SLA) over a real ``calls`` row

State columns are plain strings rather than PostgreSQL enum types: the closed
vocabularies live in the domain layer (and are pinned by
``tests/test_enum_consistency.py`` for the enums that already existed), and this
migration deliberately does not widen that contract.

Revision ID: 0012_enterprise_persistence
Revises: 0011_side_effect_exactly_once
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_enterprise_persistence"
down_revision = "0011_side_effect_exactly_once"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "automations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column(
            "tenant_id", sa.UUID(), sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("event", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="disabled"),
        sa.Column("filters", sa.JSON(), nullable=True),
        sa.Column("actions", sa.JSON(), nullable=True),
        sa.Column("schedule_kind", sa.String(length=16), nullable=False, server_default="on_event"),
        sa.Column("delay_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("backoff_seconds", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("cooldown_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_per_event", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_run_at", sa.String(length=40), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_automations_tenant_id", "automations", ["tenant_id"])
    op.create_index("ix_automations_tenant_event", "automations", ["tenant_id", "event"])

    op.create_table(
        "automation_runs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column(
            "tenant_id", sa.UUID(), sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("automation_id", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("event", sa.String(length=32), nullable=False),
        sa.Column("business_event_id", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("last_error", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("result_summary", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("finished_at", sa.String(length=40), nullable=False, server_default=""),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_automation_runs_tenant_id", "automation_runs", ["tenant_id"])
    op.create_index("ix_automation_runs_automation_id", "automation_runs", ["automation_id"])
    op.create_index("ix_automation_runs_idempotency_key", "automation_runs", ["idempotency_key"])
    op.create_index(
        "ix_automation_runs_tenant_automation", "automation_runs",
        ["tenant_id", "automation_id"],
    )
    op.create_index("ix_automation_runs_created", "automation_runs", ["created_at"])

    op.create_table(
        "notification_templates",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column(
            "tenant_id", sa.UUID(), sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("variables", sa.JSON(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_notification_templates_tenant_id", "notification_templates", ["tenant_id"]
    )

    op.create_table(
        "notifications",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column(
            "tenant_id", sa.UUID(), sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("template_id", sa.String(length=64), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("recipient", sa.JSON(), nullable=True),
        sa.Column("event_source", sa.String(length=32), nullable=False),
        sa.Column("priority", sa.String(length=16), nullable=False, server_default="normal"),
        sa.Column("dedupe_key", sa.String(length=64), nullable=False),
        sa.Column("rendered_body", sa.Text(), nullable=False, server_default=""),
        sa.Column("delivery_state", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("sent_at", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("error_summary", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("read", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "dedupe_key", name="uq_notification_dedupe"),
    )
    op.create_index("ix_notifications_tenant_id", "notifications", ["tenant_id"])
    op.create_index("ix_notifications_template_id", "notifications", ["template_id"])
    op.create_index("ix_notifications_tenant_state", "notifications", ["tenant_id", "delivery_state"])
    op.create_index("ix_notifications_created", "notifications", ["created_at"])

    op.create_table(
        "inbox_thread_states",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "tenant_id", sa.UUID(), sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "call_id", sa.UUID(), sa.ForeignKey("calls.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), nullable=True),
        sa.Column("priority", sa.String(length=16), nullable=False, server_default="normal"),
        sa.Column("assignee_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column("notes", sa.JSON(), nullable=True),
        sa.Column("unread", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("opened_at", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("sla_deadline_at", sa.String(length=40), nullable=False, server_default=""),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "call_id", name="uq_inbox_thread_state_call"),
    )
    op.create_index("ix_inbox_thread_states_tenant_id", "inbox_thread_states", ["tenant_id"])
    op.create_index("ix_inbox_thread_states_call_id", "inbox_thread_states", ["call_id"])


def downgrade() -> None:
    op.drop_index("ix_inbox_thread_states_call_id", table_name="inbox_thread_states")
    op.drop_index("ix_inbox_thread_states_tenant_id", table_name="inbox_thread_states")
    op.drop_table("inbox_thread_states")

    op.drop_index("ix_notifications_created", table_name="notifications")
    op.drop_index("ix_notifications_tenant_state", table_name="notifications")
    op.drop_index("ix_notifications_template_id", table_name="notifications")
    op.drop_index("ix_notifications_tenant_id", table_name="notifications")
    op.drop_table("notifications")

    op.drop_index("ix_notification_templates_tenant_id", table_name="notification_templates")
    op.drop_table("notification_templates")

    op.drop_index("ix_automation_runs_created", table_name="automation_runs")
    op.drop_index("ix_automation_runs_tenant_automation", table_name="automation_runs")
    op.drop_index("ix_automation_runs_idempotency_key", table_name="automation_runs")
    op.drop_index("ix_automation_runs_automation_id", table_name="automation_runs")
    op.drop_index("ix_automation_runs_tenant_id", table_name="automation_runs")
    op.drop_table("automation_runs")

    op.drop_index("ix_automations_tenant_event", table_name="automations")
    op.drop_index("ix_automations_tenant_id", table_name="automations")
    op.drop_table("automations")
```

==============================================================================
===== FILE: app/services/enterprise_store.py (423 lines, sha256 418be21aa72d532aa07093640c24b3b9ca3168be112b4651af652ce5d6b89099) =====
==============================================================================
```python
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
```

==============================================================================
===== FILE: app/db/models.py (2084 lines, sha256 868a96d6dd5549d141e2fdd0fef6d33305ae8f3a4aa18cbdb0a1c2319db879e2) =====
==============================================================================
```python
"""Database schema.

Multi-tenant from day one: every business you sell to is a Tenant row.
This is what lets you charge $199/month x N clients from one deployment.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime, time

from sqlalchemy import (
    Boolean, DateTime, Enum, Float, ForeignKey, Index, Integer, JSON, String, Text, Time, UniqueConstraint, false,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class CallStatus(str, enum.Enum):
    """
    Lifecycle of a single call.

    NOTE: SQLAlchemy's Enum() persists the member *name* (e.g. "NO_ANSWER"),
    not the value. The PostgreSQL type `callstatus` must therefore contain
    exactly these six names -- see alembic/versions/0002_enum_consistency.py.
    """
    RINGING = "ringing"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    NO_ANSWER = "no_answer"       # Twilio "no-answer": rang out, nobody picked up
    TRANSFERRED = "transferred"   # handed to a human via <Dial>


class TransferState(str, enum.Enum):
    """
    Human-escalation sub-state, tracked separately from CallStatus.

    A call can be IN_PROGRESS while a transfer is mid-flight, so this cannot be
    folded into CallStatus. It also doubles as the idempotency lock: moving out
    of NONE is what stops a second tool call from dialling the human twice.

    NOTE: SQLAlchemy's Enum() persists the member NAME, so the PostgreSQL type
    `transferstate` must contain NONE/REQUESTED/DIALING/CONNECTED/FAILED --
    see alembic/versions/0004_call_transfer_lifecycle.py.
    """
    NONE = "none"             # no escalation has been requested
    REQUESTED = "requested"   # the AI asked; we have not told the provider yet
    DIALING = "dialing"       # the provider accepted; the human's phone is ringing
    CONNECTED = "connected"   # the human answered
    FAILED = "failed"         # busy, no answer, rejected, or a provider error


#: States in which a transfer is already under way or finished. A second
#: request while in one of these must be a no-op, never a second phone call.
TRANSFER_IN_FLIGHT = frozenset({
    TransferState.REQUESTED,
    TransferState.DIALING,
    TransferState.CONNECTED,
})


class CallDirection(str, enum.Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class LeadStatus(str, enum.Enum):
    NEW = "new"
    QUEUED = "queued"
    CALLED = "called"
    QUALIFIED = "qualified"
    UNQUALIFIED = "unqualified"
    FAILED = "failed"
    DNC = "do_not_call"          # legally required: never call again


class UserRole(str, enum.Enum):
    """
    Ordered by privilege. `rbac.ROLE_LEVEL` turns these into integers so a
    role can never grant something above itself.
    """
    OWNER = "owner"
    ADMIN = "admin"
    MANAGER = "manager"
    AGENT = "agent"
    VIEWER = "viewer"


class AuditAction(str, enum.Enum):
    LOGIN_SUCCESS = "login_success"
    LOGIN_FAILURE = "login_failure"
    LOGOUT = "logout"
    TOKEN_REFRESH = "token_refresh"
    USER_CREATED = "user_created"
    USER_DEACTIVATED = "user_deactivated"
    USER_REACTIVATED = "user_reactivated"
    ROLE_CHANGED = "role_changed"
    PASSWORD_CHANGED = "password_changed"
    AUTHZ_DENIED = "authz_denied"
    # STEP 5. `detail` on these carries provider and outcome only -- never a
    # token, never a config value that could hold one.
    INTEGRATION_CONNECTED = "integration_connected"
    INTEGRATION_UPDATED = "integration_updated"
    INTEGRATION_DISCONNECTED = "integration_disconnected"
    INTEGRATION_TESTED = "integration_tested"
    # STEP 6. Same rule: provider and outcome only, never a token.
    CALENDAR_CONNECTED = "calendar_connected"
    CALENDAR_DISCONNECTED = "calendar_disconnected"
    APPOINTMENT_CANCELLED = "appointment_cancelled"
    APPOINTMENT_RESCHEDULED = "appointment_rescheduled"
    # STEP 7. `detail` carries plan codes and outcomes only -- never a card
    # number, never a Stripe key, never a webhook secret.
    BILLING_CHECKOUT_STARTED = "billing_checkout_started"
    BILLING_SUBSCRIPTION_CREATED = "billing_subscription_created"
    BILLING_SUBSCRIPTION_CHANGED = "billing_subscription_changed"
    BILLING_CANCELLATION_REQUESTED = "billing_cancellation_requested"
    BILLING_CANCELLATION_COMPLETED = "billing_cancellation_completed"
    BILLING_PAYMENT_FAILED = "billing_payment_failed"
    BILLING_PLAN_CHANGED = "billing_plan_changed"
    BILLING_LIMIT_HIT = "billing_limit_hit"
    BILLING_ADJUSTMENT = "billing_adjustment"
    # STEP 9. Data-subject rights and licensing; detail carries counts and
    # plan codes only -- never personal data beyond what the action implies.
    GDPR_EXPORT = "gdpr_export"
    GDPR_ERASURE = "gdpr_erasure"
    LICENSE_ISSUED = "license_issued"


class Speaker(str, enum.Enum):
    """
    Who produced a transcript turn.

    Canonical values are USER / ASSISTANT / SYSTEM. They match the role
    vocabulary the LLM context and the text channels already use, and they
    match the `speaker` PostgreSQL type created by the baseline migration,
    so no persisted row has to be rewritten on an Alembic-managed database.
    """
    USER = "user"            # the caller / the person texting
    ASSISTANT = "assistant"  # the AI receptionist
    SYSTEM = "system"        # system notes (transfer, timeout, error)


class Tenant(Base):
    """One business = one tenant. Their phone number routes to their config."""
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200))
    industry: Mapped[str] = mapped_column(String(80), default="general")   # dental, legal, restaurant...
    twilio_number: Mapped[str] = mapped_column(String(32), unique=True, index=True)

    # Agent personality / knowledge
    agent_name: Mapped[str] = mapped_column(String(80), default="Alex")
    greeting: Mapped[str] = mapped_column(Text, default="Thanks for calling. How can I help you today?")
    system_prompt_extra: Mapped[str] = mapped_column(Text, default="")
    knowledge_base: Mapped[dict] = mapped_column(JSON, default=dict)  # {"hours": "...", "services": [...]}

    # ---- LLM choice (প্রতি ক্লায়েন্টে আলাদা AI) ----
    llm_preset: Mapped[str | None] = mapped_column(String(32), default="natural")
    llm_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)  # openai|anthropic|google
    llm_model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    temperature: Mapped[float] = mapped_column(Float, default=0.65)

    # ---- মানুষের মতো শোনানোর সেটিং ----
    humanize: Mapped[bool] = mapped_column(Boolean, default=True)
    vad_stop_secs: Mapped[float] = mapped_column(Float, default=0.45)
    speech_speed: Mapped[float] = mapped_column(Float, default=1.0)
    voice_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    language: Mapped[str] = mapped_column(String(16), default="en-US")

    # Behaviour
    timezone: Mapped[str] = mapped_column(String(64), default="America/New_York")
    business_open: Mapped[time] = mapped_column(Time, default=time(9, 0))
    business_close: Mapped[time] = mapped_column(Time, default=time(17, 0))
    appointment_minutes: Mapped[int] = mapped_column(Integer, default=30)
    escalation_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    notify_sms_number: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Integrations
    google_calendar_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    crm_webhook_url: Mapped[str | None] = mapped_column(String(500), nullable=True)   # GHL / Zapier / Make / n8n
    crm_type: Mapped[str] = mapped_column(String(32), default="webhook")              # webhook|gohighlevel|hubspot
    crm_api_key: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Outbound calling (cold calls, follow-ups, reminders)
    outbound_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    outbound_caller_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    outbound_window_open: Mapped[time] = mapped_column(Time, default=time(9, 0))      # TCPA: no calls before 8am
    outbound_window_close: Mapped[time] = mapped_column(Time, default=time(20, 0))    # TCPA: none after 9pm
    max_call_attempts: Mapped[int] = mapped_column(Integer, default=3)

    # Reminders
    reminder_hours_before: Mapped[int] = mapped_column(Integer, default=24)
    reminder_enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    # IVR / call flow (JSON so the dashboard can edit it without a deploy)
    ivr_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    ivr_flow: Mapped[dict] = mapped_column(JSON, default=dict)

    # Text channels
    sms_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    whatsapp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    whatsapp_number: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # A2P 10DLC (US SMS is silently filtered without this)
    a2p_brand_sid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    a2p_campaign_sid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    a2p_status: Mapped[str] = mapped_column(String(32), default="not_started")

    # Compliance / recording
    record_calls: Mapped[bool] = mapped_column(Boolean, default=False)
    recording_disclaimer: Mapped[str] = mapped_column(
        Text, default="This call may be recorded for quality purposes."
    )

    # Data-subject rights (STEP 9). Consent provenance is recorded when the
    # business captures opt-in; erasure_requested_at marks a GDPR erasure
    # request (set by app/api/gdpr_routes.py) for operator completion.
    data_consent_recorded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    data_consent_source: Mapped[str | None] = mapped_column(String(80), nullable=True)
    erasure_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Billing
    plan: Mapped[str] = mapped_column(String(32), default="starter")   # starter / pro
    included_minutes: Mapped[int] = mapped_column(Integer, default=500)
    minutes_used: Mapped[float] = mapped_column(Float, default=0.0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Real-call E2E readiness (STEP 5). `is_test_tenant` marks a tenant that a
    # human operator created for a genuine, manual end-to-end Twilio call. It is
    # the single source of truth the E2E guard trusts; it has no effect on
    # anything else in the product. Defaults to False so no existing tenant is
    # ever silently marked as a test tenant.
    is_test_tenant: Mapped[bool] = mapped_column(Boolean, server_default=false(), default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    calls: Mapped[list["Call"]] = relationship(back_populates="tenant")


class Call(Base):
    __tablename__ = "calls"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    call_sid: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    from_number: Mapped[str] = mapped_column(String(32))
    to_number: Mapped[str] = mapped_column(String(32))
    status: Mapped[CallStatus] = mapped_column(Enum(CallStatus), default=CallStatus.RINGING)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)

    # Outcome
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    intent: Mapped[str | None] = mapped_column(String(80), nullable=True)
    booked: Mapped[bool] = mapped_column(Boolean, default=False)
    escalated: Mapped[bool] = mapped_column(Boolean, default=False)

    # Latency stats -- your product's #1 quality metric
    avg_response_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    llm_used: Mapped[str | None] = mapped_column(String(80), nullable=True)

    direction: Mapped[CallDirection] = mapped_column(
        Enum(CallDirection), default=CallDirection.INBOUND
    )
    recording_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    lead_score: Mapped[int | None] = mapped_column(Integer, nullable=True)   # 0-100

    # Why the call ended badly ("busy", "no-answer", ...). Only set for
    # FAILED/NO_ANSWER; `summary` stays the human-readable outcome.
    failure_reason: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # --- human transfer -------------------------------------------------
    # `escalated` (above) stays as the boolean the dashboard and CRM payload
    # already read; these columns record how the escalation actually went.
    transfer_state: Mapped[TransferState] = mapped_column(
        Enum(TransferState), default=TransferState.NONE, nullable=False
    )
    transfer_destination: Mapped[str | None] = mapped_column(String(64), nullable=True)
    transfer_reason: Mapped[str | None] = mapped_column(String(400), nullable=True)
    transfer_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    transfer_error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    transfer_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    transfer_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    transfer_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    transfer_failed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    crm_synced: Mapped[bool] = mapped_column(Boolean, default=False)
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("leads.id"), nullable=True, index=True
    )

    tenant: Mapped[Tenant] = relationship(back_populates="calls")
    turns: Mapped[list["Turn"]] = relationship(back_populates="call", cascade="all, delete-orphan")


class Turn(Base):
    """One utterance. Storing these gives you transcripts + training data."""
    __tablename__ = "turns"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    call_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("calls.id"), index=True)
    speaker: Mapped[Speaker] = mapped_column(Enum(Speaker))
    text: Mapped[str] = mapped_column(Text)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    call: Mapped[Call] = relationship(back_populates="turns")


class AppointmentStatus(str, enum.Enum):
    """
    Lifecycle of one booking.

    Before STEP 6 there was no status at all: a row existed or it did not, so
    there was no way to cancel, to record a no-show, or -- most importantly --
    to distinguish "the provider accepted this" from "we wrote a row and hoped".

    PENDING    persisted, provider has not accepted (yet, or ever)
    CONFIRMED  the provider accepted and returned an event id
    RESCHEDULED  moved; `starts_at` is the new time
    CANCELLED  cancelled by anyone
    NO_SHOW    the customer did not arrive
    FAILED     the provider refused permanently; nothing is on the calendar
    """
    PENDING = "pending"
    CONFIRMED = "confirmed"
    RESCHEDULED = "rescheduled"
    CANCELLED = "cancelled"
    NO_SHOW = "no_show"
    FAILED = "failed"


#: Statuses that still occupy their slot. Anything outside this set frees the
#: time for someone else, so it is the definition the conflict check uses --
#: named here rather than inlined, because "does a cancelled appointment still
#: block the slot" is a policy question and it should have one answer.
BLOCKING_APPOINTMENT_STATUSES = frozenset({
    AppointmentStatus.PENDING,
    AppointmentStatus.CONFIRMED,
    AppointmentStatus.RESCHEDULED,
})

#: Terminal: no further provider call will be made for these.
TERMINAL_APPOINTMENT_STATUSES = frozenset({
    AppointmentStatus.CANCELLED,
    AppointmentStatus.NO_SHOW,
    AppointmentStatus.FAILED,
})


class CalendarProviderType(str, enum.Enum):
    """
    Calendar backends with an adapter.

    `GOOGLE_SERVICE_ACCOUNT` is the pre-STEP-6 path, kept as a distinct member
    rather than folded into `GOOGLE`: it authenticates with a platform-wide
    service-account file instead of per-tenant OAuth, which is a different
    security posture and a different set of failure modes. Merging them would
    hide which tenants are still on the shared credential.
    """
    GOOGLE = "google"
    GOOGLE_SERVICE_ACCOUNT = "google_service_account"
    MICROSOFT = "microsoft"
    CALCOM = "calcom"
    INTERNAL = "internal"


class Appointment(Base):
    """
    One booking.

    Columns added in STEP 6 are all nullable or defaulted, so existing rows
    keep working: `status` defaults to CONFIRMED for them (they were created
    under the old code, which only ever wrote a row it believed in), and
    `timezone` is backfilled from the tenant.
    """
    __tablename__ = "appointments"
    __table_args__ = (
        # The concurrency primitive. Two callers racing for the same slot both
        # compute the same key, so the second INSERT loses at the database
        # rather than in application logic. See `service.book`.
        UniqueConstraint(
            "tenant_id", "slot_key", name="uq_appointment_slot"
        ),
        # Requirement 9: a retried booking request must find its own earlier
        # attempt rather than creating a second appointment.
        UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_appointment_idempotency"
        ),
        Index("ix_appointment_tenant_start", "tenant_id", "starts_at"),
        Index("ix_appointment_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    call_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("calls.id"), nullable=True)

    customer_name: Mapped[str] = mapped_column(String(200))
    customer_phone: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text, default="")
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    google_event_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    # ---- STEP 6 ----------------------------------------------------------

    status: Mapped[AppointmentStatus] = mapped_column(
        Enum(AppointmentStatus), default=AppointmentStatus.PENDING, nullable=False
    )

    #: The IANA zone the customer agreed to, captured at booking time.
    #:
    #: Not derivable from `Tenant.timezone` after the fact: a business that
    #: relocates, or corrects a wrong timezone, would otherwise silently
    #: reinterpret every appointment already in the book. The offset alone is
    #: not enough either -- it does not survive a DST boundary.
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)

    provider: Mapped[CalendarProviderType | None] = mapped_column(
        Enum(CalendarProviderType), nullable=True
    )
    #: The provider's own event id. Proof that the booking was accepted; a row
    #: is only CONFIRMED when this is set.
    external_event_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: Which calendar within the provider (Google calendar id, Graph calendar
    #: id, Cal.com event-type id).
    calendar_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)

    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("leads.id", ondelete="SET NULL"), nullable=True
    )
    attendee_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    meeting_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    #: Deterministic `{start}|{end}` in UTC. The unique constraint above turns
    #: it into a slot lock. Nullable so pre-STEP-6 rows do not all collide on
    #: NULL -- in both PostgreSQL and SQLite, NULLs are distinct in a UNIQUE
    #: index, which is exactly the behaviour needed for a backfill.
    slot_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Stable across retries of one booking request.
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)

    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancellation_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    #: Free text rather than a FK: the canceller may be a staff user, the
    #: customer on a call, or the provider itself via a webhook.
    cancelled_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    rescheduled_from: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: Scrubbed before storage. Shown to staff, so never a raw provider body.
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow,
        nullable=False,
    )


class Campaign(Base):
    """A batch of outbound calls: cold-call list, follow-up sweep, reminder run."""
    __tablename__ = "campaigns"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    goal: Mapped[str] = mapped_column(String(32), default="qualify")   # qualify|remind|followup|survey
    script_prompt: Mapped[str] = mapped_column(Text, default="")
    opening_line: Mapped[str] = mapped_column(
        Text, default="Hi, this is {agent} calling from {business}. Do you have a quick minute?"
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    calls_per_minute: Mapped[int] = mapped_column(Integer, default=2)   # throttle
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    leads: Mapped[list["Lead"]] = relationship(back_populates="campaign")


class Lead(Base):
    """A person to call. Feeds outbound campaigns and gets pushed to the CRM."""
    __tablename__ = "leads"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("campaigns.id"), nullable=True, index=True
    )

    name: Mapped[str] = mapped_column(String(200), default="")
    phone: Mapped[str] = mapped_column(String(32), index=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    company: Mapped[str | None] = mapped_column(String(200), nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    custom_fields: Mapped[dict] = mapped_column(JSON, default=dict)

    status: Mapped[LeadStatus] = mapped_column(Enum(LeadStatus), default=LeadStatus.NEW)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    campaign: Mapped["Campaign | None"] = relationship(back_populates="leads")


class Reminder(Base):
    """Scheduled outbound reminder for an appointment. Cuts no-shows ~30%."""
    __tablename__ = "reminders"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    appointment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("appointments.id"), index=True)
    channel: Mapped[str] = mapped_column(String(16), default="sms")   # sms|call|whatsapp
    send_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    sent: Mapped[bool] = mapped_column(Boolean, default=False)
    confirmed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Step 6 (scale-compliance): a durable send lease. The worker that wins the
    # atomic claim (see `app/integrations/reminders.py`) stamps these before
    # sending; a crashed worker's lease is reclaimed by the reaper. Without
    # this, two overlapping ticks (or a crash between the SMS send and the
    # commit) would text the customer twice.
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claimed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


# =============================================================================
# Authentication / RBAC
# =============================================================================

class User(Base):
    """
    A human operator. Always belongs to exactly one tenant -- that binding is
    the root of tenant isolation and is never taken from a request.

    Email is unique GLOBALLY, not per tenant. Login is by email alone with no
    tenant selector, so a duplicate address across tenants would make
    authentication ambiguous. Documented in README under "Tenant isolation".
    """
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("email", name="uq_users_email"),
        Index("ix_users_tenant_role", "tenant_id", "role"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )

    email: Mapped[str] = mapped_column(String(320), nullable=False)   # RFC 5321 max
    full_name: Mapped[str] = mapped_column(String(200), default="")

    # bcrypt output. Never exposed by any serializer -- see UserOut.
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    role: Mapped[UserRole] = mapped_column(Enum(UserRole), default=UserRole.VIEWER)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Bumping this invalidates every access token issued earlier for this user,
    # which is how deactivation and role changes take effect before expiry.
    token_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    failed_login_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow,
        nullable=False,
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    tenant: Mapped["Tenant"] = relationship()


class RefreshToken(Base):
    """
    Only a SHA-256 digest of the refresh token is stored, so a database leak
    does not hand out sessions. Rotation is enforced: using a token marks it
    used and links the replacement, and replaying a used token revokes the
    whole chain (a standard reuse-detection scheme).
    """
    __tablename__ = "refresh_tokens"
    __table_args__ = (
        Index("ix_refresh_active", "user_id", "revoked_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)

    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    replaced_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    user_agent: Mapped[str] = mapped_column(String(300), default="")
    ip_address: Mapped[str] = mapped_column(String(64), default="")


class AuditLog(Base):
    """
    Security events. Deliberately holds no secret material: no passwords, no
    raw tokens, no API keys. `detail` is free-form JSON for non-sensitive
    context such as which role changed to what.
    """
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_tenant_time", "tenant_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True, index=True
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    target_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    action: Mapped[AuditAction] = mapped_column(Enum(AuditAction), nullable=False)
    # Stored even on failed logins, so it must never be a real credential.
    actor_email: Mapped[str] = mapped_column(String(320), default="")
    ip_address: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(300), default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, index=True, nullable=False
    )


# ============================================================ knowledge ===
#
# STEP 4: tenant-scoped RAG. `Tenant.knowledge_base` (the flat JSON dict) is
# kept and still works -- it holds the handful of one-line facts a business
# types into the dashboard, and those are cheap enough to sit in the system
# prompt. Documents are the new thing: too large to inline, so they are
# chunked, embedded, and retrieved a few chunks at a time.
#
# Uploading a document does NOT mean the agent may answer from all of it. Only
# chunks returned by a retrieval query ever reach the model.


class DocumentStatus(str, enum.Enum):
    """
    Ingestion lifecycle.

    Only READY is searchable. Everything else -- including ARCHIVED -- is
    excluded from retrieval at the SQL level, not filtered afterwards.

    NOTE: SQLAlchemy's Enum() persists the member NAME, so the PostgreSQL type
    `documentstatus` must contain UPLOADED/PROCESSING/READY/FAILED/ARCHIVED.
    """
    UPLOADED = "uploaded"       # stored, not yet processed
    PROCESSING = "processing"   # extraction/chunking/embedding in flight
    READY = "ready"             # searchable
    FAILED = "failed"           # ingestion failed; error_message explains
    ARCHIVED = "archived"       # soft-deleted; never retrieved


#: The only status whose chunks may be returned by a search.
SEARCHABLE_DOCUMENT_STATUSES = frozenset({DocumentStatus.READY})


class DocumentSourceType(str, enum.Enum):
    UPLOAD = "upload"           # a file the tenant uploaded
    TEXT = "text"               # pasted directly into the dashboard
    URL = "url"                 # fetched from a URL (not implemented yet)


class KnowledgeDocument(Base):
    """One uploaded source document belonging to exactly one tenant."""

    __tablename__ = "knowledge_documents"
    __table_args__ = (
        # Deduplication is per tenant: two businesses may legitimately upload
        # the same price list, and that must not collide.
        UniqueConstraint("tenant_id", "content_hash", name="uq_knowledge_doc_hash"),
        Index("ix_knowledge_doc_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    source_type: Mapped[DocumentSourceType] = mapped_column(
        Enum(DocumentSourceType), default=DocumentSourceType.UPLOAD, nullable=False
    )
    #: Storage key, NOT a filesystem path. Resolved by the storage backend so
    #: nothing outside app/knowledge/storage knows where bytes actually live.
    source_uri: Mapped[str | None] = mapped_column(String(500), nullable=True)
    original_filename: Mapped[str | None] = mapped_column(String(300), nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    file_size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    #: SHA-256 of the raw bytes. Drives per-tenant deduplication.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus), default=DocumentStatus.UPLOADED, nullable=False
    )
    #: Bumped on every reindex. Chunks record the version they were built from,
    #: so a half-finished reindex can never mix old and new chunks.
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    #: Which embedding model produced this document's vectors. If it stops
    #: matching the configured model the document is not searchable until it
    #: is reindexed -- mixing vector spaces silently returns nonsense.
    embedding_model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    embedding_dimensions: Mapped[int | None] = mapped_column(Integer, nullable=True)

    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    token_estimate: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    #: Free-form, tenant-visible. Never holds credentials or storage paths.
    doc_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    #: Safe, human-readable failure summary. Never a raw stack trace.
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow,
        onupdate=datetime.utcnow, nullable=False,
    )
    ingested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Set when PROCESSING begins, so a stuck job can be detected and reaped.
    processing_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    chunks: Mapped[list["KnowledgeChunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )

    @property
    def is_searchable(self) -> bool:
        return self.status in SEARCHABLE_DOCUMENT_STATUSES


class KnowledgeChunk(Base):
    """
    One retrievable passage.

    `tenant_id` is denormalised onto the chunk on purpose. Retrieval filters on
    it directly in the WHERE clause, so a bug in a join can never widen the
    result set past one tenant.
    """

    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        UniqueConstraint(
            "document_id", "version", "chunk_index", name="uq_knowledge_chunk_slot"
        ),
        Index("ix_knowledge_chunk_tenant_doc", "tenant_id", "document_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"), index=True, nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )

    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    token_estimate: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    #: Page number, heading trail, CSV row range -- whatever the extractor knew.
    chunk_metadata: Mapped[dict] = mapped_column(JSON, default=dict)

    #: The vector. Stored as JSON so the same code runs on SQLite in tests and
    #: on PostgreSQL in production; app/knowledge/vectorstore.py upgrades to a
    #: real pgvector column when the extension is available.
    embedding: Mapped[list | None] = mapped_column(JSON, nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(120), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )

    document: Mapped[KnowledgeDocument] = relationship(back_populates="chunks")


# ===========================================================================
# STEP 5 -- CRM integration layer
#
# The pre-existing CRM support was three columns on `Tenant`
# (`crm_webhook_url`, `crm_type`, `crm_api_key`) and one best-effort POST.
# Those columns are deliberately left in place: `tests/test_outbound.py` still
# exercises the legacy helper, and a migration that drops a column holding a
# live tenant's webhook URL is not something to do in the same change that
# introduces its replacement. Nothing in the new layer reads them.
#
# Five new tables, and the reason each one is separate rather than folded into
# an existing row:
#
# * `CrmIntegration` -- per (tenant, provider) configuration and credentials.
#   Not on `Tenant`, because a tenant may connect several providers at once
#   and because credentials need their own encrypted column with its own
#   access pattern.
# * `CrmEvent`       -- the normalized business event. Persisted *before* any
#   provider is contacted, so a crash between "the call ended" and "the CRM
#   accepted it" loses nothing.
# * `CrmSync`        -- one delivery attempt-set per (event, integration). Fan
#   out to three providers is three rows, so one provider being down cannot
#   mark the others failed.
# * `CrmContactLink` -- the (tenant, provider, identity) -> external id map.
#   This is what stops a second call from the same phone number creating a
#   second contact.
# * `CrmWebhookReceipt` -- inbound replay protection.
# ===========================================================================

class CrmProviderType(str, enum.Enum):
    """
    Providers with an adapter. A closed enum on purpose: the old `crm_type`
    was a free string, so a typo silently selected the generic branch instead
    of failing.
    """
    GOHIGHLEVEL = "gohighlevel"
    HUBSPOT = "hubspot"
    JOBBER = "jobber"
    WEBHOOK = "webhook"


class CrmEntityType(str, enum.Enum):
    CALL = "call"
    LEAD = "lead"
    APPOINTMENT = "appointment"


class CrmEventType(str, enum.Enum):
    """
    Normalized business events.

    The values are dotted wire strings rather than the lowercase-of-the-name
    convention the knowledge enums follow, because these values are published:
    they appear in outbound webhook bodies and in tenant-facing filters.
    `lead.created` is a documented part of the integration contract, so it is
    the value, and the enum member name is what the database stores.
    """
    CALL_COMPLETED = "call.completed"
    CALL_MISSED = "call.missed"
    LEAD_CREATED = "lead.created"
    LEAD_UPDATED = "lead.updated"
    APPOINTMENT_BOOKED = "appointment.booked"
    APPOINTMENT_CANCELLED = "appointment.cancelled"
    TRANSFER_COMPLETED = "transfer.completed"


class CrmSyncStatus(str, enum.Enum):
    """
    PENDING            queued, not yet picked up
    PROCESSING         a worker holds it right now
    SYNCED             the provider acknowledged the write
    FAILED             transient failure, attempts remain
    PERMANENT_FAILURE  will not be retried without human action
    """
    PENDING = "pending"
    PROCESSING = "processing"
    SYNCED = "synced"
    FAILED = "failed"
    PERMANENT_FAILURE = "permanent_failure"


#: Statuses a worker may pick up. `SYNCED` and `PERMANENT_FAILURE` are
#: terminal; `PROCESSING` is excluded so two workers cannot claim one row, and
#: is recovered by the stuck-sync reaper instead.
RETRYABLE_SYNC_STATUSES = frozenset({CrmSyncStatus.PENDING, CrmSyncStatus.FAILED})
TERMINAL_SYNC_STATUSES = frozenset(
    {CrmSyncStatus.SYNCED, CrmSyncStatus.PERMANENT_FAILURE}
)


class CrmIntegration(Base):
    """
    One tenant's connection to one provider.

    `credentials_encrypted` is ciphertext produced by
    `app.integrations.crm.crypto`. It is never selected into an API response --
    the response models in `app/api/integration_routes.py` are allowlists, and
    there is a test asserting the column name does not appear in any response
    body.
    """
    __tablename__ = "crm_integrations"
    __table_args__ = (
        # The isolation primitive. Lookups are (tenant_id, provider); this
        # constraint is what makes that pair a key rather than a filter.
        UniqueConstraint("tenant_id", "provider", name="uq_crm_integration_tenant_provider"),
        Index("ix_crm_integration_tenant_enabled", "tenant_id", "is_enabled"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    provider: Mapped[CrmProviderType] = mapped_column(
        Enum(CrmProviderType), nullable=False
    )

    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    #: AES-GCM ciphertext of a JSON credential bundle. Opaque here on purpose.
    credentials_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Which key encrypted it, so keys can be rotated without a flag day.
    credentials_key_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Set when credentials change, so "connected but never tested" is visible.
    credentials_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: Non-secret provider settings: GHL location id, HubSpot pipeline, the
    #: outbound webhook URL. Safe to return from the API.
    config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    #: Tenant-defined mapping of VoxDesk fields to provider custom fields.
    field_mappings: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    #: Which normalized events this integration wants. Empty = all of them.
    subscribed_events: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    #: Requirement 18: transcripts are not shared unless asked for.
    share_transcripts: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    last_health_check_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_health_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    #: Operator-facing, already scrubbed of anything secret.
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow,
        nullable=False,
    )


class CrmEvent(Base):
    """
    A business fact, recorded once, independent of any provider.

    Written inside the same transaction as the thing that caused it. Delivery
    is a separate concern with separate rows, which is what makes "the CRM was
    down when the call ended" a recoverable situation rather than a lost lead.
    """
    __tablename__ = "crm_events"
    __table_args__ = (
        # The idempotency primitive. Scoped to the tenant so two tenants can
        # never collide, and so a key from one tenant cannot suppress
        # another's event.
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_crm_event_idempotency"),
        Index("ix_crm_event_tenant_created", "tenant_id", "created_at"),
        Index("ix_crm_event_entity", "tenant_id", "entity_type", "entity_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )

    event_type: Mapped[CrmEventType] = mapped_column(Enum(CrmEventType), nullable=False)
    entity_type: Mapped[CrmEntityType] = mapped_column(Enum(CrmEntityType), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    #: Deterministic; see `events.idempotency_key`. Same business fact, same
    #: key, forever.
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)

    #: The normalized payload, already scrubbed. Versioned so an adapter can
    #: tell an old row from a new one after a schema change.
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    payload_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )


class CrmSync(Base):
    """
    Delivery state for one event to one integration.

    `external_id` is written in the same commit that sets SYNCED, so a row can
    never claim success without recording what the provider created.
    """
    __tablename__ = "crm_syncs"
    __table_args__ = (
        # One delivery record per (event, integration). This is the constraint
        # that makes a duplicate worker pass a no-op rather than a second POST.
        UniqueConstraint("event_id", "integration_id", name="uq_crm_sync_event_integration"),
        Index("ix_crm_sync_due", "status", "next_attempt_at"),
        Index("ix_crm_sync_tenant_status", "tenant_id", "status"),
        Index("ix_crm_sync_entity", "tenant_id", "entity_type", "entity_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("crm_events.id", ondelete="CASCADE"), index=True, nullable=False
    )
    integration_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("crm_integrations.id", ondelete="CASCADE"), index=True, nullable=False
    )

    provider: Mapped[CrmProviderType] = mapped_column(Enum(CrmProviderType), nullable=False)
    entity_type: Mapped[CrmEntityType] = mapped_column(Enum(CrmEntityType), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    status: Mapped[CrmSyncStatus] = mapped_column(
        Enum(CrmSyncStatus), default=CrmSyncStatus.PENDING, nullable=False
    )
    #: What the provider created or updated. Proof of the SYNCED claim.
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: Scrubbed before storage -- see `errors.safe_message`. Shown to the
    #: tenant, so it must never contain a token or a raw provider body.
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    #: Normalized error class (`rate_limited`, `unauthorized`, ...), for
    #: dashboards that want to group failures without parsing prose.
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow,
        nullable=False,
    )


class CrmContactLink(Base):
    """
    (tenant, provider, identity) -> provider contact id.

    The identity is a normalized phone or email, hashed, never the raw value:
    this table exists to be looked up quickly and it should not become a
    second copy of the customer list.

    Requirement 19's "never match contacts across tenants" is enforced by
    `tenant_id` being the first column of the unique constraint, not by
    application code remembering to filter.
    """
    __tablename__ = "crm_contact_links"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "provider", "identity_hash", name="uq_crm_contact_identity"
        ),
        Index("ix_crm_contact_tenant_provider", "tenant_id", "provider"),
        Index("ix_crm_contact_lead", "tenant_id", "lead_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    provider: Mapped[CrmProviderType] = mapped_column(Enum(CrmProviderType), nullable=False)

    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("leads.id", ondelete="SET NULL"), nullable=True
    )
    #: sha256 of the normalized identity, salted per tenant.
    identity_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    external_contact_id: Mapped[str] = mapped_column(String(255), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow,
        nullable=False,
    )


class CrmWebhookReceipt(Base):
    """
    Inbound provider events we have already processed.

    Requirement 21 needs replay protection, and replay protection needs
    somewhere durable to remember event ids. Rows are pruned by age, not kept
    forever.
    """
    __tablename__ = "crm_webhook_receipts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "provider", "provider_event_id", name="uq_crm_receipt_event"
        ),
        Index("ix_crm_receipt_received", "received_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    provider: Mapped[CrmProviderType] = mapped_column(Enum(CrmProviderType), nullable=False)
    provider_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )


class MessageWebhookReceipt(Base):
    """
    Inbound Twilio message events (SMS/WhatsApp) we have already processed.

    The messaging channel was the one inbound webhook without durable replay
    protection: a redelivered message would have been appended as a second turn
    and answered a second time (extra LLM spend + a duplicate reply SMS). The
    unique constraint on `(tenant_id, channel, provider_message_id)` turns the
    redelivery into a no-op. Rows are pruned by age, not kept forever.
    """
    __tablename__ = "message_webhook_receipts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "channel", "provider_message_id",
            name="uq_message_receipt_event",
        ),
        Index("ix_message_receipt_received", "received_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    channel: Mapped[str] = mapped_column(String(16), nullable=False)   # sms | whatsapp
    provider_message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )


# ===========================================================================
# STEP 6 -- calendar integration layer
#
# Three tables. The split mirrors STEP 5's, and for the same reason: what a
# tenant configured, what policy applies, and what the provider told us are
# three different lifetimes.
#
# * `CalendarIntegration`  -- per (tenant, provider) connection + encrypted
#   OAuth credentials. Reuses STEP 5's AES-GCM envelope, including its
#   (tenant, provider) associated data.
# * `SchedulingPolicy`     -- business hours, breaks, holidays, buffers.
#   Business policy is *not* provider availability; both are checked, so both
#   need somewhere to live.
# * `CalendarWebhookReceipt` -- inbound provider notification dedupe.
# ===========================================================================

class CalendarIntegration(Base):
    """
    One tenant's connection to one calendar provider.

    Note what is *not* here: a `google_calendar_id` equivalent on `Tenant`.
    That column still exists and still works for the legacy service-account
    path; this table is what a tenant gets when they connect through OAuth.
    """
    __tablename__ = "calendar_integrations"
    __table_args__ = (
        # Same isolation primitive as STEP 5: the pair is a key, not a filter.
        UniqueConstraint(
            "tenant_id", "provider", name="uq_calendar_integration_tenant_provider"
        ),
        Index("ix_calendar_integration_tenant_enabled", "tenant_id", "is_enabled"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    provider: Mapped[CalendarProviderType] = mapped_column(
        Enum(CalendarProviderType), nullable=False
    )

    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: Which provider a booking goes to when the tenant has several connected.
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    #: AES-256-GCM envelope over {"access_token", "refresh_token", ...}.
    #: Identical scheme to CRM credentials -- one cipher, one key ring, one
    #: rotation story. Opaque here.
    credentials_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    credentials_key_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    credentials_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: When the access token stops working. Stored in the clear because it is
    #: not secret and the refresh scheduler needs to query on it.
    token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: Non-secret settings: calendar id, Cal.com event-type id, base_url.
    config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    last_health_check_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_health_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow,
        nullable=False,
    )


class SchedulingPolicy(Base):
    """
    A tenant's booking rules.

    One row per tenant. Separate from `Tenant` because the pre-STEP-6
    `business_open` / `business_close` / `appointment_minutes` columns are read
    by existing code paths and tests, and widening `Tenant` with fifteen more
    scheduling columns would make an already-large table the home of a second
    subsystem.

    `Tenant`'s three columns remain the fallback: a tenant with no policy row
    behaves exactly as it did before STEP 6.
    """
    __tablename__ = "scheduling_policies"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_scheduling_policy_tenant"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )

    #: `{"mon": [["09:00", "12:00"], ["13:00", "17:00"]], "sun": []}`
    #: A list of intervals rather than one open/close pair, because a lunch
    #: break is the single most common reason a booking lands when nobody is
    #: there. An empty list means closed that day.
    weekly_hours: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    #: `["2026-12-25", "2026-01-01"]` -- full-day closures.
    holidays: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    #: `[{"start": "2026-07-01T00:00:00", "end": "2026-07-14T23:59:59"}]`
    #: Local wall-clock. Vacations, refits, a one-off closure.
    blocked_periods: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    slot_minutes: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    #: How far apart candidate slots start. Distinct from duration: a 60-minute
    #: appointment offered on a 30-minute grid gives twice the choice.
    slot_interval_minutes: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    buffer_before_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    buffer_after_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: The soonest a caller may book. Zero means "in one second", which is
    #: what the pre-STEP-6 code allowed.
    minimum_notice_minutes: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    #: The furthest ahead. Stops a caller booking in 2071.
    booking_horizon_days: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    #: How many options the voice agent reads aloud. Reading twelve kills a call.
    max_slots_offered: Mapped[int] = mapped_column(Integer, default=3, nullable=False)

    #: Escape hatch for businesses that genuinely take out-of-hours bookings.
    #: Off by default: requirement 5 says do not book outside business hours
    #: "unless explicitly enabled".
    allow_outside_business_hours: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    #: When the provider cannot be reached, refuse to book rather than
    #: guessing. Default True -- see the audit's F2.
    require_provider_confirmation: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow,
        nullable=False,
    )


class CalendarWebhookReceipt(Base):
    """
    Inbound calendar notifications already processed.

    Same shape and same reasoning as `CrmWebhookReceipt`: replay protection
    needs somewhere durable to remember provider event ids, and rows are
    pruned by age rather than kept forever.
    """
    __tablename__ = "calendar_webhook_receipts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "provider", "provider_event_id",
            name="uq_calendar_receipt_event",
        ),
        Index("ix_calendar_receipt_received", "received_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    provider: Mapped[CalendarProviderType] = mapped_column(
        Enum(CalendarProviderType), nullable=False
    )
    provider_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )


# ===========================================================================
# STEP 7 -- billing, subscriptions and usage metering
#
# The pre-STEP-7 billing was four columns on `Tenant` and one `+=` on a float.
# Those columns stay (see `docs/BILLING-AUDIT.md`): `minutes_used` is demoted
# from *authority* to *cache*, kept in sync so the dashboard field and any
# external reader keep working, while the number that decides an invoice is
# derived from immutable `UsageEvent` rows.
#
# Five new tables. The split is not decoration -- each has a different
# lifetime and a different trust level:
#
# * `BillingPlan`   -- the catalogue. Server-owned. A tenant never names a
#   price, only a plan code, and the code is resolved here.
# * `Subscription`  -- per (tenant, provider) state, mirrored from the
#   provider. Never authoritative on its own: only a verified webhook or a
#   direct provider read may set it.
# * `UsageEvent`    -- immutable, append-only, idempotency-keyed. The
#   financial record of truth.
# * `UsageSummary`  -- a derived rollup per (tenant, period, metric). A cache
#   that can always be rebuilt from events, which is the property that makes
#   a metering bug recoverable.
# * `BillingWebhookReceipt` -- provider event dedupe and ordering.
# ===========================================================================

class BillingProviderType(str, enum.Enum):
    """
    Billing backends with an adapter.

    `MANUAL` is not a placeholder: it is how a tenant on an invoice-me
    contract, or a development instance with no Stripe account, still gets
    plans, entitlements and metering. Everything except the payment rail works
    identically.
    """
    STRIPE = "stripe"
    MANUAL = "manual"


class SubscriptionStatus(str, enum.Enum):
    """
    Normalized subscription state.

    Stripe's own vocabulary is mapped into this inside
    `app/billing/providers/stripe.py` and nowhere else. The brief is explicit
    that Stripe status strings must not be scattered through business logic,
    and the practical reason is that Stripe has changed them before --
    `incomplete_expired` did not always exist.

    CANCELING is ours, not Stripe's: Stripe expresses "cancel at period end"
    as `active` plus a boolean, which loses the distinction every UI needs.
    """
    TRIALING = "trialing"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCELING = "canceling"
    CANCELED = "canceled"
    INCOMPLETE = "incomplete"
    INCOMPLETE_EXPIRED = "incomplete_expired"
    PAUSED = "paused"


#: Statuses that entitle a tenant to use the product. `PAST_DUE` is included
#: deliberately -- see `app/billing/entitlements.py`. Cutting a business off
#: the instant a card expires costs far more goodwill than the few days of
#: service it saves, and Stripe is still retrying the payment.
ENTITLED_SUBSCRIPTION_STATUSES = frozenset({
    SubscriptionStatus.TRIALING,
    SubscriptionStatus.ACTIVE,
    SubscriptionStatus.CANCELING,
    SubscriptionStatus.PAST_DUE,
})

#: No further provider transition is expected.
TERMINAL_SUBSCRIPTION_STATUSES = frozenset({
    SubscriptionStatus.CANCELED,
    SubscriptionStatus.INCOMPLETE_EXPIRED,
})


class BillingInterval(str, enum.Enum):
    MONTH = "month"
    YEAR = "year"


class UsageMetric(str, enum.Enum):
    """
    What is counted.

    Values are the wire names used in the API and in plan configuration, so
    they are part of the published contract.
    """
    VOICE_MINUTE = "voice_minute"
    SMS_SEGMENT = "sms_segment"
    LLM_TOKEN = "llm_token"
    TTS_CHARACTER = "tts_character"


class UsageEventType(str, enum.Enum):
    """
    Why something was counted.

    Distinct from `UsageMetric` because one metric has several sources -- an
    inbound call, an outbound campaign call and a transfer leg all produce
    VOICE_MINUTE -- and the source is what makes a disputed invoice
    answerable.
    """
    VOICE_MINUTE_USED = "voice_minute_used"
    SMS_SEGMENT_USED = "sms_segment_used"
    LLM_TOKEN_USED = "llm_token_used"
    TTS_CHARACTER_USED = "tts_character_used"
    #: A signed correction. Never a destructive edit -- see `UsageEvent`.
    MANUAL_ADJUSTMENT = "manual_adjustment"


class InvoiceStatus(str, enum.Enum):
    DRAFT = "draft"
    OPEN = "open"
    PAID = "paid"
    UNCOLLECTIBLE = "uncollectible"
    VOID = "void"


class BillingPlan(Base):
    """
    The plan catalogue. Server-owned, and the reason a browser can never name
    a price.

    Prices are integer **minor units** (cents), not floats. A float `19.99`
    is not exactly 19.99, and accumulating overage across a few thousand
    fractional minutes in binary floating point produces invoices that do not
    reconcile with themselves. Money is counted, not measured.
    """
    __tablename__ = "billing_plans"
    __table_args__ = (
        UniqueConstraint("code", name="uq_billing_plan_code"),
        Index("ix_billing_plan_active", "is_active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    #: Stable identifier used by the API and by config. Never renamed.
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: Ordering for a pricing page. Not billing-relevant.
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    currency: Mapped[str] = mapped_column(String(3), default="usd", nullable=False)
    monthly_price_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    annual_price_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)

    included_voice_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    included_sms_segments: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    included_llm_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    included_tts_characters: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    #: Overage rates, in **hundredths of a cent** per unit.
    #:
    #: A voice minute at 5c is 500 here; an LLM token at $2/million is 0.0002c,
    #: which cents cannot express at all. Sub-cent granularity is not
    #: fastidiousness -- token and character pricing is genuinely below one
    #: cent per unit, and rounding each unit to a cent would overcharge by
    #: several orders of magnitude.
    overage_voice_minute_millicents: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    overage_sms_millicents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    overage_llm_token_millicents: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    overage_tts_character_millicents: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )

    #: When false, exceeding the included allowance is refused rather than
    #: metered. Requirement 26: the check happens *before* the provider call.
    overage_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    #: Non-metered limits: {"team_members": 5, "rag_documents": 100, ...}.
    #: JSON rather than columns because these are the ones that change per
    #: sales conversation, and adding a column per feature would mean a
    #: migration every time someone negotiates.
    feature_entitlements: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    #: Provider price identifiers, keyed by interval:
    #: {"month": "price_FAKE123", "year": "price_FAKE456"}.
    #: **This is the trust boundary.** A checkout request names a plan code
    #: and an interval; the price id comes from here and never from a client.
    provider_price_ids: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    trial_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow,
        nullable=False,
    )


class Subscription(Base):
    """
    One tenant's subscription with one provider.

    `provider_updated_at` and `provider_event_sequence` exist for requirement
    23. Provider webhooks arrive out of order routinely -- `subscription.updated`
    can land before `checkout.session.completed` -- and applying a stale event
    would downgrade a customer who just upgraded. Every write compares
    timestamps first.
    """
    __tablename__ = "subscriptions"
    __table_args__ = (
        # One subscription per (tenant, provider). The isolation primitive,
        # and what makes "the tenant's subscription" a well-defined phrase.
        UniqueConstraint("tenant_id", "provider", name="uq_subscription_tenant_provider"),
        # A provider subscription id belongs to exactly one tenant. Without
        # this, a webhook carrying an id could be matched to the wrong row.
        UniqueConstraint(
            "provider", "external_subscription_id",
            name="uq_subscription_external_id",
        ),
        Index("ix_subscription_tenant_status", "tenant_id", "status"),
        Index("ix_subscription_period_end", "current_period_end"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    plan_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("billing_plans.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    provider: Mapped[BillingProviderType] = mapped_column(
        Enum(BillingProviderType), nullable=False
    )

    external_customer_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    external_subscription_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: Which price the provider actually billed. Recorded so a mismatch with
    #: the plan's configured price is detectable rather than invisible.
    external_price_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    status: Mapped[SubscriptionStatus] = mapped_column(
        Enum(SubscriptionStatus), default=SubscriptionStatus.INCOMPLETE, nullable=False
    )
    interval: Mapped[BillingInterval] = mapped_column(
        Enum(BillingInterval), default=BillingInterval.MONTH, nullable=False
    )

    current_period_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    current_period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    trial_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    trial_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    cancel_at_period_end: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    canceled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: A downgrade that takes effect at renewal. Requirement 18 asks for an
    #: unambiguous policy: upgrades are immediate, downgrades are scheduled,
    #: and this is where a scheduled one waits.
    pending_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("billing_plans.id", ondelete="SET NULL"), nullable=True
    )
    pending_interval: Mapped[BillingInterval | None] = mapped_column(
        Enum(BillingInterval), nullable=True
    )

    #: The provider's own clock for the last state we applied. Ordering
    #: authority -- see the class docstring.
    provider_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    last_invoice_status: Mapped[InvoiceStatus | None] = mapped_column(
        Enum(InvoiceStatus), nullable=True
    )
    last_payment_failed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Scrubbed before storage. Shown to staff, never a raw provider body.
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow,
        nullable=False,
    )


class UsageEvent(Base):
    """
    One immutable, idempotency-keyed unit of consumption.

    **Append-only.** Nothing in `app/billing` updates or deletes a row here.
    A correction is a new row with a negative `quantity` and
    `event_type = MANUAL_ADJUSTMENT`, which is requirement 35's "never modify
    historical usage quantities destructively" -- and also the only way an
    invoice dispute can ever be answered, because the original figure survives
    alongside the correction.

    `quantity` is an integer in the metric's smallest unit: **seconds** for
    voice, segments for SMS, tokens, characters. Not minutes. Storing 1.5
    minutes as a float and summing a few thousand of them is how a total stops
    matching the sum of its parts.
    """
    __tablename__ = "usage_events"
    __table_args__ = (
        # The idempotency guarantee, at the database rather than in Python.
        # Requirement 14 asks for exactly this backstop.
        UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_usage_event_idempotency"
        ),
        # The aggregation query: everything for a tenant, period and metric.
        Index("ix_usage_event_rollup", "tenant_id", "billing_period", "metric"),
        Index("ix_usage_event_source", "tenant_id", "source_entity_id"),
        Index("ix_usage_event_created", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )

    #: `YYYY-MM` of the *billing* period, not the calendar month. Derived from
    #: the subscription's anchor where one exists -- see `billing/periods.py`.
    #: A string because it is a label, compared and grouped, never arithmetic.
    billing_period: Mapped[str] = mapped_column(String(16), nullable=False)

    metric: Mapped[UsageMetric] = mapped_column(Enum(UsageMetric), nullable=False)
    event_type: Mapped[UsageEventType] = mapped_column(
        Enum(UsageEventType), nullable=False
    )

    #: The call, message or document this came from. Makes an invoice line
    #: traceable back to the thing the customer actually did.
    source_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    source_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)

    #: Signed. Negative only for MANUAL_ADJUSTMENT.
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit: Mapped[str] = mapped_column(String(32), nullable=False)

    #: Derived from the business fact, never generated per attempt. See
    #: `billing/metering.py::usage_idempotency_key`.
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)

    #: Safe context: direction, whether the call was transferred, which leg.
    #: Never a transcript, never a credential.
    event_metadata: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )


class UsageSummary(Base):
    """
    A derived rollup per (tenant, period, metric).

    Purely a cache: `metering.rebuild_summary()` can reconstruct any row from
    `UsageEvent` at any time. That property is the point -- it means a bug in
    the summariser is a recoverable inconvenience rather than a corrupted
    ledger, and it is what makes requirement 34's reconciliation possible.

    `finalized` marks a closed period. Once true the figures are what was
    invoiced, and later events for that period are counted but do not silently
    change the number a customer already paid.
    """
    __tablename__ = "usage_summaries"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "billing_period", "metric", name="uq_usage_summary_slot"
        ),
        Index("ix_usage_summary_period", "billing_period"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    billing_period: Mapped[str] = mapped_column(String(16), nullable=False)
    metric: Mapped[UsageMetric] = mapped_column(Enum(UsageMetric), nullable=False)

    #: All in the metric's smallest unit, matching `UsageEvent.quantity`.
    included_quantity: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    used_quantity: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    overage_quantity: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Hundredths of a cent, to match the plan's overage rates.
    estimated_overage_millicents: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )

    event_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    finalized: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    finalized_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: Highest threshold already announced (80, 100). Stops a warning firing
    #: on every single call once a tenant is over the line.
    warned_at_percent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow,
        nullable=False,
    )


class BillingInvoice(Base):
    """
    A mirror of a provider invoice, holding only what is safe to show.

    Deliberately not a general-purpose accounting record: no line items, no
    tax breakdown, no payment method. Requirement 20 asks for metadata and a
    hosted URL, and anything beyond that would be re-implementing Stripe's
    invoice object badly.
    """
    __tablename__ = "billing_invoices"
    __table_args__ = (
        UniqueConstraint(
            "provider", "external_invoice_id", name="uq_billing_invoice_external"
        ),
        Index("ix_billing_invoice_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    provider: Mapped[BillingProviderType] = mapped_column(
        Enum(BillingProviderType), nullable=False
    )
    external_invoice_id: Mapped[str] = mapped_column(String(255), nullable=False)

    status: Mapped[InvoiceStatus] = mapped_column(Enum(InvoiceStatus), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="usd", nullable=False)
    amount_due_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    amount_paid_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    period_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Stripe's short-lived signed URL. Safe to hand to an authorized user;
    #: it carries no API credential.
    hosted_invoice_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow,
        nullable=False,
    )


class BillingWebhookReceipt(Base):
    """
    Provider events already processed.

    Same pattern as `CrmWebhookReceipt` and `CalendarWebhookReceipt`. The
    difference here is that a duplicate has financial consequences, so the
    unique constraint is not an optimisation.

    `tenant_id` is nullable because a Stripe event can arrive before the
    customer is linked to a tenant -- the receipt is still recorded so a
    retry of that same event is recognised.
    """
    __tablename__ = "billing_webhook_receipts"
    __table_args__ = (
        # Provider event ids are globally unique, so this is not tenant-scoped.
        UniqueConstraint(
            "provider", "provider_event_id", name="uq_billing_receipt_event"
        ),
        Index("ix_billing_receipt_received", "received_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True, index=True
    )
    provider: Mapped[BillingProviderType] = mapped_column(
        Enum(BillingProviderType), nullable=False
    )
    provider_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    #: The provider's own creation time, used to detect a stale replay.
    provider_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    processed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: Scrubbed. Kept so a failed event can be investigated and replayed.
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)

    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )

# =============================================================================
# Batch 02: the enterprise surface's durable home
# =============================================================================
#
# Batch 01 shipped the automation, notification and inbox services with state in
# process-local dictionaries and reported the missing schema as a known gap.
# These five tables close it. The service *logic* is untouched: the registries
# keep their shape, and `app/services/enterprise_store.py` hydrates them from
# these rows before an operation and flushes them back afterwards, so there is
# exactly one implementation of every rule and it is the one Batch 01 wrote.
#
# Column-type notes (deliberate, not accidental):
#   * Ids are the domain's own stable hashes (`stable_id(...)`, 24 hex chars), so
#     they are String(64) primary keys rather than UUIDs — the id a client holds
#     is the id stored, with no translation layer to drift.
#   * Timestamps that the domain models carry as ISO-8601 *strings* are stored as
#     String(40) so a round-trip through the database cannot change their
#     meaning (offset form, fractional seconds). Nothing queries them
#     arithmetically; retention uses `created_at`, which is a real timestamp.
#   * State fields are String, not `Enum`, on purpose: `test_enum_consistency.py`
#     pins the PostgreSQL enum types created by the migrations, and adding new
#     PG enum types here would extend that contract for no benefit — the closed
#     vocabularies are enforced in the domain layer, where they are tested.

class Automation(Base):
    """A tenant-owned automation definition (Batch 02 persistence)."""

    __tablename__ = "automations"
    __table_args__ = (
        Index("ix_automations_tenant_event", "tenant_id", "event"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    event: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="disabled", nullable=False)
    #: [{"field", "operator", "value"}, ...] — evaluated by the domain rule.
    filters: Mapped[list] = mapped_column(JSON, default=list)
    #: [{"name", "params"}, ...] — names are restricted to CONTROLLED_ACTIONS.
    actions: Mapped[list] = mapped_column(JSON, default=list)
    schedule_kind: Mapped[str] = mapped_column(String(16), default="on_event", nullable=False)
    delay_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    backoff_seconds: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    cooldown_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_per_event: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    #: ISO-8601 of the last completed run — the cooldown anchor.
    last_run_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow,
        nullable=False,
    )


class AutomationRun(Base):
    """One attempted execution of an automation for one business event."""

    __tablename__ = "automation_runs"
    __table_args__ = (
        # The idempotency key IS the primary key: two deliveries of the same
        # business fact cannot create two rows, whatever the concurrency.
        Index("ix_automation_runs_tenant_automation", "tenant_id", "automation_id"),
        Index("ix_automation_runs_created", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    automation_id: Mapped[str] = mapped_column(String(64), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), index=True)
    event: Mapped[str] = mapped_column(String(32), nullable=False)
    business_event_id: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    last_error: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    result_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    finished_at: Mapped[str] = mapped_column(String(40), default="", nullable=False)


class NotificationTemplateRow(Base):
    """A tenant-owned notification template (Batch 02 persistence)."""

    __tablename__ = "notification_templates"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    body: Mapped[str] = mapped_column(Text, default="", nullable=False)
    variables: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )


class NotificationRow(Base):
    """One rendered notification, durable across restarts.

    PII: ``recipient`` holds the delivery target (phone number, email address or
    webhook URL). The API never returns it unmasked, logs never include it, and
    :func:`app.core.retention.purge_expired_notifications` deletes whole rows
    once they are older than the retention window.
    """

    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("tenant_id", "dedupe_key", name="uq_notification_dedupe"),
        Index("ix_notifications_tenant_state", "tenant_id", "delivery_state"),
        Index("ix_notifications_created", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    template_id: Mapped[str] = mapped_column(String(64), index=True)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    #: {"kind", "target", "user_id"} — see the PII note above.
    recipient: Mapped[dict] = mapped_column(JSON, default=dict)
    event_source: Mapped[str] = mapped_column(String(32), nullable=False)
    priority: Mapped[str] = mapped_column(String(16), default="normal", nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(64), nullable=False)
    rendered_body: Mapped[str] = mapped_column(Text, default="", nullable=False)
    delivery_state: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    sent_at: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    error_summary: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )


class InboxThreadState(Base):
    """Inbox-only state overlaid on a real ``Call`` row.

    Assignment, priority, tags, internal notes, read/unread and the SLA clock
    have no columns on ``calls`` (Batch 01 kept them in an overlay dict), so they
    live here — one row per (tenant, call), deleted by cascade when the call
    itself is purged by retention.
    """

    __tablename__ = "inbox_thread_states"
    __table_args__ = (
        UniqueConstraint("tenant_id", "call_id", name="uq_inbox_thread_state_call"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    call_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("calls.id", ondelete="CASCADE"), index=True
    )
    #: Explicit thread status once an operator moves it (open/assigned/…).
    status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    priority: Mapped[str] = mapped_column(String(16), default="normal", nullable=False)
    assignee_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    #: [{"body", "at", "author"}, ...] — internal notes, never customer-visible.
    notes: Mapped[list] = mapped_column(JSON, default=list)
    unread: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    opened_at: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    sla_deadline_at: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow,
        nullable=False,
    )
```

==============================================================================
===== FILE: app/api/automation_routes.py (500 lines, sha256 a52b3f437ef5c085fdc7ba1a2ceff07a9c7c3e01373bc5c90c3d8b82d7a40357) =====
==============================================================================
```python
"""Automation API (Batch 02 enterprise expansion — completes Batch 01's surface).

Tenant-scoped endpoints over ``app.services.automation_service``. Batch 01
shipped the automation *service* and its domain model with no HTTP surface at
all; this module is the missing half, written against the interfaces that
already exist rather than reimplementing any of them.

The security model is inherited verbatim from the Batch 01 route modules:

* **The tenant is never a parameter.** It comes from ``ctx.tenant_id`` (the
  verified JWT), and every registry access is keyed by it. A caller cannot
  address another tenant's automation even by guessing a correct id.
* **No arbitrary code, ever.** A request body may name an *action* only from
  the closed ``CONTROLLED_ACTIONS`` vocabulary and may carry validated
  parameters; the service rejects everything else and this module translates
  that rejection into a 422 instead of masking it.
* **No dialing.** Nothing in this file imports the telephony stack. Automations
  reuse the workflow controlled-action dispatcher, whose ``start_call``-shaped
  actions enqueue intents for the existing outbound path — the API never
  places a call.
* **Deterministic execution.** ``POST /{id}/run`` derives its deduplication key
  from ``(tenant, automation, event, business_event_id)``, so a retried request
  carries the same business fact and collapses to the same run instead of
  firing twice.

RBAC mapping (there is no dedicated ``automation:*`` permission in the closed
permission set, so the closest operational owner is reused deliberately rather
than inventing a new permission outside this batch's file set):

===========================  =============================
read (list/get/runs/stats)   ``CAMPAIGN_READ``   (viewer+)
define / enable / disable    ``CAMPAIGN_WRITE``  (manager+)
run / evaluate-for-effect    ``CAMPAIGN_RUN``    (manager+)
===========================  =============================

Registered in ``app/main.py``; see ``tests/test_enterprise_batch02.py`` for the
wiring assertion that keeps that true.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import TenantContext, require_permission
from app.auth.permissions import Permission
from app.core.errors import BadRequestError, NotFoundError
from app.db.session import get_session
from app.domain.agent_models import stable_id
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
from app.domain.workflow_models import CONTROLLED_ACTIONS, WorkflowAction
from app.services import automation_service
from app.services.enterprise_store import durable_state

#: Every endpoint runs inside the tenant's durable scope: the automation
#: registry is hydrated from ``automations`` / ``automation_runs`` before the
#: handler and flushed back after it, so a restart (or a second worker) no
#: longer loses definitions or run history. See ``app/services/enterprise_store``.
router = APIRouter(prefix="/api/automations", tags=["automations"],
                   dependencies=[Depends(durable_state)])

#: The largest event payload the API will accept. Automations react to webhook
#: facts; a payload bigger than this is either a mistake or an attempt to make
#: the engine do work, and both end at the same 422.
MAX_PAYLOAD_BYTES = 64 * 1024
MAX_PAYLOAD_KEYS = 200


# ----------------------------------------------------------------- schemas ---

class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ActionIn(_Strict):
    """One controlled action. The vocabulary is validated by the service."""

    name: str = Field(min_length=1, max_length=64)
    params: dict[str, Any] = Field(default_factory=dict)


class FilterIn(_Strict):
    field: str = Field(min_length=1, max_length=64)
    operator: str = Field(min_length=1, max_length=16)
    value: Any = None


class AutomationWriteRequest(_Strict):
    name: str = Field(min_length=1, max_length=200)
    event: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=4_000)
    filters: list[FilterIn] = Field(default_factory=list, max_length=20)
    actions: list[ActionIn] = Field(min_length=1, max_length=20)
    schedule_kind: str = "on_event"
    delay_seconds: int = Field(default=0, ge=0, le=86_400)
    max_attempts: int = Field(default=3, ge=1, le=10)
    backoff_seconds: int = Field(default=60, ge=0, le=86_400)
    cooldown_seconds: int = Field(default=0, ge=0, le=86_400)
    max_per_event: int = Field(default=1, ge=1, le=100)


class ActionOut(_Strict):
    name: str
    params: dict[str, Any]


class FilterOut(_Strict):
    field: str
    operator: str
    value: Any


class AutomationOut(_Strict):
    id: str
    tenant_id: str
    name: str
    description: str
    event: str
    status: str
    schedule_kind: str
    delay_seconds: int
    max_attempts: int
    backoff_seconds: int
    cooldown_seconds: int
    max_per_event: int
    filters: list[FilterOut]
    actions: list[ActionOut]


class RunOut(_Strict):
    id: str
    automation_id: str
    event: str
    business_event_id: str
    status: str
    attempts: int
    next_attempt_at: str
    last_error: str
    created_at: str
    finished_at: str
    result_summary: dict[str, Any]


class RunRequest(_Strict):
    business_event_id: str = Field(min_length=1, max_length=200)
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("payload")
    @classmethod
    def _bounded_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        _guard_payload(value)
        return value


class EvaluateRequest(_Strict):
    event: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("payload")
    @classmethod
    def _bounded_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        _guard_payload(value)
        return value


class MatchOut(_Strict):
    automation_id: str
    name: str
    event: str


class StatsOut(_Strict):
    failed: int
    suppressed: int
    completed: int


# ---------------------------------------------------------------- helpers ---

def _guard_payload(payload: dict[str, Any]) -> None:
    """Bound what a caller can push through the evaluator.

    Raised as a ``ValueError`` so pydantic turns it into FastAPI's documented
    422 body — the same shape every other validation failure uses.
    """
    if len(payload) > MAX_PAYLOAD_KEYS:
        raise ValueError(f"payload may carry at most {MAX_PAYLOAD_KEYS} keys")
    try:
        encoded = json.dumps(payload, default=str)
    except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
        raise ValueError(f"payload must be JSON-serialisable: {exc}") from None
    if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ValueError(f"payload must be at most {MAX_PAYLOAD_BYTES} bytes")


def _event_of(value: str) -> TriggerEvent:
    try:
        return TriggerEvent(value)
    except ValueError:
        allowed = ", ".join(sorted(e.value for e in TriggerEvent))
        raise HTTPException(
            status_code=422, detail=f"unknown event {value!r}; allowed: {allowed}",
        ) from None


def _schedule_kind_of(value: str) -> ScheduleKind:
    try:
        return ScheduleKind(value)
    except ValueError:
        allowed = ", ".join(sorted(k.value for k in ScheduleKind))
        raise HTTPException(
            status_code=422,
            detail=f"unknown schedule_kind {value!r}; allowed: {allowed}",
        ) from None


def _definition_of(
    tenant_id: str, automation_id: str, payload: AutomationWriteRequest,
) -> AutomationDefinition:
    return AutomationDefinition(
        id=automation_id,
        tenant_id=tenant_id,
        name=payload.name,
        event=_event_of(payload.event),
        description=payload.description,
        filters=tuple(
            FilterRule(field=rule.field, operator=rule.operator, value=rule.value)
            for rule in payload.filters
        ),
        actions=tuple(
            WorkflowAction(name=item.name, params=item.params or {})
            for item in payload.actions
        ),
        schedule=AutomationSchedule(
            kind=_schedule_kind_of(payload.schedule_kind),
            delay_seconds=payload.delay_seconds,
        ),
        policy=ExecutionPolicy(
            max_attempts=payload.max_attempts,
            backoff_seconds=payload.backoff_seconds,
            cooldown_seconds=payload.cooldown_seconds,
            max_per_event=payload.max_per_event,
        ),
        status=AutomationStatus.DISABLED,
    )


def _out(definition: AutomationDefinition) -> AutomationOut:
    return AutomationOut(
        id=definition.id,
        tenant_id=definition.tenant_id,
        name=definition.name,
        description=definition.description,
        event=definition.event.value,
        status=definition.status.value,
        schedule_kind=definition.schedule.kind.value,
        delay_seconds=definition.schedule.delay_seconds,
        max_attempts=definition.policy.max_attempts,
        backoff_seconds=definition.policy.backoff_seconds,
        cooldown_seconds=definition.policy.cooldown_seconds,
        max_per_event=definition.policy.max_per_event,
        filters=[
            FilterOut(field=r.field, operator=r.operator, value=r.value)
            for r in definition.filters
        ],
        actions=[ActionOut(name=a.name, params=a.params) for a in definition.actions],
    )


def _run_out(run: AutomationRun) -> RunOut:
    return RunOut(
        id=run.id,
        automation_id=run.automation_id,
        event=run.event.value,
        business_event_id=run.business_event_id,
        status=run.status,
        attempts=run.attempts,
        next_attempt_at=run.next_attempt_at,
        last_error=run.last_error,
        created_at=run.created_at,
        finished_at=run.finished_at,
        result_summary=run.result_summary,
    )


def _not_found() -> HTTPException:
    # One message for "does not exist" and "belongs to someone else", so the
    # endpoint cannot be used to probe another tenant's automation ids.
    return HTTPException(status_code=404, detail="automation not found")


# ------------------------------------------------------------------ routes ---

@router.get("/events", response_model=list[str])
async def list_events(
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_READ)),
):
    """The closed trigger vocabulary — what a dashboard picker may offer.

    Served from the enum itself so the client can never drift from the server's
    definition of a valid event.
    """
    return sorted(event.value for event in TriggerEvent)


@router.get("/actions", response_model=list[str])
async def list_actions(
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_READ)),
):
    """The closed controlled-action vocabulary available to automations."""
    return sorted(CONTROLLED_ACTIONS)


@router.post("", response_model=AutomationOut, status_code=201)
async def create_automation(
    payload: AutomationWriteRequest,
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_WRITE)),
):
    """Create (or idempotently replace) an automation.

    The id derives from ``(tenant, name)``, so re-posting the same name updates
    the same automation instead of accumulating duplicates — the same rule the
    agent-management and campaign routes use.
    """
    automation_id = stable_id(str(ctx.tenant_id), payload.name)
    definition = _definition_of(str(ctx.tenant_id), automation_id, payload)
    try:
        saved = automation_service.register_automation(str(ctx.tenant_id), definition)
    except BadRequestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return _out(saved)


@router.get("", response_model=list[AutomationOut])
async def list_automations(
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_READ)),
):
    return [_out(d) for d in automation_service.list_automations(str(ctx.tenant_id))]


@router.post("/evaluate", response_model=list[MatchOut])
async def evaluate_event(
    payload: EvaluateRequest,
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_READ)),
):
    """Dry run: which enabled automations match this event payload?

    Pure selection — it runs no action, writes no run, and cannot be used to
    trigger anything. It exists so a tenant can verify a filter before enabling
    an automation against real traffic.
    """
    event = _event_of(payload.event)
    matches = automation_service.evaluate(str(ctx.tenant_id), event, payload.payload)
    return [
        MatchOut(automation_id=m.id, name=m.name, event=m.event.value) for m in matches
    ]


@router.get("/{automation_id}", response_model=AutomationOut)
async def get_automation(
    automation_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_READ)),
):
    try:
        return _out(automation_service.get_automation(str(ctx.tenant_id), automation_id))
    except NotFoundError:
        raise _not_found() from None


@router.put("/{automation_id}", response_model=AutomationOut)
async def update_automation(
    automation_id: str,
    payload: AutomationWriteRequest,
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_WRITE)),
):
    """Replace a definition. The id is the path's, never the body's."""
    try:
        automation_service.get_automation(str(ctx.tenant_id), automation_id)
    except NotFoundError:
        raise _not_found() from None
    definition = _definition_of(str(ctx.tenant_id), automation_id, payload)
    try:
        saved = automation_service.register_automation(str(ctx.tenant_id), definition)
    except BadRequestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return _out(saved)


@router.post("/{automation_id}/enable", response_model=AutomationOut)
async def enable_automation(
    automation_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_WRITE)),
):
    try:
        return _out(automation_service.set_enabled(str(ctx.tenant_id), automation_id, True))
    except NotFoundError:
        raise _not_found() from None


@router.post("/{automation_id}/disable", response_model=AutomationOut)
async def disable_automation(
    automation_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_WRITE)),
):
    try:
        return _out(automation_service.set_enabled(str(ctx.tenant_id), automation_id, False))
    except NotFoundError:
        raise _not_found() from None


@router.post("/{automation_id}/run", response_model=RunOut)
async def run_automation(
    automation_id: str,
    payload: RunRequest,
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_RUN)),
    session: AsyncSession = Depends(get_session),
):
    """Fire one automation for one business event, through the service's rules.

    Cooldown, ``max_per_event`` and retry policy are decided by the service —
    this endpoint cannot bypass them, and a suppressed run is returned as a
    ``cancelled`` run with the reason in ``last_error`` rather than an error
    status, because "the engine correctly declined to fire" is a normal
    outcome, not a client mistake.
    """
    try:
        automation = automation_service.get_automation(str(ctx.tenant_id), automation_id)
    except NotFoundError:
        raise _not_found() from None
    run = await automation_service.execute_automation(
        str(ctx.tenant_id),
        automation,
        payload.business_event_id,
        payload.payload,
        session=session,
    )
    return _run_out(run)


@router.get("/{automation_id}/runs", response_model=list[RunOut])
async def automation_runs(
    automation_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_READ)),
):
    try:
        automation_service.get_automation(str(ctx.tenant_id), automation_id)
    except NotFoundError:
        raise _not_found() from None
    runs = automation_service.run_history(str(ctx.tenant_id), automation_id)
    return [_run_out(run) for run in runs[:limit]]


@router.get("/{automation_id}/stats", response_model=StatsOut)
async def automation_stats(
    automation_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_READ)),
):
    try:
        automation_service.get_automation(str(ctx.tenant_id), automation_id)
    except NotFoundError:
        raise _not_found() from None
    counts = automation_service.failure_counts(str(ctx.tenant_id), automation_id)
    return StatsOut(**counts)


@router.post("/{automation_id}/dedupe-key", response_model=dict)
async def automation_dedupe_key(
    automation_id: str,
    payload: RunRequest,
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_READ)),
):
    """Expose the idempotency key a run *would* use.

    Read-only and side-effect free: an integrator can check whether a business
    event has already been consumed before sending it, without firing the
    automation. The key is a stable hash of tenant + automation + event +
    business id, so publishing it leaks nothing a caller does not already know.
    """
    try:
        automation = automation_service.get_automation(str(ctx.tenant_id), automation_id)
    except NotFoundError:
        raise _not_found() from None
    key = automation_service.deduplicate(
        str(ctx.tenant_id), automation, payload.business_event_id
    )
    return {"automation_id": automation_id, "dedupe_key": key}
```

==============================================================================
===== FILE: app/api/notification_routes.py (480 lines, sha256 bfe8de590667ad1c556ef1b620af3e13b275af3065961add316b6a72e4f1b507) =====
==============================================================================
```python
"""Notification API (Batch 02 enterprise expansion — completes Batch 01's surface).

Tenant-scoped endpoints over ``app.services.notification_service``. Batch 01
shipped the notification *service* (templates, preferences, dedupe, delivery)
with no HTTP surface; this module is the missing half.

Guarantees inherited from the service — this module adds none of its own and
subtracts none:

* **Nothing is sent around the compliance check.** ``SMS`` delivery still runs
  ``core.compliance.check_message`` inside ``notification_service.deliver``;
  ``/deliver`` is a thin trigger, not a second path.
* **No new provider.** ``EMAIL`` is declared-but-suppressed and ``WEBHOOK`` is
  prepared-but-not-dispatched; the API reports those as ``suppressed`` /
  ``pending`` states instead of pretending they were sent.
* **Recipient contact details never leave the service in full.** Responses
  carry a *masked* target (last digits only) plus the user id; the raw target
  is stored for delivery and is not an API field. That mirrors the rule the
  analytics and transcript surfaces already follow.
* **Deduplication is server-side.** ``POST /`` derives its key from
  ``(tenant, template, event source, business key)``; a repeated request
  returns the *same* notification rather than a second copy.

RBAC (no ``notification:*`` permission exists in the closed set, so the
operational owner is reused deliberately):

==============================  =============================
read (list/get/templates)       ``CAMPAIGN_READ``   (viewer+)
templates + create + read state ``CAMPAIGN_WRITE``  (manager+)
deliver / retry (may cost SMS)  ``CAMPAIGN_RUN``    (manager+)
==============================  =============================
"""

from __future__ import annotations

from datetime import time as _time

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import TenantContext, require_permission
from app.auth.permissions import Permission
from app.db.session import get_session
from app.domain.agent_models import stable_id
from app.domain.notification_models import (
    DeliveryState,
    EventSource,
    Notification,
    NotificationChannel,
    NotificationPriority,
    NotificationTemplate,
    PreferenceSet,
    Recipient,
)
from app.services import notification_service
from app.services.enterprise_store import durable_state

#: Every endpoint runs inside the tenant's durable scope (templates,
#: notifications, dedupe index and read state are read from and written back to
#: the database around the request). See ``app/services/enterprise_store``.
router = APIRouter(prefix="/api/notifications", tags=["notifications"],
                   dependencies=[Depends(durable_state)])

#: Templates and rendered bodies are operator-authored content; these are the
#: same ceilings the domain model validates, mirrored here so the failure is a
#: 422 with a field name instead of a 500 from deep inside the service.
MAX_TEMPLATE_BODY = 8_000
MAX_VARIABLES = 40


# ----------------------------------------------------------------- schemas ---

class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TemplateWriteRequest(_Strict):
    name: str = Field(min_length=1, max_length=200)
    channel: str = Field(min_length=1, max_length=32)
    body: str = Field(min_length=1, max_length=MAX_TEMPLATE_BODY)
    variables: list[str] = Field(default_factory=list, max_length=MAX_VARIABLES)


class TemplateOut(_Strict):
    id: str
    tenant_id: str
    name: str
    channel: str
    body: str
    variables: list[str]


class RenderRequest(_Strict):
    variables: dict[str, str] = Field(default_factory=dict)


class RenderOut(_Strict):
    template_id: str
    body: str
    channel: str


class RecipientIn(_Strict):
    kind: str = Field(default="user", max_length=16)
    target: str = Field(default="", max_length=2_048)
    user_id: str = Field(default="", max_length=64)


class RecipientOut(_Strict):
    """A recipient as it may appear in a response: masked, never raw."""

    kind: str
    user_id: str
    target_masked: str


class NotificationCreateRequest(_Strict):
    template_id: str = Field(min_length=1, max_length=64)
    recipient: RecipientIn
    event_source: str = Field(min_length=1, max_length=32)
    priority: str = "normal"
    business_key: str = Field(default="", max_length=200)
    variables: dict[str, str] = Field(default_factory=dict)


class NotificationOut(_Strict):
    id: str
    template_id: str
    channel: str
    priority: str
    event_source: str
    recipient: RecipientOut
    delivery_state: str
    attempts: int
    next_attempt_at: str
    sent_at: str
    error_summary: str
    read: bool


class PreferenceCheckRequest(_Strict):
    channel: str = Field(min_length=1, max_length=32)
    enabled: bool = True
    quiet_start: str = "00:00"
    quiet_end: str = "00:00"

    @field_validator("quiet_start", "quiet_end")
    @classmethod
    def _hhmm(cls, value: str) -> str:
        # Raising ValueError (not HTTPException) keeps the failure inside
        # pydantic, so FastAPI returns its documented 422 body naming the
        # offending field.
        _parse_hhmm(value)
        return value


class PreferenceCheckOut(_Strict):
    allowed: bool
    reason: str


# ---------------------------------------------------------------- helpers ---

def _parse_hhmm(value: str) -> _time:
    """Parse ``HH:MM`` (24-hour). Raises ``ValueError`` for pydantic to report."""
    try:
        hour, minute = value.split(":")[:2]
        return _time(int(hour), int(minute))
    except (ValueError, AttributeError):
        raise ValueError(f"time {value!r} must be HH:MM (24-hour)") from None


def _channel_of(value: str) -> NotificationChannel:
    try:
        return NotificationChannel(value)
    except ValueError:
        allowed = ", ".join(sorted(c.value for c in NotificationChannel))
        raise HTTPException(
            status_code=422, detail=f"unknown channel {value!r}; allowed: {allowed}",
        ) from None


def _priority_of(value: str) -> NotificationPriority:
    try:
        return NotificationPriority(value)
    except ValueError:
        allowed = ", ".join(sorted(p.value for p in NotificationPriority))
        raise HTTPException(
            status_code=422, detail=f"unknown priority {value!r}; allowed: {allowed}",
        ) from None


def _event_source_of(value: str) -> EventSource:
    try:
        return EventSource(value)
    except ValueError:
        allowed = ", ".join(sorted(e.value for e in EventSource))
        raise HTTPException(
            status_code=422,
            detail=f"unknown event_source {value!r}; allowed: {allowed}",
        ) from None


def _mask_target(kind: str, target: str) -> str:
    """Mask a recipient contact for display.

    Phone numbers keep their last four digits; anything else keeps the local
    part's first two characters and the domain (or, for a webhook, the host
    only). The full value is never returned, so a compromised dashboard reader
    cannot harvest the tenant's contact list from this endpoint.
    """
    if not target:
        return ""
    if kind == "phone" or target.startswith("+"):
        digits = "".join(ch for ch in target if ch.isdigit())
        return f"***{digits[-4:]}" if digits else "***"
    if "@" in target:
        local, _, domain = target.partition("@")
        return f"{local[:2]}***@{domain}"
    if "://" in target:
        scheme, _, rest = target.partition("://")
        host = rest.split("/")[0]
        return f"{scheme}://{host}/***"
    return f"{target[:2]}***"


def _template_out(template: NotificationTemplate) -> TemplateOut:
    return TemplateOut(
        id=template.id,
        tenant_id=template.tenant_id,
        name=template.name,
        channel=template.channel.value,
        body=template.body,
        variables=list(template.variables),
    )


def _notification_out(
    tenant_id: str, notification: Notification,
) -> NotificationOut:
    return NotificationOut(
        id=notification.id,
        template_id=notification.template_id,
        channel=notification.channel.value,
        priority=notification.priority.value,
        event_source=notification.event_source.value,
        recipient=RecipientOut(
            kind=notification.recipient.kind,
            user_id=notification.recipient.user_id,
            target_masked=_mask_target(
                notification.recipient.kind, notification.recipient.target
            ),
        ),
        delivery_state=notification.delivery_state.value,
        attempts=notification.attempts,
        next_attempt_at=notification.next_attempt_at,
        sent_at=notification.sent_at,
        error_summary=notification.error_summary,
        read=notification_service.is_read(tenant_id, notification.id),
    )


def _template_or_404(tenant_id: str, template_id: str) -> NotificationTemplate:
    try:
        return notification_service.get_template(tenant_id, template_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="template not found") from None


def _notification_or_404(tenant_id: str, notification_id: str) -> Notification:
    for notification in notification_service.history(tenant_id):
        if notification.id == notification_id:
            return notification
    # The service raises KeyError from its mutation helpers, but history() is
    # the only tenant-scoped reader — looking there first keeps the 404 path
    # identical for "missing" and "someone else's" notifications.
    raise HTTPException(status_code=404, detail="notification not found")


# ---------------------------------------------------------------- templates ---

@router.post("/templates", response_model=TemplateOut, status_code=201)
async def create_template(
    payload: TemplateWriteRequest,
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_WRITE)),
):
    """Create (or idempotently replace) a template.

    The id derives from ``(tenant, name)`` so a retried create lands on the
    same template instead of a duplicate.
    """
    tenant_id = str(ctx.tenant_id)
    template = NotificationTemplate(
        id=stable_id(tenant_id, "notification-template", payload.name),
        tenant_id=tenant_id,
        name=payload.name,
        channel=_channel_of(payload.channel),
        body=payload.body,
        variables=tuple(payload.variables),
    )
    try:
        saved = notification_service.create_template(tenant_id, template)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return _template_out(saved)


@router.get("/templates", response_model=list[TemplateOut])
async def list_templates(
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_READ)),
):
    return [_template_out(t) for t in notification_service.list_templates(str(ctx.tenant_id))]


@router.get("/templates/{template_id}", response_model=TemplateOut)
async def get_template(
    template_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_READ)),
):
    return _template_out(_template_or_404(str(ctx.tenant_id), template_id))


@router.post("/templates/{template_id}/render", response_model=RenderOut)
async def render_template(
    template_id: str,
    payload: RenderRequest,
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_READ)),
):
    """Preview a rendered template. Pure: it creates and sends nothing."""
    template = _template_or_404(str(ctx.tenant_id), template_id)
    try:
        body = notification_service.render(template, payload.variables)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return RenderOut(template_id=template.id, body=body, channel=template.channel.value)


# -------------------------------------------------------------- preferences ---

@router.post("/preferences/check", response_model=PreferenceCheckOut)
async def check_preferences(
    payload: PreferenceCheckRequest,
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_READ)),
):
    """Ask the service whether a channel is allowed for a preference set.

    Exposed so a dashboard can show *why* a notification would be suppressed
    (quiet hours, disabled channel) before an operator sends anything.
    """
    preference = PreferenceSet(
        channel=_channel_of(payload.channel),
        enabled=payload.enabled,
        quiet_start=_parse_hhmm(payload.quiet_start),
        quiet_end=_parse_hhmm(payload.quiet_end),
    )
    allowed, reason = notification_service.resolve_preferences(
        preference, preference.channel
    )
    return PreferenceCheckOut(allowed=allowed, reason=reason)


# ------------------------------------------------------------ notifications ---

@router.post("", response_model=NotificationOut, status_code=201)
async def create_notification(
    payload: NotificationCreateRequest,
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_WRITE)),
):
    """Render + create a notification, deduplicated on the business key."""
    tenant_id = str(ctx.tenant_id)
    template = _template_or_404(tenant_id, payload.template_id)
    recipient = Recipient(
        kind=payload.recipient.kind,
        target=payload.recipient.target,
        user_id=payload.recipient.user_id,
    )
    try:
        notification = notification_service.create_notification(
            tenant_id,
            template=template,
            recipient=recipient,
            event_source=_event_source_of(payload.event_source),
            priority=_priority_of(payload.priority),
            business_key=payload.business_key,
            variables=payload.variables,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return _notification_out(tenant_id, notification)


@router.get("", response_model=list[NotificationOut])
async def list_notifications(
    unread_only: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=500),
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_READ)),
):
    tenant_id = str(ctx.tenant_id)
    history = notification_service.history(tenant_id, unread_only=unread_only)
    return [_notification_out(tenant_id, n) for n in history[:limit]]


@router.get("/{notification_id}", response_model=NotificationOut)
async def get_notification(
    notification_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_READ)),
):
    tenant_id = str(ctx.tenant_id)
    return _notification_out(tenant_id, _notification_or_404(tenant_id, notification_id))


@router.post("/{notification_id}/read", response_model=NotificationOut)
async def mark_read(
    notification_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_WRITE)),
):
    tenant_id = str(ctx.tenant_id)
    _notification_or_404(tenant_id, notification_id)
    return _notification_out(tenant_id, notification_service.mark_read(tenant_id, notification_id))


@router.post("/{notification_id}/unread", response_model=NotificationOut)
async def mark_unread(
    notification_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_WRITE)),
):
    tenant_id = str(ctx.tenant_id)
    _notification_or_404(tenant_id, notification_id)
    return _notification_out(tenant_id, notification_service.mark_unread(tenant_id, notification_id))


@router.post("/{notification_id}/deliver", response_model=NotificationOut)
async def deliver_notification(
    notification_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_RUN)),
    session: AsyncSession = Depends(get_session),
):
    """Attempt delivery once, then record the outcome.

    ``CAMPAIGN_RUN`` (not WRITE) because an SMS costs the tenant real money —
    the same reasoning that puts campaign execution with the manager role. The
    compliance gate, the SSRF validation and the "no email provider" refusal all
    live in the service and are reported here as states, not swallowed.
    """
    tenant_id = str(ctx.tenant_id)
    notification = _notification_or_404(tenant_id, notification_id)
    result = await notification_service.deliver(notification)
    updated = notification_service.record_delivery(tenant_id, notification, result)
    return _notification_out(tenant_id, updated)


@router.post("/{notification_id}/retry", response_model=NotificationOut)
async def retry_notification(
    notification_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_RUN)),
):
    """Schedule a bounded-backoff retry (it does not send anything itself)."""
    tenant_id = str(ctx.tenant_id)
    notification = _notification_or_404(tenant_id, notification_id)
    if notification.delivery_state is DeliveryState.DELIVERED:
        raise HTTPException(status_code=422, detail="notification is already delivered")
    try:
        updated = notification_service.retry(tenant_id, notification_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="notification not found") from None
    return _notification_out(tenant_id, updated)


# ----------------------------------------------------------------- reporting ---

@router.get("/states/summary", response_model=dict)
async def delivery_summary(
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_READ)),
):
    """Delivery-state counts for the tenant (aggregate-safe, no recipients)."""
    counts: dict[str, int] = {state.value: 0 for state in DeliveryState}
    for notification in notification_service.history(str(ctx.tenant_id)):
        counts[notification.delivery_state.value] += 1
    return counts
```

==============================================================================
===== FILE: app/api/inbox_routes.py (528 lines, sha256 3e2260a5b88122a1182e55947e76899d7224ad348c3b1d1bbe5aac39c0fa7cd4) =====
==============================================================================
```python
"""Unified inbox API (Batch 02 enterprise expansion — completes Batch 01's surface).

Tenant-scoped endpoints over ``app.services.inbox_service``. Batch 01 shipped
the inbox *service* (threads projected from real ``Call``/``Turn`` rows plus an
overlay for assignment, priority, tags, notes, read state and SLA) with no HTTP
surface; this module is the missing half.

Rules this module enforces, all inherited from the service:

* **Cross-tenant visibility is impossible.** Every read resolves the thread
  through ``inbox_service.project``/``search``, which filter on
  ``Call.tenant_id`` and prove ownership before projecting. A caller who knows
  another tenant's thread id gets the same 404 as a caller who invented one.
* **Messages are real rows.** ``POST /threads/{id}/messages`` writes ``Turn``
  rows (the same storage the voice pipeline and the transcript view use), so
  analytics keeps working unchanged. Internal notes go to the overlay, labelled
  as notes, and never become customer-visible messages.
* **Participant numbers are masked in responses.** Thread participants are
  phone numbers; the API returns ``***1234``-style masks plus the channel, which
  is what a queue view needs and nothing more.
* **No sending around the existing paths.** An outbound message is appended
  here exactly as ``/channels/message`` writes it; this file imports no
  provider and places no call.

RBAC: reads require ``CALL_READ``; every mutation requires ``CALL_READ_ALL``
(manager and above). That is deliberate and is a *registered limitation*, not a
design claim: the inbox service has no per-assignee scoping yet, so allowing
``AGENT`` to mutate would hand every agent tenant-wide write access to every
conversation. Per-assignee scoping needs a permission and an ownership check
that do not exist in this batch; until they do, mutations stay at the role that
can already see all calls.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import TenantContext, require_permission
from app.auth.permissions import Permission
from app.core.errors import BadRequestError, NotFoundError
from app.db.models import Call
from app.db.session import get_session
from app.domain.inbox_models import (
    InboxChannel,
    Message,
    MessageDirection,
    Thread,
    ThreadPriority,
    ThreadStatus,
    thread_id as make_thread_id,
)
from app.services import inbox_service
from app.services.enterprise_store import durable_state

#: Every endpoint runs inside the tenant's durable scope: the inbox overlay
#: (priority, assignment, tags, notes, read state, SLA clock) is hydrated from
#: ``inbox_thread_states`` before the handler and flushed back after it, so
#: unread badges and assignments survive a restart. See
#: ``app/services/enterprise_store``.
router = APIRouter(prefix="/api/inbox", tags=["inbox"],
                   dependencies=[Depends(durable_state)])

#: How many of a tenant's most recent conversations one resolution pass may
#: walk. Thread ids are opaque hashes (not reversible), so a lookup by id is a
#: scan of the tenant's calls; this bound keeps that scan cost predictable.
#: A tenant with more than this many conversations resolves older threads only
#: through the search endpoint's own paging — stated plainly rather than
#: hidden behind an unbounded loop.
MAX_RESOLUTION_ROWS = 500


# ----------------------------------------------------------------- schemas ---

class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MessageOut(_Strict):
    id: str
    thread_id: str
    direction: str
    channel: str
    author_role: str
    body: str
    sequence: int
    sent_at: str


class SlaOut(_Strict):
    opened_at: str
    deadline_at: str
    breached: bool


class ThreadOut(_Strict):
    id: str
    tenant_id: str
    channel: str
    status: str
    priority: str
    participants_masked: list[str]
    assignee_id: str
    tags: list[str]
    internal_notes: list[str]
    unread_count: int
    escalated: bool
    sla: SlaOut
    last_message_at: str
    created_at: str
    message_count: int


class ThreadCreateRequest(_Strict):
    channel: str = Field(min_length=1, max_length=16)
    customer: str = Field(min_length=1, max_length=64)
    initial_message: str = Field(default="", max_length=8_000)


class MessageCreateRequest(_Strict):
    direction: str = Field(min_length=1, max_length=16)
    body: str = Field(min_length=1, max_length=8_000)
    author_role: str = Field(default="agent", max_length=32)


class NoteCreateRequest(_Strict):
    body: str = Field(min_length=1, max_length=8_000)


class AssignRequest(_Strict):
    assignee_id: str = Field(min_length=1, max_length=64)


class PriorityRequest(_Strict):
    priority: str = Field(min_length=1, max_length=16)


class TagRequest(_Strict):
    tag: str = Field(min_length=1, max_length=64)


class EscalateRequest(_Strict):
    reason: str = Field(min_length=1, max_length=400)


class CountsOut(_Strict):
    open: int
    closed: int
    escalated: int
    assigned: int


# ---------------------------------------------------------------- helpers ---

def _channel_of(value: str) -> InboxChannel:
    try:
        return InboxChannel(value)
    except ValueError:
        allowed = ", ".join(sorted(c.value for c in InboxChannel))
        raise HTTPException(
            status_code=422, detail=f"unknown channel {value!r}; allowed: {allowed}",
        ) from None


def _status_of(value: str) -> ThreadStatus:
    try:
        return ThreadStatus(value)
    except ValueError:
        allowed = ", ".join(sorted(s.value for s in ThreadStatus))
        raise HTTPException(
            status_code=422, detail=f"unknown status {value!r}; allowed: {allowed}",
        ) from None


def _priority_of(value: str) -> ThreadPriority:
    try:
        return ThreadPriority(value)
    except ValueError:
        allowed = ", ".join(sorted(p.value for p in ThreadPriority))
        raise HTTPException(
            status_code=422, detail=f"unknown priority {value!r}; allowed: {allowed}",
        ) from None


def _direction_of(value: str) -> MessageDirection:
    try:
        return MessageDirection(value)
    except ValueError:
        allowed = ", ".join(sorted(d.value for d in MessageDirection))
        raise HTTPException(
            status_code=422, detail=f"unknown direction {value!r}; allowed: {allowed}",
        ) from None


def _mask_participant(value: str) -> str:
    """`+15551234567` -> `***4567`; anything else is truncated, never returned raw."""
    if not value:
        return ""
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) >= 4:
        return f"***{digits[-4:]}"
    return "***"


def _message_out(message: Message) -> MessageOut:
    return MessageOut(
        id=message.id,
        thread_id=message.thread_id,
        direction=message.direction.value,
        channel=message.channel.value,
        author_role=message.author_role,
        body=message.body,
        sequence=message.sequence,
        sent_at=message.sent_at,
    )


def _thread_out(thread: Thread) -> ThreadOut:
    return ThreadOut(
        id=thread.id,
        tenant_id=thread.tenant_id,
        channel=thread.channel.value,
        status=thread.status.value,
        priority=thread.priority.value,
        participants_masked=[_mask_participant(p) for p in thread.participants],
        assignee_id=thread.assignee_id,
        tags=list(thread.tags),
        internal_notes=list(thread.internal_notes),
        unread_count=thread.unread_count,
        escalated=thread.escalated,
        sla=SlaOut(
            opened_at=thread.sla.opened_at,
            deadline_at=thread.sla.deadline_at,
            breached=thread.sla.breached,
        ),
        last_message_at=thread.last_message_at,
        created_at=thread.created_at,
        message_count=len(thread.messages),
    )


async def _resolve(
    session: AsyncSession, ctx: TenantContext, thread_id: str,
) -> Call:
    """Resolve an opaque thread id to its tenant-owned ``Call`` row.

    Scans the tenant's most recent conversations through the service's own
    projection (which is what enforces isolation) and matches on the derived
    thread id. Nothing outside ``ctx.tenant_id`` is ever considered, so a
    correct id from another tenant is simply "not found" here.
    """
    stmt = (
        select(Call)
        .where(Call.tenant_id == ctx.tenant_id)
        .order_by(Call.started_at.desc())
        .limit(MAX_RESOLUTION_ROWS)
    )
    rows = (await session.execute(stmt)).scalars().all()
    for row in rows:
        try:
            projected = await inbox_service.project(session, ctx.tenant, row)
        except NotFoundError:
            continue
        if projected.id == thread_id or str(row.id) == thread_id:
            return row
    raise HTTPException(status_code=404, detail="thread not found")


# --------------------------------------------------------------- read side ---

@router.get("/threads", response_model=list[ThreadOut])
async def list_threads(
    channel: str | None = Query(default=None),
    status: str | None = Query(default=None),
    assignee_id: str | None = Query(default=None),
    escalated: bool | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    ctx: TenantContext = Depends(require_permission(Permission.CALL_READ)),
    session: AsyncSession = Depends(get_session),
):
    threads = await inbox_service.search(
        session,
        ctx.tenant,
        channel=_channel_of(channel) if channel else None,
        status=_status_of(status) if status else None,
        assignee_id=assignee_id,
        escalated=escalated,
        limit=limit,
    )
    return [_thread_out(thread) for thread in threads]


@router.post("/threads", response_model=ThreadOut, status_code=201)
async def open_thread(
    payload: ThreadCreateRequest,
    ctx: TenantContext = Depends(require_permission(Permission.CALL_READ_ALL)),
    session: AsyncSession = Depends(get_session),
):
    """Open (or reuse, for text channels) a conversation.

    Voice-ish channels create a real ``Call`` row so the thread participates in
    every existing view; ``sms``/``whatsapp`` reuse the existing channel thread
    helper, which is what keeps the inbox and ``/channels/message`` consistent.
    """
    channel = _channel_of(payload.channel)
    try:
        thread = await inbox_service.find_or_create_thread(
            session, ctx.tenant, channel=channel, customer=payload.customer,
            initial_message=payload.initial_message,
        )
    except BadRequestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return _thread_out(await inbox_service.project(session, ctx.tenant, thread))


@router.get("/threads/{thread_id}", response_model=ThreadOut)
async def get_thread(
    thread_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.CALL_READ)),
    session: AsyncSession = Depends(get_session),
):
    thread = await _resolve(session, ctx, thread_id)
    return _thread_out(await inbox_service.project(session, ctx.tenant, thread))


@router.get("/threads/{thread_id}/messages", response_model=list[MessageOut])
async def thread_messages(
    thread_id: str,
    limit: int = Query(default=200, ge=1, le=1_000),
    ctx: TenantContext = Depends(require_permission(Permission.CALL_READ)),
    session: AsyncSession = Depends(get_session),
):
    thread = await _resolve(session, ctx, thread_id)
    view = await inbox_service.project(session, ctx.tenant, thread)
    return [_message_out(message) for message in view.messages[-limit:]]


@router.get("/counts", response_model=CountsOut)
async def counts(
    ctx: TenantContext = Depends(require_permission(Permission.CALL_READ)),
    session: AsyncSession = Depends(get_session),
):
    """Queue counts for the tenant. Aggregate-only — no participant data."""
    return CountsOut(**await inbox_service.thread_counts(session, ctx.tenant))


@router.get("/unread-counts", response_model=dict)
async def unread_counts(
    ctx: TenantContext = Depends(require_permission(Permission.CALL_READ)),
    session: AsyncSession = Depends(get_session),
):
    """Per-thread unread badges for one tenant's conversations."""
    return await inbox_service.unread_counts(session, ctx.tenant)


# ------------------------------------------------------------- write side ---

@router.post("/threads/{thread_id}/messages", response_model=MessageOut, status_code=201)
async def append_message(
    thread_id: str,
    payload: MessageCreateRequest,
    ctx: TenantContext = Depends(require_permission(Permission.CALL_READ_ALL)),
    session: AsyncSession = Depends(get_session),
):
    """Append a message to a thread.

    ``direction`` decides the storage: ``inbound``/``outbound`` become ``Turn``
    rows (visible to transcripts and analytics), ``internal_note`` goes to the
    overlay and is marked as a note so it can never be mistaken for something
    the customer saw.
    """
    thread = await _resolve(session, ctx, thread_id)
    try:
        message = await inbox_service.append_message(
            session,
            ctx.tenant,
            thread,
            direction=_direction_of(payload.direction),
            body=payload.body,
            author_role=payload.author_role,
        )
    except BadRequestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return _message_out(message)


@router.post("/threads/{thread_id}/notes", response_model=ThreadOut, status_code=201)
async def add_note(
    thread_id: str,
    payload: NoteCreateRequest,
    ctx: TenantContext = Depends(require_permission(Permission.CALL_READ_ALL)),
    session: AsyncSession = Depends(get_session),
):
    thread = await _resolve(session, ctx, thread_id)
    try:
        return _thread_out(inbox_service.add_note(ctx.tenant, thread, payload.body))
    except BadRequestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.post("/threads/{thread_id}/assign", response_model=ThreadOut)
async def assign_thread(
    thread_id: str,
    payload: AssignRequest,
    ctx: TenantContext = Depends(require_permission(Permission.CALL_READ_ALL)),
    session: AsyncSession = Depends(get_session),
):
    thread = await _resolve(session, ctx, thread_id)
    return _thread_out(inbox_service.assign(ctx.tenant, thread, payload.assignee_id))


@router.post("/threads/{thread_id}/priority", response_model=ThreadOut)
async def set_priority(
    thread_id: str,
    payload: PriorityRequest,
    ctx: TenantContext = Depends(require_permission(Permission.CALL_READ_ALL)),
    session: AsyncSession = Depends(get_session),
):
    thread = await _resolve(session, ctx, thread_id)
    return _thread_out(
        inbox_service.set_priority(ctx.tenant, thread, _priority_of(payload.priority))
    )


@router.post("/threads/{thread_id}/tags", response_model=ThreadOut)
async def tag_thread(
    thread_id: str,
    payload: TagRequest,
    ctx: TenantContext = Depends(require_permission(Permission.CALL_READ_ALL)),
    session: AsyncSession = Depends(get_session),
):
    thread = await _resolve(session, ctx, thread_id)
    try:
        return _thread_out(inbox_service.tag(ctx.tenant, thread, payload.tag))
    except BadRequestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.post("/threads/{thread_id}/read", response_model=ThreadOut)
async def mark_read(
    thread_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.CALL_READ_ALL)),
    session: AsyncSession = Depends(get_session),
):
    thread = await _resolve(session, ctx, thread_id)
    return _thread_out(inbox_service.mark_read(ctx.tenant, thread))


@router.post("/threads/{thread_id}/unread", response_model=ThreadOut)
async def mark_unread(
    thread_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.CALL_READ_ALL)),
    session: AsyncSession = Depends(get_session),
):
    thread = await _resolve(session, ctx, thread_id)
    return _thread_out(inbox_service.mark_unread(ctx.tenant, thread))


@router.post("/threads/{thread_id}/escalate", response_model=ThreadOut)
async def escalate_thread(
    thread_id: str,
    payload: EscalateRequest,
    ctx: TenantContext = Depends(require_permission(Permission.CALL_READ_ALL)),
    session: AsyncSession = Depends(get_session),
):
    """Escalate to a human. This sets the escalation flag and the reason; the
    transfer itself remains the existing transfer path's job."""
    thread = await _resolve(session, ctx, thread_id)
    try:
        view = await inbox_service.escalate(session, ctx.tenant, thread, reason=payload.reason)
    except BadRequestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return _thread_out(view)


@router.post("/threads/{thread_id}/close", response_model=ThreadOut)
async def close_thread(
    thread_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.CALL_READ_ALL)),
    session: AsyncSession = Depends(get_session),
):
    thread = await _resolve(session, ctx, thread_id)
    try:
        view = await inbox_service.close(session, ctx.tenant, thread)
    except BadRequestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return _thread_out(view)


@router.post("/threads/{thread_id}/reopen", response_model=ThreadOut)
async def reopen_thread(
    thread_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.CALL_READ_ALL)),
    session: AsyncSession = Depends(get_session),
):
    thread = await _resolve(session, ctx, thread_id)
    try:
        view = await inbox_service.reopen(session, ctx.tenant, thread)
    except BadRequestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return _thread_out(view)


@router.get("/threads/{thread_id}/identity", response_model=dict)
async def thread_identity(
    thread_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.CALL_READ)),
    session: AsyncSession = Depends(get_session),
):
    """Echo the derived identity of a thread without exposing its participants.

    Handy for a client that stores the opaque id and wants to prove it still
    addresses the same channel — the id itself is a hash, so this is the only
    way to read back the pieces it was derived from.
    """
    thread = await _resolve(session, ctx, thread_id)
    view = await inbox_service.project(session, ctx.tenant, thread)
    return {
        "thread_id": thread_id,
        "derived_from": {
            "tenant_id": str(ctx.tenant_id),
            "channel": view.channel.value,
            "source_ref": str(thread.id),
        },
        "expected_id": make_thread_id(str(ctx.tenant_id), view.channel, str(thread.id)),
    }
```

==============================================================================
===== FILE: tests/test_enterprise_persistence.py (415 lines, sha256 1ef471b7578d32b75ac1d6bee4c75989da9245e98d29d7d307fa86f1421f4000) =====
==============================================================================
```python
"""Batch 02 persistence — the registries' state survives a restart.

Batch 01 was explicit that the automation, notification and inbox services kept
their state in process-local dictionaries and that "a migration is reported at
the end of the batch". Batch 02 supplies both halves: the schema
(``alembic/versions/0012_enterprise_persistence.py``) and the scope that
hydrates/flushes those dictionaries around each request
(``app/services/enterprise_store.py``).

The tests below simulate a restart the honest way: they clear the *entire*
in-process state between requests (exactly what a new worker process starts
with) and then re-read through the API. Anything the second read returns must
have come back out of the database, because there is nowhere else for it to
come from.

They also pin the two properties that make the swap safe:

* the database is authoritative — a failed request writes nothing, and
* hydration is tenant-scoped — one tenant's rows never appear in another's view.
"""
from __future__ import annotations

import pytest
import pytest_asyncio

from app.domain.automation_models import TriggerEvent
from app.services import automation_service, inbox_service, notification_service
from app.services.enterprise_store import _ROWS
from tests.conftest import auth_headers


@pytest_asyncio.fixture
async def app_module_routes(app):
    """The real app's route table (mirrors the Batch 02 test module)."""
    return app.routes


def _forget_everything() -> None:
    """Wipe all in-process state: the post-restart starting position.

    This is deliberately harsher than a real restart (it drops every tenant, not
    just one) so a passing assertion cannot be explained by leftover globals.
    """
    automation_service._REGISTRY.clear()
    automation_service._RUNS.clear()
    automation_service._LAST_RUN_AT.clear()
    notification_service._TEMPLATES.clear()
    notification_service._NOTIFICATIONS.clear()
    notification_service._DEDUPE.clear()
    notification_service._READ.clear()
    inbox_service._OVERLAY.clear()
    _ROWS.clear()


@pytest.fixture(autouse=True)
def _isolated_registries():
    """Same isolation discipline as the Batch 02 suite: no cross-test tenants."""
    _forget_everything()
    yield
    _forget_everything()


def _automation_body(name: str = "Negative sentiment alert") -> dict:
    return {
        "name": name,
        "event": "call_completed",
        "description": "Escalate when a call ends badly",
        "filters": [{"field": "sentiment", "operator": "eq", "value": "negative"}],
        "actions": [
            {"name": "record_escalation_intent", "params": {"destination": "+15550009999"}}
        ],
        "max_per_event": 1,
        "cooldown_seconds": 0,
    }


def _template_body(channel: str = "in_app") -> dict:
    return {
        "name": "Booking confirmed",
        "channel": channel,
        "body": "Hi {customer}, your appointment {when} is confirmed.",
        "variables": ["customer", "when"],
    }


def _notification_body(template_id: str, business_key: str = "appt-42") -> dict:
    return {
        "template_id": template_id,
        "recipient": {"kind": "phone", "target": "+15551234567"},
        "event_source": "appointment_reminder",
        "business_key": business_key,
        "variables": {"customer": "Ada", "when": "Tuesday"},
    }


async def _open_thread(client, headers, *, channel: str = "web",
                       customer: str = "+15550001111") -> dict:
    response = await client.post(
        "/api/inbox/threads",
        json={"channel": channel, "customer": customer, "initial_message": "hello"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


# =================================================== the migration is present ===

class TestSchema:
    async def test_the_five_batch02_tables_exist_in_the_metadata(self):
        from app.db.models import Base

        for table in ("automations", "automation_runs", "notification_templates",
                      "notifications", "inbox_thread_states"):
            assert table in Base.metadata.tables, f"{table} has no model"

    async def test_the_revision_chains_off_the_previous_head(self):
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        script = ScriptDirectory.from_config(Config("alembic.ini"))
        heads = script.get_heads()
        assert heads == ["0012_enterprise_persistence"]
        revision = script.get_revision("0012_enterprise_persistence")
        assert revision.down_revision == "0011_side_effect_exactly_once"


# ======================================================== automations, durable ===

class TestAutomationsSurviveRestart:
    async def test_definition_and_run_history_outlive_the_process(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        created = await client.post("/api/automations", json=_automation_body(), headers=headers)
        assert created.status_code == 201, created.text
        automation_id = created.json()["id"]
        assert (await client.post(
            f"/api/automations/{automation_id}/enable", headers=headers
        )).json()["status"] == "enabled"
        run = await client.post(
            f"/api/automations/{automation_id}/run",
            json={"business_event_id": "call-1", "payload": {"sentiment": "negative"}},
            headers=headers,
        )
        assert run.status_code in (200, 201), run.text

        # ---- restart ----
        _forget_everything()

        fetched = await client.get(f"/api/automations/{automation_id}", headers=headers)
        assert fetched.status_code == 200, fetched.text
        body = fetched.json()
        assert body["name"] == "Negative sentiment alert"
        assert body["status"] == "enabled"                    # not silently disabled
        assert body["filters"] == [
            {"field": "sentiment", "operator": "eq", "value": "negative"}
        ]
        assert body["actions"][0]["name"] == "record_escalation_intent"

        runs = await client.get(f"/api/automations/{automation_id}/runs", headers=headers)
        assert [r["business_event_id"] for r in runs.json()] == ["call-1"]

    async def test_evaluation_still_works_after_a_restart(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        created = await client.post("/api/automations", json=_automation_body(), headers=headers)
        automation_id = created.json()["id"]
        await client.post(f"/api/automations/{automation_id}/enable", headers=headers)

        _forget_everything()

        matched = await client.post(
            "/api/automations/evaluate",
            json={"event": "call_completed", "payload": {"sentiment": "negative"}},
            headers=headers,
        )
        assert [m["automation_id"] for m in matched.json()] == [automation_id]
        # And the filter is still the filter: a positive call does not match.
        missed = await client.post(
            "/api/automations/evaluate",
            json={"event": "call_completed", "payload": {"sentiment": "positive"}},
            headers=headers,
        )
        assert missed.json() == []

    async def test_create_is_still_idempotent_on_name_across_restarts(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        first = await client.post("/api/automations", json=_automation_body(), headers=headers)
        _forget_everything()
        second = await client.post("/api/automations", json=_automation_body(), headers=headers)
        assert second.status_code == 201, second.text
        assert second.json()["id"] == first.json()["id"]

        listed = await client.get("/api/automations", headers=headers)
        assert [a["id"] for a in listed.json()] == [first.json()["id"]]

    async def test_a_refused_create_writes_nothing(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        bad = {**_automation_body(), "event": "teleport_completed"}
        assert (await client.post("/api/automations", json=bad, headers=headers)).status_code == 422

        _forget_everything()                                    # nothing to recover from...
        assert (await client.get("/api/automations", headers=headers)).json() == []


# ====================================================== notifications, durable ===

class TestNotificationsSurviveRestart:
    async def test_template_and_notification_outlive_the_process(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        template = await client.post(
            "/api/notifications/templates", json=_template_body(), headers=headers
        )
        template_id = template.json()["id"]
        created = await client.post(
            "/api/notifications", json=_notification_body(template_id), headers=headers
        )
        assert created.status_code == 201, created.text
        notification_id = created.json()["id"]

        _forget_everything()

        listed = await client.get("/api/notifications", headers=headers)
        assert [n["id"] for n in listed.json()] == [notification_id]
        reopened = await client.get(f"/api/notifications/{notification_id}", headers=headers)
        assert reopened.status_code == 200, reopened.text
        assert reopened.json()["template_id"] == template_id
        assert reopened.json()["recipient"]["target_masked"] == "***4567"
        assert "+15551234567" not in reopened.text        # PII stays masked after a reload

    async def test_dedupe_and_read_state_survive_a_restart(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        template_id = (await client.post(
            "/api/notifications/templates", json=_template_body(), headers=headers
        )).json()["id"]
        body = _notification_body(template_id)
        first = await client.post("/api/notifications", json=body, headers=headers)
        notification_id = first.json()["id"]
        await client.post(f"/api/notifications/{notification_id}/read", headers=headers)

        _forget_everything()

        # The dedupe index is a database unique constraint, so a replayed create
        # after a restart still returns the same notification instead of a twin.
        second = await client.post("/api/notifications", json=body, headers=headers)
        assert second.status_code == 201, second.text
        assert second.json()["id"] == notification_id
        assert len((await client.get("/api/notifications", headers=headers)).json()) == 1

        unread = await client.get(
            "/api/notifications", params={"unread_only": True}, headers=headers
        )
        assert unread.json() == []                        # the read flag was persisted

        summary = await client.get("/api/notifications/states/summary", headers=headers)
        assert summary.status_code == 200
        assert summary.json()["pending"] == 1      # still awaiting delivery, counted once

    async def test_render_still_uses_the_single_brace_syntax_after_a_restart(
        self, client, manager_a
    ):
        headers = await auth_headers(client, manager_a)
        template_id = (await client.post(
            "/api/notifications/templates", json=_template_body(), headers=headers
        )).json()["id"]

        _forget_everything()

        rendered = await client.post(
            f"/api/notifications/templates/{template_id}/render",
            json={"variables": {"customer": "Ada", "when": "Tuesday"}},
            headers=headers,
        )
        assert rendered.status_code == 200, rendered.text
        assert rendered.json()["body"] == "Hi Ada, your appointment Tuesday is confirmed."


# ============================================================= inbox, durable ===

class TestInboxSurvivesRestart:
    async def test_overlay_state_outlives_the_process(self, client, manager_a, agent_a):
        headers = await auth_headers(client, manager_a)
        thread = await _open_thread(client, headers)
        thread_id = thread["id"]
        await client.post(
            f"/api/inbox/threads/{thread_id}/assign",
            json={"assignee_id": "agent-7"}, headers=headers)
        await client.post(
            f"/api/inbox/threads/{thread_id}/priority",
            json={"priority": "high"}, headers=headers)
        await client.post(
            f"/api/inbox/threads/{thread_id}/tags",
            json={"tag": "billing"}, headers=headers)
        await client.post(
            f"/api/inbox/threads/{thread_id}/tags",
            json={"tag": "urgent"}, headers=headers)
        await client.post(
            f"/api/inbox/threads/{thread_id}/notes",
            json={"body": "internal hint"}, headers=headers)
        await client.post(f"/api/inbox/threads/{thread_id}/unread", headers=headers)

        _forget_everything()

        fetched = await client.get(f"/api/inbox/threads/{thread_id}", headers=headers)
        assert fetched.status_code == 200, fetched.text
        body = fetched.json()
        assert body["status"] == "assigned"               # derived from the assignee
        assert body["assignee_id"] == "agent-7"
        assert body["priority"] == "high"
        assert set(body["tags"]) == {"billing", "urgent"}
        assert body["internal_notes"] == ["internal hint"]
        assert body["unread_count"] == 1

        counts = await client.get("/api/inbox/unread-counts", headers=headers)
        assert counts.status_code == 200
        # One badge, still set: the endpoint keys by the internal call id (the
        # opaque thread id is a hash of it, deliberately not reversible), so the
        # assertion is on the badge set rather than on a guessable key.
        assert list(counts.json().values()) == [1]

    async def test_messages_are_still_ordered_and_notes_still_internal(
        self, client, manager_a
    ):
        headers = await auth_headers(client, manager_a)
        thread_id = (await _open_thread(client, headers))["id"]
        await client.post(
            f"/api/inbox/threads/{thread_id}/notes",
            json={"body": "internal hint"}, headers=headers)

        _forget_everything()

        messages = await client.get(
            f"/api/inbox/threads/{thread_id}/messages", headers=headers
        )
        assert messages.status_code == 200, messages.text
        assert [m["direction"] for m in messages.json()] == ["inbound", "internal_note"]

    async def test_escalation_and_reopen_still_follow_the_transition_table(
        self, client, manager_a
    ):
        headers = await auth_headers(client, manager_a)
        thread_id = (await _open_thread(client, headers))["id"]
        assert (await client.post(
            f"/api/inbox/threads/{thread_id}/escalate",
            json={"reason": "customer asked for a supervisor"}, headers=headers,
        )).json()["status"] == "escalated"

        _forget_everything()

        # ESCALATED -> CLOSED -> OPEN is legal; ESCALATED is still not a status
        # you can jump straight out of an arbitrary way.
        closed = await client.post(f"/api/inbox/threads/{thread_id}/close", headers=headers)
        assert closed.json()["status"] == "closed"
        reopened = await client.post(f"/api/inbox/threads/{thread_id}/reopen", headers=headers)
        assert reopened.status_code == 200, reopened.text
        assert reopened.json()["status"] == "open"


# ============================================================ tenant isolation ===

class TestHydrationIsTenantScoped:
    async def test_one_tenants_rows_never_appear_in_anothers_view(
        self, client, manager_a, owner_b
    ):
        headers_a = await auth_headers(client, manager_a)
        headers_b = await auth_headers(client, owner_b)

        created = await client.post("/api/automations", json=_automation_body(), headers=headers_a)
        automation_id = created.json()["id"]
        template_id = (await client.post(
            "/api/notifications/templates", json=_template_body(), headers=headers_a
        )).json()["id"]
        await client.post(
            "/api/notifications", json=_notification_body(template_id), headers=headers_a
        )
        thread_id = (await _open_thread(client, headers_a))["id"]

        _forget_everything()

        # Tenant B, hydrated from the same tables, sees its own (empty) rows.
        assert (await client.get("/api/automations", headers=headers_b)).json() == []
        assert (await client.get("/api/notifications", headers=headers_b)).json() == []
        assert (await client.get("/api/inbox/threads", headers=headers_b)).json() == []

        # A genuinely correct id from tenant A is still not found by tenant B.
        for path in (f"/api/automations/{automation_id}",
                     f"/api/notifications/{template_id}",
                     f"/api/inbox/threads/{thread_id}"):
            response = await client.get(path, headers=headers_b)
            assert response.status_code in (403, 404), (path, response.status_code)

        # Tenant A's own view is intact after B's reads.
        assert [a["id"] for a in (
            await client.get("/api/automations", headers=headers_a)
        ).json()] == [automation_id]


# ======================================================= the scope's own rules ===

class TestTenantScope:
    async def test_the_scope_clears_the_registry_when_the_handler_raises(self, db):
        """A failing operation must leave the process as it found it."""
        from app.services.enterprise_store import tenant_scope

        tenant_id = "00000000-0000-0000-0000-00000000c0de"
        automation_service._REGISTRY.setdefault(tenant_id, {})["leak"] = object()
        try:
            async with tenant_scope(db, tenant_id):
                automation_service._REGISTRY.setdefault(tenant_id, {})["leak"] = object()
                raise RuntimeError("handler failed")
        except RuntimeError:
            pass
        assert automation_service._REGISTRY.get(tenant_id, {}).get("leak") is None

    async def test_unknown_events_are_still_refused_by_the_domain(self):
        with pytest.raises(ValueError):
            TriggerEvent("teleport_completed")
```

==============================================================================
===== FILE: tests/test_deployment.py (359 lines, sha256 768f79be85b18fa2d3e258ebbea96995f954c34f6b840aef827e12c13f9b7c27) =====
==============================================================================
```python
"""Step 8 — deployment & disaster-recovery invariants.

Every test here is deterministic and requires no Docker, no live Postgres and
no network: they inspect the repository's configuration, scripts and source as
the evidence they are. The migration-chain test parses the Alembic revisions
themselves; the compose/Caddy/Dockerfile tests parse the files as YAML/text;
the Redis tests exercise the degradation path directly. Where a real runtime
check is possible (a live Postgres, a running stack) it is a separate,
opt-in validation — never assumed here.
"""
from __future__ import annotations

import base64
import os
import re
from pathlib import Path

import pytest
import yaml

from app.core.config import Settings

REPO = Path(__file__).resolve().parent.parent


def _valid_prod(**overrides) -> Settings:
    """A production Settings that passes every non-test production rule, so a
    single override can be asserted against cleanly. All values are fake."""
    key = base64.urlsafe_b64encode(os.urandom(32)).decode()
    values = dict(
        app_env="production",
        public_base_url="https://app.example.com",
        cors_origins="https://app.example.com",
        rate_limit_enabled=True,
        log_level="INFO",
        secret_key="a" * 48,
        jwt_secret="b" * 48,
        twilio_account_sid="AC" + "0" * 32,
        twilio_auth_token="c" * 32,
        twilio_phone_number="+15550001111",
        deepgram_api_key="d" * 32,
        openai_api_key="sk-" + "e" * 32,
        elevenlabs_api_key="f" * 32,
        crm_encryption_keys=f"k1:{key}",
        knowledge_embedding_provider="openai",
        knowledge_embedding_model="text-embedding-3-small",
        knowledge_embedding_dimensions=1536,
        billing_provider="manual",
    )
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _staging(**overrides) -> Settings:
    key = base64.urlsafe_b64encode(os.urandom(32)).decode()
    values = dict(
        app_env="staging",
        public_base_url="https://staging.example.com",
        secret_key="s" * 48,
        jwt_secret="t" * 48,
        crm_encryption_keys=f"k1:{key}",
        rate_limit_enabled=False,
        e2e_enabled=False,
    )
    values.update(overrides)
    return Settings(_env_file=None, **values)


# ----------------------------------------------------------- (1)(2)(3) config ---

def test_production_rejects_e2e_mode():
    s = _valid_prod(e2e_enabled=True, e2e_test_number="+15550001111",
                    e2e_allowed_callers="+15550001111")
    problems = s.validate_security()
    assert any("E2E_ENABLED" in p for p in problems)


def test_production_rejects_rate_limit_off():
    s = _valid_prod(rate_limit_enabled=False)
    problems = s.validate_security()
    assert any("RATE_LIMIT_ENABLED" in p for p in problems)


def test_production_rejects_debug_log_level():
    s = _valid_prod(log_level="DEBUG")
    problems = s.validate_security()
    assert any("DEBUG" in p for p in problems)


def test_production_rejects_placeholder_secrets():
    s = _valid_prod(twilio_auth_token="xxxxxxxxxxxx")
    problems = s.validate_security()
    assert any("TWILIO_AUTH_TOKEN" in p for p in problems)


def test_production_rejects_development_cors_origins():
    s = _valid_prod(cors_origins="http://localhost:5173,https://app.example.com")
    problems = s.validate_security()
    assert any("https" in p and "localhost" in p for p in problems)


def test_production_rejects_localhost_base_url():
    s = _valid_prod(public_base_url="https://localhost:8000")
    problems = s.validate_security()
    assert any("localhost" in p for p in problems)


def test_fully_configured_production_is_clean():
    assert _valid_prod().validate_security() == []


def test_unknown_app_env_is_rejected():
    s = Settings(_env_file=None, app_env="qa")
    problems = s.validate_security()
    assert any("APP_ENV" in p for p in problems)


def test_staging_is_not_production():
    s = _staging()
    assert s.is_staging is True
    assert s.is_production is False
    assert s.uses_https is True


def test_staging_accepts_rate_limit_off_and_e2e_disarmed():
    s = _staging(rate_limit_enabled=False, e2e_enabled=False)
    problems = s.validate_security()
    assert not any("RATE_LIMIT_ENABLED" in p for p in problems)
    assert not any("E2E_ENABLED must not be set in production" in p for p in problems)


def test_secure_cookie_is_scheme_driven():
    assert Settings(_env_file=None, public_base_url="https://x.example.com").uses_https
    assert not Settings(_env_file=None, public_base_url="http://localhost:8000").uses_https


# ------------------------------------------------------- (4)(5) health endpoints ---

def test_main_defines_liveness_and_readiness():
    src = (REPO / "app" / "main.py").read_text()
    assert '@app.get("/health")' in src
    assert '@app.get("/health/ready")' in src
    assert "health_check.readiness" in src


def test_staging_does_not_run_create_all():
    src = (REPO / "app" / "main.py").read_text()
    # The dev/test-only schema bootstrap must exclude staging.
    assert '"development", "test"' in src


# ---------------------------------------------- (6) websocket / proxy assumptions ---

def test_caddy_preserves_websocket_and_adds_security():
    text = (REPO / "Caddyfile").read_text()
    assert "reverse_proxy api:8000" in text
    assert "admin off" in text
    assert "Strict-Transport-Security" in text
    assert "25MB" in text
    # Nothing may disable the WebSocket upgrade the voice stream depends on.
    assert "header_upgrade" not in text.lower()


def test_entrypoint_trusts_proxy_headers():
    text = (REPO / "scripts" / "entrypoint.sh").read_text()
    assert "--proxy-headers" in text
    assert "scripts/migrate.py" in text


# ------------------------------------------------------------ (7) migrations ---

def test_migration_chain_is_linear_with_no_gaps():
    versions = sorted(
        p for p in (REPO / "alembic" / "versions").iterdir() if p.suffix == ".py"
    )
    ids: dict[str, str | None] = {}
    for path in versions:
        text = path.read_text()
        rev = re.search(r'^revision = "(.+)"', text, re.M).group(1)
        down = re.search(r'^down_revision = (.+)$', text, re.M).group(1).strip()
        ids[rev] = None if down == "None" else down.strip('"')
    assert ids, "no migrations found"
    # Exactly one head: the revision nobody points down_revision at.
    heads = [r for r in ids if r not in {d for d in ids.values() if d}]
    assert heads == ["0012_enterprise_persistence"]
    # Exactly one base (down_revision None), and a single linear walk.
    bases = [r for r, d in ids.items() if d is None]
    assert bases == ["0001_baseline"]
    current: str | None = heads[0]
    visited = 0
    while current is not None:
        visited += 1
        current = ids[current]
    assert visited == len(ids)


# --------------------------------------------- (8)(9) backup/restore safety ---

def test_backup_scripts_use_environment_not_hardcoded_secrets():
    for name in ("backup.sh", "restore.sh", "backup_verify.sh"):
        text = (REPO / "scripts" / name).read_text()
        for marker in ("sk_live_", "AKIA", "-----BEGIN", "password=voxdesk"):
            assert marker not in text, f"{name} contains {marker!r}"
    backup = (REPO / "scripts" / "backup.sh").read_text()
    assert "POSTGRES_USER" in backup and "PGHOST" in backup
    assert "pg_restore --list" in backup  # integrity check before retention


def test_restore_refuses_overwrite_and_supports_drill():
    text = (REPO / "scripts" / "restore.sh").read_text()
    assert "RESTORE_TARGET_DB" in text      # drill restores into a scratch DB
    assert "RESTORE_ALLOW_OVERWRITE" in text  # never silently clobber a live DB
    assert "pg_restore --list" in text      # verify before writing


# ------------------------------------------------------ (10) secret exposure ---

def test_gitignore_covers_secrets_and_dumps():
    text = (REPO / ".gitignore").read_text()
    for entry in (".env", "secrets/", "backups/", "*.dump", "*.pem", "*.key", ".netrc"):
        assert entry in text


def test_dockerignore_excludes_secrets_and_dumps():
    text = (REPO / ".dockerignore").read_text()
    for entry in (".env", "secrets/", "backups/", "*.dump", "*.pem", "*.key"):
        assert entry in text


def test_env_examples_contain_only_placeholder_credentials():
    for name in (".env.example", ".env.staging.example"):
        text = (REPO / name).read_text()
        for marker in ("sk_live_", "AKIA", "BEGIN RSA", "BEGIN PRIVATE"):
            assert marker not in text, f"{name} contains {marker!r}"
    example = (REPO / ".env.example").read_text()
    assert "sk-xxxxxxxx" in example
    assert "change-me" in example


# ---------------------------------------------- (11)(12) docker/compose invariants ---

def test_dockerfile_runs_non_root_and_healthchecks():
    text = (REPO / "Dockerfile").read_text()
    assert "USER appuser" in text
    assert "HEALTHCHECK" in text
    assert "python:3.12-slim-bookworm" in text
    assert "COPY .env" not in text


def test_prod_compose_keeps_datastores_off_the_host():
    prod = yaml.safe_load((REPO / "docker-compose.prod.yml").read_text())
    assert "ports" not in prod["services"]["db"]
    assert "ports" not in prod["services"]["redis"]
    api_ports = [str(p) for p in prod["services"]["api"]["ports"]]
    assert api_ports and all(p.startswith("127.0.0.1:") for p in api_ports)
    assert prod["services"]["api"]["depends_on"]["db"]["condition"] == "service_healthy"
    assert prod["services"]["api"].get("init") is True
    assert prod["services"]["backup"]["depends_on"]["db"]["condition"] == "service_healthy"


def test_staging_compose_is_isolated_from_production():
    st = yaml.safe_load((REPO / "docker-compose.staging.yml").read_text())
    assert st["services"]["api"]["environment"]["APP_ENV"] == "staging"
    assert "pgdata_staging" in st.get("volumes", {})
    api_vols = " ".join(str(v) for v in st["services"]["api"]["volumes"])
    assert "secrets-staging" in api_vols
    api_ports = " ".join(str(p) for p in st["services"]["api"]["ports"])
    assert "8001" in api_ports
    # Staging scheduler inherits the staging env file, not production's.
    assert st["services"]["scheduler"]["env_file"] == ".env.staging"


# ----------------------------------------------------- (13) CI workflow invariants ---

def test_ci_workflow_gates_and_separates_real_providers():
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text()
    assert "alembic upgrade head" in ci
    assert "npm ci" in ci
    assert "not real_provider" in ci
    assert "docker build ." in ci
    assert "npm audit --omit=dev" in ci
    real = (REPO / ".github" / "workflows" / "real-integrations.yml").read_text()
    assert "workflow_dispatch" in real
    sec = (REPO / ".github" / "workflows" / "security-scan.yml").read_text()
    assert "bandit" in sec and "pip-audit" in sec and "gitleaks" in sec


# ------------------------------------------- (14) monitoring endpoint protection ---

def test_metrics_endpoint_is_token_gated_when_configured():
    src = (REPO / "app" / "core" / "metrics.py").read_text()
    assert "_scrape_authorized" in src
    assert "METRICS_TOKEN" in src


def test_caddy_does_not_expose_metrics_as_a_separate_route():
    text = (REPO / "Caddyfile").read_text()
    assert "/metrics" not in text


# ---------------------------------------------- (15) redis restart behaviour ---

@pytest.mark.asyncio
async def test_redis_cache_degrades_gracefully_when_down(monkeypatch):
    from app.core.cache import _RedisCache

    class _Down:
        async def ping(self):
            raise ConnectionError("redis down")

        async def get(self, key):
            raise ConnectionError("redis down")

        async def set(self, key, value, ex):
            raise ConnectionError("redis down")

        async def delete(self, key):
            raise ConnectionError("redis down")

    rc = _RedisCache("redis://127.0.0.1:1/0")
    monkeypatch.setattr(rc, "_get", lambda: _Down())
    assert await rc.ping() is False
    assert await rc.get("k") is None
    await rc.set("k", "v", 60)   # must not raise
    await rc.delete("k")          # must not raise


@pytest.mark.asyncio
async def test_memory_cache_ping_reports_reachable():
    from app.core.cache import _MemoryCache

    assert await _MemoryCache().ping() is True


# ----------------------------------------------------- (16) rollback guard ---

def test_rollback_checks_out_and_redeploys_without_pull():
    text = (REPO / "scripts" / "rollback.sh").read_text()
    assert "git checkout" in text
    assert "deploy.sh --no-pull" in text


def test_rollback_never_executes_alembic_downgrade():
    """The downgrade command may appear only in comments/documentation; the
    script must never run it. Database downgrades are operator-only."""
    for line in (REPO / "scripts" / "rollback.sh").read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        assert "alembic downgrade" not in stripped
        assert "command.downgrade" not in stripped


def test_deploy_script_backs_up_before_migrating():
    text = (REPO / "scripts" / "deploy.sh").read_text()
    assert "pg_dump" in text
    assert "pg_restore --list" in text          # integrity check
    assert "scripts/migrate.py" in text          # advisory-locked migration
    assert "/health/ready" in text               # readiness wait
```

==============================================================================
===== FILE: docs/DEPLOYMENT.md (299 lines, sha256 da8d2c0bb2048516b3e3d954f783cf69b0385fd680bb0547063ab72173bf0240) =====
==============================================================================
```markdown
# VoxDesk — Deployment (staging & production)

This document is the operator runbook for taking VoxDesk from development →
staging → production. It covers the deployment dependency graph, environment
separation, the safe deploy/rollback sequence, migrations, backup/restore, TLS
and the security controls that hold it together. Anything that is *enforced*
here is enforced by code (`Settings.validate_security()`, `scripts/migrate.py`,
`scripts/deploy.sh`, `scripts/restore.sh`) and verified by
`tests/test_deployment.py` — not just prose.

## 1. Deployment dependency graph

Built from the repository, not assumed. Arrows mean "depends on to serve or
run correctly":

```
                     ┌──────────────┐
                     │    caddy     │  TLS termination, security headers,
                     │  (80/443)    │  request-size ceiling, WebSocket passthrough
                     └──────┬───────┘
                            │ reverse_proxy api:8000  (and grafana:3000 when
                            │ GRAFANA_DOMAIN is set)
                ┌───────────▼───────────┐
                │         api           │  FastAPI + built dashboard
                │  (127.0.0.1:8000)     │  entrypoint → migrate.py → uvicorn
                └──┬───────┬────────┬───┘
        ┌──────────▼──┐ ┌──▼──────┐ │ ┌──────────────┐
        │     db      │ │  redis  │ │ │  prometheus  │ scrapes api:8000/metrics
        │  PostgreSQL │ │ cache + │ │ └──────┬───────┘
        │  (internal) │ │ rate    │ │   ┌────▼─────┐
        └──────┬──────┘ │ limit   │ │   │ grafana  │ (127.0.0.1:3000, internal)
               │        └─────────┘ │   └──────────┘
        ┌──────▼──────────────────┐ │
        │  scheduler (worker)     │ │  reminders, campaigns, CRM sync,
        │  depends_on api+db+redis│ │  knowledge ingestion, billing
        └─────────────────────────┘ │  reconciliation, retention
        ┌───────────────────────────▼─┐
        │  backup (nightly pg_dump)   │  depends_on db: service_healthy
        └─────────────────────────────┘
```

Ordering is enforced in `docker-compose.prod.yml`: `api` waits for `db` and
`redis` to be *healthy*; `scheduler` waits for `db` healthy and `api` started;
`backup`, `prometheus` and `caddy` wait for their upstreams. `db` and `redis`
expose **no host ports** — they are reachable only on the compose network —
and `api`/`grafana` bind to `127.0.0.1` so nothing can bypass Caddy.

## 2. Environments

Three environments, strictly separated:

| | development | staging | production |
|---|---|---|---|
| Compose file | `docker-compose.yml` | `docker-compose.staging.yml` | `docker-compose.prod.yml` |
| `APP_ENV` | `development` | `staging` | `production` |
| Env file | `.env` | `.env.staging` | `.env` |
| Schema owner | `create_all` (dev convenience) | **Alembic only** | **Alembic only** |
| DB/Redis/volumes | local | `*_staging` (separate) | `pgdata`/`redisdata` |
| Secrets | dev placeholders | **its own** (`secrets-staging/`) | `secrets/` |
| Stripe | — | test-mode keys only | live keys |
| E2E guard (Step 5) | allowed | allowed (default off) | **refused at boot** |
| Failure injection | allowed | allowed | refused |
| Host ports | `8000`, `3000` | `8001`, `3001`, `8080/8443` | `8000`, `3000`, `80/443` |

Rules:

* **Never copy production secrets into staging.** Staging uses its own
  `.env.staging`, its own `secrets-staging/` directory, its own volumes
  (`pgdata_staging`, …), its own database and its own test-mode provider
  credentials (Twilio test account, Stripe `sk_test_*`, etc.). Template:
  `.env.staging.example`.
* `validate_security()` treats staging as **not production** (E2E and failure
  injection are permitted) but also as **not development** (no `create_all`,
  Alembic owns the schema). Staging therefore exercises the same migration,
  readiness and proxy path production does, without touching production data.
* `KNOWN_APP_ENVS = {development, test, staging, production, prod}` — any other
  `APP_ENV` fails at boot.

### Sandbox limitations (this repository's environment)

This workspace has no Docker daemon, so container-level staging/production
validation cannot run here. PostgreSQL 17 **is** available locally, so
migration and restore logic can be exercised against it. Everywhere else the
validation is static and deterministic (see `tests/test_deployment.py`). Real
runtime claims are never fabricated: when a check needs Docker or a live
stack, that blocker is stated explicitly rather than simulated.

## 3. Production configuration (fail-closed)

`Settings.validate_security()` runs at startup and, in production, **refuses
to boot** on any of:

* `RATE_LIMIT_ENABLED` not `true`
* `LOG_LEVEL=DEBUG`
* `PUBLIC_BASE_URL` not `https://`, or containing `localhost`/`127.0.0.1`
* any `CORS_ORIGINS` entry that is not `https://`
* placeholder-looking secrets (markers like `change-me`, `xxxx`,
  `placeholder`, …) in `SECRET_KEY`, `JWT_SECRET`, `TWILIO_AUTH_TOKEN`,
  `DEEPGRAM_API_KEY`, `ELEVENLABS_API_KEY`, the LLM keys, `STRIPE_*`
* `TWILIO_AUTH_TOKEN` empty, or `TWILIO_SKIP_WEBHOOK_VERIFY` enabled
* `E2E_ENABLED` set (real-call E2E is operator-run in a test environment only)
* `KNOWLEDGE_EMBEDDING_PROVIDER=hashing` (development stub)
* `CRM_ENCRYPTION_KEYS` empty (provider credentials would be stored plaintext)
* `BILLING_PROVIDER=stripe` with a `sk_test_*` key, or
  `BILLING_UNLIMITED_ENTITLEMENTS` enabled
* `JWT_SECRET` still the default or shorter than 32 chars; `SECRET_KEY` default

There is **no second configuration system** — everything lives in
`app/core/config.py` on the existing `Settings` architecture. `uses_https` is
derived from the actual `PUBLIC_BASE_URL` scheme, so the Secure-cookie flag
(`auth_routes.py`) and the HSTS header (`security_headers.py`) follow the real
TLS posture instead of guessing from the environment name.

## 4. Deploy sequence (safe, single-VM)

`scripts/deploy.sh` (idempotent; flags `--no-build`, `--no-backup`, `--no-pull`):

1. `git pull --ff-only` (skipped under rollback)
2. build images
3. **pre-deploy backup** of the database, then **verify** the dump with
   `pg_restore --list`. A deploy that cannot back up (or whose backup fails
   verification) aborts before touching the schema — the backup is the
   rollback point.
4. **migrations under an advisory lock** — `scripts/migrate.py` (below)
5. recreate containers onto the new image
6. wait up to 120 s for `/health/ready`
7. verify `/health` and report

On any failure the script prints the rollback command. It **never** downgrades
the database automatically.

### Honest zero-downtime statement

This is a **single-VM, single-Postgres topology**, so it **does not guarantee
zero-downtime**. `docker compose up -d` recreates the `api` container, which
means a brief (seconds) connection drop on the public path while Caddy
retries. There is no blue/green deployment, no load balancer, and no
online/offline schema pattern. Migrations run *before* the new code serves, so
requests are not served against a half-migrated schema; that is the only
uptime guarantee claimed. If true zero-downtime is required, the deployment
must grow a second host, a load balancer and an expand/contract migration
cadence — out of scope for this step and stated here rather than pretended.

## 5. Migrations and rollback

`scripts/migrate.py` is the **only** sanctioned way the API applies migrations
in staging/production. It acquires a session-level PostgreSQL advisory lock
(`pg_try_advisory_lock`, key `7272_0011`) and then runs
`alembic upgrade head` in a subprocess. Concurrent container starts serialize
on the lock instead of racing DDL. If the lock is not acquired within
`MIGRATE_LOCK_TIMEOUT_SECONDS` (default 600 s) it fails loudly. Startup never
silently skips migrations: the entrypoint runs `migrate.py` and exits on
failure, so the container never serves with a stale schema.

Migration chain (`alembic/versions/`): a single linear head at
`0012_enterprise_persistence` with no gaps back to `0001_baseline`
(enforced by `tests/test_deployment.py`). Revision `0012` adds the five tables
behind the automation/notification/inbox surface (`automations`,
`automation_runs`, `notification_templates`, `notifications`,
`inbox_thread_states`); before it those services kept their state in process
memory, so a restart lost automations, notifications and unread badges. `alembic.ini`/`env.py` use the
SQLAlchemy URL from settings.

**Rollback policy:**

* `scripts/rollback.sh <git-sha>` checks out the sha and runs
  `deploy.sh --no-pull`. It records the *applied* migration revision first, so
  the operator can see whether the target sha predates the schema.
* **It never runs `alembic downgrade`.** Forward migrations can be destructive
  in principle (a `downgrade` can drop columns and lose data). Downgrades are
  a separate, explicitly-authorized, operator-run action.
* Migrations are written **additive/backward-compatible wherever practical**,
  so an old app against a newer schema keeps working — code rollback then
  needs no schema rollback. When a change cannot be backward-compatible it is
  released as its own step and documented as "no code rollback past this point
  without a manual downgrade".

## 6. Backup and restore

**Schedule/format/retention:** the `backup` service (and `scripts/backup.sh`)
take a nightly `pg_dump` custom-format (`-Fc`) archive; the last 14 are kept
locally; optional `rclone` sync to an S3-compatible remote when `RCLONE_REMOTE`
is set. **Off-site is required, not optional**: backups must not live only in
the same failure domain as the primary database — keep `RCLONE_REMOTE` (or an
equivalent) pointed somewhere outside the primary VM.

**Verification:** every dump — nightly, pre-deploy, or manual — is verified
with `pg_restore --list` before it is retained or synced; an unreadable dump
is reported and never counted as a backup (`backup_verify.sh` does this
standalone). `backup.sh` exits non-zero on an unreadable dump.

**Restore (`scripts/restore.sh`):**

1. verifies the dump with `pg_restore --list` **before writing anything**
2. refuses to restore over a database that already has tables unless
   `RESTORE_ALLOW_OVERWRITE=1` — a drill can never silently clobber a live DB
3. `RESTORE_TARGET_DB=voxdesk_restore_drill` restores into a **scratch**
   database for safe, non-production drills
4. after restoring, re-counts tables and fails if nothing landed

Post-restore verification (operator checklist): confirm the Alembic revision
(`alembic current`), spot-check tenant rows, billing/subscription rows, call
records and audit rows, then run `/health/ready` and `scripts/smoke_test.py`.
A restore that has not actually been exercised is not trusted.

## 7. Redis recovery classification

Redis is **ephemeral / cache-only** in this architecture: it holds the
per-IP rate-limit counters and the cache. The compose stack gives it an AOF
volume (`redisdata`) for convenient restart, but no correctness-critical state
lives in Redis — call state, tenant data, billing and audit all live in
PostgreSQL. The application already treats Redis as best-effort: on any Redis
error, reads return cache-miss, writes are dropped, and `ping()` reports
unreachable (`app/core/cache.py`). **Recovery is therefore "wipe and restart"**
— a lost cache is repopulated on demand and rate-limit counters reset, which
is an acceptable, documented behavior (not a data-loss event). This is pinned
by `tests/test_deployment.py::test_redis_cache_degrades_gracefully_when_down`.

## 8. TLS / reverse proxy

Caddy is the public entry point (80/443; automatic Let's Encrypt when `DOMAIN`
is set). `Caddyfile`:

* `admin off` — the admin control plane is not exposed
* security headers at the proxy layer (HSTS, `X-Content-Type-Options`,
  `X-Frame-Options: DENY`, `Referrer-Policy`), mirrored by the app middleware
* `request_body max_size 25MB` — above the 20 MB knowledge-upload ceiling
* **WebSocket upgrade passes through unchanged** — the Twilio Media Streams
  handshake depends on it, and nothing in the file interferes
* Grafana is only exposed when `GRAFANA_DOMAIN` is explicitly set; otherwise
  it is reachable only inside the compose network

The API trusts `X-Forwarded-*` via `--proxy-headers` (entrypoint) so
`PUBLIC_BASE_URL`, `wss://` and Twilio signature validation work; tighten
`FORWARDED_ALLOW_IPS` from the default `*` in hardened setups.

## 9. Monitoring exposure

Prometheus and Grafana are **internal**. Prometheus has no host ports at all;
Grafana binds `127.0.0.1:3000` and is only proxied when `GRAFANA_DOMAIN` is
set. The API `/metrics` scrape endpoint is gated by `METRICS_TOKEN` when set
(`app/core/metrics.py::_scrape_authorized`). The Caddyfile contains no
`/metrics` route. `tests/test_deployment.py` pins all of these.

## 10. Secrets and supply chain

* No secrets in Git: `.gitignore` covers `.env`, `secrets/`, `backups/`,
  `*.dump`, `*.pem`, `*.key`, `.netrc`; gitleaks runs in CI; `.env.example`
  and `.env.staging.example` contain placeholders only (asserted by tests).
* No secrets in the image or build context: `.dockerignore` excludes the same
  set, and the Dockerfile never copies `.env`.
* No secrets in logs/errors: Sentry DSN is the only observability secret and
  is never logged or returned by the API (existing behavior, unchanged).
* **Dockerfile**: multi-stage, runs as non-root `appuser` (UID 10001),
  `HEALTHCHECK`, pinned base `python:3.12-slim-bookworm` (see the comment in
  the file — the pin is a deliberate change policy, not a blind upgrade), no
  build tooling in the runtime stage.
* **Dependency/image scanning**: CI runs `bandit` (SAST), `pip-audit`
  (Python dependency CVEs), `npm audit --omit=dev --audit-level=high`
  (shipped frontend dependencies) and gitleaks; compose images are pinned
  (`postgres:16-alpine`, `redis:7-alpine`, `prom/prometheus:v2.53.0`,
  `grafana/grafana:11.1.0`, `caddy:2.9-alpine`).

### Accepted risks (documented, not hidden)

* `npm audit` (full, including dev) reports a **moderate** advisory in the
  Vitest *test tooling* (`@vitest/mocker` redirect mock, fixed only by a
  breaking Vitest major). It does not affect shipped code: `npm audit
  --omit=dev` is clean. Mitigation: upgrade Vitest on its next major in a
  dedicated change; do not `--force` a breaking upgrade mid-step.
* `FORWARDED_ALLOW_IPS` defaults to `*` for single-proxy deployments; document
  and tighten when a second proxy is introduced.

## 11. CI/CD and deploy authorization

* Normal CI (every push/PR): backend lint + tests, Postgres migration round
  trip (`alembic upgrade head` + `downgrade base`), frontend tests + build +
  production audit, Docker image build.
* **Real-provider tests are separate and manual** (`real-integrations.yml`,
  `workflow_dispatch`, gated on `VOXDESK_REAL_INTEGRATION=1` and the
  `real_provider` marker). They are never in the default CI path.
* **Production deployment requires explicit authorization**: there is no
  automatic deploy-to-prod step. Deploys are operator-run via
  `scripts/deploy.sh`; the safe defaults (backup + verified migration +
  readiness wait) are the authorization gate.

## 12. Staging smoke test

`scripts/smoke_test.py` is the deterministic, read-only deployment smoke test:
it exercises `/health`, `/health/ready`, the dashboard shell, `/metrics`
(200/401/404 are all legitimate), `/auth/login` (a bogus login must 4xx,
never 500), `/api/tenants` (anonymous must be refused), the Twilio webhook
(unsigned POST must 403 — proving signature verification is fail-closed), the
provider-config presence and the E2E guard state. It performs **no** writes
and **no** external calls; non-zero exit gates the deploy.

```bash
SMOKE_BASE_URL=http://localhost:8001 python scripts/smoke_test.py   # staging
```
```
