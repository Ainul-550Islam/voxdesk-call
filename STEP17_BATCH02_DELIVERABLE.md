# Step 17 — Batch 02: the missing HTTP surface and the wiring Batch 01 deferred

Repo: `/home/user/voxdesk` (HEAD `ccba554`)
Generated: 2026-09-21T16:46:50Z

Batch 01 shipped three route modules whose registration it explicitly reported as an
integration dependency, and three services (automation, notification, inbox) with no HTTP
surface at all. This batch closes both: it adds the three missing route modules, registers
all six enterprise routers in `app/main.py`, fixes the one genuine bug the new tests found in
Batch 01 service code (`inbox_service._channel_of` made `web`/`crm` unreachable), and ships
the test file that proves the whole surface from the outside — including tenant isolation
with real tokens and real cross-tenant identifiers.

**Files in this batch: 6.** Every block below is the file's content byte-for-byte as it exists in the working tree. Each block header carries the line count and the SHA-256 of the whole file, so a reader can confirm the block is complete and unmodified — nothing is paraphrased, summarised, or replaced by a placeholder comment.

---


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
===== FILE: app/main.py (258 lines, sha256 915170c43fb866c42cbac2c0bb4ec22eec94848fd5da69a519d5c88ebe58d9fc, no trailing newline in the file) =====
==============================================================================
```python
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.agent_management_routes import router as agent_management_router
from app.api.auth_routes import router as auth_router
from app.api.appointment_routes import (
    calendar_router,
    router as appointment_router,
)
from app.api.analytics_routes import router as analytics_router
from app.api.automation_routes import router as automation_router
from app.api.campaign_routes import router as campaign_router
from app.api.inbox_routes import router as inbox_router
from app.api.notification_routes import router as notification_router
from app.api.workflow_routes import router as workflow_router
from app.api.billing_routes import router as billing_router
from app.api.calendar_webhook_routes import router as calendar_webhook_router
from app.api.crm_webhook_routes import router as crm_webhook_router
from app.api.integration_routes import router as integration_router
from app.api.knowledge_routes import router as knowledge_router
from app.api.routes import router as api_router
from app.api.team_routes import router as team_router
from app.api.gdpr_routes import router as gdpr_router
from app.api.license_routes import router as license_router
from app.core import health as health_check
from app.core.chaos import add_chaos_middleware
from app.core.config import settings
from app.core.errors import install_error_handling
from app.core.logging import log
from app.core.metrics import add_metrics_endpoint, add_metrics_middleware
from app.core.rate_limit import add_rate_limit_middleware
from app.core.security_headers import add_security_headers
from app.core.security_txt import add_security_txt
from app.db.models import Base
from app.db.session import get_engine
from app.channels.messaging import router as channels_router
from app.telephony.twilio_handler import router as telephony_router

# Observability: Sentry error reporting is optional and off unless a DSN is
# configured. Initialised at import time so it covers startup failures too.
if settings.sentry_dsn:
    import sentry_sdk

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.app_env,
        # Keep a small trace sample in production; none in dev/test.
        traces_sample_rate=0.1 if settings.is_production else 0.0,
        send_default_pii=False,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Refuse to serve traffic with an insecure configuration. In development
    # the same problems are logged as warnings so the app stays runnable.
    problems = settings.validate_security()
    if problems:
        if settings.is_production:
            raise RuntimeError(
                "Insecure configuration, refusing to start: " + "; ".join(problems)
            )
        for problem in problems:
            log.warning("config.insecure", problem=problem)

    engine = get_engine()
    if settings.app_env.lower() in {"development", "test"}:
        # Development/test bootstrap: create any missing tables so the app is
        # usable without running migrations. Production AND staging never do
        # this -- Alembic is the sole schema owner there, and create_all
        # would build schema outside the migration history (and staging must
        # mirror production's schema exactly). See alembic/versions/.
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    # STEP 7: make sure the plan catalogue exists, and check that every active
    # priced plan has a provider price id.
    #
    # Seeding lives here rather than in a migration because prices are
    # business data that changes: repricing should be an operator action
    # against a running system, not a schema change. `sync_seed_plans` never
    # overwrites a price an operator has edited.
    #
    # The configuration check is requirement 6 -- a plan with no price id
    # fails at boot rather than at checkout, where the failure would be in
    # front of a customer holding a credit card.
    from app.billing.plans import configuration_problems, list_plans, sync_seed_plans
    from app.db.session import get_sessionmaker

    maker = get_sessionmaker()
    async with maker() as session:
        await sync_seed_plans(session)
        plan_problems = configuration_problems(
            await list_plans(session, active_only=True),
            is_production=settings.is_production,
        )
    if plan_problems:
        if settings.is_production and settings.billing_provider == "stripe":
            raise RuntimeError(
                "Billing is misconfigured, refusing to start: "
                + "; ".join(plan_problems)
            )
        for problem in plan_problems:
            log.warning("billing.plan_misconfigured", problem=problem)

    log.info("voxdesk.started")
    yield
    await engine.dispose()


def _api_docs_config() -> dict[str, str | None]:
    """Swagger / ReDoc / OpenAPI are developer surfaces.

    In production they expose the full API schema and an interactive
    "try it out" console, so they are disabled there. Development keeps the
    FastAPI defaults.
    """
    if settings.is_production:
        return {"docs_url": None, "redoc_url": None, "openapi_url": None}
    return {}


app = FastAPI(
    title="VoxDesk",
    version="0.4.0",
    lifespan=lifespan,
    **_api_docs_config(),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,   # never "*" once cookies are in play
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

# Host-header validation. Off unless TRUSTED_HOSTS is set, so the single-proxy
# topology (Caddy terminates TLS for exactly the configured domains and binds
# the API to loopback) is unchanged; when set, the app rejects a request whose
# Host header names any other host, closing host-poisoning SSRF and
# cache-poisoning at the application layer too.
if settings.trusted_host_list:
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=settings.trusted_host_list,
    )

app.include_router(telephony_router)
app.include_router(channels_router)
app.include_router(auth_router)
app.include_router(team_router)
app.include_router(knowledge_router)
app.include_router(integration_router)
app.include_router(crm_webhook_router)
app.include_router(appointment_router)
app.include_router(calendar_router)
app.include_router(calendar_webhook_router)
app.include_router(billing_router)
app.include_router(analytics_router)
app.include_router(api_router)
app.include_router(gdpr_router)
app.include_router(license_router)

# Enterprise expansion surface. Batch 01 shipped these route modules with
# registration deliberately out of its file set ("reported as an integration
# dependency"); Batch 02 closes that dependency, and adds the three route
# modules whose services had no HTTP surface at all (automation, notification,
# inbox). Registration order is irrelevant to routing — every path here is
# distinct — but it is kept stable so `app.routes` is diffable.
app.include_router(agent_management_router)
app.include_router(workflow_router)
app.include_router(campaign_router)
app.include_router(automation_router)
app.include_router(notification_router)
app.include_router(inbox_router)

# Cross-cutting middleware and handlers. Order is deliberate: exception
# handlers + request-id first, then security headers, then rate limiting, then
# (test-only) failure injection, then metrics — which observes whatever the
# inner stack produces, injected failures and latency included.
install_error_handling(app)
add_security_headers(app)
add_rate_limit_middleware(app)
add_chaos_middleware(app)
add_metrics_middleware(app)
add_metrics_endpoint(app)
add_security_txt(app)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/health/ready")
async def readiness():
    """Readiness probe: the process is up AND it can serve traffic.

    Distinct from /health (liveness): a load balancer routes traffic only to
    nodes whose /health/ready returns 200, so a node that lost its database —
    or its Redis, or (in production) its voice providers — stops receiving
    work instead of failing every request. The checks never call a provider:
    they verify configuration presence and dependency reachability only. See
    app/core/health.py for the semantics.
    """
    result = await health_check.readiness()
    status_code = 200 if result["ready"] else 503
    if not result["ready"]:
        log.error("readiness.unavailable", checks=result["body"]["checks"])
    return JSONResponse(status_code=status_code, content=result["body"])


def _mount_dashboard_if_built(app: FastAPI, dist_dir: str | None = None) -> None:
    """Serve the built dashboard when it is present in the image.

    The React build is a separate stage in the Dockerfile. When it exists
    (production image) its static assets are mounted and any non-API path falls
    back to index.html so client-side routes (e.g. /agent) survive a refresh.
    In dev/test the dist directory does not exist and the app stays API-only.
    """
    if dist_dir is None:
        dist_dir = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "dashboard", "dist")
        )
    index_file = os.path.join(dist_dir, "index.html")
    if not os.path.isfile(index_file):
        return

    assets_dir = os.path.join(dist_dir, "assets")
    if os.path.isdir(assets_dir):
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def _spa_fallback(full_path: str):
        # Never let the SPA shell swallow unknown API/telephony/auth paths -- a
        # typo'd client call must 404 (JSON), not receive an HTML 200.
        if full_path.startswith(("api/", "auth/", "telephony/", "channels/", "health")):
            return JSONResponse(status_code=404, content={"detail": "Not found"})
        # API/telephony/auth paths are handled by the routers above; anything
        # else maps to a real file when one exists, otherwise the SPA shell.
        candidate = os.path.normpath(os.path.join(dist_dir, full_path))
        if (
            full_path
            and os.path.isfile(candidate)
            and os.path.abspath(candidate).startswith(os.path.abspath(dist_dir))
        ):
            return FileResponse(candidate)
        return FileResponse(index_file)


_mount_dashboard_if_built(app)
```

