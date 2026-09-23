"""
Authentication endpoints.

Response models are explicit allow-lists. There is no `from_attributes` model
over `User` that could start leaking a new sensitive column the day somebody
adds one -- every exposed field is written out by hand in `UserOut`.
"""
from __future__ import annotations

import uuid
from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import service
from app.auth.dependencies import TenantContext, _client_ip, get_context
from app.auth.identity import events as identity_events
from app.auth.identity import exceptions as mfa_identity
from app.auth.identity import mfa
from app.auth.identity import sessions
from app.auth.identity.models import ChallengePurpose
from app.auth.rbac import describe_roles, permissions_for
from app.core.config import settings
from app.db.models import AuditAction, User
from app.db.session import get_session

log = structlog.get_logger()
router = APIRouter(prefix="/auth", tags=["auth"])

REFRESH_COOKIE = "voxdesk_refresh"


# ----------------------------------------------------------------- schemas ---

class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=512)


class RefreshIn(BaseModel):
    """Optional: the refresh token normally travels in an HttpOnly cookie."""
    refresh_token: str | None = None


class UserOut(BaseModel):
    """Safe public projection. Never add a secret field to this model."""
    id: uuid.UUID
    email: str
    full_name: str
    role: str
    tenant_id: uuid.UUID
    is_active: bool
    created_at: datetime | None = None
    last_login_at: datetime | None = None

    @classmethod
    def of(cls, user: User) -> UserOut:
        return cls(
            id=user.id,
            email=user.email,
            full_name=user.full_name,
            role=user.role.value,
            tenant_id=user.tenant_id,
            is_active=user.is_active,
            created_at=user.created_at,
            last_login_at=user.last_login_at,
        )


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut
    #: STEP 18. Additive field: existing clients ignore it. It reports the one
    #: state a client must act on -- the tenant requires a second factor and
    #: this user has not enrolled one yet -- without pretending the login
    #: failed, because the enrollment page it needs lives behind a login.
    mfa_enrollment_required: bool = False


class MFAChallengeOut(BaseModel):
    """Returned by ``/auth/login`` instead of tokens when a factor is owed."""

    mfa_required: bool = True
    challenge: str
    expires_in: int
    methods: list[str] = ["totp", "recovery_code"]


class MeOut(BaseModel):
    user: UserOut
    tenant: dict
    permissions: list[str]


# ----------------------------------------------------------------- helpers ---

def _set_refresh_cookie(response: Response, token: str) -> None:
    """
    HttpOnly so browser JavaScript cannot read it, which keeps the long-lived
    credential out of localStorage and out of reach of XSS.
    """
    response.set_cookie(
        key=REFRESH_COOKIE,
        value=token,
        httponly=True,
        secure=settings.uses_https,      # scheme-driven: Secure only over TLS
        samesite="lax",
        max_age=settings.refresh_token_days * 24 * 3600,
        path="/auth",                    # never sent to /api or /telephony
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(REFRESH_COOKIE, path="/auth")


# ------------------------------------------------------------------ routes ---

@router.post(
    "/login",
    response_model=TokenOut,
    responses={
        202: {
            "model": MFAChallengeOut,
            "description": (
                "A second factor is required. Complete it with "
                "POST /auth/mfa/verify to obtain tokens."
            ),
        }
    },
)
async def login(
    payload: LoginIn,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
):
    """
    Returns a short-lived access token in the body and sets the refresh token
    as an HttpOnly cookie. Failure is always the same 401 and the same
    message, whether or not the address exists.
    """
    ip = _client_ip(request)
    agent = request.headers.get("user-agent", "")[:300]

    try:
        user = await service.authenticate(
            session, email=payload.email, password=payload.password,
            ip_address=ip, user_agent=agent,
        )
    except service.AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from None

    requirements = await service.post_login_requirements(session, user)

    # A required second factor is not a failed login: the password was right
    # and the session is verified, but no tokens are handed out until the
    # factor is presented. 202 with a challenge says exactly that, and cannot be
    # mistaken for success by a client that only checks for a 2xx.
    if requirements.mfa_required and requirements.mfa_available:
        challenge = await mfa.create_challenge(
            session, user, purpose=ChallengePurpose.LOGIN,
            ip_address=ip, user_agent=agent, commit=False,
        )
        await session.commit()
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content=MFAChallengeOut(
                challenge=challenge.token,
                expires_in=settings.mfa_challenge_ttl_seconds,
            ).model_dump(),
        )

    tokens = await service.issue_tokens(session, user, ip_address=ip, user_agent=agent)
    _set_refresh_cookie(response, tokens["refresh_token"])

    return TokenOut(
        access_token=tokens["access_token"],
        expires_in=tokens["expires_in"],
        user=UserOut.of(user),
        mfa_enrollment_required=requirements.enrollment_required,
    )


