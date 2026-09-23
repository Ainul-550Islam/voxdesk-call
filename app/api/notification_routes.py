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
