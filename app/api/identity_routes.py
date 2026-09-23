"""Identity status, tenant policy, and fresh reauthentication.

Three small surfaces that everything else depends on:

* **Status** answers "what does this deployment and this account support?" --
  whether MFA is on, whether the caller has a factor, whether SSO is configured,
  whether their address is verified. The dashboard needs it to decide what to
  render; it reveals nothing about anyone else.
* **Policy** is the per-tenant identity configuration. Reading it needs
  ``identity:read``; writing it needs ``identity:write``, a human session and a
  fresh proof of presence, and it refuses two combinations that would lock a
  workspace out of itself.
* **Reauth** is how a session proves presence again. It is deliberately
  indifferent to *how* -- password or second factor -- and equally rate limited
  either way, because the endpoint exists to raise the cost of a stolen session,
  not to be a second login form with a different set of mistakes.

Route table::

    GET   /api/identity/status       any authenticated caller
    GET   /api/identity/policy       identity:read
    PATCH /api/identity/policy       identity:write + human + fresh reauth
    POST  /api/identity/reauth       own session, human, rate limited
    GET   /api/identity/events       identity:read
"""
from __future__ import annotations

from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.identity_errors import translate
from app.auth import password as pw
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
from app.auth.identity.models import SSOConnection, SSOStatus
from app.auth.identity.service import (
    IdentityContext,
    assert_privileged,
    identity_features,
    mark_password_confirmed,
)
from app.auth.permissions import Permission
from app.core.rate_limit import allow_identity_action
from app.db.models import AuditAction, AuditLog
from app.db.session import get_session

log = structlog.get_logger()
router = APIRouter(prefix="/api/identity", tags=["identity"])

#: How many events the identity feed returns by default. The full audit log has
#: its own paginated endpoint; this one answers "what changed lately".
DEFAULT_EVENT_LIMIT = 50


class PolicyOut(BaseModel):
    identity_policy_id: str | None = None
    is_default: bool = False
    mfa_required: bool = False
    mfa_required_for_admins: bool = False
    privileged_reauth_required: bool = True
    privileged_reauth_minutes: int = 15
    sso_required: bool = False
    password_login_allowed: bool = True
    api_keys_allowed: bool = True
    service_accounts_allowed: bool = True
    scim_enabled: bool = True
    jit_provisioning_allowed: bool = True
    session_idle_minutes: int = 720
    session_max_active: int = 20
    refresh_token_days: int = 14
    allowed_email_domains: list[str] = []

    @classmethod
    def of(cls, policy: policies.ResolvedPolicy) -> PolicyOut:
        return cls(
            identity_policy_id=str(policy.row_id) if policy.row_id else None,
            is_default=policy.is_default,
            mfa_required=policy.mfa_required,
            mfa_required_for_admins=policy.mfa_required_for_admins,
            privileged_reauth_required=policy.privileged_reauth_required,
            privileged_reauth_minutes=policy.privileged_reauth_minutes,
            sso_required=policy.sso_required,
            password_login_allowed=policy.password_login_allowed,
            api_keys_allowed=policy.api_keys_allowed,
            service_accounts_allowed=policy.service_accounts_allowed,
            scim_enabled=policy.scim_enabled,
            jit_provisioning_allowed=policy.jit_provisioning_allowed,
            session_idle_minutes=policy.session_idle_minutes,
            session_max_active=policy.session_max_active,
            refresh_token_days=policy.refresh_token_days,
            allowed_email_domains=list(policy.allowed_email_domains),
        )


class MFAStatusOut(BaseModel):
    enrolled: bool
    pending: bool
    recovery_codes_remaining: int = 0
    recovery_codes_total: int = 0
    required_by_policy: bool = False


class SessionOut(BaseModel):
    session_id: str | None = None
    auth_method: str = "password"
    mfa_verified: bool = False
    mfa_verified_at: datetime | None = None
    password_confirmed_at: datetime | None = None
    created_at: datetime | None = None
    idle_expires_at: datetime | None = None
    expires_at: datetime | None = None
    device: str = ""


class StatusOut(BaseModel):
    features: dict
    policy: PolicyOut
    mfa: MFAStatusOut
    session: SessionOut
    email_verified: bool
    reauth: dict


