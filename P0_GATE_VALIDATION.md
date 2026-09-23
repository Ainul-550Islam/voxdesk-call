# P0 gate — validation record

Everything below was executed on the tree in this repository (branch `main`,
based on upstream `13ea4e0` + the Step 17 batch-02/05 work). Nothing in this
document is inferred: each row is an exact command with its exact result, and
rows that could **not** be executed say so and name the blocker.

Two rules were held throughout:

* no existing behaviour was replaced with a placeholder, and no security check
  was weakened to make a test pass;
* a toolchain that is missing is reported as missing — never as a pass.

---

## 1. Toolchains

| Tool | Version | Location |
| --- | --- | --- |
| Python | 3.13.14 | `/usr/local/bin/python3` |
| Node / npm | 20.20.2 / 10.8.2 | `/usr/local/bin` |
| Go | go1.27.1 linux/amd64 | `/usr/local/toolchains/go-sdk/go` |
| Rust (rustc / cargo) | 1.90.0 (1159e78c0 2025-09-14) | `/usr/local/cargo/bin` |
| rustfmt / clippy | 1.8.0-stable / clippy 0.1.90 | added via `rustup component add rustfmt clippy` |
| PostgreSQL | 17.11 (Debian) | `/usr/lib/postgresql/17/bin` |
| libclang | LLVM 19 | `/usr/lib/llvm-19/lib/libclang.so` (needed by wolfssl-sys) |
| ruff / mypy | 0.8.4 / 2.3.1 | pip |
| gcc / g++ | 14.2.0 | system |

CI's Postgres service image is `postgres:16-alpine`; this sandbox has 17.11, so
the migration round trip and the suite were run on **17.11 — one major version
newer than CI**. Recorded here, not hidden.

---

## 2. Results by phase

