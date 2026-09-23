"""Typed identity failures.

One exception hierarchy, so a route can translate a failure without guessing at
a message, and so a test can assert *why* something was refused rather than
asserting a 403 and hoping. Messages here are safe to return to a caller:
they name a policy, never a credential, never whether a specific address or
subject exists in another tenant.
"""
from __future__ import annotations


class IdentityError(Exception):
    """Base class. `code` is a stable, secret-free identifier for clients."""

    code = "identity_error"

    def __init__(self, message: str = "", *, code: str | None = None) -> None:
        super().__init__(message or self.code)
        if code:
            self.code = code


class IdentityConfigurationError(IdentityError):
    """The deployment or the connection is misconfigured (fail closed)."""

    code = "identity_misconfigured"


class IdentitySecretsUnavailable(IdentityConfigurationError):
    """No encryption key ring is configured, so a secret could not be sealed.

    Raised instead of storing plaintext. Callers surface it as a 503: the
    feature is unavailable, and the operator has a one-line fix.
    """

    code = "identity_secrets_unavailable"


class PolicyDenied(IdentityError):
    """A policy said no. `reason` is a stable code, not prose."""

    code = "policy_denied"

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(message or reason)
        self.reason = reason


class ReauthenticationRequired(PolicyDenied):
    """The action needs a fresh second factor (or a fresh password check).

    ``code`` and the reason are the same string on purpose: a client reads
    ``detail.code``, a log line reads ``detail.reason``, and having them differ
    by a prefix is how one of the two ends up unhandled.
    """

    code = "reauth_required"

    def __init__(self, message: str = "Reauthentication required.") -> None:
        super().__init__("reauth_required", message)


class MFARequired(IdentityError):
    """Login is not finished: a second factor is owed."""

    code = "mfa_required"

    def __init__(self, challenge_id: str, expires_at_iso: str = "") -> None:
        super().__init__("mfa_required")
        self.challenge_id = challenge_id
        self.expires_at_iso = expires_at_iso


class MFAError(IdentityError):
    code = "mfa_error"


class MFAAlreadyEnrolled(MFAError):
    code = "mfa_already_enrolled"


class MFANotEnrolled(MFAError):
    code = "mfa_not_enrolled"


class MFAVerificationFailed(MFAError):
    """Wrong code. Deliberately identical for TOTP and recovery codes."""

    code = "mfa_invalid_code"


class MFAChallengeLocked(MFAError):
    code = "mfa_challenge_locked"


class MFADisabled(IdentityConfigurationError):
    """The MFA surface is switched off for this deployment."""

    code = "mfa_disabled"


class SessionError(IdentityError):
    code = "session_error"


class SessionRevoked(SessionError):
    code = "session_revoked"


class SSOError(IdentityError):
    code = "sso_error"


class SSODisabled(SSOError):
    code = "sso_disabled"


class SSOConfigurationError(SSOError):
    code = "sso_misconfigured"


class SSOValidationError(SSOError):
    """A value from the IdP failed validation (issuer, audience, signature...)."""

    code = "sso_validation_failed"


class SSOReplayDetected(SSOError):
    code = "sso_replay_detected"


class SSOStateExpired(SSOError):
    code = "sso_state_expired"


class SSOAccountLinkError(SSOError):
    code = "sso_link_rejected"


class SCIMError(IdentityError):
    code = "scim_error"

    def __init__(self, message: str = "", *, status: int = 400, scim_type: str = "") -> None:
        super().__init__(message or self.code)
        self.status = status
        self.scim_type = scim_type


class SCIMNotFound(SCIMError):
    code = "scim_not_found"

    def __init__(self, message: str = "Resource not found") -> None:
        super().__init__(message, status=404, scim_type="notFound")


class SCIMConflict(SCIMError):
    code = "scim_conflict"

    def __init__(self, message: str = "Resource already exists") -> None:
        super().__init__(message, status=409, scim_type="uniqueness")


class SCIMInvalidValue(SCIMError):
    code = "scim_invalid_value"

    def __init__(self, message: str = "Invalid value") -> None:
        super().__init__(message, status=400, scim_type="invalidValue")


class SCIMInvalidFilter(SCIMError):
    """RFC 7644 §3.12 ``invalidFilter``: the filter is not one we can execute.

    Distinct from ``invalidValue`` because that is what a client checks: an IdP
    that receives ``invalidFilter`` knows to rewrite its query, while a generic
    400 leaves it retrying the same request.
    """

    code = "scim_invalid_filter"

    def __init__(self, message: str = "Invalid filter") -> None:
        super().__init__(message, status=400, scim_type="invalidFilter")


class SCIMUnauthorized(SCIMError):
    code = "scim_unauthorized"

    def __init__(self, message: str = "Invalid SCIM credential") -> None:
        super().__init__(message, status=401, scim_type="")


class SCIMForbidden(SCIMError):
    code = "scim_forbidden"

    def __init__(self, message: str = "Insufficient SCIM scope") -> None:
        super().__init__(message, status=403, scim_type="")


class CredentialError(IdentityError):
    code = "credential_error"


class DomainError(IdentityError):
    code = "domain_error"


class DomainNotVerified(DomainError):
    code = "domain_not_verified"


class DomainConflict(DomainError):
    code = "domain_conflict"


class DomainDNSUnavailable(DomainError):
    code = "domain_dns_unavailable"


class EmailDeliveryError(IdentityError):
    code = "email_delivery_failed"


class TokenError(IdentityError):
    """A one-time token (reset, verification) is invalid, used, or expired."""

    code = "token_invalid"
