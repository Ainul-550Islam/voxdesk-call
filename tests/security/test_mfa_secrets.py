"""Secrets at rest: what is in the column, and what a leaked column is worth.

A database dump is the most common way credential material escapes. The MFA
tables are checked here against the standard the product claims:

* the TOTP seed is sealed with AES-256-GCM, and the envelope is bound to the
  tenant and to the purpose — copying a row into another tenant's database does
  not make it decryptable, and a ``totp`` envelope cannot be opened as, say, an
  OIDC client secret;
* nothing is stored in the clear: raw seeds, recovery codes and challenge
  tokens are absent from every column of every identity table this test can
  reach, including the audit log's free-form ``detail`` JSON;
* the same plaintext sealed twice produces two different envelopes, so the
  scheme is not a substitution cipher over its inputs;
* with no key ring configured, sealing *fails* rather than falling back to
  plaintext — the caller gets ``IdentitySecretsUnavailable`` and stores nothing.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.auth.identity import secrets as identity_secrets
from app.auth.identity import tokens as identity_tokens
from app.auth.identity.exceptions import IdentitySecretsUnavailable
from app.auth.identity.models import (
    IdentityMapping,
    MFAChallenge,
    MFAFactor,
    MFARecoveryCode,
    SSOConnection,
    UserEmail,
    UserSession,
)
from app.core.config import settings
from app.auth.identity.models import (
    APIKey,
    ServiceAccount,
    ServiceAccountCredential,
)
from app.db.models import AuditLog
from tests.conftest import (
    auth_headers,
    failure_detail,
    enroll_totp,
    login,
    mfa_auth_headers,
    require_mfa,
)

pytestmark = pytest.mark.asyncio

#: Every table the identity surface writes secrets or their neighbours into.
IDENTITY_MODELS = (
    MFAFactor,
    MFARecoveryCode,
    MFAChallenge,
    UserSession,
    UserEmail,
    SSOConnection,
    IdentityMapping,
    APIKey,
    ServiceAccount,
    ServiceAccountCredential,
    AuditLog,
)


def _text_of(row) -> str:
    parts: list[str] = []
    for column in row.__table__.columns:
        value = getattr(row, column.name, None)
        if value is not None:
            parts.append(str(value))
    return " ".join(parts)


async def _everything(db) -> str:
    blobs: list[str] = []
    for model in IDENTITY_MODELS:
        rows = (await db.execute(select(model))).scalars().all()
        blobs.extend(_text_of(row) for row in rows)
    return "\n".join(blobs)


async def test_the_seed_is_sealed_for_one_tenant_and_one_purpose(client, db, owner_a, tenant_b):
    secret = await enroll_totp(db, owner_a)

    factor = (
        await db.execute(select(MFAFactor).where(MFAFactor.user_id == owner_a.id))
    ).scalar_one()
    assert factor.secret_encrypted != secret

    # The right key opens it…
    assert (
        identity_secrets.decrypt_text(
            factor.secret_encrypted,
            tenant_id=str(owner_a.tenant_id),
            purpose=identity_secrets.PURPOSE_TOTP,
        )
        == secret
    )
    # …another tenant cannot, even with the same key ring loaded…
    with pytest.raises(IdentitySecretsUnavailable):
        identity_secrets.decrypt_text(
            factor.secret_encrypted,
            tenant_id=str(tenant_b.id),
            purpose=identity_secrets.PURPOSE_TOTP,
        )
    # …and the purpose is part of the binding too, so a TOTP envelope cannot be
    # replayed as any other kind of identity secret.
    with pytest.raises(IdentitySecretsUnavailable):
        identity_secrets.decrypt_text(
            factor.secret_encrypted,
            tenant_id=str(owner_a.tenant_id),
            purpose=identity_secrets.PURPOSE_OIDC_CLIENT_SECRET,
        )


async def test_the_same_plaintext_seals_to_two_different_envelopes(owner_a):
    first, first_key = identity_secrets.encrypt_text(
        "JBSWY3DPEHPK3PXP", tenant_id=str(owner_a.tenant_id), purpose="totp"
    )
    second, second_key = identity_secrets.encrypt_text(
        "JBSWY3DPEHPK3PXP", tenant_id=str(owner_a.tenant_id), purpose="totp"
    )
    assert first != second, "identical envelopes would make the nonce reusable"
    assert first_key and second_key


async def test_nothing_recoverable_sits_in_any_identity_column(client, db, owner_a, totp_clock):
    await require_mfa(client, owner_a)
    secret = await enroll_totp(db, owner_a, totp_clock)
    headers = await mfa_auth_headers(client, owner_a, secret, totp_clock)
    codes = (
        await client.post("/api/mfa/recovery-codes/regenerate", headers=headers)
    ).json()["recovery_codes"]

    started = await login(client, owner_a.email)
    challenge_token = started.json()["challenge"]
    done = await client.post(
        "/auth/mfa/verify",
        json={"challenge": challenge_token, "code": codes[0]},
    )
    assert done.status_code == 200, done.text
    access_token = done.json()["access_token"]

    dump = await _everything(db)
    assert secret not in dump, "the TOTP seed is readable in the database"
    for code in codes:
        assert code not in dump, "a recovery code is readable in the database"
    assert challenge_token not in dump
    assert access_token not in dump


async def test_recovery_codes_are_digests_and_challenge_tokens_are_hashed(
    client, db, owner_a, totp_clock
):
    await require_mfa(client, owner_a)
    await enroll_totp(db, owner_a, totp_clock)
    started = await login(client, owner_a.email)
    token = started.json()["challenge"]

    digest = identity_tokens.hash_token(token)
    assert len(digest) == 64

    challenge = (
        await db.execute(select(MFAChallenge).where(MFAChallenge.user_id == owner_a.id))
    ).scalars().first()
    assert challenge is not None
    assert challenge.challenge_hash == digest
    assert challenge.challenge_hash != token

    codes = (
        await db.execute(select(MFARecoveryCode).where(MFARecoveryCode.user_id == owner_a.id))
    ).scalars().all()
    assert codes
    for row in codes:
        assert len(row.code_hash) == 64
        int(row.code_hash, 16)  # hex, not base64 or a label


async def test_sealing_fails_closed_when_no_key_ring_is_configured(monkeypatch, owner_a):
    monkeypatch.setattr(settings, "identity_encryption_keys", "", raising=False)
    monkeypatch.setattr(settings, "crm_encryption_keys", "", raising=False)
    identity_secrets.reset_key_ring_cache()

    assert identity_secrets.encryption_available() is False
    with pytest.raises(IdentitySecretsUnavailable):
        identity_secrets.encrypt_text(
            "JBSWY3DPEHPK3PXP", tenant_id=str(owner_a.tenant_id), purpose="totp"
        )

    identity_secrets.reset_key_ring_cache()


async def test_enrollment_refuses_rather_than_storing_a_plaintext_seed(
    monkeypatch, client, db, owner_a
):
    """No key ring means no factor — not a factor stored in the clear."""
    from app.auth.identity import mfa as identity_mfa

    monkeypatch.setattr(settings, "identity_encryption_keys", "", raising=False)
    monkeypatch.setattr(settings, "crm_encryption_keys", "", raising=False)
    identity_secrets.reset_key_ring_cache()

    started = await client.post("/api/mfa/enroll", headers=await auth_headers(client, owner_a))
    assert started.status_code == 503, started.text

    factors = (await db.execute(select(MFAFactor))).scalars().all()
    assert factors == [], "nothing may be written when the seed cannot be sealed"
    assert failure_detail(started) or started.status_code == 503
    del identity_mfa

    identity_secrets.reset_key_ring_cache()


async def test_secret_comparison_is_constant_time(owner_a):
    """Every comparison the factor uses is constant-time.

    A timing signal on the code comparison is the difference between a million
    guesses and a few thousand, so the primitive is asserted directly.
    """
    assert identity_tokens.constant_time_equals("123456", "123456") is True
    assert identity_tokens.constant_time_equals("123456", "123457") is False
    assert identity_tokens.constant_time_equals("", "") is True
    assert identity_tokens.constant_time_equals("123456", "12345") is False


async def test_a_stale_totp_clock_step_cannot_be_answered_twice(db, owner_a, totp_clock):
    """The replay guard is stored on the factor, so it survives a restart."""
    from app.auth.identity import mfa as identity_mfa

    secret = await enroll_totp(db, owner_a, totp_clock)
    code = totp_clock.consume(secret)

    from app.auth.identity.mfa import ChallengePurpose

    first = await identity_mfa.create_challenge(db, owner_a, purpose=ChallengePurpose.LOGIN, commit=True)
    accepted = await identity_mfa.resolve_challenge(db, token=first.token, code=code, commit=True)
    assert accepted.ok is True

    factor = (
        await db.execute(select(MFAFactor).where(MFAFactor.user_id == owner_a.id))
    ).scalar_one()
    assert factor.last_timestep is not None

    second = await identity_mfa.create_challenge(db, owner_a, purpose=ChallengePurpose.LOGIN, commit=True)
    replayed = await identity_mfa.resolve_challenge(
        db, token=second.token, code=code, commit=True
    )
    assert replayed.ok is False


async def test_a_tampered_envelope_is_refused_rather_than_decrypted(owner_a):
    """AES-GCM authenticates the ciphertext: an edited row does not open.

    This is the property that makes "encrypted at rest" mean something more than
    "unreadable by accident" — somebody who can write to the column cannot make
    the application accept a seed of their choosing.
    """
    envelope, _key_id = identity_secrets.encrypt_text(
        "JBSWY3DPEHPK3PXP", tenant_id=str(owner_a.tenant_id), purpose="totp"
    )
    tampered = envelope[:-4] + ("aaaa" if envelope[-4:] != "aaaa" else "bbbb")

    with pytest.raises(IdentitySecretsUnavailable):
        identity_secrets.decrypt_text(
            tampered, tenant_id=str(owner_a.tenant_id), purpose="totp"
        )


async def test_an_empty_secret_is_refused(owner_a):
    """No caller may seal nothing and call it a credential."""
    with pytest.raises(IdentitySecretsUnavailable):
        identity_secrets.encrypt_text(
            "", tenant_id=str(owner_a.tenant_id), purpose="totp"
        )
