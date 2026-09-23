"""OpenID Connect: discovery, the authorization-code flow, and ID-token checks.

The flow is the standard one, with the parts that are usually skipped spelled
out because they are the parts that matter:

* **Discovery is validated, not trusted.** The document's ``issuer`` must equal
  the issuer the connection was configured with, and the endpoints must be
  absolute HTTPS (or loopback HTTP in development). A misconfigured
  ``discovery_url`` would otherwise be enough to point a tenant's logins at a
  server of someone else's choosing.
* **State and nonce are both used, and both are stored hashed.** State prevents
  CSRF against the callback; nonce binds the ID token to *this* authorization
  request. Either one alone is insufficient, and neither is ever logged.
* **PKCE is used for every connection** (the schema default), because the
  client secret is shared with a browser-facing flow and a code interception
  should not be enough to complete a login.
* **The ID token is verified properly**: signature against the provider's JWKS
  with an algorithm allowlist (no ``none``, no HMAC), issuer, audience,
  expiry, and — for tokens with several audiences — the ``azp`` claim.
* **Keys rotate.** A JWKS is cached and re-fetched once when a token's ``kid``
  is unknown, which is the difference between a working rotation and an outage.
"""
from __future__ import annotations

import time
import urllib.parse
import uuid
from dataclasses import dataclass

import httpx
import jwt as pyjwt
import structlog

from app.auth.identity import tokens as identity_tokens
from app.auth.identity.exceptions import SSOConfigurationError, SSOValidationError
from app.core.config import settings

log = structlog.get_logger()

#: Asymmetric algorithms we will accept for an ID token signature. HMAC is
#: excluded on purpose: a client secret is not a signing key, and accepting
#: HS256 next to RS256 is the classic algorithm-confusion bug.
ALLOWED_ID_TOKEN_ALGORITHMS = (
    "RS256",
    "RS384",
    "RS512",
    "ES256",
    "ES384",
    "ES512",
    "PS256",
    "PS384",
    "PS512",
)

_DISCOVERY_TTL_SECONDS = 3600
_JWKS_TTL_SECONDS = 3600
_HTTP_TIMEOUT = 10.0


@dataclass(frozen=True)
class DiscoveredProvider:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    userinfo_endpoint: str = ""
    end_session_endpoint: str = ""
    supported_algorithms: tuple[str, ...] = ()

    def supports(self, algorithm: str) -> bool:
        if not self.supported_algorithms:
            return algorithm in ALLOWED_ID_TOKEN_ALGORITHMS
        return algorithm in self.supported_algorithms


#: In-process caches. A process-wide cache is correct here: the documents are
#: public, identical for every request, and change on the IdP's schedule, not
#: ours. Tests clear them with ``reset_caches``.
_discovery_cache: dict[str, tuple[float, DiscoveredProvider]] = {}
_jwks_cache: dict[str, tuple[float, dict]] = {}


def reset_caches() -> None:
    _discovery_cache.clear()
    _jwks_cache.clear()


def _require_https(url: str, *, what: str) -> None:
    parsed = urllib.parse.urlparse(url or "")
    if parsed.scheme == "https":
        return
    if parsed.scheme == "http" and (parsed.hostname in ("localhost", "127.0.0.1", "::1")):
        # Development against a local provider. Allowed, and only there.
        if not settings.is_production:
            return
    raise SSOConfigurationError(f"{what} must be an absolute https URL.")


def is_acceptable_https_url(url: str) -> bool:
    """Whether this URL passes the posture rule, without raising.

    Exists so an administrator-facing validator and the runtime check cannot
    drift apart: both call the same rule. ``http`` is acceptable only for a
    loopback host outside production, which is what makes local development
    possible without opening the door in a real deployment.
    """
    try:
        _require_https(url, what="URL")
    except SSOConfigurationError:
        return False
    return True


