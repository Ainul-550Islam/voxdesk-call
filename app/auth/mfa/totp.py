"""TOTP: the arithmetic, the window and the provisioning URI.

Implementation: :mod:`app.auth.identity.totp` (RFC 4226 HMAC-based one-time
passwords and RFC 6238 time-based ones, hash comparison in constant time, and
the ``otpauth://`` URI an authenticator app scans).

Two properties matter and are asserted by ``tests/auth/test_totp.py``:

* the window accepts one step either side of now — the clock in a phone is not
  the clock in a server, and refusing a code that is fifteen seconds early is
  how a support queue fills up;
* a code that has already been accepted cannot be accepted again. The replay
  guard lives on the factor row (``last_timestep``) rather than here, because it
  is a fact about the stored credential, not about the arithmetic.
"""
from __future__ import annotations

from app.auth.identity.totp import (
    format_secret_for_display,
    generate_secret,
    hotp,
    provisioning_uri,
    step_for,
    totp,
    verify,
)

__all__ = [
    "format_secret_for_display",
    "generate_secret",
    "hotp",
    "provisioning_uri",
    "step_for",
    "totp",
    "verify",
]
