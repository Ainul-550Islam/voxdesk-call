# VoxDesk — Enterprise Identity

The system of record for **who may sign in, how they prove it, and what they may
do once they have**. This document is the map: what exists, where it lives, which
knob turns it, and which test proves it. The five feature documents go deeper:

| Document | Covers |
| --- | --- |
| [`MFA.md`](MFA.md) | TOTP enrollment, recovery codes, challenges, lockout, reauth |
| [`SSO-OIDC.md`](SSO-OIDC.md) | OIDC connections, authorization-code + PKCE, token validation |
| [`SSO-SAML.md`](SSO-SAML.md) | SAML 2.0 SP, metadata, ACS/SLO, assertion and signature rules |
| [`SCIM.md`](SCIM.md) | SCIM 2.0 Users/Groups, credentials, filtering, pagination |
| [`API-KEYS.md`](API-KEYS.md) | Tenant-scoped API keys: scopes, rotation, expiry |
| [`SERVICE-ACCOUNTS.md`](SERVICE-ACCOUNTS.md) | Machine identities, credentials, emergency disable |
| [`DOMAIN-VERIFICATION.md`](DOMAIN-VERIFICATION.md) | DNS TXT ownership proof and SSO enforcement |
| [`IDENTITY-SECURITY.md`](IDENTITY-SECURITY.md) | Threat model, invariants, adversarial cases, response |

---

## 1. What this layer is, and what it deliberately is not

It is an **extension of the existing auth stack**, not a replacement:

- `app/auth/jwt.py`, `password.py`, `rbac.py`, `permissions.py` and
  `app/auth/service.py` keep their behaviour. Access tokens still carry
  `sub`, `tid`, `role`, `tv`, `sid`, `amr`, are HS256-pinned, and are still
  checked against `token_version` on every request.
- No second `User`, `Tenant`, `Role`, `Session` or `Token` model exists.
  `users`, `tenants`, `refresh_tokens` and `audit_logs` are the same tables as
  before; the identity layer adds its own and *references* them.
- Service-to-service authentication is untouched: a tenant API key or service
  account credential authenticates through the same dependency chain and
  produces a principal the same routes already accept.

The one deliberate change to existing behaviour is documented in
[`IDENTITY-SECURITY.md`](IDENTITY-SECURITY.md) §7: "revoke every credential for
this user" now closes `user_sessions` rows as well as `refresh_tokens`. Before
this layer the session row did not exist, so there was nothing to close.

**Not in this layer** (the next P0): Organization / Tenant / Environment
expansion, delegated tenant administration, cross-tenant identity federation.

---

## 2. Layout

```
app/auth/identity/
  models.py        19 tables (below)
  policies.py      the policy engine: load + evaluate, default policy
  service.py       IdentityContext, assert_privileged, reauth, features
  sessions.py      session rows: create, touch, revoke, evict, suspicious
  mfa.py           TOTP enrollment, challenges, recovery codes, lockout
  totp.py          RFC 6238 arithmetic (no I/O, pure and unit-tested)
  tokens.py        single-use token minting + hashing
  secrets.py       envelope encryption for identity secrets (never plaintext)
  events.py        audit emission for the identity vocabulary
  email.py         reset / verification tokens and message rendering
  api_keys.py      tenant API keys: issue, rotate, revoke, authenticate
  service_accounts.py  machine identities and their credentials
  domains.py       DNS TXT ownership proof and enforcement policy
  principals.py    machine credentials -> request principal
  sso/
    service.py     connection CRUD, state, login completion, provisioning
    oidc.py        discovery, PKCE, code exchange, ID-token verification
    saml.py        XML-DSig verification, assertion validation, metadata
    claims.py      claim/role/group normalization and mapping rules
  scim/
    service.py     Users, Groups, credentials, filtering, PATCH semantics
    schemas.py     RFC 7643 resource shapes
```

Route modules (10, all registered in `app/main.py`):

| Module | Prefix | Routes |
| --- | --- | --- |
| `identity_routes.py` | `/api/identity` | 5 |
| `mfa_routes.py` | `/api/mfa` | 9 |
| `session_routes.py` | `/api/sessions` | 5 |
| `password_routes.py` | `/auth` | 5 |
| `api_key_routes.py` | `/api/api-keys` | 5 |
| `service_account_routes.py` | `/api/service-accounts` | 15 |
| `domain_routes.py` | `/api/domains` | 7 |
| `sso_routes.py` | `/api/sso` (13) + `/auth/sso` (7) | 20 |
| `scim_routes.py` | `/api/scim` (4) + `/scim/v2` (14) | 18 |