async def discover(connection, *, force: bool = False) -> DiscoveredProvider:
    """Fetch and validate the provider's discovery document.

    The URL is either the configured ``discovery_url`` or
    ``{issuer}/.well-known/openid-configuration``.
    """
    url = connection.discovery_url
    if not url and connection.issuer:
        url = connection.issuer.rstrip("/") + "/.well-known/openid-configuration"
    if not url:
        raise SSOConfigurationError("The connection has neither an issuer nor a discovery URL.")
    _require_https(url, what="The discovery URL")

    cached = _discovery_cache.get(url)
    now = time.time()
    if cached and not force and now - cached[0] < _DISCOVERY_TTL_SECONDS:
        return cached[1]

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            response = await client.get(url, headers={"accept": "application/json"})
    except httpx.HTTPError as exc:
        raise SSOConfigurationError("The identity provider's discovery document "
                                    "could not be reached.") from exc
    if response.status_code != 200:
        raise SSOConfigurationError(
            f"The identity provider returned HTTP {response.status_code} for its "
            "discovery document."
        )
    try:
        document = response.json()
    except ValueError as exc:
        raise SSOConfigurationError("The discovery document is not valid JSON.") from exc

    issuer = str(document.get("issuer") or "")
    if not issuer:
        raise SSOConfigurationError("The discovery document does not name an issuer.")
    if connection.issuer and issuer.rstrip("/") != connection.issuer.rstrip("/"):
        # The document is allowed to normalise the issuer's trailing slash and
        # nothing else. Anything more means the URL and the issuer disagree,
        # which is exactly the misconfiguration an attacker benefits from.
        raise SSOConfigurationError(
            "The discovery document's issuer does not match the configured issuer."
        )

    authorization_endpoint = str(document.get("authorization_endpoint") or "")
    token_endpoint = str(document.get("token_endpoint") or "")
    jwks_uri = str(document.get("jwks_uri") or "")
    for value, what in (
        (authorization_endpoint, "The authorization endpoint"),
        (token_endpoint, "The token endpoint"),
        (jwks_uri, "The JWKS URI"),
    ):
        if not value:
            raise SSOConfigurationError(f"{what} is missing from the discovery document.")
        _require_https(value, what=what)

    provider = DiscoveredProvider(
        issuer=issuer,
        authorization_endpoint=authorization_endpoint,
        token_endpoint=token_endpoint,
        jwks_uri=jwks_uri,
        userinfo_endpoint=str(document.get("userinfo_endpoint") or ""),
        end_session_endpoint=str(document.get("end_session_endpoint") or ""),
        supported_algorithms=tuple(
            a for a in (document.get("id_token_signing_alg_values_supported") or [])
            if isinstance(a, str)
        ),
    )
    _discovery_cache[url] = (now, provider)
    return provider


def build_authorization_url(
    connection,
    provider: DiscoveredProvider,
    *,
    redirect_uri: str,
    state: str,
    nonce: str,
    code_challenge: str | None,
) -> str:
    """The URL the browser is sent to. Nothing secret is in it."""
    params = {
        "response_type": "code",
        "client_id": connection.client_id or "",
        "redirect_uri": redirect_uri,
        "scope": connection.scopes or "openid email profile",
        "state": state,
        "nonce": nonce,
    }
    if code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    separator = "&" if "?" in provider.authorization_endpoint else "?"
    return f"{provider.authorization_endpoint}{separator}{urllib.parse.urlencode(params)}"


@dataclass(frozen=True)
class TokenResponse:
    id_token: str
    access_token: str = ""
    refresh_token: str = ""
    expires_in: int = 0
    token_type: str = "Bearer"
    scope: str = ""


