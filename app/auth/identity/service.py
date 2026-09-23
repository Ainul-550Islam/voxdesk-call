"""The identity façade: everything the rest of the app calls.

Routes, the login path and the federated login path all need the same three
things — this user's effective policy, this session's rung on the
reauthentication ladder, and a refusal that is typed rather than a bare 403 —
and they must agree on all three. Agreement is easier to keep when the logic
lives in one module with no FastAPI imports, so this file deliberately returns
data and raises domain errors; ``app/api/identity_routes.py`` and
``app/auth/dependencies.py`` do the HTTP translation.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.identity import policies, sessions
from app.auth.identity.exceptions import (
    PolicyDenied,
    ReauthenticationRequired,
)
from app.auth.identity.models import UserSession
from app.auth.identity.policies import AuthMethod, ResolvedPolicy
from app.core.config import settings
from app.db.models import Tenant, User

log = structlog.get_logger()


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=None) if value.tzinfo else value


@dataclass(frozen=True)
class IdentityContext:
    """The identity facts a request handler is allowed to rely on.

    Built once per request by ``app.auth.dependencies.get_identity_context``
    and passed to routes that need more than a role: the session being used,
    the policy in force, and whether a second factor is enrolled.
    """

    user: User
    tenant: Tenant
    policy: ResolvedPolicy
    session_row: UserSession | None
    auth_method: AuthMethod
    has_mfa_factor: bool
    scopes: frozenset[str] | None = None
    service_account_id: uuid.UUID | None = None
    api_key_id: uuid.UUID | None = None

    @property
    def tenant_id(self) -> uuid.UUID:
        return self.tenant.id

    @property
    def user_id(self) -> uuid.UUID:
        return self.user.id

    @property
    def session_id(self) -> uuid.UUID | None:
        return self.session_row.id if self.session_row else None

    @property
    def is_machine(self) -> bool:
        return self.auth_method in (
            AuthMethod.API_KEY,
            AuthMethod.SERVICE_ACCOUNT,
            AuthMethod.SCIM,
        )

    @property
    def mfa_verified_at(self) -> datetime | None:
        return self.session_row.mfa_verified_at if self.session_row else None

    @property
    def password_confirmed_at(self) -> datetime | None:
        return self.session_row.password_confirmed_at if self.session_row else None


async def load_identity_context(
    session: AsyncSession,
    *,
    user: User,
    tenant: Tenant,
    session_id: uuid.UUID | None = None,
    auth_method: AuthMethod = AuthMethod.PASSWORD,
    scopes: frozenset[str] | None = None,
    service_account_id: uuid.UUID | None = None,
    api_key_id: uuid.UUID | None = None,
    policy: ResolvedPolicy | None = None,
) -> IdentityContext:
    """Assemble the context, reading only what the caller asked for."""
    resolved_policy = policy or await policies.load_policy(session, tenant.id)
    row: UserSession | None = None
    if session_id is not None:
        row = await session.get(UserSession, session_id)
        if row is not None and (row.user_id != user.id or row.tenant_id != tenant.id):
            # A session id from another user or tenant is not a session for
            # this request. Treated as absent, and the token's other checks
            # (token_version) still apply.
            row = None
    return IdentityContext(
        user=user,
        tenant=tenant,
        policy=resolved_policy,
        session_row=row,
        auth_method=auth_method,
        has_mfa_factor=await has_active_factor(session, user_id=user.id),
        scopes=scopes,
        service_account_id=service_account_id,
        api_key_id=api_key_id,
    )


async def has_active_factor(session: AsyncSession, *, user_id: uuid.UUID) -> bool:
    """Whether this user has at least one confirmed second factor.

    Delegates to ``identity.mfa`` so there is exactly one definition of
    "active factor"; this name stays because it is the one the auth service and
    the identity routes have always imported.
    """
    from app.auth.identity import mfa as identity_mfa

    return await identity_mfa.has_active_factor(session, user_id=user_id)


async def assert_privileged(
    session: AsyncSession,
    ctx: IdentityContext,
    *,
    action: str,
) -> None:
    """Refuse a dangerous action unless the session is fresh enough.

    Raises ``ReauthenticationRequired`` (HTTP 428 via the handler in
    ``app/api/identity_routes.py``) or ``PolicyDenied``. Callers are the routes
    that change how a tenant authenticates: SSO connection writes, MFA
    disablement, recovery-code regeneration, credential revocation, domain
    enforcement, and service-account emergency controls.
    """
    evaluation = policies.evaluate_privileged_action(
        ctx.policy,
        has_active_mfa_factor=ctx.has_mfa_factor,
        mfa_verified_at=ctx.mfa_verified_at,
        password_confirmed_at=ctx.password_confirmed_at,
        auth_method=ctx.auth_method,
    )
    if not evaluation.allowed:
        log.warning(
            "identity.privileged_action_refused",
            action=action,
            reason=evaluation.reason,
            user_id=str(ctx.user_id),
            tenant_id=str(ctx.tenant_id),
        )
        raise PolicyDenied(evaluation.reason)

    if evaluation.requires_mfa:
        raise ReauthenticationRequired(
            "Confirm your identity with your authenticator app to continue."
        )
    if evaluation.requires_password:
        raise ReauthenticationRequired(
            "Confirm your password to continue."
        )


async def mark_mfa_verified(
    session: AsyncSession,
    *,
    session_row: UserSession,
    commit: bool = False,
) -> UserSession:
    """Record that the session has satisfied a second factor, just now."""
    session_row.mfa_verified = True
    session_row.mfa_verified_at = _now()
    if commit:
        await session.commit()
    return session_row


async def mark_password_confirmed(
    session: AsyncSession,
    *,
    session_row: UserSession,
    commit: bool = False,
) -> UserSession:
    """Record a fresh password check on this session (the reauth ladder)."""
    session_row.password_confirmed_at = _now()
    if commit:
        await session.commit()
    return session_row


async def ensure_session(
    session: AsyncSession,
    user: User,
    *,
    auth_method: AuthMethod = AuthMethod.PASSWORD,
    mfa_verified: bool = False,
    password_confirmed: bool = False,
    ip_address: str = "",
    user_agent: str = "",
    device_label: str = "",
    sso_connection_id: uuid.UUID | None = None,
    commit: bool = False,
) -> tuple[UserSession, str]:
    """Open a session and return it with its display label.

    Called from the login paths. Kept here rather than in
    ``app.auth.service`` so that the token layer does not import the identity
    policy layer, and so the "every login opens a session" rule has a single
    implementation that both the password and the federated flows call.
    """
    policy = await policies.load_policy(session, user.tenant_id)
    row = await sessions.create_session(
        session,
        user,
        policy=policy,
        auth_method=auth_method,
        mfa_verified=mfa_verified,
        mfa_verified_at=_now() if mfa_verified else None,
        password_confirmed_at=_now() if password_confirmed else None,
        ip_address=ip_address,
        user_agent=user_agent,
        device_label=device_label,
        sso_connection_id=sso_connection_id,
        commit=commit,
    )
    return row, row.device_label


async def default_idle_minutes(session: AsyncSession, tenant_id: uuid.UUID) -> int:
    policy = await policies.load_policy(session, tenant_id)
    return policy.session_idle_minutes


async def purge_stale_sessions(session: AsyncSession, *, tenant_id: uuid.UUID) -> int:
    """Housekeeping: mark long-expired sessions revoked for a tenant.

    Not on a timer — this codebase has no scheduler and inventing one for a
    cosmetic status field would be disproportionate. It runs when an
    administrator lists sessions, which is exactly when the number is being
    looked at.
    """
    cutoff = _now() - timedelta(days=1)
    rows = (
        await session.execute(
            select(UserSession).where(
                UserSession.tenant_id == tenant_id,
                UserSession.revoked_at.is_(None),
                UserSession.expires_at <= cutoff,
            ).limit(500)
        )
    ).scalars().all()
    for row in rows:
        row.revoked_at = row.expires_at
        row.revoked_reason = "expired"
    return len(rows)


def identity_features(policy: ResolvedPolicy | None = None) -> dict:
    """What this deployment *and this tenant* have switched on.

    The dashboard renders its settings pages from this, so a page never offers
    a control whose feature the deployment has compiled out. ``password_login``
    is reported per tenant because a tenant may require SSO and turn it off.
    """
    return {
        "mfa_enabled": settings.mfa_enabled,
        "sso_enabled": settings.sso_enabled,
        "scim_enabled": settings.scim_enabled,
        "password_login_allowed": policy.password_login_allowed if policy else True,
        "sso_required": policy.sso_required if policy else False,
        "secrets_configured": bool(settings.identity_key_ring_source),
        "mfa_issuer": settings.mfa_issuer,
        "mfa_digits": settings.mfa_totp_digits,
        "mfa_period_seconds": settings.mfa_totp_period_seconds,
    }
