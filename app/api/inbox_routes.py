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
