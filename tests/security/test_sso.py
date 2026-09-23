"""Federated sign-in, attacked on purpose.

Nothing here talks to a real identity provider: the IdP is a key pair this file
generates, a signed SAML assertion it mints, and an OIDC discovery document
served by a stub HTTP client. That is the only way to test the interesting
cases -- the ones a working provider never sends -- without waiting for one to
misbehave:

* an assertion signed by a certificate we do not trust;
* an assertion tampered with after it was signed;
* two assertions in one response (signature wrapping);
* a signature that covers a *different* element than the one carrying the
  claims;
* a token minted for another client, another issuer, or another request;
* a discovery document that disagrees with the configured issuer;
* a key rotation that must be survived without a restart.

The verification code under test is ``app.auth.identity.sso.saml``, which
implements XML-DSig digest and RSA/ECDSA verification by hand, and
``app.auth.identity.sso.oidc``. The signed fixtures are produced by
``signxml``, an independent implementation: if the hand-written verifier and
the library agree, the signature is right.
"""
from __future__ import annotations

import base64
import datetime as dt
import uuid
from types import SimpleNamespace

import jwt as pyjwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from lxml import etree
from signxml import XMLSigner, methods as signxml_methods

from app.auth.identity import secrets as identity_secrets
from app.auth.identity import tokens as identity_tokens
from app.auth.identity.exceptions import SSOConfigurationError, SSOValidationError
from app.auth.identity.sso import oidc, saml
from app.core.config import settings

pytestmark = pytest.mark.asyncio

SAML_NS = "urn:oasis:names:tc:SAML:2.0:assertion"
SAMLP_NS = "urn:oasis:names:tc:SAML:2.0:protocol"
DS_NS = "http://www.w3.org/2000/09/xmldsig#"
NSMAP = {"saml": SAML_NS, "samlp": SAMLP_NS, "ds": DS_NS}

IDP_ENTITY = "https://idp.example.com"
SP_ENTITY = "https://app.voxdesk.test/saml/metadata"
ACS_URL = "https://app.voxdesk.test/auth/sso/acme/acs"
REQUEST_ID = "_request-1234"
CLIENT_ID = "client-abc"
TENANT_ID = "11111111-1111-1111-1111-111111111111"


def _iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


