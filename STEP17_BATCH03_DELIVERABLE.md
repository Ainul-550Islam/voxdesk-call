# Step 17 — Batch 03: the realtime application surface (Python + dashboard + deploy wiring)

Repo: `/home/user/voxdesk` (HEAD `ccba554`)
Generated: 2026-09-21T16:46:50Z

These files exist in the repository only under the nested `voxdesk/voxdesk/` copy and were
absent from the root tree — which is why the root tree could not import `app.realtime` at all
and why `docker compose` in the root had no gateway to start. They are reproduced here in
full, together with the ten files whose nested revision is newer (each an addition-only
superset of the root revision, verified line by line before the merge).

**Files in this batch: 29.** Every block below is the file's content byte-for-byte as it exists in the working tree. Each block header carries the line count and the SHA-256 of the whole file, so a reader can confirm the block is complete and unmodified — nothing is paraphrased, summarised, or replaced by a placeholder comment.

---


==============================================================================
===== FILE: app/realtime/__init__.py (27 lines, sha256 85d73c7b0d0675a5649a091bc8339bb15399865a9cb52c87e3630f2317297bce) =====
==============================================================================
```python
"""
Realtime event fan-out to the public WebSocket edge (services/realtime/gateway-go).

The API is the PRODUCER side of the realtime loop: when a durable fact about
a call changes (created, status transition, transfer outcome), this package
POSTs a small *notice* event to the gateway's authenticated ingest endpoint,
and the gateway fans it out to that tenant's dashboards.

Three rules govern everything in here, for the same reasons the billing
hooks obey them:

1. **It never raises.** A realtime outage must not turn a Twilio webhook
   into a 500 (which would retry, and could double-bill or double-transfer).
2. **It never blocks the live call path.** Publishing happens on status
   callbacks and webhook flows, under a sub-two-second timeout — never on
   the media-stream loop.
3. **A notice is not data.** Payloads carry identifiers and states, never
   phone numbers, transcripts, or customer content. The dashboard re-fetches
   detail through the authenticated REST API. A leaked event frame should
   tell an onlooker almost nothing.

Wire vocabulary the package emits is defined in
services/realtime/gateway-go/README.md; the rooms it publishes to are the
closed set the gateway enforces ("calls" and "call:<uuid>").
"""

from app.realtime import events, publisher  # noqa: F401
```

==============================================================================
===== FILE: app/realtime/events.py (131 lines, sha256 c187832bf58b827dc895e3f37d0d1df4b3a399c56593e6f588469fdb09642837) =====
==============================================================================
```python
"""
Typed realtime events: which facts about a call are announced, and how.

An emission happens only after the fact is DURABLE (the callers emit after
`session.commit()`, never before) — a dashboard must never be told about a
state the database then rolled back.

Idempotency mirrors the database's exactly-once discipline: the event id is
``uuid5(namespace, "<call_id>:<kind>:<room_scope>:<status>:<transfer_state>")``
— deterministic, so a crash between the DB commit and the gateway POST,
replayed by the operator's own retry, is suppressed by the gateway's replay
cache instead of double-animating a wallboard.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.core.logging import log
from app.db.models import Call, CallStatus
from app.realtime.publisher import publish_event

#: Closed room vocabulary (the gateway refuses anything else).
ROOM_CALLS = "calls"


def room_call(call_id: uuid.UUID | str) -> str:
    """The per-call room a CallDetail page subscribes to."""
    return f"call:{call_id}"


#: Fixed namespace for derived event ids. Not a secret: it only scopes the
#: uuid5 space so a v5 derived for another purpose can never collide.
_EVENT_NAMESPACE = uuid.UUID("7e9f6d3a-2b1c-4f5e-9a8d-0c1b2a3f4e5d")


def _event_id(call: Call, *parts: str) -> str:
    """Deterministic, gateway-valid (UUID) dedupe id for one logical event."""
    material = ":".join([str(call.id), *parts])
    return str(uuid.uuid5(_EVENT_NAMESPACE, material))


def call_payload(call: Call) -> dict:
    """The notice payload. ROUTING + DISPLAY facts only.

    Deliberately absent: from_number/to_number (PII), summary, transcript,
    recording_url. The dashboard learns THAT a call changed and re-reads the
    detail through the authenticated REST API; a realtime frame on its own
    stays near-contentless.
    """
    def _iso(value: datetime | None) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()

    return {
        "call_id": str(call.id),
        "call_sid": call.call_sid,
        "status": call.status.value if isinstance(call.status, CallStatus) else str(call.status),
        "direction": call.direction.value if call.direction else None,
        "duration_seconds": call.duration_seconds,
        "booked": bool(call.booked),
        "escalated": bool(call.escalated),
        "lead_score": call.lead_score,
        "transfer_state": call.transfer_state.value if call.transfer_state else None,
        "started_at": _iso(call.started_at),
        "ended_at": _iso(call.ended_at),
    }


async def emit_call_event(
    call: Call,
    *,
    kind: str,
    extra: dict | None = None,
) -> bool:
    """Publish one call event to both rooms a dashboard may be watching.

    Two publishes, one logical event, deliberately: the list page subscribes
    "calls", the detail page subscribes "call:<id>", and the gateway has no
    room aliasing (by design — aliasing is how an impersonator-named room
    sneaks into a closed namespace). The replay id differs per room so a
    partial outage that delivered ONE of the two stays retryable for the other.

    Never raises (the publisher owns that contract); returns False if either
    publish was refused, so callers may log one line.
    """
    payload = call_payload(call)
    if extra:
        # Callers may attach transfer outcome etc. — same PII discipline
        # applies at the call site (states and reasons, never content).
        payload.update(extra)

    tenant_id = str(call.tenant_id)
    results = []
    for room, scope in ((ROOM_CALLS, "all"), (room_call(call.id), "one")):
        results.append(
            await publish_event(
                tenant_id=tenant_id,
                room=room,
                kind=kind,
                payload=payload,
                event_id=_event_id(call, kind, scope, *_event_parts(call)),
            )
        )
    ok = all(results)
    if not ok:
        # One line per logical event, not per publish — the publisher already
        # logged the specifics, this just makes "the dashboard may be stale"
        # greppable.
        log.info(
            "realtime.emit_degraded",
            kind=kind,
            call_id=str(call.id),
            tenant_id=tenant_id,
        )
    return ok


def _event_parts(call: Call) -> list[str]:
    """The state material that makes one logical event distinct from another.

    Same call + same status + same transfer state ⇒ same id ⇒ replays are
    suppressed by the gateway. Anything that legitimately changes the story
    produces a fresh id.
    """
    status = call.status.value if isinstance(call.status, CallStatus) else str(call.status)
    transfer = call.transfer_state.value if call.transfer_state else "none"
    return [status, transfer]
```

==============================================================================
===== FILE: app/realtime/publisher.py (111 lines, sha256 a91659a80d4d8203a556d7d15c7740f69a6bcda7f5b2956f2f6cc3b500c4f0ff) =====
==============================================================================
```python
"""
The ingest HTTP client: API → gateway `POST /ingest/v1/publish`.

Deliberately thin, because every guarantee about this channel already has an
owner:

* shape validation — the gateway (it rejects unknown rooms/kinds/tenant
  shapes with a 422, and 422 is the one status this client treats as
  "permanent, stop retrying", exactly like the CRM retry taxonomy);
* authentication — a shared Bearer secret, compared in constant time on the
  gateway side;
* replay suppression — the gateway's (tenant, event_id) TTL cache, when an
  event_id is supplied here;
* observability — the gateway's ingest metrics plus our structured log.

What THIS module owns: a single shared httpx client, a hard per-publish
timeout, and the never-raises contract.
"""
from __future__ import annotations

import httpx

from app.core.config import settings
from app.core.logging import log

#: Reuse one client: TCP+TLS setup per publish would cost more than the
#: event is worth, and the connection pool bounds how many sockets realtime
#: can ever hold open to the gateway. Limits mirror the billing provider
#: clients (small pools, explicit timeouts).
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.realtime_publish_timeout_seconds),
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=8),
        )
    return _client


def enabled() -> bool:
    """Realtime is on only when the operator configured BOTH endpoint and secret.

    An empty URL is the single off switch — development defaults stay silent,
    and a deployment without the gateway has no noisy failures.
    """
    return bool(settings.realtime_gateway_url and settings.realtime_gateway_ingest_secret)


async def publish_event(
    *,
    tenant_id: str,
    room: str,
    kind: str,
    payload: dict,
    event_id: str | None = None,
) -> bool:
    """Publish one event to the gateway. Returns True on acceptance.

    Acceptance covers exactly three gateway outcomes: fresh delivery,
    delivery-with-nobody-listening (delivered=0 is fine — dashboards
    reconnect), and replay suppression (duplicate=true is a SUCCESS: the
    event already happened once, which is the whole point of the id).

    Everything else — timeout, connection refused, 401, 422, 500 — returns
    False and is logged with the exception TYPE only (an httpx exception can
    quote the request, and the request headers carry the secret).
    """
    if not enabled():
        return False

    body: dict = {
        "tenant_id": tenant_id,
        "room": room,
        "kind": kind,
        "payload": payload,
    }
    if event_id:
        body["event_id"] = event_id

    url = settings.realtime_gateway_url.rstrip("/") + "/ingest/v1/publish"
    try:
        resp = await _get_client().post(
            url,
            json=body,
            headers={"Authorization": f"Bearer {settings.realtime_gateway_ingest_secret}"},
        )
    except Exception as exc:  # noqa: BLE001 — the never-raises contract is the feature
        log.warning(
            "realtime.publish_failed",
            kind=kind,
            room=room,
            error=type(exc).__name__,
        )
        return False

    if resp.status_code == 200:
        return True

    # 401 = our secret is wrong (rotate it); 422 = our shape is wrong (fix
    # the caller). Both ARE operator-visible facts, logged once per publish
    # rather than retried into a scream.
    log.warning(
        "realtime.publish_rejected",
        kind=kind,
        room=room,
        status=resp.status_code,
    )
    return False
```

==============================================================================
===== FILE: app/core/config.py (681 lines, sha256 ef157954b2066d062aee27ec1ee4470d0cfd1790df88c3654e1593f2252b6dde, no trailing newline in the file) =====
==============================================================================
```python
"""Central configuration. Everything comes from environment variables."""
import json
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict

#: The deployment environments the code understands. Anything else is
#: rejected at startup — fail closed rather than guessing.
KNOWN_APP_ENVS = frozenset({"development", "test", "staging", "production", "prod"})

#: Substrings that mark a value as an obvious placeholder. Used to refuse
#: obviously-unset secrets in production (a value that contains any of these
#: cannot be a real credential).
_PLACEHOLDER_MARKERS = (
    "change-me",
    "change_me",
    "insecure",
    "xxxx",
    "placeholder",
    "your-",
    "<",
    ">",
)


def _looks_placeholder(value: str | None) -> bool:
    """True when ``value`` is clearly a placeholder, not a real secret."""
    lowered = (value or "").lower()
    return bool(lowered) and any(marker in lowered for marker in _PLACEHOLDER_MARKERS)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # App
    app_env: str = "development"
    public_base_url: str = "http://localhost:8000"
    secret_key: str = "change-me"

    # ---- Authentication ----
    # MUST be overridden in production; validate_security() refuses to boot
    # with the placeholder. Generate with: openssl rand -hex 32
    jwt_secret: str = "insecure-development-only-change-me"
    jwt_issuer: str = "voxdesk"
    jwt_audience: str = "voxdesk-api"
    access_token_minutes: int = 15          # short-lived by design
    refresh_token_days: int = 14
    max_failed_logins: int = 8              # then a temporary lockout
    lockout_minutes: int = 15

    # Comma-separated browser origins allowed to call the API.
    cors_origins: str = "http://localhost:5173"

    # Comma-separated hostnames the HTTP layer will accept in the Host header
    # (TrustedHostMiddleware). Empty = the middleware is not installed, which
    # is the right default for single-proxy deployments where Caddy already
    # terminates TLS for exactly the configured domains. Set it in production
    # (e.g. "app.example.com") to reject Host-header spoofing and
    # host-header-based SSRF/cache-poisoning at the application layer too.
    trusted_hosts: str = ""

    # Database
    database_url: str = "postgresql+asyncpg://voxdesk:voxdesk@localhost:5432/voxdesk"

    # Twilio
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""

    # Twilio webhook signature verification is ON by default (fail-closed).
    # This opt-in flag disables it for local development so webhooks can be
    # exercised with curl and no valid X-Twilio-Signature. It MUST stay false
    # in production -- validate_security() refuses to boot otherwise.
    twilio_skip_webhook_verify: bool = False

    # ---- Real-call E2E (Step 5, scale-compliance) ----
    # The only way a *real* telephone call may exercise the AI voice pipeline
    # outside normal production traffic is a genuine, human-controlled test:
    # an operator dials a dedicated test number from an allowlisted phone, and
    # the dialed number maps to a tenant marked `is_test_tenant`. `e2e_enabled`
    # arms the guard; nothing here ever places a call. Production is rejected
    # outright (validate_security refuses to boot), and when armed, any call
    # that is not an explicit, allowlisted test call is refused at the door.
    e2e_enabled: bool = False
    # The dedicated Twilio number that the test tenant answers. Required when
    # e2e_enabled is set.
    e2e_test_number: str = ""
    # Comma-separated allowlist of operator caller numbers (E.164) permitted to
    # trigger a live E2E call. Empty means "nobody".
    e2e_allowed_callers: str = ""

    # Media-stream handshake: how long we wait for Twilio's "connected" +
    # "start" frames after accepting the socket before closing it. A client
    # that connects and never speaks would otherwise hold a database session
    # and a socket open forever (a stuck call). 0 disables the bound.
    stream_handshake_timeout_seconds: float = 15.0

    # Deepgram (STT)
    deepgram_api_key: str = ""
    deepgram_model: str = "nova-3"

    # ---- LLM providers (তিনটাই সাপোর্টেড, যেকোনো একটা থাকলেই চলবে) ----
    openai_api_key: str = ""          # ChatGPT
    anthropic_api_key: str = ""       # Claude
    google_api_key: str = ""          # Gemini
    default_llm_preset: str = "natural"   # fast | natural | cheap | smart

    # ElevenLabs (TTS)
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = "21m00Tcm4TlvDq8ikWAM"
    elevenlabs_model: str = "eleven_flash_v2_5"

    # Channels
    whatsapp_enabled: bool = False
    twilio_whatsapp_number: str = ""      # e.g. whatsapp:+14155238886 (sandbox)

    # Default language for new tenants
    default_language: str = "en-US"

    # ---------- Knowledge base / RAG ----------
    # Storage. "local" writes under knowledge_local_path; "s3" uses the bucket.
    knowledge_storage_backend: str = "local"
    knowledge_local_path: str = "./var/knowledge"
    knowledge_s3_bucket: str = ""
    knowledge_s3_region: str = ""
    knowledge_s3_endpoint_url: str = ""      # for MinIO / R2 / Spaces

    # Upload limits. Enforced before any expensive processing.
    knowledge_max_file_mb: int = 20
    knowledge_max_documents_per_tenant: int = 2000

    # Chunking. Sizes are in characters; ~4 chars per token for English, so
    # 3200 chars is roughly 800 tokens -- comfortably inside every provider's
    # context while leaving room for the conversation itself.
    knowledge_chunk_chars: int = 3200
    knowledge_chunk_overlap_chars: int = 400
    knowledge_min_chunk_chars: int = 120

    # Embeddings. "hashing" is deterministic, offline and free -- correct for
    # local development and tests. Production must configure a real provider;
    # validate_security() refuses to boot otherwise.
    knowledge_embedding_provider: str = "hashing"
    knowledge_embedding_model: str = "hashing-v1"
    # 4096 is the right default *for the hashing embedder specifically*.
    # Measured on the evaluation corpus: at 512 dimensions, hash collisions
    # gave unrelated questions ("what is your return policy on tractors")
    # higher similarity than genuinely relevant chunks. At 4096 the noise
    # floor is exactly 0.0 and relevant chunks score 0.08-0.47, which is the
    # separation retrieval needs.
    #
    # SWITCHING PROVIDER? Set this to the model's real width -- 1536 for
    # text-embedding-3-small, 3072 for -large. Leaving it at 4096 will fail
    # at startup rather than silently mis-index.
    knowledge_embedding_dimensions: int = 4096
    knowledge_embedding_batch_size: int = 32
    knowledge_embedding_timeout_seconds: float = 20.0

    # Ingestion. "inline" runs indexing in a FastAPI background task, which
    # is right for development and small deployments; "worker" leaves the
    # document UPLOADED for scripts/scheduler.py to pick up.
    knowledge_ingest_mode: str = "inline"
    #: A document PROCESSING longer than this is assumed to belong to a dead
    #: worker and is reset to FAILED so it can be retried.
    knowledge_processing_timeout_seconds: int = 900

    # Retrieval.
    knowledge_top_k: int = 4
    # Similarity floor, and it MUST be retuned when the provider changes.
    #
    # The default hashing embedder is lexical: a chunk sharing two of a
    # question's four words scores around 0.05-0.20, while a chunk sharing
    # none scores exactly 0.0. The signal is clean but the scale is
    # compressed, so the floor's only job here is to exclude zero-overlap
    # noise. A real semantic provider has the opposite shape -- unrelated
    # text still scores 0.1-0.3 -- and needs roughly 0.30. Shipping the
    # lexical default against OpenAI embeddings would return everything.
    # See docs/KNOWLEDGE-RAG.md.
    knowledge_min_score: float = 0.03
    knowledge_rerank_enabled: bool = True
    knowledge_rerank_candidates: int = 12
    #: Hard ceiling for retrieval during a live call. A caller will not wait.
    knowledge_retrieval_timeout_seconds: float = 1.5
    knowledge_context_max_chars: int = 4000

    # Google
    google_credentials_json: str = "./secrets/google_service_account.json"

    # ---------- Observability (STEP 9) ----------
    # Sentry error reporting. Optional: when empty nothing is initialised and
    # unhandled exceptions are reported nowhere (fine for development). Set it
    # to your project DSN in production. Never logged, never returned by an API.
    sentry_dsn: str = ""

    # Log level (DEBUG|INFO|WARNING|ERROR) and renderer. Production defaults to
    # machine-readable JSON (see app/core/logging.py); development keeps the
    # coloured console output. LOG_FORMAT accepts "console" or "json".
    log_level: str = "INFO"
    log_format: str = "console"

    # ---------- Data policy / compliance (STEP 9) ----------
    # Retention for call recordings and transcripts, which hold personal data.
    # Operator-configurable per jurisdiction: GDPR/EU and healthcare buyers
    # usually want 30-90 days; 365 is a safe default for the US. Records older
    # than the cutoff are flagged for deletion (app/core/data_policy.py,
    # docs/COMPLIANCE.md).
    call_retention_days: int = 365

    # EU AI Act transparency (Art. 50) and several US state laws: a caller must
    # know they are speaking to an automated agent. Default true; the tenant's
    # greeting is checked with app.core.data_policy.ai_disclosure_present().
    ai_disclosure_required: bool = True

    # ---------- Rate limiting (STEP 9) ----------
    # OFF by default so tests and local dev are unaffected; production sets
    # RATE_LIMIT_ENABLED=true. The limiter fails closed (rejects) on error.
    rate_limit_enabled: bool = False
    rate_limit_burst: int = 300        # non-auth requests per minute per IP
    rate_limit_login_per_minute: int = 10   # auth requests per minute per IP

    # ---------- Redis / cache (STEP 9) ----------
    # Empty = in-process cache and rate limiting (single worker). Set
    # REDIS_URL=redis://redis:6379/0 in multi-worker production.
    redis_url: str = ""
    cache_ttl_seconds: int = 60

    # ---------- Database pool (STEP 9) ----------
    db_pool_size: int = 10
    db_max_overflow: int = 20

    # ---------- Metrics (STEP 9) ----------
    # Prometheus /metrics endpoint. Disabled by default; production sets
    # METRICS_ENABLED=true and a METRICS_TOKEN for the scraper.
    metrics_enabled: bool = False
    metrics_token: str = ""

    # ---------- Cost awareness (STEP 7 scale-compliance) ----------
    # Operator-provided *provider* unit prices, in millicents per smallest
    # unit, as a JSON object. Keys are a fixed vocabulary; anything unknown is
    # ignored. A missing or zero price means UNKNOWN — the cost metric is
    # reported with cost_known=0 rather than an invented number. See
    # docs/COST-AWARENESS.md.
    #
    #   {"voice_minute": 1300, "sms_segment": 790,
    #    "llm_1k_tokens:openai": 15, "llm_1k_tokens:anthropic": 80,
    #    "tts_1k_chars": 30}
    #
    # These are what VoxDesk *pays providers*, not what tenants are charged
    # (the plan catalogue owns revenue). Never a billing authority.
    cost_unit_prices_json: str = ""

    # ---------- Failure injection (STEP 7 scale-compliance) ----------
    # Deterministic chaos for load tests and SLO drills. OFF by default and
    # hard-refused in production (validate_security). Rules are exact-path
    # matches with a fixed effect — never probabilistic. See
    # docs/FAILURE-INJECTION.md.
    chaos_enabled: bool = False
    chaos_rules_json: str = ""

    # ---------- Scheduler metrics (STEP 7 scale-compliance) ----------
    # The background worker is a separate process, so its job metrics need a
    # scrape target of their own. prometheus_client.start_http_server binds
    # this port on the scheduler container (compose-network only). 0 disables.
    scheduler_metrics_port: int = 8001

    # ---------- security.txt (RFC 9116, STEP 9) ----------
    # Contact for security researchers and buyers' security teams. Empty = the
    # /.well-known/security.txt endpoint returns 404 (a contact-less file is
    # worse than none). Set it to a monitored email or https:// URL in
    # production.
    security_contact: str = ""

    # ---------- Licensing (STEP 9) ----------
    # HMAC key for self-hosted/white-label license tokens. Falls back to
    # JWT_SECRET when empty (acceptable for a single deployment; set a distinct
    # key when reselling licenses across deployments).
    license_secret: str = ""

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() in {"production", "prod"}

    @property
    def is_staging(self) -> bool:
        """True for the dedicated pre-production environment.

        Staging is deliberately NOT production: it may run the real-call E2E
        guard, may enable failure injection, and does not hard-fail readiness
        on missing provider keys. It is also NOT development: schema is owned
        by Alembic (no ``create_all``), and its secrets must be its own.
        """
        return self.app_env.lower() == "staging"

    @property
    def uses_https(self) -> bool:
        """Whether the deployment is served over TLS.

        Drives the Secure cookie flag and the HSTS header from the actual URL
        scheme rather than a guess about the environment: a staging box that
        is TLS-terminated gets Secure cookies and HSTS exactly like
        production.
        """
        return self.public_base_url.startswith("https://")

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def trusted_host_list(self) -> list[str]:
        """Hostnames allowed in the Host header. Empty list = middleware off."""
        return [h.strip().lower() for h in self.trusted_hosts.split(",") if h.strip()]

    @property
    def e2e_caller_list(self) -> list[str]:
        return [n.strip() for n in self.e2e_allowed_callers.split(",") if n.strip()]

    @property
    def cost_unit_prices(self) -> dict[str, int]:
        """The parsed operator price table, or ``{}`` when unset/malformed.

        Malformed JSON is not an exception here — startup validation reports it
        and the cost layer treats every unknown/missing price as UNKNOWN.
        """
        raw = (self.cost_unit_prices_json or "").strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if not isinstance(parsed, dict):
            return {}
        return {str(k): int(v) for k, v in parsed.items() if isinstance(v, (int, float))}

    @property
    def chaos_rules(self) -> list[dict]:
        """The parsed chaos rule list, or ``[]`` when unset/malformed."""
        raw = (self.chaos_rules_json or "").strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return [r for r in parsed if isinstance(r, dict)]

    # ---------- CRM integrations (STEP 5) ----------
    # Application-level encryption of provider credentials at rest.
    # Format: "key_id:base64key,older_id:base64key" -- first entry is active.
    # Generate: python -c "import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
    # Empty is allowed in development (integrations simply cannot store
    # credentials); validate_security() refuses to boot production without it.
    crm_encryption_keys: str = ""

    # Per-request ceiling for any provider call. Deliberately short: this runs
    # on a worker, but a provider hanging for 60s still blocks the pass.
    crm_request_timeout_seconds: float = 10.0

    # Retry policy (requirement 13: must be configurable).
    crm_retry_max_attempts: int = 5
    crm_retry_base_seconds: float = 2.0
    crm_retry_max_seconds: float = 900.0

    # VoxDesk-side per-(tenant, provider) protection, so one broken tenant
    # cannot consume the shared worker.
    crm_rate_limit_per_second: float = 5.0
    crm_rate_limit_burst: float = 20.0

    # Worker loop.
    crm_sync_interval_seconds: int = 20
    crm_sync_batch_size: int = 20
    crm_stuck_sync_minutes: int = 15

    # Step 6 (scale-compliance): how long a reminder-send lease is valid. A
    # worker claims a reminder, sends the SMS, and clears the lease; if it
    # dies mid-send the reaper reclaims the row after this many seconds. Long
    # enough to cover a slow Twilio call, short enough that a dead worker
    # cannot block a customer's reminder indefinitely.
    reminder_lease_seconds: int = 300

    # Inbound provider webhooks: how much clock skew to tolerate before an
    # event is treated as a replay.
    crm_webhook_tolerance_seconds: int = 300

    # ---------- Calendar / scheduling (STEP 6) ----------
    # Per-request ceiling for any calendar provider call. Shorter than the CRM
    # one because this runs while a caller is on the phone: an availability
    # lookup that takes eight seconds has already lost the conversation.
    calendar_request_timeout_seconds: float = 6.0

    # The hard bound the voice agent applies on top. Requirement 30: an
    # availability lookup must not hang indefinitely.
    calendar_voice_timeout_seconds: float = 3.0

    # Inbound provider notifications: clock skew tolerated before an event is
    # treated as a replay.
    calendar_webhook_tolerance_seconds: int = 300

    # Refresh an OAuth access token this long before it actually expires, so a
    # booking never races the expiry.
    calendar_token_refresh_margin_seconds: int = 300

    # ---------- Billing (STEP 7) ----------
    # "stripe" or "manual". Manual is a real mode, not a stub: plans,
    # entitlements, metering and periods all work; only the payment rail is
    # absent. It is the correct setting for development and for
    # invoice-me contracts.
    billing_provider: str = "manual"

    # Stripe secrets. Never returned by an API, never logged, never in a JWT.
    # validate_security() refuses to boot production with provider=stripe and
    # either of these empty.
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_publishable_key: str = ""      # safe for a browser; not a secret

    # Where Stripe returns the browser. Required only when checkout is used.
    billing_checkout_success_url: str = ""
    billing_checkout_cancel_url: str = ""
    billing_portal_return_url: str = ""

    # Per-request ceiling for a provider call. Generous relative to the voice
    # path because nothing here runs while a caller is on the line.
    billing_request_timeout_seconds: float = 15.0

    # Requirement 25's "clearly defined unlimited entitlement". An explicit
    # switch rather than an environment guess -- an implicit staging default
    # is how unlimited reaches production.
    billing_unlimited_entitlements: bool = False

    # Requirement 26. Inbound calls are never blocked (see the audit's F10:
    # hanging up on a dentist's patients because the dentist owes money is a
    # product decision nobody made deliberately). This gates *outbound* and
    # account-level actions only.
    billing_enforce_entitlements: bool = True

    # ---------- Realtime gateway (services/realtime/gateway-go) ----------
    # Base URL of the public WebSocket edge's internal listener, e.g.
    # http://realtime-gateway:8790. EMPTY = realtime disabled (the default)
    # and every publish is a silent no-op — a deployment without the gateway
    # must not accumulate failures.
    realtime_gateway_url: str = ""

    # Shared secret for POST /ingest/v1/publish on the gateway (must equal
    # VOXDESK_GATEWAY_INGEST_SECRET over there). The gateway checks it in
    # constant time; the API must present it exactly.
    realtime_gateway_ingest_secret: str = ""

    # Hard ceiling per publish. Realtime is a notice channel: an event that
    # did not get out in 1.5 s is not worth holding a Twilio webhook for.
    realtime_publish_timeout_seconds: float = 1.5

    @property
    def realtime_enabled(self) -> bool:
        return bool(self.realtime_gateway_url and self.realtime_gateway_ingest_secret)

    def validate_security(self) -> list[str]:
        """
        Fatal misconfigurations. Called at startup; in production the app
        refuses to boot rather than serving traffic with a known-bad secret.
        """
        problems: list[str] = []

        # ---- Deployment environment (STEP 8) ----
        if self.app_env.lower() not in KNOWN_APP_ENVS:
            problems.append(
                f"APP_ENV must be one of {sorted(KNOWN_APP_ENVS)}, got "
                f"{self.app_env!r}"
            )

        if self.is_production:
            if self.rate_limit_enabled is not True:
                problems.append(
                    "RATE_LIMIT_ENABLED must be true in production; the "
                    "limiter fails closed and must not be silently off"
                )
            if (self.log_level or "").upper() == "DEBUG":
                problems.append(
                    "LOG_LEVEL=DEBUG is not allowed in production; verbose "
                    "logs can leak caller data"
                )
            if "localhost" in self.public_base_url or "127.0.0.1" in self.public_base_url:
                problems.append(
                    "PUBLIC_BASE_URL must be a public https URL in production, "
                    "not localhost"
                )
            for origin in self.cors_origin_list:
                if not origin.startswith("https://"):
                    problems.append(
                        f"CORS_ORIGINS entry {origin!r} must use https in "
                        "production; development origins cannot reach prod"
                    )
            # Placeholder secrets: an obviously-unset credential must not
            # boot a production deployment that would then fail on live calls.
            for field, value in (
                ("SECRET_KEY", self.secret_key),
                ("JWT_SECRET", self.jwt_secret),
                ("TWILIO_AUTH_TOKEN", self.twilio_auth_token),
                ("DEEPGRAM_API_KEY", self.deepgram_api_key),
                ("ELEVENLABS_API_KEY", self.elevenlabs_api_key),
                ("OPENAI_API_KEY", self.openai_api_key),
                ("ANTHROPIC_API_KEY", self.anthropic_api_key),
                ("GOOGLE_API_KEY", self.google_api_key),
                ("STRIPE_SECRET_KEY", self.stripe_secret_key),
                ("STRIPE_WEBHOOK_SECRET", self.stripe_webhook_secret),
            ):
                if value and _looks_placeholder(value):
                    problems.append(
                        f"{field} looks like a placeholder and must be "
                        "replaced in production"
                    )

        if self.jwt_secret == Settings.model_fields["jwt_secret"].default:
            problems.append("JWT_SECRET is still the built-in default")
        if len(self.jwt_secret) < 32:
            problems.append("JWT_SECRET must be at least 32 characters")
        if self.secret_key in {"", "change-me"}:
            problems.append("SECRET_KEY is still the built-in default")
        if "*" in self.cors_origin_list:
            problems.append("CORS_ORIGINS must not be '*' when credentials are allowed")
        if self.is_production and not self.public_base_url.startswith("https://"):
            problems.append("PUBLIC_BASE_URL must use https in production")
        if self.is_production and not self.twilio_auth_token:
            problems.append("TWILIO_AUTH_TOKEN is required to verify webhooks")
        if self.is_production and self.twilio_skip_webhook_verify:
            problems.append(
                "TWILIO_SKIP_WEBHOOK_VERIFY must not be enabled in production; "
                "it disables Twilio webhook signature verification"
            )

        if self.e2e_enabled:
            if self.is_production:
                problems.append(
                    "E2E_ENABLED must not be set in production; real-call E2E "
                    "is operator-run in a test environment only"
                )
            if not self.e2e_test_number:
                problems.append(
                    "E2E_TEST_NUMBER is required when E2E_ENABLED is set"
                )
            if not self.e2e_caller_list:
                problems.append(
                    "E2E_ALLOWED_CALLERS is required when E2E_ENABLED is set"
                )

        if self.knowledge_storage_backend == "s3" and not self.knowledge_s3_bucket:
            problems.append("KNOWLEDGE_S3_BUCKET is required when the backend is s3")
        if self.is_production and self.knowledge_embedding_provider == "hashing":
            problems.append(
                "KNOWLEDGE_EMBEDDING_PROVIDER=hashing is a development stub; "
                "configure a real embedding provider in production"
            )

        # ---- CRM credentials (STEP 5) ----
        # Storing a provider token with no encryption key configured would put
        # plaintext CRM credentials in the database, so production refuses.
        if self.is_production and not self.crm_encryption_keys:
            problems.append(
                "CRM_ENCRYPTION_KEYS is required in production; CRM provider "
                "credentials must not be stored without application-level "
                "encryption"
            )
        if self.crm_encryption_keys:
            # Fail at boot on a malformed key rather than at the moment a
            # tenant first tries to connect an integration.
            try:
                from app.integrations.crm.crypto import parse_key_ring

                parse_key_ring(self.crm_encryption_keys)
            except Exception as exc:
                problems.append(f"CRM_ENCRYPTION_KEYS is invalid: {exc}")
        if self.crm_retry_max_attempts < 1:
            problems.append("CRM_RETRY_MAX_ATTEMPTS must be at least 1")

        # ---- Billing (STEP 7) ----
        # Requirement 6: do not let development defaults run in production.
        if self.billing_provider not in ("stripe", "manual"):
            problems.append(
                f"BILLING_PROVIDER must be 'stripe' or 'manual', got "
                f"{self.billing_provider!r}"
            )
        if self.billing_provider == "stripe":
            if not self.stripe_secret_key:
                problems.append(
                    "STRIPE_SECRET_KEY is required when BILLING_PROVIDER=stripe"
                )
            if not self.stripe_webhook_secret:
                problems.append(
                    "STRIPE_WEBHOOK_SECRET is required when BILLING_PROVIDER=stripe; "
                    "without it the webhook endpoint cannot verify signatures and "
                    "anyone could forge a subscription"
                )
            if self.is_production and self.stripe_secret_key.startswith("sk_test_"):
                problems.append(
                    "STRIPE_SECRET_KEY is a test-mode key; production would take "
                    "no real payments"
                )
        if self.is_production and self.billing_unlimited_entitlements:
            problems.append(
                "BILLING_UNLIMITED_ENTITLEMENTS must not be enabled in production; "
                "every plan limit would be ignored"
            )

        # ---- Cost awareness (STEP 7 scale-compliance) ----
        if self.cost_unit_prices_json.strip():
            try:
                parsed = json.loads(self.cost_unit_prices_json)
            except json.JSONDecodeError as exc:
                problems.append(f"COST_UNIT_PRICES is not valid JSON: {exc}")
            else:
                if not isinstance(parsed, dict):
                    problems.append("COST_UNIT_PRICES must be a JSON object")
                else:
                    for key, value in parsed.items():
                        if not isinstance(value, (int, float)) or value < 0:
                            problems.append(
                                f"COST_UNIT_PRICES[{key!r}] must be a non-negative number"
                            )

        # ---- Failure injection (STEP 7 scale-compliance) ----
        if self.chaos_enabled:
            if self.is_production:
                problems.append(
                    "CHAOS_ENABLED must not be set in production; failure "
                    "injection is for load tests and SLO drills only"
                )
            if not self.chaos_rules:
                problems.append(
                    "CHAOS_RULES is required when CHAOS_ENABLED is set"
                )
        elif self.chaos_rules_json.strip():
            # Rules configured but the switch off: harmless, but worth saying
            # so an operator who expects chaos to be live notices it is not.
            try:
                json.loads(self.chaos_rules_json)
            except json.JSONDecodeError as exc:
                problems.append(f"CHAOS_RULES is not valid JSON: {exc}")

        # ---- Realtime gateway (services/realtime/gateway-go) ----
        # A half-configured realtime channel is ALWAYS a mistake: the URL
        # without the secret means publishes 401 forever; the secret without
        # the URL means the operator THINKS dashboards are live while nothing
        # publishes. Both are caught at boot, not in a dashboard bug report.
        if self.realtime_gateway_url and not self.realtime_gateway_ingest_secret:
            problems.append(
                "REALTIME_GATEWAY_INGEST_SECRET is required when REALTIME_GATEWAY_URL "
                "is set (must equal VOXDESK_GATEWAY_INGEST_SECRET on the gateway)"
            )
        if self.realtime_gateway_ingest_secret and not self.realtime_gateway_url:
            problems.append(
                "REALTIME_GATEWAY_URL is required when REALTIME_GATEWAY_INGEST_SECRET "
                "is set; a secret without an endpoint means realtime is silently off"
            )
        if self.realtime_gateway_ingest_secret:
            if _looks_placeholder(self.realtime_gateway_ingest_secret):
                problems.append("REALTIME_GATEWAY_INGEST_SECRET looks like a placeholder")
            elif len(self.realtime_gateway_ingest_secret) < 16:
                problems.append("REALTIME_GATEWAY_INGEST_SECRET must be at least 16 characters")
        if self.realtime_gateway_url:
            url = self.realtime_gateway_url
            if not (url.startswith("http://") or url.startswith("https://")):
                problems.append(
                    "REALTIME_GATEWAY_URL must start with http:// or https://; the "
                    "ingest endpoint is plain HTTP on the internal network"
                )
        if self.realtime_publish_timeout_seconds <= 0:
            problems.append("REALTIME_PUBLISH_TIMEOUT_SECONDS must be positive")

        return problems

    @property
    def ws_base_url(self) -> str:
        """Twilio needs wss:// for Media Streams."""
        return self.public_base_url.replace("https://", "wss://").replace("http://", "ws://")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
```

