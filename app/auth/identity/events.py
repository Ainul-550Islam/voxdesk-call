"""Identity and security events.

The product already has one event store — ``AuditLog`` — with its own retention
and its own reader (``/api/audit``), and STEP 18 adds members to its
``AuditAction`` enum rather than a second table. This module is the single
doorway those events go through, and it exists for one reason: to make it
structurally hard to write a secret into the audit trail.

Every call is filtered through :func:`scrub`, which removes any key that names
a credential — including nested dictionaries and lists, because the payload
that carries a certificate is rarely flat. The scrub is not a substitute for
callers being careful; it is the backstop for the day someone adds a field.

The forbidden set is deliberately *closed and small*. A heuristic ("anything
with 'secret' in it") would eventually eat a legitimate field like
``secret_rotated_at``, and a redactor that surprises its callers gets turned
off.
"""
from __future__ import annotations

import uuid
from typing import Any

import structlog

from app.auth.service import record_audit
from app.core.logging import log as _core_log
from app.db.models import AuditAction, AuditLog

log = structlog.get_logger()

#: Field names that must never reach the audit trail. Each one is a credential
#: or a one-time bypass; none of them is needed to explain an event.
FORBIDDEN_DETAIL_KEYS: frozenset[str] = frozenset({
    "password", "new_password", "current_password", "password_hash",
    "token", "access_token", "refresh_token", "id_token", "session_token",
    "api_key", "apikey", "api_key_secret", "secret", "client_secret",
    "scim_token", "scim_secret", "service_account_secret", "credential",
    "credentials", "totp", "totp_secret", "secret_code", "code",
    "recovery_code", "recovery_codes", "verification_code", "otp",
    "assertion", "saml_assertion", "saml_response", "saml_request",
    "certificate_pem", "pem", "private_key", "code_verifier", "state",
    "nonce", "authorization", "cookie", "set_cookie",
})

#: Values longer than this in a detail field are truncated. Audit rows are
#: small on purpose: a 400 KB claim blob stored inline is a data-retention
#: problem wearing an audit trail's clothes.
MAX_DETAIL_VALUE = 500

_REDACTED = "***"
_TRUNCATED_SUFFIX = "…(truncated)"


def scrub(value: Any, *, _key: str = "") -> Any:
    """Recursively remove credential-shaped keys and truncate long values."""
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text.lower() in FORBIDDEN_DETAIL_KEYS:
                cleaned[key_text] = _REDACTED
                continue
            cleaned[key_text] = scrub(item, _key=key_text)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [scrub(item, _key=_key) for item in value]
    if isinstance(value, str) and len(value) > MAX_DETAIL_VALUE:
        return value[:MAX_DETAIL_VALUE] + _TRUNCATED_SUFFIX
    return value


def _scrubbed_count(original: Any, cleaned: Any) -> int:
    """How many fields were masked, so the event can say so and be investigated."""
    if isinstance(original, dict) and isinstance(cleaned, dict):
        count = 0
        for key, item in original.items():
            if str(key).lower() in FORBIDDEN_DETAIL_KEYS:
                count += 1
                continue
            count += _scrubbed_count(item, cleaned.get(str(key)))
        return count
    if isinstance(original, (list, tuple)):
        return sum(
            _scrubbed_count(item, cleaned[index] if index < len(cleaned) else None)
            for index, item in enumerate(original)
        )
    return 0


async def emit(
    session,
    action: AuditAction,
    *,
    tenant_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    target_user_id: uuid.UUID | None = None,
    actor_email: str = "",
    ip_address: str = "",
    user_agent: str = "",
    detail: dict | None = None,
    commit: bool = True,
) -> AuditLog:
    """Write one identity event, with the detail payload scrubbed first."""
    payload = detail or {}
    cleaned = scrub(payload)
    masked = _scrubbed_count(payload, cleaned)
    if masked:
        # Loud, because it means a caller tried to record a credential. The
        # row is still written (the *event* matters) with the field masked and
        # a counter that says how many fields were involved.
        _core_log.warning(
            "identity.event_detail_scrubbed",
            action=action.value,
            masked_fields=masked,
        )
        cleaned["_scrubbed_fields"] = masked

    return await record_audit(
        session,
        action=action,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        target_user_id=target_user_id,
        actor_email=actor_email,
        ip_address=ip_address,
        user_agent=user_agent,
        detail=cleaned,
        commit=commit,
    )