class IdP:
    """A local identity provider: one RSA key, one self-signed certificate."""

    def __init__(self, common_name: str = "idp.example.com") -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
        now = dt.datetime.now(dt.timezone.utc)
        self.certificate = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(self.key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=365))
            .sign(self.key, hashes.SHA256())
        )
        self.certificate_pem = self.certificate.public_bytes(
            serialization.Encoding.PEM
        ).decode()
        self.key_pem = self.key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        self.kid = f"key-{uuid.uuid4().hex[:8]}"

    # ------------------------------------------------------------ SAML ------

    def assertion_xml(
        self,
        *,
        assertion_id: str,
        in_response_to: str = REQUEST_ID,
        audience: str = SP_ENTITY,
        recipient: str = ACS_URL,
        destination: str = ACS_URL,
        not_before: dt.datetime | None = None,
        not_on_or_after: dt.datetime | None = None,
        subject_not_on_or_after: dt.datetime | None = None,
        entity: str = IDP_ENTITY,
        email: str = "ada@acme.test",
        name_id: str = "user-1",
        groups: tuple[str, ...] = ("agents",),
        status: str = saml.SAML_SUCCESS,
    ) -> etree._Element:
        now = dt.datetime.now(dt.timezone.utc)
        not_before = not_before or now - dt.timedelta(minutes=1)
        not_on_or_after = not_on_or_after or now + dt.timedelta(minutes=5)
        subject_deadline = subject_not_on_or_after or not_on_or_after
        group_nodes = "".join(
            f"<saml:AttributeValue>{value}</saml:AttributeValue>" for value in groups
        )
        return etree.fromstring(
            f"""<samlp:Response xmlns:samlp="{SAMLP_NS}" xmlns:saml="{SAML_NS}"
                ID="_{uuid.uuid4().hex}" Version="2.0" IssueInstant="{_iso(now)}"
                Destination="{destination}">
              <saml:Issuer>{entity}</saml:Issuer>
              <samlp:Status>
                <samlp:StatusCode Value="{status}"/>
              </samlp:Status>
              <saml:Assertion ID="{assertion_id}" Version="2.0" IssueInstant="{_iso(now)}">
                <saml:Issuer>{entity}</saml:Issuer>
                <saml:Subject>
                  <saml:NameID Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress">{name_id}</saml:NameID>
                  <saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">
                    <saml:SubjectConfirmationData InResponseTo="{in_response_to}"
                        NotOnOrAfter="{_iso(subject_deadline)}" Recipient="{recipient}"/>
                  </saml:SubjectConfirmation>
                </saml:Subject>
                <saml:Conditions NotBefore="{_iso(not_before)}" NotOnOrAfter="{_iso(not_on_or_after)}">
                  <saml:AudienceRestriction>
                    <saml:Audience>{audience}</saml:Audience>
                  </saml:AudienceRestriction>
                </saml:Conditions>
                <saml:AttributeStatement>
                  <saml:Attribute Name="email">
                    <saml:AttributeValue>{email}</saml:AttributeValue>
                  </saml:Attribute>
                  <saml:Attribute Name="groups">{group_nodes}</saml:Attribute>
                </saml:AttributeStatement>
              </saml:Assertion>
            </samlp:Response>""".encode()
        )

    def sign(self, root: etree._Element, *, enclosing: str = "assertion") -> etree._Element:
        """Sign the assertion (or the response) and return the signed tree.

        ``signxml`` resolves the reference by ID, so the signature covers
        exactly the element named here -- which is what the verifier re-derives
        independently.
        """
        target = (
            root.find("saml:Assertion", namespaces=NSMAP)
            if enclosing == "assertion"
            else root
        )
        assert target is not None
        placeholder = etree.SubElement(target, f"{{{DS_NS}}}Signature", nsmap={"ds": DS_NS})
        placeholder.set("Id", "placeholder")
        signer = XMLSigner(
            method=signxml_methods.enveloped,
            signature_algorithm="http://www.w3.org/2001/04/xmldsig-more#rsa-sha256",
            digest_algorithm="http://www.w3.org/2001/04/xmlenc#sha256",
            c14n_algorithm="http://www.w3.org/TR/2001/REC-xml-c14n-20010315",
        )
        signed = signer.sign(
            root if enclosing == "assertion" else target,
            key=self.key_pem,
            cert=self.certificate_pem,
            reference_uri=f"#{target.get('ID')}",
        )
        return signed

    def response(self, *, assertion_id: str | None = None, sign: bool = True, **kwargs) -> bytes:
        assertion_id = assertion_id or f"_{uuid.uuid4().hex}"
        root = self.assertion_xml(assertion_id=assertion_id, **kwargs)
        if sign:
            root = self.sign(root)
        return etree.tostring(root)

    # ------------------------------------------------------------ OIDC ------

    def jwks(self) -> dict:
        numbers = self.key.public_key().public_numbers()
        return {
            "keys": [
                {
                    "kty": "RSA",
                    "use": "sig",
                    "alg": "RS256",
                    "kid": self.kid,
                    "n": _b64url(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
                    "e": _b64url(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
                }
            ]
        }

    def id_token(self, claims: dict, *, kid: str | None = None, algorithm: str = "RS256") -> str:
        key = self.key_pem if algorithm == "RS256" else "client-secret"
        return pyjwt.encode(claims, key, algorithm=algorithm, headers={"kid": kid or self.kid})


@pytest.fixture(scope="module")
def idp() -> IdP:
    return IdP()


@pytest.fixture
def other_idp() -> IdP:
    """A second provider: the one whose certificate is *not* on file."""
    return IdP(common_name="attacker.example.net")


@pytest.fixture(autouse=True)
def _fresh_sso_caches():
    oidc.reset_caches()
    yield
    oidc.reset_caches()


def token_claims(**overrides) -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    claims = {
        "iss": IDP_ENTITY,
        "aud": CLIENT_ID,
        "sub": "user-1",
        "exp": int((now + dt.timedelta(minutes=5)).timestamp()),
        "iat": int(now.timestamp()),
        "email": "ada@acme.test",
        "email_verified": True,
    }
    claims.update(overrides)
    return claims


def provider(**overrides) -> oidc.DiscoveredProvider:
    values = {
        "issuer": IDP_ENTITY,
        "authorization_endpoint": f"{IDP_ENTITY}/authorize",
        "token_endpoint": f"{IDP_ENTITY}/token",
        "jwks_uri": f"{IDP_ENTITY}/jwks",
        "supported_algorithms": ("RS256",),
    }
    values.update(overrides)
    return oidc.DiscoveredProvider(**values)


def oidc_connection(**overrides) -> SimpleNamespace:
    values = {
        "client_id": CLIENT_ID,
        "issuer": IDP_ENTITY,
        "discovery_url": "",
        "protocol": "oidc",
        "require_verified_email": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def saml_connection(**overrides) -> SimpleNamespace:
    values = {
        "id": uuid.uuid4(),
        "tenant_id": TENANT_ID,
        "protocol": "saml",
        "require_signed_assertions": True,
        "idp_entity_id": IDP_ENTITY,
        "entity_id": SP_ENTITY,
        "status": "active",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def certificate_row(certificate_pem: str, *, connection=None, active: bool = True):
    """The shape ``_trusted_certificates`` reads, sealed the way the app seals it."""
    envelope, _key_id = identity_secrets.encrypt_text(
        certificate_pem,
        tenant_id=TENANT_ID,
        purpose=identity_secrets.PURPOSE_SAML_CERTIFICATE,
    )
    return SimpleNamespace(
        tenant_id=TENANT_ID,
        connection_id=getattr(connection, "id", uuid.uuid4()),
        pem_encrypted=envelope,
        fingerprint_sha256=saml.certificate_fingerprint(certificate_pem),
        is_active=active,
    )


def stub_http_client(monkeypatch, payload, *, status_code: int = 200):
    """Replace the HTTP client OIDC discovery uses, and count the calls."""
    calls: list[str] = []

    class _Response:
        def __init__(self) -> None:
            self.status_code = status_code

        def json(self):
            if isinstance(payload, Exception):
                raise payload
            return payload

    class _Client:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc) -> bool:
            return False

        async def get(self, url, headers=None):
            calls.append(url)
            return _Response()

    monkeypatch.setattr(oidc.httpx, "AsyncClient", _Client)
    return calls


# ===================================================== OIDC: discovery ===


async def test_discovery_must_name_the_configured_issuer(monkeypatch):
    stub_http_client(
        monkeypatch,
        {
            "issuer": "https://somewhere-else.example.com",
            "authorization_endpoint": f"{IDP_ENTITY}/authorize",
            "token_endpoint": f"{IDP_ENTITY}/token",
            "jwks_uri": f"{IDP_ENTITY}/jwks",
        },
    )
    with pytest.raises(SSOConfigurationError):
        await oidc.discover(oidc_connection())


async def test_discovery_refuses_an_insecure_endpoint(monkeypatch):
    stub_http_client(
        monkeypatch,
        {
            "issuer": IDP_ENTITY,
            "authorization_endpoint": f"{IDP_ENTITY}/authorize",
            "token_endpoint": f"{IDP_ENTITY}/token",
            "jwks_uri": "http://idp.example.com/jwks",
        },
    )
    with pytest.raises(SSOConfigurationError):
        await oidc.discover(oidc_connection())


async def test_discovery_refuses_a_document_without_a_key_set(monkeypatch):
    stub_http_client(
        monkeypatch,
        {
            "issuer": IDP_ENTITY,
            "authorization_endpoint": f"{IDP_ENTITY}/authorize",
            "token_endpoint": f"{IDP_ENTITY}/token",
        },
    )
    with pytest.raises(SSOConfigurationError):
        await oidc.discover(oidc_connection())


async def test_discovery_is_cached_and_can_be_forced(monkeypatch):
    calls = stub_http_client(
        monkeypatch,
        {
            "issuer": IDP_ENTITY,
            "authorization_endpoint": f"{IDP_ENTITY}/authorize",
            "token_endpoint": f"{IDP_ENTITY}/token",
            "jwks_uri": f"{IDP_ENTITY}/jwks",
            "id_token_signing_alg_values_supported": ["RS256"],
        },
    )
    connection = oidc_connection()
    first = await oidc.discover(connection)
    again = await oidc.discover(connection)
    assert again is first, "the second call is served from the cache"
    assert first.issuer == IDP_ENTITY
    assert first.supports("RS256")
    assert calls == [f"{IDP_ENTITY}/.well-known/openid-configuration"], "one fetch, cached"

    await oidc.discover(connection, force=True)
    assert len(calls) == 2, "force is how a rotation is picked up without a restart"


@pytest.mark.parametrize(
    ("url", "acceptable"),
    [
        ("https://idp.example.com/jwks", True),
        ("http://idp.example.com/jwks", False),
        ("", False),
        ("javascript:alert(1)", False),
        ("https://user:secret@idp.example.com/jwks", True),
    ],
)
def test_the_https_posture_rule(url, acceptable):
    assert oidc.is_acceptable_https_url(url) is acceptable


# ================================================== OIDC: ID tokens ===


async def test_a_correctly_signed_id_token_is_accepted(idp, monkeypatch):
    monkeypatch.setattr(oidc, "fetch_jwks", lambda provider, force=False: _jwks(idp))
    nonce = "nonce-value"
    token = idp.id_token(token_claims(nonce=nonce))
    claims = await oidc.verify_id_token(
        oidc_connection(),
        provider(),
        id_token=token,
        nonce_hash=identity_tokens.hash_token(nonce),
    )
    assert claims["sub"] == "user-1"


async def _jwks(idp: IdP) -> dict:
    return idp.jwks()


async def test_a_token_from_another_issuer_is_refused(idp, monkeypatch):
    monkeypatch.setattr(oidc, "fetch_jwks", lambda provider, force=False: _jwks(idp))
    token = idp.id_token(token_claims(iss="https://evil.example.com", nonce="n"))
    with pytest.raises(SSOValidationError):
        await oidc.verify_id_token(
            oidc_connection(), provider(), id_token=token, nonce_hash=identity_tokens.hash_token("n")
        )


async def test_a_token_for_another_client_is_refused(idp, monkeypatch):
    monkeypatch.setattr(oidc, "fetch_jwks", lambda provider, force=False: _jwks(idp))
    token = idp.id_token(token_claims(aud="another-client", nonce="n"))
    with pytest.raises(SSOValidationError):
        await oidc.verify_id_token(
            oidc_connection(), provider(), id_token=token, nonce_hash=identity_tokens.hash_token("n")
        )


async def test_an_expired_id_token_is_refused(idp, monkeypatch):
    monkeypatch.setattr(oidc, "fetch_jwks", lambda provider, force=False: _jwks(idp))
    past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
        seconds=settings.sso_clock_skew_seconds + 300
    )
    token = idp.id_token(token_claims(exp=int(past.timestamp()), nonce="n"))
    with pytest.raises(SSOValidationError):
        await oidc.verify_id_token(
            oidc_connection(), provider(), id_token=token, nonce_hash=identity_tokens.hash_token("n")
        )


async def test_a_token_inside_the_clock_skew_is_still_accepted(idp, monkeypatch):
    """Clocks drift. A token that expired within the configured skew is valid."""
    monkeypatch.setattr(oidc, "fetch_jwks", lambda provider, force=False: _jwks(idp))
    just_expired = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
        seconds=max(1, settings.sso_clock_skew_seconds - 30)
    )
    token = idp.id_token(token_claims(exp=int(just_expired.timestamp()), nonce="n"))
    claims = await oidc.verify_id_token(
        oidc_connection(), provider(), id_token=token, nonce_hash=identity_tokens.hash_token("n")
    )
    assert claims["sub"] == "user-1"


async def test_a_token_from_another_sign_in_attempt_is_refused(idp, monkeypatch):
    monkeypatch.setattr(oidc, "fetch_jwks", lambda provider, force=False: _jwks(idp))
    token = idp.id_token(token_claims(nonce="a-different-nonce"))
    with pytest.raises(SSOValidationError):
        await oidc.verify_id_token(
            oidc_connection(),
            provider(),
            id_token=token,
            nonce_hash=identity_tokens.hash_token("our-nonce"),
        )


async def test_a_token_without_a_nonce_is_refused(idp, monkeypatch):
    monkeypatch.setattr(oidc, "fetch_jwks", lambda provider, force=False: _jwks(idp))
    token = idp.id_token(token_claims())
    with pytest.raises(SSOValidationError):
        await oidc.verify_id_token(
            oidc_connection(),
            provider(),
            id_token=token,
            nonce_hash=identity_tokens.hash_token("our-nonce"),
        )


async def test_an_unsigned_token_is_refused(idp, monkeypatch):
    """``alg: none`` is the oldest trick there is."""
    monkeypatch.setattr(oidc, "fetch_jwks", lambda provider, force=False: _jwks(idp))
    unsigned = pyjwt.encode(token_claims(nonce="n"), key="", algorithm="none")
    with pytest.raises(SSOValidationError):
        await oidc.verify_id_token(
            oidc_connection(), provider(), id_token=unsigned, nonce_hash=identity_tokens.hash_token("n")
        )


async def test_a_symmetric_token_is_refused(idp, monkeypatch):
    """A token signed with the client secret, not the provider's key."""
    monkeypatch.setattr(oidc, "fetch_jwks", lambda provider, force=False: _jwks(idp))
    token = idp.id_token(token_claims(nonce="n"), algorithm="HS256")
    with pytest.raises(SSOValidationError):
        await oidc.verify_id_token(
            oidc_connection(), provider(), id_token=token, nonce_hash=identity_tokens.hash_token("n")
        )


async def test_a_token_signed_by_an_unknown_key_is_refused(idp, monkeypatch):
    monkeypatch.setattr(oidc, "fetch_jwks", lambda provider, force=False: _jwks(IdP("someone.else")))
    token = idp.id_token(token_claims(nonce="n"))
    with pytest.raises(SSOValidationError):
        await oidc.verify_id_token(
            oidc_connection(), provider(), id_token=token, nonce_hash=identity_tokens.hash_token("n")
        )


async def test_a_rotated_key_is_picked_up_without_a_restart(idp, monkeypatch):
    """An unknown ``kid`` costs one extra JWKS fetch, not a failed login."""
    calls: list[bool] = []
    fresh = idp.jwks()

    async def fetch(_provider, *, force: bool = False) -> dict:
        calls.append(force)
        return {"keys": []} if not force else fresh

    monkeypatch.setattr(oidc, "fetch_jwks", fetch)
    claims = await oidc.verify_id_token(
        oidc_connection(),
        provider(),
        id_token=idp.id_token(token_claims(nonce="n")),
        nonce_hash=identity_tokens.hash_token("n"),
    )
    assert claims["sub"] == "user-1"
    assert calls == [False, True], "cached key set first, then one forced refresh"


async def test_a_multi_audience_token_needs_a_matching_authorized_party(idp, monkeypatch):
    monkeypatch.setattr(oidc, "fetch_jwks", lambda provider, force=False: _jwks(idp))
    both = ["another-client", CLIENT_ID]
    with pytest.raises(SSOValidationError):
        await oidc.verify_id_token(
            oidc_connection(),
            provider(),
            id_token=idp.id_token(token_claims(aud=both, nonce="n")),
            nonce_hash=identity_tokens.hash_token("n"),
        )

    accepted = await oidc.verify_id_token(
        oidc_connection(),
        provider(),
        id_token=idp.id_token(token_claims(aud=both, azp=CLIENT_ID, nonce="n")),
        nonce_hash=identity_tokens.hash_token("n"),
    )
    assert accepted["azp"] == CLIENT_ID


async def test_a_provider_that_does_not_offer_the_algorithm_is_refused(idp, monkeypatch):
    monkeypatch.setattr(oidc, "fetch_jwks", lambda provider, force=False: _jwks(idp))
    with pytest.raises(SSOValidationError):
        await oidc.verify_id_token(
            oidc_connection(),
            provider(supported_algorithms=("ES256",)),
            id_token=idp.id_token(token_claims(nonce="n")),
            nonce_hash=identity_tokens.hash_token("n"),
        )


# ================================================= SAML: happy path ===


async def _verify(idp: IdP, xml: bytes, *, connection=None, certificates=None, request_id=REQUEST_ID):
    connection = connection or saml_connection()
    rows = certificates if certificates is not None else [certificate_row(idp.certificate_pem)]
    root = saml.safe_parse(xml)
    parsed = saml.parse_response(root=root)
    signature = saml.signature_of(root, parsed, connection=connection, certificates=rows)
    saml.validate_assertion(
        parsed,
        connection=connection,
        expected_acs_url=ACS_URL,
        expected_request_id=request_id,
        expected_entity_id=SP_ENTITY,
        certificates=rows,
    )
    return parsed, signature


async def test_a_correctly_signed_assertion_is_accepted(idp):
    parsed, signature = await _verify(idp, idp.response())
    assert parsed.name_id == "user-1"
    assert parsed.attributes["email"] == "ada@acme.test"
    # One value is reported as a string, several as a list: a claim mapper
    # should not have to guess which shape it was handed.
    assert parsed.attributes["groups"] == "agents"
    assert parsed.audience == (SP_ENTITY,)
    assert parsed.in_response_to == REQUEST_ID
    assert signature is not None
    assert signature.signed_element == "assertion"
    assert signature.used_sha1 is False
    assert signature.fingerprint == saml.certificate_fingerprint(idp.certificate_pem)
    assert signature.subject  # the operator needs to see *which* certificate matched


async def test_multiple_group_values_are_reported_as_a_list(idp):
    xml = idp.response(groups=("agents", "managers"))
    parsed, _signature = await _verify(idp, xml)
    assert parsed.attributes["groups"] == ["agents", "managers"]


async def test_a_tampered_assertion_is_refused(idp):
    """The signature covers the claims, so editing a claim must break it."""
    xml = idp.response()
    root = saml.safe_parse(xml)
    for node in root.iter(f"{{{SAML_NS}}}AttributeValue"):
        if node.text == "ada@acme.test":
            node.text = "attacker@acme.test"
    tampered = etree.tostring(root)
    with pytest.raises(SSOValidationError):
        await _verify(idp, tampered)


async def test_an_unsigned_assertion_is_refused_even_inside_a_signed_response(idp):
    """The setting that stops a signed envelope from protecting a swapped body."""
    signed_response = idp.sign(
        idp.assertion_xml(assertion_id=f"_{uuid.uuid4().hex}"), enclosing="response"
    )
    xml = etree.tostring(signed_response)
    with pytest.raises(SSOValidationError) as refused:
        await _verify(idp, xml)
    assert "signed" in str(refused.value).lower()

    # The same response is acceptable to a connection that does not require it.
    parsed, signature = await _verify(
        idp, xml, connection=saml_connection(require_signed_assertions=False)
    )
    assert signature.signed_element == "response"
    assert parsed.name_id == "user-1"


async def test_an_assertion_signed_by_an_untrusted_certificate_is_refused(idp, other_idp):
    xml = other_idp.response()
    with pytest.raises(SSOValidationError) as refused:
        await _verify(idp, xml)
    message = str(refused.value)
    assert "not trust" in message
    # The fingerprint is named so an operator can compare; the certificate is not.
    assert oidc.is_acceptable_https_url(IDP_ENTITY)
    assert other_idp.certificate_pem.strip() not in message


async def test_a_response_with_two_assertions_is_refused(idp):
    """Signature wrapping: a signed assertion plus an unsigned one to be read."""
    root = saml.safe_parse(idp.response())
    extra = etree.fromstring(
        f"""<saml:Assertion xmlns:saml="{SAML_NS}" ID="_{uuid.uuid4().hex}" Version="2.0">
          <saml:Issuer>{IDP_ENTITY}</saml:Issuer>
          <saml:Subject><saml:NameID>someone-else</saml:NameID></saml:Subject>
        </saml:Assertion>""".encode()
    )
    root.insert(0, extra)
    with pytest.raises(SSOValidationError) as refused:
        await _verify(idp, etree.tostring(root))
    assert "exactly one assertion" in str(refused.value)


async def test_a_signature_that_covers_a_different_element_is_refused(idp):
    """The reference must name the element the signature sits in."""
    root = saml.safe_parse(idp.response())
    assertion = root.find("saml:Assertion", namespaces=NSMAP)
    reference = assertion.find(".//ds:Reference", namespaces=NSMAP)
    reference.set("URI", f"#{root.get('ID')}")
    with pytest.raises(SSOValidationError):
        await _verify(idp, etree.tostring(root))


async def test_a_signature_with_two_references_is_refused(idp):
    root = saml.safe_parse(idp.response())
    signed_info = root.find(".//ds:SignedInfo", namespaces=NSMAP)
    signed_info.append(etree.fromstring(signed_info[0].tag and etree.tostring(signed_info[0])))
    with pytest.raises(SSOValidationError):
        await _verify(idp, etree.tostring(root))


async def test_an_unsupported_digest_algorithm_is_refused(idp):
    root = saml.safe_parse(idp.response())
    digest_method = root.find(".//ds:DigestMethod", namespaces=NSMAP)
    digest_method.set("Algorithm", "http://www.w3.org/2000/09/xmldsig#md5")
    with pytest.raises(SSOValidationError):
        await _verify(idp, etree.tostring(root))


async def test_a_non_success_status_is_refused(idp):
    xml = idp.response(status="urn:oasis:names:tc:SAML:2.0:status:Requester")
    with pytest.raises(SSOValidationError):
        await _verify(idp, xml)


# ============================================ SAML: assertion content ===


async def test_an_assertion_answering_another_request_is_refused(idp):
    xml = idp.response(in_response_to="_someone-elses-request")
    with pytest.raises(SSOValidationError):
        await _verify(idp, xml)


async def test_an_assertion_for_another_service_provider_is_refused(idp):
    xml = idp.response(audience="https://another-app.example.com/saml/metadata")
    with pytest.raises(SSOValidationError):
        await _verify(idp, xml)


async def test_an_assertion_without_an_audience_restriction_is_refused(idp):
    root = saml.safe_parse(idp.response())
    conditions = root.find(".//saml:Conditions", namespaces=NSMAP)
    conditions.remove(conditions.find("saml:AudienceRestriction", namespaces=NSMAP))
    with pytest.raises(SSOValidationError):
        await _verify(idp, etree.tostring(root))


async def test_an_assertion_sent_to_another_recipient_is_refused(idp):
    xml = idp.response(recipient="https://attacker.example.com/acs")
    with pytest.raises(SSOValidationError):
        await _verify(idp, xml)


async def test_an_assertion_addressed_elsewhere_is_refused(idp):
    xml = idp.response(destination="https://attacker.example.com/acs")
    with pytest.raises(SSOValidationError):
        await _verify(idp, xml)


async def test_an_assertion_from_another_entity_is_refused(idp):
    xml = idp.response(entity="https://elsewhere.example.org")
    with pytest.raises(SSOValidationError):
        await _verify(idp, xml)


async def test_an_expired_assertion_is_refused(idp):
    long_ago = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
        seconds=settings.sso_clock_skew_seconds + 60
    )
    xml = idp.response(
        not_before=long_ago - dt.timedelta(minutes=5), not_on_or_after=long_ago
    )
    with pytest.raises(SSOValidationError) as refused:
        await _verify(idp, xml)
    assert "expired" in str(refused.value)


