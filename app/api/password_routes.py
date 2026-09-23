"""Password reset and email verification.

Both flows share a shape, and the shape is the security property:

* **The request step answers identically whatever happens.** Registered or not,
  active or not, deliverable or not -- the same 202 and the same body. Otherwise
  the endpoint is an account-enumeration oracle, and the reset link is exactly
  the thing worth enumerating.
* **The token is single-use and short-lived.** A conditional UPDATE spends it, so
  two requests presenting the same link cannot both succeed, and the plaintext
  exists only in the email.
* **Completing a reset kills every session.** A password change is the signal
  that the account may have been compromised; leaving other sessions alive would
  leave the attacker signed in.
* **Everything is rate limited per subject and per address**, on a limiter that
  is active regardless of whether the deployment-wide middleware is on.

Route table::

    POST /auth/password-reset/request           public, uniform, rate limited
    POST /auth/password-reset/confirm           public, single-use token
    POST /auth/email-verification/request       authenticated, own address
    POST /auth/email-verification/confirm       public, single-use token
    GET  /auth/identity-config                  public, non-secret capabilities

``GET /auth/sso/discover`` used to be served from here as well. It now lives
only in ``app.api.sso_routes.public_router``: one handler per path, so which
one answers can never depend on router include order.
"""
from __future__ import annotations

from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.identity_errors import translate
from app.auth import password as pw
from app.auth import service
from app.auth.dependencies import (
    TenantContext,
    _client_ip,
    require_human_session,
)
from app.auth.identity import email as identity_email
from app.auth.identity import events as identity_events
from app.auth.identity import policies
from app.auth.identity import sessions as identity_sessions
from app.auth.identity.exceptions import IdentityError
from app.core.config import settings
from app.core.rate_limit import allow_identity_action
from app.db.models import AuditAction, Tenant, User
from app.db.session import get_session

log = structlog.get_logger()
router = APIRouter(prefix="/auth", tags=["identity"])

#: One answer for every outcome of a reset request.
UNIFORM_MESSAGE = (
    "If an account exists for that address, a message is on its way. "
    "Check your inbox and your spam folder."
)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ResetRequestIn(BaseModel):
    email: EmailStr


class ResetRequestOut(BaseModel):
    status: str = "accepted"
    message: str = UNIFORM_MESSAGE
    #: Only ever set when ``IDENTITY_DEBUG_TOKENS`` is switched on, outside
    #: production, with the log transport: it lets a developer finish the flow
    #: without a mail server. Left off, every request gets the same body whether
    #: or not the address exists, and a production boot refuses both the log
    #: transport and the switch (``Settings.validate_security``).
    development_token: str | None = None


