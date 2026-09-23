"""A local identity provider, and the plumbing to put it behind a connection.

Nothing in ``tests/auth/sso`` talks to a real IdP, and nothing there mocks the
verification code under test. What is faked is the *network*: one RSA key pair
mints real SAML assertions (signed by ``signxml``, an independent
implementation, so agreement with the hand-written verifier means the signature
is genuinely right) and real OIDC ID tokens (signed by ``PyJWT``), and a stub
HTTP client serves discovery, token-exchange and JWKS responses from a script.

That is the only way to test the cases a working provider never produces:
a signature from a certificate nobody trusts, an assertion addressed to another
SP, a token minted for another client, a key rotation in the middle of a login.

``StubOidcTransport`` is the one piece that has to be careful: it answers by
URL, so a test that expects a token failure still gets a *valid* discovery
document, and a test that rotates keys can change the JWKS without re-patching.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import uuid
import zlib

import httpx
from types import SimpleNamespace

import jwt as pyjwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from lxml import etree
from signxml import XMLSigner
from signxml import methods as signxml_methods

from app.auth.identity import secrets as identity_secrets
from app.auth.identity.sso import oidc, saml

SAMLP_NS = "urn:oasis:names:tc:SAML:2.0:protocol"
SAML_NS = "urn:oasis:names:tc:SAML:2.0:assertion"
DS_NS = "http://www.w3.org/2000/09/xmldsig#"
NSMAP = {"samlp": SAMLP_NS, "saml": SAML_NS, "ds": DS_NS}

IDP_ENTITY = "https://idp.example.com"
SP_ENTITY = "https://app.voxdesk.test/saml/metadata"
ACS_URL = "https://app.voxdesk.test/auth/sso/acme/acs"
CLIENT_ID = "client-abc"
TENANT_ID = "11111111-1111-1111-1111-111111111111"


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class IdP:
    """One RSA key pair, one self-signed certificate, and the documents it signs."""

    def __init__(self, common_name: str = "idp.example.com", *, kid: str | None = None) -> None:
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
        self.kid = kid or f"key-{uuid.uuid4().hex[:8]}"

    # ------------------------------------------------------------- SAML ------

    def assertion_xml(
        self,
        *,
        assertion_id: str,
        request_id: str = "_request-1234",
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
        extra_attributes: dict[str, str] | None = None,
        status: str = saml.SAML_SUCCESS,
    ) -> etree._Element:
        now = dt.datetime.now(dt.timezone.utc)
        nb = not_before or now - dt.timedelta(minutes=1)
        noa = not_on_or_after or now + dt.timedelta(minutes=5)
        subject_deadline = subject_not_on_or_after or noa
        group_nodes = "".join(
            f"<saml:AttributeValue>{value}</saml:AttributeValue>" for value in groups
        )
        extra = "".join(
            f'<saml:Attribute Name="{key}"><saml:AttributeValue>{value}</saml:AttributeValue></saml:Attribute>'
            for key, value in (extra_attributes or {}).items()
        )
        in_response_to = (
            f' InResponseTo="{request_id}"' if request_id is not None else ""
        )
        return etree.fromstring(
            f"""<samlp:Response xmlns:samlp="{SAMLP_NS}" xmlns:saml="{SAML_NS}"
                ID="_{uuid.uuid4().hex}" Version="2.0" IssueInstant="{iso(now)}"
                Destination="{destination}"{in_response_to}>
              <saml:Issuer>{entity}</saml:Issuer>
              <samlp:Status><samlp:StatusCode Value="{status}"/></samlp:Status>
              <saml:Assertion ID="{assertion_id}" Version="2.0" IssueInstant="{iso(now)}">
                <saml:Issuer>{entity}</saml:Issuer>
                <saml:Subject>
                  <saml:NameID Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress">{name_id}</saml:NameID>
                  <saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">
                    <saml:SubjectConfirmationData InResponseTo="{request_id}"
                        NotOnOrAfter="{iso(subject_deadline)}" Recipient="{recipient}"/>
                  </saml:SubjectConfirmation>
                </saml:Subject>
                <saml:Conditions NotBefore="{iso(nb)}" NotOnOrAfter="{iso(noa)}">
                  <saml:AudienceRestriction><saml:Audience>{audience}</saml:Audience></saml:AudienceRestriction>
                </saml:Conditions>
                <saml:AttributeStatement>
                  <saml:Attribute Name="email"><saml:AttributeValue>{email}</saml:AttributeValue></saml:Attribute>
                  <saml:Attribute Name="name"><saml:AttributeValue>Ada Lovelace</saml:AttributeValue></saml:Attribute>
                  <saml:Attribute Name="groups">{group_nodes}</saml:Attribute>
                  {extra}
                </saml:AttributeStatement>
              </saml:Assertion>
            </samlp:Response>""".encode()  # noqa: S320 - a fixed, literal template
        )

    def sign(self, root: etree._Element, *, enclosing: str = "assertion") -> etree._Element:
        """Sign the assertion (or the whole response) and return the tree."""
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
        return signer.sign(
            root if enclosing == "assertion" else target,
            key=self.key_pem,
            cert=self.certificate_pem,
            reference_uri=f"#{target.get('ID')}",
        )

    def response(
        self, *, assertion_id: str | None = None, sign: bool = True, **kwargs
    ) -> bytes:
        root = self.assertion_xml(
            assertion_id=assertion_id or f"_{uuid.uuid4().hex}", **kwargs
        )
        if sign:
            root = self.sign(root)
        return etree.tostring(root)

    def posted_response(self, **kwargs) -> str:
        """The base64 form an IdP puts in the ``SAMLResponse`` form field."""
        return base64.b64encode(self.response(**kwargs)).decode()

    # ------------------------------------------------------------- OIDC ------

    def jwks(self) -> dict:
        numbers = self.key.public_key().public_numbers()
        return {
            "keys": [
                {
                    "kty": "RSA",
                    "use": "sig",
                    "alg": "RS256",
                    "kid": self.kid,
                    "n": b64url(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
                    "e": b64url(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
                }
            ]
        }

    def id_token(
        self, claims: dict, *, kid: str | None = None, algorithm: str = "RS256", key=None
    ) -> str:
        """Mint an ID token the way the provider would.

        ``algorithm="none"`` produces a genuinely unsigned token (an empty
        signature), which is what the "unsigned tokens are refused" case needs:
        ``RS256`` uses this provider's private key, ``HS256`` uses the client
        secret, and any other algorithm must be given a ``key``.
        """
        if algorithm == "none":
            signing_key = None
        elif key is not None:
            signing_key = key
        elif algorithm == "RS256":
            signing_key = self.key_pem
        else:
            signing_key = "client-secret"
        return pyjwt.encode(
            claims, signing_key, algorithm=algorithm, headers={"kid": kid or self.kid}
        )


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


def discovered_provider(**overrides) -> oidc.DiscoveredProvider:
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
        "discovery_url": f"{IDP_ENTITY}/.well-known/openid-configuration",
        "protocol": "oidc",
        "require_verified_email": True,
        "client_secret_encrypted": "",
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
    """The shape ``trusted_certificates`` reads, sealed the way the app seals it."""
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


class StubOidcTransport:
    """An httpx client stand-in that answers discovery, JWKS and token calls.

    ``documents`` maps a URL to a JSON body (or an exception to raise). Anything
    not listed gets a 404, which is what makes a test that forgets to script a
    call fail loudly instead of silently succeeding.
    """

    def __init__(self, *, discovery: dict | None = None, jwks: dict | None = None,
                 token: dict | None = None, overrides: dict | None = None) -> None:
        self.documents: dict[str, object] = {}
        self.calls: list[tuple[str, str]] = []
        if discovery is not None:
            self.documents[f"{IDP_ENTITY}/.well-known/openid-configuration"] = discovery
        if jwks is not None:
            self.documents[f"{IDP_ENTITY}/jwks"] = jwks
        if token is not None:
            self.documents[f"{IDP_ENTITY}/token"] = token
        for url, payload in (overrides or {}).items():
            self.documents[url] = payload

    def install(self, monkeypatch) -> "StubOidcTransport":
        transport = self

        class _Response:
            def __init__(self, url: str, payload, status_code: int) -> None:
                self.url = url
                self._payload = payload
                self.status_code = status_code

            def json(self):
                if isinstance(self._payload, Exception):
                    raise self._payload
                return self._payload

        class _Client:
            def __init__(self, **kwargs) -> None:
                self.kwargs = kwargs

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc) -> bool:
                return False

            async def get(self, url, headers=None):
                return transport._answer("GET", url)

            async def post(self, url, data=None, auth=None, headers=None):
                transport.posts = getattr(transport, "posts", [])
                transport.posts.append({"url": url, "data": data, "auth": auth})
                return transport._answer("POST", url)

        monkeypatch.setattr(oidc.httpx, "AsyncClient", _Client)
        return self

    def _answer(self, method: str, url: str):
        self.calls.append((method, url))
        payload = self.documents.get(url, {"error": "not_found"})
        if isinstance(payload, httpx.HTTPError):
            # Scripted network failures must reach the caller as httpx errors,
            # because that is the branch the fetch code catches.
            raise payload
        status = 404 if payload == {"error": "not_found"} else 200

        class _Response:
            def __init__(self, payload, status_code: int) -> None:
                self._payload = payload
                self.status_code = status_code

            def json(self):
                if isinstance(self._payload, Exception):
                    raise self._payload
                return self._payload

        return _Response(payload, status)


def default_documents(idp: IdP, **token_overrides) -> dict:
    """A working provider: valid discovery, valid JWKS, valid token endpoint."""
    discovery = {
        "issuer": IDP_ENTITY,
        "authorization_endpoint": f"{IDP_ENTITY}/authorize",
        "token_endpoint": f"{IDP_ENTITY}/token",
        "jwks_uri": f"{IDP_ENTITY}/jwks",
        "id_token_signing_alg_values_supported": ["RS256"],
    }
    return {
        "discovery": discovery,
        "jwks": idp.jwks(),
        "token": {"id_token": idp.id_token(token_claims(**token_overrides))},
    }


def as_json(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True)


# --------------------------------------------------------------- HTTP setup ---
#
# The helpers below drive the real admin API to build a connection, and the real
# public endpoints to run a login. They are plain functions rather than fixtures
# so a test can call them twice (two connections, two tenants) without any
# fixture gymnastics.


async def create_connection(
    client, owner, *, payload: dict, certificate_pem: str | None = None, activate: bool = True
) -> dict:
    """Create a connection through ``POST /api/sso/connections`` and arm it."""
    from tests.conftest import auth_headers

    payload = dict(payload)
    # Mappings live on their own endpoint: keep them out of the create payload
    # and apply them afterwards.
    mappings = {
        key: payload.pop(key)
        for key in ("group_mapping", "role_mapping")
        if payload.get(key) is not None
    }
    headers = await auth_headers(client, owner)
    created = await client.post("/api/sso/connections", json=payload, headers=headers)
    assert created.status_code == 201, created.text
    connection = created.json()

    if certificate_pem:
        added = await client.post(
            f"/api/sso/connections/{connection['id']}/certificates",
            json={"certificate": certificate_pem, "make_active": True},
            headers=headers,
        )
        assert added.status_code == 201, added.text

    if mappings:
        connection = await set_mappings(client, owner, connection, **mappings)

    if activate:
        active = await client.post(
            f"/api/sso/connections/{connection['id']}/status",
            json={"status": "active"},
            headers=headers,
        )
        assert active.status_code == 200, active.text
        connection = active.json()
    return connection


async def set_mappings(client, owner, connection: dict, **mappings) -> dict:
    """Apply claim mappings through the endpoint that owns them.

    ``group_mapping`` / ``role_mapping`` are not part of the connection create
    payload: they are edited on their own route, which validates every target
    role. Tests that need a mapping go through here rather than passing it to
    ``create_saml_connection``.
    """
    from tests.conftest import auth_headers

    response = await client.put(
        f"/api/sso/connections/{connection['id']}/mappings",
        json=mappings,
        headers=await auth_headers(client, owner),
    )
    assert response.status_code == 200, response.text
    return response.json()


async def create_saml_connection(client, owner, idp: IdP, *, slug: str = "acme", **overrides) -> dict:
    payload = {
        "name": "Acme IdP",
        "slug": slug,
        "protocol": "saml",
        "idp_entity_id": IDP_ENTITY,
        "idp_sso_url": f"{IDP_ENTITY}/sso",
        "idp_slo_url": f"{IDP_ENTITY}/slo",
        "sp_entity_id": SP_ENTITY,
        "acs_url": ACS_URL,
        "require_signed_assertions": True,
        "default_role": "agent",
        "jit_enabled": True,
        "allow_account_linking": False,
        # Left open here so the JIT happy path has a role to assign. The strict
        # setting is exercised on purpose in test_saml_mapping.py and
        # test_oidc_account_linking.py.
        "deny_unmapped_roles": False,
    }
    payload.update(overrides)
    return await create_connection(
        client, owner, payload=payload, certificate_pem=idp.certificate_pem
    )


async def create_oidc_connection(client, owner, *, slug: str = "acme", client_secret: str = "", **overrides) -> dict:
    payload = {
        "name": "Acme IdP",
        "slug": slug,
        "protocol": "oidc",
        "issuer": IDP_ENTITY,
        "discovery_url": f"{IDP_ENTITY}/.well-known/openid-configuration",
        "client_id": CLIENT_ID,
        "client_secret": client_secret,
        "scopes": "openid email profile",
        "use_pkce": True,
        "redirect_uri": f"https://app.voxdesk.test/auth/sso/{slug}/callback",
        "email_claim": "email",
        "default_role": "agent",
        "jit_enabled": True,
        "deny_unmapped_roles": False,
    }
    payload.update(overrides)
    return await create_connection(client, owner, payload=payload)


async def latest_attempt(db, connection_id):
    """The most recent login attempt for a connection, re-read from the database."""
    from sqlalchemy import select

    from app.auth.identity.models import SSOLoginAttempt

    return (
        await db.execute(
            select(SSOLoginAttempt)
            .where(SSOLoginAttempt.connection_id == connection_id)
            .order_by(SSOLoginAttempt.created_at.desc())
            .execution_options(populate_existing=True)
        )
    ).scalars().first()


def request_id_of(attempt) -> str:
    from app.auth.identity.sso.service import request_id_for

    return request_id_for(attempt)


def hand_back_token(idp: IdP, stub: StubOidcTransport, *, nonce: str, **claim_overrides) -> str:
    """Script the provider's token endpoint to answer with a signed ID token.

    Returns the token, so a test can also assert on what it contained.
    """
    token = idp.id_token(token_claims(nonce=nonce, **claim_overrides))
    stub.documents[f"{IDP_ENTITY}/token"] = {"id_token": token}
    return token


def query_of(url: str) -> dict:
    import urllib.parse

    return {
        key: values[0]
        for key, values in urllib.parse.parse_qs(
            urllib.parse.urlparse(url).query
        ).items()
    }


# ------------------------------------------------------- SAML over HTTP ---


def authn_request_id(authorization_url: str) -> str:
    """The request id inside a deflated AuthnRequest, read the way an IdP reads it.

    The IdP answers ``InResponseTo`` with this value, so a test that mints an
    assertion has to take it from the URL the login actually produced — which is
    also what proves the id is derived from the attempt rather than invented.
    """
    parameters = query_of(authorization_url)
    # ``parse_qs`` turns the ``+`` of standard base64 into a space, exactly as a
    # form decoder would; undo that before decoding.
    encoded = parameters["SAMLRequest"].replace(" ", "+")
    deflated = base64.b64decode(encoded)
    xml = zlib.decompress(deflated, -15).decode("utf-8")
    return etree.fromstring(xml.encode("utf-8")).get("ID")


async def start_saml_login(client, *, slug: str = "acme") -> dict:
    """POST the start endpoint and return its body, with the request id resolved."""
    started = await client.post(f"/auth/sso/{slug}/start")
    assert started.status_code == 200, started.text
    body = started.json()
    body["request_id"] = authn_request_id(body["authorization_url"])
    return body


async def post_assertion(client, idp: IdP, *, started: dict, slug: str = "acme", response: bytes | None = None, **kwargs):
    """Play the IdP: POST a signed response for the attempt that was started."""
    payload = response if response is not None else idp.response(
        request_id=started["request_id"], **kwargs
    )
    return await client.post(
        f"/auth/sso/{slug}/acs",
        data={
            "SAMLResponse": base64.b64encode(payload).decode(),
            "RelayState": started["state"],
        },
    )
