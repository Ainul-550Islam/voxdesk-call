"""The identity policy evaluator.

Every "may this happen?" question in the identity layer is answered here, in
pure functions over a resolved policy — no database, no request, no FastAPI.
That matters for three reasons:

* **One place to read.** The alternative is the one this feature exists to
  avoid: `if tenant.sso_required and user.role != "admin" ...` sprinkled across
  a dozen route handlers, each subtly different.
* **Testable without a server.** The policy matrix in
  ``tests/security/test_authorization_matrix.py`` calls these functions
  directly and pins the decisions, including the ones that must be *refusals*.
* **Traceable.** Each function names its callers in the docstring, so a change
  can be followed to the routes that depend on it.

Default behaviour is the important part: a tenant with **no** policy row gets
exactly today's behaviour — password login allowed, MFA available but not
required, SSO optional, API keys and service accounts allowed. Turning this
feature on cannot change an existing tenant's login by itself; only an
administrator writing a policy row can.
"""
from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.auth.permissions import Permission
from app.auth.rbac import permissions_for, role_level
from app.core.config import settings
from app.db.models import UserRole


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=None) if value.tzinfo else value


class AuthMethod(str, enum.Enum):
    """How a caller proved who they are. Carried on the request context."""

    PASSWORD = "password"
    SSO = "sso"
    API_KEY = "api_key"
    SERVICE_ACCOUNT = "service_account"
    SCIM = "scim"


#: Roles that count as "administrative" for the MFA-for-admins policy.
ADMIN_ROLES: frozenset[UserRole] = frozenset({UserRole.OWNER, UserRole.ADMIN})


@dataclass(frozen=True)
class ResolvedPolicy:
    """A tenant's identity policy with every default filled in."""

    tenant_id: uuid.UUID
    row_id: uuid.UUID | None = None

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
    allowed_email_domains: tuple[str, ...] = ()

    @property
    def is_default(self) -> bool:
        """True when no row exists, i.e. nothing has been configured."""
        return self.row_id is None


def default_policy(tenant_id: uuid.UUID) -> ResolvedPolicy:
    """The policy for a tenant that has never configured identity."""
    return ResolvedPolicy(
        tenant_id=tenant_id,
        privileged_reauth_minutes=settings.mfa_fresh_minutes,
        session_idle_minutes=settings.session_idle_minutes,
        session_max_active=settings.session_max_active,
        refresh_token_days=settings.refresh_token_days,
    )


def resolve_policy(row, tenant_id: uuid.UUID) -> ResolvedPolicy:
    """Fold a stored row (or ``None``) into a fully-defaulted policy."""
    if row is None:
        return default_policy(tenant_id)
    return ResolvedPolicy(
        tenant_id=tenant_id,
        row_id=row.id,
        mfa_required=bool(row.mfa_required),
        mfa_required_for_admins=bool(row.mfa_required_for_admins),
        privileged_reauth_required=bool(row.privileged_reauth_required),
        privileged_reauth_minutes=(
            row.privileged_reauth_minutes
            if row.privileged_reauth_minutes is not None
            else settings.mfa_fresh_minutes
        ),
        sso_required=bool(row.sso_required),
        password_login_allowed=bool(row.password_login_allowed),
        api_keys_allowed=bool(row.api_keys_allowed),
        service_accounts_allowed=bool(row.service_accounts_allowed),
        scim_enabled=bool(row.scim_enabled),
        jit_provisioning_allowed=bool(row.jit_provisioning_allowed),
        session_idle_minutes=(
            row.session_idle_minutes
            if row.session_idle_minutes is not None
            else settings.session_idle_minutes
        ),
        session_max_active=(
            row.session_max_active
            if row.session_max_active is not None
            else settings.session_max_active
        ),
        refresh_token_days=(
            row.refresh_token_days
            if row.refresh_token_days is not None
            else settings.refresh_token_days
        ),
        allowed_email_domains=tuple(
            d.strip().lower() for d in (row.allowed_email_domains or []) if str(d).strip()
        ),
    )


