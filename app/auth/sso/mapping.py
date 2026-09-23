"""Subject, email and group mapping — and the rules for creating a user.

Implementation: :mod:`app.auth.identity.sso.claims` (decisions) and
:mod:`app.auth.identity.sso.service` (persistence of the mappings).

The order a federated login resolves an identity in, and why:

1. ``identity_mappings`` for (connection, external subject) — the stable key.
   A person who changes their email keeps their account.
2. ``identity_mappings`` for (connection, email) — covers an IdP that changes
   the subject when a directory is migrated.
3. an existing user with that verified email, **only** when the connection sets
   ``allow_account_linking``. This is the dangerous step: without the flag,
   an IdP that asserts an address you control would otherwise hand over an
   account that was created with a password.
4. just-in-time creation, **only** when the connection sets ``jit_enabled``,
   into the connection's own tenant, with the mapped or default role.

Two refusals are absolute and audited:

* an address that belongs to a user in **another tenant** is never linked and
  never provisioned — that is the cross-tenant case, and it answers
  ``IDENTITY_LINK_REJECTED`` with ``address_belongs_to_another_tenant``;
* a deactivated user stays deactivated. A successful login at the IdP does not
  reactivate an account an administrator turned off.
"""
from __future__ import annotations

from app.auth.identity.models import IdentityMapping, MappingOrigin
from app.auth.identity.sso.claims import (
    ProvisioningDecision,
    decide_provisioning,
    find_mapping,
    find_user_anywhere,
    find_user_by_email,
    safe_username,
)
from app.auth.identity.sso.service import set_mappings

__all__ = [
    "IdentityMapping",
    "MappingOrigin",
    "ProvisioningDecision",
    "decide_provisioning",
    "find_mapping",
    "find_user_anywhere",
    "find_user_by_email",
    "safe_username",
    "set_mappings",
]
