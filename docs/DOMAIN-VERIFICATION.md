# VoxDesk — Enterprise domain verification

Proving a tenant owns a domain, and deciding — deliberately, in two separate
steps — what that ownership does to the way people sign in. Implementation:
`app/auth/identity/domains.py`, routes in `app/api/domain_routes.py`.

---

## 1. The two-key design

A single setting that both proves ownership and starts refusing password logins
is a setting that will eventually be flipped by accident, and the failure mode is
a whole company unable to sign in. So there are two keys:

| Key | Set by | Effect |
| --- | --- | --- |
| **verification** | DNS (`verified_at` is written) | the domain is *proven*; it may route users to an IdP |
| **enforcement** | an administrator, explicitly | `off` / `warn` / `require_sso`; only a **verified** domain may be set to anything but `off` |

`verified_at is None` means a claim, and a claim **imposes nothing** — the rule
is enforced in `policies.evaluate_domain_policy`, not only in the domain
service, so no other code path can accidentally treat a claim as proof.

---

## 2. Endpoints

| Method | Path | Guard | Purpose |
| --- | --- | --- | --- |
| `GET` | `/api/domains` | `identity:read` | list claims, their status and enforcement |
| `POST` | `/api/domains` | `identity:write` + fresh proof | claim a domain; opens the first challenge |
| `GET` | `/api/domains/{id}` | `identity:read` | one claim |
| `POST` | `/api/domains/{id}/challenge` | `identity:write` + fresh proof | new token; supersedes any open challenge |
| `POST` | `/api/domains/{id}/verify` | `identity:write` + fresh proof | read DNS and settle the challenge |
| `PATCH` | `/api/domains/{id}` | `identity:write` + fresh proof | set enforcement / block password login / bind an SSO connection |
| `DELETE` | `/api/domains/{id}` | `identity:write` + fresh proof | remove the claim |

Every route scopes by the caller's `tenant_id`; a domain id from another tenant
answers 404 exactly like one that does not exist.

---

## 3. Claiming, and the name that gets compared

`normalize_domain` lowercases, strips a scheme and path, a leading `@` and a
trailing dot. `https://Acme.example.com/` and `acme.example.com.` are the same
claim — treating them as different would let one tenant take a domain another
tenant already proved. `validate_domain` then requires a label sequence of at
least two labels (1–63 characters each, no leading or trailing hyphen) and
refuses the reserved names:

- `voxdesk.local`, `localhost`, `invalid`, `example.com`, `example.org` and
  anything ending in them.

The `domain` column is **globally unique**. A second tenant trying to claim the
same name gets a clear conflict ("already claimed by another workspace") rather
than a 500 from the constraint — who owns a domain is public information, so
there is nothing to hide and hiding it would produce an unexplainable failure.

Claiming a domain emits `DOMAIN_ADDED` and immediately opens a challenge.

---

## 4. The challenge

| Property | Value |
| --- | --- |
| record name | `voxdesk-verify.<domain>` (`DOMAIN_VERIFICATION_PREFIX`) |
| record type | `TXT` |
| record value | `voxdesk-verify=<token>` |
| token | 3 groups × 8 characters, from `identity_tokens.new_human_code` — sized to be typed by a human from a screen |
| stored as | **SHA-256 digest only**; the token is returned once, when it is issued |
| lifetime | `DOMAIN_VERIFICATION_TTL_HOURS` (default 72) |
| attempts | `MAX_ATTEMPTS = 20`, then the challenge fails and must be re-issued |

Re-issuing supersedes every open challenge for the domain, so an old TXT record
left in a zone cannot be used to re-verify later. The record value is prefixed
so an operator reading their zone file can see what the record is for, and so a
lookup cannot be satisfied by an unrelated SPF or site-verification record.

Because the token is not recoverable from the row, the comparison goes the other
way: `_record_value_from_hash_check` hashes each published value and compares
it to the stored digest, matching both the `prefix=value` form and the bare
value, so a provider that strips the separator still works.

**DNS is read over DNS-over-HTTPS** (`DOMAIN_DNS_RESOLVER_URL`, default
`https://cloudflare-dns.com/dns-query`) with `httpx`, which is already a
dependency: nothing new to install, and tests replace one function.

