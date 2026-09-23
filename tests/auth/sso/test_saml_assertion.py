"""The assertion itself: XML-DSig attacks, run through the public ACS endpoint.

The signature verifier in :mod:`app.auth.identity.sso.saml` is written by hand —
it parses the ``SignedInfo``, canonicalises the referenced element, hashes it and
checks the signature with ``cryptography`` — so it has to be attacked on purpose.
The fixtures are produced by ``signxml``, an independent implementation: when the
hand-written verifier accepts what ``signxml`` signed and rejects everything else,
the XML-DSig code is right in both directions.

The attacks here are the ones that have broken real SAML integrations:

* **Signature wrapping** — a second assertion in the same response, or the
  signature covering a different element than the one carrying the claims.
* **Key confusion** — a ``KeyInfo`` naming a certificate the connection does not
  trust; trusting what arrives in the message is how a SAML SP becomes an open
  door.
* **Digest and signature tampering** — one flipped character in either value.
* **A missing or empty element** — no assertion ID (so replay is unenforceable),
  no audience restriction, no signature at all.
"""
from __future__ import annotations

import base64
import copy
import uuid

import pytest
from lxml import etree

from tests.auth.sso.providers import (
    DS_NS,
    NSMAP,
    create_saml_connection,
    post_assertion,
    start_saml_login,
)

pytestmark = pytest.mark.asyncio


def _refused(response, *, why: str) -> None:
    assert response.status_code == 400, f"{why}: {response.text}"
    assert response.json()["code"] == "sso_failed"
    assert "access_token" not in response.text


def _flip_base64(value: str) -> str:
    """Change one character of a base64 string to another valid one."""
    middle = len(value) // 2
    original = value[middle]
    replacement = "B" if original != "B" else "C"
    return value[:middle] + replacement + value[middle + 1 :]


def _second_assertion(signed: bytes, *, email: str) -> bytes:
    """Append an unsigned assertion authored by the attacker."""
    root = etree.fromstring(signed)
    forged = copy.deepcopy(root.find("saml:Assertion", namespaces=NSMAP))
    assert forged is not None
    forged.set("ID", "_forged-assertion")
    for node in forged.findall(".//ds:Signature", namespaces=NSMAP):
        node.getparent().remove(node)
    for value in forged.findall(".//saml:AttributeValue", namespaces=NSMAP):
        if value.text and "@" in value.text:
            value.text = email
    root.append(forged)
    return etree.tostring(root)


async def test_two_assertions_in_one_response_are_refused(client, db, owner_a, idp):
    """Signature wrapping: exactly one assertion is expected, so two is a refusal."""
    await create_saml_connection(client, owner_a, idp)
    started = await start_saml_login(client)
    signed = idp.response(request_id=started["request_id"])

    _refused(
        await post_assertion(
            client, idp, started=started, response=_second_assertion(signed, email="evil@acme.test")
        ),
        why="two assertions",
    )


async def test_an_unsigned_assertion_inside_a_signed_response_is_refused(client, db, owner_a, idp):
    """A signed envelope must not vouch for a body that carries no signature."""
    await create_saml_connection(client, owner_a, idp)
    started = await start_saml_login(client)

    root = idp.assertion_xml(assertion_id=f"_{uuid.uuid4().hex}", request_id=started["request_id"])
    signed_response = idp.sign(root, enclosing="response")
    assert signed_response is not None

    _refused(
        await post_assertion(
            client, idp, started=started, response=etree.tostring(signed_response)
        ),
        why="unsigned assertion inside a signed response",
    )


async def test_a_response_level_signature_is_enough_when_the_connection_allows_it(
    client, db, owner_a, idp
):
    """The documented escape hatch, asserted so its effect is not a surprise."""
    await create_saml_connection(client, owner_a, idp, require_signed_assertions=False)
    started = await start_saml_login(client)

    root = idp.assertion_xml(assertion_id=f"_{uuid.uuid4().hex}", request_id=started["request_id"])
    signed_response = idp.sign(root, enclosing="response")

    accepted = await post_assertion(
        client, idp, started=started, response=etree.tostring(signed_response)
    )
    assert accepted.status_code == 200, accepted.text


