# VoxDesk — Service accounts

A machine identity that belongs to a tenant: a named principal with its own
scopes, its own credentials, an owner, an expiry, and a disable switch that does
not need the credential to be found first. Implementation:
`app/auth/service_accounts.py` (a re-export facade) over the implementation in
`app/auth/identity/service_accounts.py`; routes in `app/api/service_account_routes.py`.

API keys are the subject of [`API-KEYS.md`](API-KEYS.md). A service account is
the heavier of the two: an API key is a token, a service account is a
*principal* that can hold several.

---

## 1. The shape

| Field | Notes |
| --- | --- |
| `id` | the identity, referenced by its credentials |
| `tenant_id` | the isolation boundary; a service account never spans tenants |
| `name` | unique **within the tenant**; a duplicate is refused with a clear message rather than a 500 from the constraint |
| `description` | free text, for the operator who finds it in a year |
| `scopes` | a list of RBAC permission values; the ceiling on everything the account can do |
| `enabled` | the customer-facing switch |
| `emergency_disabled` | the platform stop, clearable only by the path in §4 |
| `disabled_reason` / `disabled_at` | why, and since when |
| `expires_at` | optional expiry for the whole identity |
| `created_by_user_id` | **the owner**: whose permissions the account inherits, re-checked on every request |
| `last_used_at` | touched at most once per interval, not per request |

`ServiceAccountOut` reports all of that plus `scope_summary`
(`read-only` / `read-write` / `write-only` / `no scopes`) and
`credential_count`, so a list view needs no second call.

---

## 2. Endpoints

| Method | Path | Guard |
| --- | --- | --- |
| `GET` | `/api/service-accounts` | `service_account:manage` |
| `POST` | `/api/service-accounts` | `service_account:manage` + fresh proof |
| `GET` | `/api/service-accounts/{id}` | `service_account:manage` |
| `PATCH` | `/api/service-accounts/{id}` | `service_account:manage` + fresh proof |
| `POST` | `/api/service-accounts/{id}/enable` | `service_account:manage` + fresh proof |
| `POST` | `/api/service-accounts/{id}/disable` | `service_account:manage` + fresh proof |
| `POST` | `/api/service-accounts/{id}/emergency-disable` | `service_account:manage` + fresh proof |
| `POST` | `/api/service-accounts/{id}/emergency-clear` | **owner only** + fresh proof + reason |
| `DELETE` | `/api/service-accounts/{id}` | `service_account:manage` + fresh proof |
| `GET` | `/api/service-accounts/{id}/credentials` | `service_account:manage` |
| `POST` | `/api/service-accounts/{id}/credentials` | + fresh proof; 201, token shown once |
| `POST` | `/api/service-accounts/{id}/credentials/{cid}/rotate` | + fresh proof |
| `DELETE` | `/api/service-accounts/{id}/credentials/{cid}` | + fresh proof |
| `GET` | `/api/service-accounts/{id}/keys` | the account's named API keys |
| `POST` | `/api/service-accounts/{id}/keys` | + fresh proof; 201, token shown once |

`service_account:manage` is held by OWNER and ADMIN. Every mutating route also
requires a fresh proof of presence, so a hijacked session cannot arm a new
machine identity without the operator's password (or a fresh factor).

---

## 3. Credentials and keys

A service account can hold **two families** of secret, and the resolver accepts
either:

