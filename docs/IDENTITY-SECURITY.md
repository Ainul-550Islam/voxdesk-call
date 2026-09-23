# VoxDesk — Identity security model

How the enterprise identity system is put together, what it defends against, and
which invariant each mechanism exists to hold. Read this first; the
feature-specific documents ([`SSO-OIDC.md`](SSO-OIDC.md),
[`SSO-SAML.md`](SSO-SAML.md), [`SCIM.md`](SCIM.md), [`MFA.md`](MFA.md),
[`API-KEYS.md`](API-KEYS.md), [`SERVICE-ACCOUNTS.md`](SERVICE-ACCOUNTS.md),
[`DOMAIN-VERIFICATION.md`](DOMAIN-VERIFICATION.md),
[`ENTERPRISE-IDENTITY.md`](ENTERPRISE-IDENTITY.md)) fill in the details.

---

## 1. The five rules

Everything below is an instance of one of these:

1. **A secret exists in plaintext exactly once** — when it is issued, to the
   caller who asked for it. Everywhere else it is a digest or a sealed envelope.
2. **Nothing is trusted because it arrived.** An IdP's claims, a SCIM payload's
   `active`, a machine credential's tenant, a domain name somebody typed: each is
   either proved by DNS, by a signature, by a hash, or by a row in our database —
   or it is not believed.
3. **Fail closed, and fail with the truth.** An unverifiable signature, an
   inactive tenant, an unknown credential, a disabled user and a missing key ring
   all refuse. Refusals are specific enough for an operator to act on and vague
   enough not to enumerate.
4. **A check that matters lives in one place.** Fresh proof of presence is
   `assert_privileged`; the tenant policy is `policies.evaluate_*`; the
   provisioning decision is `claims.decide_provisioning`; XML-DSig is
   `saml.verify_signature`. A second copy of a security rule is a second rule.
5. **Authority never outlives its source.** A service-account credential whose
   owner is gone stops working. An SSO user the IdP keeps asserting but we
   disabled stays disabled. A resolved policy change takes effect on the next
   request, not at the next expiry.

---

## 2. Threat model

| Adversary | Has | Defence |
| --- | --- | --- |
| Credential stuffer | a valid password | per-user lockout after `MAX_FAILED_LOGINS` (default 8) for `LOCKOUT_MINUTES` (default 15); per-IP limits when `RATE_LIMIT_ENABLED`; MFA available per tenant and forced for admins; enumeration-safe errors |
| Phisher with the password *and* the TOTP code | one code | per-user verification rate limit (`MFA_MAX_VERIFICATIONS`, default 10 / `MFA_VERIFICATION_WINDOW_SECONDS` 300); a wrong code on one challenge burns and locks it (`MFA_CHALLENGE_MAX_FAILURES` 5 → `MFA_CHALLENGE_LOCKOUT_MINUTES` 15); a challenge expires in `MFA_CHALLENGE_TTL_SECONDS` (300) |
| Stolen session cookie | a live session | session rows with idle and absolute expiry, refresh rotation, revocation by the user (one / all others / all), revocation on password reset and on MFA change, `token_version` bump, suspicious-session event |
| Stolen laptop, unattended browser | a live session | privileged actions require **fresh** proof: MFA verified within `MFA_FRESH_MINUTES` (default 15) or a password confirmation within `PRIVILEGED_REAUTH_MINUTES`, else **428** and the user is asked again |
| Leaked API key | a machine credential | hash-only storage, explicit scopes that cannot exceed the creator's, prefix for identification, expiry, last-used, per-request policy evaluation, revoke and rotate that leave no overlap window |
| Leaked provisioning token | a SCIM credential | tenant-bound by its row, scopes limited to `scim:users`/`scim:groups` (never RBAC values), cannot call the product API at all, rotate/revoke, one identical 401 for every failure |
| Malicious or compromised IdP | the ability to mint assertions and tokens | signature verified against a **pinned** certificate/JWKS, issuer, audience, destination, recipient, `InResponseTo` and nonce binding, time windows with skew, single-use state, single-use assertion id, one assertion per response, no DTD, `active`/tenant checks, and a refusal to ever cross tenants |
| Someone who types a domain into the dashboard | a claim | unverified claims route nobody and enforce nothing; only DNS proof makes a domain real, and only an explicit second action makes it enforce |
| A tenant trying to reach another tenant | valid credentials of their own | every query is keyed on the caller's `tenant_id`; a foreign id is 404, never 403; cross-tenant email during SSO is refused and audited (`IDENTITY_LINK_REJECTED`) |
| Curious administrator | `identity:write`, and the ability to trip an emergency stop | they cannot clear their own emergency stop (owner only, with a reason), cannot read any secret back, cannot grant themselves a scope, and every action is audited with the actor |
| Operator mistake | a plan | enforcement is two-key, the last active owner cannot be deprovisioned or MFA-locked out, retiring the last SAML certificate is refused, and every destructive action is reversible or recorded with a reason |

