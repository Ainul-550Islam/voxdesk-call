"""Session and device lifecycle.

A session in VoxDesk is a ``user_sessions`` row plus the refresh tokens minted
from it. The implementation is :mod:`app.auth.identity.sessions`; this module is
the single import site for it, so route code, the WebSocket path and the tests
all reach the same functions instead of growing parallel ones.

What the implementation guarantees (and therefore what importing this module
gets you):

* a session is listed with a coarse device description (``describe_device``
  reduces a user-agent to "<browser> on <platform>" — no version strings, no
  device fingerprints, nothing kept that the product has no use for);
* idle and absolute expiry are both enforced on every touch, so a session that
  should have died does not survive because no background job ran;
* a tenant session ceiling is enforced by evicting the oldest session rather
  than refusing the new login;
* revocation cascades: every refresh token that was minted from a revoked
  session is revoked with it, including bulk paths that never touched the row
  individually;
* a second session from an address that user has never signed in from raises a
  suspicious-session event.
"""
from __future__ import annotations

from app.auth.identity.service import ensure_session
from app.auth.identity.sessions import (
    count_live_sessions,
    create_session,
    describe_device,
    get_session,
    live_sessions,
    revoke_all_for_user,
    revoke_other_sessions,
    revoke_session,
    revoke_sessions,
    sweep_expired,
    touch,
)

__all__ = [
    "count_live_sessions",
    "create_session",
    "describe_device",
    "ensure_session",
    "get_session",
    "live_sessions",
    "revoke_all_for_user",
    "revoke_other_sessions",
    "revoke_session",
    "revoke_sessions",
    "sweep_expired",
    "touch",
]
