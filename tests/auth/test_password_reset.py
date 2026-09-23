"""Password reset, driven through the HTTP surface.

A reset flow is a public, unauthenticated endpoint that hands out a credential
for somebody's account, so almost everything asserted here is about what the
endpoint *does not* reveal or allow:

* every request gets the same 202 and the same message, whether or not the
  address exists and whether or not the account is active;
* the token is single-use and expiring, and it is delivered by email — the only
  way a test can read it back is the development transport, which a production
  boot refuses;
* a completed reset ends every session, because the whole reason to reset a
  password is that somebody else may have had it;
* a reset token is not a verification token: spending one for the other is
  refused, which is what stops a reset link being a way to prove an address;
* the request path is rate limited per address and per address-of-caller.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.auth.identity import email as identity_email
from app.auth.identity.models import PasswordResetToken, UserSession
from app.core.config import settings
from app.db.models import AuditAction, AuditLog, User, UserRole
from tests.conftest import TEST_PASSWORD, auth_headers, login, make_user

pytestmark = pytest.mark.asyncio

NEW_PASSWORD = "Entirely-Different-Passphrase-7!"


@pytest.fixture(autouse=True)
def _development_tokens(monkeypatch):
    """Read the reset link back from the response instead of a mailbox.

    This is the documented development escape hatch (`IDENTITY_DEBUG_TOKENS`
    with the log transport, refused in production by `Settings.validate_security`),
    so exercising it in tests is the same code path an operator uses locally.
    """
    monkeypatch.setattr(settings, "identity_debug_tokens", True)
    monkeypatch.setattr(settings, "email_transport", "log")


async def _request_reset(client, email: str):
    return await client.post("/auth/password-reset/request", json={"email": email})


async def _token_for(client, email: str) -> str:
    response = await _request_reset(client, email)
    assert response.status_code == 202, response.text
    token = response.json().get("development_token")
    assert token, "the development transport must return the link"
    return token


async def test_the_same_answer_comes_back_for_a_real_and_an_unknown_address(client, db, tenant_a):
    known = await make_user(db, tenant_a, UserRole.AGENT, email="known@example.com")

    first = await _request_reset(client, known.email)
    second = await _request_reset(client, "nobody@example.com")
    third = await _request_reset(client, "not-even-an-account@example.com")

    assert first.status_code == second.status_code == third.status_code == 202
    assert first.json()["message"] == second.json()["message"] == third.json()["message"]
    assert first.json()["status"] == "accepted"

    # The inactive-account case answers identically too.
    dormant = await make_user(db, tenant_a, UserRole.AGENT, email="dormant@example.com", active=False)
    fourth = await _request_reset(client, dormant.email)
    assert fourth.status_code == 202
    assert fourth.json()["message"] == first.json()["message"]


async def test_an_inactive_account_is_sent_nothing(client, db, tenant_a):
    dormant = await make_user(db, tenant_a, UserRole.AGENT, email="dormant@example.com", active=False)
    token = await _request_reset(client, dormant.email)
    assert token.status_code == 202

    rows = (
        await db.execute(select(PasswordResetToken).where(PasswordResetToken.user_id == dormant.id))
    ).scalars().all()
    assert rows == [], "a disabled account must not be handed a reset link"


async def test_a_reset_ends_every_session_and_works_once(client, db, tenant_a, owner_a):
    """The two things that make a reset a reset: the new password works, the
    old sessions do not, and the link cannot be spent twice."""
    user = await make_user(db, tenant_a, UserRole.AGENT, email="reset@example.com")
    first = await auth_headers(client, user)
    second = await auth_headers(client, user)
    assert (await client.get("/api/sessions", headers=first)).status_code == 200

    token = await _token_for(client, user.email)
    done = await client.post(
        "/auth/password-reset/confirm", json={"token": token, "new_password": NEW_PASSWORD}
    )
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "password_changed"
    assert done.json()["revoked_sessions"] >= 2

    # Every session that existed before the reset is gone.
    assert (await client.get("/api/sessions", headers=first)).status_code == 401
    assert (await client.get("/api/sessions", headers=second)).status_code == 401
    live = (
        await db.execute(
            select(UserSession).where(UserSession.user_id == user.id).execution_options(populate_existing=True)
        )
    ).scalars().all()
    assert all(row.revoked_at is not None for row in live)

    # The old password is refused and the new one works.
    assert (await login(client, user.email, TEST_PASSWORD)).status_code == 401
    assert (await login(client, user.email, NEW_PASSWORD)).status_code == 200

    # The link is spent.
    reused = await client.post(
        "/auth/password-reset/confirm", json={"token": token, "new_password": NEW_PASSWORD}
    )
    assert reused.status_code == 400, reused.text


async def test_an_expired_link_is_refused(client, db, tenant_a):
    user = await make_user(db, tenant_a, UserRole.AGENT, email="expired@example.com")
    token = await _token_for(client, user.email)

    row = (
        await db.execute(
            select(PasswordResetToken)
            .where(PasswordResetToken.user_id == user.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().one()
    row.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
    await db.commit()

    refused = await client.post(
        "/auth/password-reset/confirm", json={"token": token, "new_password": NEW_PASSWORD}
    )
    assert refused.status_code == 400, refused.text
    assert refused.json()["detail"]["code"] == "token_invalid"


async def test_a_reset_token_is_not_an_email_verification_token(client, db, tenant_a):
    user = await make_user(db, tenant_a, UserRole.AGENT, email="interchange@example.com")
    token = await _token_for(client, user.email)

    refused = await client.post("/auth/email-verification/confirm", json={"token": token})
    assert refused.status_code == 400, refused.text

    # And the reverse: a verification token cannot set a password.
    verified = await identity_email.issue_verification_token(
        db, user_id=user.id, tenant_id=tenant_a.id, email=user.email, commit=True
    )
    refused_again = await client.post(
        "/auth/password-reset/confirm",
        json={"token": verified.plaintext, "new_password": NEW_PASSWORD},
    )
    assert refused_again.status_code == 400, refused_again.text


async def test_the_password_policy_applies_to_a_reset_link(client, db, tenant_a):
    user = await make_user(db, tenant_a, UserRole.AGENT, email="policy@example.com")
    token = await _token_for(client, user.email)

    refused = await client.post(
        "/auth/password-reset/confirm", json={"token": token, "new_password": "short"}
    )
    assert refused.status_code == 400, refused.text
    assert "password" in refused.text.lower()

    # The weak attempt did not change the password.
    assert (await login(client, user.email, TEST_PASSWORD)).status_code == 200


async def test_a_token_for_another_account_cannot_be_used_against_yours(client, db, tenant_a):
    """The token names one user; confirming it must only ever touch that user."""
    target = await make_user(db, tenant_a, UserRole.AGENT, email="target@example.com")
    bystander = await make_user(db, tenant_a, UserRole.AGENT, email="bystander@example.com")

    token = await _token_for(client, target.email)
    done = await client.post(
        "/auth/password-reset/confirm", json={"token": token, "new_password": NEW_PASSWORD}
    )
    assert done.status_code == 200, done.text

    bystander_row = (
        await db.execute(
            select(User).where(User.id == bystander.id).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert bystander_row.password_changed_at is None, "the bystander's password was not touched"
    assert (await login(client, bystander.email, TEST_PASSWORD)).status_code == 200


async def test_requests_are_rate_limited_per_address(client, db, tenant_a):
    user = await make_user(db, tenant_a, UserRole.AGENT, email="flood@example.com")
    seen = 0
    limited = None
    for _ in range(8):
        response = await _request_reset(client, user.email)
        if response.status_code == 429:
            limited = response
            break
        assert response.status_code == 202
        seen += 1
    assert seen >= 3, "a few requests are allowed"
    assert limited is not None, "the limiter must eventually stop the flood"
    assert "retry-after" in {key.lower() for key in limited.headers}


async def test_the_flow_is_audited_without_ever_recording_the_token(client, db, tenant_a):
    user = await make_user(db, tenant_a, UserRole.AGENT, email="audited@example.com")
    token = await _token_for(client, user.email)
    done = await client.post(
        "/auth/password-reset/confirm", json={"token": token, "new_password": NEW_PASSWORD}
    )
    assert done.status_code == 200, done.text

    entries = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.tenant_id == tenant_a.id,
                AuditLog.action.in_(
                    [AuditAction.PASSWORD_RESET_REQUESTED, AuditAction.PASSWORD_RESET_COMPLETED]
                ),
            )
        )
    ).scalars().all()
    actions = [row.action for row in entries]
    assert AuditAction.PASSWORD_RESET_REQUESTED in actions
    assert AuditAction.PASSWORD_RESET_COMPLETED in actions

    rendered = " ".join(f"{row.detail}" for row in entries)
    assert token not in rendered
    assert NEW_PASSWORD not in rendered
    assert {row.target_user_id for row in entries} == {user.id}, (
        "the trail names the account it acted on"
    )
    assert all(row.tenant_id == tenant_a.id for row in entries)
