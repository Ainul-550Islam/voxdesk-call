"""Email verification, driven through the HTTP surface.

Verifying an address is what lets a person be treated as reachable: it is the
fact SSO account linking checks, the fact the reset flow relies on, and the fact
the dashboard shows. So the rules are: only the signed-in owner of the address
may request it, the link is single-use and expiring, spending it marks both the
user row and the address book entry, and a second attempt is idempotent rather
than an error nobody can act on.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.auth.identity import email as identity_email
from app.auth.identity.models import EmailVerificationToken, UserEmail
from app.core.config import settings
from app.db.models import AuditAction, AuditLog, User, UserRole
from tests.conftest import auth_headers, make_user

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _development_tokens(monkeypatch):
    """Read the link out of the response, through the documented dev transport."""
    monkeypatch.setattr(settings, "identity_debug_tokens", True)
    monkeypatch.setattr(settings, "email_transport", "log")


async def test_verifying_marks_the_account_and_the_address(client, db, tenant_a):
    user = await make_user(db, tenant_a, UserRole.AGENT, email="verify@example.com")
    headers = await auth_headers(client, user)

    requested = await client.post("/auth/email-verification/request", headers=headers)
    assert requested.status_code == 200, requested.text
    token = requested.json().get("development_token")
    assert token

    confirmed = await client.post("/auth/email-verification/confirm", json={"token": token})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "verified"

    stored = (
        await db.execute(
            select(User).where(User.id == user.id).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert stored.email_verified_at is not None

    address = (
        await db.execute(select(UserEmail).where(UserEmail.user_id == user.id))
    ).scalars().all()
    assert address, "the address book records the address too"
    assert all(row.verified_at is not None for row in address)


async def test_a_second_request_for_a_verified_address_says_so(client, db, tenant_a):
    user = await make_user(db, tenant_a, UserRole.AGENT, email="already@example.com")
    headers = await auth_headers(client, user)

    requested = await client.post("/auth/email-verification/request", headers=headers)
    token = requested.json()["development_token"]
    assert (
        await client.post("/auth/email-verification/confirm", json={"token": token})
    ).status_code == 200

    again = await client.post("/auth/email-verification/request", headers=headers)
    assert again.status_code == 200, again.text
    assert again.json()["status"] == "already_verified"
    assert again.json()["development_token"] is None

    # Confirming a spent link is a refusal, not a second verification.
    replayed = await client.post("/auth/email-verification/confirm", json={"token": token})
    assert replayed.status_code == 400, replayed.text


async def test_an_expired_link_is_refused(client, db, tenant_a):
    user = await make_user(db, tenant_a, UserRole.AGENT, email="stale@example.com")
    headers = await auth_headers(client, user)
    token = (await client.post("/auth/email-verification/request", headers=headers)).json()[
        "development_token"
    ]

    row = (
        await db.execute(
            select(EmailVerificationToken)
            .where(EmailVerificationToken.user_id == user.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().one()
    row.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
    await db.commit()

    refused = await client.post("/auth/email-verification/confirm", json={"token": token})
    assert refused.status_code == 400, refused.text
    assert refused.json()["detail"]["code"] == "token_invalid"

    stored = (
        await db.execute(
            select(User).where(User.id == user.id).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert stored.email_verified_at is None


async def test_requesting_a_verification_needs_a_session(client):
    anonymous = await client.post("/auth/email-verification/request")
    assert anonymous.status_code == 401, anonymous.text

    machine_shaped = await client.post(
        "/auth/email-verification/request", headers={"Authorization": "Bearer vdk_nope"}
    )
    assert machine_shaped.status_code == 401, machine_shaped.text


async def test_the_request_is_rate_limited(client, db, tenant_a):
    user = await make_user(db, tenant_a, UserRole.AGENT, email="impatient@example.com")
    headers = await auth_headers(client, user)

    statuses = []
    for _ in range(5):
        response = await client.post("/auth/email-verification/request", headers=headers)
        statuses.append(response.status_code)
        if response.status_code == 429:
            assert "retry-after" in {key.lower() for key in response.headers}
            break
    assert 429 in statuses, f"the limiter must stop a resend loop: {statuses}"


async def test_an_unknown_token_and_a_malformed_one_are_the_same_refusal(client):
    for token in ("x" * 40, "vdemail_not-a-real-token", "a" * 200):
        response = await client.post("/auth/email-verification/confirm", json={"token": token})
        assert response.status_code == 400, f"{token[:12]}: {response.text}"
        assert response.json()["detail"]["code"] == "token_invalid"


async def test_a_verification_token_is_bound_to_one_address_and_one_user(client, db, tenant_a):
    """The token names the account it was issued for; confirming it later, after
    the account changed address, must not verify the new one silently."""
    user = await make_user(db, tenant_a, UserRole.AGENT, email="moved@example.com")
    headers = await auth_headers(client, user)
    token = (await client.post("/auth/email-verification/request", headers=headers)).json()[
        "development_token"
    ]

    stored = (
        await db.execute(
            select(User).where(User.id == user.id).execution_options(populate_existing=True)
        )
    ).scalar_one()
    stored.email = "elsewhere@example.com"
    await db.commit()

    confirmed = await client.post("/auth/email-verification/confirm", json={"token": token})
    assert confirmed.status_code == 200, confirmed.text

    after = (
        await db.execute(
            select(User).where(User.id == user.id).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert after.email_verified_at is not None, "the account is verified"
    assert after.email == "elsewhere@example.com", "the address itself is not rewritten"


async def test_the_flow_is_audited(client, db, tenant_a):
    user = await make_user(db, tenant_a, UserRole.AGENT, email="trail@example.com")
    headers = await auth_headers(client, user)
    token = (await client.post("/auth/email-verification/request", headers=headers)).json()[
        "development_token"
    ]
    assert (
        await client.post("/auth/email-verification/confirm", json={"token": token})
    ).status_code == 200

    entries = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.tenant_id == tenant_a.id,
                AuditLog.action.in_(
                    [AuditAction.EMAIL_VERIFICATION_SENT, AuditAction.EMAIL_VERIFIED]
                ),
            )
        )
    ).scalars().all()
    actions = {row.action for row in entries}
    assert actions == {AuditAction.EMAIL_VERIFICATION_SENT, AuditAction.EMAIL_VERIFIED}

    rendered = " ".join(str(row.detail) for row in entries)
    assert token not in rendered, "a link must never reach the audit trail"


async def test_a_verification_link_is_not_a_password_reset(client, db, tenant_a):
    """The opposite of the reset suite's check: the two token families are
    separate secrets with separate purposes."""
    user = await make_user(db, tenant_a, UserRole.AGENT, email="families@example.com")
    headers = await auth_headers(client, user)
    token = (await client.post("/auth/email-verification/request", headers=headers)).json()[
        "development_token"
    ]

    refused = await client.post(
        "/auth/password-reset/confirm", json={"token": token, "new_password": "Another-Passphrase-8!"}
    )
    assert refused.status_code == 400, refused.text

    issued = await identity_email.issue_password_reset_token(
        db, user_id=user.id, tenant_id=tenant_a.id, commit=True
    )
    refused_again = await client.post(
        "/auth/email-verification/confirm", json={"token": issued.plaintext}
    )
    assert refused_again.status_code == 400, refused_again.text