`scripts/check_identity_route_contracts.py` asserts the mechanical contract
these modules share (dependency names, response models, tenant scoping) and
fails the build rather than letting one route drift; it currently reports
`OK: keyword contracts hold across 8 route modules`.

---

## 3. Data model

`alembic/versions/0013_enterprise_identity.py` creates the tables and the audit
vocabulary; `0014_identity_audit_actions.py` repairs the labels to the form
SQLAlchemy actually writes and adds `SECURITY_SETTINGS_CHANGED`;
`0015_credential_auth_rejected.py` adds the label a refused machine credential
is recorded under. Head is `0015`
(pinned by `tests/test_deployment.py` and `tests/test_enterprise_persistence.py`).

| Table | Purpose |
| --- | --- |
| `identity_policies` | one row per tenant; created only when an administrator changes something |
| `user_sessions` | the device/sitting a user can see and revoke |
| `user_emails` | email change + verification state |
| `mfa_factors` | TOTP factors (sealed seed, last consumed step) |
| `mfa_recovery_codes` | hashed, single-use recovery codes |
| `mfa_challenges` | in-flight second-factor challenges, failure counters, lockout |
| `sso_connections` | OIDC/SAML connection configuration per tenant |
| `sso_connection_certificates` | sealed IdP certificates, with rotation state |
| `sso_login_attempts` | every federated attempt and why it ended |
| `identity_mappings` | group/role → VoxDesk role mapping rows |
| `scim_credentials` | hashed, rotatable SCIM bearer tokens |
| `scim_group_mappings` | IdP groups tracked for role synchronisation |
| `service_accounts` | machine identities, scopes, expiry, ownership |
| `service_account_credentials` | hashed credentials belonging to service accounts |
| `api_keys` | hashed tenant API keys with explicit scopes |
| `enterprise_domains` | claimed domains and their enforcement posture |
| `domain_verifications` | DNS TXT challenges (per attempt, with expiry) |
| `password_reset_tokens` | hashed, single-use, expiring |
| `email_verification_tokens` | hashed, single-use, expiring |

Every secret column holds ciphertext or a hash — never a plaintext secret.
`app/auth/identity/secrets.py` seals with the deployment key ring
(`IDENTITY_ENCRYPTION_KEYS`, falling back to `CRM_ENCRYPTION_KEYS`) under the
AAD `voxdesk:identity:v1:{tenant}:{purpose}`, so a row copied between tenants
fails to decrypt rather than silently working.

---

## 4. The policy engine

`app/auth/identity/policies.py` is the single decision point. Every login,
every session, every dangerous action and every machine credential goes through
it; no route re-implements a rule.

### 4.1 Resolution

`load_policy(session, tenant_id)` returns a frozen `ResolvedPolicy`. A tenant
with no `identity_policies` row gets `default_policy()`, which is exactly the
pre-existing behaviour: password login allowed, MFA available but not required,
SSO optional, API keys and service accounts allowed. **Enabling this feature
cannot change a tenant until an administrator changes a setting.**

`ResolvedPolicy` is frozen and composed with `dataclasses.replace`, so a
half-updated policy cannot leak out of a request.

### 4.2 Evaluation entry points

| Function | Answers |
| --- | --- |
| `evaluate_login_policy` | may this principal finish a login of this kind? |
| `evaluate_session_policy` | how long, and how many? (idle, absolute, max active) |
| `evaluate_privileged_action` | may this session do a dangerous thing right now? |
| `evaluate_api_credential` | may this machine credential act at all? |
| `evaluate_scope_grant` | may this actor grant these scopes to that credential? |
| `scopes_allow` | does this credential's scope set cover this permission? |
| `evaluate_sso_policy` | is federation usable for this tenant / this address? |
| `evaluate_domain_policy` | does a *verified* domain claim restrict this address? |
| `evaluate_password_reset_policy` | may this account use the reset flow? |

Each returns a frozen decision dataclass with `allowed` and a `reason` string.
A refusal is a policy statement, and the reason is surfaced to administrators
(never as a generic failure): a misconfiguration must be diagnosable.

