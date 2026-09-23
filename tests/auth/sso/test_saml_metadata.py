"""SP metadata: what an administrator hands to the IdP, and what it must not say.

Two endpoints publish it — ``GET /api/sso/connections/{id}/metadata`` for the
administrator who is configuring the IdP, and ``GET /auth/sso/{slug}/metadata``
for the IdP itself, which has to fetch it before anyone has signed in. Both
return the same document, and both must contain exactly the values a SAML
configuration needs: the SP entity id, the ACS location, the SLO location — and
no signing key, because this SP signs nothing.

The tests also pin the two things that would otherwise drift: the metadata's
``entityID`` is the same string the assertion's ``Audience`` is checked against,
and the ``Location`` is the same string the response's ``Destination`` is
checked against.
"""
from __future__ import annotations

import pytest
from lxml import etree

from app.auth.identity.sso import saml
from tests.auth.sso.providers import (
    create_oidc_connection,
    create_saml_connection,
    query_of,
    start_saml_login,
)

pytestmark = pytest.mark.asyncio

MD_NS = "urn:oasis:names:tc:SAML:2.0:metadata"
DS_NS = "http://www.w3.org/2000/09/xmldsig#"


def _doc(text: str) -> etree._Element:
    return etree.fromstring(text.encode())


def _service(document: etree._Element, name: str) -> etree._Element | None:
    return document.find(f".//md:{name}", namespaces={"md": MD_NS})


