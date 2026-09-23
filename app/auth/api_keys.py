"""Scoped API key lifecycle.

The implementation is :mod:`app.auth.identity.api_keys`; this module is the
import site for it (routes: ``app/api/api_key_routes.py``).

A key is ``vdx_<prefix>_<secret>``. Only the SHA-256 digest of the secret is
stored, the plaintext exists in exactly one HTTP response — the one that created
it — and rotation revokes the predecessor in the same transaction, so there is
no window in which two secrets work. ``prefix`` is stored in the clear on
purpose: it is what lets an operator identify a leaked key in a log or a bug
report without the log having to contain the key.

Authentication reads the row on every request (no cache), so a revocation or a
scope change takes effect immediately. ``lookup_credential`` also resolves the
*principal* the key belongs to — a user or a service account — and expires it
when the owner is deactivated, the owning service account is disabled, or an
emergency stop is in force.
"""
from __future__ import annotations

from app.auth.identity.api_keys import (
    LAST_USED_WRITE_INTERVAL,
    CredentialLookup,
    IssuedKey,
    authenticate_credential,
    build_token,
    create_key,
    describe_scopes,
    get_key,
    keys_for_principal,
    known_scope_values,
    list_keys,
    lookup_credential,
    matches_scope,
    principal_from_lookup,
    revoke_key,
    rotate_key,
    scopes_from_permissions,
    service_account_keys,
    split_token,
)

__all__ = [
    "LAST_USED_WRITE_INTERVAL",
    "CredentialLookup",
    "IssuedKey",
    "authenticate_credential",
    "build_token",
    "create_key",
    "describe_scopes",
    "get_key",
    "keys_for_principal",
    "known_scope_values",
    "list_keys",
    "lookup_credential",
    "matches_scope",
    "principal_from_lookup",
    "revoke_key",
    "rotate_key",
    "scopes_from_permissions",
    "service_account_keys",
    "split_token",
]
