"""The state parameter and the nonce, at the HTTP boundary.

The state is the only CSRF defence the callback has, and the nonce is what ties
an ID token to the request that asked for it. Both are stored **hashed** — the
row keeps a digest, so a database read cannot be turned into a forged callback —
and both are single-use, because the state is spent with a conditional ``UPDATE``
that exactly one caller can win.

Covered here:

* the plaintext never reaches the database;
* a state cannot be spent twice, and cannot be spent after it expires;
* a state issued by one connection cannot be redeemed at another connection's
  callback, even when the two connections share a provider;
* a state from a different protocol is refused rather than reinterpreted;
* the PKCE verifier is sealed on the attempt row, and the token exchange sends
  the verifier whose challenge the browser was given.
"""
from __future__ import annotations

import datetime as dt
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.auth.identity.exceptions import SSOValidationError
from app.auth.identity.models import SSOLoginAttempt
from app.auth.identity.sso import service as sso_service
from app.auth.identity.tokens import hash_token, pkce_challenge
from tests.auth.sso.providers import (
    create_oidc_connection,
    hand_back_token,
    query_of,
)

pytestmark = pytest.mark.asyncio


async def _start(client, slug: str = "acme") -> dict:
    started = await client.post(f"/auth/sso/{slug}/start")
    assert started.status_code == 200, started.text
    return started.json()


async def _attempt_for(db, connection: dict) -> SSOLoginAttempt:
    """The attempt row a connection's start just created, re-read from the database."""
    rows = (
        await db.execute(
            select(SSOLoginAttempt)
            .where(SSOLoginAttempt.connection_id == uuid.UUID(connection["id"]))
            .order_by(SSOLoginAttempt.created_at.desc())
            .execution_options(populate_existing=True)
        )
    ).scalars().all()
    assert rows, "starting a login must record an attempt"
    return rows[0]


async def _complete(client, idp, stub, *, started: dict, slug: str = "acme", email: str = "ada@acme.test"):
    nonce = query_of(started["authorization_url"])["nonce"]
    hand_back_token(idp, stub, nonce=nonce, email=email)
    return await client.get(
        f"/auth/sso/{slug}/callback", params={"code": "c-1", "state": started["state"]}
    )


async def test_the_state_and_nonce_are_never_stored_in_the_clear(client, db, owner_a, idp, oidc_provider):
    connection = await create_oidc_connection(client, owner_a)
    started = await _start(client)
    parameters = query_of(started["authorization_url"])

    attempt = await _attempt_for(db, connection)

    assert started["state"].startswith("vdsso_")
    assert attempt.state_hash == hash_token(started["state"])
    assert attempt.state_hash != started["state"]
    assert attempt.nonce_hash == hash_token(parameters["nonce"])
    assert attempt.nonce_hash != parameters["nonce"]
    assert attempt.consumed_at is None


async def test_a_state_cannot_be_spent_twice(client, db, owner_a, idp, oidc_provider):
    await create_oidc_connection(client, owner_a)
    started = await _start(client)

    first = await _complete(client, idp, oidc_provider, started=started)
    assert first.status_code == 200, first.text

    replayed = await client.get(
        "/auth/sso/acme/callback", params={"code": "c-1", "state": started["state"]}
    )
    assert replayed.status_code == 400
    assert replayed.json()["code"] == "sso_failed"
    assert "access_token" not in replayed.text


async def test_an_expired_state_is_refused(client, db, owner_a, idp, oidc_provider):
    connection = await create_oidc_connection(client, owner_a)
    started = await _start(client)

    attempt = await _attempt_for(db, connection)
    attempt.expires_at = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(seconds=1)
    await db.commit()

    refused = await _complete(client, idp, oidc_provider, started=started)
    assert refused.status_code == 400, refused.text
    assert refused.json()["code"] == "sso_failed"
    assert "access_token" not in refused.text