@router.post("/password-reset/request", response_model=ResetRequestOut, status_code=202)
async def request_password_reset(
    payload: ResetRequestIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Start a password reset. Uniform response, always 202."""
    ip = _client_ip(request)

    for action, who, limit in (
        ("password_reset_request", str(payload.email).lower(), 5),
        ("password_reset_request_ip", ip, 20),
    ):
        if not await allow_identity_action(action=action, who=who, limit=limit, window=3600.0):
            log.warning("identity.password_reset_rate_limited", action=action)
            raise HTTPException(
                status_code=429,
                detail={"code": "rate_limited", "message": "Too many requests. Try again later."},
                headers={"Retry-After": "3600"},
            )

    normalized = service.normalize_email(str(payload.email))
    user = (await session.execute(select(User).where(User.email == normalized))).scalar_one_or_none()

    development_token: str | None = None
    if user is not None:
        policy = await policies.load_policy(session, user.tenant_id)
        domain = await policies.load_domain_evaluation(session, user.email)
        decision = policies.evaluate_password_reset_policy(
            policy, role=user.role, domain_policy=domain
        )
        if decision.allowed and user.is_active:
            token = await identity_email.issue_password_reset_token(
                session, user_id=user.id, tenant_id=user.tenant_id, commit=False
            )
            message = identity_email.render_password_reset(
                to=user.email,
                reset_url=identity_email.reset_url(token.plaintext),
                ttl_minutes=settings.password_reset_ttl_minutes,
                tenant_name=await _tenant_name(session, user.tenant_id),
            )
            try:
                await identity_email.send(message)
            except IdentityError as exc:
                # A delivery failure must not change the response -- that would
                # leak which addresses exist -- but operators must see it.
                log.error(
                    "identity.password_reset_delivery_failed",
                    reason=exc.code,
                    user_id=str(user.id),
                )
            await identity_events.emit(
                session,
                AuditAction.PASSWORD_RESET_REQUESTED,
                tenant_id=user.tenant_id,
                actor_user_id=user.id,
                target_user_id=user.id,
                actor_email=user.email,
                ip_address=ip,
                user_agent=request.headers.get("user-agent", "")[:300],
                detail={"expires_at": token.expires_at.isoformat(), "policy_allowed": True},
                commit=False,
            )
            if (
                settings.identity_debug_tokens
                and not settings.is_production
                and settings.email_transport == "log"
            ):
                development_token = token.plaintext
        else:
            await identity_events.emit(
                session,
                AuditAction.PASSWORD_RESET_REQUESTED,
                tenant_id=user.tenant_id,
                actor_user_id=user.id,
                target_user_id=user.id,
                actor_email=user.email,
                ip_address=ip,
                detail={"policy_allowed": False, "reason": decision.reason},
                commit=False,
            )
        await session.commit()
    else:
        # No user: no token, no audit row tying an address to this response.
        await session.commit()

    return ResetRequestOut(development_token=development_token)


async def _tenant_name(session: AsyncSession, tenant_id) -> str:
    tenant = await session.get(Tenant, tenant_id)
    return tenant.name if tenant is not None else "VoxDesk"


class ResetConfirmIn(BaseModel):
    token: str = Field(min_length=16, max_length=400)
    new_password: str = Field(min_length=1, max_length=512)


class ResetConfirmOut(BaseModel):
    status: str = "password_changed"
    revoked_sessions: int = 0


@router.post("/password-reset/confirm", response_model=ResetConfirmOut)
async def confirm_password_reset(
    payload: ResetConfirmIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Spend the token, set the new password, and end every session."""
    ip = _client_ip(request)
    if not await allow_identity_action(
        action="password_reset_confirm", who=ip, limit=20, window=900.0
    ):
        raise HTTPException(
            status_code=429,
            detail={"code": "rate_limited", "message": "Too many attempts. Try again later."},
            headers={"Retry-After": "900"},
        )

    try:
        user_id = await identity_email.consume_password_reset_token(
            session, plaintext=payload.token
        )
    except IdentityError as exc:
        await session.commit()
        raise translate(exc) from None

    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        await session.commit()
        raise HTTPException(
            status_code=400,
            detail={"code": "token_invalid", "message": "That reset link is not valid."},
        )

    try:
        # The same policy signup uses: a reset link must not be a way around the
        # minimum length or the common-password list.
        pw.validate_policy(payload.new_password, email=user.email)
        new_hash = pw.hash_password(payload.new_password)
    except pw.PasswordPolicyError as exc:
        await session.commit()
        raise HTTPException(
            status_code=400,
            detail={"code": "password_policy", "message": str(exc)},
        ) from None

    user.password_hash = new_hash
    user.password_changed_at = _now()
    user.failed_login_count = 0
    user.locked_until = None
    # Every existing session dies: whoever held the account before the reset
    # does not keep their session because they asked for the reset.
    user.token_version += 1
    revoked = await identity_sessions.revoke_all_for_user(
        session, user_id=user.id, reason="password_reset", commit=False
    )
    await service.revoke_all_for_user(session, user.id, bump_version=False)

    await identity_events.emit(
        session,
        AuditAction.PASSWORD_RESET_COMPLETED,
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        target_user_id=user.id,
        actor_email=user.email,
        ip_address=ip,
        user_agent=request.headers.get("user-agent", "")[:300],
        detail={"revoked_sessions": revoked},
        commit=False,
    )
    await service.record_audit(
        session,
        action=AuditAction.PASSWORD_CHANGED,
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        target_user_id=user.id,
        actor_email=user.email,
        ip_address=ip,
        detail={"method": "password_reset"},
        commit=False,
    )
    await session.commit()

    # Notice after the fact: if this was not the user, they need to know now, and
    # the notice must not block the reset itself.
    await identity_email.send_security_notice(
        to=user.email,
        title="Your password was reset",
        lines=[
            "The password for your account was changed just now.",
            "If this was you, nothing further is needed. If it was not, contact your "
            f"administrator immediately and change your password again (from {ip or 'an unknown address'}).",
        ],
        tenant_name=await _tenant_name(session, user.tenant_id),
    )
    return ResetConfirmOut(revoked_sessions=revoked)


class VerifyRequestOut(BaseModel):
    status: str = "accepted"
    message: str = "Verification message sent."
    development_token: str | None = None


@router.post("/email-verification/request", response_model=VerifyRequestOut)
async def request_email_verification(
    request: Request,
    ctx: TenantContext = Depends(require_human_session),
    session: AsyncSession = Depends(get_session),
):
    """Send (or resend) the verification link for the caller's own address."""
    if ctx.user.email_verified_at is not None:
        return VerifyRequestOut(
            status="already_verified", message="This address is already confirmed."
        )
    if not await allow_identity_action(
        action="email_verification", who=str(ctx.user_id), limit=3, window=3600.0
    ):
        raise HTTPException(
            status_code=429,
            detail={"code": "rate_limited", "message": "Too many requests. Try again later."},
            headers={"Retry-After": "3600"},
        )

    token = await identity_email.issue_verification_token(
        session,
        user_id=ctx.user_id,
        tenant_id=ctx.tenant_id,
        email=ctx.user.email,
        commit=False,
    )
    message = identity_email.render_email_verification(
        to=ctx.user.email,
        verify_url=identity_email.verification_url(token.plaintext),
        ttl_hours=settings.email_verification_ttl_hours,
        tenant_name=ctx.tenant.name,
    )
    try:
        await identity_email.send(message)
    except IdentityError as exc:
        await session.rollback()
        raise translate(exc) from None

    await identity_events.emit(
        session,
        AuditAction.EMAIL_VERIFICATION_SENT,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        target_user_id=ctx.user_id,
        actor_email=ctx.user.email,
        ip_address=_client_ip(request),
        detail={"expires_at": token.expires_at.isoformat()},
        commit=False,
    )
    await session.commit()
    development_token = (
        token.plaintext
        if (
            settings.identity_debug_tokens
            and not settings.is_production
            and settings.email_transport == "log"
        )
        else None
    )
    return VerifyRequestOut(development_token=development_token)


class VerifyConfirmIn(BaseModel):
    token: str = Field(min_length=16, max_length=400)


class VerifyConfirmOut(BaseModel):
    status: str = "verified"


@router.post("/email-verification/confirm", response_model=VerifyConfirmOut)
async def confirm_email_verification(
    payload: VerifyConfirmIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Spend a verification link and mark the address proven."""
    ip = _client_ip(request)
    if not await allow_identity_action(
        action="email_verification_confirm", who=ip, limit=20, window=900.0
    ):
        raise HTTPException(
            status_code=429,
            detail={"code": "rate_limited", "message": "Too many attempts. Try again later."},
            headers={"Retry-After": "900"},
        )

    try:
        user_id = await identity_email.consume_verification_token(
            session, plaintext=payload.token
        )
    except IdentityError as exc:
        await session.commit()
        raise translate(exc) from None

    user = await session.get(User, user_id)
    if user is None:
        await session.commit()
        raise HTTPException(
            status_code=400,
            detail={"code": "token_invalid", "message": "That verification link is not valid."},
        )

    from app.auth.identity.models import UserEmail

    verified_at = _now()
    user.email_verified_at = verified_at
    row = (
        await session.execute(select(UserEmail).where(UserEmail.email == user.email))
    ).scalar_one_or_none()
    if row is None:
        session.add(
            UserEmail(
                tenant_id=user.tenant_id,
                user_id=user.id,
                email=user.email,
                is_primary=True,
                verified_at=verified_at,
                source="verify",
            )
        )
    else:
        row.verified_at = verified_at

    await identity_events.emit(
        session,
        AuditAction.EMAIL_VERIFIED,
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        target_user_id=user.id,
        actor_email=user.email,
        ip_address=ip,
        user_agent=request.headers.get("user-agent", "")[:300],
        detail={},
        commit=False,
    )
    await session.commit()
    return VerifyConfirmOut()


class IdentityConfigOut(BaseModel):
    mfa_supported: bool
    sso_supported: bool
    password_reset_supported: bool
    password_min_length: int
    delivery_configured: bool


@router.get("/identity-config", response_model=IdentityConfigOut)
async def identity_config():
    """Non-secret capabilities for a login page.

    Deliberately tiny and tenant-independent: a login form needs to know whether
    to render "use single sign-on" and "forgot password" links, not which tenant
    it is looking at.
    """
    return IdentityConfigOut(
        mfa_supported=settings.mfa_enabled,
        sso_supported=settings.sso_enabled,
        password_reset_supported=settings.email_transport in ("log", "smtp"),
        password_min_length=pw.MIN_PASSWORD_LENGTH,
        # Whether a reset link would actually be delivered here. The login page
        # uses it to decide between "forgot password" and "contact your
        # administrator", which is the honest distinction when a deployment has
        # no mail transport configured.
        delivery_configured=identity_email.delivery_is_configured(),
    )


