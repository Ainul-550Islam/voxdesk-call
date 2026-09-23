"""Discovery: the one place a provider-supplied URL can move a trust boundary.

A discovery document is fetched from a URL an administrator typed, and then it
tells this deployment where to send an authorization request and where to fetch
signing keys. Every rule asserted here exists because the alternative is an
attacker who controls one of those fields:

* ``https`` only (with a loopback carve-out outside production, so local
  development is possible without weakening a real deployment);
* the document's issuer must equal the connection's issuer — a document that
  says "I am somebody else's IdP" is the mix-up set-up;
* there must be a key set to verify against, and at least one usable algorithm;
* the fetch is cached with a TTL and a ``force`` flag, so key rotation and an
  administrator's "test" button both work without waiting out the cache.
"""
from __future__ import annotations

import pytest

from app.auth.identity.exceptions import SSOConfigurationError, SSOValidationError
from app.auth.identity.sso import oidc
from tests.auth.sso.providers import (
    IDP_ENTITY,
    StubOidcTransport,
    default_documents,
    oidc_connection,
    token_claims,
)

pytestmark = pytest.mark.asyncio


async def test_discovery_returns_the_published_endpoints(idp, monkeypatch):
    StubOidcTransport(**default_documents(idp)).install(monkeypatch)

    provider = await oidc.discover(oidc_connection())
    assert provider.issuer == IDP_ENTITY
    assert provider.authorization_endpoint == f"{IDP_ENTITY}/authorize"
    assert provider.token_endpoint == f"{IDP_ENTITY}/token"
    assert provider.jwks_uri == f"{IDP_ENTITY}/jwks"
    assert "RS256" in provider.supported_algorithms


async def test_discovery_refuses_a_document_naming_another_issuer(idp, monkeypatch):
    documents = default_documents(idp)
    documents["discovery"] = {**documents["discovery"], "issuer": "https://evil.example.net"}
    StubOidcTransport(**documents).install(monkeypatch)

    with pytest.raises(SSOConfigurationError):
        await oidc.discover(oidc_connection())


async def test_discovery_refuses_a_document_without_a_key_set(idp, monkeypatch):
    documents = default_documents(idp)
    documents["discovery"] = {key: value for key, value in documents["discovery"].items() if key != "jwks_uri"}
    StubOidcTransport(**documents).install(monkeypatch)

    with pytest.raises(SSOConfigurationError):
        await oidc.discover(oidc_connection())


async def test_discovery_refuses_an_endpoint_that_is_not_https(idp, monkeypatch):
    documents = default_documents(idp)
    documents["discovery"] = {
        **documents["discovery"],
        "token_endpoint": "http://idp.example.com/token",
    }
    StubOidcTransport(**documents).install(monkeypatch)

    with pytest.raises(SSOConfigurationError):
        await oidc.discover(oidc_connection())


async def test_discovery_refuses_an_insecure_discovery_url(monkeypatch):
    with pytest.raises(SSOConfigurationError):
        await oidc.discover(oidc_connection(discovery_url="http://idp.example.com/oidc"))


async def test_discovery_is_cached_and_can_be_forced(idp, monkeypatch):
    stub = StubOidcTransport(**default_documents(idp)).install(monkeypatch)
    connection = oidc_connection()

    await oidc.discover(connection)
    await oidc.discover(connection)
    fetches = [call for call in stub.calls if call[0] == "GET"]
    assert len(fetches) == 1, "a second login must not re-fetch the document"

    await oidc.discover(connection, force=True)
    assert len([call for call in stub.calls if call[0] == "GET"]) == 2


async def test_an_advertised_symmetric_algorithm_is_recorded_and_never_trusted(idp, monkeypatch):
    """What the provider *says* it signs with is metadata, not authority.

    The algorithm list is recorded so the error messages can be precise, but a
    token is only accepted if its signature verifies against a key in the
    provider's own key set. Here the document advertises HS256 — a symmetric
    algorithm, which a client must never accept from an IdP — and discovery
    happily records it while verification still refuses the token.
    """
    from app.auth.identity import tokens as identity_tokens

    documents = default_documents(idp)
    documents["discovery"] = {
        **documents["discovery"],
        "id_token_signing_alg_values_supported": ["HS256"],
    }
    symmetric_token = idp.id_token(token_claims(), algorithm="HS256")
    StubOidcTransport(**documents).install(monkeypatch)

    connection = oidc_connection()
    provider = await oidc.discover(connection)
    assert provider.supported_algorithms == ("HS256",)

    with pytest.raises(SSOValidationError):
        await oidc.verify_id_token(
            connection,
            provider,
            id_token=symmetric_token,
            nonce_hash=identity_tokens.hash_token("whatever"),
        )


async def test_a_document_that_is_not_json_is_refused(idp, monkeypatch):
    StubOidcTransport(
        overrides={f"{IDP_ENTITY}/.well-known/openid-configuration": ValueError("not json")}
    ).install(monkeypatch)

    with pytest.raises((SSOConfigurationError, SSOValidationError)):
        await oidc.discover(oidc_connection())


def test_the_https_posture_rule_is_the_same_one_everywhere():
    assert oidc.is_acceptable_https_url("https://idp.example.com/oidc") is True
    assert oidc.is_acceptable_https_url("http://idp.example.com/oidc") is False
    assert oidc.is_acceptable_https_url("http://localhost:8080/oidc") is True
    assert oidc.is_acceptable_https_url("") is False
    assert oidc.is_acceptable_https_url("idp.example.com/oidc") is False