==============================================================================
===== FILE: app/telephony/twilio_handler.py (581 lines, sha256 1de09f5d750c30193c5e30f62751697b9957dc237c2664b35a8438c1ffd3e0cd, no trailing newline in the file) =====
==============================================================================
```python
"""Twilio webhooks.

Flow:
  1. Someone dials the tenant's number.
  2. Twilio POSTs /telephony/voice  -> we answer with TwiML containing <Stream>.
  3. Twilio opens a WebSocket to /telephony/ws and pumps raw audio both ways.
"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from twilio.twiml.voice_response import Connect, VoiceResponse

from app.agent.errors import ProviderError
from app.agent.provider_observability import record_provider_error
from app.core import metrics
from app.core.config import settings
from app.core.logging import log
from app.db.models import (
    Call,
    CallStatus,
    Lead,
    LeadStatus,
    Tenant,
    TransferState,
)
from app.db.session import get_session, get_sessionmaker
from app.integrations import crm
from app.billing import hooks as billing_hooks
from app.integrations.crm import hooks as crm_hooks
from app.realtime import events as realtime_events
from app.telephony import call_state, e2e_guard, phone, transfer_service
from app.telephony.ivr import DEFAULT_FLOW, next_node, render_node
from app.telephony.stream_auth import (
    create_stream_token,
    verify_stream_token,
    verify_twilio_request,
)

router = APIRouter(prefix="/telephony", tags=["telephony"])


# Single shared implementation -- see app/telephony/stream_auth.py.
_verify_twilio = verify_twilio_request


@router.post("/voice", response_class=PlainTextResponse)
async def incoming_call(
    request: Request,
    CallSid: str = Form(...),
    From: str = Form(...),
    To: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    if not await _verify_twilio(request):
        return PlainTextResponse("forbidden", status_code=403)

    tenant = (
        await session.execute(select(Tenant).where(Tenant.twilio_number == To))
    ).scalar_one_or_none()

    # Step 5 (scale-compliance): the real-call E2E guard. A no-op unless the
    # operator armed E2E mode; when armed, only an explicit, allowlisted,
    # test-tenant call may pass. Rejections happen before any Call row exists,
    # so a refused caller can never create a session, stream, usage event, or
    # audit record. See app/telephony/e2e_guard.py.
    try:
        e2e_ctx = e2e_guard.check_inbound(
            settings, from_number=From, to_number=To, tenant=tenant
        )
    except e2e_guard.E2ERejected as exc:
        log.warning(
            "call.e2e_rejected_hangup",
            reason=str(exc),
            from_=phone.redact(From),
            to=phone.redact(To),
        )
        rejected = VoiceResponse()
        rejected.say(
            "This number is in test mode and this caller is not authorized. Goodbye."
        )
        rejected.hangup()
        return PlainTextResponse(str(rejected), media_type="application/xml")
    except e2e_guard.E2EConfigurationError:
        # Fail-closed: refuse traffic rather than answer without the guard.
        raise HTTPException(status_code=503, detail="E2E configuration error")

    response = VoiceResponse()

    if tenant is None or not tenant.is_active:
        response.say("This number is not configured. Goodbye.")
        response.hangup()
        return PlainTextResponse(str(response), media_type="application/xml")

    # STEP 7 removed the usage cap that used to live here.
    #
    # It compared a **lifetime** counter (`minutes_used`, which nothing ever
    # reset -- audit F2) against a **monthly** allowance, so a tenant on 500
    # included minutes got 750 minutes ever and was then permanently answered
    # with "This account has reached its usage limit".
    #
    # It was also the wrong lever. Hanging up on a dentist's patients because
    # the dentist owes forty dollars punishes the one party who has no way to
    # fix it, and costs the business far more than the debt. Entitlement is
    # now enforced where the spend actually originates -- outbound calls and
    # account-level features, via `billing.hooks.may_place_outbound_call` --
    # and inbound calls, which are the customer's revenue, are never blocked.
    #
    # `tenant.is_active`, checked above, remains the deliberate off switch for
    # an account that genuinely must stop.

    # Idempotent call creation: Twilio may re-deliver /voice for the same
    # CallSid (network retry). The unique index on call_sid already prevents a
    # duplicate row, but the naive insert turned that into a 500 and another
    # retry. Answering an already-known CallSid with fresh TwiML is correct and
    # creates no second call/session/billing/usage/audit record.
    call = (
        await session.execute(select(Call).where(Call.call_sid == CallSid))
    ).scalar_one_or_none()
    created_call = False
    if call is None:
        call = Call(
            tenant_id=tenant.id,
            call_sid=CallSid,
            from_number=From,
            to_number=To,
            status=CallStatus.IN_PROGRESS,
        )
        session.add(call)
        try:
            await session.commit()
            created_call = True
        except IntegrityError:
            # Lost a race with a concurrent duplicate; roll back and reuse the
            # row the other request created.
            await session.rollback()
            call = (
                await session.execute(select(Call).where(Call.call_sid == CallSid))
            ).scalar_one_or_none()
    else:
        log.info("call.incoming_duplicate", call_sid=CallSid, tenant=tenant.name)

    # Realtime: announce a genuinely-new call once, AFTER its row is durable.
    # A redelivered /voice (Twilio retry) does not re-announce — that is the
    # whole point of created_call tracking instead of "emit on every POST".
    if created_call and call is not None:
        await realtime_events.emit_call_event(call, kind="call.created")

    # Short-lived signed token so the media WebSocket is not open to anyone
    # who learns a call SID. See app/telephony/stream_auth.py.
    ws_url = (
        f"{settings.ws_base_url}/telephony/ws"
        f"?token={create_stream_token(CallSid)}"
    )

    # IVR চালু থাকলে আগে মেনু, তারপর AI। ফ্লো ভাঙা থাকলে চুপচাপ AI-তে যাবে।
    if tenant.ivr_enabled:
        flow = tenant.ivr_flow or DEFAULT_FLOW
        start = flow.get("start", "start")
        log.info(
            "call.incoming.ivr", tenant=tenant.name, node=start, e2e_test=bool(e2e_ctx)
        )
        return PlainTextResponse(
            render_node(flow, start, tenant, ws_url=ws_url),
            media_type="application/xml",
        )

    connect = Connect()
    connect.stream(url=ws_url)
    response.append(connect)
    log.info(
        "call.incoming",
        from_=phone.redact(From),
        to=phone.redact(To),
        tenant=tenant.name,
        call_sid=CallSid,
        e2e_test=bool(e2e_ctx),
    )
    return PlainTextResponse(str(response), media_type="application/xml")


@router.post("/ivr", response_class=PlainTextResponse)
async def ivr_step(
    request: Request,
    node: str = "",
    To: str = Form(...),
    CallSid: str = Form(...),
    Digits: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    """Every Gather in a flow posts back here with the pressed digit."""
    if not await _verify_twilio(request):
        return PlainTextResponse("forbidden", status_code=403)

    tenant = (
        await session.execute(select(Tenant).where(Tenant.twilio_number == To))
    ).scalar_one_or_none()
    if tenant is None:
        return PlainTextResponse("<Response><Hangup/></Response>",
                                 media_type="application/xml")

    flow = tenant.ivr_flow or DEFAULT_FLOW
    target = next_node(flow, node, Digits) if Digits else (node or flow.get("start"))
    # Short-lived signed token so the media WebSocket is not open to anyone
    # who learns a call SID. See app/telephony/stream_auth.py.
    ws_url = (
        f"{settings.ws_base_url}/telephony/ws"
        f"?token={create_stream_token(CallSid)}"
    )
    log.info("ivr.step", node=node, digit=Digits, next=target)
    return PlainTextResponse(
        render_node(flow, target, tenant, ws_url=ws_url), media_type="application/xml"
    )


@router.post("/outbound-answer", response_class=PlainTextResponse)
async def outbound_answer(
    request: Request,
    campaign_id: str = "",
    lead_id: str = "",
    AnsweredBy: str = Form(""),
    CallSid: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    """
    Twilio hits this when an outbound call is picked up. If the answer machine
    detector says it is voicemail we hang up immediately -- talking to an
    answering machine burns money and annoys people.

    Like the other Twilio webhooks this authenticates by HMAC signature, not
    by a user token. It was the last route in the telephony path still missing
    that check: without it anyone could mint a media-stream URL for an
    arbitrary call SID.
    """
    from twilio.twiml.voice_response import VoiceResponse as VR

    if not await _verify_twilio(request):
        return PlainTextResponse("forbidden", status_code=403)

    response = VR()
    if AnsweredBy.startswith("machine"):
        log.info("outbound.voicemail_detected", lead=lead_id)
        response.hangup()
        return PlainTextResponse(str(response), media_type="application/xml")

    connect = Connect()
    connect.stream(
        url=(
            f"{settings.ws_base_url}/telephony/ws"
            f"?token={create_stream_token(CallSid)}"
            f"&campaign_id={campaign_id}&lead_id={lead_id}"
        )
    )
    response.append(connect)
    log.info("outbound.answered", lead=lead_id, campaign=campaign_id)
    return PlainTextResponse(str(response), media_type="application/xml")


@router.websocket("/ws")
async def media_stream(websocket: WebSocket):
    """
    Twilio Media Stream. Machine-to-machine: authenticated by the signed
    token we minted into the <Stream> URL, NOT by a user JWT.
    """
    token = websocket.query_params.get("token")
    await websocket.accept()

    # Twilio sends "connected" then "start" before any audio. Bound this with
    # a timeout: a socket that connects and never speaks must not hold a
    # database session and a pipeline slot open forever. The bound is a
    # handshake bound only (generous by default and disable-able via
    # STREAM_HANDSHAKE_TIMEOUT_SECONDS=0) -- it never touches the live
    # conversation, which runs off the WebSocket, not a wall clock.
    async def _await_start():
        await websocket.receive_text()                       # connected
        return json.loads(await websocket.receive_text())    # start

    try:
        timeout = settings.stream_handshake_timeout_seconds
        start_msg = await (
            asyncio.wait_for(_await_start(), timeout)
            if timeout and timeout > 0
            else _await_start()
        )
    except (WebSocketDisconnect, asyncio.TimeoutError):
        log.warning("ws.handshake_incomplete")
        await websocket.close(code=1008)     # policy violation
        return

    start = start_msg.get("start", {})
    stream_sid = start.get("streamSid")
    call_sid = start.get("callSid")

    # The token is bound to this call SID, so a token captured from one call
    # cannot be replayed to listen in on another.
    # Imported here, not at module scope: the pipecat stack is heavy and is
    # only needed once a real media stream arrives. Keeping it lazy lets the
    # HTTP API, the webhooks and the test suite boot without it.
    from app.agent.pipeline import run_voice_agent

    if not verify_stream_token(call_sid, token):
        log.warning("ws.rejected_bad_stream_token", call_sid=call_sid)
        await websocket.close(code=1008)     # policy violation
        return

    async with get_sessionmaker()() as session:
        call = (
            await session.execute(select(Call).where(Call.call_sid == call_sid))
        ).scalar_one_or_none()
        if call is None:
            await websocket.close()
            return
        tenant = await session.get(Tenant, call.tenant_id)

        # Step 7 observability: the gauge counts live media-stream pipelines,
        # which is what "calls in flight" means to an operator watching the
        # dashboard. Bounded (a bare gauge), and cleared in `finally` even if
        # the pipeline crashes.
        metrics.ACTIVE_CALLS.inc()
        try:
            await run_voice_agent(
                websocket=websocket,
                stream_sid=stream_sid,
                call_sid=call_sid,
                session=session,
                tenant=tenant,
                call=call,
            )
        except Exception as exc:
            if isinstance(exc, ProviderError):
                # A typed provider failure: record the category and
                # retryability so dashboards/alerts can group them, and log
                # only the safe, fixed message (never a payload or secret).
                # The counter labels are normalised to a closed set, so this
                # cannot mint unbounded Prometheus series.
                record_provider_error(exc.provider, exc.category)
                log.error("call.crashed", call_sid=call_sid,
                          tenant_id=str(call.tenant_id),
                          provider=exc.provider, category=exc.category,
                          retryable=exc.retryable, error=exc.safe_message)
            else:
                log.error("call.crashed", call_sid=call_sid,
                          tenant_id=str(call.tenant_id), error=str(exc))
            # A transfer tears our stream down on purpose -- Twilio replaces
            # the TwiML and the socket dies. That is a successful handoff, not
            # a crashed call, so it must not be recorded as FAILED. Going
            # through call_state also protects an already-terminal status.
            await session.refresh(call)
            if call.transfer_state is TransferState.NONE:
                call_state.apply_status(
                    call, CallStatus.FAILED,
                    reason="media stream error", source="media_stream",
                )
                await session.commit()
            else:
                log.info("call.stream_closed_for_transfer", call_sid=call_sid,
                         transfer_state=call.transfer_state.value)
        finally:
            metrics.ACTIVE_CALLS.dec()


@router.post("/status", response_class=PlainTextResponse)
async def call_status(
    request: Request,
    CallSid: str = Form(...),
    CallStatus_: str = Form(alias="CallStatus", default=""),
    CallDuration: str = Form(default="0"),
    session: AsyncSession = Depends(get_session),
):
    """
    Twilio's call status callback.

    Twilio retries this webhook, and for a transferred call the parent leg and
    the dial leg can report out of order, so everything here must be safe to
    run twice and must never regress a terminal state. All of that logic lives
    in `app.telephony.call_state`; this function only decides what a *changed*
    status means for billing, leads and the CRM.

    Previously this route had no signature check at all, which meant anyone
    who could reach the URL could terminate any call and inflate a tenant's
    billed minutes.
    """
    if not await _verify_twilio(request):
        return PlainTextResponse("forbidden", status_code=403)

    call = (
        await session.execute(select(Call).where(Call.call_sid == CallSid))
    ).scalar_one_or_none()
    if call is None:
        # Unknown SID: acknowledge so Twilio stops retrying, but do nothing.
        log.info("status.unknown_call_sid", call_sid=CallSid)
        return PlainTextResponse("ok")

    try:
        duration = float(CallDuration or 0)
    except ValueError:
        duration = 0.0

    # `previous_duration` used to be captured here for the delta-billing that
    # STEP 7 removed. Nothing reads it now: billing takes the final absolute
    # duration once, which is what makes it order-independent.
    result = call_state.apply_provider_status(
        call, CallStatus_, duration_seconds=duration, source="twilio_status"
    )

    tenant = await session.get(Tenant, call.tenant_id)

    # A transfer that was still dialling when the call ended never connected.
    if result.applied and call_state.is_terminal(call.status):
        if call.transfer_state in (TransferState.REQUESTED, TransferState.DIALING):
            # An inference, not a provider verdict: the <Dial> callback may
            # still arrive and correct it. See transfer_service.INFERRED_PREFIX.
            await transfer_service.mark_transfer_failed(
                session, call,
                f"{transfer_service.INFERRED_PREFIX}"
                f"call ended while {call.transfer_state.value}",
            )

    # STEP 7: bill from the *final absolute duration*, once, through an
    # immutable idempotency-keyed usage event.
    #
    # The old line here was `minutes_used += duration - previous_duration`,
    # which subtracted whatever an earlier callback had stamped. Duplicates
    # were handled correctly, but *ordered* callbacks were not: the same
    # 120-second call billed 2.00, 1.00 or 0.50 minutes depending on whether
    # Twilio sent an intermediate `in-progress` or `answered` payload
    # carrying a duration. Measured, not theorised -- see
    # `docs/BILLING-AUDIT.md` F1. Direction of the error was *under*-billing,
    # so nobody complained and it was never found.
    #
    # `on_call_finalized` keys on the call id, so ordering stops mattering:
    # the first terminal callback records the full duration and every
    # subsequent one is a no-op at the database's unique constraint.
    if result.applied and call_state.is_terminal(call.status) and tenant:
        await billing_hooks.on_call_finalized(session, tenant, call)

    # Outbound: record how the attempt went so the dialer stops or retries.
    # Guarded by `result.applied` so a retried webhook cannot re-grade a lead.
    if result.applied and call.lead_id:
        lead = await session.get(Lead, call.lead_id)
        if lead and lead.status is not LeadStatus.DNC:
            if call.status is CallStatus.COMPLETED and (call.duration_seconds or 0) > 10:
                lead.status = (
                    LeadStatus.QUALIFIED
                    if (call.lead_score or 0) >= 50
                    else LeadStatus.CALLED
                )
                lead.score = call.lead_score
            elif lead.attempts >= (tenant.max_call_attempts if tenant else 3):
                lead.status = LeadStatus.FAILED

    # STEP 5: record the CRM event *inside this transaction*, before the
    # commit. The old code committed first, set `crm_synced = True`, committed
    # again, and only then fired an unawaited `asyncio.create_task` -- so a
    # process death between the flag and the POST marked the call synced and
    # never sent it. Now the event is part of the same commit as the call's
    # final state: either both land or neither does, and delivery is the
    # worker's problem.
    if result.applied and call_state.is_terminal(call.status) and not call.crm_synced:
        if call.status is CallStatus.COMPLETED:
            emitted = await crm_hooks.on_call_completed(session, tenant, call)
        else:
            # NO_ANSWER / FAILED. Commercially the more valuable of the two
            # for a home-service business: somebody should call back.
            emitted = await crm_hooks.on_call_missed(session, tenant, call)
        # The flag now means "an event exists for this call", which is a fact
        # about our own database rather than a guess about a remote system.
        call.crm_synced = call.crm_synced or emitted

    await session.commit()

    # Realtime: announce the APPLIED status change, after the commit, exactly
    # once per logical transition — a retried callback fails result.applied
    # and emits nothing, and the gateway's replay cache covers the remaining
    # crash-between-commit-and-publish window via the deterministic event id.
    if result.applied:
        await realtime_events.emit_call_event(call, kind="call.updated")

    return PlainTextResponse("ok")


@router.post("/transfer-status", response_class=PlainTextResponse)
async def transfer_status(
    request: Request,
    CallSid: str = Form(...),
    DialCallStatus: str = Form(default=""),
    DialCallDuration: str = Form(default="0"),
    session: AsyncSession = Depends(get_session),
):
    """
    Outcome of the <Dial> leg to the human.

    This is what turns "we asked Twilio to transfer" into "a human actually
    picked up". Without it the application could never honestly distinguish a
    connected transfer from one that rang out, which is exactly the guarantee
    STEP 3 is about.

    Point Twilio's Dial `action` at this URL. It is idempotent: a retried
    callback neither re-writes state nor duplicates transcript events.
    """
    if not await _verify_twilio(request):
        return PlainTextResponse("forbidden", status_code=403)

    call = (
        await session.execute(select(Call).where(Call.call_sid == CallSid))
    ).scalar_one_or_none()
    if call is None:
        log.info("transfer_callback", result="unknown_call_sid", call_sid=CallSid)
        return PlainTextResponse("ok")

    outcome = (DialCallStatus or "").strip().lower()
    log.info("transfer_callback", call_id=str(call.id), call_sid=CallSid,
             tenant_id=str(call.tenant_id), dial_status=outcome,
             transfer_state=call.transfer_state.value)

    if outcome in ("answered", "completed"):
        changed = await transfer_service.mark_transfer_connected(session, call)
        # A human actually picked up. Emitted here rather than on call
        # completion because "was transferred" and "the transfer connected"
        # are different facts, and a CRM that conflates them tells the
        # business somebody spoke to the customer when nobody did.
        tenant = await session.get(Tenant, call.tenant_id)
        await crm_hooks.on_transfer_completed(session, tenant, call)
    elif outcome in ("busy", "no-answer", "failed", "canceled", "cancelled"):
        changed = await transfer_service.mark_transfer_failed(session, call, outcome)
    else:
        changed = False
        log.warning("invalid_transfer_state", reason="unknown_dial_status",
                    call_id=str(call.id), dial_status=outcome)

    await session.commit()

    # Realtime: transfers are the moment a wallboard most needs live — and
    # only a state that genuinely CHANGED is announced (a retried callback
    # makes mark_transfer_* return False, which is the same idempotency the
    # transcript events already honour).
    if outcome in ("answered", "completed"):
        if changed:
            await realtime_events.emit_call_event(
                call, kind="call.transfer", extra={"transfer_outcome": "connected"}
            )
    elif outcome in ("busy", "no-answer", "failed", "canceled", "cancelled"):
        if changed:
            await realtime_events.emit_call_event(
                call, kind="call.transfer", extra={"transfer_outcome": outcome}
            )

    # Empty TwiML: let the rest of the original <Dial> verb's document run
    # (our transfer TwiML falls through to voicemail when nobody answers).
    return PlainTextResponse("<Response/>", media_type="application/xml")


async def _push_to_crm(tenant: Tenant, call: Call) -> None:
    """Fire-and-forget CRM sync. Logged, never raised."""
    payload = crm.build_payload(
        tenant_name=tenant.name,
        call_id=str(call.id),
        direction=call.direction.value,
        from_number=call.from_number,
        to_number=call.to_number,
        duration_seconds=call.duration_seconds,
        intent=call.intent,
        summary=call.summary,
        booked=call.booked,
        escalated=call.escalated,
        lead_score=call.lead_score,
        recording_url=call.recording_url,
    )
    ok = await crm.push(
        webhook_url=tenant.crm_webhook_url,
        payload=payload,
        crm_type=tenant.crm_type,
        api_key=tenant.crm_api_key,
    )
    log.info("crm.sync", call=str(call.id), ok=ok)
```