async def test_an_assertion_whose_subject_confirmation_expired_is_refused(idp):
    long_ago = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
        seconds=settings.sso_clock_skew_seconds + 60
    )
    xml = idp.response(subject_not_on_or_after=long_ago)
    with pytest.raises(SSOValidationError):
        await _verify(idp, xml)


async def test_an_assertion_from_the_future_is_refused(idp):
    later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(
        seconds=settings.sso_clock_skew_seconds + 600
    )
    xml = idp.response(not_before=later, not_on_or_after=later + dt.timedelta(minutes=5))
    with pytest.raises(SSOValidationError):
        await _verify(idp, xml)


async def test_an_assertion_without_an_expiry_is_refused(idp):
    root = saml.safe_parse(idp.response())
    conditions = root.find(".//saml:Conditions", namespaces=NSMAP)
    del conditions.attrib["NotOnOrAfter"]
    confirmation = root.find(".//saml:SubjectConfirmationData", namespaces=NSMAP)
    del confirmation.attrib["NotOnOrAfter"]
    with pytest.raises(SSOValidationError):
        await _verify(idp, etree.tostring(root))


async def test_a_connection_without_a_certificate_cannot_verify_anything(idp):
    """Fail closed: no certificate on file means no federation, not a free pass."""
    with pytest.raises(SSOConfigurationError):
        await _verify(idp, idp.response(), certificates=[])


