# VoxDesk — OIDC single sign-on

Authorization-code + PKCE against an OpenID Provider, with discovery, state and
nonce binding, strict ID-token validation and a JWKS cache that survives a key
rotation without a restart. Implementation: `app/auth/identity/sso/oidc.py`
(protocol), `app/auth/identity/sso/service.py` (connections, attempts,
provisioning), `app/auth/identity/sso/claims.py` (claim mapping), routes in
`app/api/sso_routes.py`.

SAML is the subject of [`SSO-SAML.md`](SSO-SAML.md); the two protocols share
the connection table, the attempts table, the provisioning rules and the audit
vocabulary, and differ only in how a token/assertion is obtained and verified.

---

## 1. Endpoints

| Method | Path | Who | Purpose |
| --- | --- | --- | --- |
| `GET` | `/api/sso/connections` | `identity:read` | list connections |
| `POST` | `/api/sso/connections` | `identity:write` + fresh proof | create (always starts `draft`) |
| `GET` | `/api/sso/connections/{id}` | `identity:read` | one connection |
| `PATCH` | `/api/sso/connections/{id}` | `identity:write` + fresh proof | update configuration |
| `POST` | `/api/sso/connections/{id}/status` | `identity:write` + fresh proof | `draft` / `active` / `disabled` |
| `DELETE` | `/api/sso/connections/{id}` | `identity:write` + fresh proof | remove |
| `GET`/`POST` | `/api/sso/connections/{id}/certificates` | `identity:*` | SAML certificates (see `SSO-SAML.md`) |
| `DELETE` | `/api/sso/connections/{id}/certificates/{cid}` | `identity:write` + fresh proof | retire a certificate |
| `PUT` | `/api/sso/connections/{id}/mappings` | `identity:write` + fresh proof | role/group mappings |
| `GET` | `/api/sso/connections/{id}/metadata` | `identity:read` | SP metadata (SAML) |
| `POST` | `/api/sso/connections/{id}/test` | `identity:write` | discovery / configuration self-test |
| `GET` | `/api/sso/attempts` | `identity:read` | every federated attempt and its outcome |
| `GET` | `/auth/sso/discover` | public | is federation available for this address? |
| `GET`/`POST` | `/auth/sso/{slug}/start` | public | begin a login, returns the URL to visit |
| `GET` | `/auth/sso/{slug}/callback` | public | the OIDC redirect target |

`POST /{slug}/start` exists alongside `GET` because a browser can follow a
redirect while a dashboard wants the URL. Both return
`{ authorization_url, state, connection_id, protocol }` rather than
redirecting, so the dashboard decides how to move the browser.

### The login walkthrough

1. The dashboard calls `GET /auth/sso/discover?email=…`. Only a **verified**
   enterprise domain routes anyone anywhere: `connection_for_email` reads
   `verified_domains`, and if two tenants somehow verified the same domain it
   refuses rather than guessing.
2. `start` creates an `sso_login_attempts` row: `state_hash`, `nonce_hash`, the
   relay state, an optional sealed PKCE verifier, the protocol and kind
   (`login` / `link`), IP, user agent, and an expiry of
   `SSO_STATE_TTL_SECONDS`. The **state and nonce values travel; the hashes
   stay**. Only the state is ever sent through the provider.
3. For OIDC the browser is sent to
   `authorization_endpoint?response_type=code&client_id=…&redirect_uri=…&scope=openid…&state=…&nonce=…&code_challenge=…`,
   with `code_challenge_method=S256` when the connection uses PKCE.
4. `GET /{slug}/callback?code=…&state=…` spends the state, exchanges the code,
   verifies the ID token, resolves the login, and mints the session.

Order in `complete_oidc_login` is deliberate and asserted: **consume the state
(single use) → verify the ID token (nonce bound to this attempt) → touch the
user table**. A failure before the last step cannot have changed anything.

---

## 2. State: single-use, and spent atomically

`consume_state` is a conditional `UPDATE … WHERE consumed_at IS NULL AND
expires_at > now() RETURNING id`. A read-then-write would let two concurrent
callbacks both see the attempt unconsumed and both proceed; the conditional
update means exactly one sees a row and the loser gets
`SSOStateExpired("This sign-in attempt has expired or was already used.")`.
That atomicity **is** the CSRF defence for the callback endpoint, and
`tests/security/test_sso.py` asserts it.

The attempt is additionally checked for protocol and kind, so an attempt
started for `link` cannot be spent on a `login` callback and an OIDC callback
cannot spend a SAML attempt.

---

## 3. ID-token validation

`verify_id_token(connection, provider, id_token=…, nonce_hash=…)` — verification
lives inside one function so a caller cannot forget a step:

| Check | Rule |
| --- | --- |
| structure | `get_unverified_header`; a malformed token is refused before any key lookup |
| algorithm | must be in `ALLOWED_ID_TOKEN_ALGORITHMS` **and** listed by the provider's discovery document; symmetric algorithms (`HS256`) are never accepted, so a client secret can never be used as a signing key |
| signature | verified against the JWKS key for the token's `kid` |
| key rotation | an unknown `kid` costs exactly one forced JWKS refresh, then the login succeeds — no restart, no cache flush |
| issuer | must equal the provider's `issuer` |
| audience | must equal the connection's `client_id`; a multi-audience token additionally requires `azp` to match |
| lifetime | `exp` validated with `SSO_CLOCK_SKEW_SECONDS` (default 120) of leeway; `exp`, `iat`, `iss`, `aud`, `sub` are all required |
| nonce | the presented nonce, hashed, must equal the attempt's `nonce_hash` (constant-time compare) |

The JWKS is cached in-process for `_JWKS_TTL_SECONDS`, as is the discovery
document, because both are public documents that change on the provider's
schedule. `oidc.reset_caches()` exists for tests.

---

## 4. Discovery

`discover(connection)` fetches the configured `discovery_url`, or
`{issuer}/.well-known/openid-configuration`. It refuses:

- a discovery URL that is not `https` (except loopback outside production);
- a document that names **an issuer that disagrees with the configured one**
  (trailing slash normalisation is the only tolerance);
- a document missing `authorization_endpoint`, `token_endpoint` or `jwks_uri`,
  or one naming a non-`https` endpoint.

`oidc.is_acceptable_https_url` is the shared posture rule, used both at runtime
and by the administrator-facing validator, so the two cannot drift.

---

## 5. Connections

- **A connection always starts `draft`.** Nothing is reachable until an
  administrator activates it, and `/auth/sso/{slug}/start` answers 404 for
  anything that is not `active`.
- **Activation is validated.** An OIDC connection must have a discovery URL and
  a client id; the discovery URL must pass the https rule. A SAML connection
  must additionally have at least one unexpired signing certificate and an IdP
  sign-on URL.
- **Disabling is immediate.** With `status = disabled` (or the tenant switched
  off federation) `start_login` answers 404 / `PolicyDenied`, and existing
  sessions are not extended by federated logins.
- **Client secrets are sealed.** `client_secret_encrypted` /
  `client_secret_key_id` hold ciphertext under the identity key ring with the
  tenant in the AAD; nothing decrypts a secret from another tenant's row. The
  secret is never returned by any read endpoint — the connection view reports
  only whether one is configured.
- **The redirect URI is explicit.** A configured `redirect_uri` wins, so a
  deployment behind a proxy can match what the provider registered; otherwise
  it is derived from `public_base_url` as
  `{base}/auth/sso/{slug}/callback`. Hosts beyond the connection's own are only
  accepted if listed in `SSO_ALLOWED_REDIRECT_HOSTS`.

Every create/update/status/delete/certificate/mapping change requires a
**fresh proof of presence** (`assert_privileged`) and is audited
(`SSO_CONNECTION_CREATED`, `_UPDATED`, `_ENABLED`, `_DISABLED`, `_DELETED`,
`SSO_MAPPING_CHANGED`, `SSO_CERTIFICATE_ROTATED`).

## 6. Claim normalization and role mapping

`normalize_claims(connection, claims, default_email_verified=…)` folds an
arbitrary provider's claims into the shape the rest of the code uses. The
connection's configured claim names win; the built-in alias lists are the
fallback, so a new connection works before anyone tunes it and a tuned
connection is never second-guessed. `raw_claim_names` is retained for
diagnostics and is never trusted.

`decide_role(connection, claims, current_role=…)` then applies, in order:

1. an explicit role claim through `role_mapping`;
2. group membership through `role_mapping`, then `group_mapping`;
3. the connection's `default_role`.

A mapped value naming a role **outside the assignable set is refused, not
clamped** — silently downgrading OWNER to ADMIN would leave an operator
believing their mapping worked. With `deny_unmapped_roles` an unmapped user
with no existing role is refused with a message that names the fix; an existing
user keeps the role they have, because refusing the login outright would take
away access for a mapping the operator is still tuning.

**Privilege escalation through self-chosen claims is not possible:** the claims
are only ever *mapped* through administrator-configured dictionaries, a value
that is not a key in the mapping does nothing at all, and every decision is
audited with its reason (`mapped`, `group`, `default_role`,
`kept_existing_role`, `no_mapping_kept_role`).

---

## 7. Provisioning and account linking

`decide_provisioning` runs before anything touches the user table:

| Situation | Action |
| --- | --- |
| a mapping row exists for `(connection, subject)` | **reuse** that user |
| no mapping, but an active user with that address exists in *this tenant* | **link**, if `allow_account_linking`; otherwise refused with a message telling the user to sign in with a password |
| no user at all, and `jit_enabled` | **create** |
| no user at all, JIT disabled | refused |

The HTTP answer to every one of these refusals is the same flat
`{"code": "sso_failed"}` — an unauthenticated caller must not be able to use the
login endpoint to discover which addresses exist in a workspace. The *reason* is
carried on the exception's own `code` and recorded in the audit trail
(`IDENTITY_LINK_REJECTED`), which is why the refusals below name theirs:

