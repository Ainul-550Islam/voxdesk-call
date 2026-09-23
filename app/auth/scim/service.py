"""SCIM orchestration: one entry point per thing a provider can ask for.

Implementation: :mod:`app.auth.identity.scim.service`, which this module
surfaces whole — credentials, users, groups, paging, filtering and the audit
events — because a SCIM endpoint that enforces a rule in the route and not in
the service is a rule that the next caller (a bulk sync, a webhook, a test)
will not get.

Notes that matter when reading a caller:

* every function takes the resolved ``SCIMPrincipal`` first and derives the
  tenant from it. There is no ``tenant_id`` parameter to get wrong;
* ``list_users`` compiles the ``filter`` string into a SQL expression against an
  allow-list of attributes with bound parameters. Nothing is ``eval``-ed and
  nothing is string-concatenated into SQL;
* writes emit identity audit events (``SCIM_USER_PROVISIONED``,
  ``SCIM_USER_DEPROVISIONED``, ``SCIM_GROUP_CHANGED``, …) through the same
  ``events.emit`` the rest of the identity surface uses, so a customer's SIEM
  sees provisioning in the same stream as everything else.
"""
from __future__ import annotations

from app.auth.identity.scim.service import (
    GroupPayload,
    IssuedSCIMCredential,
    SCIMPrincipal,
    UserPayload,
    UserQuery,
    authenticate,
    build_scim_token,
    create_credential,
    create_user,
    delete_group,
    delete_user,
    ensure_enabled,
    get_credential,
    get_group,
    get_user,
    list_credentials,
    list_groups,
    list_users,
    parse_group_payload,
    parse_user_payload,
    patch_group,
    patch_user,
    replace_user,
    revoke_credential,
    rotate_credential,
    set_active,
    upsert_group,
    user_count,
)

__all__ = [
    "GroupPayload",
    "IssuedSCIMCredential",
    "SCIMPrincipal",
    "UserPayload",
    "UserQuery",
    "authenticate",
    "build_scim_token",
    "create_credential",
    "create_user",
    "delete_group",
    "delete_user",
    "ensure_enabled",
    "get_credential",
    "get_group",
    "get_user",
    "list_credentials",
    "list_groups",
    "list_users",
    "parse_group_payload",
    "parse_user_payload",
    "patch_group",
    "patch_user",
    "replace_user",
    "revoke_credential",
    "rotate_credential",
    "set_active",
    "upsert_group",
    "user_count",
]
