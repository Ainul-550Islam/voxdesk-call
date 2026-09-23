"""SSO: connection management, login orchestration, and JIT provisioning.

The split of work in this package is deliberate. ``oidc.py`` and ``saml.py``
know the protocols -- discovery, signatures, claim payloads -- and nothing about
this product. This module knows this product -- tenants, users, roles, audit,
policies -- and orchestrates the protocol modules. Every decision that depends
on *our* data (is this user disabled? is this tenant active? may this mapping
grant that role? has this assertion been seen?) is made here, in one place a
reviewer can read top to bottom.

The invariants this module enforces, each with a test in
``tests/security/test_sso.py``:

* **A federated login never resurrects a disabled user or a disabled tenant.**
  Identity providers do not know about our ``is_active`` flag, and an IdP that
  keeps asserting a user we deprovisioned must not be able to let them back in.
* **A federated login never crosses tenants.** An assertion is only ever matched
  against users in the connection's tenant; a matching address in another tenant
  is a refusal, not a merge, and it is audited.
* **Linking requires proof.** An existing user is linked by ``(connection,
  subject)`` from a previous login, or by a *verified* email claim when the
  connection explicitly allows linking. An unverified claim never links.
* **OWNER is never granted, changed or revoked by SSO.** The platform owner role
  is not in the assignable set, so a mapping cannot name it; and a user who *is*
  an owner keeps that role through a federated login.
* **A state is single-use and an assertion id is single-use.** Both are enforced
  by a conditional UPDATE and a unique constraint, not by read-then-write.
* **Every attempt is recorded**, failures included, with a reason code and no
  claim values.
"""
from __future__ import annotations

import re
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import structlog
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import password as passwords
from app.auth.identity import events as identity_events
from app.auth.identity import policies, secrets as identity_secrets
from app.auth.identity import tokens as identity_tokens
from app.auth.identity.exceptions import (
    IdentityError,
    PolicyDenied,
    SSOAccountLinkError,
    SSOConfigurationError,
    SSODisabled,
    SSOReplayDetected,
    SSOStateExpired,
    SSOValidationError,
)
from app.auth.identity.models import (
    CertificateStatus,
    IdentityMapping,
    MappingOrigin,
    SSOConnection,
    SSOConnectionCertificate,
    SSOLoginAttempt,
    SSOProtocol,
    SSOStatus,
    UserEmail,
)
from app.auth.identity.policies import AuthMethod
from app.auth.identity.sso import claims as claim_rules
from app.auth.identity.sso import oidc, saml
from app.core.config import settings
from app.db.models import AuditAction, Tenant, User, UserRole

log = structlog.get_logger()

#: Purposes in the identity key ring. A distinct purpose per secret type means
#: the AAD differs, so a sealed client secret cannot be replayed as a
#: certificate even though both are sealed with the same key ring.
#:
#: Each name is bound to the constant in :mod:`app.auth.identity.secrets`, not to
#: a second string that means the same thing. The reader of a sealed value lives
#: in another module (``oidc`` opens the client secret, ``saml`` opens the
#: certificate), and a purpose that differs by one character is an AAD mismatch:
#: the value opens nowhere, and the failure looks like "not configured".
PURPOSE_CLIENT_SECRET = identity_secrets.PURPOSE_OIDC_CLIENT_SECRET
PURPOSE_PKCE_VERIFIER = identity_secrets.PURPOSE_PKCE_VERIFIER
#: The purpose an IdP signing certificate is sealed with. It *is* the constant in
#: ``identity.secrets`` rather than a second string with the same meaning: the
#: verifier opens certificates with that one, and an AAD that differs by a single
#: character makes every certificate unreadable -- which looks exactly like "no
#: certificate configured" and locks every SAML user out.
PURPOSE_IDP_CERTIFICATE = identity_secrets.PURPOSE_SAML_CERTIFICATE
PURPOSE_SAML_METADATA = identity_secrets.PURPOSE_SAML_METADATA

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,58}[a-z0-9]$")

#: How long a state/nonce stays valid. Short on purpose: the browser already has
#: everything it needs to finish, and a long window is a long replay window.
STATE_TTL = timedelta(minutes=10)

#: How long a retired certificate keeps verifying signatures, so a provider that
#: is mid-rollover does not start failing the moment an administrator retires
#: the old key.
RETIRED_OVERLAP = timedelta(hours=24)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=None) if value.tzinfo else value


def ensure_enabled() -> None:
    if not settings.sso_enabled:
        raise SSODisabled("Single sign-on is disabled on this deployment.")


# ==================================================================== URLs ===


def base_url() -> str:
    return settings.public_base_url.rstrip("/")


def acs_url(connection: SSOConnection) -> str:
    """Where the IdP should POST an assertion — one value, used everywhere.

    A configured value wins, exactly as it does for the OIDC redirect URI: a
    deployment whose public name differs from its internal one configures this,
    the metadata document advertises it, the check that validates a response's
    recipient compares against it, and the AuthnRequest is built with it. All
    four have to agree, so they all call this function.
    """
    configured = (connection.acs_url or "").strip()
    return configured or f"{base_url()}/auth/sso/{connection.slug}/acs"


def oidc_redirect_uri(connection: SSOConnection) -> str:
    """The redirect URI to send to the provider.

    A configured value wins, so a deployment behind a proxy with a different
    public name can still match what the provider has registered; otherwise it
    is derived from ``public_base_url``, which is the value the provider should
    have been given.
    """
    configured = (connection.redirect_uri or "").strip()
    return configured or f"{base_url()}/auth/sso/{connection.slug}/callback"


def slo_url(connection: SSOConnection) -> str:
    return f"{base_url()}/auth/sso/{connection.slug}/slo"


def metadata_url(connection: SSOConnection) -> str:
    return f"{base_url()}/api/sso/connections/{connection.id}/metadata"


def sp_entity_id(connection: SSOConnection) -> str:
    return (connection.sp_entity_id or "").strip() or f"{base_url()}/sso/{connection.slug}"


def request_id_for(attempt: SSOLoginAttempt) -> str:
    """The request id this attempt sent, derived and therefore verifiable.

    Derived from the attempt rather than stored in a second column, so the
    ``InResponseTo`` check cannot drift from what was actually sent: the value
    is a function of the row that produced the AuthnRequest.
    """
    return f"id-{attempt.id}"


# ========================================================== connection admin ===


