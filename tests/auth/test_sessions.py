"""The session rules stated in ``app/auth/identity/sessions.py``.

A session is what a person reasons about ("sign out the other three"), so each
rule the module promises gets a test here:

* idle and absolute expiry are properties of the row, not of a sweeper;
* a ceiling on concurrent sessions evicts the least recently used rather than
  refusing the login;
* a password or second-factor change keeps the session that performed it and
  ends every other one;
* a sign-in from a new place is an event, never a refusal.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.auth.identity import policies
from app.auth.identity import sessions as identity_sessions
from app.auth.identity.models import UserSession
from app.auth.identity.policies import AuthMethod
from app.db.models import AuditAction, AuditLog, RefreshToken, UserRole
from tests.conftest import make_tenant, make_user

pytestmark = pytest.mark.asyncio


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _policy(db, tenant, **overrides):
    base = await policies.load_policy(db, tenant.id)
    return dataclasses.replace(base, **overrides) if overrides else base


async def _open(db, user, policy, *, ip="203.0.113.5", ua="Mozilla/5.0 Firefox/128.0"):
    return await identity_sessions.create_session(
        db, user, policy=policy, ip_address=ip, user_agent=ua, commit=True
    )


async def test_an_idle_session_is_not_live_once_its_deadline_passes(db):
    tenant = await make_tenant(db, "Session Tenant")
    user = await make_user(db, tenant, UserRole.AGENT)
    policy = await _policy(db, tenant, session_idle_minutes=30)
    row = await _open(db, user, policy)

    assert row.is_live(_now())
    assert row.idle_expires_at == pytest.approx(
        (row.created_at + timedelta(minutes=30)).replace(tzinfo=None), abs=timedelta(seconds=1)
    )

    row.idle_expires_at = _now() - timedelta(seconds=1)
    await db.commit()
    assert not row.is_live(_now())
    assert await identity_sessions.live_sessions(db, user_id=user.id) == []

    # The sweeper only makes the reason visible; it decides nothing.
    assert await identity_sessions.sweep_expired(db, user_id=user.id) == 1
    await db.commit()
    await db.refresh(row)
    assert row.revoked_reason == "expired"


async def test_a_session_also_ends_at_its_absolute_deadline(db):
    tenant = await make_tenant(db, "Session Tenant")
    user = await make_user(db, tenant, UserRole.AGENT)
    policy = await _policy(db, tenant, refresh_token_days=7)
    row = await _open(db, user, policy)

    # A session that is touched constantly still cannot outlive its absolute
    # window: that is the difference between an idle timeout and a real one.
    row.expires_at = _now() - timedelta(seconds=1)
    await db.commit()
    assert not row.is_live(_now())
    assert await identity_sessions.touch(db, row, commit=True) is False


async def test_touching_a_live_session_pushes_the_idle_deadline_out(db):
    tenant = await make_tenant(db, "Session Tenant")
    user = await make_user(db, tenant, UserRole.AGENT)
    policy = await _policy(db, tenant, session_idle_minutes=60)
    row = await _open(db, user, policy)
    row.idle_expires_at = _now() + timedelta(minutes=1)
    await db.commit()

    before = row.idle_expires_at
    assert await identity_sessions.touch(db, row, idle_minutes=60, commit=True) is True
    await db.refresh(row)
    assert row.idle_expires_at > before
    assert row.last_seen_at <= _now()


async def test_the_concurrent_session_ceiling_evicts_the_least_recently_used(db):
    tenant = await make_tenant(db, "Session Tenant")
    user = await make_user(db, tenant, UserRole.MANAGER)
    policy = await _policy(db, tenant, session_max_active=2)

    first = await _open(db, user, policy, ip="203.0.113.1")
    second = await _open(db, user, policy, ip="203.0.113.2")
    third = await _open(db, user, policy, ip="203.0.113.3")

    live = await identity_sessions.live_sessions(db, user_id=user.id)
    assert {row.id for row in live} == {second.id, third.id}
    await db.refresh(first)
    assert first.revoked_reason == "session_limit", "the oldest goes, and says why"


async def test_evicting_a_session_also_kills_its_refresh_tokens(db):
    tenant = await make_tenant(db, "Session Tenant")
    user = await make_user(db, tenant, UserRole.MANAGER)
    policy = await _policy(db, tenant, session_max_active=1)
    first = await _open(db, user, policy)
    token = RefreshToken(
        user_id=user.id,
        token_hash="x" * 64,
        session_id=first.id,
        expires_at=_now() + timedelta(days=1),
    )
    db.add(token)
    await db.commit()

    await _open(db, user, policy, ip="203.0.113.99")
    await db.refresh(token)
    assert token.revoked_at is not None, "the credential must not outlive its session"
    assert token.replaced_by is None, "this is a revocation, not a rotation"


async def test_a_change_of_password_keeps_this_session_and_ends_the_others(db):
    tenant = await make_tenant(db, "Session Tenant")
    user = await make_user(db, tenant, UserRole.ADMIN)
    policy = await _policy(db, tenant)
    keep = await _open(db, user, policy)
    others = [await _open(db, user, policy, ip=f"198.51.100.{n}") for n in (1, 2)]

    revoked = await identity_sessions.revoke_all_for_user(
        db, user_id=user.id, reason="password_changed", keep_session_id=keep.id, commit=True
    )
    assert revoked == 2, "the session that changed the password survives"
    live = await identity_sessions.live_sessions(db, user_id=user.id)
    assert [row.id for row in live] == [keep.id]
    for row in others:
        await db.refresh(row)
        assert row.revoked_reason == "password_changed"


async def test_a_second_factor_change_ends_every_other_session(db):
    tenant = await make_tenant(db, "Session Tenant")
    user = await make_user(db, tenant, UserRole.ADMIN)
    policy = await _policy(db, tenant)
    keep = await _open(db, user, policy)
    await _open(db, user, policy, ip="198.51.100.9")

    revoked = await identity_sessions.revoke_all_for_user(
        db, user_id=user.id, reason="mfa_changed", keep_session_id=keep.id, commit=True
    )
    assert revoked == 1
    assert [row.id for row in await identity_sessions.live_sessions(db, user_id=user.id)] == [keep.id]


async def test_a_first_login_from_a_new_place_is_not_an_event(db):
    """A fresh laptop is ordinary; flagging it trains operators to ignore alerts."""
    tenant = await make_tenant(db, "Session Tenant")
    user = await make_user(db, tenant, UserRole.AGENT)
    await _open(db, user, await _policy(db, tenant), ip="203.0.113.5")

    rows = (
        await db.execute(select(AuditLog).where(AuditLog.action == AuditAction.SESSION_SUSPICIOUS))
    ).scalars().all()
    assert rows == []


async def test_a_second_login_from_an_unseen_address_is_recorded(db):
    tenant = await make_tenant(db, "Session Tenant")
    user = await make_user(db, tenant, UserRole.AGENT)
    policy = await _policy(db, tenant)
    await _open(db, user, policy, ip="203.0.113.5", ua="Mozilla/5.0 Firefox/128.0")
    second = await _open(db, user, policy, ip="203.0.113.77", ua="Mozilla/5.0 Firefox/128.0")

    rows = (
        await db.execute(select(AuditLog).where(AuditLog.action == AuditAction.SESSION_SUSPICIOUS))
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].detail["reason"] == "new_ip_address"
    assert rows[0].detail["session_id"] == str(second.id)

    # Logging in again from the same place is not news.
    await _open(db, user, policy, ip="203.0.113.5", ua="Mozilla/5.0 Firefox/128.0")
    rows = (
        await db.execute(select(AuditLog).where(AuditLog.action == AuditAction.SESSION_SUSPICIOUS))
    ).scalars().all()
    assert len(rows) == 1


async def test_the_event_never_blocks_the_login(db):
    tenant = await make_tenant(db, "Session Tenant")
    user = await make_user(db, tenant, UserRole.AGENT)
    policy = await _policy(db, tenant)
    await _open(db, user, policy, ip="203.0.113.5")
    row = await _open(db, user, policy, ip="10.0.0.1")

    assert row.revoked_at is None
    assert row.is_live(_now())
    assert len(await identity_sessions.live_sessions(db, user_id=user.id)) == 2


async def test_devices_are_described_without_a_parser_dependency():
    assert identity_sessions.describe_device("Mozilla/5.0 (X11; Linux x86_64) Firefox/128.0").startswith(
        "Firefox"
    )
    assert identity_sessions.describe_device("curl/8.5.0").startswith("curl")
    assert identity_sessions.describe_device("python-httpx/0.28.1").startswith("API client")
    assert identity_sessions.describe_device("") == "Unknown device"
    assert identity_sessions.describe_device("SomeUnheardOfAgent/1.0").startswith("Browser")


async def test_a_caller_cannot_revoke_a_session_it_does_not_own(db):
    tenant = await make_tenant(db, "Session Tenant")
    other = await make_tenant(db, "Other Tenant")
    user = await make_user(db, tenant, UserRole.AGENT)
    stranger = await make_user(db, other, UserRole.AGENT)
    mine = await _open(db, user, await _policy(db, tenant))
    theirs = await _open(db, stranger, await _policy(db, other), ip="10.1.1.1")

    assert await identity_sessions.get_session(db, user.id, mine.id) is not None
    assert await identity_sessions.get_session(db, user.id, theirs.id) is None
    assert await identity_sessions.get_session(
        db, stranger.id, mine.id
    ) is None, "another user's session id must not resolve"


async def test_counts_reflect_only_live_sessions(db):
    tenant = await make_tenant(db, "Session Tenant")
    user = await make_user(db, tenant, UserRole.AGENT)
    policy = await _policy(db, tenant)
    first = await _open(db, user, policy)
    await _open(db, user, policy, ip="203.0.113.6")
    assert await identity_sessions.count_live_sessions(db, user.id) == 2

    first.revoked_at = _now()
    first.revoked_reason = "user_revoked"
    await db.commit()
    assert await identity_sessions.count_live_sessions(db, user.id) == 1


async def test_a_session_records_how_it_was_authenticated(db):
    tenant = await make_tenant(db, "Session Tenant")
    user = await make_user(db, tenant, UserRole.ADMIN)
    policy = await _policy(db, tenant)
    row = await identity_sessions.create_session(
        db,
        user,
        policy=policy,
        auth_method=AuthMethod.SSO,
        mfa_verified=True,
        mfa_verified_at=_now(),
        password_confirmed_at=_now(),
        ip_address="203.0.113.5",
        commit=True,
    )
    assert row.auth_method == AuthMethod.SSO.value
    assert row.mfa_verified is True
    assert row.mfa_verified_at is not None
    # The row is the source of truth privileged checks read; it must survive a
    # re-read rather than living only in the request that created it.
    await db.refresh(row)
    assert isinstance(row, UserSession) and row.mfa_verified_at is not None
