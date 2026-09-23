"""SCIM resource shapes and the rows behind them.

Implementation: :mod:`app.auth.identity.scim.service` (the payloads and the
principal) and :mod:`app.auth.identity.models` (the tables).

SCIM's ``User`` and ``Group`` are **not** new tables. A provisioned user is a
``users`` row — the same row a password login and an SSO login resolve to — and
a SCIM group is a ``scim_group_mappings`` row that maps an external group name
onto a role, plus the members it currently contains. Creating parallel
"SCIM users" would mean an account that can be provisioned but cannot log in,
and two answers to "who is in this tenant?".

What is stored per resource:

* ``SCIMCredential`` — the hashed bearer credential, its prefix, its scope list,
  its optional connection, expiry and last-use;
* ``SCIMGroupMapping`` — external group name → internal role, with the member
  ids and the origin (created by SCIM, or declared by an administrator);
* ``IdentityMapping`` — the external subject/email → internal user mapping that
  keeps provisioning idempotent across renames.
"""
from __future__ import annotations

from app.auth.identity.models import (
    IdentityMapping,
    MappingOrigin,
    SCIMCredential,
    SCIMGroupMapping,
)
from app.auth.identity.scim.service import (
    SCIMPrincipal,
    UserPayload,
    UserQuery,
    parse_group_payload,
    parse_user_payload,
)

__all__ = [
    "IdentityMapping",
    "MappingOrigin",
    "SCIMCredential",
    "SCIMGroupMapping",
    "SCIMPrincipal",
    "UserPayload",
    "UserQuery",
    "parse_group_payload",
    "parse_user_payload",
]