| Answer | Treated as |
| --- | --- |
| `Status: 3` (NXDOMAIN) / non-zero | **verification failure** — "no such record", the operator's action is still to publish it |
| resolver unreachable, HTTP != 200, unreadable body | `DomainDNSUnavailable` — recorded against the challenge |
| no TXT answer containing the value | failure, with the resolver's answer reported back |

The distinction is deliberate: "DNS is down" must never be presented to the
operator as "your record is wrong", because the two need different actions.

A failed check is **recorded, not raised**: `POST …/verify` returns
`{verified: false, status, detail, records_seen}` so the operator sees the
attempt count and exactly what the resolver returned. A 500 would lose both.

---

## 5. Success

On a match:

- `verified_at` and `last_evidence_at` are set on the domain row, the challenge
  becomes `verified`, `last_error` is cleared and `failed_attempts` resets;
- `DOMAIN_VERIFIED` is audited with the domain, the record name and the number
  of attempts it took.

An expired challenge is marked `expired`; a challenge over the attempt ceiling
is marked `failed` with `last_error = "too_many_attempts"`. Both require a new
token, and both are distinct states so the UI can say which happened.

---

## 6. Enforcement

`set_enforcement` accepts `off` / `warn` / `require_sso` and **refuses to set
anything but `off` while the domain is unverified** — an unverified domain
imposing policy is exactly how a typo locks out a workforce.

| `enforcement` | Meaning |
| --- | --- |
| `off` | the domain is verified, nothing changes for sign-in |
| `warn` | a password login for that domain is allowed, and the attempt is recorded so an operator can see who is still using a password |
| `require_sso` | password logins for that domain are refused — **only when the domain is verified**; the user is told to sign in with single sign-on, and the refusal is audited |

Setting enforcement **never changes verification**, and every change is audited
(`DOMAIN_ENFORCEMENT_CHANGED`) with the previous value, the new value and
whether password login is blocked. `block_password_login` is a separate,
explicitly recorded flag. An optional `sso_connection_id` binds the domain to
one connection, and a connection from another tenant is refused.

`remove_domain` audits `DOMAIN_REMOVED` with `was_verified` and the enforcement
that was in force — "the domain disappeared and logins changed" is otherwise
impossible to reconstruct from an audit trail — and deleting an enforcing
domain restores password login.

---

## 7. What a verified domain does *not* do

It does not by itself send anyone anywhere. Routing a human to an identity
provider is done by `connection_for_email`, which reads **only verified**
domains: a row somebody merely typed into the dashboard is not evidence of
ownership, and sending a user to an IdP chosen by whoever typed the name is the
classic pre-hijack. The same reasoning means:

- an **un**verified claim never routes a login and never refuses a password
  login (this is asserted by a regression test: an unverified claim with
  `enforcement = require_sso` still reports `sso_required is False`);
- if two tenants somehow verified the same domain, the login is **refused
  rather than guessed**;
- the enforcement policy is only consulted through
  `policies.evaluate_domain_policy`, so a new login path cannot forget it.

---

## 8. Configuration

| Setting | Default | Meaning |
| --- | --- | --- |
| `DOMAIN_VERIFICATION_PREFIX` | `voxdesk-verify` | the label and the value prefix |
| `DOMAIN_VERIFICATION_TTL_HOURS` | `72` | challenge lifetime |
| `DOMAIN_DNS_RESOLVER_URL` | `https://cloudflare-dns.com/dns-query` | DNS-over-HTTPS endpoint; empty disables lookups with a clear error |

---

## 9. Operating notes

1. Claim the domain → publish the TXT record shown once in the dashboard →
   press verify. DNS takes a few minutes to become visible; the error message
   says so.
2. Roll out SSO with `off`, watch `sso_login_attempts` until real users are
   succeeding, then `warn`, then `require_sso`. That ordering is what the three
   values are for.
3. Do not remove the TXT record after verifying: the row keeps
   `last_evidence_at`, and re-verification after a dispute is a single button
   rather than a new claim.