==============================================================================
===== FILE: app/services/inbox_service.py (425 lines, sha256 1f560aadfc7a05d418cc09e75b0ff3e936f9e803d007aa760d839fcad18b139c) =====
==============================================================================
```python
"""Unified inbox service (Batch 01 enterprise expansion).

The inbox is a tenant-scoped view over the existing conversation storage —
voice calls and text threads remain ``Call`` rows with ``Turn`` children, and
the existing ``/channels/message`` webhook keeps writing them unchanged. This
service overlays the inbox-only state (priority, assignment, tags, internal
notes, read/unread, SLA) on top, and maps messages onto ``Turn`` rows so every
analytics and transcript view keeps working.

Persistence honesty: assignment, priority, tags, notes, read/unread and SLA
timers have no columns yet and live in a per-tenant overlay (schema gap,
reported at the end of the batch). Thread existence, messages, escalation flag
and status are persisted on the real ``Call``/``Turn`` rows.

Hard rules: no cross-tenant thread visibility (every query filters tenant_id
and every access proves ownership), and message ordering is monotonic.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.messaging import get_or_start_thread
from app.core.errors import BadRequestError, NotFoundError
from app.db.models import Call, CallDirection, CallStatus, Speaker, Turn
from app.domain.inbox_models import (
    InboxChannel,
    Message,
    MessageDirection,
    SlaTimer,
    Thread,
    ThreadPriority,
    ThreadStatus,
    can_transition,
    now_iso,
    reopen_allowed,
    thread_id,
)

#: tenant_id -> call_id -> inbox-only state
_OVERLAY: dict[str, dict[str, dict]] = {}

FIRST_RESPONSE_SLA_SECONDS = 300
RESOLUTION_SLA_HOURS = 24


def _slot(store: dict, *keys: str) -> dict:
    node: dict = store
    for key in keys:
        node = node.setdefault(key, {})
    return node


def _overlay(tenant_id, call_id) -> dict:
    return _slot(_OVERLAY, str(tenant_id), str(call_id))


def _ensure_owned(call: Call | None, tenant_id) -> Call:
    if call is None or str(call.tenant_id) != str(tenant_id):
        raise NotFoundError("thread not found")
    return call


def _channel_of(call: Call) -> InboxChannel:
    intent = call.intent or ""
    # Exact intents are matched FIRST. The generic ``chat:`` prefix below
    # otherwise swallows ``chat:web`` / ``chat:crm`` before their own branches
    # can run, making WEB and CRM unreachable (every web thread was reported as
    # SMS, and its derived thread id hashed the wrong channel). Found by
    # tests/test_enterprise_batch02.py::TestInboxApi; the ordering is the fix,
    # the semantics are unchanged.
    if intent == "chat:web":
        return InboxChannel.WEB
    if intent == "chat:crm":
        return InboxChannel.CRM
    if intent.startswith("chat:whatsapp"):
        return InboxChannel.WHATSAPP
    if intent.startswith("chat:"):
        return InboxChannel.SMS
    return InboxChannel.VOICE


def _speaker_for(direction: MessageDirection) -> Speaker:
    if direction is MessageDirection.INBOUND:
        return Speaker.USER
    return Speaker.ASSISTANT


def _thread_status(call: Call, overlay: dict) -> ThreadStatus:
    explicit = overlay.get("status")
    if explicit:
        return ThreadStatus(explicit)
    if call.status is CallStatus.COMPLETED:
        return ThreadStatus.CLOSED
    if call.escalated:
        return ThreadStatus.ESCALATED
    if overlay.get("assignee_id"):
        return ThreadStatus.ASSIGNED
    return ThreadStatus.OPEN


# --------------------------------------------------------------- threads ---

async def find_or_create_thread(
    session: AsyncSession,
    tenant,
    *,
    channel: InboxChannel,
    customer: str,
    initial_message: str = "",
) -> Call:
    """Create (or reuse, for text channels) a thread's backing call row."""
    if channel in (InboxChannel.SMS, InboxChannel.WHATSAPP):
        chat = "whatsapp" if channel is InboxChannel.WHATSAPP else "sms"
        thread = await get_or_start_thread(session, tenant, customer, chat)
        if initial_message:
            await append_message(session, tenant, thread, direction=MessageDirection.INBOUND,
                                 body=initial_message)
        return thread
    call = Call(
        tenant_id=tenant.id,
        call_sid=f"inbox-{int(datetime.utcnow().timestamp()*1000)}",
        from_number=customer,
        to_number=tenant.twilio_number,
        status=CallStatus.IN_PROGRESS,
        direction=CallDirection.INBOUND,
        intent=f"chat:{channel.value}",
    )
    session.add(call)
    await session.commit()
    await session.refresh(call)
    _overlay(tenant.id, call.id).update({
        "opened_at": now_iso(),
        "sla_deadline_at": (datetime.now(timezone.utc) + timedelta(seconds=FIRST_RESPONSE_SLA_SECONDS)).isoformat(),
    })
    if initial_message:
        await append_message(session, tenant, call, direction=MessageDirection.INBOUND,
                             body=initial_message)
    return call


async def append_message(
    session: AsyncSession,
    tenant,
    thread: Call,
    *,
    direction: MessageDirection,
    body: str,
    author_role: str = "customer",
) -> Message:
    """Append a message. Notes go to the overlay; messages go to real Turns."""
    _ensure_owned(thread, tenant.id)
    if not body or len(body) > 8_000:
        raise BadRequestError("message body must be 1–8000 characters")
    if direction is MessageDirection.INTERNAL_NOTE:
        notes = _overlay(tenant.id, thread.id).setdefault("notes", [])
        notes.append({"body": body, "at": now_iso(), "author": author_role})
        sequence = await _sequence_of(session, thread)
        return Message(
            id=str(uuid.uuid4()),
            tenant_id=str(tenant.id),
            thread_id=str(thread.id),
            direction=direction,
            channel=_channel_of(thread),
            author_role=author_role,
            body=body,
            sequence=sequence,
            sent_at=now_iso(),
        )
    turn = Turn(call_id=thread.id, speaker=_speaker_for(direction), text=body)
    session.add(turn)
    await session.commit()
    await session.refresh(turn)
    sequence = await _sequence_of(session, thread)
    return Message(
        id=str(turn.id),
        tenant_id=str(tenant.id),
        thread_id=str(thread.id),
        direction=direction,
        channel=_channel_of(thread),
        author_role=author_role,
        body=body,
        sequence=sequence,
        sent_at=turn.created_at.isoformat() if turn.created_at else now_iso(),
    )


async def _sequence_of(session: AsyncSession, thread: Call) -> int:
    """Monotonic message sequence: persisted turns + overlay notes."""
    turns = (await session.execute(
        select(func.count()).select_from(Turn).where(Turn.call_id == thread.id)
    )).scalar() or 0
    notes = len(_overlay(thread.tenant_id, thread.id).get("notes", []))
    return int(turns) + notes


# --------------------------------------------------------------- projection ---

async def project(session: AsyncSession, tenant, thread: Call) -> Thread:
    """Build the domain thread from the persisted call + overlay + turns."""
    _ensure_owned(thread, tenant.id)
    overlay = _overlay(tenant.id, thread.id)
    turns = (await session.execute(
        select(Turn).where(Turn.call_id == thread.id).order_by(Turn.created_at)
    )).scalars().all()
    messages: list[Message] = []
    for index, turn in enumerate(turns, start=1):
        direction = MessageDirection.INBOUND if turn.speaker is Speaker.USER else MessageDirection.OUTBOUND
        messages.append(Message(
            id=str(turn.id),
            tenant_id=str(tenant.id),
            thread_id=str(thread.id),
            direction=direction,
            channel=_channel_of(thread),
            author_role="customer" if direction is MessageDirection.INBOUND else "assistant",
            body=turn.text or "",
            sequence=index,
            sent_at=turn.created_at.isoformat() if turn.created_at else "",
        ))
    for note in overlay.get("notes", []):
        messages.append(Message(
            id=str(uuid.uuid4()),
            tenant_id=str(tenant.id),
            thread_id=str(thread.id),
            direction=MessageDirection.INTERNAL_NOTE,
            channel=_channel_of(thread),
            author_role=note.get("author", "agent"),
            body=note.get("body", ""),
            sequence=len(messages) + 1,
            sent_at=note.get("at", ""),
        ))
    opened_at = overlay.get("opened_at", thread.started_at.isoformat() if thread.started_at else "")
    deadline = overlay.get("sla_deadline_at", "")
    sla = SlaTimer(opened_at=opened_at, deadline_at=deadline,
                   breached=bool(deadline and datetime.fromisoformat(deadline) < datetime.now(timezone.utc)))
    return Thread(
        id=thread_id(str(tenant.id), _channel_of(thread), str(thread.id)),
        tenant_id=str(tenant.id),
        channel=_channel_of(thread),
        status=_thread_status(thread, overlay),
        priority=ThreadPriority(overlay.get("priority", "normal")),
        participants=(thread.from_number, thread.to_number),
        assignee_id=overlay.get("assignee_id", ""),
        tags=tuple(overlay.get("tags", ())),
        internal_notes=tuple(n.get("body", "") for n in overlay.get("notes", [])),
        unread_count=overlay.get("unread", 0),
        sla=sla,
        escalated=bool(thread.escalated),
        messages=tuple(messages),
        last_message_at=messages[-1].sent_at if messages else thread.started_at.isoformat() if thread.started_at else "",
        created_at=thread.started_at.isoformat() if thread.started_at else "",
    )


# ---------------------------------------------------------------- actions ---

def assign(tenant, thread: Call, assignee_id: str) -> Thread:
    _ensure_owned(thread, tenant.id)
    overlay = _overlay(tenant.id, thread.id)
    overlay["assignee_id"] = assignee_id
    if overlay.get("status") not in (None, "open", "assigned"):
        overlay["status"] = "assigned"
    return _project_sync(tenant, thread)


def set_priority(tenant, thread: Call, priority: ThreadPriority) -> Thread:
    _ensure_owned(thread, tenant.id)
    _overlay(tenant.id, thread.id)["priority"] = priority.value
    return _project_sync(tenant, thread)


def add_note(tenant, thread: Call, body: str) -> Thread:
    _ensure_owned(thread, tenant.id)
    if not body or len(body) > 8_000:
        raise BadRequestError("note must be 1–8000 characters")
    notes = _overlay(tenant.id, thread.id).setdefault("notes", [])
    notes.append({"body": body, "at": now_iso(), "author": "agent"})
    return _project_sync(tenant, thread)


def mark_read(tenant, thread: Call) -> Thread:
    _ensure_owned(thread, tenant.id)
    _overlay(tenant.id, thread.id)["unread"] = 0
    return _project_sync(tenant, thread)


def mark_unread(tenant, thread: Call) -> Thread:
    _ensure_owned(thread, tenant.id)
    overlay = _overlay(tenant.id, thread.id)
    overlay["unread"] = overlay.get("unread", 0) + 1
    return _project_sync(tenant, thread)


def tag(tenant, thread: Call, value: str) -> Thread:
    _ensure_owned(thread, tenant.id)
    if not value or len(value) > 64:
        raise BadRequestError("tag must be 1–64 characters")
    overlay = _overlay(tenant.id, thread.id)
    tags = list(overlay.get("tags", ()))
    if value not in tags:
        tags.append(value)
    overlay["tags"] = tags[-100:]
    return _project_sync(tenant, thread)


async def escalate(session: AsyncSession, tenant, thread: Call, *, reason: str) -> Thread:
    _ensure_owned(thread, tenant.id)
    if not reason or len(reason) > 400:
        raise BadRequestError("escalation reason must be 1–400 characters")
    thread.escalated = True
    thread.transfer_reason = reason
    overlay = _overlay(tenant.id, thread.id)
    overlay["status"] = ThreadStatus.ESCALATED.value
    session.add(thread)
    await session.commit()
    return await project(session, tenant, thread)


async def close(session: AsyncSession, tenant, thread: Call) -> Thread:
    _ensure_owned(thread, tenant.id)
    overlay = _overlay(tenant.id, thread.id)
    current = _thread_status(thread, overlay)
    if not can_transition(current, ThreadStatus.CLOSED):
        raise BadRequestError(f"cannot close from {current.value}")
    thread.status = CallStatus.COMPLETED
    overlay["status"] = ThreadStatus.CLOSED.value
    session.add(thread)
    await session.commit()
    return await project(session, tenant, thread)


async def reopen(session: AsyncSession, tenant, thread: Call) -> Thread:
    _ensure_owned(thread, tenant.id)
    current = await project(session, tenant, thread)
    if not reopen_allowed(current):
        raise BadRequestError("thread is outside the reopen window")
    thread.status = CallStatus.IN_PROGRESS
    _overlay(tenant.id, thread.id)["status"] = ThreadStatus.OPEN.value
    session.add(thread)
    await session.commit()
    return await project(session, tenant, thread)


def _project_sync(tenant, thread: Call) -> Thread:
    """Synchronous projection for the overlay-only actions."""
    _ensure_owned(thread, tenant.id)
    overlay = _overlay(tenant.id, thread.id)
    return Thread(
        id=thread_id(str(tenant.id), _channel_of(thread), str(thread.id)),
        tenant_id=str(tenant.id),
        channel=_channel_of(thread),
        status=_thread_status(thread, overlay),
        priority=ThreadPriority(overlay.get("priority", "normal")),
        participants=(thread.from_number, thread.to_number),
        assignee_id=overlay.get("assignee_id", ""),
        tags=tuple(overlay.get("tags", ())),
        internal_notes=tuple(n.get("body", "") for n in overlay.get("notes", [])),
        unread_count=overlay.get("unread", 0),
        escalated=bool(thread.escalated),
    )


# ------------------------------------------------------------------ search ---

async def search(
    session: AsyncSession,
    tenant,
    *,
    channel: InboxChannel | None = None,
    status: ThreadStatus | None = None,
    assignee_id: str | None = None,
    escalated: bool | None = None,
    limit: int = 100,
) -> list[Thread]:
    """Tenant-scoped inbox search/filter."""
    limit = min(max(limit, 1), 500)
    stmt = select(Call).where(Call.tenant_id == tenant.id).order_by(Call.started_at.desc()).limit(limit)
    rows = (await session.execute(stmt)).scalars().all()
    results = []
    for row in rows:
        thread = await project(session, tenant, row)
        if channel is not None and thread.channel is not channel:
            continue
        if status is not None and thread.status is not status:
            continue
        if assignee_id is not None and thread.assignee_id != assignee_id:
            continue
        if escalated is not None and thread.escalated != escalated:
            continue
        results.append(thread)
    return results


async def unread_counts(session: AsyncSession, tenant) -> dict[str, int]:
    """Per-thread unread counts for the badge view."""
    stmt = select(Call.id).where(Call.tenant_id == tenant.id)
    ids = (await session.execute(stmt)).scalars().all()
    counts = {}
    for call_id in ids:
        overlay = _overlay(tenant.id, call_id)
        if overlay.get("unread"):
            counts[str(call_id)] = overlay["unread"]
    return counts


async def thread_counts(session: AsyncSession, tenant) -> dict[str, int]:
    """Open/assigned/escalated/closed counts, aggregate-safe."""
    stmt = select(Call.status).where(Call.tenant_id == tenant.id)
    statuses = (await session.execute(stmt)).scalars().all()
    counts = {"open": 0, "closed": 0, "escalated": 0, "assigned": 0}
    for status in statuses:
        if status is CallStatus.COMPLETED:
            counts["closed"] += 1
        else:
            counts["open"] += 1
    for call_id, overlay in _OVERLAY.get(str(tenant.id), {}).items():
        if overlay.get("status") == "escalated":
            counts["escalated"] += 1
        if overlay.get("assignee_id"):
            counts["assigned"] += 1
    return counts
```

