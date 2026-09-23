"""``/Users`` operations.

Implementation: :mod:`app.auth.identity.scim.service`.

Every operation takes the :class:`~app.auth.identity.scim.service.SCIMPrincipal`
resolved from the bearer credential and derives the tenant from it — never from
the URL, never from the body. That is the single line of defence for the
cross-tenant cases ``tests/security/test_identity_scim_*.py`` attacks, and it is
why a resource id that belongs to another tenant answers **404**, not 403:
"forbidden" would confirm that the id exists somewhere.

* ``create_user`` — creates a ``users`` row in the principal's tenant. A
  duplicate ``userName`` is an RFC 7644 conflict (409, ``scimType:
  uniqueness``), not a silent merge, and a retried POST with the same
  ``externalId`` resolves to the same row so an IdP that retries on a timeout
  does not create two accounts;
* ``list_users`` — bound-parameter filtering (the filter grammar is parsed, not
  evaluated) with ``startIndex``/``count`` paging and a server-side ceiling;
* ``replace_user`` (PUT) — full replace of the writable attributes;
* ``set_active`` — ``{"active": false}`` deactivates: the row stays, the
  sessions die, and the credential is refused on the next request;
* ``delete_user`` — deletes the account and its sessions; the audit trail
  survives.
"""
from __future__ import annotations

from app.auth.identity.scim.service import (
    UserPayload,
    UserQuery,
    create_user,
    delete_user,
    get_user,
    list_users,
    parse_user_payload,
    patch_user,
    replace_user,
    set_active,
    user_count,
)

__all__ = [
    "UserPayload",
    "UserQuery",
    "create_user",
    "delete_user",
    "get_user",
    "list_users",
    "parse_user_payload",
    "patch_user",
    "replace_user",
    "set_active",
    "user_count",
]