---

## 3. Where the code lives

```
app/core/config.py            identity settings + production refusals
app/auth/permissions.py       39 permissions; is_platform_permission
app/auth/rbac.py              role → permissions; _guard = human + privileged
app/auth/dependencies.py      require_permission, require_human_session
app/auth/identity/
  models.py                   19 tables, all tenant-keyed
  policies.py                 evaluate_login|session|privileged_action|
                              api_credential|sso_policy|domain_policy|scope_grant
  secrets.py                  the AES-256-GCM key ring
  events.py                   audit emission + the scrub list
  sessions.py                 device sessions, revocation, suspicious-session
  mfa.py                      TOTP, recovery codes, challenges
  api_keys.py / service_accounts.py / scim/      machine credentials
  sso/                        oidc.py, saml.py, claims.py, service.py
  domains.py                  DNS proof and enforcement
app/api/                      identity_routes, mfa_routes, session_routes,
                              password_routes, machine_routes, domain_routes,
                              sso_routes, scim_routes (+ the `sso` package)
```

---

## 4. Secrets at rest

Identity secrets are sealed with the same AES-256-GCM envelope as CRM
credentials: `encrypt_text(plaintext, tenant_id=…, purpose=…)` returns
`(envelope, key_id)` and the associated data is

```
voxdesk:{purpose}:v1:{tenant_id}
```

so a ciphertext cannot be moved between tenants **or** between purposes: a
sealed PKCE verifier cannot be replayed as an OIDC client secret even by someone
who can write rows, because the AAD differs. `key_id` records which key sealed
it, so a ring can be rotated by adding a key and re-sealing over time.

Encrypted fields: TOTP seeds, recovery-code digests' key material, OIDC client
secrets, SAML certificates (`pem_encrypted` + `pem_key_id`), PKCE verifiers,
SCIM and service-account credential digests' secrets. `seal_preview` exists for
diagnostics and never reveals the envelope.

**Plaintext is never stored, and never logged.** `identity_debug_tokens` is
`False` by default, and production refuses to boot with MFA or SSO enabled and no
key ring configured (`IDENTITY_ENCRYPTION_KEYS`, falling back to
`CRM_ENCRYPTION_KEYS`) — an unencryptable identity secret is a boot failure, not
a runtime surprise.

---

## 5. The audit pipeline

`identity_events.emit` is the only writer of identity audit rows. It:

1. **scrubs** the detail payload against `FORBIDDEN_DETAIL_KEYS` — `password`,
   `token`, `access_token`, `refresh_token`, `id_token`, `api_key`, `secret`,
   `client_secret`, `totp_secret`, `recovery_code`, `saml_assertion`,
   `saml_response`, `certificate_pem`, `private_key`, `code_verifier`, `state`,
   `nonce`, `authorization`, `cookie`, `code`, `otp` and their friends, replaced
   with `***`;
2. **logs a warning** when it had to scrub something, with the action and the
   number of masked fields — a caller that tried to record a credential is a bug
   worth seeing (the scrub list is why a refusal records the key *prefix* and not
   a field called `credential`: a field by that name is redacted wholesale);
3. **truncates** any value over 500 characters, because a 400 KB claim blob
   inline is a retention problem wearing an audit trail's clothes;
4. writes the row, with the tenant, actor, target, IP, user agent and detail.