### 4.3 The rules that matter

- **MFA requirement precedence** (`mfa_required_for`): a per-user override
  (`users.mfa_required`) beats the tenant rule *in both directions*, then the
  admin-role rule, then the tenant-wide rule. A break-glass account that the
  tenant policy could re-arm would not be one.
- **An unproven claim imposes nothing.** `evaluate_login_policy` gates every
  domain-based restriction on `domain_policy.verified`. A domain an
  administrator merely typed, or a claim an IdP asserts without proof, cannot
  refuse a login. This is the anti-pre-hijack rule.
- **Freshness, not factors.** A privileged action requires a *fresh proof of
  presence*: a second factor if one is enrolled, otherwise the account
  password. An operator with no factor is never locked out of their own
  settings. A stale proof returns `allowed=True` with `requires_mfa` /
  `requires_password` — the route turns that into **428**, not 403, so the
  client knows to re-present rather than to give up.
- **Machine credentials cannot reauthenticate.** `API_KEY`,
  `SERVICE_ACCOUNT` and `SCIM` principals get
  `machine_credential_cannot_reauth` for any privileged action. A leaked key
  cannot rewrite the tenant's SSO.
- **Scope grants are validated against the *actor*.**
  `evaluate_scope_grant` refuses `unknown_scope:…`, `platform_scope_not_grantable:…`
  and `scope_not_held:…`. A credential can never be issued more authority than
  its issuer holds.

### 4.4 Reauthentication

`POST /api/identity/reauth` accepts the account password (or a fresh MFA code)
and records the proof on the *session row* (`password_confirmed_at`,
`mfa_verified_at`). `assert_privileged` is the guard every dangerous route
calls; it raises `ReauthenticationRequired` (428, `detail.code =
"reauth_required"`) when the proof is stale, and
`machine_credential_cannot_reauth` when the caller is a machine.

---

## 5. Sessions and devices

`user_sessions` is the sitting behind a credential. A session carries the
device label, IP, user agent, auth method, whether MFA was satisfied and when,
the SSO connection it came from, `last_seen_at`, an idle deadline and an
absolute deadline.

- `POST /auth/login` (and the SSO callback) create one through
  `sessions.create_session`, which also evicts the oldest session when the
  tenant's `session_max_active` ceiling is exceeded — including `keep=0`, i.e.
  a ceiling of exactly one session.
- Refresh tokens stay the credential (`refresh_tokens` is unchanged: hashed,
  single-use, reuse-detected). Revoking a session revokes the tokens issued
  for it, so a credential cannot outlive its sitting.
