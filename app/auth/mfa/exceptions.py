"""MFA and step-up errors, as the routes translate them.

Every one of these is raised by :mod:`app.auth.identity.mfa` and turned into an
HTTP response by :func:`app.api.identity_errors.translate`, so the status codes
are decided in exactly one place:

===========================  =================================================
Error                        Response
===========================  =================================================
``MFADisabled``              403 — MFA is off for this deployment
``MFAAlreadyEnrolled``       409 — a factor is already active
``MFANotEnrolled``           403 — no factor (verify/disable without enrolling)
``MFAVerificationFailed``    401 — wrong code (``mfa_invalid_code``)
``MFAChallengeLocked``       429 — too many wrong codes; lockout is stored
``ReauthenticationRequired`` 428 — the action needs a fresh proof of presence
``PolicyDenied``             403 — the policy engine said no
===========================  =================================================

``ReauthenticationRequired`` is a subclass of ``PolicyDenied`` so a caller that
only knows about policy refusals still catches it, but it is raised first and
translated first: a privileged action that needs a fresh factor answers 428
"go and re-authenticate", never a blanket 403.
"""
from __future__ import annotations

from app.auth.identity.exceptions import (
    IdentityConfigurationError,
    IdentityError,
    IdentitySecretsUnavailable,
    MFAAlreadyEnrolled,
    MFAChallengeLocked,
    MFADisabled,
    MFAError,
    MFANotEnrolled,
    MFARequired,
    MFAVerificationFailed,
    PolicyDenied,
    ReauthenticationRequired,
)

__all__ = [
    "IdentityConfigurationError",
    "IdentityError",
    "IdentitySecretsUnavailable",
    "MFAAlreadyEnrolled",
    "MFAChallengeLocked",
    "MFADisabled",
    "MFAError",
    "MFANotEnrolled",
    "MFARequired",
    "MFAVerificationFailed",
    "PolicyDenied",
    "ReauthenticationRequired",
]
