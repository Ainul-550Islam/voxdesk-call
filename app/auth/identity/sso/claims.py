"""Claim normalization, role mapping, and the provisioning decision.

Everything an IdP calls things differently — ``email`` vs ``mail`` vs
``http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress`` — is
configuration on the connection row, and everything that follows from those
claims is decided here, in functions that take plain data and return a decision.
No provider is special-cased, and no claim is trusted because it looks
plausible.

Three rules, and they are the ones that matter:

* **The subject is the identity; the email is only an address.** Accounts are
  matched by ``(connection, external_subject)``. An IdP that lets a user change
  their address must not thereby hand them someone else's account, and an IdP
  that recycles addresses must not either. Email is used for *provisioning*
  decisions and for the safe-linking check, never as the key.
* **A role comes from the mapping or not at all.** ``roles``/``groups`` from the
  IdP are translated through the connection's mapping; a value the mapping does
  not name is refused when ``deny_unmapped_roles`` is set (the default). An IdP
  administrator who can mint a claim cannot thereby mint an OWNER in this
  product.
* **Nothing is granted quietly.** A login that would change a role records the
  change; a login that would create a user records the creation; a login that
  would *link* to a pre-existing account needs the connection to allow linking
  **and** a verified address.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.identity.exceptions import (
    SSOAccountLinkError,
    SSOValidationError,
)
from app.db.models import User, UserRole

log = structlog.get_logger()

#: Roles a federated login may ever produce. OWNER is deliberately absent: an
#: IdP can hand out administration, but ownership of a tenant is transferred
#: deliberately, by a person, in the product -- not by a group membership.
ASSIGNABLE_VIA_SSO: frozenset[UserRole] = frozenset(
    {UserRole.ADMIN, UserRole.MANAGER, UserRole.AGENT, UserRole.VIEWER}
)

#: Attribute names that hold the same value under different RFCs and vendors.
_EMAIL_ALIASES = (
    "email",
    "mail",
    "emailaddress",
    "email_address",
    "upn",
    "userprincipalname",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/upn",
    "urn:oid:0.9.2342.19200300.100.1.3",
)
_NAME_ALIASES = (
    "name",
    "displayname",
    "display_name",
    "full_name",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name",
    "urn:oid:2.16.840.1.113730.3.1.241",
)
_SUBJECT_ALIASES = (
    "sub",
    "subject",
    "nameid",
    "name_id",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/nameidentifier",
    "urn:oasis:names:tc:SAML:2.0:nameid-format:persistent",
)
_GROUP_ALIASES = (
    "groups",
    "group",
    "memberof",
    "roles",
    "http://schemas.microsoft.com/ws/2008/06/identity/claims/groups",
    "http://schemas.xmlsoap.org/claims/Group",
)
_VERIFIED_ALIASES = (
    "email_verified",
    "emailverified",
    "verified_email",
    "verifiedemail",
)


@dataclass(frozen=True)
class NormalizedClaims:
    """The five things the identity layer needs, whatever the IdP called them."""

    subject: str
    email: str
    email_verified: bool
    display_name: str
    groups: list[str] = field(default_factory=list)
    #: Values of the connection's configured ``role_claim``, normalised to a
    #: list. Empty when the connection has no role claim.
    role_values: list[str] = field(default_factory=list)
    raw_claim_names: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.subject:
            raise SSOValidationError("The identity provider did not assert a subject.")
        if not self.email:
            raise SSOValidationError("The identity provider did not assert an email address.")


def _pick(claims: dict, *names: str) -> str:
    for name in names:
        if name in claims and claims[name] not in (None, "", [], {}):
            value = claims[name]
            if isinstance(value, list):
                return str(value[0]) if value else ""
            return str(value)
    return ""


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value if v not in (None, "")]
    return [str(value)]


def _as_bool(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def normalize_claims(connection, claims: dict, *, default_email_verified: bool) -> NormalizedClaims:
    """Fold an IdP's claims into the shape the rest of the code uses.

    The connection's configured claim names win; the alias lists are the
    fallback for the common cases, so a new connection works before anyone has
    tuned it while a tuned connection is never second-guessed.
    """
    claims = claims or {}
    subject = _pick(claims, connection.subject_claim, *_SUBJECT_ALIASES)
    email = _pick(claims, connection.email_claim, *_EMAIL_ALIASES).strip().lower()
    name = _pick(claims, connection.name_claim, *_NAME_ALIASES)

    groups: list[str] = []
    if connection.group_claim:
        groups = _as_list(claims.get(connection.group_claim))
    if not groups:
        for alias in _GROUP_ALIASES:
            if alias in claims:
                groups = _as_list(claims[alias])
                break

    role_values: list[str] = []
    if connection.role_claim:
        role_values = _as_list(claims.get(connection.role_claim))

    verified = default_email_verified
    for alias in _VERIFIED_ALIASES:
        if alias in claims:
            verified = _as_bool(claims[alias], default_email_verified)
            break

    return NormalizedClaims(
        subject=subject,
        email=email,
        email_verified=verified,
        display_name=name or email.split("@", 1)[0],
        groups=groups,
        role_values=role_values,
        raw_claim_names=sorted(claims.keys()),
    )


@dataclass(frozen=True)
class RoleDecision:
    role: UserRole
    reason: str
    matched_value: str = ""


def decide_role(connection, claims: NormalizedClaims, *, current_role: UserRole | None) -> RoleDecision:
    """Translate claims into a role, or refuse.

    Precedence is explicit and additive: an explicit role claim through the role
    mapping, then group membership through the group mapping, then the
    connection's ``default_role``. A mapped value that names a role outside the
    assignable set is refused rather than clamped: silently downgrading OWNER
    to ADMIN would leave an operator believing their mapping worked.
    """
    role_map = dict(getattr(connection, "role_mapping", None) or {})
    group_map = dict(getattr(connection, "group_mapping", None) or {})

    matched_value = ""
    target: str | None = None

    # 1) An explicit role claim, translated through the role mapping.
    for value in claims.role_values:
        if value in role_map:
            target = role_map[value]
            matched_value = value
            break

    # 2) Group membership: the role mapping is consulted first so an operator
    #    can override a group whose name is also an external role name.
    for group in ([] if target is not None else claims.groups):
        if group in role_map:
            target = role_map[group]
            matched_value = group
            break
        if group in group_map:
            target = group_map[group]
            matched_value = group
            break

    if target is None:
        if current_role is not None and not connection.deny_unmapped_roles:
            return RoleDecision(current_role, "kept_existing_role")
        if current_role is not None and connection.deny_unmapped_roles:
            # An existing user whose groups no longer map keeps the role they
            # have -- refusing the login outright would take away access to
            # everything for a mapping the operator is still tuning.
            return RoleDecision(current_role, "no_mapping_kept_role")
        if not connection.deny_unmapped_roles:
            return RoleDecision(_parse_role(connection.default_role), "default_role")
        raise SSOValidationError(
            "Your groups do not map to a role in this workspace. "
            "Ask an administrator to update the SSO mapping."
        )

    return RoleDecision(_parse_role(target), "mapped", matched_value=matched_value)


def _parse_role(value: str) -> UserRole:
    try:
        role = UserRole(str(value).strip().lower())
    except ValueError:
        raise SSOValidationError(
            f"The SSO mapping names a role that does not exist: {value!r}"
        ) from None
    if role not in ASSIGNABLE_VIA_SSO:
        raise SSOValidationError(
            f"SSO cannot grant the {role.value} role. Ownership is transferred "
            "inside the product, not by a claim."
        )
    return role


def parse_role(raw: str) -> UserRole:
    """Public spelling of the role parser used by mapping validation.

    ``set_mappings`` needs to validate an administrator-entered default role and
    refused to import the private helper to do it; one public name is the fix.
    """
    return _parse_role(raw)


def parse_role_mapping(mapping: dict) -> dict[str, str]:
    """Validate an admin-supplied ``{external: internal}`` mapping."""
    if not isinstance(mapping, dict):
        raise SSOValidationError("A mapping must be an object of external-to-internal values.")
    cleaned: dict[str, str] = {}
    for external, internal in mapping.items():
        key = str(external).strip()
        if not key:
            continue
        role = _parse_role(str(internal))
        cleaned[key] = role.value
    return cleaned


@dataclass(frozen=True)
class ProvisioningDecision:
    """What a completed federated login should do to the user table."""

    action: str  # "reuse" | "create" | "link"
    user_id: uuid.UUID | None = None
    reason: str = ""


async def find_mapping(
    session: AsyncSession, *, connection_id: uuid.UUID, subject: str
):
    from app.auth.identity.models import IdentityMapping

    return (
        await session.execute(
            select(IdentityMapping).where(
                IdentityMapping.connection_id == connection_id,
                IdentityMapping.external_subject == subject,
            )
        )
    ).scalar_one_or_none()


async def find_user_by_email(
    session: AsyncSession, *, tenant_id: uuid.UUID, email: str
) -> User | None:
    """A user with this address **in this tenant**.

    The tenant filter is not decoration: without it, an IdP assertion for
    ``ceo@acme.com`` would find a user in a different tenant and the next
    branch would decide about linking them.
    """
    return (
        await session.execute(
            select(User).where(User.email == email, User.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()


async def find_user_anywhere(session: AsyncSession, *, email: str) -> User | None:
    """A user with this address in *any* tenant.

    Used only to detect a cross-tenant collision, which is refused rather than
    resolved: the same person federating into two workspaces gets two accounts
    with two addresses, not one account quietly moved.
    """
    return (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()


def decide_provisioning(
    *,
    connection,
    claims: NormalizedClaims,
    mapped_user: User | None,
    email_user: User | None,
    jit_allowed: bool = True,
) -> ProvisioningDecision:
    """Whether to reuse, create, or link — and whether any of it is allowed.

    Called by ``service.complete_login`` before it touches the user table.

    Two independent switches govern automatic provisioning and both must be on:
    ``connection.jit_enabled`` (this connection may create accounts) and
    ``jit_allowed`` (the tenant's ``identity_policies.jit_provisioning_allowed``).
    They are checked separately rather than folded together so the refusal says
    which one is off — an administrator who turned the tenant switch off should
    not be sent looking at the connection.
    """
    if mapped_user is not None:
        return ProvisioningDecision("reuse", mapped_user.id, "subject_mapping")

    if email_user is not None:
        if not connection.allow_account_linking:
            # The reason code is carried on the exception rather than left to
            # the class: the HTTP response is deliberately flat (an SSO failure
            # must not tell a stranger which address exists here), so the audit
            # row is the only place that can say *which* switch refused.
            raise SSOAccountLinkError(
                "An account already exists for this address. Ask an administrator "
                "to enable account linking for this connection, or sign in with "
                "your password.",
                code="sso_account_linking_disabled",
            )
        if connection.require_verified_email and not claims.email_verified:
            raise SSOAccountLinkError(
                "The identity provider did not assert a verified address, so the "
                "existing account cannot be linked.",
                code="sso_link_requires_verified_email",
            )
        return ProvisioningDecision("link", email_user.id, "verified_email_match")

    if not connection.jit_enabled:
        raise SSOAccountLinkError(
            "No account exists for this identity and automatic provisioning is "
            "disabled. Ask an administrator to create your account.",
            code="sso_provisioning_disabled_on_connection",
        )
    if not jit_allowed:
        raise SSOAccountLinkError(
            "No account exists for this identity and automatic provisioning is "
            "switched off for this workspace.",
            code="sso_provisioning_disabled_for_workspace",
        )
    if connection.require_verified_email and not claims.email_verified:
        raise SSOValidationError(
            "The identity provider did not assert a verified email address."
        )
    return ProvisioningDecision("create", None, "jit")


def safe_username(email: str) -> str:
    """The local part of an address, truncated for ``users.full_name``."""
    return (email.split("@", 1)[0] or "user")[:120]
