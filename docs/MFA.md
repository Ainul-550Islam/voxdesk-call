# VoxDesk — Multi-Factor Authentication

TOTP second factors, recovery codes, challenge lockout, and the fresh-proof rule
that guards every dangerous action. Implementation: `app/auth/identity/mfa.py`
(service), `app/auth/identity/totp.py` (arithmetic), `app/api/mfa_routes.py`
(HTTP), `app/api/auth_routes.py` (`/auth/mfa/verify`, the login second step).

---

## 1. What a factor is

A factor is one row in `mfa_factors`: a sealed TOTP seed, the parameters it was
created with (`digits`, `period_seconds`), its status (`pending` → `active`),
when it was confirmed and last used, and **the last time step it consumed**.

- The seed is sealed with `identity_secrets.encrypt_text` under purpose
  `totp` and the tenant id, so the ciphertext is useless in another tenant's
  row. It is stored once, in `secret_encrypted`, alongside `secret_key_id`
  naming the envelope key that sealed it.
- The seed is returned to the client **exactly once**, by
  `POST /api/mfa/enroll`, together with an `otpauth://` provisioning URI.
  There is no endpoint that returns it again — not even for an administrator.
- `POST /api/mfa/users/{user_id}/status` tells an administrator *whether* a
  colleague has a factor, never what it is.

TOTP follows RFC 6238 over RFC 4226 HMAC-SHA1. `totp.verify` returns the
matching **time step** rather than a boolean, which is what makes replay
protection possible: the step is stored on the factor and passed back as
`min_step` on the next attempt, so a code cannot be used twice inside its own
window. The RFC 4226 Appendix D and RFC 6238 Appendix B vectors (seed
`12345678901234567890`) are asserted in `tests/auth/test_totp.py`.

---

## 2. Enrollment

```
POST /api/mfa/enroll            -> 201 { factor_id, secret, provisioning_uri, digits, period_seconds }
POST /api/mfa/enroll/confirm    <- { code }
                                -> 200 { recovery_codes: [...], warning }
GET  /api/mfa/status            -> whether enrolled / pending / how many codes remain
```

1. **`/enroll`** creates a *pending* factor. A pending factor does not gate
   login. If one already exists it is replaced, so a user who lost the QR code
   can start over without an administrator.
2. **`/enroll/confirm`** verifies a live code (window `MFA_TOTP_WINDOW`), flips
   the factor to `active`, records the consumed step, and issues the first set
   of recovery codes. A wrong code fails the enrollment attempt and is
   rate-limited like any other verification.
3. Both steps require a human session (`require_human_session`) and are
   self-service: an administrator cannot enroll a factor *for* somebody else,
   because that would mean an administrator holding a colleague's seed.

Enrollment is gated by the deployment switch `MFA_ENABLED`; with it off the
routes answer 404 and `mfa.ensure_enabled()` refuses service calls.

---

## 3. Recovery codes

- Generated from the alphabet `ABCDEFGHJKMNPQRSTUVWXYZ23456789` — no `0/O/1/I/L`,
  because these get written on paper and read back by a human.
- Stored **hashed**, one row per code, single-use, with `used_at` recorded.
  `MFA_RECOVERY_CODE_USED` is emitted when one is consumed.
- Returned only from `/enroll/confirm` (the first set) and
  `POST /api/mfa/recovery-codes/regenerate` (a replacement set). Regeneration
  requires a fresh proof of presence and invalidates the previous set, because
  "old codes keep working after a regeneration" is how a stolen paper backup
  stays useful forever.
- `GET /api/mfa/status` reports `recovery_codes_remaining`,
  `recovery_codes_total` and `recovery_codes_low`, so the dashboard can prompt
  before the last code is used.
- Using a recovery code satisfies the second factor for that login only. It is
  never a password replacement, and it is never accepted where a TOTP code is
  required for a *privileged* action unless the action's check is the same
  freshness check (see §5).

---

## 4. Login and challenges

A user who must present a factor gets a **202, not a 200** from
`POST /auth/login`:

```json
{ "challenge": "vdmfa_…", "expires_in": 300 }
```

The password was correct and no tokens exist yet. The client completes the
login with `POST /auth/mfa/verify { challenge, code }`, which returns the same
token pair as a normal login and sets the session's `mfa_verified` flag and
`mfa_verified_at`.

Challenge properties:

- **Single use.** A consumed challenge cannot be replayed; the stored step
  prevents the same TOTP code being accepted twice.
- **Short lived.** `MFA_CHALLENGE_TTL_SECONDS` (default 300). A half-finished
  login is not resumable an hour later.
- **Failure-counted.** `MFA_CHALLENGE_MAX_FAILURES` wrong codes (default 5)
  burn the challenge and set `locked_until` for
  `MFA_CHALLENGE_LOCKOUT_MINUTES` (default 15). A locked challenge answers
  **429** with `Retry-After`, and emits `MFA_CHALLENGE_LOCKED`.
- **Rate-limited per user.** `MFA_MAX_VERIFICATIONS` attempts per
  `MFA_VERIFICATION_WINDOW_SECONDS` (default 10 / 300), so an attacker cannot
  walk a six-digit space by asking for fresh challenges.
- **Bound to a session where one exists.** A challenge created by
  `POST /api/mfa/challenge` for a reauthentication is tied to the caller's
  session row and dies with it, so a token captured in one browser cannot
  elevate another. The binding is checked at `POST /api/mfa/verify` *before* the
  code is resolved, so a request from the wrong session is refused without
  spending the challenge the rightful one is about to answer.
- **One live challenge per user.** Asking for a new one consumes any open
  challenge, whatever its purpose: an attempt cannot fan out into several
  challenges and dilute the per-challenge failure counter. Asking again is how
  a user recovers from one they abandoned.
