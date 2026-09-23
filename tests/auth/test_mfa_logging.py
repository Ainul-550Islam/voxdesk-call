"""What must never appear in a log, a response or an audit row.

Secrets leak through the boring paths: a structured logger that dumps the
response model, an audit ``detail`` dict that copies the request body, an error
handler that echoes what it was given. These tests walk the whole enrollment and
verification flow and then grep everything that was written down.

The rule the product states — and the rule asserted here — is that a TOTP seed,
a recovery code, a challenge token and an access token appear in exactly one
place each: the HTTP response that created them.
"""
from __future__ import annotations

import logging

import pytest
from sqlalchemy import select

from app.auth.identity.models import MFAChallenge, MFAFactor
from app.auth.identity import tokens as identity_tokens
from app.db.models import AuditLog
from tests.conftest import (
    auth_headers,
    enroll_totp,
    login,
    mfa_auth_headers,
    require_mfa,
)

pytestmark = pytest.mark.asyncio


def _recorded(caplog) -> str:
    return "\n".join(
        f"{record.getMessage()} {record.__dict__}" for record in caplog.records
    )


async def test_the_enrollment_seed_never_reaches_a_log(client, db, caplog, owner_a):
    caplog.set_level(logging.DEBUG)
    response = await client.post(
        "/api/mfa/enroll", headers=await auth_headers(client, owner_a)
    )
    assert response.status_code == 201, response.text
    seed = response.json()["secret"]

    assert seed not in _recorded(caplog), "the TOTP seed was logged"


async def test_neither_the_seed_nor_the_codes_reach_a_log(
    client, db, caplog, owner_a, totp_clock
):
    caplog.set_level(logging.DEBUG)
    await require_mfa(client, owner_a)
    secret = await enroll_totp(db, owner_a, totp_clock)
    headers = await mfa_auth_headers(client, owner_a, secret, totp_clock)
    codes = (
        await client.post("/api/mfa/recovery-codes/regenerate", headers=headers)
    ).json()["recovery_codes"]

    started = await login(client, owner_a.email)
    assert started.status_code == 202, started.text
    done = await client.post(
        "/auth/mfa/verify",
        json={"challenge": started.json()["challenge"], "code": codes[0]},
    )
    assert done.status_code == 200, done.text

    recorded = _recorded(caplog)
    assert secret not in recorded
    for code in codes:
        assert code not in recorded
    assert codes[0] not in recorded
    assert done.json()["access_token"] not in recorded, "an access token was logged"


async def test_the_audit_trail_records_the_event_without_the_secret(
    client, db, owner_a, totp_clock
):
    secret = await enroll_totp(db, owner_a, totp_clock)

    rows = (await db.execute(select(AuditLog))).scalars().all()
    assert rows, "enrollment must be audited"
    for row in rows:
        blob = f"{row.action} {row.actor_email} {row.detail}"
        assert secret not in blob
        detail = row.detail or {}
        assert "secret" not in detail
        assert "secret_encrypted" not in detail
        assert "recovery_codes" not in detail


async def test_the_challenge_token_is_stored_only_as_a_hash(client, db, owner_a, totp_clock):
    await require_mfa(client, owner_a)
    await enroll_totp(db, owner_a, totp_clock)

    started = await login(client, owner_a.email)
    assert started.status_code == 202, started.text
    token = started.json()["challenge"]

    row = (
        await db.execute(select(MFAChallenge).where(MFAChallenge.user_id == owner_a.id))
    ).scalars().first()
    assert row is not None
    assert row.challenge_hash == identity_tokens.hash_token(token)
    assert token != row.challenge_hash
    assert token not in row.challenge_hash


async def test_status_reports_the_posture_and_none_of_the_material(client, db, owner_a, totp_clock):
    await require_mfa(client, owner_a)
    secret = await enroll_totp(db, owner_a, totp_clock)
    headers = await mfa_auth_headers(client, owner_a, secret, totp_clock)

    body = (await client.get("/api/mfa/status", headers=headers)).json()
    rendered = str(body)
    assert secret not in rendered
    for forbidden in ("secret", "secret_encrypted", "provisioning_uri", "recovery_codes"):
        assert forbidden not in body, forbidden


async def test_the_factor_row_never_holds_the_seed_in_the_clear(client, db, owner_a):
    started = await client.post(
        "/api/mfa/enroll", headers=await auth_headers(client, owner_a)
    )
    seed = started.json()["secret"]

    factors = (await db.execute(select(MFAFactor))).scalars().all()
    assert factors
    for factor in factors:
        assert factor.secret_encrypted != seed
        assert seed not in factor.secret_encrypted
        assert not factor.secret_encrypted.startswith("JBSWY"), (
            "base32 seeds are recognisable — one in a column means it is plaintext"
        )
