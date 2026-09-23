# VoxDesk — API Keys

Tenant-scoped credentials for machines and scripts: created with explicit
scopes, shown once, stored hashed, listed, rotated and revoked. Implementation:
`app/auth/api_keys.py` (a re-export facade) over the implementation in
`app/auth/identity/api_keys.py`; routes in `app/api/api_key_routes.py`.

---

## 1. Shape of a key

```
vdk_<32 hex chars of the row id>_<32 bytes of secret>
│    │                              └── the secret: hashed at rest, never stored
│    └── the key's identity: the row is looked up by it, so no scan is needed
└── identification prefix (API_KEY_PREFIX); it says what kind of token this is
```

Stored on the row: `id`, `tenant_id`, `user_id` (owner), `name`, `prefix`
(display form `vdk_1a2b3c4d`), `secret_hash`, `scopes`, `created_by_user_id`,
`created_at`, `expires_at`, `last_used_at`, `revoked_at`, `revoked_reason`,
`rotated_from_id`.

Three properties worth stating plainly:

- **The secret is never stored.** Only `identity_tokens.hash_token(secret)` is
  persisted; verification is a constant-time comparison. `tests/auth/test_identity_credentials.py`
  asserts the plaintext never appears in the row.
- **The token is shown once**, in the response to the create or rotate call.
  There is no endpoint that reveals it again — a lost key is rotated, not
  recovered.
- **The prefix is not a secret.** It is how a human tells two keys apart in the
  list; two keys can share a prefix and still be different keys (the row id is
  the identity).

---

## 2. Endpoints

| Method | Path | Permission | Notes |
| --- | --- | --- | --- |
| `GET` | `/api/api-keys` | `api_key:manage` | tenant keys, newest first; `include_revoked` available |
| `GET` | `/api/api-keys/scopes` | `api_key:manage` | the scope catalogue this actor may grant, with labels |
| `POST` | `/api/api-keys` | `api_key:manage` | 201; returns the token **once** |
| `DELETE` | `/api/api-keys/{key_id}` | `api_key:manage` | 204; `revoked_reason = "revoked_by_admin"` |
| `POST` | `/api/api-keys/{key_id}/rotate` | `api_key:manage` | new token; the old one dies in the same transaction |

`api_key:manage` is held by OWNER and ADMIN only (`app/auth/rbac.py`). No route
in this module accepts a machine credential: every one of them takes a
`TenantContext` from a human session, and reauthentication-style actions are
additionally guarded by `assert_privileged`. Service-account-owned keys are
created through `POST /api/service-accounts/{id}/keys`
(`service_account:manage`), so the credential is attributed to the machine
identity rather than to a human who might leave.

---

## 3. Scopes

Scopes are **permission values**, not free-form strings: each element of
`scopes` must be a member of `app.auth.permissions.Permission`. Three refusals
make escalation impossible, all from `policies.evaluate_scope_grant`:

| Refusal | Meaning |
| --- | --- |
| `unknown_scope:<value>` | not a permission in this codebase — a typo is caught at creation, not at use |
| `platform_scope_not_grantable:<value>` | creating/deleting whole tenants; never grantable to a credential |
| `scope_not_held:<value>` | the *creator* does not hold this permission, so they cannot hand it out |

The rule is enforced in the **service**, not only in the route
(`api_keys.create_key` and `service_accounts.create_account` both call
`validate_scopes`), so a future call site cannot forget it and quietly mint
privilege. `scopes_from_permissions` / `describe_scopes` build the picker the
dashboard renders; `grantable_scopes(role)` narrows it to what that role holds.

A credential authorised by a key gets exactly its scopes —
`AuthenticatedPrincipal.scopes` — and every `require_permission` check consults
them. Scopes are a *ceiling* on top of the owner's standing: revoking a
permission from the owner, deactivating them, or deleting their tenant all stop
the key at the next request (§5).

---

## 4. Expiry and rotation

