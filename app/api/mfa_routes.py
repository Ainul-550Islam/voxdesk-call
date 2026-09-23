"""Multi-factor authentication endpoints.

Everything here is about a *person's* second factor, so every route requires a
human session (``require_human_session``) unless it is an administrator acting
on somebody else's factor -- and that one demands ``identity:write``, a human
session, and a fresh proof of presence, because "reset this user's MFA" is one
step away from taking over their account.

Enrollment is a two-step handshake on purpose. ``POST /api/mfa/enroll`` creates a
*pending* factor and returns the seed once; ``POST /api/mfa/enroll/confirm``
activates it only after the caller proves they can produce a code from that
seed. A browser that loses the response, or a seed that never made it into the
authenticator, therefore cannot leave an account that demands a code nobody can
generate.

Route table::

    GET    /api/mfa/status                      own account, human
    POST   /api/mfa/enroll                      own account, human
    POST   /api/mfa/enroll/confirm              own account, human
    POST   /api/mfa/verify                      own session, human (step-up)
    POST   /api/mfa/disable                     own account, human + fresh reauth
    POST   /api/mfa/recovery-codes/regenerate    own account, human + fresh reauth
    POST   /api/mfa/users/{user_id}/reset       identity:write + fresh reauth
    POST   /api/mfa/challenge                  own session (fresh-factor challenge)
"""
from __future__ import annotations

import uuid
from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.identity_errors import translate
from app.auth.dependencies import (
    TenantContext,
    _client_ip,
    get_identity_context,
    require_human_session,
    require_permission,
)
from app.auth.identity import events as identity_events
from app.auth.identity import exceptions as identity_exc
from app.auth.identity import mfa as identity_mfa
from app.auth.identity import policies
from app.auth.identity import sessions as identity_sessions
from app.auth.identity.service import (
    IdentityContext,
    assert_privileged,
    mark_mfa_verified,
)
from app.auth.permissions import Permission
from app.core.config import settings
from app.core.rate_limit import allow_identity_action
from app.db.models import AuditAction, User
from app.db.session import get_session

log = structlog.get_logger()
router = APIRouter(prefix="/api/mfa", tags=["identity"])


class MFAStatusOut(BaseModel):
    enrolled: bool
    pending: bool
    factor_id: str | None = None
    confirmed_at: datetime | None = None
    last_used_at: datetime | None = None
    recovery_codes_remaining: int = 0
    recovery_codes_total: int = 0
    recovery_codes_low: bool = False
    required_by_policy: bool = False
    required_for_admins: bool = False
    enabled_on_deployment: bool = True
    issuer: str = "VoxDesk"
    digits: int = 6
    period_seconds: int = 30


def _status(state: identity_mfa.MFAState, user: User, policy) -> MFAStatusOut:
    return MFAStatusOut(
        enrolled=state.enrolled,
        pending=state.pending,
        factor_id=str(state.factor_id) if state.factor_id else None,
        confirmed_at=state.confirmed_at,
        last_used_at=state.last_used_at,
        recovery_codes_remaining=state.recovery_codes_remaining,
        recovery_codes_total=state.recovery_codes_total,
        recovery_codes_low=state.recovery_codes_low,
        required_by_policy=policies.mfa_required_for(
            policy, role=user.role, user_override=user.mfa_required
        ),
        required_for_admins=policy.mfa_required_for_admins,
        enabled_on_deployment=settings.mfa_enabled,
        issuer=settings.mfa_issuer,
        digits=settings.mfa_totp_digits,
        period_seconds=settings.mfa_totp_period_seconds,
    )


@router.get("/status", response_model=MFAStatusOut)
async def mfa_status(
    ctx: TenantContext = Depends(require_human_session),
    ictx: IdentityContext = Depends(get_identity_context),
    session: AsyncSession = Depends(get_session),
):
    """Whether the caller has a second factor, and whether they must have one."""
    return _status(await identity_mfa.load_state(session, ctx.user), ctx.user, ictx.policy)


class EnrollOut(BaseModel):
    factor_id: str
    secret: str
    provisioning_uri: str
    digits: int
    period_seconds: int
    hint: str = (
        "Add this to your authenticator app, then confirm with a code. The seed "
        "is shown exactly once."
    )


