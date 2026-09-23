"""Session and device management.

The user-facing answer to "where am I signed in, and how do I get the other ones
out?". Every route here is scoped to the caller's *own* sessions: there is no
administrative variant, because signing somebody else out of their browser is
what deactivating their account is for, and a second path to it would only be a
second thing to get wrong.

Route table::

    GET    /api/sessions                  own sessions
    DELETE /api/sessions/{session_id}     revoke one (own only)
    POST   /api/sessions/revoke-others    revoke every other (own only)
    DELETE /api/sessions                  revoke all, including this one
    PATCH  /api/sessions/{session_id}     rename a device (own only)

Revocation is immediate for both layers. The session row is marked revoked, the
refresh tokens that belong to it are revoked in the same transaction, and the
access token carries a ``sid`` claim that every request re-checks -- so "sign out
this device" does not leave a working access token behind for the rest of its
lifetime.
"""
from __future__ import annotations

import uuid
from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import (
    TenantContext,
    _client_ip,
    get_identity_context,
    require_human_session,
)
from app.auth.identity import events as identity_events
from app.auth.identity import sessions as identity_sessions
from app.auth.identity.service import IdentityContext
from app.db.models import AuditAction
from app.db.session import get_session

log = structlog.get_logger()
router = APIRouter(prefix="/api/sessions", tags=["identity"])


class SessionOut(BaseModel):
    id: str
    device: str
    user_agent: str = ""
    ip_address: str = ""
    auth_method: str = "password"
    mfa_verified: bool = False
    current: bool = False
    created_at: datetime
    last_seen_at: datetime
    idle_expires_at: datetime
    expires_at: datetime


class SessionListOut(BaseModel):
    sessions: list[SessionOut]
    current_session_id: str | None = None
    limits: dict


def _out(row, *, current_id: uuid.UUID | None) -> SessionOut:
    return SessionOut(
        id=str(row.id),
        device=row.device_label or identity_sessions.describe_device(row.user_agent),
        user_agent=row.user_agent,
        ip_address=row.ip_address,
        auth_method=row.auth_method,
        mfa_verified=bool(row.mfa_verified),
        current=row.id == current_id,
        created_at=row.created_at,
        last_seen_at=row.last_seen_at,
        idle_expires_at=row.idle_expires_at,
        expires_at=row.expires_at,
    )


@router.get("", response_model=SessionListOut)
async def list_sessions(
    ctx: TenantContext = Depends(require_human_session),
    ictx: IdentityContext = Depends(get_identity_context),
    session: AsyncSession = Depends(get_session),
):
    """Every live session for the caller, newest first, with the current one marked."""
    await identity_sessions.sweep_expired(session, user_id=ctx.user_id)
    rows = await identity_sessions.live_sessions(session, user_id=ctx.user_id)
    return SessionListOut(
        sessions=[_out(row, current_id=ctx.session_id) for row in rows],
        current_session_id=str(ctx.session_id) if ctx.session_id else None,
        limits={
            "idle_minutes": ictx.policy.session_idle_minutes,
            "max_active": ictx.policy.session_max_active,
            "refresh_token_days": ictx.policy.refresh_token_days,
        },
    )


async def _own_session(session: AsyncSession, ctx: TenantContext, session_id: uuid.UUID):
    row = await identity_sessions.get_session(session, ctx.user_id, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Not found"})
    return row


@router.delete("/{session_id}", status_code=204)
async def revoke_session(
    session_id: uuid.UUID,
    request: Request,
    ctx: TenantContext = Depends(require_human_session),
    session: AsyncSession = Depends(get_session),
):
    """Sign out one device."""
    row = await _own_session(session, ctx, session_id)
    revoked_tokens = await identity_sessions.revoke_session(
        session, row, reason="user_revoked", commit=False
    )
    await identity_events.emit(
        session,
        AuditAction.SESSION_REVOKED,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        target_user_id=ctx.user_id,
        actor_email=ctx.user.email,
        ip_address=_client_ip(request),
        detail={
            "session_id": str(row.id),
            "reason": "user_revoked",
            "device": row.device_label,
            "tokens_revoked": revoked_tokens,
        },
        commit=False,
    )
    await session.commit()


class RevokeOthersOut(BaseModel):
    revoked: int


@router.post("/revoke-others", response_model=RevokeOthersOut)
async def revoke_other_sessions(
    request: Request,
    ctx: TenantContext = Depends(require_human_session),
    session: AsyncSession = Depends(get_session),
):
    """Sign out every other device, keeping this one."""
    revoked = await identity_sessions.revoke_all_for_user(
        session,
        user_id=ctx.user_id,
        reason="user_revoked_others",
        keep_session_id=ctx.session_id,
        commit=False,
    )
    await identity_events.emit(
        session,
        AuditAction.SESSION_REVOKED,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        target_user_id=ctx.user_id,
        actor_email=ctx.user.email,
        ip_address=_client_ip(request),
        detail={"reason": "user_revoked_others", "revoked": revoked},
        commit=False,
    )
    await session.commit()
    return RevokeOthersOut(revoked=revoked)


@router.delete("", status_code=204)
async def revoke_all_sessions(
    request: Request,
    ctx: TenantContext = Depends(require_human_session),
    session: AsyncSession = Depends(get_session),
):
    """Sign out everywhere, including here.

    ``token_version`` is bumped as well, so access tokens already sitting in a
    browser stop working at once rather than at their next expiry. This is the
    button someone presses when they think their laptop was stolen, and it has to
    mean *everything*, not "everything except the tab that clicked it".
    """
    from app.auth import service as auth_service

    await auth_service.revoke_all_for_user(session, ctx.user_id, bump_version=True)
    revoked = await identity_sessions.revoke_all_for_user(
        session, user_id=ctx.user_id, reason="user_revoked_all", commit=False
    )
    await identity_events.emit(
        session,
        AuditAction.SESSION_REVOKED,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        target_user_id=ctx.user_id,
        actor_email=ctx.user.email,
        ip_address=_client_ip(request),
        detail={"reason": "user_revoked_all", "revoked": revoked, "token_version_bumped": True},
        commit=False,
    )
    await session.commit()


class RenameIn(BaseModel):
    label: str = Field(min_length=1, max_length=120)


@router.patch("/{session_id}", response_model=SessionOut)
async def rename_session(
    session_id: uuid.UUID,
    payload: RenameIn,
    ctx: TenantContext = Depends(require_human_session),
    session: AsyncSession = Depends(get_session),
):
    """Give a device a name the user recognises.

    Purely cosmetic -- it is a label the user chooses for their own row -- which
    is why it is the one mutating route here that does not write an audit event.
    """
    row = await _own_session(session, ctx, session_id)
    row.device_label = payload.label.strip()[:120]
    await session.commit()
    return _out(row, current_id=ctx.session_id)