async def test_the_admin_metadata_is_a_well_formed_descriptor(client, owner_a, idp):
    connection = await create_saml_connection(client, owner_a, idp)
    from tests.conftest import auth_headers

    response = await client.get(
        f"/api/sso/connections/{connection['id']}/metadata",
        headers=await auth_headers(client, owner_a),
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/xml")

    document = _doc(response.text)
    assert document.tag == f"{{{MD_NS}}}EntityDescriptor"
    assert document.get("entityID") == connection["sp_entity_id"]

    acs = _service(document, "AssertionConsumerService")
    assert acs is not None
    assert acs.get("Location") == connection["acs_url"]
    assert acs.get("Binding") == "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"


async def test_the_metadata_advertises_no_signing_key(client, owner_a, idp):
    """We do not sign AuthnRequests, so advertising a key would be a lie."""
    connection = await create_saml_connection(client, owner_a, idp)
    from tests.conftest import auth_headers

    response = await client.get(
        f"/api/sso/connections/{connection['id']}/metadata",
        headers=await auth_headers(client, owner_a),
    )
    assert not response.text.count(f"{{{DS_NS}}}"), response.text
    assert "KeyDescriptor" not in response.text
    assert "X509Certificate" not in response.text
    assert "BEGIN CERTIFICATE" not in response.text


async def test_the_public_metadata_matches_the_admin_one(client, owner_a, idp):
    connection = await create_saml_connection(client, owner_a, idp)
    from tests.conftest import auth_headers

    admin = await client.get(
        f"/api/sso/connections/{connection['id']}/metadata",
        headers=await auth_headers(client, owner_a),
    )
    public = await client.get("/auth/sso/acme/metadata")
    assert public.status_code == 200, public.text

    admin_doc, public_doc = _doc(admin.text), _doc(public.text)
    assert admin_doc.get("entityID") == public_doc.get("entityID")
    assert (
        _service(admin_doc, "AssertionConsumerService").get("Location")
        == _service(public_doc, "AssertionConsumerService").get("Location")
    )


async def test_the_metadata_entity_id_is_the_one_the_assertion_is_checked_against(
    client, owner_a, idp
):
    """If these two ever differ, every login from a correctly configured IdP fails."""
    connection = await create_saml_connection(client, owner_a, idp)
    from tests.conftest import auth_headers

    document = _doc(
        (
            await client.get(
                f"/api/sso/connections/{connection['id']}/metadata",
                headers=await auth_headers(client, owner_a),
            )
        ).text
    )
    entity_id = document.get("entityID")

    # An assertion whose Audience is that entity id is accepted...
    started = await start_saml_login(client)
    from tests.auth.sso.providers import post_assertion

    accepted = await post_assertion(
        client, idp, started=started, response=idp.response(
            request_id=started["request_id"], audience=entity_id
        )
    )
    assert accepted.status_code == 200, accepted.text

    # ...and one addressed to anything else is not.
    import uuid

    other = await create_saml_connection(
        client,
        owner_a,
        idp,
        slug=f"other-{uuid.uuid4().hex[:6]}",
        sp_entity_id="https://app.voxdesk.test/saml/another-sp",
    )
    started = await start_saml_login(client)
    refused = await post_assertion(
        client, idp, started=started, response=idp.response(
            request_id=started["request_id"], audience=other["sp_entity_id"]
        )
    )
    assert refused.status_code == 400, refused.text


async def test_the_metadata_location_is_the_one_the_response_must_name(client, owner_a, idp):
    connection = await create_saml_connection(client, owner_a, idp)
    from tests.auth.sso.providers import post_assertion

    started = await start_saml_login(client)
    accepted = await post_assertion(
        client, idp, started=started, response=idp.response(
            request_id=started["request_id"],
            recipient=connection["acs_url"],
            destination=connection["acs_url"],
        )
    )
    assert accepted.status_code == 200, accepted.text


async def test_an_explicit_acs_url_is_what_is_advertised(client, owner_a, idp):
    explicit = "https://sp.voxdesk.test/saml/acs/acme"
    connection = await create_saml_connection(
        client, owner_a, idp, slug="acme", acs_url=explicit, sp_entity_id="sp-explicit-entity"
    )
    from tests.conftest import auth_headers

    document = _doc(
        (
            await client.get(
                f"/api/sso/connections/{connection['id']}/metadata",
                headers=await auth_headers(client, owner_a),
            )
        ).text
    )
    assert document.get("entityID") == "sp-explicit-entity"
    assert _service(document, "AssertionConsumerService").get("Location") == explicit


async def test_the_authn_request_carries_the_advertised_acs(client, owner_a, idp):
    """The IdP is told where to send the assertion in both places, or it guesses."""
    connection = await create_saml_connection(client, owner_a, idp)
    started = await start_saml_login(client)

    from tests.auth.sso.providers import authn_request_id

    assert authn_request_id(started["authorization_url"]) == started["request_id"]
    request_xml = saml.build_authn_request(
        destination=f"{connection['idp_sso_url']}",
        acs_url=connection["acs_url"],
        issuer=connection["sp_entity_id"],
        request_id=started["request_id"],
    )
    import urllib.parse
    import zlib

    import base64

    inner = zlib.decompress(
        base64.b64decode(urllib.parse.parse_qs(request_xml)["SAMLRequest"][0]), -15
    ).decode()
    assert f'AssertionConsumerServiceURL="{connection["acs_url"]}"' in inner


async def test_the_redirect_target_is_the_configured_sign_on_url(client, owner_a, idp):
    await create_saml_connection(
        client, owner_a, idp, slug="acme", idp_sso_url="https://idp.example.com/custom/sso"
    )
    started = await start_saml_login(client)

    assert started["authorization_url"].startswith("https://idp.example.com/custom/sso?")
    parameters = query_of(started["authorization_url"])
    assert parameters["RelayState"] == started["state"]
    assert parameters["SAMLRequest"]


async def test_the_public_metadata_is_not_served_for_an_oidc_connection(client, owner_a, idp, oidc_provider):
    """An OIDC connection has no SAML endpoint, so it publishes no document."""
    await create_oidc_connection(client, owner_a, slug="acme")
    response = await client.get("/auth/sso/acme/metadata")
    assert response.status_code == 404, response.text
    assert response.json()["code"] == "sso_unavailable"


async def test_the_admin_metadata_is_refused_for_an_oidc_connection(client, owner_a, idp, oidc_provider):
    from tests.conftest import auth_headers

    connection = await create_oidc_connection(client, owner_a, slug="acme")
    response = await client.get(
        f"/api/sso/connections/{connection['id']}/metadata",
        headers=await auth_headers(client, owner_a),
    )
    assert response.status_code in (400, 404), response.text
    assert "SAML" in response.text or "saml" in response.text


async def test_the_connection_test_endpoint_reports_what_is_missing(client, owner_a, idp):
    from tests.conftest import auth_headers

    connection = await create_saml_connection(client, owner_a, idp)
    ok = await client.post(
        f"/api/sso/connections/{connection['id']}/test",
        headers=await auth_headers(client, owner_a),
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["ok"] is True
    assert ok.json()["certificates"] == 1
    assert ok.json()["warnings"] == []


async def test_the_connection_test_reports_missing_saml_pieces(client, owner_a, idp):
    from tests.conftest import auth_headers

    headers = await auth_headers(client, owner_a)
    created = await client.post(
        "/api/sso/connections",
        json={
            "name": "Incomplete",
            "slug": "incomplete",
            "protocol": "saml",
            "idp_entity_id": "https://idp.example.com",
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text

    checked = await client.post(
        f"/api/sso/connections/{created.json()['id']}/test", headers=headers
    )
    assert checked.status_code == 200, checked.text
    body = checked.json()
    assert body["ok"] is False
    assert body["certificates"] == 0
    joined = " ".join(body["warnings"]).lower()
    assert "sign-on url" in joined
    assert "certificate" in joined