| Refusal | Audit reason |
| --- | --- |
| account exists, linking not allowed for the connection | `sso_account_linking_disabled` |
| account exists, the IdP did not assert a verified address | `sso_link_requires_verified_email` |
| no account, provisioning off for the connection | `sso_provisioning_disabled_on_connection` |
| no account, provisioning off for the workspace | `sso_provisioning_disabled_for_workspace` |

Three refusals protect the boundary:

- **A verified address is required to link** (`require_verified_email`); an
  unasserted address is a string the IdP chose to send.
- **A cross-tenant address is refused and audited.** If the address belongs to
  a user in *another* tenant, the login is refused with
  `IDENTITY_LINK_REJECTED` (`address_belongs_to_another_tenant`). This is the
  one place a federated login could cross a tenant boundary, so it is refused
  explicitly rather than resolved by preference.
- **A deactivated user stays deactivated.** An IdP that keeps asserting a user
  we disabled gets `SSO_LOGIN_FAILED` with `reason = "user_disabled"`, because
  otherwise "disable this account" would be a setting an identity provider
  could undo.

A JIT-created user gets the mapped role (or `default_role`), is created in the
**connection's** tenant only, and emits `SSO_USER_PROVISIONED`. Since they have
no password, their row is unusable through the password path — which is exactly
what should happen.

---

## 8. Attempts, audit and what is never logged

Every attempt is a row in `sso_login_attempts` with its outcome (`succeeded`,
`failed`) and a short failure reason, listed by `GET /api/sso/attempts`. The
attempt row additionally gives an operator the IP, user agent and timing of a
failed federation without any secret in it.

Audit vocabulary: `SSO_LOGIN_STARTED`, `SSO_LOGIN_SUCCEEDED`, `SSO_LOGIN_FAILED`,
`SSO_USER_PROVISIONED`, `SSO_ACCOUNT_LINKED`, `IDENTITY_LINK_REJECTED`, plus the
connection events above.

Never logged, never returned and never stored in the clear: the authorization
code, the ID token, the access/refresh tokens the provider issues, the client
secret, the PKCE verifier (sealed at rest, decrypted only to complete the
exchange), the raw nonce, the raw state. Where a value must appear in an audit
detail it appears **hashed** (`subject_hint = hash_token(subject)[:16]`).

---

## 9. Configuration

| Setting | Default | Meaning |
| --- | --- | --- |
| `SSO_ENABLED` | `true` | master switch; off means the routes answer 404 |
| `SSO_STATE_TTL_SECONDS` | `600` | attempt lifetime |
| `SSO_CLOCK_SKEW_SECONDS` | `120` | leeway on `exp` / assertion timestamps |
| `SSO_ALLOWED_REDIRECT_HOSTS` | empty | extra permitted redirect hosts |
| `SSO_GROUP_CLAIM` / `SSO_ROLE_CLAIM` | `groups` / `roles` | default claim names |
| `PUBLIC_BASE_URL` | — | the base the redirect URI and SP entity id are derived from |
| `IDENTITY_ENCRYPTION_KEYS` | falls back to `CRM_ENCRYPTION_KEYS` | seals client secrets; production refuses SSO with no ring |

Per connection: `slug`, `protocol`, `issuer`, `discovery_url`, `client_id`,
`client_secret_encrypted`, `redirect_uri`, `scopes`, `use_pkce`,
`require_verified_email`, `allow_account_linking`, `jit_enabled`,
`deny_unmapped_roles`, `default_role`, `role_mapping`, `group_mapping`,
`subject_claim`, `email_claim`, `name_claim`, `role_claim`, `group_claim`.

---

## 10. Tests

`tests/security/test_sso.py` runs a **local provider** — a real RSA key, a real
discovery document served by a stub HTTP client, and ID tokens signed by that
key — so the hand-written verifier is exercised against genuine tokens rather
than mocked claims:

| Test | Proves |
| --- | --- |
| discovery: wrong issuer / insecure endpoint / missing JWKS / caching / `force` | the document is validated, not trusted |
| `is_acceptable_https_url` (parametrised) | the posture rule, shared with the admin validator |
| signed token accepted | the happy path, including `exp`, `iat`, `iss`, `aud`, `sub` |
| token from another issuer / another client / expired / inside the skew | issuer and audience pinning; the skew window works as configured |
| token from another sign-in attempt, token with no nonce | the nonce binding is enforced, not optional |
| `alg: none`, HS256 with the client secret, unknown key | algorithm confusion and key-confusion are refused |
| rotated `kid` | exactly one forced JWKS refresh, then success |
| multi-audience without `azp` / with `azp` | the authorized-party rule |
| provider not offering the algorithm | discovery's algorithm list is honoured |

---

## 11. Secrets at rest

The client secret and the PKCE verifier are sealed with the repository's
encrypted-secret storage, each under its own purpose
(`identity.secrets.PURPOSE_OIDC_CLIENT_SECRET`, `…_PKCE_VERIFIER`). The purpose
is part of the additional authenticated data, so a value sealed under one
purpose cannot be opened under another — which is also why the aliases in
`identity.sso.service` point at the `identity.secrets` constants instead of
repeating the strings: a single character of drift between the writer and the
reader turns into "not configured" at runtime.

