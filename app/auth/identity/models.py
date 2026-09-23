"""Enterprise identity schema (STEP 18).

Every table here is tenant-scoped, cascades from ``tenants``, and is owned by
exactly one identity concept. The rules this module follows, so that the rest
of the identity layer can be read as policy rather than plumbing:

* **No duplicates of existing tables.** ``users``, ``tenants``,
  ``refresh_tokens`` and ``audit_logs`` already exist and are extended rather
  than replaced. There is deliberately no ``IdentityEvent`` table: identity and
  security events are written to the existing ``AuditLog`` with the new
  ``AuditAction`` members, because a second event store would mean two places
  to look during an incident and two retention policies to keep in step.
  There is likewise no ``SCIMProvisioningEvent`` table; SCIM provisioning is
  audited through the same store.
* **Secrets are never stored in the clear.** Anything that is a credential at
  rest — a TOTP seed, an OIDC client secret, an IdP certificate, a PKCE
  verifier — is stored as an AES-256-GCM envelope produced by
  ``app.integrations.crm.crypto`` (the repository's existing cipher) through
  ``app.auth.identity.secrets``. Columns holding a secret are named
  ``*_encrypted`` and carry the key id beside them so a ring can be rotated.
* **Credentials that only need to be *checked* are stored as digests.**
  API-key, service-account, SCIM and recovery-code material is SHA-256 hashed
  for the same reason refresh tokens are: the plaintext is 32+ bytes of CSPRNG
  output, so there is no dictionary to attack and lookup has to stay O(1).
* **Both directions of the wrap are closed.** Every outbound credential links
  to the tenant that owns it, and every inbound assertion is matched through a
  row that is already tenant-scoped, so a callback can never address a
  connection in another tenant.

Import order note: this module imports ``Base`` from ``app.db.models``, and
``app.db.models`` imports *this* module at its very bottom, after ``Base`` and
every pre-existing model are defined. That is the only way to add tables to one
metadata object from two modules without a circular import, and it is why this
file must never import ``User``/``Tenant`` classes — foreign keys are declared
by table name.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    false,
    true,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models import Base


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


def _utcnow() -> datetime:
    return datetime.utcnow()


# ================================================================ vocabulary ===
#
# Stored as plain strings rather than PostgreSQL enum types, matching the
# STEP 17 batches: a closed vocabulary that may grow does not need a type
# migration and an ``ALTER TYPE`` per new member. The constants below are the
# single definition; ``tests/auth`` and ``tests/security`` assert them.


class FactorType(str, enum.Enum):
    TOTP = "totp"


class FactorStatus(str, enum.Enum):
    PENDING = "pending"      # secret generated, not yet proven by the user
    ACTIVE = "active"        # proven; required at login
    DISABLED = "disabled"    # turned off (kept for the audit trail)


class ChallengePurpose(str, enum.Enum):
    LOGIN = "login"
    REAUTHENTICATE = "reauth"
    DISABLE_MFA = "disable_mfa"
    ROTATE_RECOVERY = "rotate_recovery"


class SessionAuthMethod(str, enum.Enum):
    PASSWORD = "password"
    SSO = "sso"


class SSOProtocol(str, enum.Enum):
    OIDC = "oidc"
    SAML = "saml"


class SSOStatus(str, enum.Enum):
    DRAFT = "draft"          # configured, not usable for login yet
    ACTIVE = "active"
    DISABLED = "disabled"    # kept, refused at login, reversible


class CertificateStatus(str, enum.Enum):
    """Where a signing certificate sits in its lifecycle.

    ``PENDING`` is a certificate that has been registered but is not the one the
    IdP is expected to sign with yet -- the first half of a rotation. It is still
    trusted for verification (a provider may switch at any moment, and refusing
    it would make the rotation fail in the direction that locks people out), but
    it is not what makes a connection "ready", and retiring it is always
    allowed.
    """

    PENDING = "pending"
    ACTIVE = "active"
    RETIRED = "retired"      # still trusted during a rotation overlap
    REVOKED = "revoked"


class AttemptOutcome(str, enum.Enum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class MappingOrigin(str, enum.Enum):
    """How an external identity came to be attached to an account.

    ``LINKED`` is distinct from ``JIT`` on purpose: a provisioning report that
    cannot tell "this account exists because the IdP created it on first login"
    from "this account pre-existed and the IdP was attached to it" cannot answer
    the question auditors actually ask.
    """

    JIT = "jit"
    LINKED = "linked"
    SCIM = "scim"
    MANUAL = "manual"


class DomainEnforcement(str, enum.Enum):
    OFF = "off"                    # domain is informational only
    WARN = "warn"                  # log a security event, still allow
    REQUIRE_SSO = "require_sso"    # password login refused for this domain


class VerificationStatus(str, enum.Enum):
    PENDING = "pending"
    VERIFIED = "verified"
    FAILED = "failed"
    EXPIRED = "expired"
    #: Replaced by a freshly issued challenge. Kept as a terminal state so a TXT
    #: record left in a zone cannot be replayed after the challenge is rotated.
    SUPERSEDED = "superseded"


class CredentialState(str, enum.Enum):
    ACTIVE = "active"
    REVOKED = "revoked"


#: SCIM scope names. Deliberately not the same strings as the RBAC vocabulary:
#: a SCIM credential can never be used to call the product API, so it carries
#: provisioning scopes only and nothing else.
SCIM_SCOPE_USERS = "scim:users"
SCIM_SCOPE_GROUPS = "scim:groups"
SCIM_SCOPES = (SCIM_SCOPE_USERS, SCIM_SCOPE_GROUPS)


# ============================================================ user additions ===


class UserEmail(Base):
    """Every address that identifies a user, and whether it is proven.

    ``User.email`` stays the single login address — that is the existing
    contract and changing it would break login. This table records the *proof*
    (``verified_at``) that the existing column never had, and gives SSO/SCIM a
    place to attach the secondary addresses an IdP reports without touching
    ``User.email``.

    The uniqueness is global rather than per tenant: an address that is
    verified for one identity must not be claimable by another, or "verified
    email" would not mean anything across the platform.
    """

    __tablename__ = "user_emails"
    __table_args__ = (
        UniqueConstraint("email", name="uq_user_emails_email"),
        Index("ix_user_emails_user", "user_id", "is_primary"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Where the address came from: signup | sso | scim | admin.
    source: Mapped[str] = mapped_column(String(16), default="signup", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class IdentityPolicy(Base):
    """One row per tenant: the policy the evaluator reads.

    A tenant with no row gets the permissive defaults (see
    ``app.auth.identity.policies``), which are exactly today's behaviour:
    password login allowed, MFA available but not required, SSO optional. A row
    exists only once an administrator has changed something, so enabling this
    feature cannot alter an existing tenant by accident.
    """

    __tablename__ = "identity_policies"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_identity_policies_tenant"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )

    mfa_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    mfa_required_for_admins: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: Privileged actions (SSO changes, credential revocation, MFA policy) may
    #: require a *fresh* second factor even when MFA is not mandatory at login.
    privileged_reauth_required: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )

    sso_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    password_login_allowed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    api_keys_allowed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    service_accounts_allowed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    scim_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    jit_provisioning_allowed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    #: NULL means "inherit the deployment default from settings".
    session_idle_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    session_max_active: Mapped[int | None] = mapped_column(Integer, nullable=True)
    refresh_token_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    privileged_reauth_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: Domain restriction for password login/invites. Empty means no restriction.
    allowed_email_domains: Mapped[list] = mapped_column(JSON, default=list)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


# ==================================================================== sessions ===


class UserSession(Base):
    """A browser/device session: the unit a user can see and revoke.

    The refresh token stays the credential (``refresh_tokens`` is unchanged and
    still single-use with reuse detection); this row is the *sitting*: which
    device, from where, whether it has satisfied MFA, and when it must die.
    Refresh tokens link to it, so revocation can act on either granularity.

    Nothing here is more identifying than it has to be: a user agent string
    capped at 300 characters, an IP as text, and a label the user may set. No
    device fingerprint, no geolocation, no browser history.
    """

    __tablename__ = "user_sessions"
    __table_args__ = (
        Index("ix_user_sessions_user_active", "user_id", "revoked_at"),
        Index("ix_user_sessions_tenant", "tenant_id", "revoked_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    # Deliberately no session secret column. The session is addressed by this
    #: row's id, carried in the access token's signed `sid` claim and copied
    #: onto every refresh token issued for it; there is no second credential to
    #: leak, rotate, or forget to hash. A database dump therefore contains no
    #: value that can be replayed as a session.
    user_agent: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    ip_address: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    device_label: Mapped[str] = mapped_column(String(120), default="", nullable=False)

    auth_method: Mapped[str] = mapped_column(String(16), default="password", nullable=False)
    mfa_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    mfa_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: When the account password was last confirmed on this session (set at a
    #: password login and refreshed by an explicit reauthentication). It is the
    #: fallback proof of presence for privileged actions when the user has no
    #: second factor enrolled -- see `policies.evaluate_privileged_action`.
    password_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Which connection authenticated the session (SSO), when applicable.
    sso_connection_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sso_connections.id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    idle_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_reason: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    def is_live(self, now: datetime) -> bool:
        return (
            self.revoked_at is None
            and self.expires_at > now
            and self.idle_expires_at > now
        )


# ======================================================================= MFA ===


class MFAFactor(Base):
    """A registered second factor.

    TOTP only (RFC 6238), because it is the one factor every enterprise IdP,
    phone and password manager already speaks, and because it needs no outbound
    message on the critical path of a login.

    The seed is the credential: it is stored encrypted with the tenant and
    purpose bound into the AEAD associated data, and the row records which key
    encrypted it so the ring can be rotated. ``last_timestep`` is the replay
    guard — a code accepted once cannot be accepted again, even inside its
    validity window.
    """

    __tablename__ = "mfa_factors"
    __table_args__ = (
        UniqueConstraint("user_id", "factor_type", name="uq_mfa_factors_user_type"),
        Index("ix_mfa_factors_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    factor_type: Mapped[str] = mapped_column(String(16), default="totp", nullable=False)
    label: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)

    #: AES-256-GCM envelope of the base32 seed. Never plaintext, never logged.
    secret_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    secret_key_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    digits: Mapped[int] = mapped_column(Integer, default=6, nullable=False)
    period_seconds: Mapped[int] = mapped_column(Integer, default=30, nullable=False)

    #: Highest TOTP time-step already accepted (replay protection).
    last_timestep: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MFARecoveryCode(Base):
    """Single-use recovery codes, stored only as digests.

    One row per code so consumption is a row-level fact that survives a race
    (the update is conditional on ``used_at IS NULL`` and reports how many rows
    it changed), rather than a counter that two concurrent logins could both
    decrement.
    """

    __tablename__ = "mfa_recovery_codes"
    __table_args__ = (
        UniqueConstraint("code_hash", name="uq_mfa_recovery_codes_hash"),
        Index("ix_mfa_recovery_user_unused", "user_id", "used_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class MFAChallenge(Base):
    """A short-lived second-factor challenge raised in the middle of a login.

    The client receives an opaque challenge token; only its digest is stored,
    for the same reason refresh tokens are hashed. Failure counting and the
    lockout live on this row, so a brute-force attempt against one challenge
    cannot be spread across many and stay invisible.
    """

    __tablename__ = "mfa_challenges"
    __table_args__ = (
        UniqueConstraint("challenge_hash", name="uq_mfa_challenges_hash"),
        Index("ix_mfa_challenges_user_open", "user_id", "consumed_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    challenge_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), default="login", nullable=False)

    failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: Context carried into the session that a successful verification creates.
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user_sessions.id", ondelete="SET NULL"), nullable=True
    )
    ip_address: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    user_agent: Mapped[str] = mapped_column(String(300), default="", nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ======================================================================= SSO ===


class SSOConnection(Base):
    """One tenant-scoped SAML or OIDC connection.

    Configuration, not code: everything an IdP decides differently (claim
    names, group names, role names, whether JIT is allowed, whether a verified
    email may be linked) is a column, so no customer is special-cased in
    ``app/auth/sso``.

    Secrets on this row — the OIDC client secret and the imported IdP metadata
    (which carries certificates) — are stored as envelopes with their key id
    beside them. The login path looks the connection up by
    ``(tenant, slug)`` or by the state issued for that specific connection, so
    a callback cannot be pointed at another tenant's connection.
    """

    __tablename__ = "sso_connections"
    __table_args__ = (
        UniqueConstraint("tenant_id", "slug", name="uq_sso_connections_tenant_slug"),
        Index("ix_sso_connections_tenant_protocol", "tenant_id", "protocol", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    #: URL-safe identifier used in login/ACS URLs. Unique per tenant.
    slug: Mapped[str] = mapped_column(String(60), nullable=False)
    protocol: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="draft", nullable=False)

    # ---- OIDC ----
    issuer: Mapped[str | None] = mapped_column(String(500), nullable=True)
    discovery_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    client_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    client_secret_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    client_secret_key_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scopes: Mapped[str] = mapped_column(String(300), default="openid email profile", nullable=False)
    redirect_uri: Mapped[str | None] = mapped_column(String(500), nullable=True)
    use_pkce: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # ---- SAML ----
    idp_entity_id: Mapped[str | None] = mapped_column(String(500), nullable=True)
    idp_sso_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    idp_slo_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    idp_metadata_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    idp_metadata_key_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sp_entity_id: Mapped[str | None] = mapped_column(String(500), nullable=True)
    acs_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    #: Security policy. When true (the default) an unsigned *assertion* is
    #: refused even if the Response around it was signed.
    require_signed_assertions: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, server_default=true()
    )
    name_id_format: Mapped[str] = mapped_column(String(200), default="", nullable=False)

    # ---- claim / attribute mapping (configuration, never hard-coded) ----
    email_claim: Mapped[str] = mapped_column(String(160), default="email", nullable=False)
    name_claim: Mapped[str] = mapped_column(String(160), default="name", nullable=False)
    subject_claim: Mapped[str] = mapped_column(String(160), default="sub", nullable=False)
    group_claim: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    role_claim: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    default_role: Mapped[str] = mapped_column(String(32), default="viewer", nullable=False)
    #: {"external-group": "internal role"} and {"external-role": "internal role"}.
    group_mapping: Mapped[dict] = mapped_column(JSON, default=dict)
    role_mapping: Mapped[dict] = mapped_column(JSON, default=dict)
    #: When true (default) a role the mapping does not name is refused instead
    #: of silently falling back — a customer's IdP cannot hand out a role the
    #: customer did not choose to grant.
    deny_unmapped_roles: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, server_default=true()
    )

    # ---- provisioning / linking ----
    jit_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, server_default=true()
    )
    allow_account_linking: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    require_verified_email: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, server_default=true()
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    secret_rotated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class SSOConnectionCertificate(Base):
    """A signing certificate trusted for a SAML connection.

    Kept as its own table so a rotation is "add the new certificate, wait for
    the IdP to switch, retire the old one" — an overlap window instead of a
    moment where logins fail. The fingerprint is the identity of the row, so
    re-importing the same certificate is idempotent.
    """

    __tablename__ = "sso_connection_certificates"
    __table_args__ = (
        UniqueConstraint(
            "connection_id", "fingerprint_sha256", name="uq_sso_cert_fingerprint"
        ),
        Index("ix_sso_certs_connection_status", "connection_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sso_connections.id", ondelete="CASCADE"), index=True, nullable=False
    )

    fingerprint_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    subject: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    issuer: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    not_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    pem_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    pem_key_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class SSOLoginAttempt(Base):
    """The state of one in-flight SSO login or link.

    ``state_hash`` is unique, which is what makes the callback safe: the state
    the IdP echoes back identifies exactly one attempt, in exactly one tenant,
    for exactly one purpose, and consuming it is a conditional update — a
    replayed callback finds nothing to consume and is refused.

    Nothing from the assertion is stored here. A failed attempt records a short
    reason code and nothing else, so the table cannot become a store of
    identity data that is harder to purge than the users it describes.
    """

    __tablename__ = "sso_login_attempts"
    __table_args__ = (
        UniqueConstraint("state_hash", name="uq_sso_attempt_state"),
        UniqueConstraint("assertion_id", name="uq_sso_attempt_assertion"),
        Index("ix_sso_attempts_tenant_time", "tenant_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sso_connections.id", ondelete="CASCADE"), index=True, nullable=False
    )

    state_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    nonce_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: SAML: the assertion's ID, unique across the table. A second callback
    #: carrying the same assertion violates the constraint instead of creating
    #: a second session -- replay protection that is enforced by the database
    #: rather than by a check the code could forget.
    assertion_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    relay_state: Mapped[str] = mapped_column(String(300), default="", nullable=False)

    #: PKCE verifier for OIDC, encrypted at rest like every other secret.
    code_verifier_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    code_verifier_key_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    protocol: Mapped[str] = mapped_column(String(16), nullable=False)
    #: login | link — a link attempt is an authenticated user attaching an
    #: external identity to an existing account, and is never a token source.
    kind: Mapped[str] = mapped_column(String(16), default="login", nullable=False)
    #: Set for link attempts (the authenticated user), NULL for logins.
    initiated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    outcome: Mapped[str] = mapped_column(String(24), default="pending", nullable=False)
    failure_reason: Mapped[str] = mapped_column(String(80), default="", nullable=False)

    ip_address: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    user_agent: Mapped[str] = mapped_column(String(300), default="", nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class IdentityMapping(Base):
    """External subject → internal user, per connection.

    The subject is the only identifier an IdP guarantees to be stable; email is
    mutable and is therefore never used as the key. ``external_subject`` is
    unique per connection, and every mapping is tenant-scoped, so two tenants
    can federate the same IdP and the same person without either seeing the
    other's account.
    """

    __tablename__ = "identity_mappings"
    __table_args__ = (
        UniqueConstraint(
            "connection_id", "external_subject", name="uq_identity_mapping_subject"
        ),
        Index("ix_identity_mapping_user", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sso_connections.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    external_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    external_email: Mapped[str] = mapped_column(String(320), default="", nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    external_groups: Mapped[list] = mapped_column(JSON, default=list)
    mapped_role: Mapped[str] = mapped_column(String(32), default="", nullable=False)

    created_via: Mapped[str] = mapped_column(String(16), default="jit", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    linked_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


# ====================================================================== SCIM ===


class SCIMCredential(Base):
    """A tenant/connection-scoped bearer credential for a SCIM client.

    Deliberately its own principal: it is not a user JWT, cannot be minted from
    one, and carries only provisioning scopes. Only the digest is stored, the
    prefix is kept for identification in a UI or a log line, and rotation links
    the replacement to the credential it replaces so the overlap can be audited.
    """

    __tablename__ = "scim_credentials"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_scim_credentials_hash"),
        Index("ix_scim_credentials_tenant_active", "tenant_id", "revoked_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: Optional binding to one connection. NULL means "any connection in this
    #: tenant", which is what most IdPs expect from a tenant-wide SCIM token.
    connection_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sso_connections.id", ondelete="SET NULL"), nullable=True
    )

    label: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    token_prefix: Mapped[str] = mapped_column(String(24), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scopes: Mapped[list] = mapped_column(JSON, default=list)

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    rotated_from_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class SCIMGroupMapping(Base):
    """An external group as SCIM sees it, plus the role it grants.

    Group membership is stored as a list of user ids on the group row because
    SCIM replaces membership wholesale (``PUT``/``PATCH`` with
    ``path: members``); the derived *role* is what the product actually uses.
    """

    __tablename__ = "scim_group_mappings"
    __table_args__ = (
        UniqueConstraint("connection_id", "external_id", name="uq_scim_group_external"),
        Index("ix_scim_groups_tenant", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sso_connections.id", ondelete="CASCADE"), index=True, nullable=False
    )

    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    member_user_ids: Mapped[list] = mapped_column(JSON, default=list)
    #: Role granted to members through the connection's group mapping, if any.
    mapped_role: Mapped[str] = mapped_column(String(32), default="", nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ========================================================== machine identity ===


class ServiceAccount(Base):
    """A machine identity: a principal that is not a human and cannot log in.

    Naming matters here. A service account has no password, no MFA factor and
    no browser session — it holds scoped credentials and nothing else. It is
    never implicitly privileged: its scopes are an explicit subset of the
    creator's permissions, checked at creation time, and it is refused outright
    by endpoints that require a human session.
    """

    __tablename__ = "service_accounts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_service_accounts_tenant_name"),
        Index("ix_service_accounts_tenant_enabled", "tenant_id", "enabled"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    scopes: Mapped[list] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: Emergency stop: set by an operator to kill the identity without deleting
    #: its audit history. ``enabled`` stays as the customer-facing switch.
    emergency_disabled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default=false()
    )

    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disabled_reason: Mapped[str] = mapped_column(String(200), default="", nullable=False)


class ServiceAccountCredential(Base):
    """A credential belonging to a service account (rotatable, revocable)."""

    __tablename__ = "service_account_credentials"
    __table_args__ = (
        UniqueConstraint("secret_hash", name="uq_service_account_credentials_hash"),
        Index("ix_sa_credentials_account_active", "service_account_id", "revoked_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    service_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("service_accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )

    label: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    prefix: Mapped[str] = mapped_column(String(24), nullable=False)
    secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rotated_from_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class APIKey(Base):
    """A scoped machine or user credential stored as a digest.

    Scopes are drawn from the permission vocabulary itself, so there is exactly
    one list to review and a read-only key is *structurally* unable to write:
    it simply does not hold a ``*:write`` permission and ``require_permission``
    refuses on the missing scope.

    ``secret_hash`` is SHA-256 of the plaintext, which is only ever shown once,
    at creation. 32 bytes of CSPRNG output has no dictionary to attack, so a
    plain digest is the right primitive here rather than bcrypt.
    """

    __tablename__ = "api_keys"
    __table_args__ = (
        UniqueConstraint("secret_hash", name="uq_api_keys_hash"),
        Index("ix_api_keys_tenant_active", "tenant_id", "revoked_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: A key belongs either to a human (acting as them, within scope) or to a
    #: service account. Exactly one of the two is set, which is what makes
    #: "human-only" and "machine-only" endpoints expressible.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    service_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("service_accounts.id", ondelete="CASCADE"), nullable=True, index=True
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    prefix: Mapped[str] = mapped_column(String(24), nullable=False)
    secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scopes: Mapped[list] = mapped_column(JSON, default=list)

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_reason: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    rotated_from_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


# =================================================================== domains ===


class EnterpriseDomain(Base):
    """A domain a tenant claims, its proof of ownership, and its policy.

    Claiming a domain is *not* enforcement. ``enforcement`` starts at ``off``
    and only ever changes because an administrator set it, after the domain is
    verified — the two are deliberately separate columns so "we added the
    domain" can never quietly start refusing password logins.

    The domain is globally unique: the same domain cannot be claimed by two
    tenants, or "which tenant does this person belong to" would be ambiguous
    exactly where it matters most (login, and SSO discovery).
    """

    __tablename__ = "enterprise_domains"
    __table_args__ = (
        UniqueConstraint("domain", name="uq_enterprise_domains_domain"),
        Index("ix_enterprise_domains_tenant", "tenant_id", "verified_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    domain: Mapped[str] = mapped_column(String(253), nullable=False)

    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: off | warn | require_sso. Explicit, and refused until the domain is verified.
    enforcement: Mapped[str] = mapped_column(String(16), default="off", nullable=False)
    block_password_login: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default=false()
    )
    sso_connection_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sso_connections.id", ondelete="SET NULL"), nullable=True
    )

    last_evidence_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class DomainVerification(Base):
    """One verification challenge for one domain.

    The token is stored as a digest and shown exactly once, when the challenge
    is created; an operator publishes it as a TXT record. Attempts are counted
    per challenge so a retry loop cannot be used to brute-force a DNS zone or
    to hammer a resolver.
    """

    __tablename__ = "domain_verifications"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_domain_verifications_token"),
        Index("ix_domain_verifications_domain_status", "domain_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    domain_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("enterprise_domains.id", ondelete="CASCADE"), index=True, nullable=False
    )

    method: Mapped[str] = mapped_column(String(16), default="dns_txt", nullable=False)
    record_name: Mapped[str] = mapped_column(String(300), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)

    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str] = mapped_column(String(200), default="", nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


# ======================================================== one-time tokens ===
#
# Password reset and email verification both need the same primitive: a random
# value that is stored as a digest, expires, and can be spent exactly once.
# They get their own tables rather than sharing one with a `purpose` column so
# that a unique constraint on the digest is per family -- a verification token
# can never be presented to the reset endpoint even if the two ever collided,
# and the indexes that matter (user + unused) stay small.


class PasswordResetToken(Base):
    """A single-use password-reset token, stored only as a SHA-256 digest.

    The request context is kept beside it because a reset is a security event:
    knowing which address asked, and from where, is what lets an operator tell
    a forgotten password from an attempt to take over an account. The plaintext
    exists in the email and nowhere else.
    """

    __tablename__ = "password_reset_tokens"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_password_reset_tokens_hash"),
        Index("ix_password_reset_tokens_user_open", "user_id", "used_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_ip: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    requested_user_agent: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    used_ip: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EmailVerificationToken(Base):
    """A single-use proof that a mailbox is reachable and controlled."""

    __tablename__ = "email_verification_tokens"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_email_verification_tokens_hash"),
        Index("ix_email_verification_tokens_user_open", "user_id", "used_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    email: Mapped[str] = mapped_column(String(320), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_ip: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

