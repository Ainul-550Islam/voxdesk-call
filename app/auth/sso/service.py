"""Connection lifecycle, login attempts, and completing a federated login.

Implementation: :mod:`app.auth.identity.sso.service`.

The service owns everything that touches the database, which is what keeps the
protocol modules testable against crafted input:

* **connections** — create, update, list, enable/disable, delete. Secrets (the
  OIDC client secret, the imported IdP metadata) are sealed on write and never
  read back through the API; only "has one" is exposed;
* **login attempts** — one row per started login, carrying the hashed state (or
  the request id), the nonce, the sealed PKCE verifier, the expiry, and the
  outcome. ``consume_state`` spends it with a single conditional UPDATE, so two
  concurrent callbacks with the same state cannot both proceed;
* **completion** — ``complete_oidc_login`` and ``complete_saml_login`` verify,
  normalize, map, provision-or-link, and write the session. Both end in the same
  place, so a policy or audit change cannot apply to one protocol and not the
  other;
* **audit** — attempts and their outcomes are listable per connection
  (``/api/sso/attempts``) with no assertion contents, no tokens, and no claim
  values that are not already in the audit event the service emitted.
"""
from __future__ import annotations

from app.auth.identity.sso.service import (
    STATE_TTL,
    ConnectionInput,
    LoginOutcome,
    StartResult,
    acs_url,
    active_certificate_count,
    add_certificate,
    base_url,
    complete_oidc_login,
    complete_saml_login,
    connection_for_email,
    consume_state,
    create_connection,
    delete_connection,
    domain_of,
    ensure_enabled,
    get_connection,
    get_connection_by_slug,
    list_attempts,
    list_certificates,
    list_connections,
    metadata_url,
    metadata_xml,
    normalized_slug,
    oidc_redirect_uri,
    record_outcome,
    request_id_for,
    retire_certificate,
    set_mappings,
    set_status,
    slo_url,
    sp_entity_id,
    start_login,
    trusted_certificates,
    update_connection,
)

__all__ = [
    "STATE_TTL",
    "ConnectionInput",
    "LoginOutcome",
    "StartResult",
    "acs_url",
    "active_certificate_count",
    "add_certificate",
    "base_url",
    "complete_oidc_login",
    "complete_saml_login",
    "connection_for_email",
    "consume_state",
    "create_connection",
    "delete_connection",
    "domain_of",
    "ensure_enabled",
    "get_connection",
    "get_connection_by_slug",
    "list_attempts",
    "list_certificates",
    "list_connections",
    "metadata_url",
    "metadata_xml",
    "normalized_slug",
    "oidc_redirect_uri",
    "record_outcome",
    "request_id_for",
    "retire_certificate",
    "set_mappings",
    "set_status",
    "slo_url",
    "sp_entity_id",
    "start_login",
    "trusted_certificates",
    "update_connection",
]