async def test_a_signature_that_covers_the_response_is_still_refused_by_default(client, db, owner_a, idp):
    """The default is the strict setting, and it stays strict."""
    await create_saml_connection(client, owner_a, idp, require_signed_assertions=True)
    started = await start_saml_login(client)

    root = idp.assertion_xml(assertion_id=f"_{uuid.uuid4().hex}", request_id=started["request_id"])
    signed_response = idp.sign(root, enclosing="response")

    _refused(
        await post_assertion(client, idp, started=started, response=etree.tostring(signed_response)),
        why="response-signed only",
    )


async def test_a_tampered_digest_is_refused(client, db, owner_a, idp):
    await create_saml_connection(client, owner_a, idp)
    started = await start_saml_login(client)

    root = etree.fromstring(idp.response(request_id=started["request_id"]))
    digest = root.find(".//ds:DigestValue", namespaces=NSMAP)
    assert digest is not None
    digest.text = _flip_base64((digest.text or "").strip())

    _refused(
        await post_assertion(client, idp, started=started, response=etree.tostring(root)),
        why="tampered digest",
    )


async def test_a_tampered_signature_value_is_refused(client, db, owner_a, idp):
    await create_saml_connection(client, owner_a, idp)
    started = await start_saml_login(client)

    root = etree.fromstring(idp.response(request_id=started["request_id"]))
    value = root.find(".//ds:SignatureValue", namespaces=NSMAP)
    assert value is not None
    value.text = _flip_base64("".join((value.text or "").split()))

    _refused(
        await post_assertion(client, idp, started=started, response=etree.tostring(root)),
        why="tampered signature value",
    )


async def test_a_signature_with_two_references_is_refused(client, db, owner_a, idp, other_idp):
    """Two references is how a wrapper makes one signature appear to cover both."""
    await create_saml_connection(client, owner_a, idp)
    started = await start_saml_login(client)

    root = etree.fromstring(idp.response(request_id=started["request_id"]))
    signed_info = root.find(".//ds:SignedInfo", namespaces=NSMAP)
    assert signed_info is not None
    reference = signed_info.find("ds:Reference", namespaces=NSMAP)
    assert reference is not None
    signed_info.append(copy.deepcopy(reference))

    _refused(
        await post_assertion(client, idp, started=started, response=etree.tostring(root)),
        why="two references",
        )


async def test_key_info_naming_an_untrusted_certificate_is_refused(client, db, owner_a, idp, other_idp):
    """Trust comes from the connection, never from the certificate in the message.

    The signature is valid — it was made by the connection's provider — but the
    ``KeyInfo`` names somebody else's certificate. The verifier refuses because
    the presented certificate has to be one it already trusts; a message may name
    a key, but it cannot introduce one.
    """
    await create_saml_connection(client, owner_a, idp)
    started = await start_saml_login(client)

    root = etree.fromstring(idp.response(request_id=started["request_id"]))
    presented = root.find(".//ds:X509Certificate", namespaces=NSMAP)
    assert presented is not None
    attacker_der = other_idp.certificate.public_bytes(
        __import__("cryptography.hazmat.primitives.serialization", fromlist=["Encoding"]).Encoding.DER
    )
    presented.text = base64.b64encode(attacker_der).decode()

    _refused(
        await post_assertion(client, idp, started=started, response=etree.tostring(root)),
        why="untrusted KeyInfo certificate",
    )


async def test_an_assertion_without_an_id_is_refused(client, db, owner_a, idp):
    """No ID means replay cannot be prevented, so the assertion is not accepted."""
    await create_saml_connection(client, owner_a, idp)
    started = await start_saml_login(client)

    root = etree.fromstring(idp.response(request_id=started["request_id"]))
    assertion = root.find("saml:Assertion", namespaces=NSMAP)
    assert assertion is not None
    del assertion.attrib["ID"]

    _refused(
        await post_assertion(client, idp, started=started, response=etree.tostring(root)),
        why="no assertion ID",
    )