async def exchange_code(
    connection,
    provider: DiscoveredProvider,
    *,
    code: str,
    redirect_uri: str,
    code_verifier: str | None,
) -> TokenResponse:
    """Swap the authorization code for tokens.

    A failure here is reported as a *validation* error with a short reason, not
    with the provider's body: the body can echo back the code or the client id,
    and it is not the tenant administrator's business what the IdP's error
    payload looks like.
    """
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": connection.client_id or "",
    }
    if code_verifier:
        data["code_verifier"] = code_verifier

    auth = None
    if connection.client_secret_encrypted:
        from app.auth.identity import secrets as identity_secrets

        secret = identity_secrets.decrypt_text(
            connection.client_secret_encrypted,
            tenant_id=str(connection.tenant_id),
            purpose=identity_secrets.PURPOSE_OIDC_CLIENT_SECRET,
        )
        # RFC 6749 §2.3.1: the client credentials go in the Authorization
        # header, so the secret never appears in a URL or in a proxy log.
        auth = (connection.client_id or "", secret)

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            response = await client.post(
                provider.token_endpoint,
                data=data,
                auth=auth,
                headers={"accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise SSOValidationError("The identity provider's token endpoint could not be reached.") from exc

    if response.status_code != 200:
        log.warning(
            "sso.oidc_token_exchange_failed",
            connection_id=str(connection.id),
            status=response.status_code,
            tenant_id=str(connection.tenant_id),
        )
        raise SSOValidationError("The identity provider rejected the authorization code.")
    try:
        payload = response.json()
    except ValueError as exc:
        raise SSOValidationError("The token endpoint returned an unreadable response.") from exc

    if payload.get("error"):
        raise SSOValidationError("The identity provider rejected the authorization code.")
    id_token = str(payload.get("id_token") or "")
    if not id_token:
        raise SSOValidationError("The identity provider did not return an ID token.")
    return TokenResponse(
        id_token=id_token,
        access_token=str(payload.get("access_token") or ""),
        refresh_token=str(payload.get("refresh_token") or ""),
        expires_in=int(payload.get("expires_in") or 0),
        token_type=str(payload.get("token_type") or "Bearer"),
        scope=str(payload.get("scope") or ""),
    )


async def fetch_jwks(provider: DiscoveredProvider, *, force: bool = False) -> dict:
    cached = _jwks_cache.get(provider.jwks_uri)
    now = time.time()
    if cached and not force and now - cached[0] < _JWKS_TTL_SECONDS:
        return cached[1]
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            response = await client.get(provider.jwks_uri, headers={"accept": "application/json"})
    except httpx.HTTPError as exc:
        raise SSOValidationError("The identity provider's key set could not be fetched.") from exc
    if response.status_code != 200:
        raise SSOValidationError("The identity provider's key set could not be fetched.")
    document = response.json()
    if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
        raise SSOValidationError("The identity provider's key set is malformed.")
    _jwks_cache[provider.jwks_uri] = (now, document)
    return document


def _signing_key(jwks: dict, kid: str) -> object:
    for entry in jwks.get("keys", []):
        if not isinstance(entry, dict):
            continue
        if kid and entry.get("kid") != kid:
            continue
        algorithm = str(entry.get("alg") or "")
        if algorithm and algorithm not in ALLOWED_ID_TOKEN_ALGORITHMS:
            continue
        try:
            return pyjwt.PyJWK.from_dict(entry).key
        except Exception as exc:  # noqa: BLE001 - any unusable key is "not found"
            log.warning("sso.jwks_key_unusable", error_type=type(exc).__name__)
            continue
    raise SSOValidationError("No usable signing key was found for the ID token.")


async def verify_id_token(
    connection,
    provider: DiscoveredProvider,
    *,
    id_token: str,
    nonce_hash: str,
) -> dict:
    """Verify an ID token and return its claims.

    The nonce travels as a *hash* because that is all the attempt row keeps: the
    value the token carries is hashed here and compared, so the nonce needed to
    complete this login is never stored anywhere. Verification stays inside this
    function so a caller cannot forget it.

    Raises ``SSOValidationError`` for anything that is not provably valid: a bad
    signature, a wrong issuer or audience, an expired token, a mismatched nonce,
    or an algorithm we do not accept.
    """
    try:
        header = pyjwt.get_unverified_header(id_token)
    except pyjwt.InvalidTokenError as exc:
        raise SSOValidationError("The ID token is malformed.") from exc

    algorithm = str(header.get("alg") or "")
    if algorithm not in ALLOWED_ID_TOKEN_ALGORITHMS:
        raise SSOValidationError("The ID token is signed with an algorithm we do not accept.")
    if not provider.supports(algorithm):
        raise SSOValidationError(
            "The identity provider's discovery document does not list this signing algorithm."
        )

    kid = str(header.get("kid") or "")
    jwks = await fetch_jwks(provider)
    try:
        key = _signing_key(jwks, kid)
    except SSOValidationError:
        # One forced refresh: this is what makes a key rotation a non-event.
        jwks = await fetch_jwks(provider, force=True)
        key = _signing_key(jwks, kid)

    try:
        claims = pyjwt.decode(
            id_token,
            key=key,
            algorithms=[algorithm],
            audience=connection.client_id or "",
            issuer=provider.issuer,
            leeway=float(settings.sso_clock_skew_seconds),
            options={
                "require": ["exp", "iat", "iss", "aud", "sub"],
                "verify_signature": True,
                "verify_exp": True,
                "verify_aud": True,
                "verify_iss": True,
            },
        )
    except pyjwt.ExpiredSignatureError as exc:
        raise SSOValidationError("The ID token has expired.") from exc
    except pyjwt.InvalidTokenError as exc:
        raise SSOValidationError("The ID token could not be verified.") from exc

    presented_nonce = str(claims.get("nonce") or "")
    if not presented_nonce or not nonce_hash:
        raise SSOValidationError("The ID token does not match this sign-in attempt.")
    import hmac

    if not hmac.compare_digest(
        identity_tokens.hash_token(presented_nonce), nonce_hash
    ):
        # The nonce is what ties this token to *our* authorization request. A
        # missing or different value means the token was minted for somebody
        # else's request.
        raise SSOValidationError("The ID token does not match this sign-in attempt.")

    audiences = claims.get("aud")
    if isinstance(audiences, list) and len(audiences) > 1:
        if str(claims.get("azp") or "") != (connection.client_id or ""):
            raise SSOValidationError(
                "The ID token is addressed to more than one audience without a "
                "matching authorized party."
            )

    return claims


def new_nonce() -> str:
    return uuid.uuid4().hex