@router.post("/enroll", response_model=EnrollOut, status_code=201)
async def enroll(
    request: Request,
    ctx: TenantContext = Depends(require_human_session),
    session: AsyncSession = Depends(get_session),
):
    """Start TOTP enrollment. Returns the seed once, and nothing is active yet."""
    if not await allow_identity_action(
        action="mfa_enroll", who=str(ctx.user_id), limit=10, window=3600.0
    ):
        raise HTTPException(
            status_code=429,
            detail={"code": "rate_limited", "message": "Too many attempts. Try again later."},
            headers={"Retry-After": "600"},
        )
    try:
        start = await identity_mfa.begin_enrollment(session, ctx.user, commit=False)
    except identity_exc.IdentityError as exc:
        await session.rollback()
        raise translate(exc) from None
    await session.commit()
    log.info(
        "identity.mfa_enrollment_started",
        user_id=str(ctx.user_id),
        tenant_id=str(ctx.tenant_id),
        ip_address=_client_ip(request),
    )
    return EnrollOut(
        factor_id=str(start.factor_id),
        secret=start.secret,
        provisioning_uri=start.provisioning_uri,
        digits=start.digits,
        period_seconds=start.period_seconds,
    )


class ConfirmIn(BaseModel):
    code: str = Field(min_length=6, max_length=10)


class RecoveryCodesOut(BaseModel):
    recovery_codes: list[str]
    warning: str = (
        "These codes are shown once. Each one works a single time and replaces "
        "your password only as the second factor."
    )


@router.post("/enroll/confirm", response_model=RecoveryCodesOut)
async def confirm_enrollment(
    payload: ConfirmIn,
    request: Request,
    ctx: TenantContext = Depends(require_human_session),
    session: AsyncSession = Depends(get_session),
):
    """Activate the pending factor and return the recovery codes once."""
    try:
        codes = await identity_mfa.confirm_enrollment(
            session, ctx.user, code=payload.code, commit=False
        )
    except identity_exc.MFAChallengeLocked as exc:
        await session.rollback()
        raise translate(exc) from None
    except identity_exc.IdentityError as exc:
        await session.rollback()
        raise translate(exc) from None
    await session.commit()
    log.info(
        "identity.mfa_enrolled",
        user_id=str(ctx.user_id),
        tenant_id=str(ctx.tenant_id),
        ip_address=_client_ip(request),
    )
    return RecoveryCodesOut(recovery_codes=codes)


class StepUpIn(BaseModel):
    code: str = Field(min_length=6, max_length=40)

    #: Optional, and the preferred form: the token handed out by
    #: ``POST /api/mfa/challenge``. Presenting it binds the verification to
    #: that challenge, which means it is single use, counted against the
    #: per-challenge failure ceiling, and locked to the session that asked for
    #: it. A bare ``code`` stays supported for clients written before the
    #: challenge existed, and is rate limited per user instead.
    challenge: str | None = Field(default=None, min_length=8, max_length=200)


class StepUpOut(BaseModel):
    ok: bool = True
    mfa_verified: bool = True
    used_recovery_code: bool = False