==============================================================================
===== FILE: tests/test_realtime_publisher.py (282 lines, sha256 4c3f71acc630dd1d5380df485bcdbe5b5418e4d2680353c4120acb7c0dccd0bb) =====
==============================================================================
```python
"""
Realtime publisher, in isolation: the never-raises contract, the ingest
request shape, event-id determinism, and the no-PII payload boundary.

No database and no network are involved — httpx.MockTransport stands in for
the gateway, and the module-global publish client is swapped for one backed
by that transport (the global exists precisely so tests can do this without
reaching into httpx internals).
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import httpx
import pytest

from app.core.config import settings
from app.db.models import (
    Call,
    CallDirection,
    CallStatus,
    TransferState,
)
from app.realtime import events, publisher


# ---------------------------------------------------------------- helpers ---


def _enable_realtime(monkeypatch, *, url="http://gateway:8790", secret="s" * 32):
    monkeypatch.setattr(settings, "realtime_gateway_url", url, raising=False)
    monkeypatch.setattr(
        settings, "realtime_gateway_ingest_secret", secret, raising=False
    )


def _swap_client(monkeypatch, handler):
    """Install a MockTransport-backed client; return a recorder of requests."""
    recorder = []

    def tracking_handler(request: httpx.Request) -> httpx.Response:
        recorder.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(tracking_handler))
    # `raising=False`: the global is created lazily, so it may not exist yet.
    monkeypatch.setattr(publisher, "_client", client, raising=False)
    return recorder


def _call(**overrides) -> Call:
    """An in-memory Call row with every content field set — so a payload that
    leaks ANY of them fails the PII test, not just the ones the author thought
    to check."""
    call = Call(
        tenant_id=uuid.uuid4(),
        call_sid="CA" + uuid.uuid4().hex[:30],
        from_number="+15551112222",
        to_number="+15553334444",
        status=CallStatus.IN_PROGRESS,
        direction=CallDirection.INBOUND,
        started_at=datetime(2026, 9, 16, 10, 0, 0, tzinfo=timezone.utc),
    )
    call.id = uuid.uuid4()
    call.summary = "Customer wants a boiler replaced before Friday"
    call.recording_url = "https://api.twilio.com/recordings/REdeadbeef"
    call.transfer_state = TransferState.DIALING
    call.lead_score = 42
    call.escalated = True
    for key, value in overrides.items():
        setattr(call, key, value)
    return call


def _published_json(request):
    return json.loads(request.content)


# ------------------------------------------------------------- enablement ---


async def test_disabled_without_a_gateway_url_silently_does_nothing(monkeypatch):
    _enable_realtime(monkeypatch, url="")
    recorded = _swap_client(
        monkeypatch,
        lambda request: (_ for _ in ()).throw(AssertionError("must not publish")),
    )

    assert publisher.enabled() is False
    ok = await publisher.publish_event(
        tenant_id="t", room="calls", kind="call.updated", payload={}
    )
    assert ok is False
    assert recorded == []


async def test_disabled_without_a_secret_silently_does_nothing(monkeypatch):
    _enable_realtime(monkeypatch, secret="")
    recorded = _swap_client(
        monkeypatch,
        lambda request: (_ for _ in ()).throw(AssertionError("must not publish")),
    )

    assert publisher.enabled() is False
    ok = await publisher.publish_event(
        tenant_id="t", room="calls", kind="call.updated", payload={}
    )
    assert ok is False
    assert recorded == []


# ------------------------------------------------------------ publish path --


async def test_publish_ok_posts_the_ingest_contract(monkeypatch):
    _enable_realtime(monkeypatch, secret="top-secret-ingest-token-123456")
    recorded = _swap_client(
        monkeypatch, lambda request: httpx.Response(200, json={"delivered": 2})
    )

    ok = await publisher.publish_event(
        tenant_id="tenant-1",
        room="calls",
        kind="call.updated",
        payload={"call_id": "c1", "status": "completed"},
        event_id="0f8fad5b-d9cb-469f-a165-70867728950e",
    )

    assert ok is True
    assert len(recorded) == 1
    request = recorded[0]
    assert request.method == "POST"
    assert request.url.path == "/ingest/v1/publish"
    assert request.headers["authorization"] == "Bearer top-secret-ingest-token-123456"
    body = _published_json(request)
    assert body == {
        "tenant_id": "tenant-1",
        "room": "calls",
        "kind": "call.updated",
        "payload": {"call_id": "c1", "status": "completed"},
        "event_id": "0f8fad5b-d9cb-469f-a165-70867728950e",
    }


async def test_duplicate_acknowledgement_counts_as_success(monkeypatch):
    """Gateway 200 + duplicate=true means "already happened once" — the exact
    outcome the deterministic event id is FOR. It must be a success."""
    _enable_realtime(monkeypatch)
    _swap_client(monkeypatch, lambda request: httpx.Response(200, json={"duplicate": True}))

    ok = await publisher.publish_event(
        tenant_id="t", room="calls", kind="call.updated",
        payload={}, event_id="0f8fad5b-d9cb-469f-a165-70867728950e",
    )
    assert ok is True


@pytest.mark.parametrize("status_code", [401, 422, 500])
async def test_rejections_and_gateway_errors_return_false(monkeypatch, status_code):
    _enable_realtime(monkeypatch)
    _swap_client(monkeypatch, lambda request: httpx.Response(status_code))

    ok = await publisher.publish_event(
        tenant_id="t", room="calls", kind="call.updated", payload={}
    )
    assert ok is False


async def test_a_gateway_timeout_returns_false_and_never_raises(monkeypatch):
    _enable_realtime(monkeypatch)

    def raise_timeout(request):
        raise httpx.ConnectTimeout("gateway unreachable")

    _swap_client(monkeypatch, raise_timeout)
    ok = await publisher.publish_event(
        tenant_id="t", room="calls", kind="call.updated", payload={}
    )
    assert ok is False


# --------------------------------------------------------------- payloads ---


def test_call_payload_excludes_every_content_field():
    call = _call(status=CallStatus.COMPLETED, duration_seconds=95)
    payload = events.call_payload(call)
    rendered = json.dumps(payload)

    for forbidden_fragment in (
        "from_number", "to_number", "summary", "transcript", "recording_url",
        "+15551112222", "+15553334444", "boiler", "REdeadbeef",
    ):
        assert forbidden_fragment not in rendered, forbidden_fragment

    # ...while keeping exactly the routing + display facts a wallboard needs.
    assert payload["call_id"] == str(call.id)
    assert payload["call_sid"] == call.call_sid
    assert payload["status"] == "completed"
    assert payload["direction"] == "inbound"
    assert payload["duration_seconds"] == 95
    assert payload["lead_score"] == 42
    assert payload["transfer_state"] == "dialing"
    assert payload["started_at"] == "2026-09-16T10:00:00+00:00"
    assert payload["ended_at"] is None


async def test_emit_publishes_to_both_rooms_with_per_room_event_ids(monkeypatch):
    _enable_realtime(monkeypatch)
    recorded = _swap_client(
        monkeypatch, lambda request: httpx.Response(200, json={"delivered": 1})
    )
    call = _call()

    ok = await events.emit_call_event(call, kind="call.updated")

    assert ok is True
    assert len(recorded) == 2
    rooms = {_published_json(r)["room"] for r in recorded}
    assert rooms == {"calls", f"call:{call.id}"}
    ids = [_published_json(r)["event_id"] for r in recorded]
    # Per-room ids differ — a partial outage that delivered ONE room must
    # stay retryable for the other.
    assert ids[0] != ids[1]
    # Both are gateway-valid UUIDs (the ingest schema requires them).
    for event_id in ids:
        uuid.UUID(event_id)


async def test_event_ids_are_deterministic_and_state_material(monkeypatch):
    """uuid5 over <call_id>:<kind>:<scope>:<status>:<transfer_state>, pinned to
    the documented namespace — a regression here silently breaks replay
    suppression across API restarts."""
    _enable_realtime(monkeypatch)
    recorded = _swap_client(monkeypatch, lambda request: httpx.Response(200))
    call = _call(status=CallStatus.COMPLETED)
    call.transfer_state = None

    await events.emit_call_event(call, kind="call.updated")
    await events.emit_call_event(call, kind="call.updated")

    assert len(recorded) == 4  # 2 emissions × 2 rooms
    first_pair = [_published_json(r) for r in recorded[:2]]
    second_pair = [_published_json(r) for r in recorded[2:]]
    for first, second in zip(first_pair, second_pair):
        assert first["event_id"] == second["event_id"]

    namespace = uuid.UUID("7e9f6d3a-2b1c-4f5e-9a8d-0c1b2a3f4e5d")
    expected_all = str(
        uuid.uuid5(namespace, f"{call.id}:call.updated:all:completed:none")
    )
    by_room = {b["room"]: b["event_id"] for b in first_pair}
    assert by_room["calls"] == expected_all


async def test_extra_outcome_rides_along_inside_the_payload(monkeypatch):
    _enable_realtime(monkeypatch)
    recorded = _swap_client(monkeypatch, lambda request: httpx.Response(200))
    call = _call(transfer_state=TransferState.CONNECTED)

    await events.emit_call_event(call, kind="call.transfer", extra={"transfer_outcome": "connected"})

    body = _published_json(recorded[0])
    assert body["payload"]["transfer_outcome"] == "connected"
    assert body["payload"]["transfer_state"] == "connected"


async def test_one_failed_room_degrades_the_whole_event(monkeypatch):
    _enable_realtime(monkeypatch)
    attempts = []

    def flaky(request):
        attempts.append(request)
        # First room fine, second room refused (partial outage).
        return httpx.Response(200 if len(attempts) == 1 else 500)

    _swap_client(monkeypatch, flaky)
    ok = await events.emit_call_event(_call(), kind="call.updated")
    assert ok is False
    assert len(attempts) == 2
```

==============================================================================
===== FILE: tests/test_realtime_wiring.py (173 lines, sha256 4fe3c2a9931c7f140c3cc2c6ae309b360e60c04f8b8c1ea8196e170a50812fbd) =====
==============================================================================
```python
"""
Realtime emission points, wired into the real webhook routes.

The routes run against the real router stack and a real database; only the
publish call itself is patched out (a recorder). What is asserted is the
wiring contract, not the network: which kind is announced, how many times,
and — for the callback routes, which Twilio retries aggressively — that a
retried callback still announces exactly once.
"""
from __future__ import annotations

import pytest

from app.db.models import CallStatus, TransferState
from tests.conftest import make_tenant
from tests.test_call_callbacks import post_dial, post_status
from tests.test_e2e_guard import post_voice
from tests.test_transfer import seed_call, tenant_with_human


class EmissionRecorder:
    """Stands in for events.emit_call_event; records (kind, extra) per call."""

    def __init__(self):
        self.calls = []

    async def __call__(self, call, *, kind, extra=None):
        self.calls.append(
            {"call_id": str(call.id), "kind": kind, "extra": extra or {}}
        )
        return True

    def kinds(self):
        return [c["kind"] for c in self.calls]


@pytest.fixture
def emissions(monkeypatch):
    recorder = EmissionRecorder()
    # The handler holds a module reference (`from app.realtime import events
    # as realtime_events`), so patching the attribute on the module object is
    # what the routes actually call.
    monkeypatch.setattr(
        "app.realtime.events.emit_call_event", recorder, raising=False
    )
    return recorder


# ---------------------------------------------------------------- /voice ----


async def test_a_new_incoming_call_announces_call_created_once(client, db, emissions):
    tenant = await make_tenant(db, "Realtime Co")

    resp = await post_voice(
        client, "CAaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa01", "+15559990000", tenant.twilio_number
    )
    assert resp.status_code == 200
    assert emissions.kinds() == ["call.created"]


async def test_a_redelivered_voice_webhook_does_not_reannounce(client, db, emissions):
    """Twilio retries webhooks; a retry hits the 'already exists' branch and
    must not double-animate a wallboard."""
    tenant = await make_tenant(db, "Realtime Co")

    for _ in range(2):
        resp = await post_voice(
            client, "CAbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb02", "+15559990000", tenant.twilio_number
        )
        assert resp.status_code == 200

    assert emissions.kinds() == ["call.created"]


# --------------------------------------------------------------- /status ----


async def test_an_applied_status_change_announces_call_updated(client, db, emissions):
    tenant = await make_tenant(db, "Realtime Co")
    call = await seed_call(db, tenant)

    resp = await post_status(client, call.call_sid, "completed")

    assert resp.status_code == 200
    assert emissions.kinds() == ["call.updated"]


async def test_duplicate_completed_callbacks_announce_call_updated_exactly_once(
    client, db, emissions
):
    """Mirrors test_duplicate_completed_callbacks_bill_the_minutes_once from
    the money side: idempotency isn't real until the realtime side honours it
    too."""
    tenant = await make_tenant(db, "Realtime Co")
    call = await seed_call(db, tenant)

    for _ in range(2):
        resp = await post_status(client, call.call_sid, "completed")
        assert resp.status_code == 200

    assert emissions.kinds() == ["call.updated"]


async def test_an_unknown_callsid_announces_nothing(client, db, emissions):
    resp = await post_status(client, "CAnonexistent000000000000000000", "completed")

    assert resp.status_code == 200
    assert emissions.kinds() == []


# ------------------------------------------------------- /transfer-status ---


async def test_a_connected_transfer_announces_call_transfer(client, db, emissions):
    tenant = await tenant_with_human(db)
    call = await seed_call(
        db, tenant, status=CallStatus.IN_PROGRESS, transfer_state=TransferState.DIALING
    )

    resp = await post_dial(client, call.call_sid, "completed")

    assert resp.status_code == 200
    assert emissions.kinds() == ["call.transfer"]
    assert emissions.calls[0]["extra"]["transfer_outcome"] == "connected"


async def test_a_busy_transfer_announces_the_failure_outcome(client, db, emissions):
    tenant = await tenant_with_human(db)
    call = await seed_call(
        db, tenant, status=CallStatus.IN_PROGRESS, transfer_state=TransferState.DIALING
    )

    resp = await post_dial(client, call.call_sid, "busy")

    assert resp.status_code == 200
    assert emissions.kinds() == ["call.transfer"]
    assert emissions.calls[0]["extra"]["transfer_outcome"] == "busy"


async def test_a_retried_dial_callback_annotates_exactly_once(client, db, emissions):
    """mark_transfer_* returning False on a duplicate must also suppress the
    second announcement — realtime duplicates are display bugs, not data bugs,
    but they are still bugs."""
    tenant = await tenant_with_human(db)
    call = await seed_call(
        db, tenant, status=CallStatus.IN_PROGRESS, transfer_state=TransferState.DIALING
    )

    for _ in range(2):
        resp = await post_dial(client, call.call_sid, "busy")
        assert resp.status_code == 200

    assert emissions.kinds() == ["call.transfer"]


