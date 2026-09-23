"""Session and device invalidation.

The question this file answers is the one an incident responder asks first:
*when I end a session, is it actually over?* A session is not one record but
three things that have to move together --

* the refresh token row (stored hashed) that can mint new access tokens,
* the ``user_sessions`` row every authenticated request is checked against,
  and
* the access token the client still holds, which is valid until ``exp`` unless
  the version it was minted with is bumped.

A logout that closes only the first leaves the other two alive, so every test
here asserts on the *observable* consequence (the old token is refused) and on
the stored reason, which is what the audit trail and the sessions screen read.

Everything runs through the real routes and the real services against a real
database (see ``tests/conftest.py``).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.auth.identity import email as identity_email
from app.auth.identity import mfa as identity_mfa
from app.auth.identity import policies
from app.auth.identity import sessions as identity_sessions
from app.auth.identity.models import (
    ChallengePurpose,
    PasswordResetToken,
    UserSession,
)
from app.db.models import AuditAction, AuditLog, RefreshToken, UserRole
from tests.conftest import (
    TEST_PASSWORD,
    auth_headers,
    enroll_totp,
    login,
    make_tenant,
    make_user,
    mfa_auth_headers,
    require_mfa,
)

pytestmark = pytest.mark.asyncio


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _second_device(app) -> AsyncClient:
    """A second browser: its own client, therefore its own cookie jar."""
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _rows(db, user_id) -> list[UserSession]:
    """This user's session rows, re-read rather than taken from the identity map.

    ``db`` is the test's session while the routes commit through their own; a
    plain re-select would hand back the instances SQLAlchemy already holds with
    their pre-request values, and every assertion below would be about the past.
    """
    return list(
        (
            await db.execute(
                select(UserSession)
                .where(UserSession.user_id == user_id)
                .execution_options(populate_existing=True)
            )
        ).scalars().all()
    )


async def _live_rows(db, user_id) -> list[UserSession]:
    return [row for row in await _rows(db, user_id) if row.is_live(_now())]


def _reasons(rows) -> set[str]:
    return {(row.revoked_reason or "") for row in rows if row.revoked_at is not None}


def _session_id(headers: dict[str, str]) -> uuid.UUID:
    """The session an access token belongs to, read the way a route reads it."""
    from app.auth.jwt import decode_access_token

    token = headers["Authorization"].split(" ", 1)[1]
    return decode_access_token(token).session_id


# ================================================================ logout ===


async def test_logging_out_one_device_leaves_the_other_signed_in(client, app, db):
    tenant = await make_tenant(db, "Two device tenant")
    user = await make_user(db, tenant, UserRole.AGENT)
    first = await auth_headers(client, user)

    async with _second_device(app) as second:
        second_headers = await auth_headers(second, user)
        assert len(await _live_rows(db, user.id)) == 2

        assert (await client.post("/auth/logout", headers=first)).status_code == 204

        # The device that logged out is refused on the access token it still
        # holds -- logout is not a promise about the next fifteen minutes.
        assert (await client.get("/api/sessions", headers=first)).status_code == 401

        # The other device keeps both its access token and its ability to
        # refresh: one device signing out is not a user-wide event.
        assert (await second.get("/api/sessions", headers=second_headers)).status_code == 200
        assert (await second.post("/auth/refresh", headers=second_headers)).status_code == 200

    assert _reasons(await _rows(db, user.id)) == {"logout"}
    assert len(await _live_rows(db, user.id)) == 1


async def test_replaying_a_spent_refresh_token_ends_every_session(client, app, db):
    """Refresh-token reuse is treated as theft, not as a stale cookie.

    Every session of that user dies, including the innocent one on another
    device, because "the replay was probably harmless" is not knowable. The
    denial is recorded so an operator can see why the other device signed out.
    """
    from app.api.auth_routes import REFRESH_COOKIE

    tenant = await make_tenant(db, "Reuse tenant")
    user = await make_user(db, tenant, UserRole.AGENT)
    first = await auth_headers(client, user)
    version_before = user.token_version

    async with _second_device(app) as second:
        second_headers = await auth_headers(second, user)

        spent = second.cookies.get(REFRESH_COOKIE)
        assert spent, "a login sets the refresh cookie"
        assert (await second.post("/auth/refresh")).status_code == 200, "the first use rotates"

        played_back = await second.post("/auth/refresh", json={"refresh_token": spent})
        assert played_back.status_code == 401
        assert played_back.json()["detail"] == "Invalid refresh token."

        assert (await second.get("/api/sessions", headers=second_headers)).status_code == 401
        assert (await client.get("/api/sessions", headers=first)).status_code == 401

    assert await _live_rows(db, user.id) == []
    await db.refresh(user)
    assert user.token_version > version_before, "access tokens must stop verifying"

    denied = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.action == AuditAction.AUTHZ_DENIED,
                AuditLog.actor_user_id == user.id,
            )
        )
    ).scalars().all()
    assert any(row.detail.get("reason") == "refresh_token_reuse" for row in denied)


async def test_logging_out_everywhere_ends_every_session_and_its_access_tokens(client, app, db):
    tenant = await make_tenant(db, "Logout all tenant")
    user = await make_user(db, tenant, UserRole.AGENT)
    first = await auth_headers(client, user)
    version_before = user.token_version

    async with _second_device(app) as second:
        second_headers = await auth_headers(second, user)

        assert (await client.post("/auth/logout-all", headers=first)).status_code == 204

        assert (await client.get("/api/sessions", headers=first)).status_code == 401
        assert (await second.get("/api/sessions", headers=second_headers)).status_code == 401
        assert (await second.post("/auth/refresh", headers=second_headers)).status_code == 401

    assert await _live_rows(db, user.id) == []
    rows = await _rows(db, user.id)
    assert rows and all(row.revoked_at is not None for row in rows)
    assert _reasons(rows) == {"logout_all"}

    await db.refresh(user)
    assert user.token_version > version_before, "old access tokens must stop verifying"


async def test_refresh_tokens_do_not_outlive_the_session_they_were_minted_for(client, db):
    tenant = await make_tenant(db, "Refresh cascade tenant")
    user = await make_user(db, tenant, UserRole.AGENT)
    headers = await auth_headers(client, user)

    row = (await _live_rows(db, user.id))[0]
    await identity_sessions.revoke_session(db, row, reason="user_revoked", commit=True)

    tokens = (
        await db.execute(select(RefreshToken).where(RefreshToken.user_id == user.id))
    ).scalars().all()
    assert tokens, "the login issued a refresh token"
    assert all(
        token.revoked_at is not None for token in tokens
    ), "a revoked session must revoke the refresh tokens issued for it"
    assert (await client.post("/auth/refresh", headers=headers)).status_code == 401


# ====================================================== device management ===


async def test_revoking_another_users_session_is_not_possible(client, app, db):
    tenant = await make_tenant(db, "Revoke other tenant")
    actor = await make_user(db, tenant, UserRole.OWNER)
    target = await make_user(db, tenant, UserRole.AGENT)
    actor_headers = await auth_headers(client, actor)

    async with _second_device(app) as target_device:
        target_headers = await auth_headers(target_device, target)
        target_session = (await _live_rows(db, target.id))[0]

        response = await client.delete(
            f"/api/sessions/{target_session.id}", headers=actor_headers
        )
        assert response.status_code == 404, "another user's session is not addressable"

        assert (await target_device.get("/api/sessions", headers=target_headers)).status_code == 200

    await db.refresh(target_session)
    assert target_session.revoked_at is None


async def test_a_session_id_from_another_tenant_is_not_addressable(client, db):
    home = await make_tenant(db, "Home tenant")
    other = await make_tenant(db, "Other tenant")
    outsider = await make_user(db, other, UserRole.OWNER)
    victim = await make_user(db, home, UserRole.AGENT)

    victim_session = await identity_sessions.create_session(
        db,
        victim,
        policy=await policies.load_policy(db, home.id),
        ip_address="203.0.113.5",
        user_agent="pytest/1.0",
        commit=True,
    )

    response = await client.delete(
        f"/api/sessions/{victim_session.id}", headers=await auth_headers(client, outsider)
    )
    assert response.status_code == 404
    await db.refresh(victim_session)
    assert victim_session.revoked_at is None


async def test_sign_out_other_devices_keeps_only_the_caller(client, app, db):
    tenant = await make_tenant(db, "Revoke others tenant")
    user = await make_user(db, tenant, UserRole.AGENT)
    headers = await auth_headers(client, user)

    async with _second_device(app) as other:
        other_headers = await auth_headers(other, user)
        assert len(await _live_rows(db, user.id)) == 2

        response = await client.post("/api/sessions/revoke-others", headers=headers)
        assert response.status_code == 200
        assert response.json()["revoked"] == 1

        assert (await client.get("/api/sessions", headers=headers)).status_code == 200
        # The other device is out immediately -- on the token it is holding,
        # not only the next time it tries to refresh.
        assert (await other.get("/api/sessions", headers=other_headers)).status_code == 401

    live = await _live_rows(db, user.id)
    assert len(live) == 1, "exactly the caller's session survives"
    assert str(live[0].id) == str(_session_id(headers))
    assert _reasons(await _rows(db, user.id)) == {"user_revoked_others"}


async def test_renaming_a_session_is_scoped_to_the_owner(client, app, db):
    tenant = await make_tenant(db, "Rename tenant")
    actor = await make_user(db, tenant, UserRole.AGENT)
    target = await make_user(db, tenant, UserRole.AGENT)
    actor_headers = await auth_headers(client, actor)

    async with _second_device(app) as target_device:
        await auth_headers(target_device, target)
        target_session = (await _live_rows(db, target.id))[0]

        response = await client.patch(
            f"/api/sessions/{target_session.id}",
            json={"label": "someone else's laptop"},
            headers=actor_headers,
        )
        assert response.status_code == 404

        own = (await _live_rows(db, actor.id))[0]
        assert (
            await client.patch(
                f"/api/sessions/{own.id}",
                json={"label": "my laptop"},
                headers=actor_headers,
            )
        ).status_code == 200
        await db.refresh(own)
        assert own.device_label == "my laptop"

    await db.refresh(target_session)
    assert target_session.device_label != "someone else's laptop"


# =============================================== credential-change cascade ===


async def test_a_completed_password_reset_ends_every_session(client, app, db):
    tenant = await make_tenant(db, "Reset cascade tenant")
    user = await make_user(db, tenant, UserRole.AGENT)
    headers = await auth_headers(client, user)

    async with _second_device(app) as other:
        other_headers = await auth_headers(other, user)

        token = await identity_email.issue_password_reset_token(
            db, user_id=user.id, tenant_id=tenant.id, commit=True
        )
        new_password = "Brand-New-Passphrase-4!"
        response = await client.post(
            "/auth/password-reset/confirm",
            json={"token": token.plaintext, "new_password": new_password},
        )
        assert response.status_code == 200, response.text
        assert response.json()["revoked_sessions"] >= 1

        assert (await client.get("/api/sessions", headers=headers)).status_code == 401
        assert (await other.get("/api/sessions", headers=other_headers)).status_code == 401
        assert await _live_rows(db, user.id) == []
        assert _reasons(await _rows(db, user.id)) == {"password_reset"}

        # The new password works, the old one does not, and the reset does not
        # leave the account signed out for good.
        assert (await login(other, user.email, new_password)).status_code == 200
        assert (await login(other, user.email)).status_code == 401

    assert len(await _live_rows(db, user.id)) == 1


async def test_a_second_reset_with_the_same_token_is_refused(client, app, db):
    tenant = await make_tenant(db, "Single use reset tenant")
    user = await make_user(db, tenant, UserRole.AGENT)
    token = await identity_email.issue_password_reset_token(
        db, user_id=user.id, tenant_id=tenant.id, commit=True
    )
    body = {"token": token.plaintext, "new_password": "Another-Passphrase-7!"}
    assert (await client.post("/auth/password-reset/confirm", json=body)).status_code == 200
    assert (await client.post("/auth/password-reset/confirm", json=body)).status_code != 200


async def test_disabling_a_factor_ends_every_other_session(client, app, db, totp_clock):
    tenant = await make_tenant(db, "Disable factor tenant")
    owner = await make_user(db, tenant, UserRole.OWNER)
    user = await make_user(db, tenant, UserRole.AGENT)
    await require_mfa(client, owner)
    secret = await enroll_totp(db, user, totp_clock)

    # The device that will disable the factor signs in *with* the factor.
    holder_headers = await mfa_auth_headers(client, user, secret, totp_clock)

    async with _second_device(app) as other:
        other_headers = await mfa_auth_headers(other, user, secret, totp_clock)
        assert len(await _live_rows(db, user.id)) == 2

        assert (await client.post("/api/mfa/disable", headers=holder_headers)).status_code == 204

        assert (await other.get("/api/sessions", headers=other_headers)).status_code == 401
        assert (await client.get("/api/sessions", headers=holder_headers)).status_code == 200

    assert _reasons(await _rows(db, user.id)) == {"mfa_disabled"}


async def test_an_admin_reset_of_someone_elses_factor_ends_all_their_sessions(
    client, app, db, totp_clock
):
    tenant = await make_tenant(db, "Admin reset cascade tenant")
    owner = await make_user(db, tenant, UserRole.OWNER)
    target = await make_user(db, tenant, UserRole.ADMIN)
    owner_headers = await auth_headers(client, owner)
    await require_mfa(client, owner)
    secret = await enroll_totp(db, target, totp_clock)

    async with _second_device(app) as target_device:
        target_headers = await mfa_auth_headers(target_device, target, secret, totp_clock)

        response = await client.post(
            f"/api/mfa/users/{target.id}/reset", json={}, headers=owner_headers
        )
        assert response.status_code == 204, response.text

        assert (await target_device.get("/api/sessions", headers=target_headers)).status_code == 401

    assert await _live_rows(db, target.id) == []
    assert _reasons(await _rows(db, target.id)) == {"mfa_reset_by_admin"}

    # A reset aimed at somebody else does not touch the operator's own session.
    assert (await client.get("/api/sessions", headers=owner_headers)).status_code == 200


# =================================================== idle / expiry / audit ===


async def test_an_idle_session_is_no_longer_usable(client, db):
    tenant = await make_tenant(db, "Idle tenant")
    user = await make_user(db, tenant, UserRole.AGENT)
    headers = await auth_headers(client, user)

    row = (await _live_rows(db, user.id))[0]
    row.idle_expires_at = _now() - timedelta(minutes=1)
    await db.commit()

    assert (await client.get("/api/sessions", headers=headers)).status_code == 401
    assert (await client.post("/auth/refresh", headers=headers)).status_code == 401
    assert await _live_rows(db, user.id) == []


async def test_an_expired_session_is_no_longer_usable(client, db):
    tenant = await make_tenant(db, "Expired tenant")
    user = await make_user(db, tenant, UserRole.AGENT)
    headers = await auth_headers(client, user)

    row = (await _live_rows(db, user.id))[0]
    row.expires_at = _now() - timedelta(seconds=1)
    await db.commit()

    assert (await client.get("/api/sessions", headers=headers)).status_code == 401


async def test_a_new_address_with_a_live_session_raises_a_security_event(db):
    """The "was that me?" event.

    A first sign-in is not suspicious. A second one, from an address the user
    has never used while another device is already signed in, is worth telling
    somebody about -- it is the only signal a tenant gets that a password was
    used from somewhere else.
    """
    tenant = await make_tenant(db, "Suspicious tenant")
    user = await make_user(db, tenant, UserRole.AGENT)
    policy = await policies.load_policy(db, tenant.id)

    async def open_session(ip: str, agent: str) -> None:
        await identity_sessions.create_session(
            db, user, policy=policy, ip_address=ip, user_agent=agent, commit=True
        )

    async def events() -> list[AuditLog]:
        return list(
            (
                await db.execute(
                    select(AuditLog).where(
                        AuditLog.action == AuditAction.SESSION_SUSPICIOUS,
                        AuditLog.target_user_id == user.id,
                    )
                )
            ).scalars().all()
        )

    await open_session("198.51.100.10", "Mozilla/5.0 (Macintosh) pytest")
    assert await events() == [], "a first sign-in is ordinary and must not be reported"

    await open_session("198.51.100.10", "Mozilla/5.0 (Macintosh) pytest")
    assert await events() == [], "the same address is not a new address"

    await open_session("203.0.113.77", "Mozilla/5.0 (Windows NT) pytest")
    raised = await events()
    assert len(raised) == 1
    assert raised[0].tenant_id == tenant.id
    assert raised[0].detail["reason"] == "new_ip_address"
    assert "203.0.113.77" not in str(raised[0].detail), "the event does not echo the address"


async def test_an_ended_session_disappears_from_the_session_list(client, app, db):
    tenant = await make_tenant(db, "Session list tenant")
    user = await make_user(db, tenant, UserRole.AGENT)
    headers = await auth_headers(client, user)

    async with _second_device(app) as other:
        other_headers = await auth_headers(other, user)
        ended = [
            row for row in await _live_rows(db, user.id) if row.id == _session_id(other_headers)
        ][0]
        assert (await other.post("/auth/logout", headers=other_headers)).status_code == 204

        listing = await client.get("/api/sessions", headers=headers)
        assert listing.status_code == 200, listing.text
        payload = listing.json()
        sessions = payload["sessions"] if isinstance(payload, dict) else payload
        listed = {str(item["id"]) for item in sessions}
        assert str(ended.id) not in listed
        assert str((await _live_rows(db, user.id))[0].id) in listed


async def test_the_password_reset_request_never_reveals_whether_an_account_exists(client, db):
    tenant = await make_tenant(db, "Enumeration tenant")
    user = await make_user(db, tenant, UserRole.AGENT)

    known = await client.post("/auth/password-reset/request", json={"email": user.email})
    unknown = await client.post(
        "/auth/password-reset/request",
        json={"email": f"nobody-{uuid.uuid4().hex[:8]}@example.com"},
    )
    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json(), "the response must not depend on the account existing"

    tokens = (
        await db.execute(select(PasswordResetToken).where(PasswordResetToken.user_id == user.id))
    ).scalars().all()
    assert len(tokens) == 1, "a reset token is issued for a real account"
    assert TEST_PASSWORD not in (tokens[0].token_hash or ""), "the token is stored hashed"



# ==================================================== step-up reauth (HTTP) ==
#
# ``POST /api/mfa/challenge`` and ``POST /api/mfa/verify`` are the pair the
# dashboard uses when a privileged action answers 428. The service-level MFA
# tests cover the machinery; these cover the *route*, which is where an enum
# mistake or a missing binding check hides -- the only caller who reaches that
# code is somebody who already has a factor, which is exactly the caller a
# unit test forgets to build.


async def test_step_up_with_a_challenge_verifies_the_calling_session(
    client, db, tenant_a, owner_a, totp_clock
):
    """The 428 flow end to end: ask, answer, and the session is verified."""
    secret = await enroll_totp(db, owner_a, totp_clock)
    headers = await auth_headers(client, owner_a)

    before = await client.get("/api/identity/status", headers=headers)
    assert before.status_code == 200, before.text
    assert before.json()["session"]["mfa_verified"] is False
    assert before.json()["reauth"]["has_factor"] is True

    issued = await client.post("/api/mfa/challenge", headers=headers)
    assert issued.status_code == 201, issued.text
    challenge = issued.json()
    assert challenge["purpose"] == "reauth"
    assert challenge["challenge_token"]

    code = totp_clock.consume(secret)
    stepped = await client.post(
        "/api/mfa/verify",
        json={"challenge": challenge["challenge_token"], "code": code},
        headers=headers,
    )
    assert stepped.status_code == 200, stepped.text
    assert stepped.json() == {
        "ok": True,
        "mfa_verified": True,
        "used_recovery_code": False,
    }

    after = await client.get("/api/identity/status", headers=headers)
    assert after.json()["session"]["mfa_verified"] is True

    # Spent in both directions: the challenge is single use even with a correct
    # code, so a replayed request cannot re-verify anything.
    replay = await client.post(
        "/api/mfa/verify",
        json={"challenge": challenge["challenge_token"], "code": code},
        headers=headers,
    )
    assert replay.status_code == 400, replay.text
    assert replay.json()["detail"]["code"] == "mfa_error"


async def test_step_up_refuses_a_wrong_code_without_verifying_the_session(
    client, db, tenant_a, owner_a, totp_clock
):
    secret = await enroll_totp(db, owner_a, totp_clock)
    headers = await auth_headers(client, owner_a)
    issued = await client.post("/api/mfa/challenge", headers=headers)
    assert issued.status_code == 201, issued.text
    assert secret  # the factor is real; the code below simply is not

    wrong = await client.post(
        "/api/mfa/verify",
        json={"challenge": issued.json()["challenge_token"], "code": "000000"},
        headers=headers,
    )
    assert wrong.status_code == 401, wrong.text
    assert wrong.json()["detail"]["code"] == "mfa_invalid_code"

    status = await client.get("/api/identity/status", headers=headers)
    assert status.json()["session"]["mfa_verified"] is False


async def test_a_login_challenge_cannot_be_spent_as_a_step_up(
    client, db, tenant_a, owner_a, totp_clock
):
    """Purpose is checked, so a half-finished login cannot elevate a session.

    A LOGIN challenge is minted for somebody who has not finished signing in.
    If the step-up route accepted it, that half-finished login -- which lives in
    a browser the attacker may be driving -- would become a way to raise the
    assurance of a *different* session.
    """
    secret = await enroll_totp(db, owner_a, totp_clock)
    headers = await auth_headers(client, owner_a)

    login_challenge = await identity_mfa.create_challenge(
        db, owner_a, purpose=ChallengePurpose.LOGIN, commit=True
    )

    refused = await client.post(
        "/api/mfa/verify",
        json={
            "challenge": login_challenge.token,
            "code": totp_clock.consume(secret),
        },
        headers=headers,
    )
    assert refused.status_code == 401, refused.text
    assert refused.json()["detail"]["code"] == "mfa_invalid_challenge"

    status = await client.get("/api/identity/status", headers=headers)
    assert status.json()["session"]["mfa_verified"] is False


async def test_a_challenge_from_another_session_cannot_be_spent(
    client, app, db, tenant_a, owner_a, totp_clock
):
    """The challenge belongs to the session that asked for it."""
    secret = await enroll_totp(db, owner_a, totp_clock)
    headers_a = await auth_headers(client, owner_a)
    issued = await client.post("/api/mfa/challenge", headers=headers_a)
    assert issued.status_code == 201, issued.text

    code = totp_clock.consume(secret)
    headers_b = await auth_headers(_second_device(app), owner_a)

    refused = await client.post(
        "/api/mfa/verify",
        json={"challenge": issued.json()["challenge_token"], "code": code},
        headers=headers_b,
    )
    assert refused.status_code == 401, refused.text
    assert refused.json()["detail"]["code"] == "mfa_invalid_challenge"

    # Neither side was elevated, and the challenge was not consumed by the
    # refusal: the session that asked for it can still answer it.
    for session_headers in (headers_a, headers_b):
        status = await client.get("/api/identity/status", headers=session_headers)
        assert status.json()["session"]["mfa_verified"] is False

    stepped = await client.post(
        "/api/mfa/verify",
        json={"challenge": issued.json()["challenge_token"], "code": code},
        headers=headers_a,
    )
    assert stepped.status_code == 200, stepped.text