| # | Phase | Command | Result |
| --- | --- | --- | --- |
| 1 | TextAgent / LLM-factory contract | `python3 -m pytest tests/test_text_agent.py tests/test_llm_factory.py tests/test_channels.py tests/test_correlation_and_redaction.py -q -p no:randomly` | **151 passed** (28 + 76 across three files + 12 + new) |
| 2 | Broad handlers hiding failures | `python3 scripts/audit_except_handlers.py app` | 72 broad handlers; **29 → 18 unobserved**, see §4 |
| 3 | Python validation | `python3 -m compileall -q app scripts tests` → exit 0; `ruff check app scripts tests` → **All checks passed**; `mypy app` → 147 errors in 34 files (**pre-existing baseline**, 0 in touched files) | pass |
| 4 | P0-path test coverage | new `tests/test_text_agent.py`, `tests/test_error_visibility.py`; extended `tests/test_channels.py`, `tests/test_llm_factory.py`, `tests/test_correlation_and_redaction.py`, `tests/test_billing_voice.py` | **+58 tests** (2505 → 2563) |
| 5 | Go toolchain + gate | `gofmt -l .` (clean) · `go vet ./...` (0) · `go vet -tags it ./...` (0) · `go test -race -count=1 ./...` (exit 0) · `go test -tags it -count=1 ./...` (exit 0) | pass |
| 6 | Go realtime P0 regressions | existing 181 gateway tests incl. `internal/auth` (14 token tests: algo pinning, tamper, issuer/audience, nbf/exp, refresh-type), `tests/integration`, `tests/protocol` | pass, no redesign |
| 7 | Rust toolchain + gate | `cargo fmt --all -- --check` (clean) · `cargo check --workspace` (0) · `cargo test --workspace` (**122 passed / 0 failed**, 3 consecutive runs) · `cargo clippy --workspace --all-targets --all-features -- -D warnings` (0) | pass |
| 8 | Rust DTLS/SRTP regression | `cargo test -p dtls -p engine` → CM-only server, GCM-only refusal, GCM-128 pin, default GCM-256, RFC 5764 split, plus a new `srtp_profiles` contract test; vendored crate's own upstream suite: `cargo test --lib` in `vendor/dimpl` → **301 passed / 0 failed** | pass |
| 9 | Next.js/TS validation | `dashboard-next`: `npm ci` (0) · `npx vitest run` (**16 passed**) · `npx tsc --noEmit` (0) · `next build` (0). No `any`, no `@ts-ignore` anywhere; the one pre-existing `skipLibCheck: true` is the Next.js default and was left as-is (removing it did not finish in 15 min in this sandbox — evidence, not preference) | pass |
| 10 | Legacy frontend validation | `dashboard`: `npm ci` (0) · `npm test` (**375 passed / 15 files**) · `npm run build` (0) · `npm audit --omit=dev --audit-level=high` (**0 vulnerabilities**) | pass |
| 11 | C/C++ validation | no C/C++ targets in the repo (the only native code is the vendored dimpl, which is Rust); the Docker image build is the only compile path — `docker` is unavailable in this sandbox | **not applicable / env-blocked** |
| 12 | CI consistency | see §5 for the two stage gaps found (Rust workspace gates, `dashboard-next`) | reported, not silently skipped |
| 13 | DB/migration safety | `alembic upgrade head` → `downgrade base` → `upgrade head` against **real PostgreSQL** (CI's exact sequence) | exit 0 / 0 / 0, head `0012_enterprise_persistence`, 33 tables |
| 14 | Security regression | no guard touched: token/API-key/WS-auth/webhook-signature/SSRF/egress/rate-limit/replay code paths unchanged except for *added* observability; secret redaction asserted by tests | pass |
| 15–16 | Verification matrix + report | this document | — |

### Full suites

| Suite | Command | Result |
| --- | --- | --- |
| Python, default SQLite engine | `python3 -m pytest tests -q -p no:randomly` | **2563 passed, 43 skipped** in 146.21 s |
| Python, CI parity on real Postgres | `DATABASE_URL=postgresql+asyncpg://voxdesk:voxdesk-ci@localhost:5432/voxdesk_ci python3 -m pytest -q -m "not real_provider"` | **2563 passed, 30 skipped, 13 deselected** in 143.58 s (final tree) |
| Migration round trip | `alembic upgrade head && alembic downgrade base && alembic upgrade head` | exit 0, 0, 0 |

---

## 3. P0 blockers found and fixed

1. **`LLMChoice` unpacked as a tuple** — `llm_factory.resolve()` returns a
   frozen `LLMChoice`; `TextAgent.__init__` unpacked it (`provider, model,
   latency = ...`), so *every* construction raised `TypeError` and every SMS /
   webhook turn answered with the apology text. Fixed by consuming the
   attributes (`select_for_use`) with explicit precedence.
2. **Provider errors were untyped** — SDK failures surfaced as raw SDK
   exceptions, so retry/category logic and alerting had nothing to key on.
   Added `classify_provider_exception` (HTTP status → typed error; class-name
   fragments; never copies the SDK message, so nothing leaks).
3. **Construction errors happened outside the request's error handling** —
   `messaging.py` built the agent before the `try`, so a misconfiguration
   produced a 500 from the webhook instead of a graceful 200 answer. Moved
   inside the `try` and added the provider-vs-contract log split.
4. **A failed turn left no trace** — now `thread.llm_used` is only set on
   success, the failed turn is persisted, and `channel.agent_*` records carry
   provider / category / retryable / tenant / request id.
5. **`vendor/dimpl` was incomplete** — 70 of 148 files were missing
   (`src/lib.rs` among them), so the Rust workspace could not compile at all.
   Restored byte-for-byte from crates.io `dimpl 0.7.3` and verified with
   `diff -r`; the repo's own documented carve-out is preserved.
6. **The documented `ConfigBuilder::srtp_profiles` knob did not exist** — the
   workspace comment and `PROMPT2-DESIGN.md` Amendment (d) describe it, but no
   such method existed, so a CM-only `Identity` could still negotiate GCM.
   Implemented in the vendored crate (offers **and** server-side selection),
   configured through the existing `Config::builder()` surface.
7. **Go `lostcancel` defect hidden by a build tag** — `go vet ./...` (the CI
   command) never compiles `//go:build it` files, so
   `internal/engineclient/it_live_test.go` discarded a `context.WithTimeout`
   cancel func. `go vet -tags it ./...` reproduces it; fixed by returning the
   cancel func and deferring it at each call site.
8. **A flaky DTLS test** — `dtls_retransmit_timer_rides_sweep_cadence` slept a
   fixed 1200 ms for a retransmit that dimpl jitters by ±250 ms
   (`vendor/dimpl/src/timer.rs`, `JITTER_RANGE = 0.5`), so it failed ~1 run in
   3. Replaced with a bounded sweep-until-observed loop; the assertion is
   unchanged.
9. **Silent error paths** — 11 handlers that discarded the failure outright
   (health probe, rate limiter, cache, live-call usage counting, billing cost
   observations, S3 `exists`, release TLS evidence). Each keeps its exact
   contract and now logs a bounded, secret-free record; see `tests/test_error_visibility.py`.

---

## 4. Broad-handler audit

`scripts/audit_except_handlers.py` walks the AST of every module under
`app/` and classifies each `except Exception` / bare `except` by what the
handler body actually does with the failure (log, re-raise, or any other
effectful call — record / report / persist / retry / metric).

| | Before | After |
| --- | --- | --- |
| broad handlers | 72 | 72 |
| unobserved (no log, no re-raise, no effect) | 29 | **18** |
| observed | 43 | 54 |

The 18 that remain are deliberate and *do* surface the failure to their
caller — `CheckOutcome(FAIL)`, `HealthResult(safe_message=…)`,
`RedirectResult(False, …)`, a boot-time `problems` list, a validation problem
list, a `failed_pages` counter, or a return value the caller records. They are
listed with their rationale in the audit output; none of them returns success
on failure.

---

## 5. CI stage gaps (reported, deliberately not applied)

`/.github/workflows/ci.yml` runs: backend (ruff + alembic round trip +
`pytest -m "not real_provider"`), gateway (`go vet` + `go test -race`),
`dashboard` (npm ci/test/build/audit), and two Docker builds. Two gaps:

1. **The Rust workspace gates never run in CI.** The gates exist and pass
   locally (see §2 row 7) but nothing enforces them — which is exactly how a
   truncated `vendor/dimpl` shipped in the first place.
2. **`dashboard-next` is not in CI** (its vitest suite and `tsc --noEmit` pass,
   but no job runs them).

The reviewer's call: adding jobs to `ci.yml` is a pipeline change that cannot
be validated from this sandbox (it would run on GitHub runners, with an
unpinned Rust toolchain). The exact snippet is ready in §7 — deliberately
**not** applied here rather than shipped unverified.

---

## 6. Environment-only failures (never counted as passes)

| Gate | Blocker | Exact command that stops |
| --- | --- | --- |
| `docker` image builds | docker not installed in the sandbox | `docker build -f services/realtime/media-engine-rs/Dockerfile .` |
| docker-compose/E2E stack | no docker, no compose | `docker compose up` |
| vendored dimpl's `tests/` (integration, ossl) | needs a wolfssl build; `--lib` (301 tests) runs, the integration targets need the full toolchain | `cargo test --manifest-path vendor/dimpl/Cargo.toml` |
| `cargo audit` / `npm audit` for the Rust tree | `cargo-audit` not installed | `cargo audit` |
| C/C++ sanitizer builds | no C/C++ targets and no cmake | `cmake -S . -B build` |

Everything else in §2 ran to completion. Postgres runs needed a real server:
installed with `sudo apt-get install -y postgresql` (17.11) and started with
`sudo pg_ctlcluster 17 main start`, then a `voxdesk`/`voxdesk_ci` role+DB
matching the CI service container.

Two runs are worth recording verbatim, because both are the reason a number
could be misread:

* the first Postgres suite run (`2555 passed, 30 skipped, 13 deselected`,
  134.60 s) predates the Phase 2 edits — it is superseded by the final run;
* a re-run was **OOM-killed** (exit 137) because it shared the sandbox with a
  `cargo test` and a TypeScript typecheck at the same time. It was re-run alone;
  only the clean run is quoted above.

---

## 7. Ready-to-apply CI snippet (reviewer decision)

```yaml
  media-engine:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: dtolnay/rust-toolchain@1.90.0
        with:
          components: rustfmt, clippy
      - name: Rust gates
        working-directory: services/realtime/media-engine-rs
        run: |
          cargo fmt --all -- --check
          cargo check --workspace
          cargo test --workspace
          cargo clippy --workspace --all-targets --all-features -- -D warnings

  dashboard-next:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 20
          cache: npm
          cache-dependency-path: dashboard-next/package-lock.json
      - working-directory: dashboard-next
        run: |
          npm ci
          npm test
          npx tsc --noEmit
          npm run build
```

`dtolnay/rust-toolchain@1.90.0` pins the same version as the engine's
`Dockerfile` (`rust:1.90-bookworm`), which is what keeps the clippy gate from
drifting under a new stable.

---

## 8. Migration / schema impact

**No migrations were added, removed or edited.** The chain is linear
(`0001_baseline … 0012_enterprise_persistence`, single head) and was exercised
in both directions against real PostgreSQL. No model change accompanies this
work, so no new migration is warranted.

---

## 9. Security and backwards-compatibility impact

* **Security**: nothing weakened. Token verification, API-key checks, session
  grants, webhook signature verification, SSRF/egress guards and rate limits
  are untouched; the rate limiter still fails **closed**. New log records carry
  exception *types* and bounded identifiers only — never SDK messages, DSNs,
  Redis URLs, card data or customer text (asserted in `tests/test_channels.py`,
  `tests/test_correlation_and_redaction.py`, `tests/test_error_visibility.py`).
  The vendored-dimpl change *tightens* crypto negotiation: an endpoint pinned
  to a profile list can no longer negotiate anything outside it.
* **Backwards compatibility**: public Python signatures are unchanged; the
  `LLMChoice`/`resolve()` contract is unchanged and its consumers were adapted
  (voice path already used attributes, the text path now does too). New
  behaviour is additive: `api_key_for`, `select_for_use`, `validate_llm_config`,
  `classify_provider_exception`, `Config::srtp_profiles`. Callers that never set
  `srtp_profiles` get dimpl's stock preference order (asserted by test).