async def test_disabled_realtime_keeps_webhooks_untouched(client, db, monkeypatch):
    """The off-by-default path: with REALTIME_GATEWAY_URL unset, the real
    emission path runs end to end, the publisher's enabled() gate returns
    False before any network client even exists, and the webhook contract is
    exactly what it was. Nothing here is patched — that IS the assertion."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "realtime_gateway_url", "", raising=False)
    monkeypatch.setattr(
        settings, "realtime_gateway_ingest_secret", "", raising=False
    )

    tenant = await make_tenant(db, "Realtime Co")
    resp = await post_voice(
        client, "CAcccccccccccccccccccccccccccccc03", "+15559990000", tenant.twilio_number
    )
    assert resp.status_code == 200
```

==============================================================================
===== FILE: dashboard/src/lib/realtime.js (267 lines, sha256 ec94f4bdfc9793fc51b07b7126317f74c08f1a8e5ebbf85bc1198a6662e3e1c5) =====
==============================================================================
```js
/**
 * Realtime client: the dashboard's WebSocket edge.
 *
 * One durable connection to the gateway carries every live fact the UI needs
 * (calls appearing, statuses changing, transfers resolving). The model is
 * NOTICE, not data: a delivery says "call X changed to state Y" with
 * near-contentless fields, and the detail views re-read through the
 * authenticated REST API if they need more. That mirrors the server side,
 * where the publish path deliberately excludes phone numbers and transcripts.
 *
 * **Auth.** Browsers cannot set headers on a WebSocket, so the JWT goes in
 * the hello frame — which is exactly what the gateway's `reader.go` expects
 * before anything else. The token is obtained from api.js (`getAccessToken`),
 * never from storage; when the gateway closes with 1008 (reason
 * `token_expired` or `authentication failed`) the client rotates the token
 * once via `refreshAccessToken()` and reconnects immediately. A rotation that
 * also fails means the session is over: the client stops and waits for an
 * explicit `connect()` (e.g. after the next login) rather than hammering.
 *
 * **Reconnect.** Any other close — network flap, gateway restart, proxy
 * timeout — retries on exponential backoff with jitter. Subscriptions are
 * remembered and re-sent after every successful `ready`, so a page that
 * subscribed once never has to know a reconnect happened.
 */

import { getAccessToken, refreshAccessToken } from './api'

/** Closed room vocabulary, mirroring app/realtime/events.py and the gateway. */
export const ROOM_CALLS = 'calls'
export const roomCall = (callId) => `call:${callId}`

/**
 * Where the socket lives. Same origin as the page — Caddy routes exactly one
 * public path (/realtime/ws → gateway /ws), so there is no configuration to
 * keep in sync and nothing cross-origin to leak the token to.
 */
export function defaultGatewayUrl(loc = window.location) {
  const scheme = loc.protocol === 'https:' ? 'wss' : 'ws'
  return `${scheme}://${loc.host}/realtime/ws`
}

const CLOSE_POLICY = 1008 // gateway: auth failed / timed out / room refused

export class RealtimeClient {
  /**
   * @param {object} opts
   * @param {(event: {room, kind, payload, eventId, sentAt}) => void} opts.onEvent
   * @param {(state: string) => void} [opts.onState]
   *        'connecting' | 'ready' | 'reconnecting' | 'unauthorized' | 'closed'
   * @param {(url: string) => WebSocket} [opts.socketFactory]  test seam
   * @param {{initialMs?: number, maxMs?: number, jitter?: () => number}} [opts.backoff]
   *        jitter() returns [0,1); effective delay is delay * (1 - jitter*0.5)
   */
  constructor({
    url = defaultGatewayUrl(),
    onEvent,
    onState = () => {},
    socketFactory = (u) => new WebSocket(u),
    backoff = {},
  } = {}) {
    if (typeof onEvent !== 'function') {
      throw new TypeError('RealtimeClient requires an onEvent handler')
    }
    this._url = url
    this._onEvent = onEvent
    this._onState = onState
    this._socketFactory = socketFactory
    this._backoff = {
      initialMs: backoff.initialMs ?? 1000,
      maxMs: backoff.maxMs ?? 30000,
      jitter: backoff.jitter ?? (() => Math.random()),
    }

    this._rooms = new Set()
    this._socket = null
    this._ready = false
    this._closedByUs = false
    this._timer = null
    this._attempts = 0
    this.state = 'closed'
    // Deliveries issued before hello completes are deduped per connection by
    // the gateway; across reconnects an id may reappear (we asked again), so
    // the dedupe ring resets with each socket.
    this._seen = new Set()
  }

  /** Begin (or schedule) the connection. Idempotent. */
  connect() {
    if (this._closedByUs === false && (this._socket || this._timer)) return
    this._closedByUs = false
    void this._open()
  }

  /** Permanent shutdown: cancel timers, close the socket, keep no state. */
  close() {
    this._closedByUs = true
    if (this._timer) {
      clearTimeout(this._timer)
      this._timer = null
    }
    const socket = this._socket
    this._socket = null
    this._ready = false
    if (socket && socket.readyState <= 1) socket.close(1000)
    this._setState('closed')
  }

  /**
   * Watch a room. Remembered across reconnects; sent immediately when the
   * connection is already ready.
   */
  subscribe(room) {
    this._rooms.add(room)
    if (this._ready) this._send({ type: 'subscribe', room })
  }

  unsubscribe(room) {
    this._rooms.delete(room)
    if (this._ready) this._send({ type: 'unsubscribe', room })
  }

  get roomList() {
    return [...this._rooms]
  }

  // ------------------------------------------------------------ internals ---

  _setState(state) {
    if (this.state !== state) {
      this.state = state
      this._onState(state)
    }
  }

  async _open() {
    if (this._closedByUs) return
    this._setState(this._attempts === 0 ? 'connecting' : 'reconnecting')

    // No token yet (page just loaded, login pending): don't open a socket we
    // already know will be refused — retry the whole attempt on backoff.
    if (!getAccessToken()) {
      const renewed = await refreshAccessToken()
      if (!renewed) {
        // Not signed in is not a server problem, and the user is about to
        // log in through the normal page flow; retry quietly, slowly.
        this._scheduleReconnect()
        return
      }
    }

    let socket
    try {
      socket = this._socketFactory(this._url)
    } catch {
      this._scheduleReconnect()
      return
    }
    this._socket = socket
    this._seen = new Set()

    socket.onopen = () => {
      this._send({ type: 'hello', token: getAccessToken() ?? '' })
    }
    socket.onmessage = (message) => this._handleFrame(message)
    socket.onerror = () => {
      // onclose always follows; handling errors there keeps one code path.
    }
    socket.onclose = (event) => this._handleClose(event)
  }

  _handleFrame(message) {
    let frame
    try {
      frame = JSON.parse(message.data)
    } catch {
      return // a non-JSON frame is not part of this protocol; ignore
    }
    switch (frame.type) {
      case 'welcome':
        // Informational (auth timeout, heartbeat cadence). Nothing to do —
        // hello is sent on open and timeouts are enforced server-side.
        break
      case 'ready':
        this._ready = true
        this._attempts = 0
        this._setState('ready')
        for (const room of this._rooms) this._send({ type: 'subscribe', room })
        break
      case 'delivery':
        if (frame.event_id) {
          if (this._seen.has(frame.event_id)) return
          this._seen.add(frame.event_id)
          if (this._seen.size > 1024) {
            // Bound the ring: a wallboard runs for weeks on one connection.
            this._seen = new Set([...this._seen].slice(-512))
          }
        }
        this._onEvent({
          room: frame.room,
          kind: frame.kind,
          payload: frame.payload ?? {},
          eventId: frame.event_id ?? null,
          sentAt: frame.sent_at ?? null,
        })
        break
      case 'error':
        // 'auth_failed' etc. are always followed by a close frame (that is
        // the gateway's whole close-flow discipline), so the close handler
        // remains the single decision point.
        break
      case 'pong':
      case 'subscribed':
      case 'unsubscribed':
        break
      default:
        break // forward-compatible: unknown frames are ignorable
    }
  }

  async _handleClose(event) {
    this._socket = null
    this._ready = false
    if (this._closedByUs) {
      this._setState('closed')
      return
    }
    const authFailure =
      event.code === CLOSE_POLICY &&
      (event.reason === 'token_expired' || event.reason === 'authentication failed')
    if (authFailure) {
      const renewed = await refreshAccessToken()
      if (renewed) {
        // Fresh token: retry NOW, not after backoff — the moment a
        // wallboard's token expires mid-shift is the moment it most needs
        // to come straight back.
        this._attempts = 0
        void this._open()
        return
      }
      // The session is genuinely over. The next failed REST call already
      // routes the user through onUnauthorized; we just stop consuming.
      this._setState('unauthorized')
      return
    }
    this._scheduleReconnect()
  }

  _scheduleReconnect() {
    if (this._closedByUs) return
    this._attempts += 1
    const base = Math.min(
      this._backoff.initialMs * 2 ** Math.max(0, this._attempts - 1),
      this._backoff.maxMs
    )
    const delay = Math.round(base * (1 - this._backoff.jitter() * 0.5))
    this._setState('reconnecting')
    this._timer = setTimeout(() => {
      this._timer = null
      void this._open()
    }, delay)
  }

  _send(frame) {
    const socket = this._socket
    if (socket && socket.readyState === 1) socket.send(JSON.stringify(frame))
  }
}
```

==============================================================================
===== FILE: dashboard/src/lib/api.js (385 lines, sha256 fd955642d171d78581470851b045d24927eb70339a412109c37b3a7749f43038, no trailing newline in the file) =====
==============================================================================
```js
/**
 * The API client.
 *
 * Everything that talks to the backend goes through `request()`. That is the
 * point of the file: the audit found fetch logic inlined in `App.jsx` with no
 * shared error handling, so the second page would have re-invented it.
 *
 * **Token storage.** The access token lives in a module-level variable —
 * never localStorage, never sessionStorage, never a cookie JavaScript can
 * read. Anything reachable from JS is reachable from an XSS payload. The cost
 * is that a refresh loses it, which is exactly why the refresh token is an
 * HttpOnly cookie: `bootstrap()` trades it for a new access token on load.
 * This was already right before STEP 8 and is carried forward unchanged.
 *
 * **One retry, then out.** A 401 rotates the token once and replays. If that
 * fails we log out. There is no path that can loop.
 */

const BASE = '/api'

let accessToken = null
let onUnauthorized = () => {}

export function setUnauthorizedHandler(fn) {
  onUnauthorized = fn
}

export function isAuthenticated() {
  return accessToken !== null
}

function clearToken() {
  accessToken = null
}

export class ApiError extends Error {
  constructor(status, message, detail = null) {
    super(message)
    this.status = status
    this.detail = detail
  }

  /** Whether retrying the same request could plausibly succeed. */
  get retryable() {
    return this.status === 0 || this.status === 429 || this.status >= 500
  }
}

/**
 * Human-facing text for a status code.
 *
 * Requirement 21 and 22: never show a raw backend exception. The server's
 * `detail` is used when it is a plain string, because FastAPI validation
 * messages are written for people; a dict detail is a structured error and is
 * summarised instead of stringified.
 */
function friendlyMessage(status, detail) {
  if (typeof detail === 'string' && detail.trim()) return detail
  if (detail && typeof detail === 'object' && typeof detail.message === 'string') {
    return detail.message
  }
  return {
    0: 'Could not reach the server. Check your connection and try again.',
    400: 'That request could not be processed.',
    401: 'Your session has expired. Please sign in again.',
    403: 'You do not have permission to do that.',
    404: 'That item could not be found.',
    409: 'That conflicts with something that already exists.',
    422: 'Some of the details were not valid.',
    429: 'Too many requests. Please wait a moment and try again.',
    502: 'A third-party service is unavailable right now.',
    503: 'The service is temporarily unavailable.',
    504: 'That took too long. Please try again.',
  }[status] || 'Something went wrong. Please try again.'
}

async function readError(resp) {
  try {
    const data = await resp.json()
    return data.detail ?? null
  } catch {
    return null
  }
}

async function request(path, { method = 'GET', body, retry = true, raw } = {}) {
  const headers = {}
  if (!raw) headers['Content-Type'] = 'application/json'
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`

  let resp
  try {
    resp = await fetch(path, {
      method,
      headers,
      credentials: 'same-origin',      // carries the HttpOnly refresh cookie
      body: raw ?? (body === undefined ? undefined : JSON.stringify(body)),
    })
  } catch {
    // Network failure, DNS, offline. Status 0 so callers can offer a retry
    // rather than showing "undefined".
    throw new ApiError(0, friendlyMessage(0))
  }

  // An expired 15-minute access token is normal. Rotate once, then replay.
  if (resp.status === 401 && retry && accessToken) {
    const renewed = await tryRefresh()
    if (renewed) return request(path, { method, body, raw, retry: false })
    clearToken()
    onUnauthorized()
    throw new ApiError(401, friendlyMessage(401))
  }

  if (resp.status === 401) {
    clearToken()
    onUnauthorized()
    throw new ApiError(401, friendlyMessage(401))
  }

  if (!resp.ok) {
    const detail = await readError(resp)
    throw new ApiError(resp.status, friendlyMessage(resp.status, detail), detail)
  }

  return resp.status === 204 ? null : resp.json()
}

/** Append only the query parameters that have a value. */
export function query(params = {}) {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue
    search.set(key, String(value))
  }
  const encoded = search.toString()
  return encoded ? `?${encoded}` : ''
}

// --------------------------------------------------------------- session ---

export async function login(email, password) {
  const resp = await fetch('/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify({ email, password }),
  })
  if (!resp.ok) {
    // The server deliberately returns one generic message; do not embellish
    // it here or we would reintroduce user enumeration in the UI.
    const detail = await readError(resp)
    throw new ApiError(resp.status, friendlyMessage(resp.status, detail))
  }
  const data = await resp.json()
  accessToken = data.access_token
  return data.user
}

async function tryRefresh() {
  try {
    const resp = await fetch('/auth/refresh', {
      method: 'POST',
      credentials: 'same-origin',
    })
    if (!resp.ok) return false
    accessToken = (await resp.json()).access_token
    return true
  } catch {
    return false
  }
}

/**
 * The current in-memory access token, for the one consumer allowed to see it
 * beyond `request()`: the realtime WebSocket client, which must put the token
 * in its hello frame (browsers cannot set headers on a WebSocket). There is
 * deliberately NO setter export — the token stays write-private to this
 * module, so the only writers remain login/refresh/logout.
 */
export function getAccessToken() {
  return accessToken
}

/**
 * Trade the HttpOnly refresh cookie for a new access token, for consumers
 * outside the request() retry loop (again: the realtime socket, when the
 * gateway closes an expired-token connection with 1008). Returns false when
 * the session is genuinely over — callers must NOT call onUnauthorized
 * themselves; the next failed API request already does that once.
 */
export async function refreshAccessToken() {
  return tryRefresh()
}

/** On page load, exchange the HttpOnly refresh cookie for a session. */
export async function bootstrap() {
  if (!(await tryRefresh())) return null
  try {
    return await request('/auth/me')
  } catch {
    clearToken()
    return null
  }
}

export async function logout() {
  try {
    await request('/auth/logout', { method: 'POST' })
  } catch {
    // A failed logout must still clear the local session. The refresh cookie
    // is HttpOnly and the server will reject a stale one anyway.
  } finally {
    clearToken()
    onUnauthorized()
  }
}

export const getMe = () => request('/auth/me')

// ------------------------------------------------------------- analytics ---
//
// `range` is a preset name or `start`/`end` are local dates. The *server*
// resolves both against the tenant's timezone -- requirement 5 forbids doing
// period arithmetic in the browser, and two people in different timezones
// must see the same dashboard.

export const getOverview = (range) =>
  request(`${BASE}/analytics/overview${query(range)}`)
export const getCallAnalytics = (range) =>
  request(`${BASE}/analytics/calls${query(range)}`)
export const getConversion = (range) =>
  request(`${BASE}/analytics/conversion${query(range)}`)
export const getUsageAnalytics = () => request(`${BASE}/analytics/usage`)

// ----------------------------------------------------------------- calls ---

export const listCalls = (tenantId, params) =>
  request(`${BASE}/tenants/${tenantId}/calls${query(params)}`)
export const getCall = (callId) => request(`${BASE}/calls/${callId}`)
export const getTranscript = (callId) => request(`${BASE}/calls/${callId}/transcript`)
export const getTransfer = (callId) => request(`${BASE}/calls/${callId}/transfer`)

// ----------------------------------------------------------------- leads ---

export const listLeads = (tenantId, params) =>
  request(`${BASE}/tenants/${tenantId}/leads${query(params)}`)
export const createLeads = (tenantId, leads) =>
  request(`${BASE}/tenants/${tenantId}/leads`, { method: 'POST', body: { leads } })
export const markLeadDnc = (tenantId, leadId) =>
  request(`${BASE}/tenants/${tenantId}/leads/${leadId}/do-not-call`, { method: 'POST' })

// ---------------------------------------------------------- appointments ---

export const listAppointments = (params) =>
  request(`${BASE}/appointments${query(params)}`)
export const getAppointment = (id) => request(`${BASE}/appointments/${id}`)
export const cancelAppointment = (id, reason) =>
  request(`${BASE}/appointments/${id}/cancel`, { method: 'POST', body: { reason } })
export const rescheduleAppointment = (id, startsAtLocal, reason = '') =>
  request(`${BASE}/appointments/${id}`, {
    method: 'PATCH',
    body: { starts_at_local: startsAtLocal, reason },
  })
export const getAvailability = (day) =>
  request(`${BASE}/appointments/availability${query({ day })}`)

// ------------------------------------------------------------- campaigns ---

export const listCampaigns = (tenantId) =>
  request(`${BASE}/tenants/${tenantId}/campaigns`)
export const runCampaign = (tenantId, campaignId) =>
  request(`${BASE}/tenants/${tenantId}/campaigns/${campaignId}/run`, { method: 'POST' })

// ------------------------------------------------------------- knowledge ---

export const listDocuments = (params) =>
  request(`${BASE}/knowledge/documents${query(params)}`)
export const getDocument = (id) => request(`${BASE}/knowledge/documents/${id}`)
export const knowledgeStats = () => request(`${BASE}/knowledge/stats`)
export const reindexDocument = (id) =>
  request(`${BASE}/knowledge/documents/${id}/reindex`, { method: 'POST' })
export const restoreDocument = (id) =>
  request(`${BASE}/knowledge/documents/${id}/restore`, { method: 'POST' })
export const deleteDocument = (id, hard = false) =>
  request(`${BASE}/knowledge/documents/${id}${query({ hard })}`, { method: 'DELETE' })

export function uploadDocument(file, title) {
  // Multipart, so `request` must not set a JSON content type -- the browser
  // has to supply its own boundary.
  const form = new FormData()
  form.append('file', file)
  if (title) form.append('title', title)
  return request(`${BASE}/knowledge/documents`, { method: 'POST', raw: form })
}

// ---------------------------------------------------------- integrations ---

export const listCrmIntegrations = () => request(`${BASE}/integrations/crm`)
export const crmProviders = () => request(`${BASE}/integrations/crm/providers`)
export const listCrmSyncs = (params) =>
  request(`${BASE}/integrations/crm/syncs${query(params)}`)
export const testCrmIntegration = (provider) =>
  request(`${BASE}/integrations/crm/${provider}/test`, { method: 'POST' })
export const saveCrmIntegration = (provider, body) =>
  request(`${BASE}/integrations/crm/${provider}`, { method: 'PUT', body })
export const disconnectCrmIntegration = (provider) =>
  request(`${BASE}/integrations/crm/${provider}/disconnect`, { method: 'POST' })
// Distinct from disconnect: DELETE removes the row, disconnect only drops
// the credentials and keeps the configuration. Returns 204.
export const deleteCrmIntegration = (provider) =>
  request(`${BASE}/integrations/crm/${provider}`, { method: 'DELETE' })

export const listCalendarIntegrations = () => request(`${BASE}/calendar/integrations`)
export const calendarProviders = () => request(`${BASE}/calendar/providers`)
export const testCalendarIntegration = (provider) =>
  request(`${BASE}/calendar/integrations/${provider}/test`, { method: 'POST' })
export const saveCalendarIntegration = (provider, body) =>
  request(`${BASE}/calendar/integrations/${provider}`, { method: 'PUT', body })
export const deleteCalendarIntegration = (provider) =>
  request(`${BASE}/calendar/integrations/${provider}`, { method: 'DELETE' })
export const getSchedulingPolicy = () => request(`${BASE}/calendar/policy`)
export const saveSchedulingPolicy = (body) =>
  request(`${BASE}/calendar/policy`, { method: 'PUT', body })

// --------------------------------------------------------------- billing ---

export const getBilling = () => request(`${BASE}/billing`)
export const getPlans = () => request(`${BASE}/billing/plans`)
export const getUsage = () => request(`${BASE}/billing/usage`)
export const getInvoices = () => request(`${BASE}/billing/invoices`)
export const startCheckout = (planCode, interval = 'month') =>
  request(`${BASE}/billing/checkout`, {
    method: 'POST',
    // Plan code and interval only. There is no price, amount or currency
    // field -- the server resolves the price from its catalogue, and the
    // request model would reject anything else anyway.
    body: { plan_code: planCode, interval },
  })
export const openPortal = () => request(`${BASE}/billing/portal`, { method: 'POST' })
export const changePlan = (planCode, interval) =>
  request(`${BASE}/billing/change-plan`, {
    method: 'POST',
    body: { plan_code: planCode, interval },
  })
export const cancelSubscription = (immediately, reason) =>
  request(`${BASE}/billing/cancel`, {
    method: 'POST',
    body: { immediately, reason },
  })
// Re-reads the provider and rebuilds the usage summary; returns the refreshed
// BillingStatusOut. No body -- the tenant comes from the token.
export const reconcileBilling = () =>
  request(`${BASE}/billing/reconcile`, { method: 'POST' })

// ------------------------------------------------------------------ team ---

export const listUsers = () => request(`${BASE}/team/users`)
// The RBAC policy itself (`describe_roles()`): every role with its level and
// full permission list, so the dashboard never hard-codes the role table.
export const getRoles = () => request('/auth/roles')
export const createUser = (body) =>
  request(`${BASE}/team/users`, { method: 'POST', body })
export const setUserRole = (userId, role) =>
  request(`${BASE}/team/users/${userId}/role`, { method: 'PATCH', body: { role } })
export const setUserActive = (userId, isActive) =>
  request(`${BASE}/team/users/${userId}/active`, {
    method: 'PATCH',
    body: { is_active: isActive },
  })
export const listAudit = (params) => request(`${BASE}/team/audit${query(params)}`)

// ------------------------------------------------------------------ agent ---
//
// `getAgentConfig` is the read half of an API that was previously write-only:
// `PATCH .../voice` has always existed, but nothing could read the greeting,
// the prompt or the model back. The response never contains a credential --
// see `AgentConfigOut` in `app/api/routes.py`.

export const getAgentConfig = (tenantId) =>
  request(`${BASE}/tenants/${tenantId}/agent`)
export const listTenants = () => request(`${BASE}/tenants`)
export const listLanguages = () => request(`${BASE}/languages`)
export const listPresets = () => request(`${BASE}/llm/presets`)
export const updateVoice = (tenantId, body) =>
  request(`${BASE}/tenants/${tenantId}/voice`, { method: 'PATCH', body })
```

==============================================================================
===== FILE: dashboard/vite.config.js (36 lines, sha256 0b17fdbff1ae8fdb2e9c995efbe7f6190dc27fb1a33a629e83845b358b56f66d, no trailing newline in the file) =====
==============================================================================
```js
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    // Vite 6 rejects unknown Host headers. Local dev is unaffected; this only
    // lets the app be reached through a hosted preview/tunnel domain.
    allowedHosts: true,
    // Browser calls /api -> vite proxies to FastAPI. Never hardcode localhost in the browser.
    // /auth is proxied too, so the HttpOnly refresh cookie stays same-origin.
    // /realtime/ws upgrades to the websocket gateway — the entry mirrors the
    // production Caddy rule (path rewritten to /ws) so dev and prod see the
    // same wire from the client's seat.
    proxy: {
      '/api': 'http://localhost:8000',
      '/auth': 'http://localhost:8000',
      '/realtime/ws': {
        target: 'http://localhost:8790',
        ws: true,
        rewrite: (path) => path.replace(/^\/realtime\/ws/, '/ws'),
      },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./tests/setup.js'],
    include: ['tests/**/*.test.{js,jsx}'],
    // The XSS-boundary test greps the real source tree, so it must not be
    // confused by a build directory.
    exclude: ['node_modules', 'dist'],
    restoreMocks: true,
  },
})
```

==============================================================================
===== FILE: dashboard/tests/realtime.test.js (308 lines, sha256 d580f47fd1f8707a2a7d67a4803eeb318443845b02a49fef3cf54f69825c8901) =====
==============================================================================
```js
/**
 * Realtime client: handshake shape, delivery dispatch, dedupe, token
 * rotation on a 1008 policy close, and reconnect discipline — pinned against
 * a FakeWebSocket so the wire frames are asserted literally, the same frames
 * the Go gateway's protocol package documents.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// The client talks to api.js only through these two exports. The harness
// drives a hoisted state object so "login happened" / "session over" are
// flippable mid-test.
const auth = vi.hoisted(() => ({
  token: 'token-a',
  refreshOk: true,
  refreshCalls: 0,
  rotatedToken: 'token-b',
}))

vi.mock('../src/lib/api', () => ({
  getAccessToken: () => auth.token,
  refreshAccessToken: async () => {
    auth.refreshCalls += 1
    if (!auth.refreshOk) return false
    auth.token = auth.rotatedToken
    return true
  },
}))

import { RealtimeClient, ROOM_CALLS, defaultGatewayUrl, roomCall } from '../src/lib/realtime'

class FakeWebSocket {
  static instances = []
  constructor(url) {
    this.url = url
    this.sent = []
    this.readyState = 0 // CONNECTING
    this.onopen = null
    this.onmessage = null
    this.onclose = null
    this.onerror = null
    FakeWebSocket.instances.push(this)
  }
  send(data) {
    this.sent.push(JSON.parse(data))
  }
  close(code = 1000, reason = '') {
    this.readyState = 3
    this.onclose?.({ code, reason })
  }
  // ---- test drivers (what the real gateway would do) ----
  open() {
    this.readyState = 1
    this.onopen?.()
  }
  receive(frame) {
    this.onmessage?.({ data: JSON.stringify(frame) })
  }
  drop(code = 1006, reason = '') {
    this.readyState = 3
    this.onclose?.({ code, reason })
  }
}

const FAST = { initialMs: 100, maxMs: 400, jitter: () => 0 }

function makeClient(overrides = {}) {
  const events = []
  const states = []
  const client = new RealtimeClient({
    url: 'ws://test/realtime/ws',
    socketFactory: (url) => new FakeWebSocket(url),
    backoff: FAST,
    onEvent: (event) => events.push(event),
    onState: (state) => states.push(state),
    ...overrides,
  })
  return { client, events, states }
}

/** Flush microtasks: the auth-refresh paths in _open/_handleClose are async.
 * Deliberately microtask-based — fake timers would eat a setTimeout(0). */
async function flush() {
  for (let i = 0; i < 8; i += 1) await Promise.resolve()
}

beforeEach(() => {
  FakeWebSocket.instances = []
  auth.token = 'token-a'
  auth.refreshOk = true
  auth.refreshCalls = 0
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('URL mapping', () => {
  it('maps the page scheme to the single public gateway path', () => {
    expect(defaultGatewayUrl({ protocol: 'https:', host: 'app.example.com' }))
      .toBe('wss://app.example.com/realtime/ws')
    expect(defaultGatewayUrl({ protocol: 'http:', host: 'localhost:5173' }))
      .toBe('ws://localhost:5173/realtime/ws')
  })
})

describe('handshake and subscriptions', () => {
  it('sends hello with the access token as the first frame', () => {
    const { client } = makeClient()
    client.connect()
    const socket = FakeWebSocket.instances[0]
    expect(socket.url).toBe('ws://test/realtime/ws')
    socket.open()
    expect(socket.sent).toEqual([{ type: 'hello', token: 'token-a' }])
  })

  it('flushes remembered subscriptions only after the ready frame', () => {
    const { client } = makeClient()
    client.subscribe(ROOM_CALLS)
    client.connect()
    const socket = FakeWebSocket.instances[0]
    socket.open()
    socket.receive({ type: 'welcome', auth_required: true })
    // Before ready, no subscribe frame may leak out (the gateway would treat
    // it as unauthenticated traffic).
    expect(socket.sent).toEqual([{ type: 'hello', token: 'token-a' }])
    socket.receive({ type: 'ready', session_id: 's1', tenant_id: 't1', role: 'owner' })
    expect(socket.sent[1]).toEqual({ type: 'subscribe', room: 'calls' })
  })

  it('subscribes immediately when the connection is already ready', () => {
    const { client } = makeClient()
    client.connect()
    const socket = FakeWebSocket.instances[0]
    socket.open()
    socket.receive({ type: 'ready', session_id: 's1', tenant_id: 't1', role: 'owner' })
    client.subscribe(roomCall('abc-123'))
    expect(socket.sent.at(-1)).toEqual({ type: 'subscribe', room: 'call:abc-123' })
    client.unsubscribe(roomCall('abc-123'))
    expect(socket.sent.at(-1)).toEqual({ type: 'unsubscribe', room: 'call:abc-123' })
  })
})

describe('deliveries', () => {
  function readyClient() {
    const harness = makeClient()
    harness.client.subscribe(ROOM_CALLS)
    harness.client.connect()
    const socket = FakeWebSocket.instances[0]
    socket.open()
    socket.receive({ type: 'ready', session_id: 's1', tenant_id: 't1', role: 'owner' })
    return { ...harness, socket }
  }

  it('hands parsed delivery frames to onEvent', () => {
    const { client, events, socket } = readyClient()
    socket.receive({
      type: 'delivery', room: 'calls', kind: 'call.updated',
      payload: { call_id: 'c-1', status: 'completed' },
      event_id: 'e-111', sent_at: '2026-09-16T10:00:00Z',
    })
    expect(events).toEqual([{
      room: 'calls',
      kind: 'call.updated',
      payload: { call_id: 'c-1', status: 'completed' },
      eventId: 'e-111',
      sentAt: '2026-09-16T10:00:00Z',
    }])
    client.close()
  })

  it('drops exact duplicate event ids on the same connection', () => {
    const { client, events, socket } = readyClient()
    const frame = {
      type: 'delivery', room: 'calls', kind: 'call.updated',
      payload: { call_id: 'c-1' }, event_id: 'e-same',
    }
    socket.receive(frame)
    socket.receive(frame)
    expect(events).toHaveLength(1)
    client.close()
  })

  it('delivers the same logical event per room without collapsing it', () => {
    const { client, events, socket } = readyClient()
    // List page and detail page both subscribed: the gateway gives each room
    // its own event id ON PURPOSE (per-room replay windows), so the client
    // must NOT dedupe across rooms.
    socket.receive({
      type: 'delivery', room: 'calls', kind: 'call.updated',
      payload: { call_id: 'c-1', status: 'completed' }, event_id: 'e-list',
    })
    socket.receive({
      type: 'delivery', room: 'call:c-1', kind: 'call.updated',
      payload: { call_id: 'c-1', status: 'completed' }, event_id: 'e-detail',
    })
    expect(events.map((event) => event.room)).toEqual(['calls', 'call:c-1'])
    client.close()
  })

  it('ignores malformed frames instead of dying', () => {
    const { client, events, socket } = readyClient()
    socket.onmessage({ data: 'this is not json{' })
    socket.receive({ type: 'something-new', future: true })
    expect(events).toEqual([])
    expect(client.state).toBe('ready')
    client.close()
  })
})

describe('token rotation on policy close', () => {
  async function readySocket() {
    const harness = makeClient()
    harness.client.subscribe(ROOM_CALLS)
    harness.client.connect()
    const socket = FakeWebSocket.instances[0]
    socket.open()
    socket.receive({ type: 'ready', session_id: 's1', tenant_id: 't1', role: 'owner' })
    return { ...harness, socket }
  }

  it('1008 token_expired refreshes once and reconnects with the new token', async () => {
    const { client, socket, states } = await readySocket()
    socket.drop(1008, 'token_expired')
    await flush()

    expect(auth.refreshCalls).toBe(1)
    expect(FakeWebSocket.instances).toHaveLength(2)
    const next = FakeWebSocket.instances[1]
    next.open()
    expect(next.sent[0]).toEqual({ type: 'hello', token: 'token-b' })
    // Subscriptions are replayed on the new connection after ready.
    next.receive({ type: 'ready', session_id: 's2', tenant_id: 't1', role: 'owner' })
    expect(next.sent[1]).toEqual({ type: 'subscribe', room: 'calls' })
    expect(states).toContain('ready')
    client.close()
  })

  it('a failed rotation stops instead of hammering', async () => {
    auth.refreshOk = false
    const { client, socket, states } = await readySocket()
    socket.drop(1008, 'authentication failed')
    await flush()

    expect(auth.refreshCalls).toBe(1)
    expect(FakeWebSocket.instances).toHaveLength(1)
    vi.advanceTimersByTime(60_000)
    expect(FakeWebSocket.instances).toHaveLength(1)
    expect(client.state).toBe('unauthorized')
    expect(states).toContain('unauthorized')
    client.close()
  })
})

describe('reconnect discipline', () => {
  it('an ordinary network drop retries on the configured backoff', async () => {
    const { client, states } = makeClient()
    client.connect()
    const socket = FakeWebSocket.instances[0]
    socket.open()
    socket.receive({ type: 'ready', session_id: 's1', tenant_id: 't1', role: 'owner' })

    socket.drop(1006, '')
    expect(client.state).toBe('reconnecting')
    expect(FakeWebSocket.instances).toHaveLength(1)

    vi.advanceTimersByTime(99)
    expect(FakeWebSocket.instances).toHaveLength(1)
    vi.advanceTimersByTime(1)
    expect(FakeWebSocket.instances).toHaveLength(2)
    expect(states).toEqual(
      expect.arrayContaining(['connecting', 'ready', 'reconnecting'])
    )
    client.close()
  })

  it('close() is final: no timer can resurrect the socket', async () => {
    const { client } = makeClient()
    client.connect()
    const socket = FakeWebSocket.instances[0]
    socket.open()
    socket.drop(1006, '')
    client.close()
    vi.advanceTimersByTime(300_000)
    expect(FakeWebSocket.instances).toHaveLength(1)
    expect(client.state).toBe('closed')
  })

  it('with no token yet it retries quietly without opening a refused socket', async () => {
    auth.token = null
    auth.refreshOk = false
    const { client } = makeClient()
    client.connect()
    await flush()
    expect(auth.refreshCalls).toBe(1)
    expect(FakeWebSocket.instances).toHaveLength(0)
    // Login completes on the next retry tick: a socket appears and hello
    // carries whatever token is current then.
    auth.token = 'token-login'
    vi.advanceTimersByTime(100)
    await flush()
    expect(FakeWebSocket.instances).toHaveLength(1)
    const socket = FakeWebSocket.instances[0]
    socket.open()
    expect(socket.sent[0]).toEqual({ type: 'hello', token: 'token-login' })
    client.close()
  })
})
```

==============================================================================
===== FILE: docs/REALTIME.md (186 lines, sha256 8138df0b105abe6b21bf82958044061108597aa5666772c88458d140cd870c49) =====
==============================================================================
```markdown
# Realtime events (dashboard live updates)

VoxDesk pushes **notices about calls** to the dashboard over a durable
WebSocket: a call was created, a call changed state, a transfer resolved.
This document is the whole story — what moves, over which wire, with which
guarantees, and what to set where.

## The three moving parts

```
┌────────────┐  POST /ingest/v1/publish   ┌──────────────────┐  WS /realtime/ws   ┌────────────┐
│  API (this │ ─────────────────────────► │ realtime-gateway │ ─────────────────► │ dashboard  │
│  repo,     │  Bearer ingest secret      │ (Go,             │  hello {token:JWT} │ (realtime. │
│  app/      │  after every durable       │  services/       │  then subscribe    │  js)       │
│  realtime/ │  call fact commits         │  realtime/       │  frames)           │            │
└────────────┘                            │  gateway-go/)    │                    └────────────┘
                                          └──────────────────┘
```

1. **Publisher** — `app/realtime/`. Emits one event per *logical change*,
   after the database commit that made it real. Never raises, never blocks
   the webhook response path, and stays silent when unconfigured.
2. **Gateway** — `services/realtime/gateway-go/`. A standalone Go service:
   authenticates dashboard browsers against the same JWT the API issues,
   validates ingest payloads against a closed room/kind vocabulary,
   suppresses replays, fans out to subscribers. It knows nothing about
   calls — the payload is an opaque envelope.
3. **Client** — `dashboard/src/lib/realtime.js`. Keeps one socket up,
   rotates the token when the gateway says so (close `1008`), replaying
   subscriptions after every reconnect.

## Event kinds and the payload contract

| kind           | emitted when                                              | extra fields        |
| -------------- | --------------------------------------------------------- | ------------------- |
| `call.created` | `/telephony/voice` created a genuinely new call row       | —                   |
| `call.updated` | `/telephony/status` *applied* a state change              | —                   |
| `call.transfer`| the `<Dial>` callback *changed* the transfer state        | `transfer_outcome`  |

The payload is **routing + display facts only**: `call_id`, `call_sid`,
`status`, `direction`, `duration_seconds`, `booked`, `escalated`,
`lead_score`, `transfer_state`, `started_at`, `ended_at`. Deliberately
absent — and asserted absent by `tests/test_realtime_publisher.py`: phone
numbers, summary, transcript text, recording URLs. A realtime frame learns
*that* a call changed; the detail views re-read the content through the
authenticated REST API. Treat every frame as "refetch signal with enough
metadata to animate a list."

## Rooms

| room           | who subscribes                        | receives                       |
| -------------- | ------------------------------------- | ------------------------------ |
| `calls`        | wallboards, call list pages           | every call event for the tenant|
| `call:<uuid>`  | a call detail page                    | events for that one call only  |

The room namespace is closed and validated at the gateway (`calls`,
`metrics`, `call:<uuid>`, `campaign:<uuid>`); an arbitrary client-declared
room is refused with a 422-class error. Each logical event is published to
both rooms independently, with **different event ids per room** — that is
intentional, so a partial publish (one room delivered, the other refused by
a transient error) stays retryable for the missed room.

## Exactly-once, as far as it goes

* **DB → gateway:** the API derives `event_id = uuid5(namespace,
  "<call_id>:<kind>:<room_scope>:<status>:<transfer_state>")`. The id is
  deterministic, so a crash between commit and publish — replayed on boot or
  by an operator — is dropped by the gateway's (tenant, event_id) replay
  cache (TTL 30 min, 50k entries).
* **Webhooks → DB:** duplicates never produce an event. A retried Twilio
  callback fails `result.applied` / `mark_transfer_*`'s changed-flag, which
  is the same flag that gates emission. `tests/test_realtime_wiring.py`
  pins this: two identical callbacks, exactly one frame.
* **Gateway → dashboard:** at-most-once per connection. A dropped socket may
  miss frames; the UI is expected to resync details through REST on reconnect
  (the client's `ready` → resubscribe moment is a good trigger).

## Configuration

### API (`app/core/config.py`)

| env                                | default           | meaning                                        |
| ---------------------------------- | ----------------- | ---------------------------------------------- |
| `REALTIME_GATEWAY_URL`             | *(empty = off)*   | gateway base URL; the API appends `/ingest/v1/publish` |
| `REALTIME_GATEWAY_INGEST_SECRET`   | *(empty = off)*   | shared Bearer secret for ingest                |
| `REALTIME_PUBLISH_TIMEOUT_SECONDS` | `1.5`             | hard per-publish timeout                       |

Either field without the other is a **startup refusal** in production
(half-configured realtime fails more confusingly than none). Placeholder or
`< 16`-char secrets and non-http(s) URLs are refused too. With both empty,
the feature is cleanly off.

### Gateway (excerpt — full table in the gateway README)

| env                               | meaning                                             |
| --------------------------------- | --------------------------------------------------- |
| `VOXDESK_GATEWAY_JWT_SECRET`      | **same secret the API signs access tokens with**    |
| `VOXDESK_GATEWAY_JWT_ISSUER` / `…_AUDIENCE` | must match the API's `JWT_ISSUER`/`JWT_AUDIENCE` |
| `VOXDESK_GATEWAY_INGEST_SECRET`   | must equal the API's `REALTIME_GATEWAY_INGEST_SECRET` |
| `VOXDESK_GATEWAY_ALLOWED_ORIGINS` | exact browser origins allowed to open `/ws`         |
| `VOXDESK_GATEWAY_METRICS_TOKEN`   | Bearer for `/metrics`; empty = ungated (warns)      |

### Deploy wiring (already in this repo)

* `docker-compose.prod.yml` — `realtime-gateway` service (host port
  loopback-only, `127.0.0.1:8790`), and the API gets
  `REALTIME_GATEWAY_URL=http://realtime-gateway:8790` plus the required
  `${REALTIME_GATEWAY_INGEST_SECRET:?}` interpolation, so a missing secret
  fails `compose up` before anything boots half-configured.
* `Caddyfile` — one public route: `handle /realtime/ws` → rewrite to `/ws` →
  `realtime-gateway:8790`. Ingest, metrics, and health endpoints have **no**
  public path. Caddy passes the WebSocket upgrade through unchanged.
* `observability/prometheus.yml` — `voxdesk-realtime-gateway` job. When
  `METRICS_TOKEN` is set (the standard production posture), uncomment the
  `authorization.credentials` line with the same token; otherwise the target
  401s, which is at least loud.
* `dashboard/vite.config.js` — dev proxy for `/realtime/ws` →
  `localhost:8790` with the same `/ws` rewrite, so dev and prod see the
  identical wire.

## Wire protocol (summary)

Client → gateway: `{"type":"hello","token":"<JWT>"}` must be first
(`ping` is tolerated before it), then `{"type":"subscribe","room":"calls"}`,
`{"type":"unsubscribe",…}`, `{"type":"ping"}`.

Gateway → client: `welcome` (timeouts), `ready` (tenant bound, expires-at),
`subscribed`/`unsubscribed`, `delivery {room, kind, payload, event_id,
sent_at}`, `error {code, message}`, `pong`. Auth problems end as close code
**1008** with reason `token_expired` or `authentication failed` — the JS
client treats that pair as "rotate once, reconnect immediately" and anything
else as backoff. See `internal/protocol/protocol.go` for the normative
definition.

## Failure semantics

| failure                          | what happens                                                        |
| -------------------------------- | ------------------------------------------------------------------- |
| gateway down / unreachable       | publishes return `False` and log `realtime.publish_failed` (type only, never the secret); webhooks respond exactly as before; dashboards reconnect on backoff |
| gateway 401s ingest              | `realtime.publish_rejected` with the status — means the two secrets differ |
| dashboard token expires mid-socket | gateway closes 1008/`token_expired`; client refreshes once and comes back immediately |
| API session truly over           | subscription attempts stop after one failed refresh; the next REST 401 drives the login flow |
| publish path throws anything     | logged, swallowed — realtime must never 500 a telephony webhook     |

## Scoped honestly

* Fan-out is **single-process**: all dashboards must reach the same gateway
  instance (sticky-less horizontal scaling is future work; capacity ceilings
  are in the gateway README).
* Token **revocation** (`token_version`) is checked on REST calls, not per
  WebSocket frame: a socket lives until the JWT's own `exp`, at which point
  the gateway closes it. Maximum exposure equals the access-token lifetime
  (15 minutes by default).
* Events are ephemeral — no event store, no catch-up. Reconnecting pages
  resync through REST.

### A second traffic class rides the same socket: signaling

The gateway ALSO speaks a signaling plane (WebRTC session setup:
`session.start/join/end`, SDP offer/answer and ICE candidate relays between
two same-tenant connections). It shares the authentication, the connection
caps and the socket lifecycle with the notice plane, but it is NOT part of
the event pipeline described above: nothing on it is published from the API,
and its frames never flow through `app/realtime/`. The dashboard client in
`src/lib/realtime.js` handles only notice-plane frames; signaling frames
would arrive as ignored unknown types until a consumer adds dispatching.
See the gateway README's signaling tables for the wire vocabulary and
guarantees (glare guard, capability session ids, collapsed unknowns).

## Verifying it locally

```bash
# 1. gateway tests
cd services/realtime/gateway-go && go test -race ./...

# 2. publisher + wiring tests (API side)
/usr/local/bin/python3 -m pytest tests/test_realtime_publisher.py tests/test_realtime_wiring.py -q

# 3. dashboard client tests
cd dashboard && npx vitest run tests/realtime.test.js
```

For a live loop: run the gateway with dev secrets, set `REALTIME_GATEWAY_URL`
+ `REALTIME_GATEWAY_INGEST_SECRET` in `.env`, run the API and dashboard, and
watch the dashboard's network tab hold one `wss://…/realtime/ws` while calls
arrive from Twilio's test console.
```

==============================================================================
===== FILE: .github/workflows/ci.yml (112 lines, sha256 bdff1f28ad3760b494701672ee23c297b012a6cff8871af51dff076150e88f79) =====
==============================================================================
```yaml
name: ci

# The default gate for every push and pull request. Real external-provider
# tests are DELIBERATELY excluded (see real-integrations.yml) — nothing in
# this file may touch the network beyond dependency installation.
on:
  push:
    branches: [main]
  pull_request:

concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true

jobs:
  backend:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16-alpine
        env:
          POSTGRES_USER: voxdesk
          POSTGRES_PASSWORD: voxdesk-ci
          POSTGRES_DB: voxdesk_ci
        ports: ["5432:5432"]
        options: >-
          --health-cmd "pg_isready -U voxdesk"
          --health-interval 5s --health-timeout 5s --health-retries 10
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip

      - name: Install dependencies
        run: |
          pip install -r requirements.txt
          pip install pytest pytest-asyncio aiosqlite httpx ruff

      - name: Lint
        run: ruff check app scripts tests

      # Migration round trip against REAL Postgres: upgrade to head, all the
      # way back to base, and up again. A migration that cannot go backwards
      # fails release here, not during a 3am rollback.
      - name: Migration round trip
        run: |
          alembic upgrade head
          alembic downgrade base
          alembic upgrade head
        env:
          DATABASE_URL: postgresql+asyncpg://voxdesk:voxdesk-ci@localhost:5432/voxdesk_ci

      # Everything except the opt-in real-provider marker.
      - name: Tests
        run: python -m pytest -q -m "not real_provider"

  gateway:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-go@v5
        with:
          go-version: "1.27"
          cache-dependency-path: services/realtime/gateway-go/go.sum

      - name: Go tests (with the race detector)
        working-directory: services/realtime/gateway-go
        run: |
          go vet ./...
          go test -race -count=1 ./...

  frontend:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-node@v4
        with:
          node-version: 20
          cache: npm
          cache-dependency-path: dashboard/package-lock.json

      - name: Install, test, build
        working-directory: dashboard
        run: |
          npm ci
          npm test
          npm run build

      # Shipped dependencies only; the accepted dev-only Vitest advisory is
      # documented in docs/DEPLOYMENT.md §10.
      - name: Audit production dependencies
        working-directory: dashboard
        run: npm audit --omit=dev --audit-level=high

  image:
    needs: [backend, gateway, frontend]
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      # Full-context build: if .dockerignore regresses and a secret can reach
      # the build, this step is where it would surface.
      - name: Build the API image
        run: docker build . -t voxdesk-api:ci

      - name: Build the realtime gateway image
        run: docker build services/realtime/gateway-go -t voxdesk-realtime-gateway:ci
```

==============================================================================
===== FILE: .github/workflows/security-scan.yml (57 lines, sha256 f69b11c547a003f2fdac15c053d62de6125e48392e2cb7e18ab57618e2e23069) =====
==============================================================================
```yaml
name: security-scan

# Supply-chain and static analysis. Runs alongside CI on every push/PR and
# weekly on a schedule (dependency CVEs appear after a commit, not at it).
on:
  push:
    branches: [main]
  pull_request:
  schedule:
    - cron: "0 6 * * 1"   # Mondays 06:00 UTC

permissions:
  contents: read

jobs:
  sast:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      # SAST: bandit only scans our code (app/, scripts/) — never vendored
      # dependencies, so it stays fast and actionable.
      - name: Bandit (Python SAST)
        run: |
          pip install bandit
          bandit -r app scripts -q

  dependency-audit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      # pip-audit with the documented accepted-risk list: the script is the
      # single source of truth for WHICH findings are accepted and why
      # (docs/SECURITY.md), so CI and `make`-less local runs agree.
      - name: pip-audit (Python dependency CVEs)
        run: |
          pip install pip-audit
          bash scripts/audit_dependencies.sh

  secret-scan:
    runs-on: ubuntu-latest
    steps:
      # gitleaks with full history: a secret deleted in a later commit is
      # still a leaked secret.
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: gitleaks
        uses: gitleaks/gitleaks-action@v2
        env:
          GITLEAKS_ENABLE_COMMENTS: "true"
```

==============================================================================
===== FILE: .github/workflows/real-integrations.yml (47 lines, sha256 28f7484e9e593cf8af445d4a123526f8cfd661ddafb54bc0ce5a4e3b80b914c8) =====
==============================================================================
```yaml
name: real-integrations

# Real external-provider tests (Twilio, Deepgram, ElevenLabs, LLM keys,
# Stripe test mode). These are NEVER part of the default CI path: they cost
# money, need live credentials, and their flakiness must not gate normal
# development. Run them manually before a release or when touching a provider
# client. The tests themselves double-check the opt-in flag and skip without
# it, so even a misfire of this workflow is inert.
on:
  workflow_dispatch:
    inputs:
      reason:
        description: Why this run (e.g. 'pre-release', 'twilio webhook refactor')
        required: false
        default: manual check

jobs:
  real-providers:
    runs-on: ubuntu-latest
    environment: real-provider-tests     # hold the secrets behind a GitHub environment
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip

      - name: Install dependencies
        run: |
          pip install -r requirements.txt
          pip install pytest pytest-asyncio aiosqlite httpx

      # ONLY the marked tests: `-m real_provider`, guarded again by the
      # environment switch the tests themselves require.
      - name: Real-provider suite
        run: python -m pytest -q -m real_provider
        env:
          VOXDESK_REAL_INTEGRATION: "1"
          TWILIO_ACCOUNT_SID: ${{ secrets.REAL_TEST_TWILIO_ACCOUNT_SID }}
          TWILIO_AUTH_TOKEN: ${{ secrets.REAL_TEST_TWILIO_AUTH_TOKEN }}
          TWILIO_PHONE_NUMBER: ${{ secrets.REAL_TEST_TWILIO_PHONE_NUMBER }}
          DEEPGRAM_API_KEY: ${{ secrets.REAL_TEST_DEEPGRAM_API_KEY }}
          ELEVENLABS_API_KEY: ${{ secrets.REAL_TEST_ELEVENLABS_API_KEY }}
          OPENAI_API_KEY: ${{ secrets.REAL_TEST_OPENAI_API_KEY }}
          ANTHROPIC_API_KEY: ${{ secrets.REAL_TEST_ANTHROPIC_API_KEY }}
          GOOGLE_API_KEY: ${{ secrets.REAL_TEST_GOOGLE_API_KEY }}
```

==============================================================================
===== FILE: .dockerignore (44 lines, sha256 dc2981ba41f8f0bba412921b2b6d9c4f580251654abab46bdefae36c94e13962) =====
==============================================================================
```bash
# Build-context hygiene for the API image (docker build .). Two purposes:
#
# 1. Secrets must never enter an image layer or its build cache — .env,
#    secrets/, backups, keys of any stripe. docker-compose.prod.yml mounts
#    ./secrets read-only at runtime precisely because baking them in is
#    wrong.
# 2. Cache control: dependency caches, test artifacts and VCS metadata
#    change all the time and would bust image layers for nothing.
#
# The realtime gateway image has its own build context
# (services/realtime/gateway-go/) and is unaffected by this file.

.git/
.gitignore
.dockerignore

.env
.env.*
!.env.example
!.env.staging.example
secrets/
backups/
recordings/
*.dump
*.pem
*.key
.netrc

__pycache__/
*.pyc
.pytest_cache/
.ruff_cache/
.mypy_cache/
.venv/

node_modules/
dashboard/dist/
dashboard/node_modules/

tests/
docs/
VOXDESK-CODE-REVIEW.md
services/realtime/
go/
```

==============================================================================
===== FILE: .env.staging.example (68 lines, sha256 2fa862b14aab14b406adb2bb99ab905afbb8ba8ae5d9bed4cc2ce43b92032cfd) =====
==============================================================================
```bash
# Copy to .env.staging and fill in REAL values (placeholders only here — the
# test suite asserts this file can never carry production-looking keys).
# Staging mirrors production's .env.example with the differences that make
# staging staging: non-production posture, its own database password, and the
# manual real-call E2E defaults. Never copy .env from production into here.

# ---------- App ----------
APP_ENV=staging

# Compose REQUIRES this (the :? interpolation fails loudly when unset).
POSTGRES_PASSWORD=change-me-staging-only
GRAFANA_ADMIN_PASSWORD=change-me-staging-only

# ---------- Auth / JWT ----------
# Staging MUST use its own secret material, not a copy of production's: a
# production JWT must be meaningless on staging and vice versa. Generate
# with: openssl rand -hex 32
JWT_SECRET=insecure-staging-only-change-me
JWT_ISSUER=voxdesk
JWT_AUDIENCE=voxdesk-api
ACCESS_TOKEN_MINUTES=15
REFRESH_TOKEN_DAYS=14
MAX_FAILED_LOGINS=8
LOCKOUT_MINUTES=15

CORS_ORIGINS=http://localhost:5173

# ---------- Database (overridden by the compose environment block) ----------
DATABASE_URL=postgresql+asyncpg://voxdesk:change-me-staging-only@localhost:5432/voxdesk

# ---------- Twilio ----------
# Load the staging/test numbers, NOT the production account, so an accidental
# prod webhook cannot land here. Staging is where signature drills run.
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=change-me
TWILIO_PHONE_NUMBER=+15550001111

# ---------- Speech to Text ----------
DEEPGRAM_API_KEY=xxxxxxxx
DEEPGRAM_MODEL=nova-3

# ---------- LLM ----------
OPENAI_API_KEY=sk-xxxxxxxx
ANTHROPIC_API_KEY=sk-ant-xxxxxxxx
GOOGLE_API_KEY=AIzaxxxxxxxx
DEFAULT_LLM_PRESET=natural

# ---------- Text to Speech ----------
ELEVENLABS_API_KEY=xxxxxxxx
ELEVENLABS_VOICE_ID=21m00Tcm4TlvDq8ikWAM
ELEVENLABS_MODEL=eleven_flash_v2_5

# ---------- Google Calendar ----------
GOOGLE_CREDENTIALS_JSON=./secrets-staging/google_service_account.json

# ---------- Realtime gateway ----------
REALTIME_GATEWAY_URL=http://localhost:8790
REALTIME_GATEWAY_INGEST_SECRET=insecure-staging-only-change-me
REALTIME_PUBLISH_TIMEOUT_SECONDS=1.5

# ---------- Manual real-call E2E (REFUSED in production; default off here) ----------
E2E_ENABLED=false
E2E_TEST_NUMBER=
E2E_ALLOWED_CALLERS=

# ---------- Metrics ----------
METRICS_ENABLED=true
METRICS_TOKEN=change-me
```

==============================================================================
===== FILE: .env.example (73 lines, sha256 abd49f9a5a16e85d261581d2b9223e9b394508baf75670f9e3c291f6de2b5cc5) =====
==============================================================================
```bash
# ---------- App ----------
APP_ENV=development
PUBLIC_BASE_URL=https://your-ngrok-subdomain.ngrok-free.app
SECRET_KEY=change-me

# ---------- Auth / JWT  (STEP 2) ----------
# REQUIRED in production. Generate with:  openssl rand -hex 32
# The app REFUSES TO START in production if this is left at its default.
JWT_SECRET=insecure-development-only-change-me
JWT_ISSUER=voxdesk
JWT_AUDIENCE=voxdesk-api
ACCESS_TOKEN_MINUTES=15          # short-lived; the refresh cookie renews it
REFRESH_TOKEN_DAYS=14
MAX_FAILED_LOGINS=8              # then the account locks
LOCKOUT_MINUTES=15

# Comma-separated. Wildcards are not allowed with credentialed requests.
CORS_ORIGINS=http://localhost:5173

# ---------- Database ----------
DATABASE_URL=postgresql+asyncpg://voxdesk:voxdesk@localhost:5432/voxdesk

# ---------- Twilio ----------
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_PHONE_NUMBER=+15550001111

# ---------- Speech to Text ----------
DEEPGRAM_API_KEY=xxxxxxxx
DEEPGRAM_MODEL=nova-3

# ---------- LLM: তিনটার যেকোনো একটা থাকলেই চলবে ----------
# একটার key না থাকলে কোড নিজে থেকেই পরেরটায় fallback নেবে
OPENAI_API_KEY=sk-xxxxxxxx                 # ChatGPT   (gpt-4o-mini)
ANTHROPIC_API_KEY=sk-ant-xxxxxxxx          # Claude    (claude-haiku-4-5)
GOOGLE_API_KEY=AIzaxxxxxxxx                # Gemini    (gemini-2.0-flash)

# fast = ChatGPT | natural = Claude | cheap = Gemini | smart = Claude Sonnet
DEFAULT_LLM_PRESET=natural

# ---------- Text to Speech ----------
ELEVENLABS_API_KEY=xxxxxxxx
ELEVENLABS_VOICE_ID=21m00Tcm4TlvDq8ikWAM
ELEVENLABS_MODEL=eleven_flash_v2_5

# ---------- Google Calendar ----------
GOOGLE_CREDENTIALS_JSON=./secrets/google_service_account.json

# ---------- Realtime gateway (in-app events push) ----------
# When BOTH of these are set, the API publishes call lifecycle notices
# (call.created / call.updated / call.transfer) to the gateway's ingest
# endpoint, and the dashboard receives them over WebSocket. When either is
# unset the feature degrades silently: the API skips publishing and the
# dashboard simply does not show live updates. The app validates production
# configs at startup: a URL without a secret (or vice versa), a placeholder
# or < 16-char secret, a non-http(s) URL, or a non-positive timeout all
# refuse to boot.
REALTIME_GATEWAY_URL=http://localhost:8790
REALTIME_GATEWAY_INGEST_SECRET=insecure-development-only-change-me
REALTIME_PUBLISH_TIMEOUT_SECONDS=1.5

# --- Media engine (Rust SFU) ---------------------------------------------
# Public IPv4 the engine advertises as its ICE candidate in SDP answers.
# REQUIRED in prod: with a wrong or private value browsers cannot discover
# media connectivity and every call hangs at ICE.
MEDIA_ENGINE_PUBLIC_IP=
# Per-fleet node label (entropy seed for SSRC allocation and log greps).
MEDIA_ENGINE_LABEL=edge-1
MEDIA_ENGINE_MAX_PARTICIPANTS=64
MEDIA_ENGINE_TIMEOUT_SECONDS=1.5
# Browser media path through the engine: off (default; P2P relay) | v1.2 |
# force. "force" before the dashboard speaks wire 1.2 will hang calls.
MEDIA_ENGINE_STEER=off
```

==============================================================================
===== FILE: .gitignore (42 lines, sha256 5ce0674d03632ad5b0461bd2baa4a064fc0c3195dda68bb9c132de469205bb45) =====
==============================================================================
```bash
__pycache__/
*.py[cod]
.venv/
.env
secrets/
recordings/
node_modules/
dist/
.pytest_cache/
.ruff_cache/

# Database dumps and backup artifacts: they contain the whole business,
# calls and PII included, and must never be committable even by accident.
backups/
*.dump
*.pem
*.key
.netrc

# Local tooling state.
.idea/
.vscode/
*.swp

# Realtime gateway build artifacts (Go test/build run from the repo tree).
/services/realtime/gateway-go/gateway

# Sandbox toolchain + Go module caches, and NLTK corpora: build/download artifacts,
# not source. restore-tooling.sh recreates the toolchains ($HOME/toolchains, $HOME/go-work).
toolchains/
go-work/
gop/
/go/pkg/
nltk_data/

# Rust build output (cargo check/test run from the repo tree).
/services/realtime/media-engine-rs/target/

# Control-plane build output (cargo check/test run from the repo tree) and the
# media-plane's make(1) output directory.
/services/control-plane/target/
/services/media-plane/build/
```

==============================================================================
===== FILE: Caddyfile (92 lines, sha256 00c9c21a5a55ba2e743b5df3ed6b2f33692797546d6b7f1d1b29d21249ae0d26) =====
==============================================================================
```text
# VoxDesk TLS reverse proxy (Caddy v2).
#
# Automatic HTTPS: when DOMAIN is a real public hostname, Caddy obtains and
# renews Let's Encrypt certificates with zero config and redirects HTTP -> HTTPS
# on the same site. Leave DOMAIN unset to serve plain HTTP on :80 (behind your
# own load balancer, or for a local smoke test on http://localhost:8080).
# Certificate verification is never disabled: Caddy's default ACME + cert
# validation is used unchanged.
#
# WebSockets (wss://) for the live voice stream pass through unchanged -- Caddy
# v2 upgrades WebSocket connections by default, which is what the Twilio Media
# Streams handshake depends on. Nothing in this file may interfere with that
# upgrade. Caddy also sets X-Forwarded-For / X-Forwarded-Proto / X-Forwarded-Host
# automatically, so the API (uvicorn --proxy-headers) sees the real client and
# scheme for PUBLIC_BASE_URL, wss:// and Twilio signature verification.
#
# Observability: the API's metrics endpoint is token-gated at the application
# layer (METRICS_TOKEN) and is scraped by Prometheus directly over the internal
# compose network (observability/prometheus.yml targets api:8000) -- it is NOT
# routed or exposed here. Prometheus, PostgreSQL, Redis and the scheduler have
# no public path at all.
#
# The Caddy admin API is disabled: it is a localhost control plane this
# deployment does not use, and leaving it on is an unnecessary surface.

{
	admin off
}

{$DOMAIN:localhost} {
	encode zstd gzip

	# Security headers. Strict-Transport-Security is ignored by browsers over
	# plain HTTP, so it is safe to set unconditionally; it takes effect the
	# moment the site is served over TLS. The origin (app/main.py security
	# headers middleware) sets the same headers as a second layer.
	header {
		Strict-Transport-Security "max-age=31536000; includeSubDomains"
		X-Content-Type-Options "nosniff"
		X-Frame-Options "DENY"
		Referrer-Policy "no-referrer"
	}

	# Request body ceiling. Knowledge-base uploads allow up to 20 MB
	# (KNOWLEDGE_MAX_FILE_MB), so leave headroom above that. WebSocket media
	# streams are unaffected by request_body limits.
	request_body {
		max_size 25MB
	}

	# Realtime gateway: the dashboard's durable WebSocket edge. ONE public
	# path only, /realtime/ws, rewritten to the gateway's plain /ws; the
	# gateway's HTTP surface (ingest, metrics, health) is never routed
	# publicly. Caddy v2 passes the WebSocket Upgrade hop-headers through
	# unchanged on reverse_proxy — the same property the Twilio media stream
	# depends on — so no extra transport options are set here either.
	handle /realtime/ws {
		rewrite * /ws
		reverse_proxy realtime-gateway:8790
	}

	# Everything else goes to the API on 8000 inside the compose network.
	# Two upstreams total, both enumerated above — there is still no
	# wildcard/arbitrary upstream path.
	handle {
		reverse_proxy api:8000
	}
}

# Grafana is exposed through Caddy ONLY on its explicit domain. When
# GRAFANA_DOMAIN is unset the site address falls back to `grafana.localhost`,
# which (per RFC 6761) resolves only to loopback -- so no remote client can
# route to it, and the operator's access path is the host-loopback port
# 127.0.0.1:3001. Set GRAFANA_DOMAIN=grafana.staging.example.com to serve it on
# a real hostname; any request that matches the site still requires Grafana
# sign-in (anonymous access and self-sign-up are disabled in compose).
{$GRAFANA_DOMAIN:grafana.localhost} {
	encode zstd gzip

	header {
		Strict-Transport-Security "max-age=31536000; includeSubDomains"
		X-Content-Type-Options "nosniff"
		X-Frame-Options "DENY"
		Referrer-Policy "no-referrer"
	}

	request_body {
		max_size 25MB
	}

	reverse_proxy grafana:3000
}
```

==============================================================================
===== FILE: docker-compose.prod.yml (249 lines, sha256 40a96ac5826f172afa4ffb9218bc7143280f4d201c47b4cb4f215607493eaeb7) =====
==============================================================================
```yaml
# Production compose: no source bind-mounts, no --reload, explicit healthchecks,
# persistent volumes, and the full observability + backup stack.
#
#   docker compose -f docker-compose.prod.yml up -d --build
#
# Requires a .env next to this file (see .env.example): APP_ENV=production,
# JWT_SECRET, SECRET_KEY, TWILIO_*, at least one LLM key, ELEVENLABS_API_KEY,
# DEEPGRAM_API_KEY, CRM_ENCRYPTION_KEYS, a real embedding provider,
# REALTIME_GATEWAY_INGEST_SECRET (shared by the API publisher and the
# realtime gateway), and METRICS_ENABLED=true + METRICS_TOKEN for Prometheus.
services:
  db:
    image: postgres:16-alpine
    restart: unless-stopped
    environment:
      POSTGRES_USER: ${POSTGRES_USER:-voxdesk}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-voxdesk}
      POSTGRES_DB: ${POSTGRES_DB:-voxdesk}
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER:-voxdesk}"]
      interval: 5s
      timeout: 5s
      retries: 10

  redis:
    image: redis:7-alpine
    restart: unless-stopped
    command: ["redis-server", "--appendonly", "yes"]
    volumes:
      - redisdata:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 5s
      retries: 10

  api:
    build: .
    restart: unless-stopped
    init: true                # reap uvicorn worker children (PID 1 hygiene)
    stop_grace_period: 30s    # let in-flight requests drain on SIGTERM
    env_file: .env
    environment:
      DATABASE_URL: postgresql+asyncpg://${POSTGRES_USER:-voxdesk}:${POSTGRES_PASSWORD:-voxdesk}@db:5432/${POSTGRES_DB:-voxdesk}
      REDIS_URL: redis://redis:6379/0
      # Realtime publishing: the API announces durable call facts to the
      # gateway over the internal network. The environment: block overrides
      # .env here deliberately — inside compose the publish endpoint is ALWAYS
      # the gateway service, never a localhost URL from a developer's .env.
      # The required-secret interpolation fails the whole deploy up front
      # rather than booting a silently-degraded realtime pair.
      REALTIME_GATEWAY_URL: http://realtime-gateway:8790
      REALTIME_GATEWAY_INGEST_SECRET: ${REALTIME_GATEWAY_INGEST_SECRET:?set REALTIME_GATEWAY_INGEST_SECRET in .env (>=16 random chars, shared with the realtime-gateway service)}
    ports:
      - "127.0.0.1:8000:8000"   # host-only; public traffic enters via Caddy
    volumes:
      - ./secrets:/srv/secrets:ro
      - knowledge:/srv/var/knowledge
    depends_on:
      db:
        condition: service_healthy
      redis:
        condition: service_healthy
      realtime-gateway:
        condition: service_healthy

  # Durable WebSocket edge for the dashboard (Go; see
  # services/realtime/gateway-go/README.md). Public traffic reaches it only
  # through Caddy's /realtime/ws route; the host port is loopback-only, same
  # posture as the API. JWT secret is the SAME one the API signs with — the
  # gateway verifies dashboard bearer tokens without the API being in the
  # request loop. The ingest secret is the API→gateway shared secret and must
  # match the API's REALTIME_GATEWAY_INGEST_SECRET or every publish 401s.
  realtime-gateway:
    build: ./services/realtime/gateway-go
    restart: unless-stopped
    stop_grace_period: 20s    # let close frames drain; clients reconnect + resubscribe
    environment:
      VOXDESK_GATEWAY_PORT: "8790"
      VOXDESK_GATEWAY_JWT_SECRET: ${JWT_SECRET:?set JWT_SECRET in .env — the gateway must verify the API's tokens}
      VOXDESK_GATEWAY_JWT_ISSUER: ${JWT_ISSUER:-voxdesk}
      VOXDESK_GATEWAY_JWT_AUDIENCE: ${JWT_AUDIENCE:-voxdesk-api}
      VOXDESK_GATEWAY_INGEST_SECRET: ${REALTIME_GATEWAY_INGEST_SECRET:?set REALTIME_GATEWAY_INGEST_SECRET in .env (>=16 random chars, shared with the api service)}
      # Comma-separated exact browser origins allowed to open /ws. Same rule
      # as the API's CORS_ORIGINS: no wildcards. Dashboard and API share a
      # public origin, so the production default is the site itself.
      VOXDESK_GATEWAY_ALLOWED_ORIGINS: ${REALTIME_ALLOWED_ORIGINS:-}
      # /metrics is token-gated at the gateway; leave unset only if
      # Prometheus does not scrape this service (observability/prometheus.yml
      # ships the matching credentials).
      VOXDESK_GATEWAY_METRICS_TOKEN: ${METRICS_TOKEN:-}
      # Media-engine control-plane link (Go→Rust). Readiness of THIS service
      # reflects engine availability, so a dead SFU takes the node out of the
      # load-balancer's ready pool while web sessions keep serving degraded.
      VOXDESK_GATEWAY_MEDIA_ENGINE_URL: http://media-engine:9001
      VOXDESK_GATEWAY_MEDIA_ENGINE_TIMEOUT_SECONDS: ${MEDIA_ENGINE_TIMEOUT_SECONDS:-1.5}
      # Browser media path: off (P2P relay only) | v1.2 (steer only for
      # upgraded pairs) | force (all sessions steer; do NOT set until the
      # dashboard ships wire 1.2). See services/realtime/gateway-go's
      # internal/signaling/steer.go for the full contract.
      VOXDESK_GATEWAY_ENGINE_STEER: ${MEDIA_ENGINE_STEER:-off}
    ports:
      - "127.0.0.1:8790:8790"  # host-only; public sockets enter via Caddy
    healthcheck:
      test: ["CMD", "wget", "-q", "-O", "/dev/null", "http://127.0.0.1:8790/healthz"]
      interval: 10s
      timeout: 3s
      retries: 5
      start_period: 5s
    depends_on:
      media-engine:
        condition: service_started

  # Rust media engine (SFU; see services/realtime/media-engine-rs). UDP
  # media is published on the HOST (browsers ICE against this address —
  # VOXDESK_PUBLIC_IP must be the host's public v4, set in .env); the
  # control plane is inter-container only, hit by realtime-gateway.
  media-engine:
    build: ./services/realtime/media-engine-rs
    restart: unless-stopped
    stop_grace_period: 10s
    environment:
      VOXDESK_PORT: "5000"
      VOXDESK_CONTROL_ADDR: "0.0.0.0:9001"
      VOXDESK_PUBLIC_IP: ${MEDIA_ENGINE_PUBLIC_IP:?set MEDIA_ENGINE_PUBLIC_IP in .env to the host's public IPv4 (SDP candidate)}
      VOXDESK_ENGINE_LABEL: ${MEDIA_ENGINE_LABEL:-edge-1}
      VOXDESK_MAX_PARTICIPANTS: ${MEDIA_ENGINE_MAX_PARTICIPANTS:-64}
    ports:
      - "5000:5000/udp"   # public: browser ICE targets this directly
    healthcheck:
      test: ["CMD", "/bin/bash", "-c", "exec 3<>/dev/tcp/127.0.0.1/9001 && echo -e 'GET /v1/health HTTP/1.1\r\nHost: x\r\n\r\n' >&3 && grep -q ready <&3"]
      interval: 10s
      timeout: 3s
      retries: 5
      start_period: 5s

  scheduler:
    build: .
    restart: unless-stopped
    stop_grace_period: 30s
    env_file: .env
    environment:
      DATABASE_URL: postgresql+asyncpg://${POSTGRES_USER:-voxdesk}:${POSTGRES_PASSWORD:-voxdesk}@db:5432/${POSTGRES_DB:-voxdesk}
      REDIS_URL: redis://redis:6379/0
    # Same image as the API, but run the background worker instead of the
    # entrypoint (no uvicorn, no migrations -- the API already migrated).
    entrypoint: ["python", "-m", "scripts.scheduler"]
    volumes:
      - ./secrets:/srv/secrets:ro
    depends_on:
      db:
        condition: service_healthy
      api:
        condition: service_started

  backup:
    image: postgres:16-alpine
    restart: unless-stopped
    environment:
      PGPASSWORD: ${POSTGRES_PASSWORD:-voxdesk}
      POSTGRES_USER: ${POSTGRES_USER:-voxdesk}
      POSTGRES_DB: ${POSTGRES_DB:-voxdesk}
    volumes:
      - ./backups:/backups
    entrypoint: ["/bin/sh", "-c"]
    # Nightly pg_dump at 02:00 UTC, keeping the last 14 verified dumps.
    # Each dump is verified with `pg_restore --list` before it is retained or
    # synced; a dump that fails verification is logged loudly and left out of
    # the off-site sync. Restore with scripts/restore.sh. Rclone sync
    # (optional) if RCLONE_REMOTE is set.
    command: |
      while true; do
        sleep 3600
        if [ "$$(date +%H)" = "02" ]; then
          DUMP="/backups/voxdesk-$$(date +%F).dump"
          if pg_dump -h db -U "$${POSTGRES_USER}" -Fc "$${POSTGRES_DB}" > "$${DUMP}" \
              && [ -s "$${DUMP}" ] \
              && pg_restore --list "$${DUMP}" > /dev/null 2>&1; then
            echo "backup ok: $${DUMP}"
            ls -1 /backups/*.dump | head -n -14 | xargs -r rm
            if [ -n "$${RCLONE_REMOTE:-}" ]; then
              rclone copy /backups "$${RCLONE_REMOTE}"/voxdesk-backups || true
            fi
          else
            echo "BACKUP FAILED (dump unreadable or empty); keeping previous dumps" >&2
          fi
        fi
      done
    depends_on:
      db:
        condition: service_healthy

  prometheus:
    image: prom/prometheus:v2.53.0
    restart: unless-stopped
    volumes:
      - ./observability/prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - ./observability/alerts.yml:/etc/prometheus/alerts.yml:ro
      - ./observability/slos.yml:/etc/prometheus/slos.yml:ro
      - promdata:/prometheus
    command: ["--config.file=/etc/prometheus/prometheus.yml"]
    depends_on:
      - api

  grafana:
    image: grafana/grafana:11.1.0
    restart: unless-stopped
    environment:
      GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_ADMIN_PASSWORD:-voxdesk}
      GF_SECURITY_ADMIN_USER: ${GRAFANA_ADMIN_USER:-admin}
      GF_AUTH_ANONYMOUS_ENABLED: "false"
      GF_USERS_ALLOW_SIGN_UP: "false"
      GF_USERS_ALLOW_ORG_CREATE: "false"
    volumes:
      - grafdata:/var/lib/grafana
      - ./observability/grafana/provisioning:/etc/grafana/provisioning:ro
      - ./observability/grafana/dashboards:/var/lib/grafana/dashboards:ro
    ports:
      - "127.0.0.1:3000:3000"   # host-only; public traffic enters via Caddy
    depends_on:
      - prometheus

  caddy:
    image: caddy:2.9-alpine
    restart: unless-stopped
    environment:
      DOMAIN: ${DOMAIN:-}
      GRAFANA_DOMAIN: ${GRAFANA_DOMAIN:-}
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - caddy_config:/config
    depends_on:
      - api
      - realtime-gateway

volumes:
  pgdata:
  redisdata:
  knowledge:
  promdata:
  grafdata:
  caddy_data:
  caddy_config:
```

==============================================================================
===== FILE: observability/prometheus.yml (54 lines, sha256 c483f326f5b5b027395702cf755d1c5ffe8bfa91b4d787e3f06dc0fc3de96751) =====
==============================================================================
```yaml
global:
  scrape_interval: 15s
  evaluation_interval: 15s

rule_files:
  - /etc/prometheus/alerts.yml
  - /etc/prometheus/slos.yml

scrape_configs:
  - job_name: voxdesk-api
    metrics_path: /metrics
    static_configs:
      - targets: ["api:8000"]
    # The API only serves /metrics on the internal compose network, so an
    # unauthenticated scrape is the out-of-the-box default. If you set
    # METRICS_TOKEN in the API (to expose /metrics beyond the network),
    # uncomment and point at the same token:
    #
    #   authorization:
    #     credentials: "<same value as METRICS_TOKEN>"

  # The background worker is a separate process and exposes its own /metrics
  # (job runs, last-success timestamps, stuck side effects) on
  # SCHEDULER_METRICS_PORT. Scraping it here is what makes
  # `voxdesk_job_*` and the stuck-side-effect alert actually work.
  - job_name: voxdesk-scheduler
    metrics_path: /metrics
    static_configs:
      - targets: ["scheduler:8001"]

  # The realtime gateway's /metrics is token-gated whenever
  # VOXDESK_GATEWAY_METRICS_TOKEN is set (docker-compose.prod.yml feeds it
  # the same METRICS_TOKEN as the API). With the token set — the standard
  # production posture — the scrape below must send it too, or the target
  # 401s and shows up as down. Prometheus configs cannot interpolate .env,
  # so uncomment and paste the same value:
  - job_name: voxdesk-realtime-gateway
    metrics_path: /metrics
    static_configs:
      - targets: ["realtime-gateway:8790"]
    # authorization:
    #   credentials: "<same value as METRICS_TOKEN>"

  # Rust media engine (services/realtime/media-engine-rs). Its /metrics is
  # HTTP-plain on the compose network (control port 9001, inter-container
  # only — see docker-compose.prod.yml's media-engine service). Series:
  #   voxdesk_media_udp_frames_total{kind=stun|rtp|unknown}
  #   voxdesk_media_rtcp_total{kind=sr|rr|reports_applied|unsupported|malformed}
  #   voxdesk_media_rtp_dropped_total{kind=spoof|unknown_ssrc|other}
  #   voxdesk_engine_{rooms,participants,tracks} gauges
  - job_name: voxdesk-media-engine
    metrics_path: /metrics
    static_configs:
      - targets: ["media-engine:9001"]
```

==============================================================================
===== FILE: services/README.md (104 lines, sha256 7a75c3a7f3b2f1504c728c479440bc71c9e9d3a7b20a293db1b75305ac77a1dd) =====
==============================================================================
```markdown
# services/ — polyglot workspace

This directory is the home of the non-Python services introduced by the
expansion roadmap. The wire contracts live in `contracts/proto`; each language
directory below is an independent workspace with its own build, lint and test,
invoked by the CI polyglot workflow (`.github/workflows/polyglot.yml`).

## Layout

```
services/
├── control-plane/   Rust — session state machine, SIP/SDP signaling,
│                    usage/rating pipeline, webhook fan-out   (roadmap Phase 2)
├── signal-go/       Go — real-time WebSocket signaling hub, differential
│                    counterpart of control-plane's voxdesk-signal
├── realtime/
│   └── gateway-go/  Go — PUBLIC WebSocket edge for dashboards: JWT-gated
│                    hello, tenant-pinned rooms, ingest fan-out from the API
├── media-plane/     C++ — WebRTC SFU, DTLS-SRTP, Opus jitter buffer,
│                    recording sink, transcription tap        (roadmap Phase 3)
├── ops/             Go — ops & resilience CLI (voxops), Step 13
│                    evidence parity                          (roadmap Phase 5)
└── web/             TypeScript/Next.js — admin console, wallboard,
                     WebRTC browser client                    (roadmap Phase 1,
                                                              lives in dashboard-next/)
```

Each service generates its bindings from `contracts/proto` rather than
hand-rolling message shapes, and each keeps the same invariants the Python
backend already enforces, expressed in its own language's tooling.

## The polyglot gate

1. **Tenant isolation** — every message is tenant-scoped (contract-level rule,
   enforced by `scripts/verify_contracts.py`).
2. **Idempotency / exactly-once side effects** — mirrors the database
   `UniqueConstraint(tenant_id, idempotency_key)` guarantees.
3. **Observability** — Prometheus metrics, structured logs and trace ids on
   every service from day one.
4. **No production mutation from tests** — same E2E safety posture; no real
   calls, charges or bookings from any test or tool.
5. **Reproducible builds** — pinned toolchain and dependencies per language,
   wired into CI.

## Current state and next steps

- [x] Wire contracts (`contracts/proto`) — compiled + cross-checked.
- [x] Contract verifier (`scripts/verify_contracts.py`) + contract tests
      (`tests/test_contracts.py`).
- [x] CI polyglot workflow (`contracts`, `media-plane`, `control-plane`,
      `signal-go`, `ops-go`, `dashboard-next` jobs).
- [x] `services/media-plane/` — C++ media plane (Phase 3): jitter buffer +
      tenant consistent-hash router, plus the audio/video processing modules
      (radix-2 FFT, spectral-subtraction denoiser, G.711 mu/A-law codec,
      min-statistics energy VAD); `g++` + `make`, both test binaries green
      (1175 + 130k checks).
- [x] `dashboard-next/` — Next.js/TypeScript dashboard (Phase 1): all 13
      Vite pages ported (Overview, Calls, CallDetail, Analytics, Appointments,
      Campaigns, Leads, Knowledge, Integrations, Agent, Team, Billing, Audit)
      + typed API client + strict TS; `next build` + vitest green.
- [x] `services/control-plane/` — Rust control plane (Phase 2): `voxdesk-control`
      std-only core (session/transfer state machine mirroring
      `app/telephony/call_state.py` + `transfer_service.py`, tenant-scoped
      registry, idempotency guard, bounded broadcast, interval scheduler
      mirroring `scripts/scheduler.py`, usage/metering + rating mirroring
      `app/billing/metering.py`+`plans.py`, token-bucket rate limiter,
      backoff retry) + `voxdesk-signal` (tokio + tungstenite WebSocket hub with
      tenant-scoped rooms, plus `session.proto`-shaped JSON messages in
      `protocol.rs`); `cargo fmt` + `clippy -D warnings` + 59 tests green
      (53 core incl. differential/property session-parity tests + 3 signal +
      3 in-process WebSocket hub integration tests in `tests/hub.rs`).
- [x] `services/signal-go/` — Go real-time signaling hub: same tagged-JSON
      wire protocol and tenant-scoping rules as `voxdesk-signal` (hello once,
      `hello_required`, backpressure-drop, 60 s idle reap), plus token-bucket
      rate limiter and exactly-once idempotency guard; `gofmt` + `go vet` +
      `go test -race ./...` + `go build` green (37 tests, incl. concurrent
      publish tenant-isolation smoke test).
- [x] `services/realtime/gateway-go/` — Go PUBLIC WebSocket edge, two planes
      over one socket: (1) NOTICE — HS256 JWT hello (same tokens the API
      mints, alg pinned, iss/aud/exp/typ enforced), tenant pinned from the
      token, closed room namespace, per-tenant + global connection caps,
      frame limiter, heartbeat with pong-timeout reaping and token-expiry
      close, Bearer-gated `/metrics`, secret-gated `/ingest/v1/publish` with
      `(tenant, event_id)` replay suppression; (2) SIGNALING — point-to-point
      WebRTC session setup between two same-tenant connections
      (`session.start/join/end`, offer/answer/candidate relay byte-verbatim,
      UUID-capability session ids, one-outstanding-offer glare guard,
      socket-owned liveness with `peer_disconnected`/`join_timeout` reaping,
      tenant-mismatch/non-member/unknown-id collapsed to session_unknown);
      fail-closed boot on missing/placeholder secrets; `gofmt` + `go vet` +
      `go test -race ./...` + `go build` green (107 tests, incl. full-socket
      round trips for both planes and wire-level tenant isolation).
- [x] `services/ops/` — Go module (`github.com/voxdesk/ops`, stdlib only) and its
      first CLI, `voxops backup-verify`: a byte-for-byte port of
      `verify_backup_integrity` for the local pre-check, `pg_restore --list`
      shell-out for the authoritative check, and an offline `PGDMP` magic-header
      check when `pg_restore` is absent, with exit codes mirroring
      `ops.exit_code_for_status`; `gofmt` + `go vet` + `go test -race ./...` +
      `go build` green (43 test cases — 17 top-level plus their subtests —
      across `cmd/voxops`, `internal/backup`, `internal/status`). The remaining
      Phase 5 commands are listed in `services/ops/README.md`.

Language toolchains are intentionally installed per-phase; see
`docs/EXPANSION-ROADMAP.md` for the sequencing and decision gates.
```

==============================================================================
===== FILE: .github/workflows/polyglot.yml (135 lines, sha256 eed2bc9d51e8b0e1c934e4a321d3dc68b863bdf9a8036fbc4b0d5f13787ff0de) =====
==============================================================================
```yaml
name: polyglot

# The gate for the non-Python services introduced by the expansion roadmap
# (`services/README.md` "The polyglot gate"). Each job is one language
# workspace and runs exactly the toolchain that workspace documents in its own
# README — nothing else. The Python backend has its own gate in `ci.yml`; this
# workflow exists because a Rust/C++/Go/TypeScript regression cannot fail a
# workflow that never invokes their compilers.
#
# No job reaches the network beyond dependency installation, and no job needs a
# database, a provider key or a running service: every check here is a build,
# a lint or a test that runs locally the same way.
on:
  push:
    branches: [main]
  pull_request:

concurrency:
  group: polyglot-${{ github.ref }}
  cancel-in-progress: true

jobs:
  # Wire contract (Phase 0): the protobuf vocabulary every other job in this
  # file is supposed to speak. Compiles with protoc and cross-checks the enum
  # vocabulary + tenant scoping against app/db/models.py.
  contracts:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip

      - name: Install dependencies
        run: |
          pip install -r requirements.txt
          pip install pytest pytest-asyncio aiosqlite httpx

      - name: Install protoc
        run: sudo apt-get update -qq && sudo apt-get install -y -qq protobuf-compiler

      - name: Verify the wire contract
        run: python scripts/verify_contracts.py

      - name: Contract tests
        run: python -m pytest tests/test_contracts.py -q

  # Phase 3 — C++ media plane: dependency-free, built and tested with g++ + make.
  media-plane:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Build and run both suites
        working-directory: services/media-plane
        run: make test

  # Phase 2 — Rust control plane (`voxdesk-control` + `voxdesk-signal`).
  control-plane:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: dtolnay/rust-toolchain@1.90.0
        with:
          components: rustfmt, clippy

      - name: Rust gates
        working-directory: services/control-plane
        run: |
          cargo fmt --all -- --check
          cargo check --workspace
          cargo test --workspace
          cargo clippy --workspace --all-targets --all-features -- -D warnings

  # Go real-time signaling hub — the differential counterpart of
  # `services/control-plane/crates/signal`.
  signal-go:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-go@v5
        with:
          go-version: "1.27"
          cache-dependency-path: services/signal-go/go.sum

      - name: Go gates
        working-directory: services/signal-go
        run: |
          test -z "$(gofmt -l .)"
          go vet ./...
          go test -race -count=1 ./...
          go build ./...

  # Phase 5 — Go ops tooling (`voxops`).
  ops-go:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-go@v5
        with:
          go-version: "1.27"
          cache-dependency-path: services/ops/go.mod

      - name: Go gates
        working-directory: services/ops
        run: |
          test -z "$(gofmt -l .)"
          go vet ./...
          go test -race -count=1 ./...
          go build ./...

  # Phase 1 — Next.js/TypeScript admin console.
  dashboard-next:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-node@v4
        with:
          node-version: 20
          cache: npm
          cache-dependency-path: dashboard-next/package-lock.json

      - name: Install, test, typecheck, build
        working-directory: dashboard-next
        run: |
          npm ci
          npm test
          npx tsc --noEmit
          npm run build
```

==============================================================================
===== FILE: docs/DASHBOARD.md (161 lines, sha256 6b4922636af314bf9ff82ba59bdd1145cc5aa5c0eebafa155dad9dde71e4c541) =====
==============================================================================
```markdown
# Dashboard metric contract

Every number the dashboard shows, what it counts, and where it comes from.

The rule this document exists to enforce (requirement 12): **a percentage whose
denominator nobody can name is a number nobody can defend.** The pre-STEP-8
dashboard broke that rule with `booking_rate = booked / all calls`, which put
wrong numbers, missed calls and 3-second misdials in the denominator and made a
well-run agent look bad. That defect, and the made-up revenue tile, are recorded
in `docs/DASHBOARD-AUDIT.md` (F1, F10); this file is the definitional half of
the fix, and `app/api/analytics_routes.py` is the code half. The two are meant
to be read together — if a rate's definition changes in code, it changes here
in the same commit.

---

## 1. Where the numbers come from

| Page | Endpoint | Handler |
| --- | --- | --- |
| Overview | `GET /api/analytics/overview?range=` | `app/api/analytics_routes.py::overview` |
| Calls | `GET /api/analytics/calls?range=` | `call_analytics` |
| Analytics | `GET /api/analytics/conversion?range=` | `conversion` |
| Billing | `GET /api/analytics/usage` | `usage_analytics` |

Every figure is a single `SELECT count(...)` / `SELECT sum(...)` scoped by
`tenant_id` and a date window — conditional aggregation in one scan, not eight
round trips and never "fetch the rows and total them in the browser"
(requirement 13). A tenant with 400,000 calls costs the same round trip as a
tenant with four.

Two window rules are load-bearing:

* **The tenant's timezone decides the day boundaries**, not the browser's.
  `Tenant.timezone` is resolved server-side and echoed in the `window` object
  (`start`, `end`, `timezone`), so every client renders the same period.
* **A hard ceiling of 400 days** (`MAX_RANGE_DAYS`) applies to any range, so a
  single request cannot scan a decade.

## 2. Call counters

Source: `Call` rows whose `started_at` falls in the window.

| Field | Counts | Notes |
| --- | --- | --- |
| `total` | every call in the window | the denominator of the two "of every call" rates |
| `answered` | `status = COMPLETED` | |
| `missed` | `status = NO_ANSWER` | rang out |
| `failed` | `status = FAILED` | provider or pipeline failure |
| `booked` | `booked = true` | the tenant's own definition of a booking |
| `transferred` | `escalated = true` | a human took over |
| `inbound` / `outbound` | `direction` | |
| `eligible` | `status = COMPLETED` **and** `duration_seconds >= 10` | see §3 |
| `total_seconds` / `minutes` | `sum(duration_seconds)` | `minutes` is the same figure rounded to 0.1 |

`ELIGIBLE_CALL_SECONDS = 10` (`app/api/analytics_routes.py`). A call that lasted
three seconds because someone misdialled was never a booking opportunity.

## 3. The four rates (`analytics/calls` → `totals`, `analytics/conversion` → `rates`)

| Rate | Numerator | Denominator | Reads as |
| --- | --- | --- | --- |
| `answer_rate` | `answered` | `total` | of every call that arrived, how many did the agent complete? |
| `booking_rate` | `booked` | `eligible` | of the calls that could plausibly have booked, how many did? |
| `transfer_rate` | `transferred` | `answered` | of the answered calls, how many needed a human? |
| `failure_rate` | `failed` | `total` | of every call, how many failed outright? |

`booking_rate` is **not** `booked / total`. That is the F10 defect: it charges
the agent for wrong numbers and hang-ups. The Analytics page prints these
definitions next to the numbers, because an operator has to be able to explain
the rate to their own customer without opening the source.

The conversion endpoint adds three funnel rates:

| Rate | Numerator | Denominator | Notes |
| --- | --- | --- | --- |
| `call_to_lead_rate` | `leads` | `eligible` | capped at 100%: a lead may be created by an import rather than a call |
| `lead_to_appointment_rate` | booked appointments | `leads` | capped at 100% for the same reason |
| `appointment_kept_rate` | `kept` | booked appointments | kept = booked − no-shows |

## 4. The funnel (`analytics/conversion`)

Five stages, each counted independently over the same window:

| Stage | Counts |
| --- | --- |
| `calls` | every call in the window |
| `eligible` | answered and at least 10 s |
| `leads` | `Lead` rows created in the window |
| `appointments` | `pending` + `confirmed` + `rescheduled` + `no_show` |
| `kept` | booked appointments the customer attended (no-shows removed) |

Two deliberate decisions, both of which were bugs first and comments second:

* **Stages are counted, not followed.** A call in the window can produce an
  appointment outside it; tracing individual records would make this page
  disagree with the Calls page. Each stage is defensible on its own.
* **`booked` includes no-shows.** They *were* booked — dropping out is what the
  next stage measures, not this one. Only `cancelled` and `failed` appointments
  are excluded (a cancelled booking left the funnel earlier; a failed one never
  reached the provider). Subtracting no-shows from a set that had already
  removed them made a tenant with one kept appointment and one no-show report
  zero kept; a test now pins that case.

Because a funnel stage must never be wider than the one above it, the two
lead-derived rates are capped at 100%.

## 5. Operations and integrations tiles (`analytics/overview`)

| Field | Meaning |
| --- | --- |
| `operations.average_call_seconds` | `total_seconds / answered` — 0 when nothing was answered |
| `integrations.crm_sync_failures` | CRM sync failures in the window |
| `integrations.calendar_failures` | calendar provider failures in the window |

A provider error appears on the integrations page and in
`call.crashed` / `channel.agent_*` log events — never folded into the answer
rate, because a provider outage is not the tenant's agent performing badly.

## 6. Usage and overage (`analytics/usage`)

Requires `Permission.BILLING_READ`. The response carries the billing period
(`billing_period`, `period_start`, `period_end`), the plan code, per-metric
used/included quantities, `percent_used`, `estimated_overage_cents` and the
currency. Overage is computed from the plan's own limits
(`app/billing/metering.py`, `app/billing/plans.py`) — it is a price from
configured prices, not a constant.

When the caller may not read billing, `overview.usage` is **absent**, not
zeroed: a zero reads as "you have used nothing" rather than "you may not see
this".

## 7. What is deliberately not shown

* **`estimated_value_usd`.** The old Overview tile rendered
  `booked × $150` — a constant with no relationship to anything the tenant
  sells (audit F1, requirement 33). The field is still returned by the legacy
  `/stats` endpoint so no client breaks, and the dashboard no longer displays
  it: *a number a buyer will read as money has to come from money.*
* **Projections, forecasts and "potential" figures** of any kind. Every tile is
  a measurement of rows that exist.
* **Browser-localised timestamps.** Call times render in the tenant's timezone
  (`Tenant.timezone`), never in whatever locale the laptop happens to have
  (audit F11).

## 8. Checking a change

The definitions above are pinned by tests, in both halves of the stack:

| Test | Holds down |
| --- | --- |
| `tests/test_analytics.py::TestFormulas::test_booking_rate_excludes_calls_that_could_never_book` | the eligible denominator |
| `tests/test_analytics.py::TestFormulas::test_the_eligibility_threshold_is_documented_and_shared` | `ELIGIBLE_CALL_SECONDS` is one constant, not two copies |
| `tests/test_analytics.py::TestFormulas::test_a_rate_cannot_exceed_one_hundred` | the funnel cap |
| `tests/test_analytics.py::TestFormulas::test_no_estimated_revenue_anywhere` | §7, server side |
| `tests/test_analytics.py` (funnel + series classes) | kept-vs-no-show arithmetic, gap-filled series, local-day/local-hour bucketing |
| `dashboard/tests/no-fake-revenue.test.jsx` | §7, client side: the tile is gone and nothing invented a replacement |
| `dashboard/tests/formatting.test.jsx` | tenant-timezone rendering, not browser locale |

When a rate's denominator changes, exactly one of those tests is supposed to
fail — that is the point.
```

==============================================================================
===== FILE: docs/runbooks/REAL-E2E-RUNBOOK.md (218 lines, sha256 430f29f4a1b6c1151c713578b234fcda3fa83e2ed22b561aa9f78ff5c3773279) =====
==============================================================================
```markdown
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
```

==============================================================================
===== FILE: scripts/audit_missing_files.py (235 lines, sha256 84a6b42a81936bea16b5835c2bd1bc71fea6757924994b92c6355c52a9125776) =====
==============================================================================
```python
#!/usr/bin/env python3
"""Find files the repo REFERS to but does not contain.

