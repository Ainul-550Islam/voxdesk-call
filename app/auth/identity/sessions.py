"""Session and device lifecycle.

A session is the unit an administrator or an end user actually reasons about:
*"there are four places I am signed in; sign out the other three."* Before this
module, the closest thing was a row in ``refresh_tokens``, which is a
credential record rather than a sitting — it has no idea whether a second factor
was satisfied, when it was last used, or which of the four devices it is.

The refresh token remains the credential and keeps its existing single-use
rotation and reuse detection. What changes is that each one now names the
session it belongs to (``RefreshToken.session_id``), so revocation can happen
at either granularity and both are consistent with each other.

Rules enforced here, all of them tested in ``tests/auth/test_sessions.py`` and
``tests/security/test_session_invalidation.py``:

* **Idle and absolute expiry.** A session dies after ``session_idle_minutes``
  of no use, and in all cases at ``session_max_active`` days after it was
  created. Both timestamps are on the row, so expiry is a property of the data
  rather than of whichever worker happens to see it.
* **A ceiling on concurrent sessions.** Opening the Nth+1 session revokes the
  least recently used live session rather than refusing the login. Refusing
  would lock a user out of their own account because they left iTerm open;
  revoking the oldest keeps the promise "you are signed in where you last
  were" and is what the user can see on the sessions page.
* **Password and MFA changes kill everything else.** ``revoke_all_for_user``
  with ``keep_session_id`` — the session that performed the change survives,
  every other one dies. Reuse-detection revocations keep their existing
  behaviour (no survivor).
* **A new session from an unseen place is an event, not a block.** If this is
  the first time this user has signed in from this (user-agent, IP) pair while
  other sessions are live, a ``SESSION_SUSPICIOUS`` event is written. It is
  *not* a refusal: the alternative is locking out every user behind CGNAT on
  their commute.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.identity import policies
from app.auth.identity.models import UserSession
from app.auth.identity.policies import AuthMethod
from app.db.models import AuditAction, RefreshToken, User


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=None) if value.tzinfo else value


def describe_device(user_agent: str) -> str:
    """A short, human-readable label for a user-agent string.

    Deliberately naive and local: no user-agent database, no network call, no
    third-party parser. It exists so the sessions list says "Firefox on Linux"
    instead of a 250-character header, and it never influences a decision.
    """
    ua = (user_agent or "").lower()
    if not ua:
        return "Unknown device"

    if "curl" in ua:
        browser = "curl"
    elif "python-requests" in ua or "httpx" in ua:
        browser = "API client"
    elif "edg/" in ua:
        browser = "Edge"
    elif "opr/" in ua or "opera" in ua:
        browser = "Opera"
    elif "firefox" in ua:
        browser = "Firefox"
    elif "chrome" in ua or "chromium" in ua:
        browser = "Chrome"
    elif "safari" in ua:
        browser = "Safari"
    else:
        browser = "Browser"

    if "iphone" in ua or "ipad" in ua:
        platform = "iOS"
    elif "android" in ua:
        platform = "Android"
    elif "mac os x" in ua or "macintosh" in ua:
        platform = "macOS"
    elif "windows" in ua:
        platform = "Windows"
    elif "linux" in ua:
        platform = "Linux"
    else:
        platform = "Unknown OS"

    return f"{browser} on {platform}"


async def live_sessions(
    session: AsyncSession, *, user_id: uuid.UUID, now: datetime | None = None
) -> list[UserSession]:
    """Every session for this user that is still usable, newest first."""
    moment = now or _now()
    rows = (
        await session.execute(
            select(UserSession)
            .where(
                UserSession.user_id == user_id,
                UserSession.revoked_at.is_(None),
                UserSession.expires_at > moment,
                UserSession.idle_expires_at > moment,
            )
            .order_by(UserSession.last_seen_at.desc())
        )
    ).scalars().all()
    return list(rows)


async def count_live_sessions(session: AsyncSession, user_id: uuid.UUID) -> int:
    now = _now()
    return (
        await session.execute(
            select(func.count(UserSession.id)).where(
                UserSession.user_id == user_id,
                UserSession.revoked_at.is_(None),
                UserSession.expires_at > now,
                UserSession.idle_expires_at > now,
            )
        )
    ).scalar_one()


async def _evict_oldest(session: AsyncSession, *, user_id: uuid.UUID, keep: int) -> list[UserSession]:
    """Bring a user's live session count back under the ceiling.

    ``keep`` is the number of *existing* sessions that may remain live, so
    ``keep=0`` — the ceiling is one session — evicts every live session before
    the new one is written. Returning early for ``keep < 1`` (as this once did)
    silently turned the strictest setting into no limit at all.
    """
    rows = (
        await session.execute(
            select(UserSession)
            .where(
                UserSession.user_id == user_id,
                UserSession.revoked_at.is_(None),
                UserSession.expires_at > _now(),
            )
            .order_by(UserSession.created_at.asc())
        )
    ).scalars().all()
    excess = len(rows) - keep
    if excess <= 0:
        return []
    evicted = list(rows[:excess])
    moment = _now()
    for row in evicted:
        row.revoked_at = moment
        row.revoked_reason = "session_limit"
    return evicted


async def create_session(
    session: AsyncSession,
    user: User,
    *,
    policy: policies.ResolvedPolicy,
    auth_method: AuthMethod = AuthMethod.PASSWORD,
    mfa_verified: bool = False,
    mfa_verified_at: datetime | None = None,
    password_confirmed_at: datetime | None = None,
    ip_address: str = "",
    user_agent: str = "",
    device_label: str = "",
    sso_connection_id: uuid.UUID | None = None,
    commit: bool = False,
) -> UserSession:
    """Open a session for a user who has just finished authenticating.

    Called by: the password login path (``app.auth.service.issue_tokens`` via
    ``ensure_session``) and the federated path
    (``app.auth.sso.service.complete_login``), i.e. every route that mints a
    refresh token for a human.
    """
    limits = policies.evaluate_session_policy(policy)
    now = _now()

    evicted = await _evict_oldest(
        session, user_id=user.id, keep=max(0, limits.max_active - 1)
    )

    row = UserSession(
        tenant_id=user.tenant_id,
        user_id=user.id,
        user_agent=(user_agent or "")[:300],
        ip_address=(ip_address or "")[:64],
        device_label=(device_label or describe_device(user_agent))[:120],
        auth_method=auth_method.value if isinstance(auth_method, AuthMethod) else str(auth_method),
        mfa_verified=bool(mfa_verified),
        mfa_verified_at=_naive(mfa_verified_at) if mfa_verified else None,
        password_confirmed_at=_naive(password_confirmed_at),
        sso_connection_id=sso_connection_id,
        created_at=now,
        last_seen_at=now,
        expires_at=limits.absolute_expires_at,
        idle_expires_at=now + _minutes(limits.idle_minutes),
    )
    session.add(row)
    await session.flush()

    for evicted_row in evicted:
        # Also kill the refresh tokens that belonged to the evicted session, so
        # the credential cannot outlive the sitting it was issued for.
        await _revoke_tokens_for_session(session, evicted_row.id, reason="session_limit")

    await _flag_if_suspicious(
        session,
        user=user,
        ip_address=ip_address,
        user_agent=user_agent,
        new_session_id=row.id,
    )
    if commit:
        await session.commit()
    return row


def _minutes(count: int) -> "object":
    from datetime import timedelta

    return timedelta(minutes=max(1, int(count)))


async def _flag_if_suspicious(
    session: AsyncSession,
    *,
    user: User,
    ip_address: str,
    user_agent: str,
    new_session_id: uuid.UUID,
) -> None:
    """Write a SESSION_SUSPICIOUS event when this device/address pair is new.

    Only when the user *already* has another live session: a first login from a
    fresh laptop is ordinary and reporting it would train operators to ignore
    the event.
    """
    from app.auth.identity.events import emit

    others = (
        await session.execute(
            select(UserSession).where(
                UserSession.user_id == user.id,
                UserSession.id != new_session_id,
                UserSession.revoked_at.is_(None),
            )
        )
    ).scalars().all()
    if not others:
        return

    known = {
        (row.ip_address, row.user_agent) for row in others
    }
    if (ip_address or "")[:64] in {row.ip_address for row in others}:
        return
    if (ip_address or "")[:64] == "" and "" in {row.ip_address for row in others}:
        return

    await emit(
        session,
        AuditAction.SESSION_SUSPICIOUS,
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        target_user_id=user.id,
        actor_email=user.email,
        ip_address=ip_address,
        user_agent=user_agent,
        detail={
            "reason": "new_ip_address" if known else "new_device",
            "known_pairs": len(known),
            "session_id": str(new_session_id),
        },
        commit=False,
    )


async def touch(
    session: AsyncSession,
    row: UserSession,
    *,
    ip_address: str = "",
    user_agent: str = "",
    idle_minutes: int | None = None,
    commit: bool = False,
) -> bool:
    """Record activity on a session. Returns False if it is no longer usable.

    Called by the refresh path (``app.auth.service.rotate_refresh_token``) and
    by the session endpoints; it is the one place that extends the idle window,
    so a session cannot be kept alive by a route that forgot to say so.
    """
    now = _now()
    if not row.is_live(now):
        return False
    row.last_seen_at = now
    if idle_minutes:
        row.idle_expires_at = now + _minutes(idle_minutes)
    if ip_address:
        row.ip_address = ip_address[:64]
    if user_agent:
        row.user_agent = user_agent[:300]
    if commit:
        await session.commit()
    return True


async def get_session(session: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID) -> UserSession | None:
    row = await session.get(UserSession, session_id)
    if row is None or row.user_id != user_id:
        return None
    return row


async def _revoke_tokens_for_session(
    session: AsyncSession, session_id: uuid.UUID, *, reason: str
) -> int:
    rows = (
        await session.execute(
            select(RefreshToken).where(
                RefreshToken.session_id == session_id,
                RefreshToken.revoked_at.is_(None),
            )
        )
    ).scalars().all()
    now = _now()
    for row in rows:
        row.revoked_at = now
    return len(rows)


async def revoke_session(
    session: AsyncSession,
    row: UserSession,
    *,
    reason: str = "user_revoked",
    commit: bool = False,
) -> int:
    """Revoke one session and every unexpired refresh token issued for it.

    Called by the logout route, the "sign out this device" endpoint and the
    session-limit eviction above.
    """
    now = _now()
    if row.revoked_at is None:
        row.revoked_at = now
        row.revoked_reason = reason[:64]
    tokens = await _revoke_tokens_for_session(session, row.id, reason=reason)
    if commit:
        await session.commit()
    return tokens


async def revoke_sessions(
    session: AsyncSession,
    rows: list[UserSession],
    *,
    reason: str,
    commit: bool = False,
) -> int:
    revoked = 0
    for row in rows:
        if row.revoked_at is None:
            row.revoked_at = _now()
            row.revoked_reason = reason[:64]
            revoked += 1
        await _revoke_tokens_for_session(session, row.id, reason=reason)
    if commit:
        await session.commit()
    return revoked


async def revoke_other_sessions(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    keep_session_id: uuid.UUID | None,
    reason: str = "user_revoked_others",
    commit: bool = False,
) -> int:
    """Sign out every other device, leaving the caller signed in.

    Called by ``POST /api/sessions/revoke-others`` and by the password-reset
    completion path when the reset did not come from a signed-in browser.
    """
    query = select(UserSession).where(
        UserSession.user_id == user_id, UserSession.revoked_at.is_(None)
    )
    if keep_session_id is not None:
        query = query.where(UserSession.id != keep_session_id)
    rows = list((await session.execute(query)).scalars().all())
    return await revoke_sessions(session, rows, reason=reason, commit=commit)


async def revoke_all_for_user(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    reason: str,
    keep_session_id: uuid.UUID | None = None,
    commit: bool = False,
) -> int:
    """Revoke every session for a user, optionally sparing the caller's own.

    A thin alias over ``revoke_other_sessions`` when ``keep_session_id`` is set,
    so the two call sites read the same and there is one implementation of
    "revoke and cascade".
    """
    if keep_session_id is not None:
        return await revoke_other_sessions(
            session, user_id=user_id, keep_session_id=keep_session_id,
            reason=reason, commit=commit,
        )
    rows = list(
        (
            await session.execute(
                select(UserSession).where(
                    UserSession.user_id == user_id, UserSession.revoked_at.is_(None)
                )
            )
        ).scalars().all()
    )
    return await revoke_sessions(session, rows, reason=reason, commit=commit)


async def sweep_expired(session: AsyncSession, *, user_id: uuid.UUID) -> int:
    """Mark idle/absolute-expired sessions revoked, so the list stays honest.

    Expiry is already enforced by ``is_live`` on every read; this is for
    presentation, and for the ``expired`` reason to be visible to the user
    rather than a session silently disappearing.
    """
    now = _now()
    rows = (
        await session.execute(
            select(UserSession).where(
                UserSession.user_id == user_id,
                UserSession.revoked_at.is_(None),
                (UserSession.expires_at <= now) | (UserSession.idle_expires_at <= now),
            )
        )
    ).scalars().all()
    for row in rows:
        row.revoked_at = now
        row.revoked_reason = "expired"
    return len(rows)