async def load_policy(session, tenant_id: uuid.UUID) -> ResolvedPolicy:
    """Read the tenant's policy row, defaulting when there is none.

    Called by: ``app.auth.service.authenticate`` (login), the MFA routes, the
    session routes, the SSO service, the SCIM service, the domain service and
    the API-key service. It is one indexed lookup on a unique key.
    """
    from sqlalchemy import select

    from app.auth.identity.models import IdentityPolicy

    row = (
        await session.execute(
            select(IdentityPolicy).where(IdentityPolicy.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    return resolve_policy(row, tenant_id)


# ============================================================ login policy ===


@dataclass(frozen=True)
class LoginEvaluation:
    allowed: bool
    reason: str = ""
    mfa_required: bool = False
    sso_required: bool = False
    password_login_allowed: bool = True


def evaluate_login_policy(
    policy: ResolvedPolicy,
    *,
    email: str = "",
    role: UserRole | None = None,
    auth_method: AuthMethod = AuthMethod.PASSWORD,
    mfa_available: bool = False,
    user_mfa_override: bool | None = None,
    domain_policy: "DomainEvaluation | None" = None,
) -> LoginEvaluation:
    """May this principal finish a login of this kind, and with what caveats?

    Called by: ``app.auth.service.authenticate`` (password path) and
    ``app.auth.sso.service.complete_login`` (federated path), plus the
    reauthentication helper. A refusal is a policy statement — the caller turns
    it into a 403 with the *reason code*, never into a generic failure that
    would hide a misconfiguration from an administrator.
    """
    effective_mfa = mfa_required_for(
        policy, role=role, user_override=user_mfa_override
    )
    # An unproven claim imposes nothing. ``evaluate_domain_policy`` already
    # gates its output on ``verified_at``; the rule is repeated here, where the
    # decision is actually taken, so a caller that assembles a DomainEvaluation
    # by hand still cannot refuse a login on the strength of a domain name
    # somebody typed. This is the pre-hijack rule, in the one place every login
    # path goes through.
    verified_claim = bool(domain_policy and domain_policy.verified)
    sso_required = policy.sso_required or (verified_claim and domain_policy.requires_sso)

    if auth_method is AuthMethod.PASSWORD:
        if not policy.password_login_allowed:
            return LoginEvaluation(
                False, "password_login_disabled", sso_required=sso_required,
                password_login_allowed=False,
            )
        if verified_claim and domain_policy.blocks_password_login:
            return LoginEvaluation(
                False, "domain_requires_sso", sso_required=True,
                password_login_allowed=False,
            )
        if sso_required:
            return LoginEvaluation(
                False, "sso_required", sso_required=True,
                password_login_allowed=False,
            )
        if policy.allowed_email_domains:
            domain = email.rsplit("@", 1)[-1].lower() if "@" in email else ""
            if domain not in policy.allowed_email_domains:
                return LoginEvaluation(False, "email_domain_not_allowed",
                                       sso_required=sso_required)

    if auth_method is AuthMethod.SSO and not mfa_available:
        # A tenant that requires MFA and authenticates through an IdP is
        # relying on that IdP to assert it. We do not guess: the requirement is
        # only satisfied when the assertion says so (see sso/claims.py), and
        # this branch documents that an unasserted login is not automatically
        # considered MFA-verified.
        pass

    return LoginEvaluation(
        True,
        "",
        mfa_required=effective_mfa,
        sso_required=sso_required,
        password_login_allowed=policy.password_login_allowed,
    )


def mfa_required_for(
    policy: ResolvedPolicy, *, role: UserRole | None, user_override: bool | None = None
) -> bool:
    """Whether this principal must present a second factor at login.

    Precedence, highest first: an explicit per-user value, then the admin-role
    rule, then the tenant-wide rule.

    The per-user column (``users.mfa_required``) is nullable and NULL means
    "follow the tenant policy"; a value is set by an operator, either to force
    a factor on for one account or to keep a break-glass account out of the
    requirement. It wins over the tenant rule in both directions, on purpose:
    a break-glass account that the tenant policy could re-arm would not be one.
    """
    if user_override is not None:
        return bool(user_override)
    if policy.mfa_required:
        return True
    if policy.mfa_required_for_admins and role in ADMIN_ROLES:
        return True
    return False


# ========================================================== session policy ===


@dataclass(frozen=True)
class SessionLimits:
    idle_minutes: int
    max_active: int
    refresh_days: int
    absolute_expires_at: datetime

    def idle_deadline(self, now: datetime | None = None) -> datetime:
        return (now or _now()) + timedelta(minutes=self.idle_minutes)


def evaluate_session_policy(policy: ResolvedPolicy, *, now: datetime | None = None) -> SessionLimits:
    """Lifetimes and the concurrent-session ceiling for a new session.

    Called by: ``app.auth.sessions.create_session`` (and therefore every login
    path, password and federated) and by the session-listing route, which
    reports the same numbers it enforces rather than a second copy.
    """
    moment = now or _now()
    refresh_days = max(1, int(policy.refresh_token_days))
    return SessionLimits(
        idle_minutes=max(1, int(policy.session_idle_minutes)),
        max_active=max(1, int(policy.session_max_active)),
        refresh_days=refresh_days,
        absolute_expires_at=moment + timedelta(days=refresh_days),
    )


# ===================================================== privileged actions ===


@dataclass(frozen=True)
class PrivilegedEvaluation:
    allowed: bool
    reason: str = ""
    requires_mfa: bool = False
    requires_password: bool = False
    fresh_within_minutes: int = 0


def evaluate_privileged_action(
    policy: ResolvedPolicy,
    *,
    now: datetime | None = None,
    mfa_verified_at: datetime | None = None,
    password_confirmed_at: datetime | None = None,
    has_active_mfa_factor: bool = False,
    auth_method: AuthMethod = AuthMethod.PASSWORD,
) -> PrivilegedEvaluation:
    """Whether a dangerous action may proceed on this session as it stands.

    Called by: ``app.auth.identity.service.assert_privileged``, which is in
    turn the guard behind every dangerous route (SSO create/update/disable,
    MFA disable, recovery-code regeneration, credential revocation, domain
    enforcement changes).

    The rule is deliberately *not* "require a second factor": an operator with
    no factor enrolled must still be able to administer their tenant, and
    locking them out of their own security settings is a self-inflicted outage.
    What is required is a *fresh proof of presence* — a second factor if one is
    enrolled, otherwise the account password.
    """
    if not policy.privileged_reauth_required:
        return PrivilegedEvaluation(True, fresh_within_minutes=0)

    window = max(1, int(policy.privileged_reauth_minutes))
    moment = now or _now()

    if auth_method in (AuthMethod.API_KEY, AuthMethod.SERVICE_ACCOUNT, AuthMethod.SCIM):
        # A machine credential cannot reauthenticate as a human, so it may
        # never perform a privileged identity action. This is the rule that
        # stops a leaked API key from rewriting the tenant's SSO.
        return PrivilegedEvaluation(
            False, "machine_credential_cannot_reauth", fresh_within_minutes=window
        )

    if has_active_mfa_factor:
        verified = _naive(mfa_verified_at)
        if verified is None:
            return PrivilegedEvaluation(
                True, "mfa_required", requires_mfa=True, fresh_within_minutes=window
            )
        if verified >= moment - timedelta(minutes=window):
            return PrivilegedEvaluation(True, fresh_within_minutes=window)
        return PrivilegedEvaluation(
            True, "fresh_mfa_required", requires_mfa=True, fresh_within_minutes=window
        )

    # No factor enrolled: demand the password instead of an impossible factor.
    confirmed = _naive(password_confirmed_at)
    if confirmed is not None and confirmed >= moment - timedelta(minutes=window):
        return PrivilegedEvaluation(True, fresh_within_minutes=window)
    return PrivilegedEvaluation(
        True, "password_reauth_required", requires_password=True,
        fresh_within_minutes=window,
    )


# ======================================================= machine credentials ===


@dataclass(frozen=True)
class CredentialEvaluation:
    allowed: bool
    reason: str = ""


def evaluate_api_credential(
    policy: ResolvedPolicy, *, kind: str
) -> CredentialEvaluation:
    """Whether a machine credential of this kind may be used at all.

    Called by: ``app.auth.api_keys.authenticate_key`` and
    ``app.auth.service_accounts.authenticate_credential``. A tenant that has
    turned API keys off must lose them immediately, not at their next
    expiration — so this is evaluated per request, not per issuance.
    """
    if kind == "api_key" and not policy.api_keys_allowed:
        return CredentialEvaluation(False, "api_keys_disabled")
    if kind == "service_account":
        if not policy.service_accounts_allowed:
            return CredentialEvaluation(False, "service_accounts_disabled")
    return CredentialEvaluation(True)


def evaluate_scope_grant(
    *,
    creator_role: UserRole,
    creator_permissions: frozenset[Permission] | None,
    requested_scopes: list[str],
) -> CredentialEvaluation:
    """Privilege-escalation guard for keys and service accounts.

    Called by: ``app.auth.api_keys.create_key`` and
    ``app.auth.service_accounts.create_account``. A credential may only carry
    scopes its creator already holds, so minting a key is never a way to obtain
    a permission — and the platform-only permissions (creating or deleting
    whole tenants) are never grantable this way at all.
    """
    from app.auth.rbac import is_platform_permission

    held = creator_permissions
    if held is None:
        held = permissions_for(creator_role)

    for scope in requested_scopes:
        try:
            permission = Permission(scope)
        except ValueError:
            return CredentialEvaluation(False, f"unknown_scope:{scope}")
        if is_platform_permission(permission):
            return CredentialEvaluation(False, f"platform_scope_not_grantable:{scope}")
        if permission not in held:
            return CredentialEvaluation(False, f"scope_not_held:{scope}")
    return CredentialEvaluation(True)


def scopes_allow(
    scopes: frozenset[str] | None, required: tuple[Permission, ...], *, require_all: bool = True
) -> bool:
    """Whether a credential's scopes satisfy a route's requirement.

    Called by: ``app.auth.dependencies.require_permission`` when the principal
    authenticated with a machine credential. A scope string is the permission
    value, so read/write separation falls straight out of the vocabulary:
    a key holding ``call:read`` cannot satisfy a route asking for
    ``campaign:run``, and there is no wildcard to widen it.
    """
    if scopes is None:
        return False
    values = {p.value for p in required}
    if require_all:
        return values.issubset(scopes)
    return bool(values & scopes)


# ============================================================= SSO / domain ===


@dataclass(frozen=True)
class SSOEvaluation:
    allowed: bool
    reason: str = ""
    jit_allowed: bool = True


def evaluate_sso_policy(
    policy: ResolvedPolicy,
    *,
    connection_status: str,
    jit_requested: bool = False,
) -> SSOEvaluation:
    """Whether a connection may be used to log in, and whether it may provision.

    Called by: ``app.auth.sso.service.start_login`` (before an authorization
    request is ever built) and ``complete_login`` (before a user is touched).
    Both, because the connection could have been disabled in between.
    """
    if connection_status != "active":
        return SSOEvaluation(False, f"connection_{connection_status}")
    if jit_requested and not policy.jit_provisioning_allowed:
        return SSOEvaluation(False, "jit_provisioning_disabled", jit_allowed=False)
    return SSOEvaluation(True)


@dataclass(frozen=True)
class DomainEvaluation:
    domain: str = ""
    known: bool = False
    verified: bool = False
    requires_sso: bool = False
    blocks_password_login: bool = False
    enforcement: str = "off"
    connection_id: uuid.UUID | None = None


def evaluate_domain_policy(email: str, domains: list) -> DomainEvaluation:
    """What a verified domain says about an address.

    Called by: ``app.auth.service.authenticate`` (password login),
    ``app.auth.email.password_reset`` (reset eligibility) and
    ``app.auth.sso.service`` (connection discovery). Only a *verified* domain
    can impose anything: `enforcement` and `block_password_login` are ignored
    until `verified_at` is set, which is what stops an unverified claim from
    ever refusing a login.
    """
    if "@" not in (email or ""):
        return DomainEvaluation()
    domain = email.rsplit("@", 1)[-1].strip().lower()
    for row in domains:
        if (row.domain or "").lower() != domain:
            continue
        verified = row.verified_at is not None
        enforcement = (row.enforcement or "off").lower()
        return DomainEvaluation(
            domain=domain,
            known=True,
            verified=verified,
            requires_sso=bool(verified and enforcement == "require_sso"),
            blocks_password_login=bool(verified and row.block_password_login),
            enforcement=enforcement if verified else "off",
            connection_id=row.sso_connection_id,
        )
    return DomainEvaluation(domain=domain)


async def load_domain_evaluation(session, email: str) -> DomainEvaluation:
    """Look the address's domain up across *all* tenants, then evaluate it.

    Domain claims are globally unique (enforced by the table), so this cannot
    be ambiguous. A domain that belongs to another tenant is still reported as
    ``known`` here — it has to be, to refuse a cross-tenant login — but the
    connection id is only ever used together with that domain's own tenant.
    """
    from sqlalchemy import select

    from app.auth.identity.models import EnterpriseDomain

    if "@" not in (email or ""):
        return DomainEvaluation()
    domain = email.rsplit("@", 1)[-1].strip().lower()
    row = (
        await session.execute(
            select(EnterpriseDomain).where(EnterpriseDomain.domain == domain)
        )
    ).scalar_one_or_none()
    if row is None:
        return DomainEvaluation(domain=domain)
    return evaluate_domain_policy(email, [row])


# ========================================================= password / reset ===


@dataclass(frozen=True)
class ResetEvaluation:
    allowed: bool
    reason: str = ""


def evaluate_password_reset_policy(
    policy: ResolvedPolicy, *, role: UserRole | None, domain_policy: DomainEvaluation | None
) -> ResetEvaluation:
    """Whether a password reset may be *started* for this principal.

    Called by: ``app.auth.email.password_reset.request_reset``. Refusing here
    is not an enumeration leak: the route answers identically either way (see
    the uniform-response note in that module), so nothing is learned by the
    caller — only the operator sees the reason in the audit trail.
    """
    if not policy.password_login_allowed:
        return ResetEvaluation(False, "password_login_disabled")
    if domain_policy is not None and domain_policy.blocks_password_login:
        return ResetEvaluation(False, "domain_requires_sso")
    if policy.sso_required and (domain_policy is None or domain_policy.requires_sso):
        return ResetEvaluation(False, "sso_required")
    return ResetEvaluation(True)


def role_level_for(role: UserRole | str | None) -> int:
    """Small helper so callers do not each re-parse a role string."""
    if role is None:
        return 0
    if isinstance(role, str):
        try:
            role = UserRole(role)
        except ValueError:
            return 0
    return role_level(role)
