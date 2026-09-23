"""The SCIM provisioning credential: mint, rotate, revoke, authenticate.

Implementation: :mod:`app.auth.identity.scim.service`.

Deliberately *not* a user JWT. A provisioning credential is a separate kind of
thing, and it is treated that way:

* the token is ``vdscim_<prefix>_<secret>``; only the SHA-256 digest of the
  secret is stored, and the plaintext is returned by the call that created it
  and by nothing else — rotation is the only way to obtain a new one;
* it is scoped to one tenant and, optionally, to one SSO connection, so a
  reseller's automation for tenant A cannot read tenant B's directory;
* it carries its own scope list (``scim:users``, ``scim:groups``,
  ``scim:read``), which is what allows a read-only sync job to exist;
* ``authenticate`` answers **uniformly**: an unknown token, a revoked token, an
  expired token, a disabled tenant and a malformed header all produce the same
  401 body. Distinguishing them would tell an attacker which half of a guess was
  right;
* rotation revokes the predecessor in the same transaction, so there is no
  overlap in which both secrets work. Last-use is recorded so an operator can
  tell an active sync from an abandoned one.
"""
from __future__ import annotations

from app.auth.identity.models import SCIMCredential
from app.auth.identity.scim.service import (
    IssuedSCIMCredential,
    SCIMPrincipal,
    authenticate,
    build_scim_token,
    create_credential,
    ensure_enabled,
    get_credential,
    list_credentials,
    revoke_credential,
    rotate_credential,
)

__all__ = [
    "IssuedSCIMCredential",
    "SCIMCredential",
    "SCIMPrincipal",
    "authenticate",
    "build_scim_token",
    "create_credential",
    "ensure_enabled",
    "get_credential",
    "list_credentials",
    "revoke_credential",
    "rotate_credential",
]
