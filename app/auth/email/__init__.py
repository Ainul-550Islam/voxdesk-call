"""Outbound account email: verification and password reset.

Two flows, one delivery path. :mod:`app.auth.identity.email` holds the
rendering, the SMTP transport and the single-use tokens; this package names the
flows so a caller imports the flow it means.

Neither flow leaks whether an account exists:

* requesting a reset for an unknown address returns the same body as for a known
  one, and the same is true for a verification request;
* the token is random (``secrets.token_urlsafe``-grade), stored **only** as a
  digest, single-use via a conditional UPDATE, and expiring;
* completing a reset revokes every other session, because a reset is exactly the
  moment to assume the old sessions are not trustworthy.
"""
from __future__ import annotations

from app.auth.email import password_reset, verification

__all__ = ["password_reset", "verification"]