Upstream-to-upstream comparison cannot see these: a config that points at a
missing script, a module declared but never written, a test fixture that was
never committed. This walks the actual references.

Usage: python3 scripts/audit_missing_files.py [--json]
Exit code 0 always (an audit, not a gate).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", "node_modules", "target", "__pycache__", ".venv", "dist",
             "toolchains", "go-work", "gop", "nltk_data", "backups"}


def _walk(pattern: str = "*"):
    for p in ROOT.rglob(pattern):
        if any(part in SKIP_DIRS for part in p.relative_to(ROOT).parts):
            continue
        if p.is_file():
            yield p


def exists(rel: str) -> bool:
    rel = rel.strip().strip('"').strip("'")
    if not rel or rel.startswith(("http://", "https://", "$", "-")):
        return True
    return (ROOT / rel).exists()


findings: list[dict] = []


def note(kind: str, where: str, detail: str) -> None:
    findings.append({"kind": kind, "where": where, "detail": detail})


# --------------------------------------------------------------- Makefile ---

def audit_makefile() -> None:
    mk = ROOT / "Makefile"
    if not mk.exists():
        note("makefile", "Makefile", "Makefile does not exist")
        return
    text = mk.read_text()
    for m in re.finditer(r"^\t(?:@?-?)(?:python3?|bash|sh|\./)?\s*([\w./-]+\.(?:py|sh))", text, re.M):
        ref = m.group(1)
        if not exists(ref):
            note("makefile", "Makefile", f"recipe references missing file: {ref}")
    for m in re.finditer(r"-f\s+([\w./-]+)", text):
        if not exists(m.group(1)):
            note("makefile", "Makefile", f"-f target missing: {m.group(1)}")


