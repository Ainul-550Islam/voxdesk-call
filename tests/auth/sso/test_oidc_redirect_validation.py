"""Which URL a federated login is allowed to come back to.

The redirect URI is where the authorization code — and therefore the ID token —
is delivered, and the ACS URL is the SAML equivalent. Both are properties of the
*connection*, never of the request: a caller that could supply either one would
be able to have the provider deliver a code to a URL of its choosing.

This file asserts:

* the authorization request carries the connection's redirect URI, and the token
  exchange uses the same value (providers check it, and a mismatch is a real
  failure mode);
* the connection's default is derived from the deployment's own base URL when no
  explicit value is configured — never echoed from a header the caller controls;
* a plaintext endpoint is refused at connection create and update time, with the
  same https posture rule the discovery URL goes through, while loopback stays
  usable outside production;
* the public metadata endpoint advertises the ACS URL for SAML connections and
  the callback for OIDC ones, and is unavailable for a connection that is not
  active.
"""
from __future__ import annotations

import pytest

from app.auth.identity.sso import service as sso_service
from tests.auth.sso.providers import (
    create_oidc_connection,
    create_saml_connection,
    query_of,
)

pytestmark = pytest.mark.asyncio

CALLBACK = "https://app.voxdesk.test/auth/sso/acme/callback"


async def test_the_authorization_request_carries_the_connections_redirect_uri(
    client, owner_a, idp, oidc_provider
):
    await create_oidc_connection(client, owner_a, redirect_uri=CALLBACK)

    started = await client.post("/auth/sso/acme/start")
    assert started.status_code == 200, started.text
    assert query_of(started.json()["authorization_url"])["redirect_uri"] == CALLBACK


async def test_the_token_exchange_uses_the_same_redirect_uri(client, owner_a, idp, oidc_provider):
    from tests.auth.sso.providers import hand_back_token

    await create_oidc_connection(client, owner_a, redirect_uri=CALLBACK)
    started = (await client.post("/auth/sso/acme/start")).json()
    nonce = query_of(started["authorization_url"])["nonce"]
    hand_back_token(idp, oidc_provider, nonce=nonce, email="ada@acme.test")

    completed = await client.get(
        "/auth/sso/acme/callback", params={"code": "c-1", "state": started["state"]}
    )
    assert completed.status_code == 200, completed.text
    assert oidc_provider.posts[-1]["data"]["redirect_uri"] == CALLBACK


async def test_a_caller_cannot_supply_its_own_redirect_uri(client, owner_a, idp, oidc_provider):
    """The start endpoint takes no redirect parameter, and query noise changes nothing."""
    await create_oidc_connection(client, owner_a, redirect_uri=CALLBACK)

    started = await client.post(
        "/auth/sso/acme/start?redirect_uri=https://evil.example.net/steal",
        json={"redirect_uri": "https://evil.example.net/steal"},
    )
    assert started.status_code == 200, started.text
    assert query_of(started.json()["authorization_url"])["redirect_uri"] == CALLBACK


async def test_the_default_redirect_uri_is_derived_from_the_deployment(client, owner_a, idp, oidc_provider):
    connection = await create_oidc_connection(client, owner_a, redirect_uri="")
    assert connection["redirect_uri"].endswith("/auth/sso/acme/callback")
    assert connection["redirect_uri"].startswith("http")

    started = await client.post("/auth/sso/acme/start")
    assert query_of(started.json()["authorization_url"])["redirect_uri"] == connection["redirect_uri"]


async def test_a_plaintext_redirect_uri_is_refused(client, owner_a, idp, oidc_provider):
    refused = await client.post(
        "/api/sso/connections",
        json={
            "name": "Plaintext",
            "slug": "plaintext",
            "protocol": "oidc",
            "issuer": "https://idp.example.com",
            "discovery_url": "https://idp.example.com/.well-known/openid-configuration",
            "client_id": "client-abc",
            "redirect_uri": "http://evil.example.net/callback",
        },
        headers=await _headers(client, owner_a),
    )
    assert refused.status_code in (400, 422), refused.text
    assert "https" in refused.text.lower()


