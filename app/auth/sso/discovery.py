"""Discovery: where the provider says its endpoints are, and whether to believe it.

Implementation: :mod:`app.auth.identity.sso.oidc`.

Discovery is the one place where an attacker-supplied URL can move a trust
boundary, so the rules are explicit:

* the discovery URL — and every URL inside the document — must be ``https`` and
  on a host this deployment allows (:func:`is_acceptable_https_url`); the
  ``is_acceptable_https_url`` check runs again on the endpoints after they come
  back, because a document is not a promise;
* the issuer the document declares must equal the issuer the connection
  configured. A document that names a different issuer is the classic
  mix-up set-up: it lets one tenant's connection be answered by another
  tenant's IdP;
* the document must carry a ``jwks_uri`` and at least one usable algorithm, or
  there is nothing to verify a token with;
* the result is cached per connection with a TTL and a ``force`` flag, so key
  rotation and an operator's "test connection" both work without a restart.
"""
from __future__ import annotations

from app.auth.identity.sso.oidc import (
    DiscoveredProvider,
    discover,
    fetch_jwks,
    is_acceptable_https_url,
    reset_caches,
)

__all__ = [
    "DiscoveredProvider",
    "discover",
    "fetch_jwks",
    "is_acceptable_https_url",
    "reset_caches",
]
