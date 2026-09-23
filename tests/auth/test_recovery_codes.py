"""Recovery codes: issued once, hashed at rest, spendable exactly once.

They are the escape hatch for a lost phone, which makes them a credential — and
these tests treat them that way:

* the plaintext exists only in the response that created it; the rows hold
  SHA-256 digests and nothing else;
* a code works exactly once. Consumption is a row-level conditional update, so
  the second attempt finds nothing to consume — and a *replayed* code cannot
  ride two concurrent logins through either;
* regenerating is a fresh proof of presence (a stale session gets 428) and it
  retires the whole previous set, so a code photographed off a sticky note
  stops working the moment the set is rotated;
* spending a code is audited as ``MFA_RECOVERY_CODE_USED`` and removes it from
  the remaining count.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.auth.identity.models import MFARecoveryCode, UserSession
from app.auth.identity import tokens as identity_tokens
from app.db.models import AuditAction, AuditLog
from tests.conftest import (
    enroll_totp,
    failure_detail,
    login,
    mfa_auth_headers,
    require_mfa,
)

pytestmark = pytest.mark.asyncio


async def refresh_codes(client, user, headers) -> list[str]:
    response = await client.post(
        "/api/mfa/recovery-codes/regenerate", headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()["recovery_codes"]


async def test_codes_are_stored_only_as_digests(client, db, owner_a, totp_clock):
    await require_mfa(client, owner_a)
    secret = await enroll_totp(db, owner_a, totp_clock)
    headers = await mfa_auth_headers(client, owner_a, secret, totp_clock)
    codes = await refresh_codes(client, owner_a, headers)

    rows = (
        await db.execute(
            select(MFARecoveryCode).where(MFARecoveryCode.user_id == owner_a.id)
        )
    ).scalars().all()
    assert rows, "regenerating must leave a usable set"

    stored = {row.code_hash for row in rows}
    for code in codes:
        assert code not in stored
    normalised = {identity_tokens.normalize_recovery_code(code) for code in codes}
    hashes = {identity_tokens.hash_token(normalised_code) for normalised_code in normalised}
    assert hashes <= stored, "every issued code must have exactly one stored digest"
    for row in rows:
        assert len(row.code_hash) == 64, "a digest, not a code"
        assert row.used_at is None


async def test_a_recovery_code_logs_in_once_and_only_once(client, db, owner_a, totp_clock):
    await require_mfa(client, owner_a)
    secret = await enroll_totp(db, owner_a, totp_clock)
    headers = await mfa_auth_headers(client, owner_a, secret, totp_clock)
    codes = await refresh_codes(client, owner_a, headers)
    code = codes[0]

    started = await login(client, owner_a.email)
    assert started.status_code == 202, started.text
    used = await client.post(
        "/auth/mfa/verify",
        json={"challenge": started.json()["challenge"], "code": code},
    )
    assert used.status_code == 200, used.text
    assert "access_token" in used.text

    spent = (
        await db.execute(
            select(MFARecoveryCode).where(
                MFARecoveryCode.code_hash
                == identity_tokens.hash_token(identity_tokens.normalize_recovery_code(code))
            )
        )
    ).scalar_one()
    assert spent.used_at is not None

    # Same code, fresh challenge: refused, and the row is not resurrected.
    second = await login(client, owner_a.email)
    assert second.status_code == 202, second.text
    refused = await client.post(
        "/auth/mfa/verify",
        json={"challenge": second.json()["challenge"], "code": code},
    )
    assert refused.status_code == 401, refused.text
    assert "access_token" not in refused.text
    assert failure_detail(refused), "a refusal must say why"

    await db.refresh(spent)
    assert spent.used_at is not None


async def test_a_used_code_is_recorded_in_the_audit_trail(client, db, owner_a, totp_clock):
    await require_mfa(client, owner_a)
    secret = await enroll_totp(db, owner_a, totp_clock)
    headers = await mfa_auth_headers(client, owner_a, secret, totp_clock)
    code = (await refresh_codes(client, owner_a, headers))[0]

    started = await login(client, owner_a.email)
    done = await client.post(
        "/auth/mfa/verify",
        json={"challenge": started.json()["challenge"], "code": code},
    )
    assert done.status_code == 200, done.text

    events = (
        await db.execute(
            select(AuditLog).where(AuditLog.action == AuditAction.MFA_RECOVERY_CODE_USED)
        )
    ).scalars().all()
    assert len(events) == 1
    detail = events[0].detail or {}
    assert code not in str(detail), "the code itself must never be recorded"


async def test_regenerating_retires_the_previous_set(client, db, owner_a, totp_clock):
    await require_mfa(client, owner_a)
    secret = await enroll_totp(db, owner_a, totp_clock)
    headers = await mfa_auth_headers(client, owner_a, secret, totp_clock)
    first = await refresh_codes(client, owner_a, headers)
    second = await refresh_codes(client, owner_a, headers)

    assert set(first).isdisjoint(second)

    started = await login(client, owner_a.email)
    old_code_refused = await client.post(
        "/auth/mfa/verify",
        json={"challenge": started.json()["challenge"], "code": first[0]},
    )
    assert old_code_refused.status_code == 401, old_code_refused.text
    assert "access_token" not in old_code_refused.text

    body = await client.get("/api/mfa/status", headers=headers)
    assert body.json()["recovery_codes_remaining"] == len(second)


async def test_regenerating_needs_a_fresh_proof_of_presence(client, db, owner_a, totp_clock):
    """A borrowed browser must not be able to mint itself a new escape hatch."""
    await require_mfa(client, owner_a)
    secret = await enroll_totp(db, owner_a, totp_clock)
    headers = await mfa_auth_headers(client, owner_a, secret, totp_clock)

    session = (
        await db.execute(
            select(UserSession).where(
                UserSession.user_id == owner_a.id, UserSession.mfa_verified.is_(True)
            )
        )
    ).scalars().first()
    assert session is not None, "the MFA-verified session is the one the routes act on"
    assert session.password_confirmed_at is not None
    session.password_confirmed_at = session.password_confirmed_at - timedelta(hours=4)
    session.mfa_verified_at = session.mfa_verified_at - timedelta(hours=4) if session.mfa_verified_at else None
    await db.commit()

    stale = await client.post("/api/mfa/recovery-codes/regenerate", headers=headers)
    assert stale.status_code == 428, stale.text
    assert stale.json()["detail"]["code"] == "reauth_required"

    # Prove presence with a bound step-up challenge, then it works.
    issued = await client.post("/api/mfa/challenge", headers=headers)
    assert issued.status_code == 201, issued.text
    proved = await client.post(
        "/api/mfa/verify",
        json={
            "challenge": issued.json()["challenge_token"],
            "code": totp_clock.consume(secret),
        },
        headers=headers,
    )
    assert proved.status_code == 200, proved.text

    refreshed = await client.post("/api/mfa/recovery-codes/regenerate", headers=headers)
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["recovery_codes"]


async def test_a_recovery_code_outside_the_set_is_refused(client, db, owner_a, totp_clock):
    await require_mfa(client, owner_a)
    secret = await enroll_totp(db, owner_a, totp_clock)
    headers = await mfa_auth_headers(client, owner_a, secret, totp_clock)
    await refresh_codes(client, owner_a, headers)

    invented = identity_tokens.generate_recovery_codes(1)[0]
    started = await login(client, owner_a.email)
    refused = await client.post(
        "/auth/mfa/verify",
        json={"challenge": started.json()["challenge"], "code": invented},
    )
    assert refused.status_code == 401, refused.text
    assert "access_token" not in refused.text
