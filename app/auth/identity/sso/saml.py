"""SAML 2.0 service provider: metadata, AuthnRequest, and response validation.

The parsing here is deliberately paranoid, because a SAML response is
attacker-controlled XML arriving from outside:

* **No DTDs, no entities, no network.** ``resolve_entities=False``,
  ``no_network=True``, and a bare ``<!DOCTYPE`` is refused outright. That closes
  XXE and entity-expansion attacks before any parsing happens.
* **The signature is verified over canonicalised bytes.** The digest is computed
  over the *referenced element* with the enveloped-signature transform applied
  (the ``ds:Signature`` removed), using XML C14N — which is what makes
  signature wrapping fail: an attacker who moves the signed element elsewhere in
  the document changes the octets that were signed.
* **Only certificates the tenant configured are trusted.** A response carrying
  its own certificate is not thereby valid; the certificate must already be in
  the connection's certificate table (an active one, or a retired one still
  inside its overlap window).
* **Every structural check is made**: status, issuer, audience, recipient,
  destination, both time bounds, InResponseTo, and the subject confirmation.
  A missing check is not "lenient", it is a bypass.
* **Replay is prevented by the database.** The assertion ID is written to
  ``sso_login_attempts.assertion_id``, which is unique: the second callback with
  the same assertion cannot insert, so it cannot log in.

What is deliberately *not* implemented: signing our AuthnRequest. Requiring
signed assertions from the IdP makes that redundant (an attacker who could
rewrite our request cannot forge our response), and an unsigned request is what
most SPs send.
"""
from __future__ import annotations

import base64
import hashlib
import urllib.parse
import uuid
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import structlog
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from lxml import etree

from app.auth.identity.exceptions import SSOConfigurationError, SSOValidationError
from app.core.config import settings

log = structlog.get_logger()

NSMAP = {
    "samlp": "urn:oasis:names:tc:SAML:2.0:protocol",
    "saml": "urn:oasis:names:tc:SAML:2.0:assertion",
    "ds": "http://www.w3.org/2000/09/xmldsig#",
}
SAML_SUCCESS = "urn:oasis:names:tc:SAML:2.0:status:Success"
BEARER = "urn:oasis:names:tc:SAML:2.0:cm:bearer"
PERSISTENT_FORMAT = "urn:oasis:names:tc:SAML:2.0:nameid-format:persistent"

_C14N_EXCLUSIVE = "http://www.w3.org/2001/10/xml-exc-c14n#"
_C14N_INCLUSIVE = "http://www.w3.org/TR/2001/REC-xml-c14n-20010315"
_ENVELOPED = "http://www.w3.org/2000/09/xmldsig#enveloped-signature"