async def test_a_certificate_that_cannot_be_decrypted_is_ignored(idp):
    row = certificate_row(idp.certificate_pem)
    row.pem_encrypted = "not-a-valid-envelope"
    with pytest.raises(SSOConfigurationError):
        await _verify(idp, idp.response(), certificates=[row])


# ===================================================== SAML: certificates ===


def test_certificate_parsing_accepts_pem_and_a_pasted_base64_body(idp):
    from_pem = saml.read_certificate(idp.certificate_pem)
    der = idp.certificate.public_bytes(serialization.Encoding.DER)
    from_body = saml.read_certificate(base64.b64encode(der).decode())
    assert from_pem.fingerprint == from_body.fingerprint
    assert from_body.pem.startswith("-----BEGIN CERTIFICATE-----")

    wrapped = saml.read_certificate(
        f'<ds:X509Certificate xmlns:ds="{DS_NS}">{base64.b64encode(der).decode()}</ds:X509Certificate>'
    )
    assert wrapped.fingerprint == from_pem.fingerprint


@pytest.mark.parametrize("junk", ["", "not base64 !!", "-----BEGIN CERTIFICATE-----\nnope\n"])
def test_unusable_certificate_material_is_refused(junk):
    with pytest.raises(SSOConfigurationError):
        saml.read_certificate(junk)


