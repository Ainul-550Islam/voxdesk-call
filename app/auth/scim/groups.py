"""``/Groups`` operations.

Implementation: :mod:`app.auth.identity.scim.service`.

A SCIM group is a real thing in this product, not a simulation: it maps an
external group name onto an internal role, and its membership drives that role
for the users in it. So "the IdP put Ada in *Helpdesk Admins*" becomes a role
change through the same ``change_role`` code path the team page uses — one
authorization model, one audit trail.

* ``upsert_group`` — create or replace, keyed by the external group name within
  the tenant. Idempotent: a repeated create with the same name updates the
  membership instead of making a second group;
* ``_sync_group_roles`` — adds the role to members, removes it from
  people who left the group, and never touches a user who is in the group
  through a *different* group's grant;
* ``patch_group`` — RFC 7644 §3.5.2 ``add``/``remove``/``replace`` of members,
  which is how a provider expresses "this person joined" without sending the
  whole group;
* ``delete_group`` — removes the mapping and the role grants it created;
  the users stay, because deprovisioning a *group* is not deprovisioning its
  members.
"""
from __future__ import annotations

from app.auth.identity.scim.service import (
    GroupPayload,
    delete_group,
    get_group,
    list_groups,
    parse_group_payload,
    patch_group,
    upsert_group,
)

__all__ = [
    "GroupPayload",
    "delete_group",
    "get_group",
    "list_groups",
    "parse_group_payload",
    "patch_group",
    "upsert_group",
]