# ------------------------------------------------------- Docker / compose ---

def audit_docker() -> None:
    for df in list(_walk("Dockerfile*")) + list(_walk("*.dockerfile")):
        text = df.read_text()
        for m in re.finditer(r"^\s*(?:COPY|ADD)\s+(.+)$", text, re.M | re.I):
            args = m.group(1).split()
            for src in args[:-1]:
                if src.startswith(("--from=", "$")):
                    continue
                rel = src.lstrip("./")
                if rel in ("", "."):
                    continue
                if not exists(rel.rstrip("/")):
                    note("dockerfile", str(df.relative_to(ROOT)), f"COPY source missing: {src}")

    for comp in list(_walk("docker-compose*.yml")):
        text = comp.read_text()
        for m in re.finditer(r"^\s*-\s+([\w./~-]+\.(?:sh|py|json|yml|yaml|conf|sql|toml))", text, re.M):
            ref = m.group(1)
            if ref.startswith(("./", "~")):
                if not exists(ref):
                    note("compose", str(comp.relative_to(ROOT)), f"mounted file missing: {ref}")
        for m in re.finditer(r"env_file:\s*\n\s*-\s*([\w./-]+)", text):
            if not exists(m.group(1)):
                note("compose", str(comp.relative_to(ROOT)), f"env_file missing: {m.group(1)}")


