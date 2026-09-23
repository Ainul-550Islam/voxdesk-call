"""OpenID Connect: the authorization-code flow and ID-token verification.

Implementation: :mod:`app.auth.identity.sso.oidc`.

The flow is the ordinary one — discovery, ``state``, ``nonce``, PKCE when the
connection asks for it, code exchange, then verify the ID token — and every step
that can be skipped by accident is skipped deliberately or not at all:

* ``state`` and ``nonce`` are generated per attempt, stored **hashed**, and
  consumed by a single conditional UPDATE, so a replayed callback finds nothing
  to consume;
* the PKCE verifier is sealed with the tenant-bound key ring rather than kept in
  a cookie or a session table;
* the ID token is verified against the connection's issuer, the connection's
  client id, the ``azp`` claim when more than one audience is present, the
  attempt's nonce, an expiry with a clock-skew allowance, and a signature from
  the provider's JWKS. An unknown ``key id`` triggers exactly one forced JWKS
  refresh, which is what makes provider key rotation survivable without a
  restart;
* ``none`` and symmetric algorithms are refused by construction — the key set
  decides which algorithms are acceptable, and it is fetched over https from a
  host this deployment allows.
"""
from __future__ import annotations

from app.auth.identity.sso.oidc import (
    DiscoveredProvider,
    TokenResponse,
    build_authorization_url,
    discover,
    exchange_code,
    fetch_jwks,
    is_acceptable_https_url,
    new_nonce,
    reset_caches,
    verify_id_token,
)
from app.auth.identity.sso.service import oidc_redirect_uri

__all__ = [
    "DiscoveredProvider",
    "TokenResponse",
    "build_authorization_url",
    "discover",
    "exchange_code",
    "fetch_jwks",
    "is_acceptable_https_url",
    "new_nonce",
    "oidc_redirect_uri",
    "reset_caches",
    "verify_id_token",
]
