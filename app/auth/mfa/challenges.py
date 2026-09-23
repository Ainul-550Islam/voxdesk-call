"""The MFA challenge lifecycle.

Implementation: :mod:`app.auth.identity.mfa`.

A challenge is what a client holds between "the password was right" and "the
code was right": an opaque token whose digest — never the token — is stored on
the row, together with the purpose, the session it belongs to, the failure
counter and the lockout deadline.

Three rules the implementation keeps, and why they are visible here:

* **One open challenge per user per purpose.** Raising a new one consumes every
  earlier open challenge, so a second login attempt invalidates the first
  attempt's token instead of leaving two live keys to the same account.
* **Failures accumulate on the row.** Four wrong codes in a row lock the
  challenge, and the lockout is stored, so clearing it needs the TTL to pass —
  not a new request.
* **A step-up challenge is bound to the session that asked for it.** The binding
  (purpose and session) is checked with :func:`load_challenge` *before*
  :func:`resolve_challenge` spends it, because resolving first would consume a
  challenge the rightful caller still needs.
"""
from __future__ import annotations

from app.auth.identity.mfa import (
    ChallengeResult,
    IssuedChallenge,
    create_challenge,
    load_challenge,
    resolve_challenge,
    verify_user_code,
)
from app.auth.identity.models import ChallengePurpose, MFAChallenge

__all__ = [
    "ChallengePurpose",
    "ChallengeResult",
    "IssuedChallenge",
    "MFAChallenge",
    "create_challenge",
    "load_challenge",
    "resolve_challenge",
    "verify_user_code",
]
