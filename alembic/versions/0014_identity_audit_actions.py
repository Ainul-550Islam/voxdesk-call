"""identity audit actions, in the case SQLAlchemy actually writes

A repair migration, and a deliberate one.

``audit_logs.action`` is ``Enum(AuditAction)``, and SQLAlchemy persists the
**member name** for a ``str``-enum: ``AuditAction.LOGIN_SUCCESS`` is stored as
the label ``LOGIN_SUCCESS``. The 30 pre-existing labels in the live
``auditaction`` type are exactly that, which is why ``SELECT DISTINCT action``
on any existing installation reads like the Python enum.

Revision ``0013`` added the 53 identity actions in their lower-case *values*
(``identity_reauthenticated``) instead. Those labels are not wrong in the sense
of being data loss -- they are simply labels nothing can ever write, because
``Enum(AuditAction)`` will ask for ``IDENTITY_REAUTHENTICATED``. The visible
consequence would have been that on PostgreSQL, and only on PostgreSQL, every
identity audit insert raised::

    invalid input value for enum auditaction: "IDENTITY_REAUTHENTICATED"

The test suite would not have caught it: the suite runs on SQLite, where
``Enum`` renders as ``VARCHAR`` plus a ``CHECK`` constraint over the member
names, so the mismatch is invisible until a real deployment writes a row.

This migration therefore:

* adds the 53 identity actions to ``auditaction`` **as member names**, which is
  the form the ORM writes (``ADD VALUE IF NOT EXISTS`` makes it a no-op for
  databases created from the corrected ``0013``);
* adds ``SECURITY_SETTINGS_CHANGED``, the one action the identity surface
  referenced that the vocabulary did not yet have -- emitted by
  ``PATCH /api/identity/policy``;
* leaves the stray lower-case labels in place. PostgreSQL cannot drop an enum
  value, so the alternatives are a type swap (rewriting every ``audit_logs``
  row, on a table that is append-only and the one thing an auditor trusts) or
  leaving 53 unused labels. Unused labels cost nothing and are documented here;
  rewriting audit history is not worth that price. ``0013`` no longer creates
  them, so no new installation ever has them.

Downgrade is intentionally a no-op: there is nothing to undo that leaving alone
does not already achieve.
"""

from __future__ import annotations

from alembic import op

revision = "0014_identity_audit_actions"
down_revision = "0013_enterprise_identity"
branch_labels = None
depends_on = None


#: STEP 18 audit vocabulary, as SQLAlchemy member names. ``0013`` creates the
#: same set on a fresh database; this list is what repairs one that already ran
#: the earlier form. Passing ``security_settings_changed`` in the model's value
#: case is not possible here -- the label has to be the name -- so the member is
#: written in the same upper-case form as its neighbours.
IDENTITY_AUDIT_ACTIONS = (
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
    "SECURITY_SETTINGS_CHANGED",
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite stores the enum as VARCHAR + CHECK built from the model, so
        # there is no type to alter; the model is the single source of truth
        # there and already carries SECURITY_SETTINGS_CHANGED.
        return

    for value in IDENTITY_AUDIT_ACTIONS:
        op.execute(f"ALTER TYPE auditaction ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    # Enum values cannot be removed from a PostgreSQL type. Any row written with
    # one of these labels must keep resolving, so "downgrade" is a no-op rather
    # than a rotation of the type that would rewrite audit history.
    return