def event_names() -> list[str]:
    """Every identity-related action name, for docs and for tests to pin."""
    return sorted(
        action.value
        for action in AuditAction
        if action.value in _IDENTITY_ACTIONS
    )


#: The identity/security vocabulary this feature owns. Kept explicit so a test
#: can assert that all of them are *reachable* from a code path (no dead event
#: names) rather than only that they exist.
_IDENTITY_ACTIONS = frozenset({
    AuditAction.IDENTITY_REAUTHENTICATED.value,
    AuditAction.IDENTITY_LINK_REJECTED.value,
    AuditAction.PASSWORD_RESET_REQUESTED.value,
    AuditAction.PASSWORD_RESET_COMPLETED.value,
    AuditAction.EMAIL_VERIFICATION_SENT.value,
    AuditAction.EMAIL_VERIFIED.value,
    AuditAction.MFA_ENROLLMENT_STARTED.value,
    AuditAction.MFA_ENABLED.value,
    AuditAction.MFA_DISABLED.value,
    AuditAction.MFA_VERIFIED.value,
    AuditAction.MFA_FAILED.value,
    AuditAction.MFA_CHALLENGE_LOCKED.value,
    AuditAction.MFA_RECOVERY_CODE_USED.value,
    AuditAction.MFA_RECOVERY_CODES_REGENERATED.value,
    AuditAction.SESSION_CREATED.value,
    AuditAction.SESSION_REVOKED.value,
    AuditAction.SESSION_SUSPICIOUS.value,
    AuditAction.SSO_CONNECTION_CREATED.value,
    AuditAction.SSO_CONNECTION_UPDATED.value,
    AuditAction.SSO_CONNECTION_DELETED.value,
    AuditAction.SSO_CONNECTION_ENABLED.value,
    AuditAction.SSO_CONNECTION_DISABLED.value,
    AuditAction.SSO_MAPPING_CHANGED.value,
    AuditAction.SSO_CERTIFICATE_ROTATED.value,
    AuditAction.SSO_LOGIN_STARTED.value,
    AuditAction.SSO_LOGIN_SUCCEEDED.value,
    AuditAction.SSO_LOGIN_FAILED.value,
    AuditAction.SSO_USER_PROVISIONED.value,
    AuditAction.SSO_ACCOUNT_LINKED.value,
    AuditAction.SCIM_CREDENTIAL_CREATED.value,
    AuditAction.SCIM_CREDENTIAL_ROTATED.value,
    AuditAction.SCIM_CREDENTIAL_REVOKED.value,
    AuditAction.SCIM_USER_PROVISIONED.value,
    AuditAction.SCIM_USER_UPDATED.value,
    AuditAction.SCIM_USER_DEPROVISIONED.value,
    AuditAction.SCIM_GROUP_CREATED.value,
    AuditAction.SCIM_GROUP_UPDATED.value,
    AuditAction.SCIM_GROUP_DELETED.value,
    AuditAction.API_KEY_CREATED.value,
    AuditAction.API_KEY_ROTATED.value,
    AuditAction.API_KEY_REVOKED.value,
    AuditAction.CREDENTIAL_AUTH_REJECTED.value,
    AuditAction.SERVICE_ACCOUNT_CREATED.value,
    AuditAction.SERVICE_ACCOUNT_UPDATED.value,
    AuditAction.SERVICE_ACCOUNT_DISABLED.value,
    AuditAction.SERVICE_ACCOUNT_ENABLED.value,
    AuditAction.SERVICE_ACCOUNT_CREDENTIAL_CREATED.value,
    AuditAction.SERVICE_ACCOUNT_CREDENTIAL_ROTATED.value,
    AuditAction.SERVICE_ACCOUNT_CREDENTIAL_REVOKED.value,
    AuditAction.DOMAIN_ADDED.value,
    AuditAction.DOMAIN_VERIFIED.value,
    AuditAction.DOMAIN_VERIFICATION_FAILED.value,
    AuditAction.DOMAIN_REMOVED.value,
    AuditAction.DOMAIN_ENFORCEMENT_CHANGED.value,
})
