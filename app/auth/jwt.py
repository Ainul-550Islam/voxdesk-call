"""
JWT issuing and verification.

Design notes:
  * HS256 with a configured secret. The algorithm is pinned on decode, so a
    token claiming `alg: none` or `alg: RS256` is rejected rather than
    accepted with an attacker-supplied key.
  * Issuer and audience are always validated.
  * The access token carries `tid` (tenant) and `role`. Tenant is therefore a
    signed claim -- a client cannot change which tenant it belongs to.
  * `tv` (token version) mirrors User.token_version, letting deactivation and
    role changes take effect before the token expires.
  * `sid` (STEP 18) names the browser session the token was minted for, so
    "sign out this device" revokes a token that would otherwise stay valid for
    its full lifetime. It is emitted only when a caller opened a session; a
    token without it keeps working exactly as before, which is what lets the
    identity layer ship without invalidating anyone's current login.
  * `amr`/`mfa` (STEP 18) record *how* the principal authenticated and whether
    the session satisfied a second factor. They are informational claims: the
    authoritative copies are the session row and the user's factor rows, and
    every privileged check reads those, never the token.
  * Refresh tokens are opaque random strings, NOT JWTs, so they can only be
    validated against the database and can be revoked.
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt

from app.core.config import settings

ALGORITHM = "HS256"
TOKEN_TYPE_ACCESS = "access"

REFRESH_TOKEN_BYTES = 48


class TokenError(Exception):
    """Any failure to produce a trustworthy principal from a token."""


@dataclass(frozen=True)
class TokenClaims:
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    role: str
    token_version: int
    expires_at: datetime
    jti: str
    #: STEP 18 additions, all optional so tokens minted before this feature and
    #: tokens minted for non-session callers decode exactly as they used to.
    session_id: uuid.UUID | None = None
    auth_method: str = ""
    mfa_verified: bool = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_access_token(
    *,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    role: str,
    token_version: int,
    expires_minutes: int | None = None,
    session_id: uuid.UUID | None = None,
    auth_method: str = "",
    mfa_verified: bool = False,
    mfa_verified_at: int | None = None,
) -> tuple[str, int]:
    """Returns (token, expires_in_seconds).

    Everything after ``expires_minutes`` is optional; omitting all of it
    produces byte-for-byte the payload this function produced before the
    identity work, which is the compatibility guarantee the existing callers
    (and their tests) rely on.
    """
    ttl = timedelta(
        minutes=expires_minutes
        if expires_minutes is not None
        else settings.access_token_minutes
    )
    issued = _now()
    expires = issued + ttl
    payload = {
        "sub": str(user_id),
        "tid": str(tenant_id),
        "role": role,
        "tv": token_version,
        "typ": TOKEN_TYPE_ACCESS,
        "iat": int(issued.timestamp()),
        "nbf": int(issued.timestamp()),
        "exp": int(expires.timestamp()),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "jti": secrets.token_urlsafe(12),
    }
    if session_id is not None:
        payload["sid"] = str(session_id)
    if auth_method:
        payload["amr"] = auth_method
    if mfa_verified:
        payload["mfa"] = True
    if mfa_verified_at is not None:
        payload["mfa_at"] = int(mfa_verified_at)
    token = jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)
    return token, int(ttl.total_seconds())


def decode_access_token(token: str) -> TokenClaims:
    """Raise TokenError on anything that is not a valid, current access token."""
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[ALGORITHM],          # pinned: no alg confusion
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            options={
                "require": ["exp", "iat", "sub", "iss", "aud"],
                "verify_signature": True,
                "verify_exp": True,
                "verify_aud": True,
                "verify_iss": True,
            },
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("token expired") from exc
    except jwt.InvalidTokenError as exc:
        # Covers bad signature, wrong issuer/audience, malformed, alg=none.
        raise TokenError("invalid token") from exc

    if payload.get("typ") != TOKEN_TYPE_ACCESS:
        raise TokenError("wrong token type")

    try:
        raw_sid = payload.get("sid")
        return TokenClaims(
            user_id=uuid.UUID(payload["sub"]),
            tenant_id=uuid.UUID(payload["tid"]),
            role=str(payload["role"]),
            token_version=int(payload.get("tv", 0)),
            expires_at=datetime.fromtimestamp(payload["exp"], tz=timezone.utc),
            jti=str(payload.get("jti", "")),
            session_id=uuid.UUID(raw_sid) if raw_sid else None,
            auth_method=str(payload.get("amr", "")),
            mfa_verified=bool(payload.get("mfa", False)),
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise TokenError("malformed claims") from exc


# ------------------------------------------------------------ refresh tokens ---

def generate_refresh_token() -> tuple[str, str]:
    """
    Returns (plaintext, sha256_hex).

    Only the digest is persisted. SHA-256 without a salt is correct here and
    not a password-hashing mistake: the input is 48 bytes of CSPRNG output, so
    there is no dictionary to attack, and the lookup must stay O(1).
    """
    plaintext = secrets.token_urlsafe(REFRESH_TOKEN_BYTES)
    return plaintext, hash_refresh_token(plaintext)


def hash_refresh_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def refresh_expiry() -> datetime:
    return _now() + timedelta(days=settings.refresh_token_days)