- `expires_in_days` is optional. When set, `expires_at` is stored and an expired
  key is refused by the same uniform error as a revoked one.
- **Rotation does not overlap.** `rotate_key` creates the replacement and
  revokes the old key (`revoked_reason = "rotated"`) in one transaction, and
  records `rotated_from_id` so the lineage is auditable. A window in which two
  credentials are valid is where a leaked key hides; there is no such window
  here.
- Rotation carries the previous key's scopes and expiry unless the caller
  overrides the expiry, so a rotation cannot silently grant more authority than
  the key it replaces.
- `API_KEY_CREATED`, `API_KEY_ROTATED` and `API_KEY_REVOKED` are emitted with
  the key prefix (never the secret) and the actor.

A rotation incident — "we think this key leaked" — is: rotate, then confirm the
old key answers 401, then look at what it did before the rotation in
`audit_logs`.

---

## 5. Every request re-checks the world

`authenticate_credential` resolves the token to a row, then to a principal, and
the resolution is deliberately paranoid:

1. The row must exist and the secret must hash-match (constant time).
2. `revoked_at` must be null and `expires_at` must be in the future, otherwise
   the *same* `CredentialError("Invalid credential")` as a malformed token —
   a caller cannot tell "revoked" from "unknown".
3. The tenant must exist and be active.
4. The tenant policy must still allow this credential family
   (`evaluate_api_credential`): switching API keys off in `identity_policies`
   revokes every key immediately, per request, not at expiration.
5. For a service-account-backed key: the account must be enabled, not
   emergency-disabled, and unexpired.
6. The **owner must exist, be active, and be in the same tenant**. A key whose
   owner was deactivated stops working — the credential never outlives the
   human or machine authority behind it.

`last_used_at` is updated at most once per `LAST_USED_WRITE_INTERVAL`, so a hot
credential does not turn every request into a write.

**Each of those six refusals is recorded**, as `CREDENTIAL_AUTH_REJECTED` with
the reason (`api_key_revoked`, `api_key_expired`, `api_keys_disabled`,
`credential_owner_inactive`, …), the credential's prefix and its kind. The
response stays the uniform `Invalid credential`, so a caller cannot tell a
revoked key from a malformed one — the *operator* is the party who needs to know
that a key they revoked is still being presented. A token that resolves to no row
writes nothing, so the endpoint cannot be used to fill the audit table.

Revocation and rotation are compare-and-set writes (`WHERE revoked_at IS NULL`):
two administrators revoking the same key produce one revocation and one audit
row, and two rotating it produce one replacement and one 404 — never two live
keys. Because the compare-and-set deliberately bypasses the identity map, the
service refreshes the caller's own copy of the row afterwards, so a revocation is
immediately visible to the very session that performed it.

---

## 6. Limits, and what a key may never do

- An API key is **not** a user session. It cannot reach anything that requires
  `require_human_session`: `/api/identity/status`, MFA self-service, session
  management, or any privileged action
  (`machine_credential_cannot_reauth`). It cannot reauthenticate, because a
  machine cannot prove a human is present.
- It cannot be a super-admin JWT: the permissions it carries are the scopes on
  its row intersected with what the owner still holds, and it is never treated
  as an OWNER. `tests/security/test_authorization_matrix.py` drives an owner
  sweep and a machine-credential sweep over the whole identity surface and
  asserts a refusal (never a 5xx) on every route that requires a human.
- Tenant isolation is by `tenant_id` on the key row, not by anything the caller
  sends: the principal's tenant is read from the row, and no route accepts a
  tenant id from a machine credential's request body.

---

## 7. Tests

| File | Proves |
| --- | --- |
| `tests/auth/test_identity_credentials.py` | hashed storage, show-once, expiry, revocation, rotation lineage, tenant scope, scope-grant refusals |
| `tests/security/test_authorization_matrix.py` | machine credentials refused on every human-session route, including the self-service reads |
| `tests/test_api_contract.py` | the api-key routes keep their response shapes |