_DIGEST_METHODS = {
    "http://www.w3.org/2000/09/xmldsig#sha1": hashes.SHA1,
    "http://www.w3.org/2001/04/xmlenc#sha256": hashes.SHA256,
    "http://www.w3.org/2001/04/xmldsig-more#sha384": hashes.SHA384,
    "http://www.w3.org/2001/04/xmlenc#sha512": hashes.SHA512,
}
_SIGNATURE_METHODS = {
    "http://www.w3.org/2000/09/xmldsig#rsa-sha1": (hashes.SHA1, "rsa"),
    "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256": (hashes.SHA256, "rsa"),
    "http://www.w3.org/2001/04/xmldsig-more#rsa-sha384": (hashes.SHA384, "rsa"),
    "http://www.w3.org/2001/04/xmldsig-more#rsa-sha512": (hashes.SHA512, "rsa"),
    "http://www.w3.org/2001/04/xmldsig-more#ecdsa-sha256": (hashes.SHA256, "ecdsa"),
    "http://www.w3.org/2001/04/xmldsig-more#ecdsa-sha384": (hashes.SHA384, "ecdsa"),
    "http://www.w3.org/2001/04/xmldsig-more#ecdsa-sha512": (hashes.SHA512, "ecdsa"),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def parse_instant(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise SSOValidationError(f"The assertion carries an unreadable timestamp: {value!r}") from exc


# ==================================================================== XML ===


def safe_parse(xml: bytes) -> etree._Element:
    """Parse untrusted XML with every external-processing door closed."""
    if b"<!DOCTYPE" in xml.upper():
        # Refused before parsing rather than after: a document type declaration
        # has no legitimate use in a SAML response, and its absence removes the
        # entire XXE and entity-expansion class of attack.
        raise SSOValidationError("The SAML response contains a document type declaration.")
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        huge_tree=False,
        recover=False,
    )
    try:
        return etree.fromstring(xml, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise SSOValidationError("The SAML response is not valid XML.") from exc


def find(element: etree._Element, path: str):
    return element.find(path, namespaces=NSMAP)


def findall(element: etree._Element, path: str):
    return element.findall(path, namespaces=NSMAP)


def _c14n(element: etree._Element, *, exclusive: bool) -> bytes:
    return etree.tostring(
        element, method="c14n", exclusive=exclusive, with_comments=False
    )


def _element_by_id(root: etree._Element, identifier: str) -> etree._Element | None:
    for attribute in ("ID", "Id", "id"):
        found = root.xpath(f'//*[@*[local-name()="{attribute}"]=$i]', i=identifier)
        if found:
            return found[0]
    return None


def certificate_fingerprint(pem: str) -> str:
    cert = x509.load_pem_x509_certificate(pem.encode())
    return hashlib.sha256(cert.public_bytes(_der_encoding())).hexdigest()


def _der_encoding():
    from cryptography.hazmat.primitives.serialization import Encoding

    return Encoding.DER


@dataclass(frozen=True)
class VerifiedSignature:
    fingerprint: str
    subject: str
    issuer: str
    signed_element: str  # "response" | "assertion"
    used_sha1: bool = False


def _trusted_certificates(certificates: list) -> dict[str, str]:
    """``{fingerprint: pem}`` for the certificates this connection trusts."""
    trusted: dict[str, str] = {}
    for row in certificates:
        from app.auth.identity import secrets as identity_secrets

        try:
            pem = identity_secrets.decrypt_text(
                row.pem_encrypted,
                tenant_id=str(row.tenant_id),
                purpose=identity_secrets.PURPOSE_SAML_CERTIFICATE,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("sso.certificate_unreadable", connection_id=str(row.connection_id),
                        error_type=type(exc).__name__)
            continue
        trusted[row.fingerprint_sha256] = pem
    return trusted


def verify_signature(
    root: etree._Element,
    element: etree._Element,
    *,
    trusted: dict[str, str],
    signed_element: str,
) -> VerifiedSignature:
    """Verify the enveloped signature on ``element`` against trusted certs."""
    signature = element.find("ds:Signature", namespaces=NSMAP)
    if signature is None:
        raise SSOValidationError(f"The SAML {signed_element} is not signed.")

    signed_info = signature.find("ds:SignedInfo", namespaces=NSMAP)
    if signed_info is None:
        raise SSOValidationError("The SAML signature has no SignedInfo.")

    c14n_method = find(signed_info, "ds:CanonicalizationMethod")
    if c14n_method is None:
        raise SSOValidationError("The SAML signature has no canonicalization method.")
    exclusive = c14n_method.get("Algorithm") == _C14N_EXCLUSIVE
    if c14n_method.get("Algorithm") not in (_C14N_EXCLUSIVE, _C14N_INCLUSIVE):
        raise SSOValidationError("The SAML signature uses an unsupported canonicalization.")

    references = findall(signed_info, "ds:Reference")
    if len(references) != 1:
        # Multiple references are legal in XML-DSig and are the substrate of
        # signature-wrapping attacks. One is all a SAML assertion needs.
        raise SSOValidationError("The SAML signature must cover exactly one element.")
    reference = references[0]

    uri = reference.get("URI") or ""
    if not uri.startswith("#"):
        raise SSOValidationError("The SAML signature does not reference an element by ID.")
    target_id = uri[1:]
    target = _element_by_id(root, target_id)
    if target is None:
        raise SSOValidationError("The SAML signature references an element that is not present.")
    if target is not element:
        raise SSOValidationError(
            "The SAML signature does not cover the element it appears in."
        )

    transforms = [
        t.get("Algorithm") for t in findall(reference, "ds:Transforms/ds:Transform")
    ]
    enveloped = _ENVELOPED in transforms
    for algorithm in transforms:
        if algorithm not in (_ENVELOPED, _C14N_EXCLUSIVE, _C14N_INCLUSIVE):
            raise SSOValidationError(f"The SAML signature uses an unsupported transform: {algorithm}")

    digest_method = find(reference, "ds:DigestMethod")
    digest_value = find(reference, "ds:DigestValue")
    if digest_method is None or digest_value is None:
        raise SSOValidationError("The SAML signature has no digest.")
    digest_algorithm = _DIGEST_METHODS.get(digest_method.get("Algorithm") or "")
    if digest_algorithm is None:
        raise SSOValidationError("The SAML signature uses an unsupported digest algorithm.")

    # Digest the referenced element with the enveloped-signature transform
    # applied. Working on a *copy* leaves the original tree intact for the
    # structural checks that follow.
    working = etree.fromstring(etree.tostring(root))
    working_target = _element_by_id(working, target_id)
    if working_target is None:
        raise SSOValidationError("The SAML signature could not be evaluated.")
    if enveloped:
        for child in list(working_target.findall("ds:Signature", namespaces=NSMAP)):
            working_target.remove(child)
    canonical = _c14n(working_target, exclusive=exclusive)

    hasher = hashes.Hash(digest_algorithm())
    hasher.update(canonical)
    computed = base64.b64encode(hasher.finalize()).decode()
    if not _constant_time_equals(computed, (digest_value.text or "").strip()):
        raise SSOValidationError(
            "The SAML signature does not match the element it covers."
        )

    signature_method = find(signed_info, "ds:SignatureMethod")
    if signature_method is None:
        raise SSOValidationError("The SAML signature has no signature method.")
    method = _SIGNATURE_METHODS.get(signature_method.get("Algorithm") or "")
    if method is None:
        raise SSOValidationError("The SAML signature uses an unsupported signature algorithm.")
    hash_algorithm, key_kind = method

    signature_value = find(signature, "ds:SignatureValue")
    if signature_value is None or not (signature_value.text or "").strip():
        raise SSOValidationError("The SAML signature has no value.")
    try:
        raw_signature = base64.b64decode((signature_value.text or "").strip(), validate=True)
    except Exception as exc:  # noqa: BLE001
        raise SSOValidationError("The SAML signature is not valid base64.") from exc

    # Which certificate signed this? The one in the response, if we also have
    # the same certificate on file. A certificate we do not recognise is not a
    # reason to try harder -- it is a reason to stop.
    # The certificate the provider presented, used *only* to name it when
    # nothing trusted matches. It is never trusted because it arrived in the
    # response. Standard KeyInfo nests it inside X509Data; some providers leave
    # the wrapper out, so the lookup has a fallback.
    presented = find(signature, "ds:KeyInfo/ds:X509Data/ds:X509Certificate")
    if presented is None:
        candidates = signature.findall(
            ".//ds:KeyInfo//ds:X509Certificate", namespaces=NSMAP
        )
        presented = candidates[0] if candidates else None
    presented_der = None
    if presented is not None and (presented.text or "").strip():
        try:
            presented_der = base64.b64decode("".join((presented.text or "").split()), validate=True)
        except Exception as exc:  # noqa: BLE001
            raise SSOValidationError("The certificate in the SAML response is malformed.") from exc

    signed_info_bytes = _c14n(signed_info, exclusive=exclusive)

    for fingerprint, pem in trusted.items():
        try:
            certificate = x509.load_pem_x509_certificate(pem.encode())
        except Exception:  # noqa: BLE001
            continue
        if presented_der is not None:
            stored_der = certificate.public_bytes(_der_encoding())
            if not _constant_time_equals(
                hashlib.sha256(presented_der).hexdigest(),
                hashlib.sha256(stored_der).hexdigest(),
            ):
                continue
        public_key = certificate.public_key()
        try:
            if key_kind == "rsa" and isinstance(public_key, rsa.RSAPublicKey):
                public_key.verify(raw_signature, signed_info_bytes, padding.PKCS1v15(),
                                  hash_algorithm())
            elif key_kind == "ecdsa" and isinstance(public_key, ec.EllipticCurvePublicKey):
                public_key.verify(raw_signature, signed_info_bytes, ec.ECDSA(hash_algorithm()))
            else:
                continue
        except Exception:  # noqa: BLE001 - any verification failure is a non-match
            continue

        return VerifiedSignature(
            fingerprint=fingerprint,
            subject=certificate.subject.rfc4514_string(),
            issuer=certificate.issuer.rfc4514_string(),
            signed_element=signed_element,
            used_sha1="sha1" in (signature_method.get("Algorithm") or "").lower()
            or "sha1" in (digest_method.get("Algorithm") or "").lower(),
        )

    if presented_der is not None:
        # Say which certificate was presented, by fingerprint, so an operator
        # can compare it with what their IdP is currently using. The
        # certificate itself is not echoed.
        raise SSOValidationError(
            "The SAML response is signed by a certificate this connection does not trust "
            f"(presented fingerprint {hashlib.sha256(presented_der).hexdigest()[:16]})."
        )
    raise SSOValidationError("The SAML signature could not be verified with a trusted certificate.")


def _constant_time_equals(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left or "", right or "")


# ================================================================ parsing ===


@dataclass(frozen=True)
class ParsedAssertion:
    assertion_id: str
    name_id: str
    name_id_format: str
    issuer: str
    audience: tuple[str, ...]
    recipient: str
    destination: str
    in_response_to: str
    not_before: datetime | None
    not_on_or_after: datetime | None
    subject_not_on_or_after: datetime | None
    attributes: dict = field(default_factory=dict)
    signature: VerifiedSignature | None = None
    response_id: str = ""
    session_index: str = ""


def _attributes(assertion: etree._Element) -> dict:
    values: dict = {}
    for attribute in assertion.xpath(
        ".//saml:AttributeStatement/saml:Attribute", namespaces=NSMAP
    ):
        name = attribute.get("Name") or attribute.get("FriendlyName") or ""
        if not name:
            continue
        collected = []
        for value in attribute.xpath("./saml:AttributeValue", namespaces=NSMAP):
            if value.text is not None and value.text.strip():
                collected.append(value.text.strip())
            elif len(value):
                collected.append("".join(value.itertext()).strip())
        if not collected:
            continue
        existing = values.get(name)
        if existing is None:
            values[name] = collected[0] if len(collected) == 1 else collected
        elif isinstance(existing, list):
            existing.extend(collected)
        else:
            values[name] = [existing, *collected]
    return values


def parse_response(
    xml: bytes | None = None, *, root: "etree._Element | None" = None
) -> ParsedAssertion:
    """Parse a SAML ``Response`` into a structured assertion.

    Structural checks that do not depend on configuration (is this a success
    response? is there exactly one assertion?) happen here, so no caller can
    forget them. Configuration-dependent checks live in
    :func:`validate_assertion`.

    ``root`` lets a caller that has already parsed the document -- the ACS
    handler, which needs the tree for signature verification -- pass it in
    rather than parsing the same bytes twice. Parsing untrusted XML twice is not
    just wasted work; it is two chances for the two parses to disagree.
    """
    if root is None:
        if xml is None:
            raise SSOValidationError("No SAML response was supplied.")
        root = safe_parse(xml)

    if etree.QName(root).localname != "Response":
        raise SSOValidationError("The SAML payload is not a Response.")

    status = find(root, "samlp:Status/samlp:StatusCode")
    status_code = (status.get("Value") if status is not None else "") or ""
    if status_code != SAML_SUCCESS:
        raise SSOValidationError(f"The identity provider refused the sign-in ({status_code}).")

    assertions = findall(root, "saml:Assertion")
    if len(assertions) != 1:
        raise SSOValidationError("The SAML response must contain exactly one assertion.")
    assertion = assertions[0]

    conditions = find(assertion, "saml:Conditions")
    audience_nodes = (
        findall(conditions, "saml:AudienceRestriction/saml:Audience") if conditions is not None else []
    )
    confirmation = find(assertion, "saml:Subject/saml:SubjectConfirmation/saml:SubjectConfirmationData")
    name_id_node = find(assertion, "saml:Subject/saml:NameID")

    assertion_id = assertion.get("ID") or ""
    if not assertion_id:
        raise SSOValidationError("The SAML assertion has no ID, so it cannot be replayed safely.")

    return ParsedAssertion(
        assertion_id=assertion_id,
        name_id=(name_id_node.text or "").strip() if name_id_node is not None else "",
        name_id_format=(name_id_node.get("Format") if name_id_node is not None else "") or "",
        issuer=(find(assertion, "saml:Issuer").text or "").strip()
        if find(assertion, "saml:Issuer") is not None
        else "",
        audience=tuple((node.text or "").strip() for node in audience_nodes),
        recipient=(confirmation.get("Recipient") if confirmation is not None else "") or "",
        destination=root.get("Destination") or "",
        in_response_to=(confirmation.get("InResponseTo") if confirmation is not None else "")
        or root.get("InResponseTo")
        or "",
        not_before=parse_instant(conditions.get("NotBefore") if conditions is not None else None),
        not_on_or_after=parse_instant(
            conditions.get("NotOnOrAfter") if conditions is not None else None
        ),
        subject_not_on_or_after=parse_instant(
            confirmation.get("NotOnOrAfter") if confirmation is not None else None
        ),
        attributes=_attributes(assertion),
        response_id=root.get("ID") or "",
        session_index=(find(root, "samlp:SessionIndex").text or "")
        if find(root, "samlp:SessionIndex") is not None
        else "",
    )


def validate_assertion(
    parsed: ParsedAssertion,
    *,
    connection,
    expected_acs_url: str,
    expected_request_id: str,
    expected_entity_id: str,
    certificates: list,
    now: datetime | None = None,
) -> None:
    """Every configuration-dependent check on the assertion's *content*.

    Signature verification is a separate step (``signature_of``) because it
    needs the XML tree rather than the parsed facts. Raises
    ``SSOValidationError`` for every refusal, naming the check that failed — an
    operator chasing a broken integration needs to know it was the audience,
    not "invalid SAML".
    """
    moment = now or _now()
    skew = timedelta(seconds=settings.sso_clock_skew_seconds)

    if expected_request_id and parsed.in_response_to != expected_request_id:
        raise SSOValidationError(
            "The SAML response does not answer this sign-in attempt."
        )
    if connection.idp_entity_id and parsed.issuer != connection.idp_entity_id:
        raise SSOValidationError("The SAML assertion was issued by an unexpected entity.")
    if expected_entity_id and parsed.audience and expected_entity_id not in parsed.audience:
        raise SSOValidationError("The SAML assertion is addressed to a different audience.")
    if not parsed.audience:
        raise SSOValidationError("The SAML assertion has no audience restriction.")
    if parsed.recipient and parsed.recipient != expected_acs_url:
        raise SSOValidationError("The SAML assertion was sent to a different recipient.")
    if parsed.destination and parsed.destination != expected_acs_url:
        raise SSOValidationError("The SAML response was sent to a different destination.")

    if parsed.not_before is not None and _utc(parsed.not_before) > moment + skew:
        raise SSOValidationError("The SAML assertion is not valid yet.")
    deadline = parsed.not_on_or_after or parsed.subject_not_on_or_after
    if deadline is None:
        raise SSOValidationError("The SAML assertion has no expiry.")
    if _utc(deadline) + skew <= moment:
        raise SSOValidationError("The SAML assertion has expired.")
    if (
        parsed.subject_not_on_or_after is not None
        and _utc(parsed.subject_not_on_or_after) + skew <= moment
    ):
        raise SSOValidationError("The SAML subject confirmation has expired.")


def signature_of(root: etree._Element, parsed: ParsedAssertion, *, connection, certificates: list):
    """Verify the response's and the assertion's signatures under the policy.

    ``require_signed_assertions`` (the default) means an unsigned assertion is
    refused even when the Response around it carried a valid signature — that
    is the setting that stops a signed envelope from protecting a body someone
    else swapped.
    """
    trusted = _trusted_certificates(certificates)
    if not trusted:
        raise SSOConfigurationError(
            "No signing certificate is configured for this connection. Import the "
            "identity provider's certificate before signing in."
        )

    assertions = findall(root, "saml:Assertion")
    assertion_element = assertions[0] if assertions else root
    assertion_signature = None
    if assertion_element.find("ds:Signature", namespaces=NSMAP) is not None:
        assertion_signature = verify_signature(
            root, assertion_element, trusted=trusted, signed_element="assertion"
        )

    response_signature = None
    if root.find("ds:Signature", namespaces=NSMAP) is not None:
        response_signature = verify_signature(
            root, root, trusted=trusted, signed_element="response"
        )

    if connection.require_signed_assertions and assertion_signature is None:
        raise SSOValidationError(
            "The SAML assertion is not signed. This connection requires signed assertions."
        )
    if assertion_signature is None and response_signature is None:
        raise SSOValidationError("The SAML response is not signed.")
    return assertion_signature or response_signature


# ================================================ SP metadata / requests ===


def sp_metadata(*, entity_id: str, acs_url: str, slo_url: str = "", name: str = "VoxDesk") -> str:
    """The SP metadata an administrator uploads to their IdP.

    No ``KeyDescriptor``: we do not sign AuthnRequests, and advertising a key we
    do not use would be worse than omitting one.
    """
    slo = (
        f'      <md:SingleLogoutService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect" '
        f'Location="{slo_url}"/>\n'
        if slo_url
        else ""
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" '
        f'entityID="{entity_id}">\n'
        '  <md:SPSSODescriptor '
        'protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">\n'
        '    <md:NameIDFormat>urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress'
        "</md:NameIDFormat>\n"
        f"{slo}"
        f'    <md:AssertionConsumerService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST" '
        f'Location="{acs_url}" index="0" isDefault="true"/>\n'
        "  </md:SPSSODescriptor>\n"
        f'  <md:Organization><md:OrganizationName xml:lang="en">{name}</md:OrganizationName>'
        "<md:OrganizationDisplayName xml:lang=\"en\">"
        f"{name}</md:OrganizationDisplayName>"
        "<md:OrganizationURL xml:lang=\"en\">https://voxdesk.local</md:OrganizationURL>"
        "</md:Organization>\n"
        "</md:EntityDescriptor>\n"
    )


def build_authn_request(
    *,
    destination: str,
    acs_url: str,
    issuer: str,
    request_id: str,
    force_authn: bool = False,
) -> str:
    """A deflate+base64+urlencoded ``AuthnRequest`` for the Redirect binding."""
    instant = _now().strftime("%Y-%m-%dT%H:%M:%SZ")
    force = ' ForceAuthn="true"' if force_authn else ""
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<samlp:AuthnRequest xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" '
        'xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" '
        f'ID="{request_id}" Version="2.0" IssueInstant="{instant}" '
        f'Destination="{destination}" ProtocolBinding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST" '
        f'AssertionConsumerServiceURL="{acs_url}"{force}>'
        f"<saml:Issuer>{issuer}</saml:Issuer>"
        '<samlp:NameIDPolicy AllowCreate="true" '
        'Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress"/>'
        "</samlp:AuthnRequest>"
    )
    deflated = zlib.compress(xml.encode("utf-8"))[2:-4]  # raw DEFLATE, per SAML spec
    encoded = base64.b64encode(deflated).decode("ascii")
    return urllib.parse.urlencode({"SAMLRequest": encoded})


def new_request_id() -> str:
    """A SAML request ID: must be an XML NCName, so it starts with a letter."""
    return f"id-{uuid.uuid4().hex}"


# ======================================================== certificate import ===


@dataclass(frozen=True)
class CertificateInfo:
    fingerprint: str
    subject: str
    issuer: str
    not_before: datetime | None
    not_after: datetime | None
    pem: str


def read_certificate(material: str) -> CertificateInfo:
    """Parse a PEM certificate, or a base64 DER body as IdPs often paste it.

    Also accepts a whole metadata document or an X509Certificate element by
    extracting the base64 body — an operator pasting XML is the common case.
    """
    text = (material or "").strip()
    if not text:
        raise SSOConfigurationError("No certificate was provided.")

    if "-----BEGIN CERTIFICATE-----" in text:
        start = text.index("-----BEGIN CERTIFICATE-----")
        end = text.find("-----END CERTIFICATE-----")
        if end == -1:
            # A truncated paste is a configuration mistake, not a crash: the
            # administrator gets a validation message they can act on.
            raise SSOConfigurationError(
                "The certificate is incomplete: it starts with -----BEGIN "
                "CERTIFICATE----- but has no end marker."
            )
        pem = text[start : end + len("-----END CERTIFICATE-----")] + "\n"
    else:
        body = text
        if "<" in text:
            match = _extract_x509_body(text)
            if match is None:
                raise SSOConfigurationError(
                    "No certificate was found in the supplied document."
                )
            body = match
        body = "".join(body.split())
        try:
            base64.b64decode(body, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise SSOConfigurationError("The supplied certificate is not valid base64.") from exc
        pem = "-----BEGIN CERTIFICATE-----\n" + "\n".join(
            body[i : i + 64] for i in range(0, len(body), 64)
        ) + "\n-----END CERTIFICATE-----\n"

    try:
        certificate = x509.load_pem_x509_certificate(pem.encode())
    except Exception as exc:  # noqa: BLE001
        raise SSOConfigurationError("The supplied certificate could not be parsed.") from exc

    return CertificateInfo(
        fingerprint=hashlib.sha256(certificate.public_bytes(_der_encoding())).hexdigest(),
        subject=certificate.subject.rfc4514_string(),
        issuer=certificate.issuer.rfc4514_string(),
        not_before=certificate.not_valid_before_utc,
        not_after=certificate.not_valid_after_utc,
        pem=pem,
    )


def _extract_x509_body(text: str) -> str | None:

    if "<" not in text:
        return None
    try:
        root = safe_parse(text.encode("utf-8"))
    except SSOValidationError:
        return None
    nodes = root.xpath('//*[local-name()="X509Certificate"]')
    if not nodes:
        return None
    return (nodes[0].text or "").strip() or None
