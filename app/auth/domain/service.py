"""The domain lifecycle the routes call.

Implementation: :mod:`app.auth.identity.domains`.

* ``add_domain`` — normalize, validate, refuse a domain another tenant has
  already verified (409, because a shared domain is a shared identity boundary),
  and issue the first challenge;
* ``pending_challenge`` / ``verified_domains`` — the two questions the policy
  engine and the admin UI ask: "is there a proof in flight?" and "which domains
  count as owned *right now*?";
* ``remove_domain`` — removing a domain drops its enforcement with it. The
  removal itself is audited, and a domain that is currently enforcing SSO
  reports that fact so an administrator sees the consequence before it happens;
* ``domain_of`` — the one place an email is split into a domain, so
  ``Ada@Acme.test `` and ``ada@acme.test`` resolve to the same policy.
"""
from __future__ import annotations

from app.auth.identity.domains import (
    EnterpriseDomain,
    domain_of,
    get_domain,
    list_domains,
    pending_challenge,
    remove_domain,
    set_enforcement,
    verified_domains,
)

__all__ = [
    "EnterpriseDomain",
    "domain_of",
    "get_domain",
    "list_domains",
    "pending_challenge",
    "remove_domain",
    "set_enforcement",
    "verified_domains",
]
