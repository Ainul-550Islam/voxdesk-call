"""TOTP enrollment: by the book, and only by the book.

The enrollment is deliberately two-phase, and these tests assert the parts a
shortcut would lose:

* the seed is returned exactly once, from the call that created it, and the row
  stores only the envelope — the plaintext seed is not recoverable from the
  database without the key ring;
* a pending factor is **not** active. It does not gate a login, it does not
  satisfy a policy requirement, and an unconfirmed factor does not appear as
  ``enrolled`` in the status a client reads to decide what to render;
* confirmation needs a code from *that* seed. Confirming with a code from a
  different secret is refused and leaves the factor pending;
* the factor belongs to the user who enrolled it.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.auth.identity import secrets as identity_secrets
from app.auth.identity import totp
from app.auth.identity.models import MFAFactor
from tests.conftest import TOTPClock, auth_headers

pytestmark = pytest.mark.asyncio


async def enroll(client, user) -> dict:
    response = await client.post("/api/mfa/enroll", headers=await auth_headers(client, user))
    assert response.status_code == 201, response.text
    return response.json()


async def status(client, user) -> dict:
    response = await client.get("/api/mfa/status", headers=await auth_headers(client, user))
    assert response.status_code == 200, response.text
    return response.json()


async def test_enrollment_requires_an_authenticated_session(client):
    response = await client.post("/api/mfa/enroll")
    assert response.status_code == 401, response.text


async def test_the_seed_is_returned_once_and_stored_as_ciphertext_only(client, db, owner_a):
    started = await enroll(client, owner_a)
    secret = started["secret"]
    assert secret, started
    assert started["provisioning_uri"].startswith("otpauth://totp/")

    row = (
        await db.execute(select(MFAFactor).where(MFAFactor.user_id == owner_a.id))
    ).scalar_one()
    assert row.secret_encrypted != secret
    assert secret not in row.secret_encrypted
    assert row.secret_key_id, "the envelope must record which key sealed it"

    recovered = identity_secrets.decrypt_text(
        row.secret_encrypted,
        tenant_id=str(owner_a.tenant_id),
        purpose=identity_secrets.PURPOSE_TOTP,
    )
    assert recovered == secret

    body = await status(client, owner_a)
    assert "secret" not in body
    assert "provisioning_uri" not in body


async def test_a_pending_factor_gates_nothing_until_it_is_confirmed(client, owner_a):
    await enroll(client, owner_a)

    body = await status(client, owner_a)
    assert body["pending"] is True
    assert body["enrolled"] is False
    assert body["confirmed_at"] is None

    # Still an ordinary password login: a half-finished enrollment must not lock
    # the user out of their own account.
    from tests.conftest import login

    signed_in = await login(client, owner_a.email)
    assert signed_in.status_code == 200, signed_in.text


async def test_confirmation_activates_the_factor_and_shows_the_codes_once(client, db, owner_a):
    started = await enroll(client, owner_a)
    clock = TOTPClock(started["period_seconds"])

    confirmed = await client.post(
        "/api/mfa/enroll/confirm",
        json={"code": clock.code(started["secret"])},
        headers=await auth_headers(client, owner_a),
    )
    assert confirmed.status_code == 200, confirmed.text
    codes = confirmed.json()["recovery_codes"]
    assert len(codes) >= 5, codes
    assert len(set(codes)) == len(codes), "codes must be distinct"

    body = await status(client, owner_a)
    assert body["enrolled"] is True
    assert body["pending"] is False
    assert body["confirmed_at"] is not None
    assert body["recovery_codes_remaining"] == len(codes)
    assert body["recovery_codes_total"] == len(codes)
    # The status endpoint describes the factor; it never hands one out.
    assert "recovery_codes" not in body


async def test_confirmation_with_a_code_from_another_seed_is_refused(client, owner_a):
    started = await enroll(client, owner_a)
    clock = TOTPClock(started["period_seconds"])
    stranger = totp.generate_secret()

    refused = await client.post(
        "/api/mfa/enroll/confirm",
        json={"code": clock.code(stranger)},
        headers=await auth_headers(client, owner_a),
    )
    assert refused.status_code == 401, refused.text
    assert refused.json()["detail"]["code"] == "mfa_invalid_code"

    body = await status(client, owner_a)
    assert body["enrolled"] is False
    assert body["pending"] is True


async def test_the_provisioning_uri_identifies_the_deployment_and_the_seed(client, owner_a):
    started = await enroll(client, owner_a)
    uri = started["provisioning_uri"]

    from urllib.parse import parse_qs, unquote, urlparse

    parsed = urlparse(uri)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "otpauth"
    assert query["secret"] == [started["secret"]]
    assert query["digits"] == [str(started["digits"])]
    assert query["period"] == [str(started["period_seconds"])]
    assert query["issuer"], "an authenticator app needs to know whose code this is"
    # The account name is percent-encoded in the path: ``VoxDesk:ada%40acme.test``.
    assert owner_a.email in unquote(parsed.path)


async def test_a_factor_is_scoped_to_the_user_and_tenant_that_enrolled_it(client, db, owner_a):
    await enroll(client, owner_a)

    rows = (await db.execute(select(MFAFactor))).scalars().all()
    assert len(rows) == 1
    assert rows[0].user_id == owner_a.id
    assert rows[0].tenant_id == owner_a.tenant_id
    assert rows[0].status == "pending"
    assert rows[0].factor_type == "totp"