@router.post("/verify", response_model=StepUpOut)
async def step_up(
    payload: StepUpIn,
    request: Request,
    ctx: TenantContext = Depends(require_human_session),
    ictx: IdentityContext = Depends(get_identity_context),
    session: AsyncSession = Depends(get_session),
):
    """Satisfy a fresh-factor requirement on the session the caller already has.

    This is the endpoint the dashboard calls when an action answers 428: the
    session is fine, but the *proof of presence* it carries is stale. On success
    the session's ``mfa_verified_at`` is refreshed, so the next privileged call
    passes.

    Two ways in, and the difference matters. With a ``challenge`` token the
    verification is resolved through the challenge row: single use, counted
    against ``MFA_CHALLENGE_MAX_FAILURES``, and refused unless the challenge was
    issued *for this session* and *for reauthentication*. Without one, the code
    is checked against the caller's own factor, which is the pre-challenge
    behaviour and is kept so an older client does not break.
    """
    if ictx.session_row is None:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "no_session",
                "message": "This token is not bound to a session, so it cannot be elevated.",
            },
        )
    if not await allow_identity_action(
        action="mfa_step_up", who=str(ctx.user_id), limit=20, window=300.0
    ):
        raise HTTPException(
            status_code=429,
            detail={"code": "rate_limited", "message": "Too many attempts. Try again later."},
            headers={"Retry-After": "300"},
        )

    challenge_id = ""
    used_recovery_code = False
    if payload.challenge:
        # The challenge decides everything: a *login* challenge must not be
        # spendable here (that would let a half-finished sign-in elevate a
        # session it does not belong to), and a challenge minted on another
        # session must not be spendable here either. The binding is checked
        # *before* the code is resolved, so a request from the wrong session
        # cannot spend the challenge the rightful one is about to answer.
        bound = await identity_mfa.load_challenge(session, token=payload.challenge)
        if (
            bound is None
            or bound.purpose != identity_mfa.ChallengePurpose.REAUTHENTICATE.value
            or bound.session_id != ictx.session_row.id
        ):
            raise HTTPException(
                status_code=401,
                detail={
                    "code": "mfa_invalid_challenge",
                    "message": "That verification request cannot be used here.",
                },
            )

        try:
            result = await identity_mfa.resolve_challenge(
                session,
                token=payload.challenge,
                code=payload.code,
                ip_address=_client_ip(request),
                commit=False,
            )
        except identity_exc.MFAChallengeLocked as exc:
            await session.rollback()
            raise translate(exc) from None
        except identity_exc.IdentityError as exc:
            await session.rollback()
            raise translate(exc) from None

        if not result.ok:
            # Committed: the failure counter and the lockout live on the
            # challenge row, and discarding them would make it unenforceable.
            await session.commit()
            raise HTTPException(
                status_code=401,
                detail={"code": "mfa_invalid_code", "message": "That code is not valid."},
            )
        used_recovery_code = result.used_recovery_code
        challenge_id = str(result.challenge.id)
    else:
        try:
            ok = await identity_mfa.verify_user_code(
                session, ctx.user, code=payload.code, commit=False
            )
        except identity_exc.MFAChallengeLocked as exc:
            await session.rollback()
            raise translate(exc) from None
        except identity_exc.IdentityError as exc:
            await session.rollback()
            raise translate(exc) from None
        if not ok:
            # Committed: the failure counter and the lockout live on the challenge
            # row, and discarding them would make the lockout unenforceable.
            await session.commit()
            raise HTTPException(
                status_code=401,
                detail={"code": "mfa_invalid_code", "message": "That code is not valid."},
            )
    await mark_mfa_verified(session, session_row=ictx.session_row, commit=False)
    await identity_events.emit(
        session,
        AuditAction.MFA_VERIFIED,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        target_user_id=ctx.user_id,
        actor_email=ctx.user.email,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent", "")[:300],
        detail={
            "reason": "step_up",
            "session_id": str(ictx.session_row.id),
            "challenge_id": challenge_id,
            "bound": bool(payload.challenge),
        },
        commit=False,
    )
    await session.commit()
    return StepUpOut(used_recovery_code=used_recovery_code)


class ChallengeOut(BaseModel):
    challenge_token: str
    expires_at: datetime
    purpose: str


@router.post("/challenge", response_model=ChallengeOut, status_code=201)
async def create_challenge(
    ctx: TenantContext = Depends(require_human_session),
    ictx: IdentityContext = Depends(get_identity_context),
    session: AsyncSession = Depends(get_session),
):
    """Ask for a second-factor challenge before a sensitive action.

    A challenge is bound to the caller's own session and dies with it, so a token
    captured from one browser cannot be used to elevate another.
    """
    try:
        issued = await identity_mfa.create_challenge(
            session,
            ctx.user,
            purpose=identity_mfa.ChallengePurpose.REAUTHENTICATE,
            session_row_id=ictx.session_row.id if ictx.session_row else None,
            commit=False,
        )
    except identity_exc.IdentityError as exc:
        await session.rollback()
        raise translate(exc) from None
    await session.commit()
    return ChallengeOut(
        challenge_token=issued.token,
        expires_at=issued.expires_at,
        purpose=identity_mfa.ChallengePurpose.REAUTHENTICATE.value,
    )