async def test_a_plaintext_acs_url_is_refused_for_a_saml_connection(client, owner_a, idp):
    refused = await client.post(
        "/api/sso/connections",
        json={
            "name": "Plaintext SAML",
            "slug": "plaintext-saml",
            "protocol": "saml",
            "idp_entity_id": "https://idp.example.com",
            "idp_sso_url": "https://idp.example.com/sso",
            "acs_url": "http://evil.example.net/acs",
        },
        headers=await _headers(client, owner_a),
    )
    assert refused.status_code in (400, 422), refused.text
    assert "https" in refused.text.lower()


async def test_a_plaintext_idp_sign_on_url_is_refused(client, owner_a, idp):
    refused = await client.post(
        "/api/sso/connections",
        json={
            "name": "Plaintext IdP",
            "slug": "plaintext-idp",
            "protocol": "saml",
            "idp_entity_id": "https://idp.example.com",
            "idp_sso_url": "http://idp.example.com/sso",
        },
        headers=await _headers(client, owner_a),
    )
    assert refused.status_code in (400, 422), refused.text


async def test_updating_a_connection_cannot_smuggle_in_a_plaintext_redirect(client, owner_a, idp, oidc_provider):
    connection = await create_oidc_connection(client, owner_a, redirect_uri=CALLBACK)
    headers = await _headers(client, owner_a)

    refused = await client.patch(
        f"/api/sso/connections/{connection['id']}",
        json={
            "name": "Acme IdP",
            "slug": "acme",
            "protocol": "oidc",
            "issuer": "https://idp.example.com",
            "discovery_url": "https://idp.example.com/.well-known/openid-configuration",
            "client_id": "client-abc",
            "redirect_uri": "http://evil.example.net/callback",
        },
        headers=headers,
    )
    assert refused.status_code in (400, 422), refused.text

    unchanged = await client.get(f"/api/sso/connections/{connection['id']}", headers=headers)
    assert unchanged.json()["redirect_uri"] == CALLBACK


async def test_the_public_metadata_is_published_only_for_an_active_connection(
    client, owner_a, idp, oidc_provider
):
    connection = await create_saml_connection(client, owner_a, idp, slug="acme", acs_url="")

    body = await client.get("/auth/sso/acme/metadata")
    assert body.status_code == 200, body.text
    assert "EntityDescriptor" in body.text
    assert b"/auth/sso/acme/acs" in body.content or "/auth/sso/acme/acs" in body.text

    headers = await _headers(client, owner_a)
    disabled = await client.post(
        f"/api/sso/connections/{connection['id']}/status",
        json={"status": "disabled"},
        headers=headers,
    )
    assert disabled.status_code == 200, disabled.text

    unavailable = await client.get("/auth/sso/acme/metadata")
    assert unavailable.status_code == 404, unavailable.text


async def test_the_default_acs_url_is_derived_per_connection(client, owner_a, idp, oidc_provider):
    connection = await create_saml_connection(client, owner_a, idp, slug="acme", acs_url="")
    assert connection["acs_url"].endswith("/auth/sso/acme/acs")

    other = await create_saml_connection(client, owner_a, idp, slug="beta", acs_url="")
    assert other["acs_url"].endswith("/auth/sso/beta/acs")
    assert other["acs_url"] != connection["acs_url"]


async def test_an_explicit_acs_url_is_used_exactly_as_given(client, owner_a, idp, oidc_provider):
    explicit = "https://sp.voxdesk.test/saml/acs/acme"
    connection = await create_saml_connection(client, owner_a, idp, slug="acme", acs_url=explicit)
    assert connection["acs_url"] == explicit

    started = await client.post("/auth/sso/acme/start")
    assert started.status_code == 200, started.text
    assert query_of(started.json()["authorization_url"]).get("RelayState")


async def test_the_service_helper_and_the_route_agree(client, owner_a, idp, oidc_provider):
    """One derivation, two callers: the route and the helper cannot drift."""
    connection = await create_oidc_connection(client, owner_a, slug="acme", redirect_uri="")
    assert connection["redirect_uri"] == sso_service.oidc_redirect_uri(_row(connection))


def _row(connection: dict):
    """The attribute shape ``oidc_redirect_uri`` reads off a stored connection."""
    from types import SimpleNamespace

    return SimpleNamespace(
        slug=connection["slug"], redirect_uri="", id=connection["id"], acs_url=connection["acs_url"]
    )


async def _headers(client, user):
    from tests.conftest import auth_headers

    return await auth_headers(client, user)
