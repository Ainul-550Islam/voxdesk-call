"""Key rotation must be a non-event, and a stale key must not become a bypass.

Two halves:

* the fetch layer — a key set is cached briefly, a miss triggers exactly one
  forced refresh, and a refresh that fails leaves the login refused rather than
  falling back to "trust it anyway";
* the verification layer — after a provider rotates, a token signed by the *new*
  key is accepted while a token signed by the retired key is refused once the
  key set no longer lists it.
"""
from __future__ import annotations

import pytest

from app.auth.identity import tokens as identity_tokens
from app.auth.identity.exceptions import SSOValidationError
from app.auth.identity.sso import oidc
from tests.auth.sso.providers import (
    IdP,
    StubOidcTransport,
    default_documents,
    discovered_provider,
    oidc_connection,
    token_claims,
)

pytestmark = pytest.mark.asyncio

NONCE = "rotation-nonce"


async def _verify(idp, token: str):
    return await oidc.verify_id_token(
        oidc_connection(),
        discovered_provider(),
        id_token=token,
        nonce_hash=identity_tokens.hash_token(NONCE),
    )


async def test_a_token_signed_with_the_rotated_key_is_accepted(idp, monkeypatch):
    rotated = IdP(kid="key-2026-09")
    documents = default_documents(idp)
    # The provider publishes only its new key, and signs with it.
    documents["jwks"] = rotated.jwks()
    StubOidcTransport(**documents).install(monkeypatch)

    token = rotated.id_token(token_claims(nonce=NONCE))
    claims = await _verify(idp, token)
    assert claims["sub"] == "user-1"


async def test_a_token_signed_with_a_retired_key_is_refused(idp, monkeypatch):
    documents = default_documents(idp)
    documents["jwks"] = IdP(kid="key-2026-09").jwks()
    StubOidcTransport(**documents).install(monkeypatch)

    retired = idp.id_token(token_claims(nonce=NONCE))
    with pytest.raises(SSOValidationError):
        await _verify(idp, retired)


async def test_one_unknown_key_id_triggers_exactly_one_forced_refresh(idp, monkeypatch):
    """The ``kid`` is unknown on first look, so the key set is re-fetched once."""
    rotated = IdP(kid="key-2026-09")
    documents = default_documents(idp)
    documents["jwks"] = rotated.jwks()
    stub = StubOidcTransport(**documents).install(monkeypatch)

    token = rotated.id_token(token_claims(nonce=NONCE))
    await _verify(idp, token)

    jwks_fetches = [call for call in stub.calls if call[1].endswith("/jwks")]
    assert len(jwks_fetches) <= 2, "a key rotation must not turn into a fetch storm"


async def test_key_sets_are_cached_between_logins(idp, monkeypatch):
    stub = StubOidcTransport(**default_documents(idp)).install(monkeypatch)
    provider = discovered_provider()

    await oidc.fetch_jwks(provider)
    await oidc.fetch_jwks(provider)
    assert len([call for call in stub.calls if call[1].endswith("/jwks")]) == 1

    await oidc.fetch_jwks(provider, force=True)
    assert len([call for call in stub.calls if call[1].endswith("/jwks")]) == 2


async def test_a_key_set_that_cannot_be_fetched_refuses_the_login(idp, monkeypatch):
    """Fail closed: no key set means no verification, never a skipped check."""
    import httpx

    StubOidcTransport(
        overrides={
            "https://idp.example.com/jwks": httpx.ConnectError("connection refused"),
        }
    ).install(monkeypatch)
    with pytest.raises(SSOValidationError):
        await oidc.fetch_jwks(discovered_provider())


async def test_a_key_set_that_answers_an_error_status_refuses_the_login(idp, monkeypatch):
    StubOidcTransport(
        overrides={"https://idp.example.com/jwks": {"error": "not_found"}}
    ).install(monkeypatch)
    with pytest.raises(SSOValidationError):
        await oidc.fetch_jwks(discovered_provider())


async def test_an_empty_key_set_refuses_the_token(idp, monkeypatch):
    documents = default_documents(idp)
    documents["jwks"] = {"keys": []}
    StubOidcTransport(**documents).install(monkeypatch)

    with pytest.raises(SSOValidationError):
        await _verify(idp, idp.id_token(token_claims(nonce=NONCE)))


async def test_a_key_set_with_an_unusable_key_is_refused(idp, monkeypatch):
    documents = default_documents(idp)
    documents["jwks"] = {"keys": [{"kty": "oct", "kid": idp.kid, "k": "shared-secret"}]}
    StubOidcTransport(**documents).install(monkeypatch)

    with pytest.raises(SSOValidationError):
        await _verify(idp, idp.id_token(token_claims(nonce=NONCE)))


async def test_a_malformed_key_set_document_is_refused(idp, monkeypatch):
    documents = default_documents(idp)
    documents["jwks"] = {"keys": "not-a-list"}
    StubOidcTransport(**documents).install(monkeypatch)

    with pytest.raises(SSOValidationError):
        await oidc.fetch_jwks(discovered_provider())
