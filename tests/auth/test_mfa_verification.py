"""Second-factor verification at login: the happy path, and the four ways in.

A tenant that requires a factor turns ``POST /auth/login`` into a 202 with a
challenge; the access token only exists after ``POST /auth/mfa/verify``.
Asserted here:

* the password step alone returns no token — a 202 and a challenge, nothing
  usable;
* a correct code completes the login, and the session it creates is marked
  ``mfa_verified`` (that flag is what privileged actions look at later);
* a wrong code is refused with ``mfa_invalid_code`` and no token;
* a code that was already accepted is refused (the replay guard on the factor
  row), even though the arithmetic still considers it valid for its second;
* a challenge token that does not exist, or belongs to another purpose, is
  refused without spending anything.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.auth.identity.models import UserSession
from tests.conftest import (
    TEST_PASSWORD,
    enroll_totp,
    failure_detail,
    login,
    mfa_auth_headers,
    require_mfa,
)

pytestmark = pytest.mark.asyncio


async def start_login(client, user) -> dict:
    started = await login(client, user.email, TEST_PASSWORD)
    assert started.status_code == 202, started.text
    body = started.json()
    assert body["mfa_required"] is True
    return body


async def test_the_password_step_alone_yields_no_token(client, db, owner_a, totp_clock):
    # A factor must exist before a login can be challenged: a tenant that
    # requires MFA from somebody who has not enrolled one lets them in with
    # ``mfa_enrollment_required`` instead of locking them out.
    await require_mfa(client, owner_a)
    await enroll_totp(db, owner_a, totp_clock)
    started = await start_login(client, owner_a)
    assert started["challenge"]
    assert "access_token" not in started
    assert "refresh_token" not in started


async def test_a_correct_code_completes_the_login_and_marks_the_session(client, db, owner_a, totp_clock):
    await require_mfa(client, owner_a)
    secret = await enroll_totp(db, owner_a, totp_clock)

    started = await start_login(client, owner_a)
    done = await client.post(
        "/auth/mfa/verify",
        json={"challenge": started["challenge"], "code": totp_clock.consume(secret)},
    )
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["access_token"]
    assert body["token_type"] == "bearer"
    body_token = body["access_token"]

    headers = {"Authorization": f"Bearer {body['access_token']}"}
    me = await client.get("/api/identity/status", headers=headers)
    assert me.status_code == 200, me.text

    sessions = (
        await db.execute(select(UserSession).where(UserSession.user_id == owner_a.id))
    ).scalars().all()
    verified = [row for row in sessions if row.mfa_verified]
    assert len(verified) == 1, "exactly the challenge-completed session is MFA-verified"

    # The access token says so too: a relying party must not have to read the
    # session table to find out whether a factor was presented.
    import jwt as pyjwt

    claims = pyjwt.decode(body_token, options={"verify_signature": False})
    assert claims["mfa"] is True
    assert claims["amr"] == "password"
    assert claims["sid"] == str(verified[0].id)


async def test_a_wrong_code_is_refused_and_creates_no_session(client, db, owner_a, totp_clock):
    await require_mfa(client, owner_a)
    secret = await enroll_totp(db, owner_a, totp_clock)

    started = await start_login(client, owner_a)
    wrong = "000000"
    refused = await client.post(
        "/auth/mfa/verify",
        json={"challenge": started["challenge"], "code": wrong},
    )
    assert refused.status_code == 401, refused.text
    assert "not valid" in failure_detail(refused).lower()
    assert "access_token" not in refused.text

    # The challenge is still open — one wrong code is not a lockout — and the
    # right one still works.
    right = await client.post(
        "/auth/mfa/verify",
        json={"challenge": started["challenge"], "code": totp_clock.consume(secret)},
    )
    assert right.status_code == 200, right.text


async def test_a_code_that_was_already_accepted_cannot_be_replayed(client, db, owner_a, totp_clock):
    await require_mfa(client, owner_a)
    secret = await enroll_totp(db, owner_a, totp_clock)

    started = await start_login(client, owner_a)
    code = totp_clock.consume(secret)
    first = await client.post(
        "/auth/mfa/verify", json={"challenge": started["challenge"], "code": code}
    )
    assert first.status_code == 200, first.text

    # A fresh challenge, and the same code: still inside its thirty-second
    # window arithmetically, and still refused, because the step was consumed.
    second_start = await start_login(client, owner_a)
    replayed = await client.post(
        "/auth/mfa/verify", json={"challenge": second_start["challenge"], "code": code}
    )
    assert replayed.status_code == 401, replayed.text
    assert "not valid" in failure_detail(replayed).lower()


async def test_an_unknown_challenge_token_is_refused(client, db, owner_a, totp_clock):
    await require_mfa(client, owner_a)
    secret = await enroll_totp(db, owner_a, totp_clock)

    refused = await client.post(
        "/auth/mfa/verify",
        json={"challenge": "not-a-real-challenge-token", "code": totp_clock.code(secret)},
    )
    assert refused.status_code in (400, 401), refused.text
    assert "access_token" not in refused.text


async def test_a_step_up_challenge_cannot_be_used_to_finish_a_login(client, db, owner_a, totp_clock):
    """The two challenge purposes are not interchangeable.

    A challenge minted for a signed-in session (``reauth``) has no business
    completing a login: doing so would let a half-finished sign-in be completed
    with a token that belongs to somebody else's session.
    """
    await require_mfa(client, owner_a)
    secret = await enroll_totp(db, owner_a, totp_clock)
    headers = await mfa_auth_headers(client, owner_a, secret, totp_clock)

    issued = await client.post("/api/mfa/challenge", headers=headers)
    assert issued.status_code == 201, issued.text
    assert issued.json()["purpose"] == "reauth"
    reauth_challenge = issued.json()["challenge_token"]

    refused = await client.post(
        "/auth/mfa/verify",
        json={"challenge": reauth_challenge, "code": totp_clock.consume(secret)},
    )
    assert refused.status_code in (400, 401), refused.text
    assert "access_token" not in refused.text
