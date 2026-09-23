"""Enterprise domains: prove ownership, then decide what it means.

Three modules, three questions:

``verification``  can this tenant prove it owns the domain, and is the proof
                  still fresh? (DNS TXT, a hashed token, a 72-hour window, a
                  bounded retry count)
``policy``        what does a verified domain *do*? (explicit enforcement:
                  ``off`` / ``warn`` / ``require_sso``, plus the password-login
                  block that only a verified domain may set)
``service``       the lifecycle the routes call: add, list, issue a challenge,
                  check it, set enforcement, remove.

The rule that keeps this feature safe is the split between verification and
enforcement: adding a domain, or even verifying it, changes **nothing** about
how anyone signs in. Enforcement is a separate, explicit administrator action,
and it is refused while the domain is unverified — so a typo in a domain name
cannot lock a tenant out of its own workspace.
"""
from __future__ import annotations

from app.auth.domain import policy, service, verification

__all__ = ["policy", "service", "verification"]
