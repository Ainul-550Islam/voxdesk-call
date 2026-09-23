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
