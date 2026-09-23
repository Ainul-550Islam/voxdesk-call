"""Metadata: what the SP publishes, and what an administrator imports.

Implementation: :mod:`app.auth.identity.sso.saml` (generation and parsing) and
:mod:`app.auth.identity.sso.service` (the connection-facing helpers).

Outbound, :func:`sp_metadata` builds the SP document served at
``GET /auth/sso/{slug}/metadata``: the entity id, the ACS URL for that
connection, and — deliberately — **no signing key**. The SP does not sign
AuthnRequests in this deployment, and advertising a key that is not used is how
a security review ends up chasing a certificate nobody rotates.

Inbound, IdP metadata is stored sealed (the same tenant-bound envelope as every
other secret) and never echoed back; only derived, non-secret fields — the IdP
entity id and its SSO/SLO URLs — are readable through the admin API. The XML is
subject to the same parser rules as an assertion: no DTD, no external entities.
"""
from __future__ import annotations

from app.auth.identity.sso.saml import (
    build_authn_request,
    new_request_id,
    safe_parse,
    sp_metadata,
)
from app.auth.identity.sso.service import (
    metadata_url,
    metadata_xml,
    request_id_for,
    sp_entity_id,
)

__all__ = [
    "build_authn_request",
    "metadata_url",
    "metadata_xml",
    "new_request_id",
    "request_id_for",
    "safe_parse",
    "sp_entity_id",
    "sp_metadata",
]