@dataclass(frozen=True)
class ConnectionInput:
    name: str
    slug: str = ""
    protocol: str = SSOProtocol.OIDC.value
    issuer: str = ""
    discovery_url: str = ""
    client_id: str = ""
    client_secret: str = ""
    scopes: str = "openid email profile"
    use_pkce: bool = True
    redirect_uri: str = ""
    idp_entity_id: str = ""
    idp_sso_url: str = ""
    idp_slo_url: str = ""
    idp_metadata: str = ""
    sp_entity_id: str = ""
    acs_url: str = ""
    require_signed_assertions: bool = True
    name_id_format: str = ""
    email_claim: str = "email"
    name_claim: str = "name"
    subject_claim: str = "sub"
    group_claim: str = ""
    role_claim: str = ""
    default_role: str = "viewer"
    group_mapping: dict | None = None
    role_mapping: dict | None = None
    deny_unmapped_roles: bool = True
    jit_enabled: bool = True
    allow_account_linking: bool = False
    require_verified_email: bool = True


def normalized_slug(raw: str, *, name: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", (raw or name or "").strip().lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)[:60].strip("-")
    if len(slug) < 3:
        slug = f"idp-{identity_tokens.new_hex(6)}"
    if not SLUG_RE.match(slug):
        raise SSOConfigurationError("That connection name cannot be turned into a URL.")
    return slug


def _store_client_secret(connection: SSOConnection, secret: str) -> None:
    envelope, key_id = identity_secrets.encrypt_text(
        secret, tenant_id=str(connection.tenant_id), purpose=PURPOSE_CLIENT_SECRET
    )
    connection.client_secret_encrypted = envelope
    connection.client_secret_key_id = key_id


def _store_metadata(connection: SSOConnection, xml: str) -> None:
    envelope, key_id = identity_secrets.encrypt_text(
        xml, tenant_id=str(connection.tenant_id), purpose=PURPOSE_SAML_METADATA
    )
    connection.idp_metadata_encrypted = envelope
    connection.idp_metadata_key_id = key_id


def _protocol(value: str) -> str:
    try:
        return SSOProtocol(str(value).lower()).value
    except ValueError:
        raise SSOConfigurationError("The protocol must be 'oidc' or 'saml'.") from None


def _apply(connection: SSOConnection, spec: ConnectionInput) -> None:
    """Copy a validated spec onto the row. Secrets and mappings are separate."""
    connection.name = spec.name.strip()[:120]
    connection.protocol = _protocol(spec.protocol)
    connection.issuer = spec.issuer.strip()[:300]
    connection.discovery_url = spec.discovery_url.strip()[:500]
    connection.client_id = spec.client_id.strip()[:300]
    connection.scopes = (spec.scopes or "openid email profile").strip()[:300]
    connection.use_pkce = bool(spec.use_pkce)
    connection.redirect_uri = spec.redirect_uri.strip()[:500]
    connection.idp_entity_id = spec.idp_entity_id.strip()[:500]
    connection.idp_sso_url = spec.idp_sso_url.strip()[:500]
    connection.idp_slo_url = spec.idp_slo_url.strip()[:500]
    connection.sp_entity_id = spec.sp_entity_id.strip()[:500]
    connection.acs_url = spec.acs_url.strip()[:500]
    connection.require_signed_assertions = bool(spec.require_signed_assertions)
    connection.name_id_format = spec.name_id_format.strip()[:200]
    connection.email_claim = (spec.email_claim or "email").strip()[:80]
    connection.name_claim = (spec.name_claim or "name").strip()[:80]
    connection.subject_claim = (spec.subject_claim or "sub").strip()[:80]
    connection.group_claim = spec.group_claim.strip()[:80]
    connection.role_claim = spec.role_claim.strip()[:80]
    connection.default_role = claim_rules.parse_role(spec.default_role).value
    connection.jit_enabled = bool(spec.jit_enabled)
    connection.allow_account_linking = bool(spec.allow_account_linking)
    connection.require_verified_email = bool(spec.require_verified_email)
    connection.deny_unmapped_roles = bool(spec.deny_unmapped_roles)


def _validate_oidc_completeness(connection: SSOConnection) -> None:
    if connection.protocol != SSOProtocol.OIDC.value:
        _validate_saml_endpoints(connection)
        return
    if not connection.discovery_url:
        raise SSOConfigurationError(
            "An OIDC connection needs a discovery URL (the issuer's well-known document)."
        )
    if not connection.client_id:
        raise SSOConfigurationError("An OIDC connection needs a client id.")
    if not oidc.is_acceptable_https_url(connection.discovery_url):
        raise SSOConfigurationError(
            "The discovery URL must be an https URL on a host this deployment allows."
        )
    if connection.redirect_uri and not oidc.is_acceptable_https_url(connection.redirect_uri):
        # The redirect URI is where the authorization code is delivered. An
        # administrator who sets a plaintext one has configured the code to
        # travel in the clear, and the provider's answer (the ID token) with it.
        # It is refused for the same reason every other endpoint is: the
        # deployment's https posture is not a per-connection preference.
        raise SSOConfigurationError(
            "The redirect URI must be an https URL on a host this deployment allows."
        )


def _validate_saml_endpoints(connection: SSOConnection) -> None:
    """Every URL we either call or advertise must pass the https posture rule.

    ``idp_sso_url`` is where the browser is sent; ``acs_url`` is where the
    assertion comes back to. A plaintext value on either leg means the SAML
    response -- the one carrying the user's identity -- crosses the network
    unsignable-in-transit, so it is refused rather than warned about.
    """
    for value, what in (
        (connection.idp_sso_url, "The IdP sign-on URL"),
        (connection.idp_slo_url, "The IdP sign-out URL"),
        (connection.acs_url, "The ACS URL"),
        (connection.idp_entity_id, "The IdP entity id"),
    ):
        text = (value or "").strip()
        if text and text.startswith(("http://", "https://")) and not oidc.is_acceptable_https_url(text):
            raise SSOConfigurationError(
                f"{what} must be an https URL on a host this deployment allows."
            )


async def create_connection(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor: User,
    spec: ConnectionInput,
    commit: bool = False,
) -> SSOConnection:
    """Create a connection. It always starts in ``draft``."""
    ensure_enabled()
    slug = normalized_slug(spec.slug, name=spec.name)
    existing = (
        await session.execute(
            select(SSOConnection).where(
                SSOConnection.tenant_id == tenant_id, SSOConnection.slug == slug
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise SSOConfigurationError(
            "Another connection in this workspace already uses that name."
        )

    connection = SSOConnection(
        tenant_id=tenant_id,
        slug=slug,
        status=SSOStatus.DRAFT.value,
        group_mapping=claim_rules.parse_role_mapping(spec.group_mapping or {}),
        role_mapping=claim_rules.parse_role_mapping(spec.role_mapping or {}),
        created_by_user_id=actor.id,
    )
    session.add(connection)
    _apply(connection, spec)
    _validate_oidc_completeness(connection)
    if spec.client_secret:
        _store_client_secret(connection, spec.client_secret)
    if spec.idp_metadata:
        _store_metadata(connection, spec.idp_metadata)
    await session.flush()

    await identity_events.emit(
        session,
        AuditAction.SSO_CONNECTION_CREATED,
        tenant_id=tenant_id,
        actor_user_id=actor.id,
        actor_email=actor.email,
        detail={
            "connection_id": str(connection.id),
            "protocol": connection.protocol,
            "slug": connection.slug,
            "jit_enabled": connection.jit_enabled,
            "allow_account_linking": connection.allow_account_linking,
        },
        commit=False,
    )
    if commit:
        await session.commit()
    return connection


async def update_connection(
    session: AsyncSession,
    connection: SSOConnection,
    *,
    actor: User,
    spec: ConnectionInput,
    commit: bool = False,
) -> SSOConnection:
    """Apply a new configuration to an existing connection.

    The fields that decide *whether a login can be trusted* -- protocol, issuer,
    entity id, JIT and linking -- are logged before and after, so a support
    engineer can answer "did someone loosen this?" from the audit log alone.
    """
    ensure_enabled()
    # Checked against the stored value before anything is written. Applying the
    # spec and comparing afterwards compares the row with itself: the refusal
    # could never fire, and a protocol swap would take the certificates with it.
    requested_protocol = _protocol(spec.protocol)
    if requested_protocol != connection.protocol:
        raise SSOConfigurationError(
            "A connection's protocol cannot be changed. Create a new connection instead."
        )
    tracked = (
        "name",
        "issuer",
        "discovery_url",
        "client_id",
        "idp_entity_id",
        "idp_sso_url",
        "sp_entity_id",
        "default_role",
        "jit_enabled",
        "allow_account_linking",
        "deny_unmapped_roles",
        "require_signed_assertions",
    )
    before = {key: getattr(connection, key) for key in tracked}

    _apply(connection, spec)
    _validate_oidc_completeness(connection)
    if spec.client_secret:
        _store_client_secret(connection, spec.client_secret)
    if spec.idp_metadata:
        _store_metadata(connection, spec.idp_metadata)

    after = {key: getattr(connection, key) for key in tracked}
    changed = sorted(key for key in tracked if before[key] != after[key])

    await identity_events.emit(
        session,
        AuditAction.SSO_CONNECTION_UPDATED,
        tenant_id=connection.tenant_id,
        actor_user_id=actor.id,
        actor_email=actor.email,
        detail={
            "connection_id": str(connection.id),
            "changes": changed,
            "before": {key: before[key] for key in changed},
            "after": {key: after[key] for key in changed},
            "secret_rotated": bool(spec.client_secret),
        },
        commit=False,
    )
    if commit:
        await session.commit()
    return connection


async def set_status(
    session: AsyncSession,
    connection: SSOConnection,
    *,
    actor: User,
    status: str,
    commit: bool = False,
) -> SSOConnection:
    """Activate, disable or return a connection to draft.

    Activation is gated on the connection being able to complete a login: OIDC
    needs a complete client configuration, SAML needs at least one unexpired
    signing certificate and a sign-on URL. An "active" connection that cannot
    log anyone in is worse than a draft, because it appears on the login page
    and fails.
    """
    ensure_enabled()
    try:
        target = SSOStatus(str(status).lower()).value
    except ValueError:
        raise SSOConfigurationError("The status must be draft, active or disabled.") from None

    if target == SSOStatus.ACTIVE.value:
        if connection.protocol == SSOProtocol.OIDC.value:
            _validate_oidc_completeness(connection)
        else:
            if not await trusted_certificates(session, connection=connection):
                raise SSOConfigurationError(
                    "A SAML connection needs at least one unexpired signing "
                    "certificate before it can be activated."
                )
            if not connection.idp_sso_url:
                raise SSOConfigurationError("A SAML connection needs the IdP sign-on URL.")

    previous = connection.status
    connection.status = target
    await identity_events.emit(
        session,
        {
            SSOStatus.ACTIVE.value: AuditAction.SSO_CONNECTION_ENABLED,
            SSOStatus.DISABLED.value: AuditAction.SSO_CONNECTION_DISABLED,
            SSOStatus.DRAFT.value: AuditAction.SSO_CONNECTION_UPDATED,
        }[target],
        tenant_id=connection.tenant_id,
        actor_user_id=actor.id,
        actor_email=actor.email,
        detail={"connection_id": str(connection.id), "from": previous, "to": target},
        commit=False,
    )
    if commit:
        await session.commit()
    return connection


async def delete_connection(
    session: AsyncSession, connection: SSOConnection, *, actor: User, commit: bool = False
) -> None:
    """Delete a connection and everything hanging off it.

    Certificates, mappings and attempts cascade. The users themselves are
    untouched: deleting a connection must never delete accounts.
    """
    ensure_enabled()
    await identity_events.emit(
        session,
        AuditAction.SSO_CONNECTION_DELETED,
        tenant_id=connection.tenant_id,
        actor_user_id=actor.id,
        actor_email=actor.email,
        detail={
            "connection_id": str(connection.id),
            "slug": connection.slug,
            "protocol": connection.protocol,
            "status": connection.status,
        },
        commit=False,
    )
    await session.delete(connection)
    if commit:
        await session.commit()


async def list_connections(session: AsyncSession, *, tenant_id: uuid.UUID) -> list[SSOConnection]:
    rows = (
        await session.execute(
            select(SSOConnection)
            .where(SSOConnection.tenant_id == tenant_id)
            .order_by(SSOConnection.created_at)
        )
    ).scalars().all()
    return list(rows)


async def get_connection(
    session: AsyncSession, *, tenant_id: uuid.UUID, connection_id: uuid.UUID
) -> SSOConnection | None:
    return (
        await session.execute(
            select(SSOConnection).where(
                SSOConnection.tenant_id == tenant_id, SSOConnection.id == connection_id
            )
        )
    ).scalar_one_or_none()


async def get_connection_by_slug(session: AsyncSession, *, slug: str) -> SSOConnection | None:
    """Resolve the connection a public URL names.

    Slugs are unique per tenant, so a slug shared by two tenants is ambiguous and
    is refused rather than guessed: the request is unauthenticated and there is
    nothing else to disambiguate it with.
    """
    rows = (
        await session.execute(select(SSOConnection).where(SSOConnection.slug == slug).limit(2))
    ).scalars().all()
    if len(rows) != 1:
        if len(rows) > 1:
            log.warning("sso.ambiguous_slug", slug=slug)
        return None
    return rows[0]


# ============================================================== certificates ===


async def add_certificate(
    session: AsyncSession,
    connection: SSOConnection,
    *,
    actor: User,
    material: str,
    make_active: bool = True,
    commit: bool = False,
) -> tuple[SSOConnectionCertificate, bool]:
    """Register (or re-register) an IdP signing certificate.

    Idempotent by fingerprint, so a rotation that uploads last year's
    certificate and this year's does not create duplicate rows. The certificate
    is stored sealed by the identity key ring, never as PEM text in a plain
    column.
    """
    ensure_enabled()
    info = saml.read_certificate(material)

    existing = (
        await session.execute(
            select(SSOConnectionCertificate).where(
                SSOConnectionCertificate.connection_id == connection.id,
                SSOConnectionCertificate.fingerprint_sha256 == info.fingerprint,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if make_active and existing.status != CertificateStatus.ACTIVE.value:
            existing.status = CertificateStatus.ACTIVE.value
            existing.retired_at = None
        if commit:
            await session.commit()
        return existing, False

    envelope, key_id = identity_secrets.encrypt_text(
        info.pem, tenant_id=str(connection.tenant_id), purpose=PURPOSE_IDP_CERTIFICATE
    )
    row = SSOConnectionCertificate(
        tenant_id=connection.tenant_id,
        connection_id=connection.id,
        fingerprint_sha256=info.fingerprint,
        subject=info.subject,
        issuer=info.issuer,
        not_before=_naive(info.not_before),
        not_after=_naive(info.not_after),
        pem_encrypted=envelope,
        pem_key_id=key_id,
        status=CertificateStatus.ACTIVE.value if make_active else CertificateStatus.PENDING.value,
        created_by_user_id=actor.id,
    )
    session.add(row)
    await session.flush()
    await identity_events.emit(
        session,
        AuditAction.SSO_CERTIFICATE_ROTATED,
        tenant_id=connection.tenant_id,
        actor_user_id=actor.id,
        actor_email=actor.email,
        detail={
            "connection_id": str(connection.id),
            "certificate_id": str(row.id),
            "fingerprint": info.fingerprint,
            "not_after": info.not_after.isoformat() if info.not_after else "",
            "operation": "registered",
        },
        commit=False,
    )
    if commit:
        await session.commit()
    return row, True


async def retire_certificate(
    session: AsyncSession,
    connection: SSOConnection,
    row: SSOConnectionCertificate,
    *,
    actor: User,
    commit: bool = False,
) -> SSOConnectionCertificate:
    """Stop trusting a certificate, with an overlap window.

    The row is kept for 24 hours so a provider mid-rollover is not rejected the
    instant an administrator clicks retire; and refusing to retire the *last*
    certificate of an active connection is what stops one click from locking out
    every user of that connection.
    """
    ensure_enabled()
    if row.status == CertificateStatus.ACTIVE.value:
        active = [
            candidate
            for candidate in await list_certificates(session, connection=connection)
            if candidate.status == CertificateStatus.ACTIVE.value
        ]
        if len(active) <= 1 and connection.status == SSOStatus.ACTIVE.value:
            raise SSOConfigurationError(
                "This is the only usable certificate for an active connection. "
                "Add the replacement first, then retire this one."
            )
    row.status = CertificateStatus.RETIRED.value
    row.retired_at = _now()
    await identity_events.emit(
        session,
        AuditAction.SSO_CERTIFICATE_ROTATED,
        tenant_id=connection.tenant_id,
        actor_user_id=actor.id,
        actor_email=actor.email,
        detail={
            "connection_id": str(connection.id),
            "certificate_id": str(row.id),
            "fingerprint": row.fingerprint_sha256,
            "operation": "retired",
        },
        commit=False,
    )
    if commit:
        await session.commit()
    return row


async def list_certificates(
    session: AsyncSession, *, connection: SSOConnection
) -> list[SSOConnectionCertificate]:
    rows = (
        await session.execute(
            select(SSOConnectionCertificate)
            .where(SSOConnectionCertificate.connection_id == connection.id)
            .order_by(SSOConnectionCertificate.created_at.desc())
        )
    ).scalars().all()
    return list(rows)


async def trusted_certificates(
    session: AsyncSession, *, connection: SSOConnection
) -> list[SSOConnectionCertificate]:
    """Certificates that may verify a signature *right now*.

    Expired certificates are excluded rather than trusted with a warning: an
    expired key is a decision someone made, and continuing to accept it means
    whoever holds the old key still passes verification.
    """
    now = _now()
    usable: list[SSOConnectionCertificate] = []
    for row in await list_certificates(session, connection=connection):
        if row.status == CertificateStatus.RETIRED.value:
            retired_at = _naive(row.retired_at)
            if retired_at is None or (now - retired_at) > RETIRED_OVERLAP:
                continue
        elif row.status not in (
            CertificateStatus.ACTIVE.value,
            CertificateStatus.PENDING.value,
        ):
            continue
        not_after = _naive(row.not_after)
        if not_after is not None and not_after <= now:
            continue
        usable.append(row)
    return usable


async def active_certificate_count(session: AsyncSession, *, connection: SSOConnection) -> int:
    return len(await trusted_certificates(session, connection=connection))


async def set_mappings(
    session: AsyncSession,
    connection: SSOConnection,
    *,
    actor: User,
    group_mapping: dict | None = None,
    role_mapping: dict | None = None,
    default_role: str | None = None,
    deny_unmapped_roles: bool | None = None,
    commit: bool = False,
) -> SSOConnection:
    """Replace the claim mappings, validating every target role.

    Validation is not cosmetic: a mapping naming ``owner`` (or a role that does
    not exist) would otherwise be stored happily and fail at login for every
    user of the connection -- or, worse, succeed at granting ownership.
    """
    changes: dict = {}
    if group_mapping is not None:
        connection.group_mapping = claim_rules.parse_role_mapping(group_mapping)
        changes["group_mapping"] = sorted(connection.group_mapping)
    if role_mapping is not None:
        connection.role_mapping = claim_rules.parse_role_mapping(role_mapping)
        changes["role_mapping"] = sorted(connection.role_mapping)
    if default_role is not None:
        connection.default_role = claim_rules.parse_role(default_role).value
        changes["default_role"] = connection.default_role
    if deny_unmapped_roles is not None:
        connection.deny_unmapped_roles = bool(deny_unmapped_roles)
        changes["deny_unmapped_roles"] = connection.deny_unmapped_roles

    await identity_events.emit(
        session,
        AuditAction.SSO_MAPPING_CHANGED,
        tenant_id=connection.tenant_id,
        actor_user_id=actor.id,
        actor_email=actor.email,
        detail={"connection_id": str(connection.id), "changes": changes},
        commit=False,
    )
    if commit:
        await session.commit()
    return connection


def metadata_xml(connection: SSOConnection) -> str:
    """The SP document, for a SAML connection.

    Publishing one for an OIDC connection would advertise an ACS URL that no
    assertion will ever reach, and an administrator reading it would configure
    SAML against a connection that speaks OIDC. The document describes a
    protocol, so it is only produced for a connection that speaks it.
    """
    if connection.protocol != SSOProtocol.SAML.value:
        raise SSOConfigurationError(
            "This connection uses OpenID Connect, so it has no SAML metadata."
        )
    return saml.sp_metadata(
        entity_id=sp_entity_id(connection),
        acs_url=acs_url(connection),
        slo_url=slo_url(connection),
        name=connection.name or "VoxDesk",
    )


# ============================================================ login attempts ===


@dataclass(frozen=True)
class StartResult:
    authorization_url: str
    state: str
    attempt_id: uuid.UUID
    connection: SSOConnection


async def start_login(
    session: AsyncSession,
    *,
    connection: SSOConnection,
    relay_state: str = "",
    ip_address: str = "",
    user_agent: str = "",
    kind: str = "login",
    initiated_by_user_id: uuid.UUID | None = None,
    commit: bool = False,
) -> StartResult:
    """Create the attempt and build the URL the browser should visit.

    Everything secret about the attempt -- the nonce, the PKCE verifier -- stays
    on the attempt row, hashed or sealed; only the state value travels, and only
    ever through the provider.
    """
    ensure_enabled()
    policy = await policies.load_policy(session, connection.tenant_id)
    decision = policies.evaluate_sso_policy(policy, connection_status=connection.status)
    if not decision.allowed:
        raise PolicyDenied(decision.reason)

    state = identity_tokens.new_token("vdsso", 32)
    nonce = oidc.new_nonce()
    envelope = None
    key_id = None
    code_verifier = None
    if connection.protocol == SSOProtocol.OIDC.value and connection.use_pkce:
        code_verifier = identity_tokens.new_pkce_verifier()
        envelope, key_id = identity_secrets.encrypt_text(
            code_verifier,
            tenant_id=str(connection.tenant_id),
            purpose=PURPOSE_PKCE_VERIFIER,
        )

    attempt = SSOLoginAttempt(
        tenant_id=connection.tenant_id,
        connection_id=connection.id,
        state_hash=identity_tokens.hash_token(state),
        nonce_hash=identity_tokens.hash_token(nonce),
        relay_state=(relay_state or "")[:300],
        code_verifier_encrypted=envelope,
        code_verifier_key_id=key_id,
        protocol=connection.protocol,
        kind=kind if kind in ("login", "link") else "login",
        initiated_by_user_id=initiated_by_user_id,
        ip_address=(ip_address or "")[:64],
        user_agent=(user_agent or "")[:300],
        created_at=_now(),
        expires_at=_now() + STATE_TTL,
    )
    session.add(attempt)
    await session.flush()

    if connection.protocol == SSOProtocol.OIDC.value:
        provider = await oidc.discover(connection)
        code_challenge = identity_tokens.pkce_challenge(code_verifier) if code_verifier else None
        url = oidc.build_authorization_url(
            connection,
            provider,
            redirect_uri=oidc_redirect_uri(connection),
            state=state,
            nonce=nonce,
            code_challenge=code_challenge,
        )
    else:
        if not connection.idp_sso_url:
            raise SSOConfigurationError("This SAML connection has no IdP sign-on URL.")
        url = _saml_redirect_url(connection, attempt, state=state)

    await identity_events.emit(
        session,
        AuditAction.SSO_LOGIN_STARTED,
        tenant_id=connection.tenant_id,
        ip_address=ip_address,
        user_agent=(user_agent or "")[:300],
        detail={
            "connection_id": str(connection.id),
            "protocol": connection.protocol,
            "kind": attempt.kind,
            "attempt_id": str(attempt.id),
        },
        commit=False,
    )
    if commit:
        await session.commit()
    return StartResult(
        authorization_url=url, state=state, attempt_id=attempt.id, connection=connection
    )


def _saml_redirect_url(connection: SSOConnection, attempt: SSOLoginAttempt, *, state: str) -> str:
    """The AuthnRequest URL for a SAML login, HTTP-Redirect binding.

    ``build_authn_request`` already produces the binding: it deflates the XML,
    base64-encodes it and form-encodes the result as ``SAMLRequest=…``. The only
    things left for this function are the destination and ``RelayState``.

    The request id inside the XML is derived from the attempt, so the assertion
    that comes back can be checked against this exact attempt. ``RelayState``
    carries the state value the ACS will spend, and is signed nowhere because it
    is only ever compared to a row -- an attacker who changes it invalidates it.
    """
    query = saml.build_authn_request(
        destination=connection.idp_sso_url,
        acs_url=acs_url(connection),
        issuer=sp_entity_id(connection),
        request_id=request_id_for(attempt),
    )
    separator = "&" if "?" in connection.idp_sso_url else "?"
    return f"{connection.idp_sso_url}{separator}{query}&{urlencode({'RelayState': state})}"


async def consume_state(
    session: AsyncSession,
    *,
    state: str,
    protocol: str | None = None,
    kind: str | None = None,
) -> SSOLoginAttempt:
    """Spend a state value, once, atomically.

    A read-then-write would let two concurrent callbacks both find the attempt
    unconsumed and both proceed; a conditional UPDATE means exactly one of them
    sees a row and the loser sees an expired state. That is the whole of the
    CSRF defence for the callback endpoint, so it has to be atomic.
    """
    ensure_enabled()
    if not state:
        raise SSOStateExpired("This sign-in attempt is missing its state.")
    digest = identity_tokens.hash_token(state)
    now = _now()

    updated = await session.execute(
        update(SSOLoginAttempt)
        .where(
            SSOLoginAttempt.state_hash == digest,
            SSOLoginAttempt.consumed_at.is_(None),
            SSOLoginAttempt.expires_at > now,
        )
        .values(consumed_at=now)
        .returning(SSOLoginAttempt.id)
    )
    row = updated.first()
    if row is None:
        raise SSOStateExpired("This sign-in attempt has expired or was already used.")
    attempt = await session.get(SSOLoginAttempt, row[0])
    if attempt is None:  # pragma: no cover - the row was updated in this transaction
        raise SSOStateExpired("This sign-in attempt has expired or was already used.")
    if protocol is not None and attempt.protocol != protocol:
        raise SSOValidationError("This attempt belongs to a different sign-in protocol.")
    if kind is not None and attempt.kind != kind:
        raise SSOValidationError("This attempt was started for a different purpose.")
    return attempt


def _safe_id(row: object) -> str:
    """The id of an ORM row, read *without* triggering a lazy load.

    ``session.rollback()`` expires every instance the session holds: reading any
    attribute afterwards emits a lazy SELECT, which in an async session without a
    greenlet context raises ``MissingGreenlet``. A refusal must never turn into a
    500 because a log line touched an expired object, so ids that might be logged
    after a rollback are read from the instance's already-loaded state.
    """
    try:
        value = vars(row).get("id")
    except TypeError:  # pragma: no cover - an object without a __dict__
        return ""
    return "" if value is None else str(value)


def _require_own_attempt(attempt: SSOLoginAttempt, connection: SSOConnection) -> None:
    """An attempt may only be finished by the connection that started it.

    The state value is what the callback is allowed to prove, and it proves
    exactly one thing: that *this* attempt was started by *this* workspace. It
    says nothing about which connection in that workspace started it, so the
    callback path has to check that separately. Without the check, a state
    issued by connection A could be presented at connection B's callback URL --
    and because the token/assertion is validated against B, the only thing that
    would bind the result to A is a coincidence (the same client id, the same
    signing certificate). Refusing the mismatch is the same rule the rest of the
    module follows: an attempt is answerable only by the request that made it.
    """
    if attempt.connection_id != connection.id:
        raise SSOValidationError("This sign-in attempt belongs to another connection.")


async def record_outcome(
    session: AsyncSession,
    attempt: SSOLoginAttempt,
    *,
    outcome: str,
    reason: str = "",
    commit: bool = False,
) -> None:
    attempt.outcome = outcome[:24]
    attempt.failure_reason = reason[:80]
    if commit:
        await session.commit()


async def _fail_attempt(
    session: AsyncSession,
    attempt: SSOLoginAttempt,
    exc: IdentityError,
    *,
    ref: str = "",
) -> None:
    """Mark an attempt failed, and commit that fact.

    Committing here is the point: the caller is about to raise and the request
    scope will roll back, which would erase the outcome and leave the attempt
    looking permanently in-flight. The state was marked consumed in the same unit
    of work, so this also makes the single-use guarantee durable for a callback
    that is being retried.

    ``ref`` is the attempt's id captured while the row was live. It is needed
    because some failures (a replayed assertion) roll the session back before
    this runs, and an expired instance cannot be read at all -- so the write below
    is attempted, and if it cannot happen the reason is logged against the id the
    caller already has rather than looked up.
    """
    attempt_ref = ref or _safe_id(attempt)
    try:
        await record_outcome(session, attempt, outcome="failed", reason=exc.code, commit=True)
    except Exception:  # noqa: BLE001 - the original failure is what matters
        log.warning("sso.outcome_record_failed", attempt_id=attempt_ref)
        await session.rollback()


async def list_attempts(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID | None = None,
    limit: int = 50,
) -> list[SSOLoginAttempt]:
    """Recent attempts for a tenant, newest first, optionally for one connection.

    Exists for the same reason the audit log does: when a rollout goes wrong,
    "which step failed and why" is the first question, and the answer should not
    require opening the database by hand. The tenant filter is unconditional --
    an attempt id from another workspace returns nothing rather than a row.
    """
    statement = select(SSOLoginAttempt).where(SSOLoginAttempt.tenant_id == tenant_id)
    if connection_id is not None:
        statement = statement.where(SSOLoginAttempt.connection_id == connection_id)
    rows = (
        await session.execute(
            statement.order_by(SSOLoginAttempt.created_at.desc()).limit(
                max(1, min(int(limit), 200))
            )
        )
    ).scalars().all()
    return list(rows)


# ================================================================ completion ===


@dataclass(frozen=True)
class LoginOutcome:
    user: User
    created: bool
    linked: bool
    role_changed: bool
    previous_role: str
    connection: SSOConnection
    auth_method: AuthMethod = AuthMethod.SSO
    warnings: list[str] = field(default_factory=list)


async def _resolve_login(
    session: AsyncSession,
    *,
    connection: SSOConnection,
    normalized: claim_rules.NormalizedClaims,
    attempt: SSOLoginAttempt,
    ip_address: str,
    user_agent: str,
) -> LoginOutcome:
    """Turn validated claims into a user, applying every rule above."""
    tenant = await session.get(Tenant, connection.tenant_id)
    if tenant is None or not tenant.is_active:
        raise SSOValidationError("This workspace is not active.")

    # The workspace's provisioning switch, read here because this is the one
    # place that decides whether an account is created.
    policy = await policies.load_policy(session, connection.tenant_id)

    mapped = await claim_rules.find_mapping(
        session, connection_id=connection.id, subject=normalized.subject
    )
    mapped_user = await session.get(User, mapped.user_id) if mapped else None
    email_user = await claim_rules.find_user_by_email(
        session, tenant_id=connection.tenant_id, email=normalized.email
    )
    cross_tenant = await claim_rules.find_user_anywhere(session, email=normalized.email)

    # Refused before any decision that could link the identity to an account in
    # another workspace. This is the one place a federated login could cross a
    # tenant boundary, so it is refused explicitly and audited.
    if (
        cross_tenant is not None
        and cross_tenant.tenant_id != connection.tenant_id
        and (mapped_user is None or mapped_user.id != cross_tenant.id)
    ):
        await identity_events.emit(
            session,
            AuditAction.IDENTITY_LINK_REJECTED,
            tenant_id=connection.tenant_id,
            ip_address=ip_address,
            detail={
                "connection_id": str(connection.id),
                "reason": "address_belongs_to_another_tenant",
            },
            commit=False,
        )
        raise SSOAccountLinkError(
            "That address belongs to another workspace and cannot be used here."
        )

    try:
        decision = claim_rules.decide_provisioning(
            connection=connection,
            claims=normalized,
            mapped_user=mapped_user,
            email_user=email_user,
            jit_allowed=bool(policy.jit_provisioning_allowed),
        )
    except IdentityError as exc:
        await identity_events.emit(
            session,
            AuditAction.IDENTITY_LINK_REJECTED,
            tenant_id=connection.tenant_id,
            ip_address=ip_address,
            detail={
                "connection_id": str(connection.id),
                "reason": exc.code,
                "operation": "provision",
            },
            commit=False,
        )
        raise

    created = decision.action == "create"
    linked = decision.action == "link"
    user = mapped_user if decision.action == "reuse" else (
        email_user if decision.action == "link" else None
    )

    if user is None:
        user = await _create_jit_user(
            session, connection=connection, normalized=normalized, attempt=attempt
        )
        previous_role = user.role.value
        role_changed = False
    else:
        if not user.is_active:
            # An IdP keeps asserting users we deprovisioned. Accepting the
            # assertion would make "disable this account" a setting an identity
            # provider can undo, so it is refused here and audited.
            await identity_events.emit(
                session,
                AuditAction.SSO_LOGIN_FAILED,
                tenant_id=connection.tenant_id,
                actor_user_id=user.id,
                actor_email=user.email,
                ip_address=ip_address,
                detail={
                    "connection_id": str(connection.id),
                    "reason": "user_disabled",
                },
                commit=False,
            )
            raise SSOValidationError("This account is disabled.")
        if linked:
            await _record_mapping(
                session,
                connection=connection,
                normalized=normalized,
                user=user,
                origin=MappingOrigin.LINKED,
                attempt=attempt,
            )
            await identity_events.emit(
                session,
                AuditAction.SSO_ACCOUNT_LINKED,
                tenant_id=connection.tenant_id,
                actor_user_id=user.id,
                actor_email=user.email,
                ip_address=ip_address,
                detail={
                    "connection_id": str(connection.id),
                    "method": "verified_email",
                    "subject_hint": identity_tokens.hash_token(normalized.subject)[:16],
                },
                commit=False,
            )
        previous_role = user.role.value
        role_changed = await _apply_role(
            session,
            connection=connection,
            normalized=normalized,
            user=user,
            ip_address=ip_address,
        )

    await _remember_email(session, user=user, claim=normalized)
    user.last_login_at = _now()
    connection.last_login_at = _now()
    attempt.outcome = "succeeded"
    attempt.failure_reason = ""

    await identity_events.emit(
        session,
        AuditAction.SSO_LOGIN_SUCCEEDED,
        tenant_id=connection.tenant_id,
        actor_user_id=user.id,
        actor_email=user.email,
        ip_address=ip_address,
        user_agent=(user_agent or "")[:300],
        detail={
            "connection_id": str(connection.id),
            "protocol": connection.protocol,
            "created": created,
            "linked": linked,
            "role": user.role.value,
            "role_changed": role_changed,
            "previous_role": previous_role,
        },
        commit=False,
    )
    return LoginOutcome(
        user=user,
        created=created,
        linked=linked,
        role_changed=role_changed,
        previous_role=previous_role,
        connection=connection,
    )


async def _create_jit_user(
    session: AsyncSession,
    *,
    connection: SSOConnection,
    normalized: claim_rules.NormalizedClaims,
    attempt: SSOLoginAttempt,
) -> User:
    """Provision a user from claims.

    The account is created with a bcrypt hash of a random value nobody holds: a
    provisioned user must not have a guessable or empty password, and this keeps
    the existing ``users.password_hash`` contract intact for every code path that
    assumes a real hash is there. Password login for such a user fails because no
    one knows the password, not because of a special case here.
    """
    role = claim_rules.decide_role(connection, normalized, current_role=None)
    user = User(
        tenant_id=connection.tenant_id,
        email=normalized.email,
        full_name=(normalized.display_name or normalized.email.split("@")[0])[:120],
        password_hash=passwords.hash_password(secrets.token_urlsafe(48)),
        role=role.role,
        is_active=True,
        email_verified_at=_now() if normalized.email_verified else None,
    )
    session.add(user)
    await session.flush()

    await _record_mapping(
        session,
        connection=connection,
        normalized=normalized,
        user=user,
        origin=MappingOrigin.JIT,
        attempt=attempt,
        mapped_role=role.role,
    )
    await identity_events.emit(
        session,
        AuditAction.SSO_USER_PROVISIONED,
        tenant_id=connection.tenant_id,
        actor_user_id=user.id,
        actor_email=user.email,
        detail={
            "connection_id": str(connection.id),
            "role": user.role.value,
            "role_reason": role.reason,
            "username": claim_rules.safe_username(normalized.email),
            "origin": "jit",
        },
        commit=False,
    )
    return user


async def _record_mapping(
    session: AsyncSession,
    *,
    connection: SSOConnection,
    normalized: claim_rules.NormalizedClaims,
    user: User,
    origin: MappingOrigin,
    attempt: SSOLoginAttempt,
    mapped_role: UserRole | None = None,
) -> IdentityMapping:
    """Remember the subject-to-user link this login established."""
    existing = await claim_rules.find_mapping(
        session, connection_id=connection.id, subject=normalized.subject
    )
    if existing is not None:
        existing.last_login_at = _now()
        existing.external_email = normalized.email
        existing.external_groups = list(normalized.groups)
        if mapped_role is not None:
            existing.mapped_role = mapped_role.value
        return existing

    mapping = IdentityMapping(
        tenant_id=connection.tenant_id,
        connection_id=connection.id,
        user_id=user.id,
        external_subject=normalized.subject[:255],
        external_email=normalized.email,
        display_name=(normalized.display_name or "")[:200],
        external_groups=list(normalized.groups),
        mapped_role=(mapped_role or user.role).value,
        created_via=origin.value,
        last_login_at=_now(),
    )
    session.add(mapping)
    await session.flush()
    return mapping


async def _apply_role(
    session: AsyncSession,
    *,
    connection: SSOConnection,
    normalized: claim_rules.NormalizedClaims,
    user: User,
    ip_address: str,
) -> bool:
    """Reconcile the user's role with the connection's mapping.

    An owner is never touched: SSO is not a mechanism for changing who owns the
    workspace, and a mapping that swept an owner down to agent would silently
    hand the next person the top role. Everyone else moves only when the mapping
    produces a definite answer -- an existing user whose groups stop mapping
    keeps the role they have rather than being demoted on the provider's next
    sync (see ``claims.decide_role``).
    """
    if user.role == UserRole.OWNER:
        return False
    decision = claim_rules.decide_role(connection, normalized, current_role=user.role)
    if decision.role is None or decision.role == user.role:
        return False
    previous = user.role.value
    user.role = decision.role
    await identity_events.emit(
        session,
        AuditAction.ROLE_CHANGED,
        tenant_id=connection.tenant_id,
        actor_user_id=user.id,
        target_user_id=user.id,
        actor_email=user.email,
        ip_address=ip_address,
        detail={
            "from": previous,
            "to": decision.role.value,
            "via": "sso_mapping",
            "reason": decision.reason,
            "matched": decision.matched_value,
            "connection_id": str(connection.id),
        },
        commit=False,
    )
    return True


async def _remember_email(
    session: AsyncSession, *, user: User, claim: claim_rules.NormalizedClaims
) -> None:
    """Record the address and its verification state.

    ``user_emails`` is the platform-wide registry, so a verified address is
    verified once and cannot later be claimed by a second identity.
    """
    existing = (
        await session.execute(select(UserEmail).where(UserEmail.email == claim.email))
    ).scalar_one_or_none()
    verified_at = _now() if claim.email_verified else None
    if existing is None:
        session.add(
            UserEmail(
                tenant_id=user.tenant_id,
                user_id=user.id,
                email=claim.email,
                is_primary=claim.email == user.email,
                verified_at=verified_at,
                source="sso",
            )
        )
        return
    if existing.user_id == user.id and existing.verified_at is None and verified_at is not None:
        existing.verified_at = verified_at


async def complete_oidc_login(
    session: AsyncSession,
    *,
    connection: SSOConnection,
    code: str,
    state: str,
    ip_address: str = "",
    user_agent: str = "",
) -> LoginOutcome:
    """Validate the callback and finish the login.

    Order is deliberate: consume the state (single use), verify the ID token
    (nonce bound to this attempt), then touch the user table. A failure before
    the last step cannot have changed anything.
    """
    ensure_enabled()
    if connection.protocol != SSOProtocol.OIDC.value:
        raise SSOConfigurationError("This connection does not use OpenID Connect.")
    attempt = await consume_state(session, state=state, protocol=SSOProtocol.OIDC.value)
    # Read while the row is live: a failure below may roll the session back.
    attempt_ref = _safe_id(attempt)

    try:
        _require_own_attempt(attempt, connection)
        code_verifier = None
        if attempt.code_verifier_encrypted:
            code_verifier = identity_secrets.decrypt_text(
                attempt.code_verifier_encrypted,
                tenant_id=str(connection.tenant_id),
                purpose=PURPOSE_PKCE_VERIFIER,
            )

        provider = await oidc.discover(connection)
        tokens = await oidc.exchange_code(
            connection,
            provider,
            code=code,
            redirect_uri=oidc_redirect_uri(connection),
            code_verifier=code_verifier,
        )
        claims = await oidc.verify_id_token(
            connection,
            provider,
            id_token=tokens.id_token,
            nonce_hash=attempt.nonce_hash or "",
        )

        normalized = claim_rules.normalize_claims(
            connection, claims, default_email_verified=True
        )
        if connection.require_verified_email and not normalized.email_verified:
            raise SSOValidationError("The identity provider did not assert a verified address.")
        outcome = await _resolve_login(
            session,
            connection=connection,
            normalized=normalized,
            attempt=attempt,
            ip_address=ip_address,
            user_agent=user_agent,
        )
    except IdentityError as exc:
        await _fail_attempt(session, attempt, exc, ref=attempt_ref)
        raise
    return outcome


async def complete_saml_login(
    session: AsyncSession,
    *,
    connection: SSOConnection,
    saml_response: bytes,
    relay_state: str = "",
    ip_address: str = "",
    user_agent: str = "",
) -> LoginOutcome:
    """Validate an ACS POST and finish the login."""
    ensure_enabled()
    if connection.protocol != SSOProtocol.SAML.value:
        raise SSOConfigurationError("This connection does not use SAML.")
    if connection.status != SSOStatus.ACTIVE.value:
        raise PolicyDenied(f"connection_{connection.status}")

    attempt = await consume_state(session, state=relay_state, protocol=SSOProtocol.SAML.value)
    # Read while the row is live: the replay path below rolls the session back.
    attempt_ref = _safe_id(attempt)

    try:
        _require_own_attempt(attempt, connection)
        certificates = await trusted_certificates(session, connection=connection)
        # Parsed once. The tree is what signature verification canonicalises, so
        # the same tree is what the structural checks read; parsing the bytes a
        # second time would hand the two steps two different documents.
        root = saml.safe_parse(saml_response)
        parsed = saml.parse_response(root=root)

        saml.validate_assertion(
            parsed,
            connection=connection,
            expected_acs_url=acs_url(connection),
            expected_request_id=request_id_for(attempt),
            expected_entity_id=sp_entity_id(connection),
            certificates=certificates,
        )
        signature = saml.signature_of(
            root, parsed, connection=connection, certificates=certificates
        )
        if signature is not None and signature.used_sha1:
            log.warning(
                "sso.sha1_signature_accepted",
                connection_id=str(connection.id),
                tenant_id=str(connection.tenant_id),
            )

        # Replay protection: the assertion id is unique across the table, so the
        # same assertion arriving twice cannot create a second session.
        attempt.assertion_id = parsed.assertion_id
        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            raise SSOReplayDetected("This SAML assertion has already been used.") from exc

        if connection.name_id_format and parsed.name_id_format not in (
            "",
            connection.name_id_format,
        ):
            raise SSOValidationError(
                "The SAML assertion used an unexpected name-identifier format."
            )

        attributes = dict(parsed.attributes)
        if parsed.name_id and not attributes.get(connection.subject_claim or "sub"):
            attributes[connection.subject_claim or "sub"] = parsed.name_id

        normalized = claim_rules.normalize_claims(
            connection, attributes, default_email_verified=True
        )
        outcome = await _resolve_login(
            session,
            connection=connection,
            normalized=normalized,
            attempt=attempt,
            ip_address=ip_address,
            user_agent=user_agent,
        )
    except IdentityError as exc:
        await _fail_attempt(session, attempt, exc, ref=attempt_ref)
        raise
    return outcome


# ================================================================ discovery ===


def domain_of(email: str) -> str:
    return (email or "").rsplit("@", 1)[-1].strip().lower()


async def connection_for_email(session: AsyncSession, email: str) -> SSOConnection | None:
    """The active connection responsible for an address, if any.

    Only *verified* enterprise domains route anyone anywhere. An unverified
    claim is a name somebody typed, and sending users to an attacker-chosen IdP
    because of a typed name is the classic pre-hijack. If more than one tenant
    has verified the same domain -- which DNS ownership makes all but
    impossible -- the login is refused rather than guessed.
    """
    domain = domain_of(email)
    if not domain:
        return None
    from app.auth.identity import domains as domain_service

    matches: list[SSOConnection] = []
    for row in await domain_service.verified_domains(session, domain=domain):
        if row.sso_connection_id is None:
            continue
        connection = await session.get(SSOConnection, row.sso_connection_id)
        if connection is None or connection.status != SSOStatus.ACTIVE.value:
            continue
        matches.append(connection)
    if len(matches) > 1:
        log.warning("sso.domain_claimed_by_multiple_tenants", domain=domain)
        return None
    return matches[0] if matches else None


__all__ = [
    "ConnectionInput",
    "LoginOutcome",
    "RETIRED_OVERLAP",
    "STATE_TTL",
    "StartResult",
    "acs_url",
    "active_certificate_count",
    "add_certificate",
    "base_url",
    "complete_oidc_login",
    "complete_saml_login",
    "connection_for_email",
    "consume_state",
    "create_connection",
    "delete_connection",
    "domain_of",
    "ensure_enabled",
    "get_connection",
    "get_connection_by_slug",
    "list_attempts",
    "list_certificates",
    "list_connections",
    "metadata_url",
    "metadata_xml",
    "normalized_slug",
    "oidc_redirect_uri",
    "record_outcome",
    "request_id_for",
    "retire_certificate",
    "set_mappings",
    "set_status",
    "slo_url",
    "sp_entity_id",
    "start_login",
    "trusted_certificates",
    "update_connection",
]