def test_sp_metadata_declares_the_acs_without_advertising_an_unused_key(idp):
    xml = saml.sp_metadata(entity_id=SP_ENTITY, acs_url=ACS_URL, slo_url=f"{ACS_URL}/slo")
    document = etree.fromstring(xml.encode())
    assert document.get("entityID") == SP_ENTITY
    assert document.find(".//md:AssertionConsumerService", namespaces={
        "md": "urn:oasis:names:tc:SAML:2.0:metadata"
    }).get("Location") == ACS_URL
    assert "KeyDescriptor" not in xml, "we do not sign AuthnRequests, so we advertise no key"


def test_an_authn_request_is_unique_and_carries_the_acs(idp):
    """Redirect-binding request: deflate, base64, urlencode -- and a fresh id.

    A request id that repeats is a replayable login, so the id is asserted to
    differ between two requests and to be an XML NCName (letter first), which
    is what the IdP requires of the ``InResponseTo`` it must echo back.
    """
    import urllib.parse
    import zlib

    def request() -> tuple[str, str]:
        request_id = saml.new_request_id()
        query = saml.build_authn_request(
            destination=f"{IDP_ENTITY}/sso",
            acs_url=ACS_URL,
            issuer=SP_ENTITY,
            request_id=request_id,
        )
        encoded = urllib.parse.parse_qs(query)["SAMLRequest"][0]
        raw = base64.b64decode(encoded)
        xml = zlib.decompress(raw, -15).decode("utf-8")
        return request_id, xml

    first_id, first_xml = request()
    second_id, _ = request()
    assert first_id != second_id
    assert first_id[0].isalpha(), "an XML ID must be an NCName"
    assert f"ID=\"{first_id}\"" in first_xml
    assert f'AssertionConsumerServiceURL="{ACS_URL}"' in first_xml
    assert f"<saml:Issuer>{SP_ENTITY}</saml:Issuer>" in first_xml
    assert f'Destination="{IDP_ENTITY}/sso"' in first_xml


def test_an_xml_document_type_declaration_is_refused_before_parsing(idp):
    """No DTD means no entity expansion and no external fetch, ever."""
    with pytest.raises(SSOValidationError):
        saml.safe_parse(
            b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
            b"<samlp:Response/>"
        )
