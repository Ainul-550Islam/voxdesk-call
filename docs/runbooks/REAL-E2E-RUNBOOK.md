# Runbook — one real end-to-end voice call (Twilio media stream)

Operator procedure for a **single, genuine, human-dialled** call through the
whole pipeline. `app/telephony/e2e_guard.py` names this file as the operator
runbook; the long-form Step 5 rationale, the checkpoint table and the result
record live in `docs/REAL-E2E-RUNBOOK.md` and
`docs/REAL-E2E-RESULT-RECORD.md`, which this runbook mirrors.

> **Status banner: `REAL TELEPHONY E2E: NOT AUTOMATICALLY EXECUTED`.**
> Nothing in the codebase places this call, and no automated test dials a real
> number. Keep the banner until a human performed the run **and** completed
> `docs/REAL-E2E-RESULT-RECORD.md` as evidence.

**Who runs this:** an operator with access to the staging shell, the Twilio
console and a phone that can dial internationally. **Time:** ~20 min setup on
first use, ~10 min per run.

---

## 0. The rule that makes this runbook safe

The only sanctioned way to exercise a real call is `e2e_guard.check_inbound`,
called by `/telephony/voice` (`app/telephony/twilio_handler.py`). It is a pure
classifier: it does not dial, does not mutate, does not call Twilio.

| Guard state | Behaviour |
| --- | --- |
| `E2E_ENABLED=false` | **No-op.** Every inbound call is ordinary traffic; classification returns `None` and the product is unchanged. |
| `E2E_ENABLED=true`, `APP_ENV=production` | **Refused**, logged `call.e2e_rejected_production`. The app also refuses to boot in production with the flag set (`Settings.validate_security()`). |
| `E2E_ENABLED=true`, non-production, call not matching every rule below | **Refused** at the door with a TwiML hangup — *before* a `Call` row, session, media stream, usage event or audit record can exist. |
| `E2E_ENABLED=true`, non-production, call matching every rule | **Allowed**, logged `call.e2e_allowed`. |

Armed-mode rules, all five required:

1. `settings.is_production` is false;
2. dialled `To` **is** `E2E_TEST_NUMBER` (normalised comparison, not string equality);
3. caller `From` is in `E2E_ALLOWED_CALLERS` (normalised the same way caller ID arrives);
4. the resolved tenant exists and has `is_test_tenant = true`;
5. that tenant's `twilio_number` equals `E2E_TEST_NUMBER`.

Armed but misconfigured (missing `E2E_TEST_NUMBER` or an empty allowlist) is
**fail-closed**: the voice webhook answers `503 E2E configuration error` rather
than answering the call. Rejection messages are secret-free by construction —
they never contain a phone number, tenant name or configuration value.

## 1. Prerequisites

```bash
# 1. A non-production deployment that Twilio can reach, at a known host.
echo "$STAGING_HOST"                 # e.g. staging.voxdesk.example
# 2. A dedicated Twilio test number, and a phone you physically hold.
# 3. Database access to create/mark the test tenant.
# 4. The app logs, tailed on the host running the API.
```

Confirm the app is healthy and *not* production before touching anything:

```bash
curl -s "$STAGING_HOST/health"                 # -> {"status":"ok"}
```

## 2. One-time setup

### 2.1 Test tenant

The tenant must be unmistakably a test tenant — it is what rule 4 checks.

```sql
INSERT INTO tenants (id, name, industry, twilio_number, is_test_tenant, is_active)
VALUES (gen_random_uuid(), 'E2E TEST — do not bill', 'test',
        '<E2E_TEST_NUMBER>', TRUE, TRUE);
```

Point its integrations at throwaway targets only: a sandbox calendar for
availability/booking, a request bin for `crm_webhook_url`, knowledge/AI presets
you do not mind overwriting. Never a client CRM, never a client calendar.

### 2.2 Environment (non-production only)

```
APP_ENV=staging                 # development or staging — never "production"
E2E_ENABLED=true
E2E_TEST_NUMBER=<dedicated Twilio test number, E.164>
E2E_ALLOWED_CALLERS=<operator phone E.164>[,<second operator E.164>]
```

Restart the API. Then prove the guard is armed *before* dialling — start the
log tail in a second terminal:

```bash
tail -f <api log> | grep -E "call\.incoming|call\.e2e_|turn|tool|finalized|call\.crashed"
```

### 2.3 Webhook

Twilio console → the test number → **Voice → A call comes in**:

```
POST https://<staging host>/telephony/voice
```

Leave signature validation **on** (`/telephony/voice` verifies it and returns
`403 forbidden` otherwise).

## 3. Negative checks first (2 minutes, do these every run)

| # | Action | Required observation |
| --- | --- | --- |
| N1 | From a phone **not** on the allowlist, dial the test number | TwiML says "This number is in test mode and this caller is not authorized. Goodbye."; log shows `call.e2e_rejected reason=caller_not_allowlisted`, then `call.e2e_rejected_hangup`; **no** `calls` row is created. |
| N2 | Dial any other number that maps to a normal tenant | Refused (armed mode is test-only): `call.e2e_rejected reason=dialed_number_not_test_number`; no call row. |
| N3 | Boot check: `APP_ENV=production` with `E2E_ENABLED=true` | The app refuses to start (`validate_security()`). |