@router.get("/status", response_model=StatusOut)
async def identity_status(
    ctx: TenantContext = Depends(require_human_session),
    ictx: IdentityContext = Depends(get_identity_context),
    session: AsyncSession = Depends(get_session),
):
    """What this account and this deployment can do, for the settings page.

    Requires a human session rather than only a valid token: the payload
    describes a *person* -- their factors, their sitting, their reauth window --
    and a machine credential has none of those. Every signed-in role may read
    their own; nothing here is a tenant secret a viewer may not see.
    """
    state = await identity_mfa.load_state(session, ctx.user)
    row = ictx.session_row
    return StatusOut(
        features=identity_features(ictx.policy),
        policy=PolicyOut.of(ictx.policy),
        mfa=MFAStatusOut(
            enrolled=state.enrolled,
            pending=state.pending,
            recovery_codes_remaining=state.recovery_codes_remaining,
            recovery_codes_total=state.recovery_codes_total,
            required_by_policy=policies.mfa_required_for(
                ictx.policy, role=ctx.role, user_override=ctx.user.mfa_required
            ),
        ),
        session=SessionOut(
            session_id=str(row.id) if row else None,
            auth_method=row.auth_method if row else ctx.auth_method,
            mfa_verified=bool(row.mfa_verified) if row else False,
            mfa_verified_at=row.mfa_verified_at if row else None,
            password_confirmed_at=row.password_confirmed_at if row else None,
            created_at=row.created_at if row else None,
            idle_expires_at=row.idle_expires_at if row else None,
            expires_at=row.expires_at if row else None,
            device=row.device_label if row else "",
        ),
        email_verified=ctx.user.email_verified_at is not None,
        reauth={
            "required": ictx.policy.privileged_reauth_required,
            "fresh_minutes": ictx.policy.privileged_reauth_minutes,
            "has_factor": ictx.has_mfa_factor,
            "password_set": bool(ctx.user.password_hash),
        },
    )


@router.get("/policy", response_model=PolicyOut)
async def read_policy(
    ctx: TenantContext = Depends(require_permission(Permission.IDENTITY_READ)),
    session: AsyncSession = Depends(get_session),
):
    """The resolved policy, defaults included, so a UI can show the effective state."""
    return PolicyOut.of(await policies.load_policy(session, ctx.tenant_id))


class PolicyIn(BaseModel):
    #: Unknown keys are refused. A policy endpoint that silently ignores a
    #: misspelled setting is the worst outcome available: the operator sees a
    #: success, and the workspace stays open in the way they were trying to
    #: close.
    model_config = ConfigDict(extra="forbid")

    mfa_required: bool | None = None
    mfa_required_for_admins: bool | None = None
    privileged_reauth_required: bool | None = None
    privileged_reauth_minutes: int | None = Field(default=None, ge=1, le=1440)
    sso_required: bool | None = None
    password_login_allowed: bool | None = None
    api_keys_allowed: bool | None = None
    service_accounts_allowed: bool | None = None
    scim_enabled: bool | None = None
    jit_provisioning_allowed: bool | None = None
    session_idle_minutes: int | None = Field(default=None, ge=5, le=43200)
    session_max_active: int | None = Field(default=None, ge=1, le=200)
    refresh_token_days: int | None = Field(default=None, ge=1, le=365)
    allowed_email_domains: list[str] | None = None


POLICY_FIELDS = (
    "mfa_required",
    "mfa_required_for_admins",
    "privileged_reauth_required",
    "privileged_reauth_minutes",
    "sso_required",
    "password_login_allowed",
    "api_keys_allowed",
    "service_accounts_allowed",
    "scim_enabled",
    "jit_provisioning_allowed",
    "session_idle_minutes",
    "session_max_active",
    "refresh_token_days",
    "allowed_email_domains",
)


@router.patch("/policy", response_model=PolicyOut)
async def update_policy(
    payload: PolicyIn,
    request: Request,
    ctx: TenantContext = Depends(require_permission(Permission.IDENTITY_WRITE)),
    ictx: IdentityContext = Depends(get_identity_context),
    session: AsyncSession = Depends(get_session),
):
    """Change how this workspace authenticates.

    Guarded three ways, because it is the most consequential endpoint in the
    feature: the permission, a human session, and a fresh proof of presence. Two
    combinations are refused outright because they are the two ways an
    administrator locks a tenant out of itself:

    * requiring SSO with no *active* connection, and
    * disabling password login with no active connection either.

    Both are refusals rather than warnings, because the failure mode is not a
    degraded login -- it is no login at all, for everyone, including the
    administrator making the change.
    """
    if ctx.is_machine:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "human_session_required",
                "message": "A signed-in user session is required.",
            },
        )
    try:
        await assert_privileged(session, ictx, action="identity.policy.update")
    except identity_exc.IdentityError as exc:
        await session.rollback()
        raise translate(exc) from None

    from app.auth.identity.models import IdentityPolicy

    row = (
        await session.execute(
            select(IdentityPolicy).where(IdentityPolicy.tenant_id == ctx.tenant_id)
        )
    ).scalar_one_or_none()

    wants_sso_only = bool(payload.sso_required) or payload.password_login_allowed is False
    if wants_sso_only:
        active = (
            await session.execute(
                select(SSOConnection.id).where(
                    SSOConnection.tenant_id == ctx.tenant_id,
                    SSOConnection.status == SSOStatus.ACTIVE.value,
                ).limit(1)
            )
        ).first()
        if active is None:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "no_active_sso_connection",
                    "message": (
                        "Configure and activate a working SSO connection before "
                        "requiring single sign-on or turning off password sign-in. "
                        "Until then, that change would lock everyone out."
                    ),
                },
            )

    if row is None:
        row = IdentityPolicy(tenant_id=ctx.tenant_id, updated_by_user_id=ctx.user_id)
        session.add(row)

    changes: dict = {}
    for field in POLICY_FIELDS:
        value = getattr(payload, field)
        if value is None:
            continue
        if field == "allowed_email_domains":
            normalized = sorted(
                {str(item).strip().lower().lstrip("@") for item in value if str(item).strip()}
            )
            if row.allowed_email_domains != normalized:
                row.allowed_email_domains = normalized
                changes[field] = normalized
            continue
        if getattr(row, field) != value:
            setattr(row, field, value)
            changes[field] = value
    row.updated_by_user_id = ctx.user_id

    await identity_events.emit(
        session,
        AuditAction.SECURITY_SETTINGS_CHANGED,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        actor_email=ctx.user.email,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent", "")[:300],
        detail={"operation": "identity_policy_updated", "changes": changes},
        commit=False,
    )
    await session.commit()
    return PolicyOut.of(await policies.load_policy(session, ctx.tenant_id))


