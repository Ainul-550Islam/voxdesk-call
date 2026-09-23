"""Opaque secret generation, hashing and comparison.

Every credential this feature mints — API keys, service-account credentials,
SCIM tokens, one-time password-reset and email-verification tokens, SSO state
values, their PKCE verifiers, recovery codes — is generated here, and every one
of them is stored as a digest. Nothing in this module can produce a value whose
plaintext is recoverable from the database, which is the property the whole
design rests on: a database dump yields no usable credential.

The digest is plain SHA-256, deliberately, matching what the existing refresh
token already does (``app/auth/service.py`` hashes the 48-byte refresh secret
with ``sha256``). Not bcrypt: these values are 160+ bits of CSPRNG output, not
human-chosen passwords, so there is no dictionary to slow down — a per-guess
work factor would only add latency to every API request. The one credential
family that *is* human-transcribed, recovery codes, is generated with enough
entropy (50 bits) that the same reasoning holds, and is rate-limited on top.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import string

#: Ambiguity-free alphabet for values a human reads off a screen and types.
#: No 0/O, no 1/I/L.
UNMISTAKABLE = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

_ALPHANUM = string.ascii_letters + string.digits


def new_secret(nbytes: int = 32) -> str:
    """A URL-safe opaque secret. 32 bytes = 256 bits."""
    return secrets.token_urlsafe(nbytes)


def new_token(prefix: str, nbytes: int = 32) -> str:
    """A prefixed opaque secret, e.g. ``vdk_ab12...``.

    The prefix is not security — it is greppability. A leaked key is recognised
    by secret scanners (and by the log redactor, see ``app/core/logging.py``)
    precisely because it announces what it is.
    """
    return f"{prefix}_{secrets.token_urlsafe(nbytes)}"


def new_hex(nbytes: int = 16) -> str:
    return secrets.token_hex(nbytes)


def new_numeric_code(length: int = 6) -> str:
    """Numeric one-time code. Used where an operator transcribes from a log."""
    if length < 6:
        raise ValueError("numeric codes shorter than 6 digits are not acceptable")
    return "".join(secrets.choice(string.digits) for _ in range(length))


def new_human_code(groups: int = 5, group_len: int = 5) -> str:
    """A grouped, transcription-safe code, e.g. ``K7QP2-9XM3T-...``."""
    def block() -> str:
        return "".join(secrets.choice(UNMISTAKABLE) for _ in range(group_len))

    return "-".join(block() for _ in range(groups))


def hash_token(value: str) -> str:
    """The at-rest form of every opaque credential. 64 hex characters."""
    if not value:
        raise ValueError("refusing to hash an empty credential")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def token_hint(value: str, length: int = 8) -> str:
    """A non-reversible display hint: enough to identify a key in a list.

    The first characters *after* the prefix are shown because that is how a
    person tells two of their own keys apart. Eight characters of a
    ``token_urlsafe`` value is ~48 bits of an otherwise-unseen 256-bit secret;
    combined with the requirement to match the stored digest, it is not a
    meaningful reduction in search space.
    """
    if not value:
        return ""
    return value[: max(0, length)]


def matches_hash(stored_hash: str, presented: str) -> bool:
    """Constant-time comparison of a presented secret against its digest.

    ``compare_digest`` rather than ``==`` so the comparison does not leak where
    two hashes diverge. The values compared are digests, so the timing signal
    is weak to begin with — this is defence in depth, not the primary control.
    """
    if not stored_hash or not presented:
        return False
    try:
        return hmac.compare_digest(stored_hash, hash_token(presented))
    except (TypeError, ValueError):
        return False


def split_prefix(value: str, prefixes: tuple[str, ...]) -> str:
    """Which known prefix a presented value carries, or ``""``.

    Used to route a credential to the right authenticator before any database
    work, so a JWT presented to the API-key endpoint is rejected as a shape
    mismatch instead of being looked up as a key.
    """
    head = (value or "").split("_", 1)[0]
    for prefix in prefixes:
        if head == prefix:
            return prefix
    return ""


def normalize_recovery_code(value: str) -> str:
    """Canonical form of a recovery code: uppercase, separators stripped.

    Users paste these with the dashes, without them, with a trailing space, in
    lower case. Normalising once, here, means the stored digest is stable and
    the verification path has no per-call formatting rules.
    """
    return "".join(ch for ch in (value or "").upper() if ch.isalnum())


def generate_recovery_codes(count: int) -> list[str]:
    """``count`` fresh recovery codes, in display form.

    Called only from the enrollment and regenerate flows. The caller hashes
    each one and returns them to the browser exactly once; the plaintext is
    never stored and can never be re-derived.
    """
    if count < 1 or count > 50:
        raise ValueError("recovery code count must be between 1 and 50")
    return [new_human_code() for _ in range(count)]


def constant_time_equals(left: str, right: str) -> bool:
    """Compare two secrets of any type without a short circuit."""
    if left is None or right is None:
        return False
    return hmac.compare_digest(str(left), str(right))


def pkce_challenge(verifier: str) -> str:
    """RFC 7636 §4.2 S256 challenge for a code verifier."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    import base64

    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def new_pkce_verifier() -> str:
    """RFC 7636 §4.1: 43–128 characters from the unreserved set."""
    return secrets.token_urlsafe(64)[:96]


def fingerprint(*parts: str) -> str:
    """A short digest binding several values together (used for state keys)."""
    joined = "\x1f".join(p or "" for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def random_alnum(length: int) -> str:
    return "".join(secrets.choice(_ALPHANUM) for _ in range(length))
