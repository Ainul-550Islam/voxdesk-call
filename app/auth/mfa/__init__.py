"""Second-factor authentication.

Five named concerns, one implementation each — nothing in this package defines
behaviour of its own, so there is no way for the route layer, the tests and the
login path to drift apart:

``totp``            RFC 6238 / RFC 4226 arithmetic and the provisioning URI
``recovery_codes``  the one-time codes, and the audit event when one is spent
``challenges``      the short-lived, failure-counted login/step-up challenge
``policy``          whether *this* user, in *this* tenant, must present a factor
``exceptions``      the typed errors the routes translate to HTTP

The real code lives in :mod:`app.auth.identity.mfa` (flow), 
:mod:`app.auth.identity.totp` (arithmetic) and
:mod:`app.auth.identity.policies` (policy), because the login path and the MFA
routes must never disagree about whether a factor is required.
"""
from __future__ import annotations

from app.auth.mfa import challenges, policy, recovery_codes, totp

__all__ = ["challenges", "policy", "recovery_codes", "totp"]
