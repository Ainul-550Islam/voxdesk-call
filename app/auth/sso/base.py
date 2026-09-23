"""The vocabulary OIDC and SAML share.

Both protocols answer the same three questions, so both are described here with
the same words. This is what lets ``start_login`` return one shape and
``complete_oidc_login`` / ``complete_saml_login`` return another, instead of the
routes branching on which protocol is configured:

* :class:`~app.auth.identity.sso.service.ConnectionInput` — a validated
  connection specification, protocol-independent;
* :class:`~app.auth.identity.sso.service.StartResult` — where to send the
  browser, and the attempt that tracks it (the state/nonce for OIDC, the
  ``InResponseTo`` request id for SAML);
* :class:`~app.auth.identity.sso.service.LoginOutcome` — what came back:
  normalized claims, the role decision, whether the user was provisioned or
  linked, and the audit trail.

Also here: the URL shapes (ACS, redirect URI, SLO, metadata, SP entity id) and
:func:`ensure_enabled`, which is the single switch that turns federated login
off for a deployment without deleting anybody's configuration.
"""
from __future__ import annotations

from app.auth.identity.models import (
    AttemptOutcome,
    CertificateStatus,
    MappingOrigin,
    SSOProtocol,
    SSOStatus,
)
from app.auth.identity.sso.claims import (
    NormalizedClaims,
    ProvisioningDecision,
    RoleDecision,
)
from app.auth.identity.sso.service import (
    ConnectionInput,
    LoginOutcome,
    StartResult,
    acs_url,
    base_url,
    ensure_enabled,
    metadata_url,
    normalized_slug,
    oidc_redirect_uri,
    request_id_for,
    slo_url,
    sp_entity_id,
)

__all__ = [
    "AttemptOutcome",
    "CertificateStatus",
    "ConnectionInput",
    "LoginOutcome",
    "MappingOrigin",
    "NormalizedClaims",
    "ProvisioningDecision",
    "RoleDecision",
    "SSOProtocol",
    "SSOStatus",
    "StartResult",
    "acs_url",
    "base_url",
    "ensure_enabled",
    "metadata_url",
    "normalized_slug",
    "oidc_redirect_uri",
    "request_id_for",
    "slo_url",
    "sp_entity_id",
]