# ------------------------------------------------------------ CI workflows ---

def audit_workflows() -> None:
    wf_dir = ROOT / ".github" / "workflows"
    if not wf_dir.is_dir():
        note("ci", ".github/workflows", "no workflows directory")
        return
    for wf in sorted(wf_dir.glob("*.yml")):
        text = wf.read_text()
        paths: set[str] = set()
        for m in re.finditer(r"^\s{2,}([\w./-]+\.(?:py|sh|json|js|ts|tsx|yml|yaml|toml))(?:\s|$)", text, re.M):
            paths.add(m.group(1))
        for m in re.finditer(r"working-directory:\s*([\w./-]+)", text):
            paths.add(m.group(1))
        for m in re.finditer(r"cache-dependency-path:\s*([\w./-]+)", text):
            paths.add(m.group(1))
        for rel in sorted(paths):
            if rel.startswith(("uses:", "run:")):
                continue
            if "/" in rel or rel.endswith((".py", ".sh", ".json", ".toml")):
                if not exists(rel):
                    note("ci", wf.name, f"CI references missing path: {rel}")


# ------------------------------------------------------------- Python refs ---

FILE_LITERAL = re.compile(r"""["']((?:docs|contracts|scripts|observability|loadtest|alembic|tests|app)/[\w./${}-]+\.(?:md|json|sql|yml|yaml|py|sh|txt|j2|template))["']""")


def audit_python_literals() -> None:
    for py in _walk("*.py"):
        try:
            text = py.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for m in FILE_LITERAL.finditer(text):
            ref = m.group(1)
            if "$" in ref or "{" in ref:
                continue
            if not exists(ref):
                note("py-literal", str(py.relative_to(ROOT)), f"path literal with no file: {ref}")


# --------------------------------------------------------------- Rust mods ---

def audit_rust() -> None:
    for rs in _walk("*.rs"):
        if "vendor/" in str(rs):
            continue                                    # vendored crate: upstream ships complete
        text = rs.read_text(encoding="utf-8")
        base = rs.parent
        for m in re.finditer(r"^\s*(?:pub\s+)?mod\s+(\w+)\s*;", text, re.M):
            name = m.group(1)
            if (base / f"{name}.rs").exists() or (base / name / "mod.rs").exists():
                continue
            note("rust", str(rs.relative_to(ROOT)), f"`mod {name};` has no file")
    for cargo in _walk("Cargo.toml"):
        if "vendor/" in str(cargo):
            continue
        text = cargo.read_text()
        base = cargo.parent
        for key in ("path", "build"):
            for m in re.finditer(rf'^{key}\s*=\s*"([^"]+)"', text, re.M):
                if not (base / m.group(1)).exists():
                    note("cargo", str(cargo.relative_to(ROOT)), f"{key} missing: {m.group(1)}")
        for line in text.splitlines():
            s = line.strip()
            if s.startswith('"') and s.endswith('",') and "/" not in s:
                rel = s.strip('",')
                if rel.startswith(("crates/", "bins/", "benches/")) and not (base / rel).exists():
                    note("cargo", str(cargo.relative_to(ROOT)), f"workspace member missing: {rel}")


# ----------------------------------------------------------------- Go refs ---

def audit_go() -> None:
    for go in _walk("*.go"):
        text = go.read_text(encoding="utf-8")
        for m in re.finditer(r"//go:embed\s+(.+)", text):
            for pat in m.group(1).split():
                pat = pat.strip()
                if not list((go.parent).glob(pat)):
                    note("go", str(go.relative_to(ROOT)), f"go:embed pattern matches nothing: {pat}")


# ------------------------------------------------- frontend config/imports ---

def audit_frontend() -> None:
    for pkg_json in (ROOT / "dashboard" / "package.json", ROOT / "dashboard-next" / "package.json"):
        if not pkg_json.exists():
            note("frontend", str(pkg_json.relative_to(ROOT)), "package.json missing")
            continue
        data = json.loads(pkg_json.read_text())
        base = pkg_json.parent
        for name, cmd in (data.get("scripts") or {}).items():
            for m in re.finditer(r"([\w./-]+\.(?:js|ts|mjs|cjs|json|html))", cmd):
                ref = m.group(1)
                if not (base / ref).exists():
                    note("frontend", f"{pkg_json.parent.name}/package.json",
                         f"script '{name}' references missing {ref}")
        for field in ("main", "module", "types", "style"):
            rel = data.get(field)
            if rel and not (base / rel).exists() and "/" in str(rel):
                note("frontend", f"{pkg_json.parent.name}/package.json", f"{field} missing: {rel}")

    for ts in _walk("tsconfig.json"):
        text = re.sub(r"//.*", "", ts.read_text())
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        for m in re.finditer(r'"\./([\w./-]+)"', json.dumps(data)):
            ref = m.group(1)
            if not (ts.parent / ref).exists():
                note("frontend", str(ts.relative_to(ROOT)), f"tsconfig references missing {ref}")