- `GET /api/sessions` lists the caller's live sessions; `DELETE
  /api/sessions/{id}` ends one; `POST /api/sessions/revoke-others` keeps only
  the caller; `DELETE /api/sessions` ends everything; `PATCH
  /api/sessions/{id}` renames a device.
- `SESSION_SUSPICIOUS` is emitted when a *new* address appears while another
  session is already live. A first sign-in is not suspicious and is not
  reported — an alert that fires on every first login is an alert nobody reads.

The full invalidation matrix (logout, logout-all, reuse detection, password
reset, MFA disable, admin MFA reset, idle, expiry) is tested in
`tests/security/test_session_invalidation.py`.

---

## 6. Audit

Identity uses the existing `audit_logs` table and `AuditAction` enum. 53 labels
were added in `0013`, one more in `0014` (`SECURITY_SETTINGS_CHANGED`) and one
more in `0015` (`CREDENTIAL_AUTH_REJECTED`), for 85 members total.
`tests/test_enum_consistency.py` and
`tests/test_identity_migrations.py` keep the Python enum and the PostgreSQL
`auditaction` labels identical — case-sensitively, because `ALTER TYPE` with a
lower-cased value is exactly the bug that file was written after.

Identity emits, among others: `MFA_ENROLLMENT_STARTED`, `MFA_ENABLED`,
`MFA_DISABLED`, `MFA_VERIFIED`, `MFA_FAILED`, `MFA_CHALLENGE_LOCKED`,
`MFA_RECOVERY_CODE_USED`, `MFA_RECOVERY_CODES_REGENERATED`, `SESSION_CREATED`,
`SESSION_REVOKED`, `SESSION_SUSPICIOUS`, `IDENTITY_REAUTHENTICATED`,
`IDENTITY_LINK_REJECTED`, `PASSWORD_RESET_REQUESTED`,
`PASSWORD_RESET_COMPLETED`, `EMAIL_VERIFICATION_SENT`, `SSO_*` (13),
`SCIM_*` (10), `API_KEY_*` (3), `SERVICE_ACCOUNT_*` (7), `DOMAIN_*` (5).

`GET /api/identity/events` returns the caller's tenant's identity events to an
administrator. Nothing in the identity vocabulary logs a password, token, API
key, OAuth secret, SAML assertion, recovery code, session secret or
`Authorization` header: events carry ids, reasons and counts.

---

## 7. Authorization

Every route is covered by `tests/security/test_authorization_matrix.py`, which
runs three layers:

1. **Pure policy units** — the tables above, evaluated directly, including the
   precedence rules and the "unproven claim imposes nothing" case.
2. **A role × route HTTP matrix** — anonymous, VIEWER, AGENT, MANAGER, ADMIN,
   OWNER (and a second tenant's owner) against every identity route.
3. **An owner sweep** — every registered identity route is called by a
   legitimate owner and must not answer 5xx; more than 50 routes are probed.

The resulting policy, in one table:

| Surface | Who |
| --- | --- |
| `GET /api/identity/{policy,events}`, api-keys, service-accounts, domains, SSO connections, SSO attempts, SCIM credentials | OWNER / ADMIN — the administrative surfaces |
| `GET /api/identity/status`, `GET /api/mfa/status`, `GET /api/sessions` | any signed-in human — self-service reads |
| reauth, MFA enroll/confirm/verify/challenge/disable/recovery-codes, `revoke-others` | any signed-in human — self-service writes |
| SCIM `/scim/v2/...` | a SCIM credential, tenant-bound; never a user JWT |
| everything above | refused for API keys and service accounts unless explicitly allowed |

"Authenticated" is never sufficient on its own: the routes call
`require_permission`, `require_human_session` and `assert_privileged` as
appropriate, and the matrix asserts the refusal, not just the permission.

---

## 8. Configuration

All settings live in `app/core/config.py` and are validated at boot by
`Settings.validate_security`. Production refuses the unsafe combinations
outright rather than starting degraded.

| Setting | Default | Meaning |
| --- | --- | --- |
| `IDENTITY_ENCRYPTION_KEYS` | falls back to `CRM_ENCRYPTION_KEYS` | key ring for every identity secret; refused at boot in production when SSO or MFA is enabled and no ring exists |
| `MFA_ENABLED` | `true` | master switch; when false the MFA routes answer 404 |
| `MFA_ISSUER` | `VoxDesk` | label in the authenticator app |
| `MFA_TOTP_DIGITS` / `MFA_TOTP_PERIOD_SECONDS` | `6` / `30` | TOTP parameters |
| `MFA_TOTP_WINDOW` | `1` | steps accepted either side of now |
| `MFA_RECOVERY_CODE_COUNT` | `10` | codes issued per regeneration |
| `MFA_MAX_VERIFICATIONS` / `MFA_VERIFICATION_WINDOW_SECONDS` | `10` / `300` | per-user rate limit |
| `MFA_CHALLENGE_MAX_FAILURES` / `MFA_CHALLENGE_LOCKOUT_MINUTES` | `5` / `15` | per-challenge burn + lockout |
| `MFA_CHALLENGE_TTL_SECONDS` | `300` | how long a half-finished login stays resumable |
| `MFA_FRESH_MINUTES` | `15` | default privileged-action freshness window |
| `SSO_ENABLED` | `true` | master switch for federation |
| `SSO_STATE_TTL_SECONDS` | `600` | lifetime of an in-flight authorization request |
| `SSO_CLOCK_SKEW_SECONDS` | `120` | accepted skew for ID tokens and assertions |
| `SSO_ALLOWED_REDIRECT_HOSTS` | empty | extra redirect hosts, comma-separated |
| `SSO_GROUP_CLAIM` / `SSO_ROLE_CLAIM` | `groups` / `roles` | default claim names for mapping |
| `SCIM_ENABLED` | `true` | master switch for SCIM |
| `SCIM_DEFAULT_PAGE_SIZE` / `SCIM_MAX_PAGE_SIZE` | `100` / `500` | pagination |
| `API_KEY_PREFIX` / `SERVICE_ACCOUNT_PREFIX` / `SCIM_TOKEN_PREFIX` | `vdk` / `vdsa` / `vdscim` | identification prefixes (never secrecy) |
| `SESSION_IDLE_MINUTES` / `SESSION_MAX_ACTIVE` | `720` / `20` | session defaults |
| `EMAIL_TRANSPORT` | `log` | `log` or `smtp`; **production refuses `log`** |
| `PASSWORD_RESET_TTL_MINUTES` | `30` | reset token lifetime |
| `IDENTITY_DEBUG_TOKENS` | `false` | see §9 |

---

## 9. `IDENTITY_DEBUG_TOKENS`

A password-reset request answers `202` with a byte-identical body whether or not
the address exists — that uniformity is what keeps the endpoint from being an
account oracle. The development affordance that hands the fresh token back in
the response breaks that property, so it is an explicit, off-by-default switch
that can only take effect **outside production and only with the `log`
transport**. `Settings.validate_security` refuses the combination in
production.

---

## 10. Verifying this layer

| Command | What it proves |
| --- | --- |
| `python3 -m pytest tests/auth -q` | TOTP vectors, sessions and devices, MFA, credentials, API keys, service accounts, SCIM, SSO, domains, policy, principals, password reset, email verification (442 tests) |
| `python3 -m pytest tests/security -q` | authorization matrix, session invalidation, SSO, sealed MFA secrets (197 tests) |
| `python3 -m pytest tests/integration -q` | the cross-module stories: hire → provision → federate → suspend → deprovision, domain enforcement, and the cross-workspace sweep (22 tests) |
| `python3 -m pytest tests/test_identity_migrations.py -q` | migration ↔ model ↔ enum parity, read out of the migration files |
| `python3 -m pytest tests/test_identity_pg_enum.py -q` | that the live **PostgreSQL** `auditaction` accepts every `AuditAction` — the one failure mode SQLite cannot show. Skips with a printed reason when `DATABASE_URL` is not PostgreSQL |
| `python3 -m pytest tests/test_enum_consistency.py -q` | every enum member has a migration |
| `python3 -m pytest tests -q` | the whole regression suite |
| `python3 scripts/check_identity_route_contracts.py` | the route contract across the identity modules |
| `ruff check app/ tests/ alembic/` | lint gate |

Two of those gates need a reachable PostgreSQL 17 (`tests/test_api_contract.py`
and `tests/test_identity_pg_enum.py`); the rest of the suite runs against
in-memory SQLite, which is why the enum check above exists at all.

**Dependencies.** This feature adds exactly one package — `signxml`, used by
`tests/security/test_sso.py` to mint independent SAML assertions, pinned at
`5.1.0` because `4.2.0` is broken against the resolver stack in this repository.
Everything else is already in the tree: `PyJWT` for OIDC ID tokens (pinned
algorithms, `none` refused even with an empty key), `cryptography` for RSA and
X.509, `httpx` for the discovery/JWKS/DoH calls, and `sqlalchemy`/`alembic` for
the tables. No new runtime service, no new external API, nothing to enable.

`tests/security/test_sso.py` mints its own signed SAML assertions with
`signxml` (an independent XML-DSig implementation) and its own JWKS, so the
hand-written verifier in `sso/saml.py` is checked against a second
implementation rather than against itself. There is no fake provider in
production code and no `real_provider`-marked test that is silently skipped:
everything in this table runs in the default suite.

---

## 11. Operating notes

- **Break-glass.** Keep one owner account with `users.mfa_required = false` and
  no factor. It can always sign in with a password and can always re-enter the
  tenant's identity settings. Test it before you need it.
- **Emergency disable.** A service account can be disabled outright
  (`POST /api/service-accounts/{id}/emergency-disable`), which is the lever to
  reach for when a machine credential leaks; it does not need the account owner
  to be present and it is audited.
- **Rotation.** SSO signing certificates, SCIM tokens, API keys and service
  account credentials all have a rotate path that keeps the old credential
  valid only until the new one is confirmed live, then revokes it.
- **When an audit row says `SSO_LOGIN_FAILED` or `MFA_FAILED`**, the detail
  carries the *check* that refused, not the secret that failed it. Use
  `GET /api/sso/attempts` for the connection-level view.