@router.post("/disable", status_code=204)
async def disable_mfa(
    request: Request,
    ctx: TenantContext = Depends(require_human_session),
    ictx: IdentityContext = Depends(get_identity_context),
    session: AsyncSession = Depends(get_session),
):
    """Remove the caller's second factor.

    Requires a fresh proof of presence (``assert_privileged``), which is the
    point: an attacker holding a borrowed, unlocked browser must not be able to
    switch off the control that would otherwise stop them. Every other session
    dies with the factor, because the factor was what protected them.
    """
    try:
        await assert_privileged(session, ictx, action="mfa.disable")
        await identity_mfa.disable_factor(session, ctx.user, reason="user_disabled", commit=False)
    except identity_exc.IdentityError as exc:
        await session.rollback()
        raise translate(exc) from None

    revoked = await identity_sessions.revoke_all_for_user(
        session,
        user_id=ctx.user_id,
        reason="mfa_disabled",
        keep_session_id=ctx.session_id,
        commit=False,
    )
    await identity_events.emit(
        session,
        AuditAction.MFA_DISABLED,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        target_user_id=ctx.user_id,
        actor_email=ctx.user.email,
        ip_address=_client_ip(request),
        detail={"reason": "user_disabled", "other_sessions_revoked": revoked},
        commit=False,
    )
    await session.commit()
    log.info(
        "identity.mfa_disabled",
        user_id=str(ctx.user_id),
        tenant_id=str(ctx.tenant_id),
        revoked_sessions=revoked,
    )


@router.post("/recovery-codes/regenerate", response_model=RecoveryCodesOut)
async def regenerate_recovery_codes(
    request: Request,
    ctx: TenantContext = Depends(require_human_session),
    ictx: IdentityContext = Depends(get_identity_context),
    session: AsyncSession = Depends(get_session),
):
    """Replace the caller's recovery codes. The previous set stops working."""
    try:
        await assert_privileged(session, ictx, action="mfa.recovery_codes.regenerate")
        codes = await identity_mfa.regenerate_recovery_codes(session, ctx.user, commit=False)
    except identity_exc.IdentityError as exc:
        await session.rollback()
        raise translate(exc) from None
    await identity_events.emit(
        session,
        AuditAction.MFA_RECOVERY_CODES_REGENERATED,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        target_user_id=ctx.user_id,
        actor_email=ctx.user.email,
        ip_address=_client_ip(request),
        detail={"count": len(codes)},
        commit=False,
    )
    await session.commit()
    return RecoveryCodesOut(recovery_codes=codes)


@router.post("/users/{user_id}/reset", status_code=204)
async def admin_reset_user_mfa(
    user_id: uuid.UUID,
    request: Request,
    ctx: TenantContext = Depends(require_permission(Permission.IDENTITY_WRITE)),
    ictx: IdentityContext = Depends(get_identity_context),
    session: AsyncSession = Depends(get_session),
):
    """Administrative reset for a user who lost their authenticator and codes.

    This is why MFA here is not a locked door with no key. It is also the most
    abusable endpoint in the feature, so it is the most guarded: ``identity:write``,
    a human session, a fresh proof of presence, and a target that must be in the
    caller's own tenant. The target's other sessions are revoked, so a reset can
    never be used to take over a *live* session -- the legitimate user simply
    signs in again.
    """
    if ctx.is_machine:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "human_session_required",
                "message": "A signed-in user session is required.",
            },
        )
    target = await session.get(User, user_id)
    if target is None or target.tenant_id != ctx.tenant_id:
        # A user in another tenant is not "forbidden" here, it is absent: the
        # caller must not learn which ids exist elsewhere.
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Not found"})
    if target.id == ctx.user_id:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "use_self_service",
                "message": "Use the self-service flow in Settings to change your own factor.",
            },
        )
    try:
        await assert_privileged(session, ictx, action="mfa.admin_reset")
        await identity_mfa.disable_factor(session, target, reason="admin_reset", commit=False)
    except identity_exc.IdentityError as exc:
        await session.rollback()
        raise translate(exc) from None

    revoked = await identity_sessions.revoke_all_for_user(
        session, user_id=target.id, reason="mfa_reset_by_admin", commit=False
    )
    await identity_events.emit(
        session,
        AuditAction.MFA_DISABLED,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        target_user_id=target.id,
        actor_email=ctx.user.email,
        ip_address=_client_ip(request),
        detail={
            "reason": "admin_reset",
            "target_email": target.email,
            "sessions_revoked": revoked,
        },
        commit=False,
    )
    await session.commit()


@router.get("/users/{user_id}/status", response_model=MFAStatusOut)
async def admin_read_user_mfa(
    user_id: uuid.UUID,
    ctx: TenantContext = Depends(require_permission(Permission.IDENTITY_READ)),
    ictx: IdentityContext = Depends(get_identity_context),
    session: AsyncSession = Depends(get_session),
):
    """Whether another user in this tenant has a factor. Never their seed."""
    target = (
        await session.execute(
            select(User).where(User.id == user_id, User.tenant_id == ctx.tenant_id)
        )
    ).scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Not found"})
    return _status(await identity_mfa.load_state(session, target), target, ictx.policy)