# ------------------------------------------------------------------- run ----

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--json", action="store_true")
    args = p.parse_args()
    if not (ROOT / "app").is_dir():
        print("run me from inside the repo", file=sys.stderr)
        return 2

    for fn in (audit_makefile, audit_docker, audit_workflows, audit_python_literals,
               audit_rust, audit_go, audit_frontend):
        try:
            fn()
        except Exception as exc:                        # audit must never crash
            note("audit-error", fn.__name__, f"{type(exc).__name__}: {exc}")

    if args.json:
        print(json.dumps(findings, indent=1))
    else:
        print(f"reference-driven checks: {len(findings)} finding(s)")
        for f in findings:
            print(f"  [{f['kind']}] {f['where']}: {f['detail']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

==============================================================================
===== FILE: PROMPT1-FINAL-REPORT.md (150 lines, sha256 f1d4384ac3f7e65d5ba56b3391fde44f1e834daea4ce6202546642d0e7dc027b) =====
==============================================================================
```markdown
# Prompt 1 — Final Report: Realtime Foundation Audit & Fix
**Date:** 2026-09-17 · **Scope:** services/realtime/gateway-go + services/realtime/media-engine-rs + deploy wiring · **Rule set:** Prompt 1 A–J, 17-point done-checklist

---

## 1. Gates (exact commands + results)

| Gate | Command | Result |
|---|---|---|
| Rust tests | `cd services/realtime/media-engine-rs && cargo test --workspace` | **89 passed, 0 failed** (was 88 at baseline; old engine pipeline tests replaced & expanded) |
| Rust fmt | `cargo fmt --all -- --check` | clean |
| Rust lint | `cargo clippy --workspace --all-targets` | **0 warnings** (12 pre-existing style warnings fixed during the phase; aes128 crypto constant math untouched — warnings there were noise-auditable, but resolved globally via std idioms where semantically identical) |
| Go tests | `cd services/realtime/gateway-go && go test ./...` | all 19 packages ok |
| Go race | `go test -race -count=1 ./...` | **all 19 packages ok, 0 failures** |
| Go fmt/vet | `gofmt -l .` / `go vet ./...` | clean / clean |
| Verbose count | `go test -v ./...` | **177 `--- PASS`, 0 FAIL** (includes subtests; prior baseline was 149 under the same counting) |
| Live Go↔Rust | `VOXDESK_IT_ENGINE=1 VOXDESK_IT_ENGINE_BIN=... go test -tags it -run TestLive ./internal/engineclient` | **PASS** — real engine binary: health probe → join (ready + ICE creds) → offer→answer → valid trickle quiet success → malformed trickle `bad_message` → leave |
| Engine smoke | curl against built bin | `/v1/health` payload ✓, enveloped join echoes correlation id ✓, `v:9` → `unsupported_version` structured error ✓, new metric series exposed ✓ |
| Compose validation | `docker compose config` | **COULD NOT RUN — no docker client in this sandbox** (verified: `docker: command not found`). YAML parsed successfully with PyYAML; service graphs hand-checked against existing compose conventions. Docker build of the new media-engine Dockerfile therefore NOT executed here — flagged honestly per prompt section I. |
| Tooling note | mid-phase, the sandbox snapshot wiped Rust toolchains and Go (1.27.1). Both reinstalled (rustup stable 1.98.1; go 1.27.1 → `~/toolchains/go`). No gate results were ever claimed before the reinstall. |

## 2. Files modified (COMPLETE contents live in the repo — nothing omitted)

**Rust — media-engine-rs**
| File | Lines | What changed |
|---|---|---|
| crates/protocol/src/lib.rs | 351 | `WIRE_VERSION=1`; Publish/Subscribe/Unsubscribe/Leave gained additive optional `session` field (v1.1 wire, decode back-compatible) |
| crates/webrtc/src/ice.rs | 414 | `add_remote_candidate` (validate/dedupe/persist), `remote_candidates`, per-agent unique txn ids (`next_transaction_id` replacing compile-constant `b"voxdesk-ion!"`) |
| crates/signaling/src/lib.rs | 547 | real Trickle parse/validate/refusal path; publish/leave/offer produce Effects (`published`, `left`, `trickle`, `offer_context`); session-attribution enforcement (`require_session`) |
| crates/engine/src/lib.rs | 861 | **rewritten**: slot SSRC mint on publish, `by_ssrc` map (kills `iter().next()` arbitrary track routing), endpoint anti-spoof gate, RTCP parse→discriminate→apply (SR/RR counters, report blocks applied to source streams, unsupported counted), leave/sweep full teardown, `on_control` versioned envelope (`{v,id,frame}`/`{v,id,frames}` + structured errors; bare-frame back-compat), `health_json` |
| crates/engine/tests/pipeline.rs | 404 | **rewritten**: 4 suite tests — full join→trickle→offer→publish→subscribe→RTP fanout w/ `ssrc` identity assertions, spoof/unknown counters, RTCP RR record assertions, multi-track allocator & per-track routing, envelope contract, health payload |
| crates/media/src/lib.rs | 194 | SourceStream gained RR/SR feedback record fields |
| crates/signaling/tests/flows.rs | ~150 | publish/subscribe constructors carry `session: None` (ctx-attached path); trickle test updated to new refusal/accept semantics |
| crates/protocol/tests/wire.rs | ~70 | constructor field updates only |
| crates/routing/src/lib.rs, webrtc/src/srtp.rs, rate-limit/src/lib.rs | — | clippy idiom fixes only, no semantic change |
| bins/media-engine/src/main.rs | 298 | `/v1/health` route; `POST /v1/signal` now delegates to `on_control` (envelope pass-through); metrics exposition gained rtcp/drop kind series |
| bins/loadgen/src/main.rs | 197 | bench registers tracks through the REAL publish frame (session-attributed); reads the engine-minted SSRC rather than poking slot internals |
| Dockerfile, scripts/build-media-engine.sh | new | two-stage non-root image; IT build helper |

**Go — gateway-go**
| File | Lines | What changed |
|---|---|---|
| internal/engineclient/client.go | 432 | **NEW**: typed HTTP client for the engine wire (correlation ids, typed `EngineError`/`FrameError`, deadline discipline = caller ctx ∪ configured backstop, no blind retry, availability state machine `Monitor` w/ backoff 200ms→5s + Up()/LastError()/LastHealth(), `IsUnavailable` classification) |
| internal/engineclient/client_test.go | 217 | **NEW**: 9 tests (envelope shape, id echo, mismatch refusal, in-band error typing, version skew, ctx-deadline-governs, monitor transitions, availability states, exact one-hit [no hidden retry]) |
| internal/engineclient/it_live_test.go | 128 | **NEW** (build tag `it`): live test vs real engine binary, env-gated |
| internal/config/config.go | 317 | `MediaEngineURL`, `MediaEngineTimeoutSeconds` (+ `envFloat` helper); scheme-required problems, timeout bounds (0,30], disabled-plane warning when absent |
| internal/config/config_engine_test.go | 87 | **NEW**: 4 validation tests |
| internal/server/server.go | 191 | `SetEngine`/`Engine` (public `New` arity unchanged — contract preserved) |
| internal/server/health.go | 75 | readiness engine segment: disabled → ready; up → ready + version/rooms; **down → 503 with last_error** |
| internal/signaling/router.go | 157 | engine attach (`SetEngine`), `engineSessions` map, engine cleanup guarantee on ConnClosed (pending-session socket close) |
| internal/signaling/events.go | 224 | `engineJoinHook` (inline, 1.2s bound, on EvStarted/EvJoined), `engineLeaveHook` (pre-lookup-guard on every EvEnded), `engineLeaveAllForConn` safety net, UNAVAILABLE-vs-REFUSED log classification, no ICE creds in logs |
| internal/observability/metrics/metrics.go | 205 | engine series: up gauge, calls/errors (join/leave breakdown), probes, latency sum/max |
| internal/server/engine_e2e_test.go | 377 | **NEW**: 6 integration tests over REAL websockets + scripted engine |
| cmd/gateway/main.go | 157 | engine client construction + Monitor goroutine + observer adapter, deterministic boot logging |

**Deploy**
- `docker-compose.prod.yml`: new `media-engine` service (UDP 5000 published, control 9001 inter-container, `MEDIA_ENGINE_PUBLIC_IP:?required`); gateway gains `VOXDESK_GATEWAY_MEDIA_ENGINE_URL=http://media-engine:9001` + timeout + depends_on.
- `.env.example`: appended MEDIA_ENGINE_* block (documented, no insecure defaults — public IP is required-empty).

## 3. Bugs found → fixed (Rust engine)

1. **`tracks.iter().next()` arbitrary routing** → deterministic SSRC→(session,track) map; allocation counter; collision walk.
2. **Publish accepted but never registered into the forwarding path** → `on_published` mints ssrc, updates slot ledger + by_ssrc atomically per publish.
3. **RTCP silently discarded** (classify→drop) → full parse; SR/RR counters; RR report blocks applied to source stream records (`rr_fraction_lost_latest`, `rr_cumulative_lost_latest`, `rr_total`); unsupported packet types and malformed counted explicitly.
4. **Fixed STUN transaction id `b"voxdesk-ion!"`** (RFC 5389 violation; two outstanding checks could collide) → per-agent atomic counter + tiebreaker mixing, never repeating within a ufrag.
5. **Trickle frames swallowed uncommented** → parse, session-state validation, candidate validation, dedupe into ICE pair table, persisted to agent; malformed → `bad_message`; e.o.c. markers; wrong-session → `wrong_state`.
6. **Offer reduce threw away remote context** → ICE creds/candidates harvested into agent (`adopt_remote` on the session's agent; trickle-after-offer via `add_remote_candidate`).
7. **Leave never tore down wire identity** (endpoint binding and ssrc ownership survived) → leave sweeps endpoint, ssrc owners, and stream pipeline state; session sweep does the same.
8. **No anti-spoof on RTP**: any 5-tuple could claim an ssrc → datagrams are refused unless source equals that session's ICE-nominated endpoint; separate `rtp_drop_spoof` counter.
9. **No correlation/versioning on the control wire** → `{v,id,frame}`/`{v,id,frames}` + structured `{v,id,error}`; mismatched/unknown version refused in-band; bare frames still accepted for the legacy smoke path.
10. **No readiness payload for the Go side** → `GET /v1/health` reports wire v, version, ready, rooms/participants/tracks/streams, uptime.

## 4. What "real Go↔Rust integration" consists of
- Transport: engine's documented HTTP control (`POST /v1/signal` envelope + `GET /v1/health`), as chosen in the plan (least invasive).
- Explicit protocol: versioned envelope both directions, 16-hex correlation ids echoed and verified — a mis-correlated reply is a `FrameError`, never delivered.
- Timeout/cancellation: caller ctx rules; client backstop = `MEDIA_ENGINE_TIMEOUT_SECONDS` (default 1.5s); router-side per-call cap 1.2s so engine slowness can never stall a connection's read loop beyond that.
- Retry/idempotency: no blind retries inside the client (joins mint state); engine leaves are idempotent by design (unknown session = quiet success), so retry-at-ambiguity is caller-safe. Documented in code.
- Reconnect/liveness: `Monitor` probes `/v1/health` with 200ms→5s backoff; one transition log + gauge flip per change; fail-closed on boot (never claims up before first green probe).
- Lifecycle: EvStarted/EvJoined → engine join (room=`tenant:session`, participant=connID); EvEnded/ConnClosed → leave (with pending-session safety net). Inline, bounded ≤1.2s per hook.
- Graceful degradation: down/refusing engine never breaks session establishment; failures surface via metrics + classified structured logs + readiness going red (503) — exactly the three channels.
- Metrics: `voxdesk_gateway_engine_up`, `engine_signal_calls_total`, `engine_signal_errors_total`, join/leave totals+errors, probe totals+errors, latency sum/max. Engine-side: stun/rtp/unknown + new `voxdesk_media_rtcp_total{kind}` and `voxdesk_media_rtp_dropped_total{kind}`.

## 5. 17-point done checklist
✔ tracks actually registered · ✔ real SSRC routing (no `iter().next()`) · ✔ RTCP parsed/applied, nothing silently discarded · ✔ trickle persisted to agent pair table · ✔ unique ICE txn ids · ✔ REAL Go↔Rust path (live test green) · ✔ config points at real engine in prod compose · ✔ readiness reflects engine (down → 503) · ✔ no insecure defaults (empty-URL = explicit disabled + warning; JWT/ingest refusals unchanged) · ✔ no placeholders/TODO in touched paths · ✔ no fake implementations · ✔ public contracts preserved (`server.New` arity; bare-frame control back-compat; client-visible frames unchanged/additive) · ✔ race gate green (go vet + `-race`, Rust shared-state is single-owner + Arc/Mutex) · ✔ secrets out of logs/metrics (ICE pwd never logged; classified error classes only) · ✔ full-file contents = repo state · ✔ integration results reported including the one gate that could not run here (docker client absent — compose not executed). ✘/N/A: none unaccounted.

## 6. Deltas the client surface will notice
**None by contract.** Browser frames unchanged; session lifecycle frames unchanged; engine integration is fully server-side additive. The only wire evolution: engine control protocol now stamps `{v,id,…}` (gateway↔engine internal only).

## 7. Prompt-2 remainder (explicit)
1. Browser offers currently continue gateway P2P relay; the SFU steer (offers answered by the engine, answers pushed as first-class gateway frames) needs a v1.2 of the browser protocol + engine m-line batching. **Not done this phase by design** (prompt E scope chosen: join/leave/health + real wiring).
2. DTLS-SRTP key extraction feeding `bind_srtp` (engine hook exists; SDES-bound in tests).
3. Engine→gateway `track.published` fanout delivery channel (engine emits; gateway does not yet subscribe for browser-visibility).
4. Multi-node engine fleet: SSRC allocator is per-node; cross-node namespace is label-seeded but untested at fleet scale.
5. Loadgen numbers re-verified only locally (MemTransport); NIC-scale rerun in staging advised, with the new metric series wired into the Grafana dashboard.
6. Compose/docker build could not be executed in this sandbox — first deploy should run `docker compose -f docker-compose.prod.yml config` and the image build in CI.

---

# Addendum 2026-09-17 (Prompt-2 executable leg CONTINUED)

## Executed this leg
1. **Fleet-scale SSRC partitioning (item 4) — DONE.** The allocator is now `(partition_byte << 24) | counter24` where `partition_byte = FNV-1a(engine_label) mod 254 + 1` (skips 0x00/0xFF): 255 fleet slots, 16.7M SSRCs/node, restart collision still gated by the by_ssrc walk. Partition exposed via `Engine::ssrc_partition()` and `health_json`'s new `ssrc_partition` field so two colliding nodes are diagnosable from metrics. New test `fleet_ssrc_partitions_do_not_overlap_across_labels` (two labels with distinct partitions, 1024 sampled allocations, disjoint sets, top-byte assertions).
2. **Observability wiring (item 5, sandbox-executable half) — DONE.** `observability/prometheus.yml` gained the `voxdesk-media-engine` job; engine gauge series renamed to namespaced `voxdesk_engine_{rooms,participants,tracks}_current` (comment and names now agree; live curl-verified). New gateway-side engine series from Prompt-1 unchanged.
3. **Loadgen rerun (item 5, local half) — DONE.** Post-change: **328,292 pps** (baseline 315k — noise band), accounting exact: injected 280,000 (+4,375 replays), received 284,375 = n + n/64 ✓, duplicates 4,375 ✓, unknown_frames 0 ✓. NIC-scale staging rerun still pending (infra).
4. **Prompt-2 heavy items (1: SFU steer; 2: DTLS-SRTP extraction) — specified, not built.** `PROMPT2-DESIGN.md` contains: browser-protocol v1.2 frame vocabulary, gateway/engine deltas by file, the rejected-vs-chosen fork on the engine push problem (decision: A1 single `/v1/bus` chunked stream per engine node, idempotent `?since` resume, token header required in prod), rollout flags (`VOXDESK_GATEWAY_ENGINE_STEER=off|v1.2|force`), test plans, and the explicit constraint lift for the DTLS crate (pure-Rust WebRTC dtls, fingerprint-vs-SDP binding, RFC 5764 extraction into the existing `bind_srtp` hook). Nothing is faked.

## Gates at leg end (exact)
- Rust: `cargo test --workspace` → **90 passed, 0 failed** (+fleet test); `cargo fmt --all -- --check` clean; `cargo clippy --workspace --all-targets` → **0 warnings**.
- Engine smoke: metrics series verified by curl (14 named series listed above).
- Go: no code touched this leg; prior `-race` gate from the completion pass stands.
- Sandbox reality: toolchains (Rust + Go 1.27.1) are pruned between turns by the snapshot; `restore-tooling.sh` at repo-root-adjacent `~/restore-tooling.sh` now self-heals them in ~12s (documented for any continuation).

## Not done this leg (carried verbatim into Prompt-2 execution)
- SFU steer and DTLS-SRTP remain **designed, not implemented** (PROMPT2-DESIGN.md is the implementation contract).
- Staging loadgen (NIC-scale), CI `docker compose config` + engine image build — infra access required.
- live `it` lane (browser-free go↔rust) from the completion pass still passes and remains the engine/client CI gate.

---

# Addendum 2026-09-17 (leg 3: PROMPT-2 ITEM 1 — SFU STEER — IMPLEMENTED)

Prompt-2 item 1 (browser media through the Rust SFU) is now CODE-COMPLETE per PROMPT2-DESIGN.md's amendment: full wire 1.2 (hello ws:2 marker, 5 engine.* client frames, 2 new server frames, steer flag on join acks), negotiation/mixing refusal/degradation rules exactly as designed, config+compose+env wiring, deliberate golden updates. Design amendment recorded: the `/v1/bus` fork (A1) was not built — the engine's reply+room_fanout response array already carries every event the 2-member model needs; the bus stays designed for the multi-member feature line.

Gates: `go test -race -count=1 ./...` → 19/19 packages ok, **186 PASS / 0 FAIL**; live `-tags it` lane against the real engine binary → PASS (incl. publish fanout against real Rust behavior); rust gates unchanged (90/90); vet/gofmt clean; one dev-time test race caught by -race and fixed.

Remaining Prompt-2 scope: item 2 (DTLS-SRTP extraction — required before steer=force with real browsers, since browsers refuse non-DTLS media), dashboard wire-1.2 adoption (TS client work — out of this workspace's scope rules), fleet + staging verification (infra), browser UA integration tests in the it lane.

---

# Addendum — leg 4 (2026-09-17): Prompt-2 item 2, DTLS-SRTP termination — SHIPPED

Prompt-2 item 2 (RFC 5764 DTLS-SRTP key extraction in the media engine) is now CODE-COMPLETE per PROMPT2-DESIGN.md's second amendment: one pinned dependency tree (`dimpl` 0.7.3, Sans-IO sync DTLS 1.2/1.3 by the str0m author), a new `crates/dtls` shim (role negotiation, fingerprint pin-verify, RFC 4.2 role-aware split for all three negotiable profiles, typed CM view as the only bind route), transport DTLS demux, per-session engine DTLS endpoints with kick/nomination/sweep-timer plumbing, RFC 5763-correct `a=setup` mirroring, and media-level ICE-cred fallback in offers (a genuine-browser gap found by the engine tests). Per-boot self-signed identity replaces the static answer fingerprint, with a loud WARN for the fixture-pin env override.

Gates: `cargo test --workspace` → **107 PASS / 0 FAIL** (90 pre-leg); `cargo fmt --check` clean; `cargo clippy --workspace --all-targets` **0/0**; `cargo build --release --workspace` OK; Go untouched (`-race` suite still all-ok); live `-tags it` lane against the real binary PASS with a per-boot identity fingerprint.

Documented adaptations (both registered in PROMPT2-DESIGN.md amendment (b)): (1) in-repo loopbacks necessarily negotiate AEAD-GCM — dimpl's own offer/preference pair makes CM unreachable crate-internally and a byte-level CH rewrite is correctly transcript-rejected — so split/export is proven profile-wide, while `aes128_cm()` at the bind gate enforces the engine's CM-only crypto loudly; (2) the browser-grade UA lane (pion/simplewebrtc + Chrome/Firefox/Safari SDP verification) remains the one open `it` item, matching the design doc's own rollout plan. `bind_srtp_split` direction mapping (inbound = client-write keys ⇔ subscriber decrypts engine-protected legs) is proven with real RTP in both directions.

Remaining Prompt-2 scope: dashboard wire-1.2 adoption (TS client work — out of this workspace's scope rules), the browser UA `it` lane above, fleet + staging verification (infra).

---

## Addendum — Prompt-2 leg 5: genuine pion-UA lane; CM DTLS-SRTP proven end-to-end; RFC 8489 STUN HMAC bug found & fixed

**Status: SHIPPED (2026-09-18).** Two real pion/webrtc v4 peer connections with CM-only SRTP policies ran ICE + DTLS + CM-SRTP media loopback through the spawned production engine binary, with media payload + SSRC lineage + metrics ledger verification.

**Highest-value bug of the whole Prompt-2 run:** engine STUN MESSAGE-INTEGRITY framed self-consistently (fixture-generated ↔ fixture-verified) but NOT per RFC 8489 §14.5 — every browser-class client was being 401'd during ICE connectivity checks pre-fix. Only a genuine third-party client on the wire could expose it; found via captured pion frames + reference HMAC recompute; fixed in `crates/webrtc/src/stun.rs` (signing + verifying), regressed permanently against a real captured pion request.

Also shipped leg content: additive optional `ssrc` on the publish frame (real-client SSRC attribution; collisions + ssrc 0 refused loudly; fixtures unchanged); engine `/metrics` DTLS outcome counters + publish-refused counter; sandbox-honest announce-IP selection in the lane; pion webrtc v4.2.20 / dtls v3.1.9 as it-tagged test-only deps.

**Gates (final state, all rerun):** Rust `cargo test --workspace` 109/0; fmt clean; clippy 0/0; release build OK. Go `-race` 18 ok / 0 fail; vet/gofmt clean; both `it` lanes PASS against the real engine (base 0.14s; pion 0.57s with per-boot fingerprint). Recorded in full in PROMPT2-DESIGN.md Amendment (c).
```

==============================================================================
===== FILE: PROMPT2-DESIGN.md (158 lines, sha256 85ef3c1ac7f8650564eb0dcc61ceb28d83317e668a50283c5b613246eb02cc7f) =====
==============================================================================
```markdown
# Prompt-2 Design — the two heavy items deferred from the completion pass
**Status:** implementation-ready specification (no code yet). Written 2026-09-17 after the Prompt-1 completion leg; referenced as PROMPT1-FINAL-REPORT.md §7 items 1–2.

---

## Item 1 — SFU steer: browser media actually flows through media-engine-rs

### Goal and non-goal
Today: gateway P2P relay carries offer/answer/candidate between the two members of a `session.*`; the Rust SFU tracks lifecycle (join/leave + RTP/RTCP processing) but browsers never address it. Goal: for a 2-member gateway signaling session, media runs **browser ⇄ engine ⇄ browser** (true SFU steer), with the P2P relay path remaining for sessions/tenants the operator has not migrated and as an explicit fallback (`engine steers=false` kill-switch per config).

### Wire: browser protocol v1.2 (additive; clients negotiate via `hello`)
- `hello` gains optional `"ws": 2` capability marker; the gateway's `ready` gains `"engine": {"steer": true,"session_prefix":...}` when v1.2 accepted AND engine up AND steer enabled.
- New client→server frames:
  - `engine.offer` `{session_id, sdp}` — the browser's SDP offer (RFC 8829 complete empty-offers allowed).
  - `engine.candidate` `{session_id, candidate|null}` — trickle, including e.o.c `null`.
  - `engine.publish` `{session_id, track, kind}` — reuse current publish vocabulary only when steer active.
  - `engine.subscribe` `{session_id, participant, track}` / `engine.unsubscribe`.
- New server→client frames: `engine.answer` `{session_id, sdp}`, `engine.track_published` `{session_id, participant, track, kind}`, `engine.ready` `{session_id(engine), participant}` (capability triple minus ICE creds — creds stay server-side; the browser's candidate harvesting is the only ICE surface it needs).
- Refusals continue down the existing `error` frame channel; codes reuse the gateway's approved taxonomy (`engine_unavailable`, `bad_message`, `room_unknown`, `over_limit`).

### Gateway wiring (target files, delta from Prompt-1 state)
- `internal/signaling/router.go` + new `internal/signaling/steer.go`:
  - v1.2 upgraded connections route `engine.*` frames through a NEW Router section; ordering with the member lifecycle is by the session manager's lock (same invariant as emit).
  - On `session.peer_joined` for a steered session: both members already have engine sessions (Prompt-1 join hook); steer mode just flips the offer route.
- `internal/engineclient/client.go`: add typed `Offer(ctx, session, sdp) ([]Frame, error)`, `Trickle`, `Publish`, `Subscribe` wrappers + op-bucket names for metrics (`offer`/`trickle`/`subscribe` counters live next to join/leave).
- Engine response fanout: server frames returned by the engine in the SAME HTTP response route back to the initiating connection (`engine.answer`, errors) and to the room's other member (`engine.track_published`) via a mapping engineRoom ⇄ gateway session kept on the router. **No engine-initiated push channel is built;** publish events reach the peer inside the peer's next poll... — see "Push problem" below.

### Engine deltas
- Preserve current behavior for `join/publish/subscribe/leave/trickle`. Offer handling ALREADY answers; **NO change needed.**
- `track.published` fanout: today it's a route-emit only. Extend HTTP control with one long-poll endpoint per gateway process (`GET /v1/events?room=…` is rejected: too many rooms per gateway on one HTTP line) — see decision below.

### The push problem (the one real design fork)
The engine has no push transport; events the gateway must pass to *the other* member (track.published) arrive only inside answers to that member's own calls. Options:
- **A1 — Event bus per room with single fan-out poll per gateway**: gateway maintains ONE long-lived SSE-ish stream per media engine node: `GET /v1/bus` (chunked JSON lines of ALL rooms' events), reconnecting with backoff. Simple, single point of reconnect, no polling. Engine implements a ring-ordered bus keyed on a monotonically increasing seq (idempotent resume from `?since=N`).
- **A2 — Piggyback only**: publish events ride the engine's "join fanout" inside the joining HTTP response header section. Cheaper; can delay publish visibility until the peer's next call — unacceptable for UX.
- **Decision: A1.** Rationale: one TCP stream per engine node per gateway replica (bounded by fleet size × replicas, not by sessions), full idempotent resume (`since` seq), graceful degradation (bus down → members still steer media; publish visibility delayed + logged + metric). Auth for the bus: the gateway↔engine link is compose-network-scoped; a shared token header is added nonetheless (both sides must be able to run on less trustful throats; header `X-Voxdesk-Bus-Token`, ?required in prod).

### Rollout
1. Steer OFF by default (`VOXDESK_GATEWAY_ENGINE_STEER=off|v1.2|force`).
2. `v1.2`: only upgraded clients + operator-accept tenants steer; P2P continues side by side.
3. `force`: all sessions steer; P2P fold deprecation follows in a later prompt.

### Tests to write (when implementing)
- Rust: offer batching per mid; `on_bus` ordering/resume; publish fanout ordering vs. subscribe leg admission.
- Go: steer envelope surfacing engine answers; bus reconnect/resume (fake engine emits gap); degrade cases (engine down during steer ⇒ session continues, frames refused cleanly); the 8 integration cases from Prompt-1 section H PLUS steer equivalents.
- Live: browser-free E2E with two ws clients + real engine bin (extend `it_live_test.go`).

## Item 2 — DTLS-SRTP key extraction (production browser ICE → SRTP)

### Reality check
Browsers REQUIRE DTLS+SRTP (RFC 8827); the engine currently accepts SRTP via `bind_srtp(master_key, master_salt)` — a hook intentionally written so key material arrives from ANY lawful source. Implementing a browser-grade DTLS 1.2 handshake, certificate verification, rekeying, and alert protocol from scratch is measured in thousands of lines and belongs in a reviewed crypto crate, NOT in this workspace's "std-only" self-imposed constraint.
### Decision (documented here so the later phase is unambiguous)
- The std-only constraint is **lifted for exactly one dependency tree**: `rustls-dtls`-style OR OpenSSL-SRTP-bindings — the gate is: maintainers-active, fuzzed, DTLS 1.2 + 1.3, RFC 5764 use_srtp extension, `SSL_get_selected_srtp_profile`-equivalent key-material export. Candidates at decision time: `webrtc` crate-family's `dtlss` (pure Rust, WebRTC-targeted, mature), or `openssl` crate with DTLS enabled.
- Handshakes terminate ON the engine: verifier role per offer's `a=setup`, fingerprint match against the SDP `a=fingerprint`, then `bind_srtp(exported_master_key, exported_master_salt)` per RFC 5764 extraction order (client keys to inbound, server keys to outbound).
- The current SDES-style path remains ONLY in test/fixtures, clearly named; it is never browser-visible.
- Effort estimate: ~2–3 days of focused integration + interop verification against Chrome/Firefox/Safari SDP; the certificate for the engine's self-signed identity is generated per boot (standard SFU practice), fingerprint pinned into answers as today.
- Tests: RFC-exported conformance vectors for key extraction at minimum; live round-trip against an SDP-legal test UA (simplewai/pion-based driver) in the `it` gate.

### Cross-cutting acceptance for both items
- Zero demo toggles: steer defaults OFF until the e2e evidence is in; DTLS is *required* for steer=force rollout.
- Gate discipline identical to Prompt 1: full `cargo fmt/clippy/test`, `gofmt/vet/-race`, live it-lane, honest report incl. anything the sandbox can't execute.

---

# Amendment 2026-09-17 — IMPLEMENTATION RECORD (Item 1: steer SHIPPED as specified except one fork revision)

## What was implemented
- **Browser wire 1.2**: hello `"ws":2` capability marker (pinned at hello alongside tenant identity); five client frames (`engine.offer/.candidate/.publish/.subscribe/.unsubscribe`) with codec presence rules; two server frames (`engine.answer`, `engine.track_published`); `session.joined`/`session.peer_joined` gained the always-present `steer` boolean (additive — golden wire tests updated deliberately for exactly these four pinned shapes plus the two new frames).
- **Negotiation** (`internal/signaling/router.go#negotiateSteer`): steer lands ONLY when mode != off AND engine monitor up AND (force | both members v1.2-marked). Force does NOT override an unavailable engine — that pairing degrades to P2P, because joining a steered session against a dead SFU hangs calls at the engine admission gate (documented in code).
- **Mode mixing is refused both directions** with `steer_mode_blocked` (`internal/signaling/{offer,answer,candidate}.go` + `steer.go`).
- **Steer handlers** (`internal/signaling/steer.go`): membership+enrolment gate (SessionForConn + engineSessions), payload guards mirroring the relay handlers, per-call 1.2s bound, engine failures → session-preserving `engine_unavailable` + classified logs; engine in-band errors map onto the CLOSED Go code vocabulary (engine codes never leak); remote-frame dispatch: `answer` → caller, `track.published` → peer, everything else logged+dropped (no blind relay).
- **Engine client**: typed `Offer/Trickle/Publish/Subscribe/Unsubscribe` wrappers.
- **Config**: `VOXDESK_GATEWAY_ENGINE_STEER=off|v1.2|force` (closed set; steer without engine URL = boot refusal), compose + .env.example + boot-log surface.
- **FORK REVISION (documenting honestly): the `/v1/bus` (design call A1) was NOT built.** During implementation it turned out every event the 2-member gateway model needs to reach the peer (track.published) already rides the same HTTP response that carries the caller's own pipeline acks — the Rust engine returns `reply + room_fanout` in one frame array (verified live in `it_live_test.go`: publish → real engine returns the `track.published` frame). A bus would move zero bytes the response path doesn't already deliver, so the response-fanout path shipped instead — same semantics, strictly smaller surface. The bus design is retained below for the multi-member-room feature line (3+ publishers per room, engine-side async events like sweep-expiry peer notification), where it becomes load-bearing again.
- **Rollout**: default off; v1.2 for upgraded pairs; force only after the dashboard ships 1.2.

## Gates at implementation end
- `go test -race -count=1 ./...`: **all 19 packages ok; 186 --- PASS / 0 FAIL** (was 177 before this leg).
- Live lane (`-tags it` against the real media-engine binary): PASS incl. publish-fanout assertion against real engine behavior.
- `go vet`, `gofmt -l`: clean. One test-side data race found by -race during development (scripted-engine list read without lock) — fixed; production paths race-clean.
- Rust: untouched this leg; 90/90 gate from the previous leg stands (steer needed ZERO engine changes — publish/offer/trickle/subscribe effects were already complete).
- Golden wire-format delta is deliberate and minimal: `steer` field on the two join acks + two new frame types.

---

# Amendment 2026-09-17 (b) — IMPLEMENTATION RECORD (Item 2: DTLS-SRTP termination SHIPPED, with two documented reality adaptations)

## Crate chosen & dependency-tree decision
- **`dimpl` =0.7.3, pinned exact** (algesten / str0m author; MIT OR Apache-2.0; `forbid(unsafe_code)`; Sans-IO **sync**, DTLS 1.2 + 1.3, RFC 5764 use_srtp export, actively maintained, WebRTC-aimed). It is the workspace's single allowed dependency tree; everything else remains hand-rolled.
- **Feature/entropy decision (documented):** dimpl's `default` features = aws-lc-rs crypto provider + rcgen cert generation — the author's audited default path; rcgen's P-256 generation is hard-coupled to aws-lc-rs upstream. The 100%-pure-RustCrypto alternative (`rust-crypto` feature) was evaluated and BUILD-VERIFIED in the sandbox, but loses cert generation entirely (no pure-Rust signing backend can generate the identity) — rejected. Consequence: the tree contains aws-lc_rs C bindings compiled with the system cc at build time (gcc 14 present in sandbox; multi-stage Docker builds likewise). If a zero-C build is ever required, the flag day is: switch feature set + supply the identity from out-of-tree key material.
- Self-signed ECDSA P-256 identity is generated **per boot** in `bins/media-engine/main.rs` (standard SFU practice); its SHA-256 fingerprint replaces the static `VOXDESK_DTLS_FINGERPRINT` label as the answer's `a=fingerprint`. The env var survives ONLY as a fixture pin (boot logs a loud WARN if it disagrees with the real identity, since browsers would otherwise fail the peer-cert check against the answer).

## What was built (files)
- **`crates/dtls`** (new, ours): role negotiation (passive default; active iff offer said `passive`), Sans-IO drive loop with buffer-grow + spin-cap, peer-cert SHA-256 fingerprint verification against the SDP pin BEFORE keys surface, RFC 5764 §4.2 role-aware split for ALL three profiles dimpl negotiates (CM 16/14/60B, GCM-128 16/12/56B, GCM-256 32/12/88B), and the typed `aes128_cm()` view that is the ONLY route into the engine's bind path.
- **transport demux**: RFC 7983 DTLS arm (byte0 20..64, ≥13B record header) — checked BEFORE STUN (content types 22..25 satisfy the STUN arm's loose mask; the bug was caught by the very first engine test).
- **signaling**: `OfferContext` now carries `setup` + `peer_fingerprint` (extracted from the offer: BUNDLE-first-media plus session fallback). ICE creds now ALSO fall back to media level — real browser SDP puts them there; before this leg the extraction read session level only, which would have yielded NO OfferContext/DtlsEndpoint at all against a genuine Unified-Plan offer (found and fixed during engine-level tests).
- **webrtc/sdp**: `build_answer` now mirrors RFC 5763 — `a=setup:passive` for actpass/active offers, `a=setup:active` for passive offers (previously hardcoded passive, which would hang a passive offerer).
- **engine**: `EngineConfig.dtls_identity` (None = documented fixture posture), per-session `dtls::Endpoint` spawned on offer with the settled role; `by_endpoint`-routed `on_dtls` in `media_step`; `kick_dtls` flushes the active-role ClientHello buffered pre-nomination onto the nominated 5-tuple; sweep cadence services DTLS retransmit timers; `bind_srtp` kept as the SDES-style fixture API (now delegating to the new directional `bind_srtp_split(session, CmKeys)`); stats counters `dtls_dropped/.established/.failed/.profile_refused`.
- **Fail-closed posture**: protocol error or fingerprint mismatch tears the association down and NEVER binds keys (`dtls_failed`); a successful handshake on a profile the engine crypto can't bind (non-CM) is surfaced as `dtls_profile_refused` with the association scrapped — the design's "only CM is bindable" rule, enforced at the boundary, never silent.

## Documented reality adaptations (why they exist, what's NOT done)
1. **The in-repo loopback cannot negotiate CM.** dimpl's client offer hardcodes `[GCM-256, GCM-128, CM]` and its server prefers GCM when offered — two dimpl instances ALWAYS land on GCM-256. A byte-level ClientHello rewrite was attempted and correctly rejected by the DTLS transcript signature (handshake transcripts include the CH). Resolution chosen: the shim exports/splits ANY negotiated profile correctly (`Negotiated`), and the ENGINE fails closed at the only bind gate (`Negotiated::aes128_cm()`), so CM negotiation — what browsers actually offer — lands identically at the same code, with the same split semantics (proven at engine-test and unit level), while the browser lane proves the production profile end-to-end.
2. **The live-`it`-lane browser round-trip (the design's planned pion/simplewebrtc UA + real Chrome/Firefox/Safari SDP verification) is NOT part of this leg and remains open** — it needs a browser-grade UA the sandbox doesn't have; the design doc already schedules it for rollout. Engine-side everything up to that UA's first handshake packet is implemented and gate-green.

## Gates at implementation end (exact numbers)
- `cargo test --workspace`: **107 passed / 0 failed** (90 pre-leg; +17 net: dtls shim 4, loopback 4, engine dtls 7, demux 1, sdp setup 1; a debug harness test was removed). Suite runs RTP/RTCP/STUN/SDP/SRTP/Pipeline flows unchanged.
- `cargo fmt --all -- --check`: CLEAN. `cargo clippy --workspace --all-targets`: **0 warnings, 0 errors**.
- `cargo build --release --workspace`: OK (1m39s, LTO profiles untouched).
- Go: `go test -race -count=1 ./...` → all 19 packages **ok** (unchanged from the steer leg; Go code not touched this leg); `go vet`/`gofmt -l` clean.
- Live `-tags it` lane against the REAL engine binary: **PASS**, now booting with a per-boot identity fingerprint emitted in the answer (verified in the lane's log: `dtls identity active (answer fingerprint D6:B8…)`).

## Still open for the next leg (honestly registered)
- Browser/pion-UA `it` lane (CM production profile end-to-end incl. Chrome/Firefox/Safari SDP interop).
- Optional AEAD-GCM support in `crates/webrtc/src/srtp.rs` (RFC 7714) — only if a browser ever phases CM out; today every WebRTC client offers AES128_CM_SHA1_80, and dimpl negotiates it against those offers (server picks from the client's list; CM is the only commonality with our crypto).
- `/v1/bus` for multi-member rooms (already registered by Amendment (a)).

---

## Amendment (c) — pion genuine-UA lane: CM DTLS-SRTP end-to-end PROVEN, and the interop bug only a real client could find

**Shipped 2026-09-18.** The remaining question after Amendment (b) — "does an honest, browser-shaped client actually interop?" — is now answered ON THE WIRE: two genuine pion/webrtc v4.2.20 peer connections (full-ICE agents with real ICE credentials, real DTLS clients, real SRTP crypto), both FORCED to offer exactly one protection profile (`AES_CM_128_HMAC_SHA1_80`), connect through the spawned REAL engine binary, exchange CM-decryptable media both directions, and fail assertions on any deviation.

### What was built

1. **`internal/engineclient/it_pion_test.go`** (`-tags it`, same `VOXDESK_IT_ENGINE` gates): alice (send) + bob (recv), CM-only client posture, browser-baseline MID header-extension registered, RFC 7943-compatible receiver (engine speaks the minimal-SDP SFU dialect — no per-leg `a=ssrc` rows — so undeclared-SSRC media is accepted deliberately, with SSRC lineage + payload equality asserted by hand to keep the laxity honest). Actually-verified invariants:
   - ICE connectivity (engine ICE-lite ↔ pion full agents, both sessions);
   - two DTLS handshakes, both CM (server cannot pick GCM — the clients' protection-profile list excludes it);
   - alice's RTP decrypts at the engine under HER RFC 5764 outbound split, routes, re-encrypts under bob's split;
   - bob's OnTrack delivers the frame: payload bytes bit-equal, SSRC == alice's engine-published SSRC;
   - ledger cross-check on the engine's `/metrics`: `voxdesk_media_dtls_total{established}` delta == 2, `profile_refused` delta == 0, `publish_refused == 0`.
2. **Wire v1.2 (additive): optional `ssrc` on `publish`** (`crates/protocol`, `signaling`, `engine`). The engine previously MINTED session SSRCs internally and never told the client — a genuine UA publishing its own RFC 3550 SSRC would have every packet dropped as unknown-ssrc. Present → engine binds that SSRC (collision or 0 → loud refusal + fanout retraction + `publish_refused` counter); absent → mint (fixtures untouched). go.mod now carries pion webrtc v4.2.20 + pion/dtls v3.1.9 (test-only adoption).
3. **Engine `/metrics`**: `voxdesk_media_dtls_total{established|failed|profile_refused}` and `voxdesk_media_publish_refused_total` — the pion lane's ledger assertions read these.
4. **`VOXDESK_STUN_DEBUG` diagnostic streams** (engine, opt-in): STUN parse-drops and 401 audit reasons (never credentials) — this leg would have been blind without them.

### THE find: self-consistent STUN HMAC (RFC 8489 §14.5), broken for every real client

The engine's own StunBuilder hashed `header(len-through-MI-value) + attrs + MESSAGE-INTEGRITY-attr-header` when signing/verifying. RFC 8489 §14.5 — and pion/stun's actual wire — hash only through the attribute PRECEDING MI (MI's own header excluded), with length patched to the end of the MI value. Both directions were *mutually self-consistent*, so every fixture test passed while **every browser-class client was 401'd at connectivity checks** (perpetual ICE "checking"). A pion-signed request captured off the wire, then a Python reference recompute against the engine's own credentials, pinned trial A (RFC framing) as the true MAC; fixture rewritten to a real captured pion frame (`pion_signed_binding_request_verifies_our_integrity`), signing+verifying corrected in `crates/webrtc/src/stun.rs`. Same-class fix Adam-style lessons: fixture-mutual-consistency is not wire truth.

### Collateral learnings baked into the lane (not hacked around)

- **Announce IP == packet source IP**: a genuine ICE agent discards success responses from an address that wasn't an advertised remote candidate ("no such remote") — same failure class as a mis-mapped NAT. The lane auto-selects the host's first non-loopback address (`VOXDESK_ANNOUNCE_IP` overrides) instead of papering over with the sandbox's loopback↔link-local quirk.
- pion puts ICE creds and the DTLS fingerprint at MEDIA level — the engine's existing media-level fallback (`signaling/lib.rs`, "BUNDLED browser SDP…") proved its worth; no change needed.
- pion does NOT register the MID header extension by default (browsers do); the UAs register it explicitly to stay in the browser set.

### Gates (all rerun after the final state)

- `cargo test --workspace`: **109 passed / 0 failed** (pre-leg 107; +engine publish-ssrc test, +pion-signed STUN regression; −1 debug scaffold removed).
- `cargo fmt --all -- --check`: clean. `cargo clippy --workspace --all-targets`: 0 warnings / 0 errors.
- `cargo build --release --workspace`: OK (22.8s; LTO profiles untouched).
- Go: `go test -race -count=1 ./...` → 18 ok / 0 fail (one fixed-package set; pion deps are it-tagged only). `go vet` (`-tags it` incl.) clean; `gofmt -l` clean.
- Live `-tags it`: base lane (`TestLiveEngineJoinOfferTrickleLeave`) PASS 0.14s; pion lane PASS **0.57s**, log line `pion lane: 10 CM-SRTP media frames verified end-to-end (alice ssrc=…)`.

### Still open for the next leg (honestly registered)

- Per-leg SDP fidelity: MID/SSRC rewriting so multiple media sections across different legs route without dialect concessions (today asserted with deliberately receiver-lax UAs on a single-media-section shape — honest, but not yet the full SFU picture).
- Optional AEAD-GCM in `crates/webrtc/src/srtp.rs` (RFC 7714) if CM ever leaves the browser set.
- Real-browser lane (Chrome via WebDriver) — the pion lane proves the production-profile crypto/ICE cases; the browser lane would re-prove the same SDP-shape claims against libwebrtc.
- `/v1/bus` for multi-member rooms (Amendment (a)).
```
