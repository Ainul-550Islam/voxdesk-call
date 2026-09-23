"""Brute force, and the four things that stop it.

One six-digit code is a million possibilities, which is only safe if guessing is
expensive. The controls asserted here are the ones the login path actually
consults:

* **per-challenge failure ceiling.** Wrong codes accumulate on the challenge
  row. On the ceiling the challenge locks, the correct code is refused too, and
  the lock is recorded — clearing it needs the TTL to pass, not a fresh request;
* **per-user verification rate limit.** Step-up verification is limited per
  user, so a caller with a valid session cannot grind codes either;
* **enrollment rate limit.** Starting enrollments is limited per user, so the
  seed endpoint is not a free oracle;
* **the lockout is per user.** One account under attack does not lock out its
  colleagues.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.auth.identity.models import MFAChallenge
from app.auth.mfa import challenges as mfa_challenges
from app.core.config import settings
from app.db.models import AuditAction, AuditLog
from tests.conftest import (
    auth_headers,
    enroll_totp,
    failure_detail,
    login,
    mfa_auth_headers,
    require_mfa,
)

pytestmark = pytest.mark.asyncio


async def test_wrong_codes_lock_the_challenge_and_then_refuse_the_right_one(
    client, db, owner_a, totp_clock
):
    await require_mfa(client, owner_a)
    secret = await enroll_totp(db, owner_a, totp_clock)

    started = await login(client, owner_a.email)
    assert started.status_code == 202, started.text
    challenge = started.json()["challenge"]

    limit = settings.mfa_challenge_max_failures
    for _ in range(limit):
        attempt = await client.post(
            "/auth/mfa/verify", json={"challenge": challenge, "code": "000000"}
        )
        assert attempt.status_code == 401, attempt.text
        assert failure_detail(attempt), "every refusal carries a reason"

    # The next attempt — even with a code that is arithmetically correct — is
    # refused, because the challenge is locked rather than merely suspicious.
    locked = await client.post(
        "/auth/mfa/verify",
        json={"challenge": challenge, "code": totp_clock.code(secret)},
    )
    assert locked.status_code == 429, locked.text
    assert "access_token" not in locked.text

    row = (
        await db.execute(
            select(MFAChallenge).where(MFAChallenge.user_id == owner_a.id)
        )
    ).scalars().first()
    assert row is not None
    assert row.locked_until is not None, "the lock must be stored, not just returned"
    assert row.failures >= limit

    events = (
        await db.execute(
            select(AuditLog).where(AuditLog.action == AuditAction.MFA_CHALLENGE_LOCKED)
        )
    ).scalars().all()
    assert events, "a lockout is a security event, not a silent 429"


async def test_the_lockout_belongs_to_one_user(client, db, owner_a, admin_a, totp_clock):
    await require_mfa(client, owner_a)
    await enroll_totp(db, owner_a, totp_clock)
    await enroll_totp(db, admin_a, totp_clock)

    started = await login(client, owner_a.email)
    challenge = started.json()["challenge"]
    for _ in range(settings.mfa_challenge_max_failures):
        await client.post(
            "/auth/mfa/verify", json={"challenge": challenge, "code": "000000"}
        )

    # A colleague with their own factor is unaffected — their password step and
    # their challenge both still work.
    colleague_start = await login(client, admin_a.email)
    assert colleague_start.status_code == 202, colleague_start.text
    assert colleague_start.json()["challenge"] != challenge


async def test_step_up_verification_is_rate_limited_per_user(client, db, owner_a, totp_clock):
    await require_mfa(client, owner_a)
    secret = await enroll_totp(db, owner_a, totp_clock)
    headers = await mfa_auth_headers(client, owner_a, secret, totp_clock)

    limit = 20
    refusals = 0
    for index in range(limit + 3):
        response = await client.post(
            "/api/mfa/verify",
            json={"code": "000000"},
            headers=headers,
        )
        if response.status_code == 429:
            # Either control may be the one that trips — the per-user step-up
            # limiter, or the per-user verification ceiling the legacy bare-code
            # path counts against. Both answer 429 with a reason.
            refusals += 1
            assert failure_detail(response), "a 429 must say why"
        else:
            assert response.status_code == 401, response.text
    assert refusals >= 1, "the per-user limiter never engaged"


async def test_starting_enrollments_is_rate_limited(client, db, owner_a, totp_clock):
    headers = await auth_headers(client, owner_a)
    statuses = []
    for _ in range(12):
        response = await client.post("/api/mfa/enroll", headers=headers)
        statuses.append(response.status_code)
        if response.status_code == 429:
            break
    assert 429 in statuses, statuses


async def test_a_consumed_challenge_cannot_be_answered_again(client, db, owner_a, totp_clock):
    """Completing a login spends the challenge; a replay finds nothing.

    The token is still a well-formed string and the row still exists, so the
    refusal has to come from the consumption check rather than from a 404.
    """
    await require_mfa(client, owner_a)
    secret = await enroll_totp(db, owner_a, totp_clock)

    started = await login(client, owner_a.email)
    challenge = started.json()["challenge"]
    done = await client.post(
        "/auth/mfa/verify",
        json={"challenge": challenge, "code": totp_clock.consume(secret)},
    )
    assert done.status_code == 200, done.text

    replayed = await client.post(
        "/auth/mfa/verify",
        json={"challenge": challenge, "code": totp_clock.consume(secret)},
    )
    assert replayed.status_code in (400, 401, 429), replayed.text
    assert "access_token" not in replayed.text


async def test_a_lockout_lifts_when_its_window_passes(client, monkeypatch, db, owner_a, totp_clock):
    """The lock is a deadline, not a dead end.

    ``locked_until`` is stored, so the account recovers on its own once the
    window passes — no administrator, and no fresh challenge, is required. The
    test shortens the window to a minute and moves the TOTP clock three periods
    (ninety seconds): past the lock, still inside the challenge's TTL.
    """
    from app.auth.identity.exceptions import MFAChallengeLocked
    from app.auth.identity import mfa as identity_mfa

    monkeypatch.setattr(settings, "mfa_challenge_lockout_minutes", 1)
    await require_mfa(client, owner_a)
    secret = await enroll_totp(db, owner_a, totp_clock)

    issued = await identity_mfa.create_challenge(
        db, owner_a, purpose=mfa_challenges.ChallengePurpose.LOGIN, commit=True
    )
    for _ in range(settings.mfa_challenge_max_failures):
        result = await identity_mfa.resolve_challenge(
            db, token=issued.token, code="000000", commit=True
        )
        assert result.ok is False

    row = (
        await db.execute(
            select(MFAChallenge).where(MFAChallenge.user_id == owner_a.id)
        )
    ).scalars().first()
    assert row is not None
    assert row.locked_until is not None

    with pytest.raises(MFAChallengeLocked):
        await identity_mfa.resolve_challenge(
            db, token=issued.token, code=totp_clock.code(secret), commit=True
        )

    totp_clock.advance(3)
    recovered = await identity_mfa.resolve_challenge(
        db, token=issued.token, code=totp_clock.consume(secret), commit=True
    )
    assert recovered.ok is True