Emission during a mutating operation uses `commit=False` and rides the caller's
transaction, so an event cannot be recorded for an action that then rolled back —
the audit trail is written if and only if the change happened. 85 `AuditAction`
members cover the vocabulary; the categories are login, MFA, session, SSO, SCIM,
API key, service account, domain, credential and settings.

**A refused credential is an event.** `CREDENTIAL_AUTH_REJECTED` is written
whenever a machine token *resolved to a row* and was then refused: revoked,
expired, its service account switched off or expired, its credential family
disabled for the workspace, or its owner suspended or gone. The HTTP answer stays
the uniform `Invalid credential`, so the caller learns nothing; the operator
learns which key, which family and why. A token that matches no row writes
nothing at all — otherwise anyone could fill the audit table by guessing, and the
front door would be a request-amplification attack.

The reasons, and they are stable identifiers a runbook can match on:
`api_key_revoked`, `api_key_expired`, `service_account_credential_revoked`,
`service_account_credential_expired`, `service_account_disabled`,
`service_account_expired`, `api_keys_disabled`, `service_accounts_disabled`,
`tenant_inactive`, `tenant_missing`, `credential_owner_inactive`,
`credential_owner_missing`.

Where a value must appear, it appears **hashed**
(`subject_hint = hash_token(subject)[:16]`), never quoted.

---

## 6. Authorization

| Layer | Rule |
| --- | --- |
| role | OWNER / ADMIN / MANAGER / AGENT; ADMIN holds `identity:read`/`identity:write`, `api_key:manage`, `service_account:manage` |
| permission | 39 values; `require_permission` on every route |
| human | `require_human_session` for self-service and for anything a machine must never do |
| platform | `TENANT_CREATE` / `TENANT_DELETE` are `_PLATFORM_ONLY` and are never grantable to a tenant role |
| fresh proof | `assert_privileged` for actions that change how the tenant authenticates |
| tenant | every query keyed on the caller's `tenant_id` |

`rbac._guard` is `_require_human` followed by `assert_privileged`, so a route that
calls it cannot accidentally skip either. A machine credential that reaches a
privileged action is refused with `machine_credential_cannot_reauth`: a machine
cannot prove a person is present, so it may not change SSO, reset MFA, issue
credentials or lift an emergency stop.

`evaluate_scope_grant` is the privilege-escalation guard for machine credentials:
a requested scope must be known, must not be a platform permission, and must be
held by the human creating it. Unknown scopes are refused rather than dropped —
silently narrowing a scope list would leave an operator believing an integration
has access it does not.

**Never rely on `authenticated=True`.** The authorization matrix
(`tests/security/test_authorization_matrix.py`, 115 tests) asserts every
identity route against VIEWER / AGENT / MANAGER / ADMIN / OWNER, a second
tenant, an API key, a service-account credential, and an anonymous caller: who
gets 200, who gets 403, who gets 404, who gets 428, and that nothing returns 5xx.

---

## 7. Sessions

A session is a row (`user_sessions`) plus a JWT pair. The row is the truth:

- **Lifetimes** come from `evaluate_session_policy`: idle expiry, absolute
  expiry (refresh days) and the concurrent-session ceiling, which **evicts the
  oldest** rather than refusing the new login — a user locked out of their own
  account because of a forgotten phone is a support ticket, not a security win.
- **Revocation cascades.** `revoke_session`, `revoke_sessions`,
  `revoke_other_sessions` and `revoke_all_for_user` all route through
  `_revoke_tokens_for_session`, so every unexpired `RefreshToken` row belonging
  to a revoked session is revoked with it — including in the bulk paths used by
  password reset, MFA change and SCIM deprovisioning.
- **Password and MFA changes invalidate sessions.** A reset revokes every other
  session; changing the second factor does the same, because a change of
  credential is exactly the moment to distrust sessions established with the old
  one.
- **Suspicious sessions are reported, not blocked.** A login from a new address
  or device while other sessions are live emits `SESSION_SUSPICIOUS` with
  `reason = new_ip_address` or `new_device`. A first login from a fresh laptop is
  ordinary, and reporting it would train operators to ignore the event; the user
  is never locked out by a heuristic.
- Session secrets, refresh tokens and cookies never appear in a log line or an
  audit detail.

---

## 8. Enumeration and rate limits

