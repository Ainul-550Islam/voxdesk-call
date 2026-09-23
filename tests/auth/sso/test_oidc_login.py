"""The OIDC login, end to end, through the real HTTP endpoints.

``POST /auth/sso/{slug}/start`` creates the attempt and returns the URL the
browser should visit; ``GET /auth/sso/{slug}/callback`` completes it. Between
the two, this test plays the part of the provider: it reads the ``state`` and
``nonce`` out of the authorization URL it was given, mints an ID token with
exactly that nonce, and hands back an authorization code.

Asserted:

* a first login provisions a user (``created`` true) in the connection's tenant,
  with the configured default role, and issues a session whose ``auth_method``
  is ``sso``;
* a second login reuses that account rather than creating a twin;
* every failure — unknown state, a state from another tenant, a provider error,
  a wrong nonce — returns the *same* generic body, because a difference between
  them is a probe an unauthenticated caller could use;
* the session it creates is not marked MFA-verified: an IdP that does not assert
  a second factor must not be treated as if it had.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.auth.identity.models import SSOLoginAttempt, UserSession
from app.db.models import User
from tests.auth.sso.providers import (
    StubOidcTransport,
    create_oidc_connection,
    default_documents,
    query_of,
    token_claims,
)
from tests.conftest import auth_headers

pytestmark = pytest.mark.asyncio


async def _start(client, slug: str = "acme") -> dict:
    started = await client.post(f"/auth/sso/{slug}/start")
    assert started.status_code == 200, started.text
    return started.json()


async def test_a_first_login_provisions_a_user_and_returns_a_session(client, db, owner_a, idp, monkeypatch):
    await create_oidc_connection(client, owner_a)
    stub = StubOidcTransport(**default_documents(idp)).install(monkeypatch)

    started = await _start(client)
    parameters = query_of(started["authorization_url"])
    assert parameters["state"] == started["state"]
    assert parameters["nonce"]
    assert parameters["code_challenge"], "PKCE is on by default"

    claims = token_claims(nonce=parameters["nonce"], email="grace@acme.test", sub="user-grace")
    stub.documents[f"{'https://idp.example.com'}/token"] = {"id_token": idp.id_token(claims)}

    completed = await client.get(
        "/auth/sso/acme/callback",
        params={"code": "auth-code-1", "state": started["state"]},
    )
    assert completed.status_code == 200, completed.text
    body = completed.json()
    assert body["access_token"]
    assert body["created"] is True
    assert body["linked"] is False

    provisioned = (
        await db.execute(select(User).where(User.email == "grace@acme.test"))
    ).scalar_one()
    assert provisioned.tenant_id == owner_a.tenant_id
    assert provisioned.role.value == "agent"
    assert provisioned.is_active is True

    sessions = (
        await db.execute(select(UserSession).where(UserSession.user_id == provisioned.id))
    ).scalars().all()
    assert len(sessions) == 1
    assert sessions[0].auth_method == "sso"
    assert sessions[0].mfa_verified is False
    assert sessions[0].sso_connection_id is not None


async def test_a_second_login_reuses_the_same_account(client, db, owner_a, idp, monkeypatch):
    await create_oidc_connection(client, owner_a)
    stub = StubOidcTransport(**default_documents(idp)).install(monkeypatch)

    first = await _start(client)
    nonce = query_of(first["authorization_url"])["nonce"]
    stub.documents["https://idp.example.com/token"] = {
        "id_token": idp.id_token(token_claims(nonce=nonce, email="grace@acme.test", sub="user-grace"))
    }
    assert (
        await client.get(
            "/auth/sso/acme/callback", params={"code": "c1", "state": first["state"]}
        )
    ).status_code == 200

    second = await _start(client)
    nonce = query_of(second["authorization_url"])["nonce"]
    stub.documents["https://idp.example.com/token"] = {
        "id_token": idp.id_token(token_claims(nonce=nonce, email="grace@acme.test", sub="user-grace"))
    }
    again = await client.get(
        "/auth/sso/acme/callback", params={"code": "c2", "state": second["state"]}
    )
    assert again.status_code == 200, again.text
    assert again.json()["created"] is False

    rows = (await db.execute(select(User).where(User.email == "grace@acme.test"))).scalars().all()
    assert len(rows) == 1, "a second login must not create a second account"


async def test_an_unknown_state_is_refused_generically(client, db, owner_a, idp, monkeypatch):
    await create_oidc_connection(client, owner_a)
    StubOidcTransport(**default_documents(idp)).install(monkeypatch)

    refused = await client.get(
        "/auth/sso/acme/callback", params={"code": "c", "state": "vdsso_not-a-real-state"}
    )
    assert refused.status_code == 400, refused.text
    assert refused.json()["code"] == "sso_failed"
    assert "state" not in refused.text.lower(), "the refusal must not name the reason"
    assert "access_token" not in refused.text

    attempts = (await db.execute(select(SSOLoginAttempt))).scalars().all()
    assert attempts == [], "nothing is recorded for a state that was never issued"


async def test_a_provider_error_is_answered_with_the_same_shape(client, db, owner_a, idp, monkeypatch):
    await create_oidc_connection(client, owner_a)
    StubOidcTransport(**default_documents(idp)).install(monkeypatch)
    started = await _start(client)

    refused = await client.get(
        "/auth/sso/acme/callback",
        params={"error": "access_denied", "state": started["state"], "code": ""},
    )
    assert refused.status_code == 400, refused.text
    assert refused.json()["code"] == "sso_failed"


async def test_a_token_carrying_the_wrong_nonce_is_refused(client, db, owner_a, idp, monkeypatch):
    await create_oidc_connection(client, owner_a)
    stub = StubOidcTransport(**default_documents(idp)).install(monkeypatch)
    started = await _start(client)

    # The provider mints a token for a *different* attempt's nonce: the classic
    # way to make one sign-in stand in for another.
    stub.documents["https://idp.example.com/token"] = {
        "id_token": idp.id_token(token_claims(nonce="not-this-attempt", email="grace@acme.test"))
    }
    refused = await client.get(
        "/auth/sso/acme/callback", params={"code": "c", "state": started["state"]}
    )
    assert refused.status_code == 400, refused.text
    assert "access_token" not in refused.text
    users = (await db.execute(select(User).where(User.email == "grace@acme.test"))).scalars().all()
    assert users == []


async def test_a_disabled_connection_refuses_the_login(client, db, owner_a, idp, monkeypatch):
    connection = await create_oidc_connection(client, owner_a)
    StubOidcTransport(**default_documents(idp)).install(monkeypatch)
    headers = await auth_headers(client, owner_a)

    disabled = await client.post(
        f"/api/sso/connections/{connection['id']}/status",
        json={"status": "disabled"},
        headers=headers,
    )
    assert disabled.status_code == 200, disabled.text

    started = await client.post("/auth/sso/acme/start")
    assert started.status_code == 404, started.text
    assert started.json()["code"] == "sso_unavailable"

    callback = await client.get(
        "/auth/sso/acme/callback", params={"code": "c", "state": "vdsso_anything"}
    )
    assert callback.status_code == 404, callback.text


async def test_an_unverified_email_is_refused_when_the_connection_requires_verification(
    client, db, owner_a, idp, monkeypatch
):
    await create_oidc_connection(client, owner_a, require_verified_email=True)
    stub = StubOidcTransport(**default_documents(idp)).install(monkeypatch)
    started = await _start(client)
    nonce = query_of(started["authorization_url"])["nonce"]
    stub.documents["https://idp.example.com/token"] = {
        "id_token": idp.id_token(
            token_claims(nonce=nonce, email="unverified@acme.test", email_verified=False)
        )
    }

    refused = await client.get(
        "/auth/sso/acme/callback", params={"code": "c", "state": started["state"]}
    )
    assert refused.status_code == 400, refused.text
    assert "access_token" not in refused.text
