"""One-time recovery codes.

Implementation: :mod:`app.auth.identity.mfa` (the flows) and
:mod:`app.auth.identity.tokens` (generation and normalization).

The rules this module exists to keep in one place:

* a code is stored **only** as a SHA-256 digest, one row per code, so
  consumption is a row-level conditional update (``used_at IS NULL``) that two
  concurrent logins cannot both win;
* codes are returned exactly once, from the call that created them, and only
  ever from an explicit flow — enrollment confirmation, or regeneration with a
  fresh proof of presence;
* regenerating replaces the whole set. The old codes are marked used rather
  than deleted, so an incident review can still see them.
"""
from __future__ import annotations

from app.auth.identity.mfa import (
    recovery_code_used_event,
    regenerate_recovery_codes,
)
from app.auth.identity.tokens import (
    generate_recovery_codes,
    normalize_recovery_code,
)

__all__ = [
    "generate_recovery_codes",
    "normalize_recovery_code",
    "recovery_code_used_event",
    "regenerate_recovery_codes",
]
