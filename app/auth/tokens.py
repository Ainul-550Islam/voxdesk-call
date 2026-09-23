"""JWT access tokens, refresh tokens and opaque token primitives.

Two implementations live in this repository and both are reachable from here,
because the identity layer needs both and neither should be re-written:

* :mod:`app.auth.jwt` — the **original** HS256 access-token and refresh-token
  code. It is preserved exactly as it was: the claim set, the signing algorithm,
  the refresh-token hashing and the expiry policy are unchanged, and every
  existing caller (``app.auth.dependencies``, the auth routes, the WebSocket and
  Twilio paths, the integration clients) keeps importing it from there.
* :mod:`app.auth.identity.tokens` — the newer opaque-token primitives the
  enterprise identity surface mints with: API keys, SCIM tokens, service-account
  credentials, password-reset and email-verification tokens, PKCE verifiers and
  single-use recovery codes. Every one of those is a random string that is
  *stored hashed*, so they are generated, hashed and compared here rather than
  in each caller.

This module is the single import site for token work in the identity code. It
adds no behaviour of its own: a re-export that drifted from its source would be
a second implementation, which is exactly what this file exists to prevent.

``app.auth.jwt.TokenError`` is deliberately **not** re-exported: the identity
package raises :class:`app.auth.identity.exceptions.TokenError`, and having one
name mean two exception types in one namespace is how a ``except TokenError``
ends up catching the wrong thing.
"""
from __future__ import annotations

from app.auth.identity.tokens import (
    UNMISTAKABLE,
    constant_time_equals,
    fingerprint,
    generate_recovery_codes,
    hash_token,
    matches_hash,
    new_hex,
    new_human_code,
    new_numeric_code,
    new_pkce_verifier,
    new_secret,
    new_token,
    normalize_recovery_code,
    pkce_challenge,
    random_alnum,
    split_prefix,
    token_hint,
)
from app.auth.jwt import (
    ALGORITHM,
    REFRESH_TOKEN_BYTES,
    TOKEN_TYPE_ACCESS,
    TokenClaims,
    create_access_token,
    decode_access_token,
    generate_refresh_token,
    hash_refresh_token,
    refresh_expiry,
)

__all__ = [
    # app.auth.jwt — the original JWT implementation, unchanged.
    "ALGORITHM",
    "REFRESH_TOKEN_BYTES",
    "TOKEN_TYPE_ACCESS",
    "TokenClaims",
    "create_access_token",
    "decode_access_token",
    "generate_refresh_token",
    "hash_refresh_token",
    "refresh_expiry",
    # app.auth.identity.tokens — opaque, hash-stored credentials.
    "UNMISTAKABLE",
    "constant_time_equals",
    "fingerprint",
    "generate_recovery_codes",
    "hash_token",
    "matches_hash",
    "new_hex",
    "new_human_code",
    "new_numeric_code",
    "new_pkce_verifier",
    "new_secret",
    "new_token",
    "normalize_recovery_code",
    "pkce_challenge",
    "random_alnum",
    "split_prefix",
    "token_hint",
]
