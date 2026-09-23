"""refused machine credentials, as an audit action

One new label: ``CREDENTIAL_AUTH_REJECTED``.

``principal_from_lookup`` and ``lookup_credential`` now write it whenever a
machine token *resolved to a row* and was then refused -- revoked, expired, its
service account switched off, its credential family disabled for the workspace,
or its owner suspended. An unknown or malformed token is deliberately not
recorded, so the trail names a credential the operator can look up rather than
being an unauthenticated log amplifier.

The label has to be added the way ``Enum(AuditAction)`` writes it: the member
*name*, upper case. This is the same trap ``0014`` documented -- SQLite renders
the enum as ``VARCHAR`` plus a ``CHECK`` built from the model, so a missing
PostgreSQL label is invisible to the test suite and would only surface on a real
deployment, as::

    invalid input value for enum auditaction: "CREDENTIAL_AUTH_REJECTED"

Downgrade is a no-op for the same reason as ``0014``: PostgreSQL cannot remove
an enum value, and any row already written with this label must keep resolving.
"""

from __future__ import annotations

from alembic import op

revision = "0015_credential_auth_rejected"
down_revision = "0014_identity_audit_actions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite stores the action as VARCHAR + CHECK generated from the model,
        # which already carries the member; there is no type to alter.
        return
    op.execute("ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'CREDENTIAL_AUTH_REJECTED'")


def downgrade() -> None:
    # Enum values cannot be removed from a PostgreSQL type without rotating it,
    # which would rewrite an append-only audit table. Leaving an unused label is
    # the cheaper of the two.
    return