==============================================================================
===== FILE: tests/test_enterprise_batch02.py (675 lines, sha256 6ea94d97d5b23cd9f5047bdd73c88f2b3c73f0ba510327d7e93b16945b47d3a1) =====
==============================================================================
```python
"""Batch 02 — the enterprise surface completed and wired.

Batch 01 shipped eight domain models, eight services and three route modules,
with two honest gaps registered in its own docstrings:

1. **Route registration** was "outside the allowed file set for this batch and
   is reported as an integration dependency" — so ``/api/agents``,
   ``/api/workflows`` and ``/api/campaigns`` existed in code but were not
   reachable from the app.
2. Three services (``automation``, ``notification``, ``inbox``) had **no HTTP
   surface at all**.

This file proves both are closed, and that the new surface inherits the Batch 01
security posture rather than weakening it: tenant isolation is tested from the
outside (a real token for tenant B, a genuinely correct id from tenant A),
secrets/contacts are asserted absent from responses, and the "no arbitrary
code / no dialing" rules are asserted against the request surface.
"""
from __future__ import annotations

import pytest
import pytest_asyncio

from app.domain.automation_models import TriggerEvent
from app.services import automation_service, inbox_service, notification_service
from tests.conftest import auth_headers


@pytest_asyncio.fixture
async def app_module_routes(app):
    """The real app's route table, for the registration assertions below."""
    return app.routes


@pytest.fixture(autouse=True)
def _reset_enterprise_state():
    """Clear the in-process registries the Batch 01 services own.

    Those services are honest about not being durable (a migration is a
    registered gap), so their state lives in module globals. Tests must not
    inherit each other's tenants, automations or notifications.
    """
    automation_service._REGISTRY.clear()
    automation_service._RUNS.clear()
    automation_service._LAST_RUN_AT.clear()
    notification_service._TEMPLATES.clear()
    notification_service._NOTIFICATIONS.clear()
    notification_service._DEDUPE.clear()
    notification_service._READ.clear()
    inbox_service._OVERLAY.clear()
    yield
    automation_service._REGISTRY.clear()
    automation_service._RUNS.clear()
    automation_service._LAST_RUN_AT.clear()
    notification_service._TEMPLATES.clear()
    notification_service._NOTIFICATIONS.clear()
    notification_service._DEDUPE.clear()
    notification_service._READ.clear()
    inbox_service._OVERLAY.clear()


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


def _template_body(name: str = "Booking confirmed", channel: str = "in_app") -> dict:
    return {
        "name": name,
        "channel": channel,
        # Placeholder syntax is the model's single-brace substitution (see
        # NotificationTemplate.render); a doubled brace survives literally.
        "body": "Hi {customer}, your appointment {when} is confirmed.",
        "variables": ["customer", "when"],
    }


# ============================================================ router wiring ===

class TestRouterWiring:
    """The integration dependency Batch 01 reported, now closed."""

    EXPECTED = [
        "/api/agents",
        "/api/workflows",
        "/api/campaigns",
        "/api/automations",
        "/api/notifications",
        "/api/inbox/threads",
    ]

    async def test_every_enterprise_router_is_registered(self, app_module_routes):
        paths = {route.path for route in app_module_routes}
        for expected in self.EXPECTED:
            assert expected in paths, f"{expected} is not registered in app/main.py"

    async def test_new_surfaces_are_tenant_scoped_not_admin_namespaced(self, app_module_routes):
        # Every enterprise path is tenant-scoped by construction: the tenant
        # comes from the token, never from the path.
        for route in app_module_routes:
            path = getattr(route, "path", "")
            for prefix in ("/api/agents", "/api/workflows", "/api/campaigns",
                           "/api/automations", "/api/notifications", "/api/inbox"):
                if path.startswith(prefix):
                    assert "{tenant_id}" not in path, path


# ================================================================= AUTOMATION ===

class TestAutomationApi:
    async def test_create_then_read_back(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        created = await client.post("/api/automations", json=_automation_body(), headers=headers)
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["status"] == "disabled"          # created disabled on purpose
        assert body["event"] == "call_completed"
        assert body["actions"][0]["name"] == "record_escalation_intent"

        fetched = await client.get(f"/api/automations/{body['id']}", headers=headers)
        assert fetched.status_code == 200
        assert fetched.json() == body

        listed = await client.get("/api/automations", headers=headers)
        assert [a["id"] for a in listed.json()] == [body["id"]]

    async def test_create_is_idempotent_on_name(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        first = await client.post("/api/automations", json=_automation_body(), headers=headers)
        second = await client.post("/api/automations", json=_automation_body(), headers=headers)
        assert first.status_code == second.status_code == 201
        assert first.json()["id"] == second.json()["id"]

        listed = await client.get("/api/automations", headers=headers)
        assert len(listed.json()) == 1

    async def test_unknown_event_is_refused(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        body = {**_automation_body(), "event": "teleport_completed"}
        response = await client.post("/api/automations", json=body, headers=headers)
        assert response.status_code == 422

    async def test_unknown_action_is_refused(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        body = {**_automation_body(), "actions": [{"name": "exec_shell", "params": {}}]}
        response = await client.post("/api/automations", json=body, headers=headers)
        assert response.status_code == 422
        assert "controlled action" in response.json()["detail"]

    async def test_mass_assignment_is_refused(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        body = {**_automation_body(), "status": "enabled"}   # not a writable field
        response = await client.post("/api/automations", json=body, headers=headers)
        assert response.status_code == 422

    async def test_oversized_payload_is_refused(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        created = await client.post("/api/automations", json=_automation_body(), headers=headers)
        automation_id = created.json()["id"]
        run = await client.post(
            f"/api/automations/{automation_id}/run",
            json={
                "business_event_id": "call-1",
                "payload": {f"k{i}": "x" * 64 for i in range(2000)},
            },
            headers=headers,
        )
        assert run.status_code == 422

    async def test_enable_disable_then_evaluate(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        created = await client.post("/api/automations", json=_automation_body(), headers=headers)
        automation_id = created.json()["id"]

        # A disabled automation never evaluates, however well the payload matches.
        before = await client.post(
            "/api/automations/evaluate",
            json={"event": "call_completed", "payload": {"sentiment": "negative"}},
            headers=headers,
        )
        assert before.json() == []

        enabled = await client.post(f"/api/automations/{automation_id}/enable", headers=headers)
        assert enabled.json()["status"] == "enabled"

        after = await client.post(
            "/api/automations/evaluate",
            json={"event": "call_completed", "payload": {"sentiment": "negative"}},
            headers=headers,
        )
        assert [m["automation_id"] for m in after.json()] == [automation_id]

        # The filter is respected: a positive sentiment does not match.
        missed = await client.post(
            "/api/automations/evaluate",
            json={"event": "call_completed", "payload": {"sentiment": "positive"}},
            headers=headers,
        )
        assert missed.json() == []

        disabled = await client.post(f"/api/automations/{automation_id}/disable", headers=headers)
        assert disabled.json()["status"] == "disabled"

    async def test_run_is_deduplicated_by_business_event_id(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        created = await client.post("/api/automations", json=_automation_body(), headers=headers)
        automation_id = created.json()["id"]
        await client.post(f"/api/automations/{automation_id}/enable", headers=headers)

        payload = {"business_event_id": "call-123", "payload": {"sentiment": "negative"}}
        first = await client.post(f"/api/automations/{automation_id}/run", json=payload, headers=headers)
        assert first.status_code == 200, first.text
        assert first.json()["status"] == "completed"

        second = await client.post(f"/api/automations/{automation_id}/run", json=payload, headers=headers)
        assert second.json()["status"] == "cancelled"
        assert second.json()["last_error"] == "max_per_event budget exhausted"

    async def test_run_history_and_stats(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        created = await client.post("/api/automations", json=_automation_body(), headers=headers)
        automation_id = created.json()["id"]
        await client.post(f"/api/automations/{automation_id}/enable", headers=headers)
        await client.post(
            f"/api/automations/{automation_id}/run",
            json={"business_event_id": "call-9", "payload": {"sentiment": "negative"}},
            headers=headers,
        )

        runs = await client.get(f"/api/automations/{automation_id}/runs", headers=headers)
        assert len(runs.json()) == 1
        assert runs.json()[0]["business_event_id"] == "call-9"

        stats = await client.get(f"/api/automations/{automation_id}/stats", headers=headers)
        assert stats.json() == {"failed": 0, "suppressed": 0, "completed": 1}

    async def test_dedupe_key_is_deterministic(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        created = await client.post("/api/automations", json=_automation_body(), headers=headers)
        automation_id = created.json()["id"]
        body = {"business_event_id": "call-77", "payload": {}}
        one = await client.post(f"/api/automations/{automation_id}/dedupe-key", json=body, headers=headers)
        two = await client.post(f"/api/automations/{automation_id}/dedupe-key", json=body, headers=headers)
        assert one.json()["dedupe_key"] == two.json()["dedupe_key"]

    async def test_event_and_action_vocabularies_are_closed(self, client, viewer_a):
        headers = await auth_headers(client, viewer_a)
        events = await client.get("/api/automations/events", headers=headers)
        assert events.json() == sorted(e.value for e in TriggerEvent)
        actions = await client.get("/api/automations/actions", headers=headers)
        for forbidden in ("exec", "eval", "import", "os.system"):
            assert forbidden not in actions.json()

    async def test_viewer_reads_but_cannot_write(self, client, viewer_a):
        headers = await auth_headers(client, viewer_a)
        assert (await client.get("/api/automations", headers=headers)).status_code == 200
        assert (await client.post("/api/automations", json=_automation_body(), headers=headers)).status_code == 403

    async def test_agent_cannot_run_automations(self, client, agent_a, manager_a):
        manager_headers = await auth_headers(client, manager_a)
        created = await client.post("/api/automations", json=_automation_body(), headers=manager_headers)
        automation_id = created.json()["id"]

        agent_headers = await auth_headers(client, agent_a)
        response = await client.post(
            f"/api/automations/{automation_id}/run",
            json={"business_event_id": "call-1", "payload": {}},
            headers=agent_headers,
        )
        assert response.status_code == 403

    async def test_cross_tenant_automation_is_not_found(self, client, manager_a, owner_b):
        manager_headers = await auth_headers(client, manager_a)
        created = await client.post("/api/automations", json=_automation_body(), headers=manager_headers)
        automation_id = created.json()["id"]

        tenant_b_headers = await auth_headers(client, owner_b)
        assert (await client.get(f"/api/automations/{automation_id}", headers=tenant_b_headers)).status_code == 404
        assert (await client.get("/api/automations", headers=tenant_b_headers)).json() == []
        run = await client.post(
            f"/api/automations/{automation_id}/run",
            json={"business_event_id": "call-1", "payload": {}},
            headers=tenant_b_headers,
        )
        assert run.status_code == 404


# =============================================================== NOTIFICATION ===

class TestNotificationApi:
    async def test_template_create_and_render(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        created = await client.post("/api/notifications/templates", json=_template_body(), headers=headers)
        assert created.status_code == 201, created.text
        template_id = created.json()["id"]

        rendered = await client.post(
            f"/api/notifications/templates/{template_id}/render",
            json={"variables": {"customer": "Ada", "when": "Tuesday"}},
            headers=headers,
        )
        assert rendered.status_code == 200
        assert rendered.json()["body"] == "Hi Ada, your appointment Tuesday is confirmed."

    async def test_unknown_channel_is_refused(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        response = await client.post(
            "/api/notifications/templates",
            json={**_template_body(), "channel": "carrier_pigeon"},
            headers=headers,
        )
        assert response.status_code == 422

    async def test_preference_check_reports_quiet_hours(self, client, viewer_a):
        headers = await auth_headers(client, viewer_a)
        open_hours = await client.post(
            "/api/notifications/preferences/check",
            json={"channel": "in_app", "enabled": True},
            headers=headers,
        )
        assert open_hours.json() == {"allowed": True, "reason": ""}

        quiet = await client.post(
            "/api/notifications/preferences/check",
            json={"channel": "in_app", "enabled": True,
                  "quiet_start": "22:00", "quiet_end": "08:00"},
            headers=headers,
        )
        # Overnight window: whichever side of midnight "now" falls on, the
        # reason must name quiet hours (or the call must be inside the window).
        if not quiet.json()["allowed"]:
            assert quiet.json()["reason"] == "recipient is in quiet hours"

        malformed = await client.post(
            "/api/notifications/preferences/check",
            json={"channel": "in_app", "quiet_start": "25:99"},
            headers=headers,
        )
        assert malformed.status_code == 422

    async def test_create_is_deduplicated_and_masks_the_recipient(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        template = await client.post("/api/notifications/templates", json=_template_body(), headers=headers)
        template_id = template.json()["id"]

        body = {
            "template_id": template_id,
            "recipient": {"kind": "phone", "target": "+15551234567"},
            "event_source": "appointment_reminder",
            "business_key": "appt-42",
            "variables": {"customer": "Ada", "when": "Tuesday"},
        }
        first = await client.post("/api/notifications", json=body, headers=headers)
        assert first.status_code == 201, first.text
        second = await client.post("/api/notifications", json=body, headers=headers)
        assert first.json()["id"] == second.json()["id"]        # one notification, not two

        recipient = first.json()["recipient"]
        assert recipient["target_masked"] == "***4567"
        assert "+15551234567" not in first.text                 # raw contact never returned

        listed = await client.get("/api/notifications", headers=headers)
        assert len(listed.json()) == 1

    async def test_read_state_round_trip(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        template = await client.post("/api/notifications/templates", json=_template_body(), headers=headers)
        created = await client.post(
            "/api/notifications",
            json={
                "template_id": template.json()["id"],
                "recipient": {"kind": "user", "user_id": "u-1"},
                "event_source": "system",
                "business_key": "read-1",
                "variables": {"customer": "Ada", "when": "Tuesday"},
            },
            headers=headers,
        )
        notification_id = created.json()["id"]
        assert created.json()["read"] is False

        assert (await client.post(f"/api/notifications/{notification_id}/read", headers=headers)).json()["read"] is True
        unread_only = await client.get("/api/notifications", params={"unread_only": True}, headers=headers)
        assert unread_only.json() == []
        assert (await client.post(f"/api/notifications/{notification_id}/unread", headers=headers)).json()["read"] is False

    async def test_delivery_honours_the_channels_reality(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        in_app = await client.post(
            "/api/notifications/templates",
            json=_template_body(name="In app", channel="in_app"), headers=headers)
        email = await client.post(
            "/api/notifications/templates",
            json=_template_body(name="Email", channel="email"), headers=headers)

        in_app_notification = await client.post(
            "/api/notifications",
            json={"template_id": in_app.json()["id"], "recipient": {"kind": "user", "user_id": "u-1"},
                  "event_source": "system", "business_key": "deliver-1",
                  "variables": {"customer": "Ada", "when": "Tuesday"}},
            headers=headers,
        )
        delivered = await client.post(
            f"/api/notifications/{in_app_notification.json()['id']}/deliver", headers=headers)
        assert delivered.json()["delivery_state"] == "delivered"

        email_notification = await client.post(
            "/api/notifications",
            json={"template_id": email.json()["id"], "recipient": {"kind": "user", "user_id": "u-1"},
                  "event_source": "system", "business_key": "deliver-2",
                  "variables": {"customer": "Ada", "when": "Tuesday"}},
            headers=headers,
        )
        suppressed = await client.post(
            f"/api/notifications/{email_notification.json()['id']}/deliver", headers=headers)
        # Batch 01 declared EMAIL as "no provider in this batch" — the API
        # reports that honestly instead of claiming a send.
        assert suppressed.json()["delivery_state"] == "suppressed"

    async def test_retry_after_failure_and_refusal_after_delivery(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        template = await client.post("/api/notifications/templates", json=_template_body(), headers=headers)
        notification = await client.post(
            "/api/notifications",
            json={"template_id": template.json()["id"], "recipient": {"kind": "user", "user_id": "u-1"},
                  "event_source": "system", "business_key": "retry-1",
                  "variables": {"customer": "Ada", "when": "Tuesday"}},
            headers=headers,
        )
        notification_id = notification.json()["id"]

        retried = await client.post(f"/api/notifications/{notification_id}/retry", headers=headers)
        assert retried.json()["attempts"] == 1
        assert retried.json()["next_attempt_at"] != ""

        await client.post(f"/api/notifications/{notification_id}/deliver", headers=headers)
        refused = await client.post(f"/api/notifications/{notification_id}/retry", headers=headers)
        assert refused.status_code == 422

    async def test_delivery_summary_counts_states(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        template = await client.post("/api/notifications/templates", json=_template_body(), headers=headers)
        created = await client.post(
            "/api/notifications",
            json={"template_id": template.json()["id"], "recipient": {"kind": "user", "user_id": "u-1"},
                  "event_source": "system", "business_key": "summary-1",
                  "variables": {"customer": "Ada", "when": "Tuesday"}},
            headers=headers,
        )
        await client.post(f"/api/notifications/{created.json()['id']}/deliver", headers=headers)
        summary = await client.get("/api/notifications/states/summary", headers=headers)
        assert summary.json()["delivered"] == 1

    async def test_agent_cannot_create_or_deliver(self, client, agent_a, manager_a):
        manager_headers = await auth_headers(client, manager_a)
        template = await client.post("/api/notifications/templates", json=_template_body(), headers=manager_headers)
        notification = await client.post(
            "/api/notifications",
            json={"template_id": template.json()["id"], "recipient": {"kind": "user", "user_id": "u-1"},
                  "event_source": "system", "business_key": "rbac-1",
                  "variables": {"customer": "Ada", "when": "Tuesday"}},
            headers=manager_headers,
        )

        agent_headers = await auth_headers(client, agent_a)
        assert (await client.get("/api/notifications", headers=agent_headers)).status_code == 200
        assert (await client.post("/api/notifications", json={
            "template_id": template.json()["id"], "recipient": {"kind": "user"},
            "event_source": "system"}, headers=agent_headers)).status_code == 403
        delivered = await client.post(
            f"/api/notifications/{notification.json()['id']}/deliver", headers=agent_headers)
        assert delivered.status_code == 403

    async def test_cross_tenant_notifications_are_invisible(self, client, manager_a, owner_b):
        manager_headers = await auth_headers(client, manager_a)
        template = await client.post("/api/notifications/templates", json=_template_body(), headers=manager_headers)
        created = await client.post(
            "/api/notifications",
            json={"template_id": template.json()["id"], "recipient": {"kind": "user", "user_id": "u-1"},
                  "event_source": "system", "business_key": "iso-1",
                  "variables": {"customer": "Ada", "when": "Tuesday"}},
            headers=manager_headers,
        )

        tenant_b_headers = await auth_headers(client, owner_b)
        assert (await client.get("/api/notifications", headers=tenant_b_headers)).json() == []
        assert (await client.get(
            f"/api/notifications/{created.json()['id']}", headers=tenant_b_headers)).status_code == 404
        # Tenant B cannot even reference tenant A's template id.
        assert (await client.get(
            f"/api/notifications/templates/{template.json()['id']}", headers=tenant_b_headers)).status_code == 404


# ====================================================================== INBOX ===

class TestInboxApi:
    async def _open_thread(self, client, headers, *, channel: str = "web",
                           customer: str = "+15550001111") -> dict:
        response = await client.post(
            "/api/inbox/threads",
            json={"channel": channel, "customer": customer, "initial_message": "hello"},
            headers=headers,
        )
        assert response.status_code == 201, response.text
        return response.json()

    async def test_open_thread_masks_participants(self, client, manager_a, tenant_a):
        headers = await auth_headers(client, manager_a)
        thread = await self._open_thread(client, headers)
        assert thread["channel"] == "web"
        assert thread["status"] == "open"
        assert thread["participants_masked"] == ["***1111", f"***{tenant_a.twilio_number[-4:]}"]
        assert "+15550001111" not in str(thread)

    async def test_list_and_fetch_thread(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        thread = await self._open_thread(client, headers)

        listed = await client.get("/api/inbox/threads", headers=headers)
        assert [t["id"] for t in listed.json()] == [thread["id"]]

        fetched = await client.get(f"/api/inbox/threads/{thread['id']}", headers=headers)
        assert fetched.status_code == 200
        assert fetched.json()["id"] == thread["id"]
        assert fetched.json()["message_count"] == 1

    async def test_messages_are_ordered_and_notes_stay_internal(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        thread = await self._open_thread(client, headers)
        thread_id = thread["id"]

        inbound = await client.post(
            f"/api/inbox/threads/{thread_id}/messages",
            json={"direction": "inbound", "body": "I need help"}, headers=headers)
        assert inbound.status_code == 201
        outbound = await client.post(
            f"/api/inbox/threads/{thread_id}/messages",
            json={"direction": "outbound", "body": "On it", "author_role": "agent"},
            headers=headers)
        assert outbound.json()["sequence"] > inbound.json()["sequence"]

        note = await client.post(
            f"/api/inbox/threads/{thread_id}/notes", json={"body": "internal hint"}, headers=headers)
        assert note.json()["internal_notes"] == ["internal hint"]

        messages = await client.get(f"/api/inbox/threads/{thread_id}/messages", headers=headers)
        bodies = [m["body"] for m in messages.json()]
        assert "hello" in bodies and "I need help" in bodies and "On it" in bodies
        # The note is reachable, but ONLY labelled as an internal note — never
        # as something the customer said or was told. That separation is the
        # whole point of the overlay.
        note_directions = [m["direction"] for m in messages.json() if m["body"] == "internal hint"]
        assert note_directions == ["internal_note"]

    async def test_queue_actions_round_trip(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        thread_id = (await self._open_thread(client, headers))["id"]

        assigned = await client.post(f"/api/inbox/threads/{thread_id}/assign",
                                     json={"assignee_id": "agent-7"}, headers=headers)
        assert assigned.json()["assignee_id"] == "agent-7"

        priority = await client.post(f"/api/inbox/threads/{thread_id}/priority",
                                     json={"priority": "urgent"}, headers=headers)
        assert priority.json()["priority"] == "urgent"

        tagged = await client.post(f"/api/inbox/threads/{thread_id}/tags",
                                   json={"tag": "vip"}, headers=headers)
        assert tagged.json()["tags"] == ["vip"]

        unread = await client.post(f"/api/inbox/threads/{thread_id}/unread", headers=headers)
        assert unread.json()["unread_count"] == 1
        read = await client.post(f"/api/inbox/threads/{thread_id}/read", headers=headers)
        assert read.json()["unread_count"] == 0

    async def test_escalate_close_reopen(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        thread_id = (await self._open_thread(client, headers))["id"]

        escalated = await client.post(f"/api/inbox/threads/{thread_id}/escalate",
                                      json={"reason": "needs a human"}, headers=headers)
        assert escalated.json()["status"] == "escalated"
        assert escalated.json()["escalated"] is True

        closed = await client.post(f"/api/inbox/threads/{thread_id}/close", headers=headers)
        assert closed.json()["status"] == "closed"

        reopened = await client.post(f"/api/inbox/threads/{thread_id}/reopen", headers=headers)
        assert reopened.json()["status"] == "open"

    async def test_bad_direction_and_empty_body_are_refused(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        thread_id = (await self._open_thread(client, headers))["id"]
        bad_direction = await client.post(
            f"/api/inbox/threads/{thread_id}/messages",
            json={"direction": "sideways", "body": "hi"}, headers=headers)
        assert bad_direction.status_code == 422
        empty = await client.post(
            f"/api/inbox/threads/{thread_id}/messages",
            json={"direction": "inbound", "body": ""}, headers=headers)
        assert empty.status_code == 422

    async def test_counts_and_unread_badges(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        thread_id = (await self._open_thread(client, headers))["id"]
        await client.post(f"/api/inbox/threads/{thread_id}/unread", headers=headers)

        counts = await client.get("/api/inbox/counts", headers=headers)
        assert counts.json()["open"] == 1 and counts.json()["closed"] == 0

        unread = await client.get("/api/inbox/unread-counts", headers=headers)
        assert list(unread.json().values()) == [1]

    async def test_identity_endpoint_explains_the_opaque_id(self, client, manager_a):
        headers = await auth_headers(client, manager_a)
        thread_id = (await self._open_thread(client, headers))["id"]
        identity = await client.get(f"/api/inbox/threads/{thread_id}/identity", headers=headers)
        assert identity.json()["thread_id"] == thread_id
        assert identity.json()["expected_id"] == thread_id
        assert identity.json()["derived_from"]["channel"] == "web"

    async def test_agent_reads_but_cannot_mutate_the_queue(self, client, agent_a, manager_a):
        manager_headers = await auth_headers(client, manager_a)
        thread_id = (await self._open_thread(client, manager_headers))["id"]

        agent_headers = await auth_headers(client, agent_a)
        assert (await client.get("/api/inbox/threads", headers=agent_headers)).status_code == 200
        # Registered limitation: agents have no CALL_READ_ALL, and the inbox
        # service has no per-assignee scoping, so mutations stay at manager+.
        assert (await client.post(f"/api/inbox/threads/{thread_id}/close", headers=agent_headers)).status_code == 403
        assert (await client.post(f"/api/inbox/threads/{thread_id}/assign",
                                  json={"assignee_id": "agent-9"}, headers=agent_headers)).status_code == 403

    async def test_cross_tenant_thread_is_not_found(self, client, manager_a, owner_b):
        manager_headers = await auth_headers(client, manager_a)
        thread_id = (await self._open_thread(client, manager_headers))["id"]

        tenant_b_headers = await auth_headers(client, owner_b)
        assert (await client.get(f"/api/inbox/threads/{thread_id}", headers=tenant_b_headers)).status_code == 404
        assert (await client.get("/api/inbox/threads", headers=tenant_b_headers)).json() == []
        # Tenant B's own queue is unaffected by tenant A's activity.
        tenant_b_counts = await client.get("/api/inbox/counts", headers=tenant_b_headers)
        assert tenant_b_counts.json()["open"] == 0


# ============================================================ auth on the edge ===

class TestUnauthenticatedAccess:
    @pytest.mark.parametrize("path", [
        "/api/agents",
        "/api/workflows",
        "/api/campaigns",
        "/api/automations",
        "/api/notifications",
        "/api/inbox/threads",
    ])
    async def test_anonymous_requests_are_rejected(self, client, path):
        response = await client.get(path)
        assert response.status_code in (401, 403)

    async def test_a_forged_token_is_rejected(self, client):
        response = await client.get(
            "/api/automations",
            headers={"Authorization": "Bearer not-a-real-token"},
        )
        assert response.status_code == 401
```