If any of the three does not behave as stated, **stop** and fix the
configuration: the positive run below is only meaningful behind these gates.

## 4. The live call (human dials — never automate this step)

1. Note the wall-clock start time and the operator phone used.
2. Dial `E2E_TEST_NUMBER` from the allowlisted phone.
3. Confirm checkpoint 3 immediately: `call.e2e_allowed` with
   `test_tenant=<name>`. If it is absent while the call is being answered, hang
   up — you are not testing what you think you are testing.
4. Talk through the checkpoints below; keep notes as you go (the notes go into
   the result record, not into a private scratch file).
5. Hang up, then wait for the status webhook and post-call processing to settle
   (a few seconds) before querying the database.

### Checkpoints (identical numbering to `docs/REAL-E2E-RESULT-RECORD.md`)

| # | Checkpoint | Evidence to capture |
| --- | --- | --- |
| 1 | Twilio webhook | `/telephony/voice` POST accepted (signature valid), `call.incoming` logged |
| 2 | Tenant resolution | Log shows the **test tenant's** name/id |
| 3 | E2E guard | `call.e2e_allowed` present |
| 4 | Call/session creation | exactly one `calls` row for the `CallSid` |
| 5 | Media stream | `/telephony/ws` handshake completed |
| 6 | STT/TTS/LLM init | no provider error at startup of the session |
| 7 | System prompt | greeting + AI disclosure spoken |
| 8 | First utterance | your speech transcribed into the transcript rows |
| 9 | LLM response | one turn generated and spoken back |
| 10 | Tool call/result | e.g. `check_availability` ran and its result shaped the reply |
| 11 | TTS | voice/language/speed as configured |
| 12 | Barge-in | speaking over the agent stops the current utterance |
| 13 | Multi-turn | ≥ 2 exchanges, no hang |
| 14 | Human transfer (if in scope) | ends in `TRANSFERRED` (or `FAILED`) exactly once |
| 15 | Booking (if in scope) | appointment created/cancelled on the **test** calendar |
| 16 | Termination | exactly one terminal state |
| 17 | Turn persistence | all turns persisted, no duplicates |
| 18 | Usage accounting | one usage event per idempotency key |
| 19 | Billing/metering | no double charge; usage isolated to the test tenant |
| 20 | Audit logging | tenant-scoped audit rows present |
| 21 | Post-call processing | CRM hook / cleanup ran once, idempotently |
| 22 | Observability | the correlation id appears on every line of the call |

### Verification queries (adjust to your schema)

```sql
-- 4 + 16: exactly one row, exactly one terminal state
SELECT id, status, started_at, ended_at FROM calls WHERE call_sid = '<CallSid>';

-- 17: turns persisted, and the count matches the exchanges you actually had
SELECT speaker, count(*) FROM turns WHERE call_id = '<call id>' GROUP BY speaker;

-- 20: audit rows are tenant-scoped
SELECT action, count(*) FROM audit_logs
 WHERE tenant_id = '<test tenant id>' GROUP BY action;

-- 19: the run's usage landed on the test tenant only, once per key
SELECT metric, count(*), sum(quantity) FROM usage_events
 WHERE tenant_id = '<test tenant id>' GROUP BY metric;
```

Any `FAIL` is a finding: record the checkpoint number, what you observed, and
the log line — do not re-run until it is understood, because a second attempt
over a half-broken state produces two confusing datasets.

## 5. Record the result

Open `docs/REAL-E2E-RESULT-RECORD.md` and fill in: status, operator, date,
`APP_ENV`, test tenant id/name, `E2E_TEST_NUMBER`, operator caller (redacted),
`CallSid`, `StreamSid`, evidence link, then every checkpoint and every negative
check as `PASS` / `FAIL` / `BLOCKED` / `NOT_TESTED` with a note. Redact numbers
(`+1*******34`) and never paste credentials.

The final report line stays `REAL TELEPHONY E2E: NOT AUTOMATICALLY EXECUTED`
unless that record is complete and committed.

## 6. Teardown (do not skip — this is how the guard stays safe)

```bash
# 1. Disarm and restart the API.
E2E_ENABLED=false

# 2. Take the test tenant out of service so it can never answer traffic.
UPDATE tenants SET is_active = FALSE WHERE id = '<test tenant id>';

# 3. Remove the temporary webhook override from the test Twilio number.
# 4. Confirm docs/REAL-E2E-RESULT-RECORD.md is filled in and dated.
```

## 7. Emergency stop

1. **Hang up** the operator phone.
2. Set `E2E_ENABLED=false` and restart the API — the guard is the only thing
   admitting test traffic; disarming restores normal behaviour immediately.
3. Optionally set the test tenant `is_active = FALSE` as an independent second
   switch.
4. Preserve the log tail for the call before it rotates; it is the evidence.

## 8. What this runbook is not

* Not a smoke test for production: production cannot be armed at all.
* Not automatable in CI: `tests/test_e2e_guard.py` covers the guard's
  classification logic with synthetic webhooks, and that is the automated
  half — nothing in `tests/` dials a number, by design.
* Not a substitute for the load and failure-injection runs
  (`docs/LOAD-TESTING.md`, `docs/FAILURE-INJECTION.md`), which generate traffic
  without a human.
