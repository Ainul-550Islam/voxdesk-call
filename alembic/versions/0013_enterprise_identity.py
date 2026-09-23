"""enterprise identity: MFA, SSO, SCIM, sessions, machine credentials, domains

STEP 18 adds the identity surface on top of the authentication this product
already had. Nothing existing is replaced: ``users``, ``refresh_tokens``,
``audit_logs`` and the whole RBAC layer stay as they are, and this migration is
purely additive.

Twenty-one tables, every one of them tenant-scoped and cascading from
``tenants``:

* ``user_emails``                  — proof that an address is reachable, and the
                                     global uniqueness that makes "verified"
                                     mean the same thing platform-wide
* ``identity_policies``            — one row per tenant; presence is optional
* ``user_sessions``               — the sitting a user can see and revoke
* ``mfa_factors``, ``mfa_recovery_codes``, ``mfa_challenges``
* ``password_reset_tokens``, ``email_verification_tokens``
* ``sso_connections``, ``sso_connection_certificates``, ``sso_login_attempts``,
  ``identity_mappings``
* ``scim_credentials``, ``scim_group_mappings``
* ``api_keys``, ``service_accounts``, ``service_account_credentials``
* ``enterprise_domains``, ``domain_verifications``

Four additive column changes:

* ``users.email_verified_at`` / ``users.mfa_required`` /
  ``users.password_changed_at`` — all nullable, so every existing row keeps its
  exact current meaning (unverified, "follow the tenant policy", unknown).
* ``refresh_tokens.session_id`` — nullable for the same reason: tokens issued
  before this feature, and tokens issued by callers that never open a session,
  carry no session and keep rotating exactly as they did.
* ``turns.latency_ms`` — a pre-existing gap, not an identity column. The model
  declared it while no migration created it, so a database upgraded from 0012
  would be missing a column the ORM writes to. It is folded in here rather than
  into a migration of its own so that the head revision stays the identity one;
  nullable and defaulted to nothing, so existing rows and readers are unaffected.

Secret handling follows the cipher the repository already had
(``app.integrations.crm.crypto``, AES-256-GCM): TOTP seeds, OIDC client
secrets, IdP certificates and PKCE verifiers are stored as ``*_encrypted``
envelopes with the key id beside them. API-key, service-account, SCIM and
recovery-code material is stored as SHA-256 digests, as refresh tokens already
were. No column in this migration can hold a plaintext credential.

State columns are plain strings, matching 0012: the closed vocabularies live in
``app/auth/identity/models.py`` where the code that writes them can be read
next to them.

Revision ID: 0013_enterprise_identity
Revises: 0012_enterprise_persistence
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_enterprise_identity"
down_revision = "0012_enterprise_persistence"
branch_labels = None
depends_on = None


#: AuditAction members introduced by STEP 18, as the **member names**: SQLAlchemy's
#: ``Enum(AuditAction)`` persists ``str``-enum member *names* (``LOGIN_SUCCESS``),
#: not their values, which is what the 83 pre-existing labels in the live
#: ``auditaction`` type are. Adding these in lower case would have created labels
#: nothing ever writes -- every new identity audit insert would then fail with
#: "invalid input value for enum auditaction". ``tests/test_identity_migrations.py``
#: now pins that parity. Adding members to a PostgreSQL enum
#: is a separate step from creating tables, because ``ALTER TYPE ... ADD VALUE``
#: cannot run inside a transaction block on older PostgreSQL and does not exist
#: on SQLite at all -- the same dialect-guarded pattern 0007/0008/0009 use.
NEW_AUDIT_ACTIONS = (
    "IDENTITY_REAUTHENTICATED",
    "IDENTITY_LINK_REJECTED",
    "PASSWORD_RESET_REQUESTED",
    "PASSWORD_RESET_COMPLETED",
    "EMAIL_VERIFICATION_SENT",
    "EMAIL_VERIFIED",
    "MFA_ENROLLMENT_STARTED",
    "MFA_ENABLED",
    "MFA_DISABLED",
    "MFA_VERIFIED",
    "MFA_FAILED",
    "MFA_CHALLENGE_LOCKED",
    "MFA_RECOVERY_CODE_USED",
    "MFA_RECOVERY_CODES_REGENERATED",
    "SESSION_CREATED",
    "SESSION_REVOKED",
    "SESSION_SUSPICIOUS",
    "SSO_CONNECTION_CREATED",
    "SSO_CONNECTION_UPDATED",
    "SSO_CONNECTION_DELETED",
    "SSO_CONNECTION_ENABLED",
    "SSO_CONNECTION_DISABLED",
    "SSO_MAPPING_CHANGED",
    "SSO_CERTIFICATE_ROTATED",
    "SSO_LOGIN_STARTED",
    "SSO_LOGIN_SUCCEEDED",
    "SSO_LOGIN_FAILED",
    "SSO_USER_PROVISIONED",
    "SSO_ACCOUNT_LINKED",
    "SCIM_CREDENTIAL_CREATED",
    "SCIM_CREDENTIAL_ROTATED",
    "SCIM_CREDENTIAL_REVOKED",
    "SCIM_USER_PROVISIONED",
    "SCIM_USER_UPDATED",
    "SCIM_USER_DEPROVISIONED",
    "SCIM_GROUP_CREATED",
    "SCIM_GROUP_UPDATED",
    "SCIM_GROUP_DELETED",
    "API_KEY_CREATED",
    "API_KEY_ROTATED",
    "API_KEY_REVOKED",
    "SERVICE_ACCOUNT_CREATED",
    "SERVICE_ACCOUNT_UPDATED",
    "SERVICE_ACCOUNT_DISABLED",
    "SERVICE_ACCOUNT_ENABLED",
    "SERVICE_ACCOUNT_CREDENTIAL_CREATED",
    "SERVICE_ACCOUNT_CREDENTIAL_ROTATED",
    "SERVICE_ACCOUNT_CREDENTIAL_REVOKED",
    "DOMAIN_ADDED",
    "DOMAIN_VERIFIED",
    "DOMAIN_VERIFICATION_FAILED",
    "DOMAIN_REMOVED",
    "DOMAIN_ENFORCEMENT_CHANGED",
)


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    if is_postgres:
        for value in NEW_AUDIT_ACTIONS:
            op.execute(f"ALTER TYPE auditaction ADD VALUE IF NOT EXISTS '{value}'")

    # ------------------------------------------- existing tables, additive ---
    op.add_column(
        "users", sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("users", sa.Column("mfa_required", sa.Boolean(), nullable=True))
    op.add_column(
        "users", sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=True)
    )

    op.add_column("refresh_tokens", sa.Column("session_id", sa.UUID(), nullable=True))
    op.create_index(
        op.f("ix_refresh_tokens_session_id"), "refresh_tokens", ["session_id"], unique=False
    )

    # Not an identity column: see the note in the module docstring. Nullable, so
    # the rows already in this table read exactly as they did.
    op.add_column("turns", sa.Column("latency_ms", sa.Float(), nullable=True))

    # ------------------------------------------------------------ new tables ---
    op.create_table(
        "identity_policies",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("mfa_required", sa.Boolean(), nullable=False),
        sa.Column("mfa_required_for_admins", sa.Boolean(), nullable=False),
        sa.Column("privileged_reauth_required", sa.Boolean(), nullable=False),
        sa.Column("sso_required", sa.Boolean(), nullable=False),
        sa.Column("password_login_allowed", sa.Boolean(), nullable=False),
        sa.Column("api_keys_allowed", sa.Boolean(), nullable=False),
        sa.Column("service_accounts_allowed", sa.Boolean(), nullable=False),
        sa.Column("scim_enabled", sa.Boolean(), nullable=False),
        sa.Column("jit_provisioning_allowed", sa.Boolean(), nullable=False),
        sa.Column("session_idle_minutes", sa.Integer(), nullable=True),
        sa.Column("session_max_active", sa.Integer(), nullable=True),
        sa.Column("refresh_token_days", sa.Integer(), nullable=True),
        sa.Column("privileged_reauth_minutes", sa.Integer(), nullable=True),
        sa.Column("allowed_email_domains", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by_user_id", sa.UUID(), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", name="uq_identity_policies_tenant"),
    )
    op.create_index(
        op.f("ix_identity_policies_tenant_id"), "identity_policies", ["tenant_id"], unique=False
    )
    op.create_table(
        "email_verification_tokens",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("requested_ip", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_email_verification_tokens_hash"),
    )
    op.create_index(
        op.f("ix_email_verification_tokens_tenant_id"),
        "email_verification_tokens",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_email_verification_tokens_user_id"),
        "email_verification_tokens",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_email_verification_tokens_user_open",
        "email_verification_tokens",
        ["user_id", "used_at"],
        unique=False,
    )
    op.create_table(
        "mfa_factors",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("factor_type", sa.String(length=16), nullable=False),
        sa.Column("label", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("secret_encrypted", sa.Text(), nullable=False),
        sa.Column("secret_key_id", sa.String(length=64), nullable=False),
        sa.Column("digits", sa.Integer(), nullable=False),
        sa.Column("period_seconds", sa.Integer(), nullable=False),
        sa.Column("last_timestep", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "factor_type", name="uq_mfa_factors_user_type"),
    )
    op.create_index(op.f("ix_mfa_factors_tenant_id"), "mfa_factors", ["tenant_id"], unique=False)
    op.create_index(
        "ix_mfa_factors_tenant_status", "mfa_factors", ["tenant_id", "status"], unique=False
    )
    op.create_index(op.f("ix_mfa_factors_user_id"), "mfa_factors", ["user_id"], unique=False)
    op.create_table(
        "mfa_recovery_codes",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code_hash", name="uq_mfa_recovery_codes_hash"),
    )
    op.create_index(
        op.f("ix_mfa_recovery_codes_tenant_id"), "mfa_recovery_codes", ["tenant_id"], unique=False
    )
    op.create_index(
        op.f("ix_mfa_recovery_codes_user_id"), "mfa_recovery_codes", ["user_id"], unique=False
    )
    op.create_index(
        "ix_mfa_recovery_user_unused", "mfa_recovery_codes", ["user_id", "used_at"], unique=False
    )
    op.create_table(
        "password_reset_tokens",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("requested_ip", sa.String(length=64), nullable=False),
        sa.Column("requested_user_agent", sa.String(length=300), nullable=False),
        sa.Column("used_ip", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_password_reset_tokens_hash"),
    )
    op.create_index(
        op.f("ix_password_reset_tokens_tenant_id"),
        "password_reset_tokens",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_password_reset_tokens_user_id"), "password_reset_tokens", ["user_id"], unique=False
    )
    op.create_index(
        "ix_password_reset_tokens_user_open",
        "password_reset_tokens",
        ["user_id", "used_at"],
        unique=False,
    )
    op.create_table(
        "service_accounts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.String(length=300), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "emergency_disabled", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disabled_reason", sa.String(length=200), nullable=False),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_service_accounts_tenant_name"),
    )
    op.create_index(
        "ix_service_accounts_tenant_enabled",
        "service_accounts",
        ["tenant_id", "enabled"],
        unique=False,
    )
    op.create_index(
        op.f("ix_service_accounts_tenant_id"), "service_accounts", ["tenant_id"], unique=False
    )
    op.create_table(
        "sso_connections",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("slug", sa.String(length=60), nullable=False),
        sa.Column("protocol", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("issuer", sa.String(length=500), nullable=True),
        sa.Column("discovery_url", sa.String(length=500), nullable=True),
        sa.Column("client_id", sa.String(length=255), nullable=True),
        sa.Column("client_secret_encrypted", sa.Text(), nullable=True),
        sa.Column("client_secret_key_id", sa.String(length=64), nullable=True),
        sa.Column("scopes", sa.String(length=300), nullable=False),
        sa.Column("redirect_uri", sa.String(length=500), nullable=True),
        sa.Column("use_pkce", sa.Boolean(), nullable=False),
        sa.Column("idp_entity_id", sa.String(length=500), nullable=True),
        sa.Column("idp_sso_url", sa.String(length=500), nullable=True),
        sa.Column("idp_slo_url", sa.String(length=500), nullable=True),
        sa.Column("idp_metadata_encrypted", sa.Text(), nullable=True),
        sa.Column("idp_metadata_key_id", sa.String(length=64), nullable=True),
        sa.Column("sp_entity_id", sa.String(length=500), nullable=True),
        sa.Column("acs_url", sa.String(length=500), nullable=True),
        sa.Column(
            "require_signed_assertions",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column("name_id_format", sa.String(length=200), nullable=False),
        sa.Column("email_claim", sa.String(length=160), nullable=False),
        sa.Column("name_claim", sa.String(length=160), nullable=False),
        sa.Column("subject_claim", sa.String(length=160), nullable=False),
        sa.Column("group_claim", sa.String(length=160), nullable=False),
        sa.Column("role_claim", sa.String(length=160), nullable=False),
        sa.Column("default_role", sa.String(length=32), nullable=False),
        sa.Column("group_mapping", sa.JSON(), nullable=False),
        sa.Column("role_mapping", sa.JSON(), nullable=False),
        sa.Column(
            "deny_unmapped_roles", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column("jit_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("allow_account_linking", sa.Boolean(), nullable=False),
        sa.Column(
            "require_verified_email", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("secret_rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "slug", name="uq_sso_connections_tenant_slug"),
    )
    op.create_index(
        op.f("ix_sso_connections_tenant_id"), "sso_connections", ["tenant_id"], unique=False
    )
    op.create_index(
        "ix_sso_connections_tenant_protocol",
        "sso_connections",
        ["tenant_id", "protocol", "status"],
        unique=False,
    )
    op.create_table(
        "user_emails",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email", name="uq_user_emails_email"),
    )
    op.create_index(op.f("ix_user_emails_tenant_id"), "user_emails", ["tenant_id"], unique=False)
    op.create_index("ix_user_emails_user", "user_emails", ["user_id", "is_primary"], unique=False)
    op.create_index(op.f("ix_user_emails_user_id"), "user_emails", ["user_id"], unique=False)
    op.create_table(
        "api_keys",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("service_account_id", sa.UUID(), nullable=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("prefix", sa.String(length=24), nullable=False),
        sa.Column("secret_hash", sa.String(length=64), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(length=200), nullable=False),
        sa.Column("rotated_from_id", sa.UUID(), nullable=True),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["service_account_id"], ["service_accounts.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("secret_hash", name="uq_api_keys_hash"),
    )
    op.create_index(
        op.f("ix_api_keys_service_account_id"), "api_keys", ["service_account_id"], unique=False
    )
    op.create_index(
        "ix_api_keys_tenant_active", "api_keys", ["tenant_id", "revoked_at"], unique=False
    )
    op.create_index(op.f("ix_api_keys_tenant_id"), "api_keys", ["tenant_id"], unique=False)
    op.create_index(op.f("ix_api_keys_user_id"), "api_keys", ["user_id"], unique=False)
    op.create_table(
        "enterprise_domains",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("domain", sa.String(length=253), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("enforcement", sa.String(length=16), nullable=False),
        sa.Column(
            "block_password_login", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("sso_connection_id", sa.UUID(), nullable=True),
        sa.Column("last_evidence_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_attempts", sa.Integer(), nullable=False),
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["sso_connection_id"], ["sso_connections.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("domain", name="uq_enterprise_domains_domain"),
    )
    op.create_index(
        "ix_enterprise_domains_tenant",
        "enterprise_domains",
        ["tenant_id", "verified_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_enterprise_domains_tenant_id"), "enterprise_domains", ["tenant_id"], unique=False
    )
    op.create_table(
        "identity_mappings",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("connection_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("external_subject", sa.String(length=255), nullable=False),
        sa.Column("external_email", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("external_groups", sa.JSON(), nullable=False),
        sa.Column("mapped_role", sa.String(length=32), nullable=False),
        sa.Column("created_via", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("linked_by_user_id", sa.UUID(), nullable=True),
        sa.ForeignKeyConstraint(["connection_id"], ["sso_connections.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["linked_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "connection_id", "external_subject", name="uq_identity_mapping_subject"
        ),
    )
    op.create_index("ix_identity_mapping_user", "identity_mappings", ["user_id"], unique=False)
    op.create_index(
        op.f("ix_identity_mappings_connection_id"),
        "identity_mappings",
        ["connection_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_identity_mappings_tenant_id"), "identity_mappings", ["tenant_id"], unique=False
    )
    op.create_index(
        op.f("ix_identity_mappings_user_id"), "identity_mappings", ["user_id"], unique=False
    )
    op.create_table(
        "scim_credentials",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("connection_id", sa.UUID(), nullable=True),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("token_prefix", sa.String(length=24), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by_user_id", sa.UUID(), nullable=True),
        sa.Column("rotated_from_id", sa.UUID(), nullable=True),
        sa.ForeignKeyConstraint(["connection_id"], ["sso_connections.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["revoked_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_scim_credentials_hash"),
    )
    op.create_index(
        "ix_scim_credentials_tenant_active",
        "scim_credentials",
        ["tenant_id", "revoked_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_scim_credentials_tenant_id"), "scim_credentials", ["tenant_id"], unique=False
    )
    op.create_table(
        "scim_group_mappings",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("connection_id", sa.UUID(), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("member_user_ids", sa.JSON(), nullable=False),
        sa.Column("mapped_role", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["connection_id"], ["sso_connections.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("connection_id", "external_id", name="uq_scim_group_external"),
    )
    op.create_index(
        op.f("ix_scim_group_mappings_connection_id"),
        "scim_group_mappings",
        ["connection_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_scim_group_mappings_tenant_id"), "scim_group_mappings", ["tenant_id"], unique=False
    )
    op.create_index("ix_scim_groups_tenant", "scim_group_mappings", ["tenant_id"], unique=False)
    op.create_table(
        "service_account_credentials",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("service_account_id", sa.UUID(), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("prefix", sa.String(length=24), nullable=False),
        sa.Column("secret_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rotated_from_id", sa.UUID(), nullable=True),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["service_account_id"], ["service_accounts.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("secret_hash", name="uq_service_account_credentials_hash"),
    )
    op.create_index(
        "ix_sa_credentials_account_active",
        "service_account_credentials",
        ["service_account_id", "revoked_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_service_account_credentials_service_account_id"),
        "service_account_credentials",
        ["service_account_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_service_account_credentials_tenant_id"),
        "service_account_credentials",
        ["tenant_id"],
        unique=False,
    )
    op.create_table(
        "sso_connection_certificates",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("connection_id", sa.UUID(), nullable=False),
        sa.Column("fingerprint_sha256", sa.String(length=64), nullable=False),
        sa.Column("subject", sa.String(length=300), nullable=False),
        sa.Column("issuer", sa.String(length=300), nullable=False),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=True),
        sa.Column("not_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pem_encrypted", sa.Text(), nullable=False),
        sa.Column("pem_key_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        sa.ForeignKeyConstraint(["connection_id"], ["sso_connections.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("connection_id", "fingerprint_sha256", name="uq_sso_cert_fingerprint"),
    )
    op.create_index(
        "ix_sso_certs_connection_status",
        "sso_connection_certificates",
        ["connection_id", "status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_sso_connection_certificates_connection_id"),
        "sso_connection_certificates",
        ["connection_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_sso_connection_certificates_tenant_id"),
        "sso_connection_certificates",
        ["tenant_id"],
        unique=False,
    )
    op.create_table(
        "sso_login_attempts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("connection_id", sa.UUID(), nullable=False),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("assertion_id", sa.String(length=255), nullable=True),
        sa.Column("nonce_hash", sa.String(length=64), nullable=True),
        sa.Column("relay_state", sa.String(length=300), nullable=False),
        sa.Column("code_verifier_encrypted", sa.Text(), nullable=True),
        sa.Column("code_verifier_key_id", sa.String(length=64), nullable=True),
        sa.Column("protocol", sa.String(length=16), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("initiated_by_user_id", sa.UUID(), nullable=True),
        sa.Column("outcome", sa.String(length=24), nullable=False),
        sa.Column("failure_reason", sa.String(length=80), nullable=False),
        sa.Column("ip_address", sa.String(length=64), nullable=False),
        sa.Column("user_agent", sa.String(length=300), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["connection_id"], ["sso_connections.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["initiated_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("state_hash", name="uq_sso_attempt_state"),
        sa.UniqueConstraint("assertion_id", name="uq_sso_attempt_assertion"),
    )
    op.create_index(
        "ix_sso_attempts_tenant_time",
        "sso_login_attempts",
        ["tenant_id", "created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_sso_login_attempts_connection_id"),
        "sso_login_attempts",
        ["connection_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_sso_login_attempts_tenant_id"), "sso_login_attempts", ["tenant_id"], unique=False
    )
    op.create_table(
        "user_sessions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("user_agent", sa.String(length=300), nullable=False),
        sa.Column("ip_address", sa.String(length=64), nullable=False),
        sa.Column("device_label", sa.String(length=120), nullable=False),
        sa.Column("auth_method", sa.String(length=16), nullable=False),
        sa.Column("mfa_verified", sa.Boolean(), nullable=False),
        sa.Column("mfa_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("password_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sso_connection_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["sso_connection_id"], ["sso_connections.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_user_sessions_tenant", "user_sessions", ["tenant_id", "revoked_at"], unique=False
    )
    op.create_index(
        op.f("ix_user_sessions_tenant_id"), "user_sessions", ["tenant_id"], unique=False
    )
    op.create_index(
        "ix_user_sessions_user_active", "user_sessions", ["user_id", "revoked_at"], unique=False
    )
    op.create_index(op.f("ix_user_sessions_user_id"), "user_sessions", ["user_id"], unique=False)
    op.create_table(
        "domain_verifications",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("domain_id", sa.UUID(), nullable=False),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column("record_name", sa.String(length=300), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["domain_id"], ["enterprise_domains.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_domain_verifications_token"),
    )
    op.create_index(
        op.f("ix_domain_verifications_domain_id"),
        "domain_verifications",
        ["domain_id"],
        unique=False,
    )
    op.create_index(
        "ix_domain_verifications_domain_status",
        "domain_verifications",
        ["domain_id", "status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_domain_verifications_tenant_id"),
        "domain_verifications",
        ["tenant_id"],
        unique=False,
    )
    op.create_table(
        "mfa_challenges",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("challenge_hash", sa.String(length=64), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("session_id", sa.UUID(), nullable=True),
        sa.Column("ip_address", sa.String(length=64), nullable=False),
        sa.Column("user_agent", sa.String(length=300), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["session_id"], ["user_sessions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("challenge_hash", name="uq_mfa_challenges_hash"),
    )
    op.create_index(
        op.f("ix_mfa_challenges_tenant_id"), "mfa_challenges", ["tenant_id"], unique=False
    )
    op.create_index(op.f("ix_mfa_challenges_user_id"), "mfa_challenges", ["user_id"], unique=False)
    op.create_index(
        "ix_mfa_challenges_user_open", "mfa_challenges", ["user_id", "consumed_at"], unique=False
    )
    op.create_foreign_key(
        None, "refresh_tokens", "user_sessions", ["session_id"], ["id"], ondelete="CASCADE"
    )

    # The foreign key is added last: `refresh_tokens` predates this migration,
    # but the table it now points at does not.
    op.create_foreign_key(
        "fk_refresh_tokens_session_id_user_sessions",
        "refresh_tokens",
        "user_sessions",
        ["session_id"],
        ["id"],
        ondelete="CASCADE",
    )

def downgrade() -> None:
    # Enum members are deliberately NOT removed: PostgreSQL cannot remove a
    # value from a type, and if any audit row already recorded one the type
    # would be left referencing a label the enum no longer has. Leaving the
    # extra labels in place is harmless -- the tables that produce them are
    # dropped below.

    # Order matters twice here. `refresh_tokens.session_id` points at
    # `user_sessions`, so the link is detached before the table it points to is
    # dropped; and the identity tables go in reverse order of creation so a
    # dependent row cannot outlive its parent.
    op.drop_constraint(
        "fk_refresh_tokens_session_id_user_sessions", "refresh_tokens", type_="foreignkey"
    )
    op.drop_index(op.f("ix_refresh_tokens_session_id"), table_name="refresh_tokens")
    op.drop_column("refresh_tokens", "session_id")

    op.drop_column("turns", "latency_ms")

    op.drop_table("mfa_challenges")
    op.drop_table("domain_verifications")
    op.drop_table("user_sessions")
    op.drop_table("sso_login_attempts")
    op.drop_table("sso_connection_certificates")
    op.drop_table("service_account_credentials")
    op.drop_table("scim_group_mappings")
    op.drop_table("scim_credentials")
    op.drop_table("identity_mappings")
    op.drop_table("enterprise_domains")
    op.drop_table("api_keys")
    op.drop_table("user_emails")
    op.drop_table("sso_connections")
    op.drop_table("service_accounts")
    op.drop_table("password_reset_tokens")
    op.drop_table("mfa_recovery_codes")
    op.drop_table("mfa_factors")
    op.drop_table("email_verification_tokens")
    op.drop_table("identity_policies")

    op.drop_column("users", "password_changed_at")
    op.drop_column("users", "mfa_required")
    op.drop_column("users", "email_verified_at")