class ReauthIn(BaseModel):
    password: str | None = Field(default=None, max_length=512)
    code: str | None = Field(default=None, max_length=40)


class ReauthOut(BaseModel):
    ok: bool = True
    method: str


@router.post("/reauth", response_model=ReauthOut)
async def reauthenticate(
    payload: ReauthIn,
    request: Request,
    ctx: TenantContext = Depends(require_human_session),
    session: AsyncSession = Depends(get_session),
):
    """Prove presence again on this session.

    Both credentials are accepted, both are rate limited, and both fail with the
    same shape of message. A password is verified against the stored hash (with
    the same dummy-hash timing equaliser the login path uses); a code goes
    through the same challenge machinery, so a lockout applies here too.
    """
    if ctx.session_id is None:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "no_session",
                "message": "This token is not bound to a session, so it cannot be reauthenticated.",
            },
        )
    row = await identity_sessions.get_session(session, ctx.user_id, ctx.session_id)
    if row is None or not row.is_live(datetime.now().astimezone().replace(tzinfo=None)):
        raise HTTPException(
            status_code=401,
            detail={"code": "session_revoked", "message": "This session has ended."},
        )

    if not await allow_identity_action(
        action="reauth", who=str(ctx.user_id), limit=10, window=300.0
    ):
        raise HTTPException(
            status_code=429,
            detail={"code": "rate_limited", "message": "Too many attempts. Try again later."},
            headers={"Retry-After": "300"},
        )

    method = ""
    if payload.code:
        try:
            ok = await identity_mfa.verify_user_code(session, ctx.user, code=payload.code)
        except identity_exc.MFAChallengeLocked as exc:
            await session.rollback()
            raise translate(exc) from None
        except identity_exc.IdentityError as exc:
            await session.rollback()
            raise translate(exc) from None
        if not ok:
            await session.commit()
            raise HTTPException(
                status_code=401,
                detail={"code": "mfa_invalid_code", "message": "That code is not valid."},
            )
        method = "totp"
    elif payload.password:
        if not pw.verify_password(payload.password, ctx.user.password_hash):
            pw.verify_dummy()
            raise HTTPException(
                status_code=401,
                detail={"code": "invalid_password", "message": "That password is not correct."},
            )
        method = "password"
    else:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "missing_credential",
                "message": "Provide your password or a second-factor code.",
            },
        )

    if method == "password":
        await mark_password_confirmed(session, session_row=row, commit=False)
    await identity_events.emit(
        session,
        AuditAction.IDENTITY_REAUTHENTICATED,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        target_user_id=ctx.user_id,
        actor_email=ctx.user.email,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent", "")[:300],
        detail={"method": method, "session_id": str(row.id)},
        commit=False,
    )
    await session.commit()
    return ReauthOut(method=method)


class IdentityEventOut(BaseModel):
    id: str
    action: str
    actor_user_id: str | None = None
    actor_email: str = ""
    target_user_id: str | None = None
    ip_address: str = ""
    created_at: datetime | None = None
    detail: dict = {}


@router.get("/events", response_model=list[IdentityEventOut])
async def identity_event_feed(
    ctx: TenantContext = Depends(require_permission(Permission.IDENTITY_READ)),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=DEFAULT_EVENT_LIMIT, ge=1, le=200),
):
    """Recent identity and security events for this tenant.

    A filtered view over the existing audit log rather than a second store: the
    events are already recorded there, and an operator investigating an incident
    should not have to look in two places to reconstruct one.
    """
    names = {AuditAction(value) for value in identity_events.event_names()}
    rows = (
        await session.execute(
            select(AuditLog)
            .where(AuditLog.tenant_id == ctx.tenant_id, AuditLog.action.in_(names))
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [
        IdentityEventOut(
            id=str(row.id),
            action=row.action.value,
            actor_user_id=str(row.actor_user_id) if row.actor_user_id else None,
            actor_email=row.actor_email or "",
            target_user_id=str(row.target_user_id) if row.target_user_id else None,
            ip_address=row.ip_address or "",
            created_at=row.created_at,
            detail=row.detail or {},
        )
        for row in rows
    ]