class MFAVerifyIn(BaseModel):
    challenge: str = Field(min_length=8, max_length=200)
    code: str = Field(min_length=6, max_length=32)


@router.post(
    "/mfa/verify",
    response_model=TokenOut,
    responses={401: {"description": "The code, the challenge, or the session is no longer valid."}},
)
async def verify_mfa(
    payload: MFAVerifyIn,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
):
    """Second step of a login that owes a factor.

    Lives under ``/auth`` on purpose: the refresh cookie is scoped to that path,
    so the browser accepts it here without any cookie-path change and without
    the token ever being readable by JavaScript.
    """
    ip = _client_ip(request)
    agent = request.headers.get("user-agent", "")[:300]

    # The challenge's *purpose* is binding, and it is checked before the code is
    # resolved: this endpoint completes a login, so only a login challenge is
    # acceptable here. A challenge raised for a signed-in session (the step-up
    # path, purpose ``reauth``) is single-use and session-bound; spending it to
    # finish a *different* authentication would let one purpose's proof stand in
    # for another's. Resolving first and refusing afterwards would burn the
    # challenge the rightful caller was about to answer.
    bound = await mfa.load_challenge(session, token=payload.challenge)
    if bound is None or bound.purpose != ChallengePurpose.LOGIN.value:
        raise HTTPException(
            status_code=401,
            detail="That verification request cannot be used here.",
        )

    try:
        result = await mfa.resolve_challenge(
            session, token=payload.challenge, code=payload.code,
            ip_address=ip, commit=False,
        )
    except mfa_identity.MFAChallengeLocked as exc:
        await session.commit()
        raise HTTPException(
            status_code=429, detail=str(exc), headers={"Retry-After": "60"}
        ) from None
    except mfa_identity.MFAError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from None

    if not result.ok:
        await session.commit()
        # One message for every kind of wrong code, so the endpoint cannot be
        # used to tell a TOTP guess from a recovery-code guess.
        raise HTTPException(status_code=401, detail="That code is not valid.")

    user = result.user
    if result.used_recovery_code:
        await mfa.recovery_code_used_event(session, user, commit=False)
    else:
        await mfa.verified_event(session, user, commit=False)

    tokens = await service.issue_tokens(
        session, user, ip_address=ip, user_agent=agent,
        auth_method="password", mfa_verified=True,
    )
    requirements = await service.post_login_requirements(session, user)
    _set_refresh_cookie(response, tokens["refresh_token"])
    return TokenOut(
        access_token=tokens["access_token"],
        expires_in=tokens["expires_in"],
        user=UserOut.of(user),
        mfa_enrollment_required=requirements.enrollment_required,
    )


@router.post("/refresh", response_model=TokenOut)
async def refresh(
    request: Request,
    response: Response,
    payload: RefreshIn | None = None,
    session: AsyncSession = Depends(get_session),
):
    token = (payload.refresh_token if payload else None) or request.cookies.get(
        REFRESH_COOKIE
    )
    if not token:
        raise HTTPException(status_code=401, detail="Invalid refresh token.")

    try:
        tokens = await service.rotate_refresh_token(
            session, token,
            ip_address=_client_ip(request),
            user_agent=request.headers.get("user-agent", "")[:300],
        )
    except service.AuthError as exc:
        _clear_refresh_cookie(response)
        raise HTTPException(status_code=401, detail=str(exc)) from None

    from app.auth.jwt import decode_access_token
    claims = decode_access_token(tokens["access_token"])
    user = await session.get(User, claims.user_id)

    _set_refresh_cookie(response, tokens["refresh_token"])
    return TokenOut(
        access_token=tokens["access_token"],
        expires_in=tokens["expires_in"],
        user=UserOut.of(user),
    )