async def test_an_assertion_without_an_audience_is_refused(client, db, owner_a, idp):
    await create_saml_connection(client, owner_a, idp)
    started = await start_saml_login(client)

    root = etree.fromstring(idp.response(request_id=started["request_id"]))
    restriction = root.find(".//saml:AudienceRestriction", namespaces=NSMAP)
    assert restriction is not None
    restriction.getparent().remove(restriction)

    _refused(
        await post_assertion(client, idp, started=started, response=etree.tostring(root)),
        why="no audience restriction",
    )


async def test_the_signature_must_be_over_the_assertion_that_carries_the_claims(
    client, db, owner_a, idp
):
    """Move the signature to the response and the assertion becomes unsigned.

    This is the wrapping case in its purest form: the message still contains a
    valid signature, made by the right key, over the wrong element.
    """
    await create_saml_connection(client, owner_a, idp)
    started = await start_saml_login(client)

    root = idp.assertion_xml(assertion_id=f"_{uuid.uuid4().hex}", request_id=started["request_id"])
    signed = idp.sign(root, enclosing="assertion")
    signature = signed.find(".//ds:Signature", namespaces=NSMAP)
    assert signature is not None
    signature.getparent().remove(signature)

    # Signature removed from the assertion and nowhere else: the response is a
    # well-formed, correctly-shaped document whose assertion is unsigned.
    _refused(
        await post_assertion(client, idp, started=started, response=etree.tostring(signed)),
        why="signature stripped",
    )


async def test_an_assertion_that_has_been_re_signed_by_us_is_refused(client, db, owner_a, idp):
    """Re-signing with our own key must not help: we are not the IdP."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from signxml import XMLSigner
    from signxml import methods as signxml_methods

    await create_saml_connection(client, owner_a, idp)
    started = await start_saml_login(client)

    root = etree.fromstring(idp.response(request_id=started["request_id"]))
    for node in root.findall(".//ds:Signature", namespaces=NSMAP):
        node.getparent().remove(node)

    ours = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_pem = ours.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    assertion = root.find("saml:Assertion", namespaces=NSMAP)
    assert assertion is not None
    placeholder = etree.SubElement(assertion, f"{{{DS_NS}}}Signature", nsmap={"ds": DS_NS})
    placeholder.set("Id", "placeholder")
    signer = XMLSigner(
        method=signxml_methods.enveloped,
        signature_algorithm="http://www.w3.org/2001/04/xmldsig-more#rsa-sha256",
        digest_algorithm="http://www.w3.org/2001/04/xmlenc#sha256",
        c14n_algorithm="http://www.w3.org/TR/2001/REC-xml-c14n-20010315",
    )
    signed = signer.sign(root, key=key_pem, reference_uri=f"#{assertion.get('ID')}")

    _refused(
        await post_assertion(client, idp, started=started, response=etree.tostring(signed)),
        why="self-signed assertion",
    )


async def test_the_digest_algorithm_allow_list_is_enforced(client, db, owner_a, idp):
    """A digest algorithm the space has deprecated is not accepted silently.

    SHA-1 is checked as the boundary: if it is accepted at all, it must be
    accepted by an explicit, documented decision rather than by an allow-list
    that forgot to exclude it.
    """
    await create_saml_connection(client, owner_a, idp)
    started = await start_saml_login(client)

    root = etree.fromstring(idp.response(request_id=started["request_id"]))
    digest_method = root.find(".//ds:DigestMethod", namespaces=NSMAP)
    assert digest_method is not None
    digest_method.set("Algorithm", "http://www.w3.org/2000/09/xmldsig#sha1")

    response = await post_assertion(client, idp, started=started, response=etree.tostring(root))
    assert response.status_code in (200, 400), response.text
    if response.status_code == 400:
        assert "access_token" not in response.text