- **Credentials** (`ServiceAccountCredential`) — a long-lived secret for a
  daemon. Shape: `vdsa_<row id>_<32-byte secret>`, hashed at rest, shown once.
  They carry a label, `created_by_user_id`, `expires_at` (defaulting to the
  account's own expiry), `last_used_at` and revocation state.
- **API keys** (`APIKey` with `service_account_id` set) — a named,
  scope-limited token an operator can hand to a script and revoke individually.
  See [`API-KEYS.md`](API-KEYS.md) for the shape.

Both are created with the same rule: **a credential cannot exceed its maker.**
`validate_scopes` runs `policies.evaluate_scope_grant` against the *creating
human's* permissions and refuses `unknown_scope:`, `platform_scope_not_grantable:`
and `scope_not_held:`. `grantable_scopes(role)` produces the picker the
dashboard shows, so the UI cannot offer a scope the API would reject.

Rotation never overlaps: the replacement is issued and the predecessor revoked
(`revoked_reason = "rotated"`) in one transaction, and the pair is audited
(`SERVICE_ACCOUNT_CREDENTIAL_ROTATED`).

---

## 4. Disable, emergency disable, and the way back

Three distinct states, because "off" means different things to a customer and
to an operator:

| Action | Effect | Who can undo it |
| --- | --- | --- |
| `POST …/disable` | `enabled = false`, reason recorded; credentials survive so the account can be switched back on | any administrator, with a fresh proof |
| `POST …/emergency-disable` | `emergency_disabled = true`, `enabled = false`, reason defaults to `emergency_stop` | **an owner only**, through `…/emergency-clear`, with a reason |
| `DELETE` | identity removed; credentials and keys cascade; audit lines remain | nobody — it is a deletion |

The important asymmetry: tripping the stop is cheap and any administrator can
do it, because it is a mitigation. Lifting it is the action that could put a
suspected-compromised credential back into service, so it needs an owner, a
written reason (`min_length=8`), and a fresh proof of presence — and the
customer-facing `enable` toggle **refuses** while the flag is set
(`403 policy_denied / emergency_disabled`, "This account was disabled by the
platform operator").

`delete_account` emits `SERVICE_ACCOUNT_DISABLED` with `deleted: true` before
removing the row, so the audit trail keeps the history even though the identity
is gone.

---

## 5. Every request re-checks the world

A service account credential is resolved by
`api_keys.lookup_credential` and turned into a principal by
`principal_from_lookup`, which refuses if:

1. the row is missing, the secret does not hash-match, the credential is
   revoked, or it has expired — all with the **same** `Invalid credential`
   error, so a caller cannot tell "revoked" from "unknown";
2. the account is disabled, emergency-disabled, or expired;
3. the tenant is missing or inactive;
4. the tenant policy has switched service accounts off
   (`evaluate_api_credential` → `service_accounts_disabled`) — immediate, per
   request, not at the next expiry;
5. **the owner is missing, inactive, or in another tenant** — a credential
   never outlives the authority behind it. An account whose creator was deleted
   has no human to attribute actions to and no account to re-check, so it is
   refused until an operator reassigns the owner.

The principal carries `service_account_id`, `credential_id` and the account's
scopes; the tenant comes from the row, never from the request.

---

## 6. What a service account may never do

- It is **not a tenant admin**. It holds the scopes on its row intersected with
  what its owner still holds; it never inherits OWNER.
- It cannot reach anything behind `require_human_session`, and every
  `assert_privileged` action answers `machine_credential_cannot_reauth`: a
  machine cannot prove a human is present, so it may not change the tenant's SSO,
  reset anybody's MFA, lift its own emergency stop, or issue credentials to
  itself.
- It cannot create scopes for itself: issuing a credential or a key is an
  administrator action, checked against *that administrator's* permissions.
- It cannot cross tenants: every read and write is keyed on the row's
  `tenant_id`, and the authorization matrix asserts that a second tenant sees
  nothing of the first.

---

## 7. Audited lifecycle

`SERVICE_ACCOUNT_CREATED`, `SERVICE_ACCOUNT_UPDATED`, `SERVICE_ACCOUNT_ENABLED`,
`SERVICE_ACCOUNT_DISABLED` (with `emergency` / `deleted` / reason),
`SERVICE_ACCOUNT_CREDENTIAL_CREATED`, `_ROTATED`, `_REVOKED`, plus
`API_KEY_CREATED` / `_ROTATED` / `_REVOKED` for the key family. Event details
carry ids, prefixes, scope lists and reasons — never a secret.

---

## 8. Operating notes

- **Give the account an owner who will still be here.** The owner's activity
  status is checked on every request; a service account whose owner leaves and
  is deactivated stops working. That is deliberate, and it is the reason a
  rotation runbook should include an owner review.
- **Prefer a credential per consumer**, labelled with the thing that uses it.
  Revoking one integration should not break another.
- **Expiry is a feature.** Set `expires_in_days` on anything created for a
  migration or a pilot: an expired credential fails closed with no action from
  anyone.
- **A leaked credential** is: `disable` (or `emergency-disable` if you are not
  sure the tenant is the only problem) → rotate the credential → read
  `audit_logs` for what the account did while the credential was live.