@router.post("/logout", status_code=204)
async def logout(
    request: Request,
    response: Response,
    payload: RefreshIn | None = None,
    ctx: TenantContext = Depends(get_context),
    session: AsyncSession = Depends(get_session),
):
    """End this session: refresh token, session row, and therefore the access token.

    Revoking only the refresh token would leave the access token working for up
    to its remaining lifetime, which is not what "log out" means to a user
    holding a stolen laptop.
    """
    token = (payload.refresh_token if payload else None) or request.cookies.get(
        REFRESH_COOKIE
    )
    if token:
        await service.revoke_refresh_token(session, token)

    # STEP 18: ending a session ends it for the access token too, not just for
    # the refresh token. Without this, "log out" would leave the access token
    # working for up to its remaining lifetime.
    if ctx.session_id:
        row = await sessions.get_session(session, ctx.user_id, ctx.session_id)
        if row is not None:
            await sessions.revoke_session(session, row, reason="logout", commit=False)
            await identity_events.emit(
                session, AuditAction.SESSION_REVOKED,
                tenant_id=ctx.tenant_id, actor_user_id=ctx.user_id,
                target_user_id=ctx.user_id, actor_email=ctx.user.email,
                ip_address=_client_ip(request),
                detail={"session_id": str(row.id), "reason": "logout"},
                commit=False,
            )

    await service.record_audit(
        session, action=AuditAction.LOGOUT, tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id, actor_email=ctx.user.email,
        ip_address=_client_ip(request),
    )
    _clear_refresh_cookie(response)
    return Response(status_code=204)


@router.post("/logout-all", status_code=204)
async def logout_everywhere(
    response: Response,
    ctx: TenantContext = Depends(get_context),
    session: AsyncSession = Depends(get_session),
):
    """Revokes every session and invalidates outstanding access tokens.

    Both layers, because they defend different things: ``token_version`` kills
    every access token that already exists, and the session rows are what the
    user can see and what the sessions page reports.
    """
    await service.revoke_all_for_user(session, ctx.user_id, bump_version=True)
    await sessions.revoke_all_for_user(
        session, user_id=ctx.user_id, reason="logout_all", commit=False
    )
    await identity_events.emit(
        session, AuditAction.SESSION_REVOKED, tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id, target_user_id=ctx.user_id,
        actor_email=ctx.user.email, detail={"reason": "logout_all"},
        commit=True,
    )
    _clear_refresh_cookie(response)
    return Response(status_code=204)


@router.get("/me", response_model=MeOut)
async def me(ctx: TenantContext = Depends(get_context)):
    return MeOut(
        user=UserOut.of(ctx.user),
        tenant={
            "id": str(ctx.tenant.id),
            "name": ctx.tenant.name,
            "industry": ctx.tenant.industry,
            "plan": ctx.tenant.plan,
            "language": ctx.tenant.language,
            # STEP 8 phase 1, purely additive. The dashboard renders every
            # timestamp in the *tenant's* zone rather than the browser's --
            # a US business viewed from Dhaka must show US call times -- and
            # labels agent turns with the tenant's configured agent name
            # instead of the hard-coded "Alex" the audit found. Both columns
            # already existed on `Tenant`; only this projection was missing,
            # so there is no schema change and no new query.
            "timezone": ctx.tenant.timezone,
            "agent_name": ctx.tenant.agent_name,
            "twilio_number": ctx.tenant.twilio_number,
        },
        permissions=sorted(p.value for p in permissions_for(ctx.role)),
    )


@router.get("/roles")
async def roles(ctx: TenantContext = Depends(get_context)):
    """The policy itself, so the dashboard can render role pickers correctly."""
    return {"roles": describe_roles()}