async def test_a_state_from_another_connection_cannot_be_redeemed(client, db, owner_a, idp, oidc_provider):
    """Two connections on one provider must not finish each other's login.

    The ID token here is minted for the shared client id and the shared issuer,
    so verification alone would succeed. What refuses it is that the attempt row
    knows which connection created it.
    """
    await create_oidc_connection(client, owner_a, slug="alpha")
    await create_oidc_connection(client, owner_a, slug="beta")

    started = await _start(client, "alpha")
    nonce = query_of(started["authorization_url"])["nonce"]
    hand_back_token(idp, oidc_provider, nonce=nonce, email="ada@acme.test")

    refused = await client.get(
        "/auth/sso/beta/callback", params={"code": "c-1", "state": started["state"]}
    )
    assert refused.status_code == 400, refused.text
    assert "access_token" not in refused.text
    assert refused.json()["code"] == "sso_failed"

    attempt = (
        await db.execute(select(SSOLoginAttempt).execution_options(populate_existing=True))
    ).scalars().one()
    assert attempt.consumed_at is not None
    assert attempt.outcome == "failed"


async def test_the_connection_binding_rule_itself():
    """``_require_own_attempt`` refuses a foreign attempt and allows its own."""
    mine = uuid.uuid4()
    attempt = SimpleNamespace(connection_id=mine)
    sso_service._require_own_attempt(attempt, SimpleNamespace(id=mine))

    with pytest.raises(SSOValidationError):
        sso_service._require_own_attempt(attempt, SimpleNamespace(id=uuid.uuid4()))


async def test_a_state_from_another_protocol_is_refused(client, db, owner_a, idp, oidc_provider):
    connection = await create_oidc_connection(client, owner_a)
    started = await _start(client)

    attempt = await _attempt_for(db, connection)
    attempt.protocol = "saml"
    await db.commit()

    refused = await client.get(
        "/auth/sso/acme/callback", params={"code": "c-1", "state": started["state"]}
    )
    assert refused.status_code == 400, refused.text
    assert "access_token" not in refused.text


async def test_a_missing_state_or_code_is_refused(client, db, owner_a, idp, oidc_provider):
    await create_oidc_connection(client, owner_a)
    started = await _start(client)

    assert (await client.get("/auth/sso/acme/callback", params={"code": "c-1"})).status_code == 400
    assert (
        await client.get("/auth/sso/acme/callback", params={"state": started["state"]})
    ).status_code == 400
    assert (
        await client.get(
            "/auth/sso/acme/callback",
            params={"code": "c-1", "state": started["state"], "error": "access_denied"},
        )
    ).status_code == 400


async def test_the_pkce_verifier_is_sealed_and_matches_the_challenge(
    client, db, owner_a, idp, oidc_provider
):
    connection = await create_oidc_connection(client, owner_a)
    started = await _start(client)
    parameters = query_of(started["authorization_url"])
    assert parameters["code_challenge_method"] == "S256"

    attempt = await _attempt_for(db, connection)
    assert attempt.code_verifier_encrypted
    assert attempt.code_verifier_key_id

    completed = await _complete(client, idp, oidc_provider, started=started)
    assert completed.status_code == 200, completed.text

    exchange = oidc_provider.posts[-1]
    verifier = exchange["data"]["code_verifier"]
    assert verifier not in attempt.code_verifier_encrypted, "the verifier is sealed, not stored"
    assert pkce_challenge(verifier) == parameters["code_challenge"]
    assert exchange["data"]["redirect_uri"] == "https://app.voxdesk.test/auth/sso/acme/callback"


async def test_pkce_can_be_turned_off_without_losing_the_state_check(
    client, db, owner_a, idp, oidc_provider
):
    connection = await create_oidc_connection(client, owner_a, use_pkce=False)
    started = await _start(client)
    parameters = query_of(started["authorization_url"])
    assert "code_challenge" not in parameters

    attempt = await _attempt_for(db, connection)
    assert attempt.code_verifier_encrypted is None
    assert attempt.state_hash, "the state is still what protects the callback"

    completed = await _complete(client, idp, oidc_provider, started=started)
    assert completed.status_code == 200, completed.text