- **Purpose-checked.** A `login` challenge cannot be spent as a step-up, which
  would otherwise let a half-finished sign-in raise the assurance of a different
  session.

Every failure emits `MFA_FAILED` with the *reason*, never the code that was
offered. Successes emit `MFA_VERIFIED`.

---

## 5. Fresh proof of presence (step-up)

Dangerous actions do not merely require "MFA on the account". They require a
**fresh** proof, evaluated by `evaluate_privileged_action`:

| Session state | Decision | Route behaviour |
| --- | --- | --- |
| `privileged_reauth_required` is off | `allowed` | proceeds |
| machine credential (API key, service account, SCIM) | refused `machine_credential_cannot_reauth` | 403 |
| factor enrolled, `mfa_verified_at` inside the window | `allowed` | proceeds |
| factor enrolled, proof stale or absent | `allowed=True`, `requires_mfa=True` | **428** with `detail.code = "reauth_required"` |
| no factor enrolled, `password_confirmed_at` inside the window | `allowed` | proceeds |
| no factor enrolled, proof stale or absent | `allowed=True`, `requires_password=True` | **428** |

Two consequences are deliberate:

- **428, not 403.** The session is fine; it needs a fresh proof. The client
  responds with `POST /api/identity/reauth { password | code }` or
  `POST /api/mfa/verify { challenge, code }`, and the session's timestamps are
  refreshed. The dashboard asks for a challenge first and sends it with the
  code, so the verification is single-use and counted against the per-challenge
  ceiling; a bare `{ code }` is still accepted for clients written before the
  challenge existed, and is rate limited per user instead.
- **No factor is not a lockout.** An operator who never enrolled a factor is
  asked for their password rather than being told to present one. Locking an
  administrator out of their own security settings is a self-inflicted outage.

The freshness window is `privileged_reauth_minutes` per tenant, defaulting to
`MFA_FRESH_MINUTES` (15). Routes that call this guard include MFA disable and
admin reset, recovery-code regeneration, SSO connection create/update/status/
certificate/mapping changes, domain enforcement changes, API key and service
account credential revocation.

---

## 6. Disabling a factor

```
POST /api/mfa/disable                  # self-service, fresh proof required
POST /api/mfa/users/{user_id}/reset    # identity:write + fresh proof, 204
```

- **Self-service disable** requires a fresh proof of presence: an attacker
  holding a borrowed, unlocked browser must not be able to switch off the
  control that would otherwise stop them. Every *other* session dies with the
  factor (`revoked_reason = "mfa_disabled"`) — the factor was what protected
  them.
- **Administrative reset** is the answer to "I lost my phone and my codes". It
  is the most abusable endpoint in the feature, so it is the most guarded:
  `identity:write`, a fresh proof of presence on the administrator's own
  session, and every one of the target's sessions is revoked
  (`revoked_reason = "mfa_reset_by_admin"`). An administrator cannot use it to
  reset their own factor — they are told to use the self-service flow, so the
  audit trail never hides a self-disable behind a user id.
- Both emit `MFA_DISABLED`, with actor and target in the audit row.

---

## 7. Configuration

| Setting | Default | Effect |
| --- | --- | --- |
| `MFA_ENABLED` | `true` | master switch (404 when off) |
| `MFA_ISSUER` | `VoxDesk` | label in the authenticator app |
| `MFA_TOTP_DIGITS` | `6` | 6 or 8 (validated at boot) |
| `MFA_TOTP_PERIOD_SECONDS` | `30` | must be positive |
| `MFA_TOTP_WINDOW` | `1` | steps accepted either side of now |
| `MFA_RECOVERY_CODE_COUNT` | `10` | codes per regeneration |
| `MFA_MAX_VERIFICATIONS` / `MFA_VERIFICATION_WINDOW_SECONDS` | `10` / `300` | per-user rate limit |
| `MFA_CHALLENGE_MAX_FAILURES` | `5` | wrong codes before the challenge is burned |
| `MFA_CHALLENGE_LOCKOUT_MINUTES` | `15` | lockout after the burn |
| `MFA_CHALLENGE_TTL_SECONDS` | `300` | challenge lifetime |
| `MFA_FRESH_MINUTES` | `15` | default privileged-action freshness |
| `IDENTITY_ENCRYPTION_KEYS` | falls back to `CRM_ENCRYPTION_KEYS` | seeds cannot be sealed without it; production boot refuses SSO/MFA with no ring |

Per-tenant, `identity_policies` carries `mfa_required`,
`mfa_required_for_admins`, `privileged_reauth_required` and
`privileged_reauth_minutes`; `users.mfa_required` carries the per-user override
that wins in both directions (see `ENTERPRISE-IDENTITY.md` §4.3).

---

## 8. Tests

| File | Proves |
| --- | --- |
| `tests/auth/test_totp.py` | RFC 4226/6238 vectors, window, `min_step` replay refusal, period guard |
| `tests/auth/test_mfa.py` | ciphertext-only storage, single-use challenge, failure counters, lockout, recovery-code single-use, fresh-proof disable |
| `tests/security/test_session_invalidation.py` | the HTTP step-up pair: `POST /api/mfa/challenge` then `POST /api/mfa/verify { challenge, code }` marks *the calling* session verified; a wrong code, a login-purpose challenge, and a challenge minted on another session are all refused without elevating anything and without spending the rightful caller's challenge |
| `tests/auth/test_sessions.py` | `mfa_verified` / `mfa_verified_at` on the session row |
| `tests/security/test_session_invalidation.py` | disable and admin reset kill the right sessions with the right reason |
| `tests/security/test_authorization_matrix.py` | who may reach each MFA route, machine credentials refused, 428 semantics |