- Password reset and email verification answer **uniformly**: the same shape of
  response whether or not the address exists, so the endpoint cannot be used as
  an account oracle.
- SCIM, API key and service-account authentication return **one** error for
  every failure mode (unknown, revoked, expired, malformed, wrong secret), for
  the same reason.
- SSO callback failures are collapsed to a single public error body; the
  specific reason goes to the audit trail.
- Identity errors are translated by `identity_errors.translate`, so a
  `PolicyDenied` is 403 with `{code: policy_denied, reason: <rule>}` — the rule
  name is for the operator, and the message stays safe to show a user.
- Rate limits: `RATE_LIMIT_LOGIN_PER_MINUTE` (default 10) for auth routes,
  `RATE_LIMIT_BURST` for the rest, plus the per-user MFA verification window and
  the per-domain DNS attempt ceiling.

---

## 9. Tenant isolation invariants

1. Every identity table has `tenant_id`, and every read and write is keyed on it.
2. A resource id from another tenant answers **404**, identical to a resource
   that does not exist — never 403, which would confirm it exists.
3. A sealed secret's AAD includes the tenant, so a ciphertext moved between rows
   does not decrypt.
4. A federated login can never cross tenants: an email that belongs to another
   tenant is refused and audited (`address_belongs_to_another_tenant`), and a
   domain verified by two tenants refuses the login rather than guessing.
5. A machine credential's tenant comes from its row, never from the request;
   its owner is re-checked on every request.
6. A resolved policy is per tenant; a second tenant sees defaults, asserted by
   the matrix test.

These are covered by `tests/security/test_authorization_matrix.py` and
`tests/security/test_session_invalidation.py`, plus the SSO suite's
cross-tenant refusal tests.

---

## 10. Concurrency and single use

Several defences are only real if they are atomic, and the code is written so
the database enforces them rather than the application:

| Mechanism | How it is enforced |
| --- | --- |
| SSO state (CSRF) | one conditional `UPDATE … WHERE consumed_at IS NULL AND expires_at > now() RETURNING` — two concurrent callbacks cannot both win |
| SAML assertion replay | `UNIQUE(assertion_id)`; a second use is an `IntegrityError` mapped to `SSOReplayDetected` |
| Refresh-token rotation | the row is spent in the same transaction that issues the replacement |
| Credential rotation | replacement issued and predecessor revoked in one transaction — no overlap window, for API keys, SCIM tokens and service-account credentials alike |
| Domain challenge | re-issuing supersedes every open challenge, so an old TXT record is inert |
| Recovery codes | single-use rows, an `UPDATE … WHERE used_at IS NULL` claim |
| Session ceiling | eviction happens inside the create transaction |
| Last owner | counted and checked in the same transaction that would remove it |

The test suites exercise these by running the competing operations in sequence
against a real database, and the identity migrations suite asserts the unique
constraints and enum values actually exist in the migrated schema.

---

## 11. What is deliberately *not* done

- **No auto-enforcement.** Verifying a domain never refuses a password login by
  itself; that is a second, explicit decision.
- **No heuristic lockouts.** A new device is reported, not blocked.
- **No silent role clamping.** An unmapped role is refused with the fix in the
  message, never downgraded behind the operator's back.
- **No shared secrets a machine can use for administration.** A machine
  credential may do the work it was scoped for and nothing about how the tenant
  authenticates.
- **No "just this once" bypass.** There is no debug flag that returns a token in
  a response: `identity_debug_tokens` exists as a setting, defaults to `False`,
  and the paths that read it are gated and audited. **Production refuses to boot
  with it on.**
- **No security through obscurity.** Every rule here is enforced by code that
  the tests exercise, not by naming or by hiding a route.

---

## 12. Verifying this document

```
ruff check app/ tests/                                   # style and dead code
python3 -m pytest tests/security -q                      # authz matrix, SSO, sessions
python3 -m pytest tests/auth -q                          # TOTP, sessions, credentials
python3 -m pytest tests/test_identity_migrations.py -q    # the schema the rules rely on
python3 scripts/check_identity_route_contracts.py         # route contracts
python3 -m pytest tests/test_api_contract.py -q           # no route answers 5xx
```
