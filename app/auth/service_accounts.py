"""Machine identity lifecycle.

The implementation is :mod:`app.auth.identity.service_accounts`; this module is
the import site for it (routes: ``app/api/service_account_routes.py``).

A service account is a machine principal that lives in exactly one tenant and
carries an explicit scope list. It is *never* an administrator: every
``assert_privileged`` action refuses a machine credential outright
(``machine_credential_cannot_reauth``), which is what keeps "the integration
daemon" from becoming "the integration daemon that can mint API keys".

``validate_scopes`` is the interesting function. It answers one question —
*does the person making this request already hold every scope they are asking
to hand out?* — against the actor's own permission set, and it refuses platform
scopes (``platform:*``) for a tenant-scoped credential without consulting the
actor at all. A credential therefore cannot outlive or outrank its creator.
"""
from __future__ import annotations

from app.auth.identity.policies import evaluate_scope_grant
from app.auth.identity.service_accounts import (
    IssuedCredential,
    clear_emergency_disable,
    create_account,
    delete_account,
    get_account,
    get_credential,
    grantable_scopes,
    issue_credential,
    keys_for_account,
    list_accounts,
    list_credentials,
    permission_labels,
    revoke_credential,
    rotate_credential,
    scope_summary,
    set_enabled,
    update_account,
    validate_scopes,
)

__all__ = [
    "IssuedCredential",
    "clear_emergency_disable",
    "create_account",
    "delete_account",
    "evaluate_scope_grant",
    "get_account",
    "get_credential",
    "grantable_scopes",
    "issue_credential",
    "keys_for_account",
    "list_accounts",
    "list_credentials",
    "permission_labels",
    "revoke_credential",
    "rotate_credential",
    "scope_summary",
    "set_enabled",
    "update_account",
    "validate_scopes",
]
