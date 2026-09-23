"""Sealing and opening identity secrets.

There is exactly one cipher in this codebase — the AES-256-GCM envelope in
``app.integrations.crm.crypto`` — with one key ring, one nonce discipline and
one rotation story. Identity does not grow a second one; it reuses that cipher
under its own *namespace*, which is what keeps the two families of ciphertext
from being interchangeable:

    CRM credentials         v1.<key_id>.<nonce>.<ct>   AAD: voxdesk:crm:v1:<tenant>:<provider>
    identity secrets        v1.<key_id>.<nonce>.<ct>   AAD: voxdesk:identity:v1:<tenant>:<purpose>

The tenant id is in the AAD either way, so ciphertext lifted out of one
tenant's row and pasted into another's fails to authenticate. The purpose is in
it too, so a TOTP seed cannot be pasted into the SSO client-secret column of the
same tenant. Both are *authenticated*, not merely encrypted — the cipher
refuses rather than returning garbage that would then be used as a key.

The key ring comes from ``IDENTITY_ENCRYPTION_KEYS``, falling back to
``CRM_ENCRYPTION_KEYS`` (see ``Settings.identity_key_ring_source``) so an
operator who has already configured encryption does not paste the same keys
twice. If neither is set, ``encrypt_*`` raises ``IdentitySecretsUnavailable``
rather than storing plaintext — the fail-closed behaviour every caller depends
on.
"""
from __future__ import annotations

import json
from functools import lru_cache

from app.core.config import settings
from app.integrations.crm import crypto
from app.auth.identity.exceptions import (
    IdentitySecretsUnavailable,
)

#: AAD namespace for every identity secret. Changing it invalidates stored
#: ciphertext, so it is a constant and not a setting.
NAMESPACE = "identity"

#: Purposes. Each is a separate authentication domain; new ones are additive.
PURPOSE_TOTP = "totp"
PURPOSE_OIDC_CLIENT_SECRET = "oidc_client_secret"
PURPOSE_SAML_METADATA = "saml_metadata"
PURPOSE_SAML_CERTIFICATE = "saml_certificate"
PURPOSE_PKCE_VERIFIER = "pkce_verifier"

_TEXT_KEY = "value"   # the JSON field a sealed string travels in


@lru_cache(maxsize=4)
def _ring_for(source: str) -> crypto.KeyRing:
    return crypto.parse_key_ring(source)


def key_ring() -> crypto.KeyRing:
    """The configured key ring, or ``IdentitySecretsUnavailable``.

    Never returns ``None``: a caller that is about to store a credential has no
    safe fallback, so the refusal happens here rather than at the column.
    """
    source = settings.identity_key_ring_source
    if not source:
        raise IdentitySecretsUnavailable(
            "no identity encryption key ring is configured "
            "(set IDENTITY_ENCRYPTION_KEYS or CRM_ENCRYPTION_KEYS)"
        )
    try:
        return _ring_for(source)
    except crypto.CredentialCryptoError as exc:
        raise IdentitySecretsUnavailable(str(exc)) from exc


def encryption_available() -> bool:
    """Whether secrets can be sealed. Used to fail closed *early* in a route."""
    try:
        key_ring()
    except IdentitySecretsUnavailable:
        return False
    return True


def reset_key_ring_cache() -> None:
    """Drop the cached ring (tests, and after a key rotation)."""
    _ring_for.cache_clear()


def encrypt_text(plaintext: str, *, tenant_id: str, purpose: str) -> tuple[str, str]:
    """Seal a string. Returns ``(envelope, key_id)``."""
    if not isinstance(plaintext, str) or plaintext == "":
        raise IdentitySecretsUnavailable("refusing to seal an empty secret")
    envelope, key_id = crypto.encrypt_credentials(
        {_TEXT_KEY: plaintext},
        tenant_id=str(tenant_id),
        provider=purpose,
        key_ring=key_ring(),
        namespace=NAMESPACE,
    )
    return envelope, key_id


def decrypt_text(envelope: str, *, tenant_id: str, purpose: str) -> str:
    """Open a sealed string, or raise ``IdentitySecretsUnavailable``."""
    try:
        data = crypto.decrypt_credentials(
            envelope,
            tenant_id=str(tenant_id),
            provider=purpose,
            key_ring=key_ring(),
            namespace=NAMESPACE,
        )
    except (crypto.CredentialDecryptionError, crypto.CredentialCryptoError) as exc:
        # The underlying message distinguishes "tampered" from "key not
        # loaded"; neither is the caller's business, and both mean the same
        # thing operationally: this secret cannot be used.
        raise IdentitySecretsUnavailable(
            "the stored identity secret could not be opened "
            "(wrong key, tampered row, or a row copied between tenants)"
        ) from exc
    value = data.get(_TEXT_KEY)
    if not isinstance(value, str) or value == "":
        raise IdentitySecretsUnavailable("the stored identity secret is empty")
    return value


def encrypt_json(data: dict, *, tenant_id: str, purpose: str) -> tuple[str, str]:
    """Seal a JSON document (connection metadata, certificate bundles)."""
    if not isinstance(data, dict) or not data:
        raise IdentitySecretsUnavailable("refusing to seal an empty secret")
    return crypto.encrypt_credentials(
        data,
        tenant_id=str(tenant_id),
        provider=purpose,
        key_ring=key_ring(),
        namespace=NAMESPACE,
    )


def decrypt_json(envelope: str, *, tenant_id: str, purpose: str) -> dict:
    try:
        return crypto.decrypt_credentials(
            envelope,
            tenant_id=str(tenant_id),
            provider=purpose,
            key_ring=key_ring(),
            namespace=NAMESPACE,
        )
    except (crypto.CredentialDecryptionError, crypto.CredentialCryptoError) as exc:
        raise IdentitySecretsUnavailable(
            "the stored identity secret could not be opened "
            "(wrong key, tampered row, or a row copied between tenants)"
        ) from exc


def seal_preview(envelope: str) -> str:
    """A short, non-reversible descriptor of a sealed secret, safe to log.

    Returns the envelope version and key id only — enough to answer "which key
    is this row on" in a support conversation without exposing anything.
    """
    parts = (envelope or "").split(".")
    if len(parts) != 4 or parts[0] != "v1":
        return "unreadable"
    return f"v1:{parts[1]}"


def digest_for_log(data: dict) -> str:
    """A stable hash of a JSON document, for change detection in audit detail.

    Used where an operator needs to see *that* a configuration changed without
    the configuration itself being recorded — the IdP metadata blob, for
    instance, which carries certificates.
    """
    import hashlib

    canonical = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()[:16]
