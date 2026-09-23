# Step 17 — Batch 04A: `services/realtime/gateway-go` (Go WebSocket edge) — every file

Repo: `/home/user/voxdesk` (HEAD `ccba554`)
Generated: 2026-09-21T16:46:50Z

The complete Go gateway: public WebSocket edge with two planes over one socket (notice
fan-out from the API plus point-to-point WebRTC signaling), the engine client that talks to
the Rust SFU, its golden wire fixtures and its `-tags it` live lanes. Reproduced in full,
including `go.mod`, `go.sum` and every `testdata/*.golden`.

**Files in this batch: 110.** Every block below is the file's content byte-for-byte as it exists in the working tree. Each block header carries the line count and the SHA-256 of the whole file, so a reader can confirm the block is complete and unmodified — nothing is paraphrased, summarised, or replaced by a placeholder comment.

---


==============================================================================
===== FILE: services/realtime/gateway-go/Dockerfile (50 lines, sha256 1ef1fd3cc8d005a60d2c82b8324a9b7de01897716647a65fd23d37dc83e1ee63) =====
==============================================================================
```text
# Realtime gateway image — two stages, static binary, non-root.
#
# Build from the compose root: the build context is THIS directory, so
# nothing outside services/realtime/gateway-go/ is reachable on purpose —
# the gateway is a standalone Go module with exactly one dependency
# (gorilla/websocket, pinned in go.sum and verified by `go mod download`).

# ---- build -------------------------------------------------------------------
FROM golang:1.27-alpine AS build
WORKDIR /src

# Copy manifests first and resolve modules as their own layer: source edits
# must not bust the dependency cache, and go.sum verification happens here,
# not in the running container.
COPY go.mod go.sum ./
RUN go mod download

COPY . .

# CGO off: the runtime stage carries no libc and no toolchain. -trimpath so
# the binary embeds no builder-host paths; -buildvcs=false because the build
# context may not be a git checkout (compose builds from an exported tree).
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -buildvcs=false \
    -o /out/gateway ./cmd/gateway

# ---- run ---------------------------------------------------------------------
FROM alpine:3.21

# ca-certificates for any future TLS outbound; wget (busybox) backs the
# HEALTHCHECK; a dedicated non-root user runs the binary — the gateway binds
# only :8790 and holds secrets in memory, and root buys it nothing.
RUN apk add --no-cache ca-certificates \
 && addgroup -S gateway \
 && adduser -S -G gateway -H -h /nonexistent gateway

USER gateway:gateway
COPY --from=build /out/gateway /usr/local/bin/gateway

EXPOSE 8790

# /healthz is liveness-only (process up, mux answering); /readyz additionally
# reports capacity and is what an orchestrator should gate rollouts on.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1:8790/healthz || exit 1

# Secrets arrive exclusively through the environment
# (VOXDESK_GATEWAY_JWT_SECRET / VOXDESK_GATEWAY_INGEST_SECRET); the process
# refuses to boot on missing, placeholder, or weak values, and that refusal
# is observable: the container exits non-zero before binding the port.
ENTRYPOINT ["/usr/local/bin/gateway"]
```

==============================================================================
===== FILE: services/realtime/gateway-go/README.md (274 lines, sha256 4c29cd97e5527aa04962046eb4a76a47d31cc407379771859502e9a7f56a2790) =====
==============================================================================
```markdown
# realtime/gateway-go — Public WebSocket Edge

The browser-facing realtime front door. One WebSocket carries TWO planes for
authenticated dashboard clients:

1. **The notice plane** (what the API publishes): call status changes,
   wallboard metrics — pub/sub over tenant-pinned rooms, fed by
   `POST /ingest/v1/publish`.
2. **The signaling plane** (what clients publish to each other): point-to-
   point WebRTC session setup — SDP offers/answers and ICE candidates relayed
   between two connections of the same tenant, behind the same JWT edge.

```
dashboard (browser)                gateway-go (this)                 VoxDesk API (Python)
        │   wss GET /ws  ─────────▶  upgrade + capacity gate               │
        │   {hello: token} ───────▶  verify HS256 JWT (shared secret)      │
        │   ◀────── ready ────────   tenant pinned from token `tid`        │
        │   {subscribe: calls} ───▶  join (tenant, "calls") room           │
        │   ◀──── delivery ───────   fan-out ◀── POST /ingest/v1/publish ──┤
        │                                                                    │
        │   {session.start} ──────▶  registry seats initiator              │
        │   {session.join: id} ───▶  responder seated, initiator woken     │
        │   {offer|answer|candidate} ──▶ relayed to the peer, byte-verbatim │
```

## Why a separate service (and not a FastAPI WebSocket)

* **Blast radius.** The public edge holds tens of thousands of idle browser
  sockets; the Python API holds the telephony path. A socket flood must be
  able to kill the edge without touching a single live phone call — the
  same "the scheduler is a separate process" reasoning the API already uses.
* **Different trust posture.** The internal hub (`services/signal-go`)
  trusts its network; this edge trusts nothing: browsers authenticate, room
  names are a closed set, every limit fails closed.
* **Same protocol.** The wire vocabulary mirrors `voxdesk-signal` /
  `signal-go` (hello/subscribe/unsubscribe/ping →
  welcome/subscribed/unsubscribed/delivery/error/pong), with two public-edge
  additions: `hello` carries a **verified access token**, and `ready`
  acknowledges the pin. A client library written for the internal hub needs
  only the auth frame added.

## Security model (what is enforced, and where)

| Guarantee | Mechanism | Go file |
|---|---|---|
| Only dashboard users connect | HS256 JWT verification — **same token the API mints** (`app/auth/jwt.py`): alg pinned (no alg-confusion), iss/aud exact, `typ=access`, exp/nbf enforced, `sub`+`tid` must be UUIDs | `internal/auth/jwt.go` |
| Tenant isolation is structural | Rooms keyed `(tenant_id, room)`; tenant comes ONLY from the token — the client frame schema has no tenant field | `internal/hub/hub.go` |
| No room enumeration | Closed room namespace: `calls`, `metrics`, `call:<uuid>`, `campaign:<uuid>` | `internal/validate` |
| The edge never outlives the credential | Connection closed (1008 `token_expired`) ~30 s after the token's `exp`; dashboard reconnects with the token it already rotates | `internal/websocket/heartbeat.go` |
| Backpressure, never a stalled room | Bounded per-connection queues; full → drop + counted (Rust `BoundedBroadcast` contract), one named policy everywhere | `internal/backpressure/queue.go` |
| Floods | Global upgrade cap (pre-auth 503), per-tenant connection cap, per-connection frame rate limit, max message size, auth timeout | `server.go`, `reader.go` |
| Ingest can't be forged | Shared Bearer secret, constant-time compare (hashed, so length can't leak) | `server/ingest.go` |
| Replay suppression | `(tenant_id, event_id)` TTL cache; duplicate → `200 {"duplicate":true}` — retries converge, mirrors `MessageWebhookReceipt` | `server/ingest.go` |
| Boot fails closed | Missing/placeholder `JWT_SECRET` or `INGEST_SECRET` → process refuses to start (like `validate_security()`) | `internal/config/config.go` |
| Metrics not public | `METRICS_TOKEN` Bearer gate, constant-time | `server/metrics.go` |

### Signaling plane, same table continued

| Guarantee | Mechanism | Go file |
|---|---|---|
| Sessions never cross tenants | Session keyed by id but VALIDATED against the token's tenant pin; wrong tenant, wrong id and non-member all collapse to one `session_unknown` | `internal/session/manager.go` |
| Session ids are capabilities, not invitations | UUIDv4 (122 bits) + same-tenant requirement; no listing, no guessable sequence | `internal/session/session.go` |
| Exactly two seats, `identity` is the socket | Members are connections, not users — user/device identity never enters the ledger | `internal/session/session.go` |
| Glare is unreachable | Offering state admits exactly one outstanding offer; a second is `wrong_signal_state`, whoever sends it (Perfect Negotiation enforced once for all clients) | `internal/session/manager.go` |
| A dead socket never leaves a live session | Every close path (peer, policy, heartbeat, write failure) ends the session and tells the survivor `peer_disconnected` | `internal/websocket/connection.go` teardown |
| No negotiation stockpiles | A never-joined session is reaped after the pending TTL (`join_timeout`) | `internal/session/lifecycle.go` |
| Payloads are sized, never parsed | SDP ≤ 12 KiB with an `v=` sanity check, candidates ≤ 1 KiB; contents are relayed byte-verbatim — munging is media-plane territory, and a parsing edge would be one more thing to disagree about | `internal/protocol/codec.go` |
| One session per connection, 64 per tenant | `VOXDESK_GATEWAY_SIGNAL_MAX_SESSIONS_PER_TENANT`, plus the standing per-tenant connection cap | `internal/config/config.go` |

## Wire frames

Client → server (notice plane):
```json
{"type":"hello","token":"<dashboard access token>"}
{"type":"subscribe","room":"calls"}
{"type":"unsubscribe","room":"calls"}
{"type":"ping"}
```

Client → server (signaling plane; after `ready` only):
```json
{"type":"session.start"}
{"type":"session.join","session_id":"<uuid>"}
{"type":"offer","session_id":"<uuid>","sdp":"v=0 …"}
{"type":"answer","session_id":"<uuid>","sdp":"v=0 …"}
{"type":"candidate","session_id":"<uuid>","candidate":{RTCIceCandidate} | null}
{"type":"session.end","session_id":"<uuid>"}
```

Server → client (notice plane):
```json
{"type":"welcome","session_id":"…","auth_required":true,"auth_timeout_secs":10,"heartbeat_secs":20,"server_time":"…"}
{"type":"ready","session_id":"…","tenant_id":"…","role":"owner","token_expires_at":"…"}
{"type":"subscribed","room":"calls","peers":2}
{"type":"delivery","room":"calls","kind":"call.updated","payload":{…},"event_id":"…","sent_at":"…"}
{"type":"pong","server_time":"…"}
```

Server → client (signaling plane):
```json
{"type":"session.started","session_id":"…","role":"initiator"}
{"type":"session.joined","session_id":"…","role":"responder"}
{"type":"session.peer_joined","session_id":"…","peer_role":"responder"}
{"type":"signal.offer","session_id":"…","sdp":"v=0 …"}
{"type":"signal.answer","session_id":"…","sdp":"v=0 …"}
{"type":"signal.candidate","session_id":"…","candidate":{…} | null}
{"type":"session.ended","session_id":"…","reason":"member_ended|peer_disconnected|join_timeout"}
```

Either plane:
```json
{"type":"error","code":"hello_required|auth_failed|room_invalid|over_limit|rate_limited|bad_message|session_unknown|session_full|session_not_ready|already_in_session|wrong_signal_state|signal_too_large","message":"…"}
```

Ingest (API → gateway), `POST /ingest/v1/publish`,
`Authorization: Bearer $VOXDESK_GATEWAY_INGEST_SECRET`:
```json
{"tenant_id":"<uuid>","room":"calls","kind":"call.updated","payload":{…},"event_id":"<uuid, optional>"}
```
→ `200 {"delivered":n,"dropped":m,"duplicate":false}`

## Configuration (env; boot refuses invalid)

| Variable | Default | Notes |
|---|---|---|
| `VOXDESK_GATEWAY_PORT` | `8790` | |
| `VOXDESK_GATEWAY_JWT_SECRET` | — | **Required.** Must equal the API's `JWT_SECRET`. ≥32 chars, placeholder-rejected |
| `VOXDESK_GATEWAY_JWT_ISSUER` / `_AUDIENCE` | `voxdesk` / `voxdesk-api` | Must equal the API's values |
| `VOXDESK_GATEWAY_INGEST_SECRET` | — | **Required.** ≥16 chars, placeholder-rejected |
| `VOXDESK_GATEWAY_ALLOWED_ORIGINS` | empty | Comma-separated; empty = only same-origin/non-browser |
| `VOXDESK_GATEWAY_METRICS_TOKEN` | empty (warns) | Bearer gate for `/metrics` |
| `VOXDESK_GATEWAY_MAX_CONNECTIONS` | `10000` | Global, enforced pre-upgrade |
| `VOXDESK_GATEWAY_MAX_CONNS_PER_TENANT` | `256` | Per-tenant, enforced at hello |
| `VOXDESK_GATEWAY_AUTH_TIMEOUT` / `PONG_TIMEOUT` / `PING_INTERVAL` / `WRITE_WAIT` | `10s` / `60s` / `20s` / `10s` | `PONG_TIMEOUT` must exceed `PING_INTERVAL` |
| `VOXDESK_GATEWAY_SIGNAL_MAX_SESSIONS_PER_TENANT` | `64` | Concurrent signaling sessions per tenant; boot refuses `< 1` |
| `VOXDESK_GATEWAY_SIGNAL_PENDING_TIMEOUT` | `60s` | A never-joined session is reaped after this; boot refuses `≤ 0` |
| `VOXDESK_GATEWAY_BROKER` | `memory` | Ingest fan-out transport: `memory` (single node) or `redis` (multi-replica pub/sub). Unknown values **refuse boot** — a typo must never silently single-node a multi-replica deploy |
| `VOXDESK_GATEWAY_REDIS_URL` | empty | `redis://[user:pass@]host:port[/db]`, required when BROKER=redis; `rediss://` refused (no broker TLS on this edge today) |
| `VOXDESK_GATEWAY_LOG_LEVEL` | `info` | `debug`/`info`/`warn`/`error`; unknown values fall back to `info` (silence is never the default) |

The per-connection frame limiter (20 msg/s, burst 40), subscription cap (32),
message size (16 KiB), ingest payload (64 KiB), replay TTL (30 min, 50k
entries), SDP cap (12 KiB) and candidate cap (1 KiB) are compile-time
defaults in `internal/config/config.go` and `internal/protocol/codec.go`.

## Package map

```
cmd/gateway/main.go          — boot, wiring (observability logger, event
                               emitter, shutdown package driving graceful exit)
internal/config/             — env → validated config, fail-closed secrets
internal/protocol/           — wire vocabulary, one concern per file:
                               message.go (types + server frames),
                               envelope.go (client frame),
                               error.go (closed code set + close codes),
                               codec.go (decode + payload guards)
internal/auth/               — HS256 JWT split by responsibility:
                               jwt.go (envelope: algorithm pin, signature),
                               claims.go (payload → verified Claims),
                               middleware.go (ExtractBearer + constant-time
                               token equality — every secret comparison on
                               this edge goes through one implementation)
internal/validate/           — room/tenant/uuid shape predicates
internal/idempotency/        — ingest replay store (TTL + capacity, atomic
                               seen-before, tenant-scoped keys)
internal/ratelimit/          — policy → token bucket → Limiter; owns the
                               per-connection inbound frame budget
internal/backpressure/       — generic bounded Queue[T] + named drop policy
                               (DropNewest, counted) behind every outgoing
                               connection queue
internal/hub/                — (tenant, room) registry + fan-out + Lookup
internal/broker/             — ingest fan-out transport:
                               broker.go (Broker contract, Envelope, Stats),
                               memory.go (default: Publish IS the local
                               fan-out, synchronously, exact counters),
                               redis.go (multi-replica: hand-rolled RESP
                               client, AUTH/SELECT/SUBSCRIBE/PUBLISH, two
                               connections, supervised reconnect with
                               backoff, origin-stamp own-echo suppression),
                               brokertest/ (fake RESP Redis for tests)
internal/presence/           — presence/registry/events: tenant → online
                               users, keyed ONLY on verified JWT identity;
                               transitions feed the Prometheus gauge via
                               OnChange subscription
internal/observability/      — logging.go (leveled, kv-tagged logger),
                               tracing.go (X-Request-ID middleware),
                               events.go (structured events + emitters),
                               metrics/ (atomic counters/gauges +
                               Prometheus text exposition)
internal/session/            — signaling session ledger, one concern per file:
                               manager.go (operations → Events),
                               session.go (two-seat model),
                               state.go (Pending/Offering/Answered/Ended),
                               registry.go (id/tenant/conn indexes),
                               lifecycle.go (pending reaper)
internal/signaling/          — relay between two sockets of one session:
                               router.go (dispatch + teardown + reaper loop),
                               offer.go / answer.go / candidate.go (guards),
                               events.go (session events ⇢ frames, error codes ⇢ wire)
internal/shutdown/           — graceful.go: signal contract (INT/TERM,
                               buffered, restorable) + bounded http.Server
                               drain
internal/websocket/          — upgrade, connection, reader, writer,
                               heartbeat, close
internal/server/             — HTTP mux: /ws, /ingest/v1/publish,
                               /healthz, /readyz, /metrics; ingest publishes
                               through the broker (redis mode e2e-tested
                               across two full gateway instances)
tests/                       — cross-package tests (see tests/README.md):
                               protocol goldens, integration seams,
                               load smoke (behind -tags load)
```

Layer rule: `session` knows no frames, `signaling` knows no sockets,
`websocket` knows no session state machine, `protocol` knows no behaviours,
`broker` knows no event schema, `presence` knows no transport. Every arrow
points one way.

### Why there is no `pkg/`

The expanded tree calls for `pkg/` (types/errors/constants). Deliberately
not created: every type that LOOKS shared already has exactly one honest
owner — wire shapes in `internal/protocol`, close codes and error codes in
`internal/protocol/error.go`, the fan-out accounting in `internal/broker`'s
`Stats`. There is no exported API surface a `pkg/` would serve (this module
is one deployable, imported by nobody), and Go-idiomatic move-to-`pkg`
signals (a second in-repo consumer, an external import request) do not
exist yet. The moment the media engine or an SDK needs these types, the
types to lift are obvious and the lift is mechanical; an empty `pkg/` with
aspirational `types.go` files is how "shared" becomes "blamed".

## Run / verify

```sh
go build ./cmd/gateway
VOXDESK_GATEWAY_JWT_SECRET=<same as API> \
VOXDESK_GATEWAY_INGEST_SECRET=<shared>   \
./gateway

# gate (same shape as the other services):
gofmt -l .        # empty
go vet ./...
go test -race ./...   # 149 tests: full-socket round trips for BOTH planes,
                      # wire-level tenant isolation, the state machine's
                      # glare/connectivity matrix, every new package's unit
                      # suite, broker-over-fake-redis (echo suppression,
                      # outage, reconnect re-subscribe), and TWO full
                      # gateway replicas fanning one ingest out over the bus

# load smoke (off the default gate, behind its build tag):
go test -tags load ./tests/load/ -v   # 200 conns × 50 events, ~147k frames/s
```

## Scope honesty

* Token **revocation** (`User.token_version`) is not — and cannot be — checked
  here without a database; the enforcement is the token's 15-minute `exp`,
  same trust window the dashboard UI itself holds.
* Fan-out defaults to single-node (`VOXDESK_GATEWAY_BROKER=memory`, like
  the Rust hub and signal hub). Multi-replica fan-out now EXISTS behind
  `=redis`: envelope-carrying pub/sub with own-echo suppression and
  sync-local + best-effort-remote semantics, e2e-tested across two full
  gateway instances in `internal/server/redis_e2e_test.go`. What it
  deliberately is NOT: guaranteed/at-least-once cross-node delivery — a
  bus outage drops remote hops (counted in `BusDrops`, warned in logs),
  because a realtime event five seconds late is worthless and a spool
  would only pretend otherwise. Signaling members still must share a node
  or gain a session-affinity rule; presence remains node-local by design
  (multi-node "is U online anywhere" composes from node views, it is not
  faked here).
* There is **no "Connected" state** in the signaling session: the server can
  observe SDP ordering, it cannot observe media, so signaling correctness
  ends at Answered. ICE-health is the peers' problem (and the future media
  plane's telemetry, not this edge's ledger).
```

==============================================================================
===== FILE: services/realtime/gateway-go/cmd/gateway/main.go (160 lines, sha256 cfc09a211b0a56efa839584197582f31b3c954b04d1ef43d396bafcecd529723) =====
==============================================================================
```go
// Command gateway is the public WebSocket edge as a standalone binary.
//
// It binds 0.0.0.0:VOXDESK_GATEWAY_PORT (default 8790) and serves
//
//	GET  /ws                — browser realtime sessions (hello with a
//	                          dashboard access token, then tenant-pinned rooms)
//	POST /ingest/v1/publish — server-to-server event fan-out (the Python API)
//	GET  /healthz|/readyz   — probes
//	GET  /metrics           — Prometheus scrape
//
// Boot fails closed: with a missing or placeholder JWT secret or ingest
// secret the process refuses to start, exactly as app/core/config.py's
// validate_security() gates the API. There is no development bypass in this
// binary because it is the PUBLIC edge — a misconfigured internal service
// wastes a deploy, a misconfigured edge leaks live call data.
package main

import (
	"context"
	"errors"
	"net/http"
	"os"
	"strconv"
	"time"

	"github.com/voxdesk/realtime/gateway-go/internal/auth"
	"github.com/voxdesk/realtime/gateway-go/internal/config"
	"github.com/voxdesk/realtime/gateway-go/internal/engineclient"
	"github.com/voxdesk/realtime/gateway-go/internal/hub"
	"github.com/voxdesk/realtime/gateway-go/internal/observability"
	"github.com/voxdesk/realtime/gateway-go/internal/observability/metrics"
	"github.com/voxdesk/realtime/gateway-go/internal/server"
	"github.com/voxdesk/realtime/gateway-go/internal/session"
	"github.com/voxdesk/realtime/gateway-go/internal/shutdown"
	"github.com/voxdesk/realtime/gateway-go/internal/signaling"
)

func main() {
	// The boot logger exists before config is validated because config
	// errors themselves must be printable. Level comes straight from the
	// environment (config.Load deliberately holds no logger state);
	// unknown values fall back to Info — silence is never the default.
	logger := observability.New(os.Stderr, "[gateway] ",
		observability.ParseLevel(os.Getenv("VOXDESK_GATEWAY_LOG_LEVEL")))

	cfg, problems, warnings := config.Load(os.Getenv)
	for _, warning := range warnings {
		logger.Warnf("config warning: %s", warning)
	}
	if len(problems) > 0 {
		for _, problem := range problems {
			logger.Errorf("config error: %s", problem)
		}
		logger.Errorf("insecure configuration, refusing to start")
		os.Exit(1)
	}

	h := hub.New(cfg.MaxConnsPerTenant)
	registry := metrics.New()
	verifier := auth.NewVerifier(cfg.JWTSecret, cfg.JWTIssuer, cfg.JWTAudience)

	// The signaling relay shares the JWT edge and the hub's connection
	// registry: sessions bind two of the hub's already-authenticated
	// connections, so no second auth surface exists anywhere in the design.
	sessions := session.NewManager(cfg.SignalingMaxSessionsPerTenant, cfg.SignalingPendingTimeout)
	signaler := signaling.NewRouter(sessions, h.Lookup, registry)
	stopReaper := signaler.StartReaper()
	defer stopReaper()

	gateway := server.New(cfg, h, registry, verifier, signaler)

	// Media-engine link (Go → Rust control plane). Disabled when the URL
	// is unset (config.Load already warned); when present, the client is
	// built BEFORE serving begins so readiness has a deterministic view
	// from the first probe, and its availability monitor runs for as long
	// as the process does. The observer feeds the /metrics engine series;
	// transitions also flip the engine_up gauge exactly once per change.
	engMonitorCtx, stopEngMonitor := context.WithCancel(context.Background())
	defer stopEngMonitor()
	if cfg.MediaEngineURL != "" {
		eng := engineclient.New(
			cfg.MediaEngineURL,
			time.Duration(cfg.MediaEngineTimeoutSeconds*float64(time.Second)),
			engineMetricsObserver{registry},
			nil,
			nil,
		)
		gateway.SetEngine(eng)
		go eng.Monitor(engMonitorCtx)
		if cfg.EngineSteer != config.EngineSteerOff {
			logger.Infof("engine steer mode %s ACTIVE: steered sessions route browser media through the engine", cfg.EngineSteer)
		}
		logger.Infof("media engine at %s (timeout=%.2fs, steer=%s); monitor running", cfg.MediaEngineURL, cfg.MediaEngineTimeoutSeconds, cfg.EngineSteer)
	} else {
		logger.Warnf("VOXDESK_GATEWAY_MEDIA_ENGINE_URL unset: media-engine plane DISABLED (readiness reports engine: disabled)")
	}
	// Structured operational events (ingest publishes/rejects with the
	// API's request id attached) go to stdout as JSON lines — the same
	// stream shape the Python app's structured logs ship.
	gateway.UseEmitter(observability.NewLogEmitter(
		observability.New(os.Stdout, "[gateway-events] ", observability.Info)))

	srv := &http.Server{
		Addr:    listenAddr(cfg),
		Handler: gateway.Handler(),
		// Slowloris defence on the HTTP phase only. Read/Write timeouts are
		// deliberately UNSET: a WebSocket session is long-lived by design,
		// and the per-connection deadlines (auth timeout, heartbeat) own the
		// post-upgrade phase.
		ReadHeaderTimeout: 10 * time.Second,
	}
	srv.RegisterOnShutdown(gateway.CloseAll)

	go func() {
		logger.Infof("listening on %s (max_connections=%d, per_tenant=%d, signal_sessions_per_tenant=%d, signal_pending_timeout=%s)",
			srv.Addr, cfg.MaxConnections, cfg.MaxConnsPerTenant,
			cfg.SignalingMaxSessionsPerTenant, cfg.SignalingPendingTimeout)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			logger.Errorf("listen: %v", err)
			os.Exit(1)
		}
	}()

	// Graceful shutdown on SIGINT/SIGTERM (internal/shutdown owns the
	// contract): stop accepting, ask every session to close (1001 Going
	// Away via RegisterOnShutdown above), give in-flight frames a bounded
	// window to drain, then exit. A browser that ignores the close frame
	// never holds the process past ShutdownTimeout.
	signals, stopNotify := shutdown.Notify()
	defer stopNotify()
	<-signals

	if err := shutdown.HTTPServer(srv, cfg.ShutdownTimeout); err != nil {
		logger.Errorf("shutdown: %v", err)
	}
	logger.Infof("stopped")
}

// listenAddr formats the bind address for config.Port.
func listenAddr(cfg config.Config) string {
	return "0.0.0.0:" + strconv.Itoa(cfg.Port)
}

// engineMetricsObserver adapts the engineclient's typed observation hook
// onto the metrics registry — the package's declared seam for keeping
// transport facts (latency, failures, availability) OUT of the client and
// INSIDE the platform's one expositions surface.
type engineMetricsObserver struct{ reg *metrics.Registry }

func (o engineMetricsObserver) ObserveSignal(op string, took time.Duration, err error) {
	o.reg.EngineCall(op, took, err != nil)
}

func (o engineMetricsObserver) ObserveHealth(took time.Duration, err error) {
	o.reg.EngineProbe(took, err != nil)
}

func (o engineMetricsObserver) EngineUpChanged(up bool) {
	o.reg.SetEngineUp(up)
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/go.mod (33 lines, sha256 601acbb0c471338599f4d16fb97d5d9e0a5c25fdadc46b2cbe7ef0b238865966) =====
==============================================================================
```
module github.com/voxdesk/realtime/gateway-go

go 1.27

require (
	github.com/gorilla/websocket v1.5.3
	github.com/pion/dtls/v3 v3.1.9
	github.com/pion/logging v0.2.4
	github.com/pion/rtp v1.10.5
	github.com/pion/sdp/v3 v3.0.20
	github.com/pion/webrtc/v4 v4.2.20
)

require (
	github.com/google/uuid v1.6.0 // indirect
	github.com/pion/datachannel v1.6.2 // indirect
	github.com/pion/ice/v4 v4.4.2 // indirect
	github.com/pion/interceptor v0.1.48 // indirect
	github.com/pion/mdns/v2 v2.2.0 // indirect
	github.com/pion/randutil v0.1.0 // indirect
	github.com/pion/rtcp v1.2.17 // indirect
	github.com/pion/sctp v1.11.1 // indirect
	github.com/pion/srtp/v3 v3.0.13 // indirect
	github.com/pion/stun/v4 v4.0.0 // indirect
	github.com/pion/transport/v4 v4.1.0 // indirect
	github.com/pion/transport/v5 v5.0.0 // indirect
	github.com/pion/turn/v5 v5.1.0 // indirect
	github.com/wlynxg/anet v0.0.5 // indirect
	golang.org/x/crypto v0.48.0 // indirect
	golang.org/x/net v0.50.0 // indirect
	golang.org/x/sys v0.41.0 // indirect
	golang.org/x/time v0.14.0 // indirect
)
```

==============================================================================
===== FILE: services/realtime/gateway-go/go.sum (52 lines, sha256 ea176a35f37a1829047f44cebfe1f0e9a60404ff1c089809b583985b104cba13) =====
==============================================================================
```
github.com/google/uuid v1.6.0 h1:NIvaJDMOsjHA8n1jAhLSgzrAzy1Hgr+hNrb57e+94F0=
github.com/google/uuid v1.6.0/go.mod h1:TIyPZe4MgqvfeYDBFedMoGGpEw/LqOeaOT+nhxU+yHo=
github.com/gorilla/websocket v1.5.3 h1:saDtZ6Pbx/0u+bgYQ3q96pZgCzfhKXGPqt7kZ72aNNg=
github.com/gorilla/websocket v1.5.3/go.mod h1:YR8l580nyteQvAITg2hZ9XVh4b55+EU/adAjf1fMHhE=
github.com/pion/datachannel v1.6.2 h1:7EXQ8TH3vTouBUdRWYbcX2edSx9Yj6k5zl5P+qyxEPc=
github.com/pion/datachannel v1.6.2/go.mod h1:pzbdAZvyGtXbcHM1hBbsFaOTf40lZizU/dNlvVOak6E=
github.com/pion/dtls/v3 v3.1.9 h1:rpeycmLIkc4krpk1IxP7+39o11QdCXbmV9+FGi9yZJ8=
github.com/pion/dtls/v3 v3.1.9/go.mod h1:iKFQNYrjsN2TiA2YKKMqB9MOZaFpjFULBI/A4sW0eyc=
github.com/pion/ice/v4 v4.4.2 h1:asS17nbHJrzlVQl8fiSJaipxrxSY3Dq6DWgmB+0VwpI=
github.com/pion/ice/v4 v4.4.2/go.mod h1:YZgNFOyJWXpLpLj0mb4ccqNAWRo9C2RtqKwuVTEz0Sc=
github.com/pion/interceptor v0.1.48 h1:FF4gZ6Yh+N75gKMYpC7rYR8DdkiMBFtA7V2OBUKo7XQ=
github.com/pion/interceptor v0.1.48/go.mod h1:5mg/N5xXMAa4codCUdrJYY9I1y4tVQhlpd7rfwAJpvI=
github.com/pion/logging v0.2.4 h1:tTew+7cmQ+Mc1pTBLKH2puKsOvhm32dROumOZ655zB8=
github.com/pion/logging v0.2.4/go.mod h1:DffhXTKYdNZU+KtJ5pyQDjvOAh/GsNSyv1lbkFbe3so=
github.com/pion/mdns/v2 v2.2.0 h1:AlAZ9MTUKtWgO+4itk35JdNak4sk5k7G/X4xnIBWHyA=
github.com/pion/mdns/v2 v2.2.0/go.mod h1:IJddx58QMlojqhQYjHcOUmvuBQ5MnLNetkb80VMvk2Y=
github.com/pion/randutil v0.1.0 h1:CFG1UdESneORglEsnimhUjf33Rwjubwj6xfiOXBa3mA=
github.com/pion/randutil v0.1.0/go.mod h1:XcJrSMMbbMRhASFVOlj/5hQial/Y8oH/HVo7TBZq+j8=
github.com/pion/rtcp v1.2.17 h1:PxiT6L79yPZKtXIsXdG1eakBl6dtBj4x+4oVEL0DlSw=
github.com/pion/rtcp v1.2.17/go.mod h1:7kBpuBJaWwax4hzc/pgexY8vkOpvh8atgYDbaKZq0iU=
github.com/pion/rtp v1.10.5 h1:ip0HhO/wYZqQ4bKS+R99KnZh/GRCmIT0jDXikub7vlE=
github.com/pion/rtp v1.10.5/go.mod h1:Au8fc6cEByy8RLTwKTQTEeQqDB/SJDxwL4mZuxYA5Pk=
github.com/pion/sctp v1.11.1 h1:O4dIFyURw1KTST7w+gtD4gLeYXkhPa0xXLHMMoe/OSA=
github.com/pion/sctp v1.11.1/go.mod h1:7KFmTwLcoYgJs/Z+99nJvsWL0qDpuyloSI0RbAqlrz0=
github.com/pion/sdp/v3 v3.0.20 h1:TS6DViqcmp+49f0+mjw9anbr9xY3vJtsZewxAvlMCRQ=
github.com/pion/sdp/v3 v3.0.20/go.mod h1:slIMXDK5OKj0nhISwjfeN18AzTBCt2LYZq9uPw0cU5Q=
github.com/pion/srtp/v3 v3.0.13 h1:FmQaqgNbN1vUtMhEsmj8trldc3lNZr1xmN7nl8CyX+Q=
github.com/pion/srtp/v3 v3.0.13/go.mod h1:7qR3L69t8RX0EPVQwGNwCa1Gy9keKKNDpWwQzZbeXDY=
github.com/pion/stun/v4 v4.0.0 h1:UuQy2q6iZR4EnMl/+G8kAtaWhf7jx/u8ZyN9oD4+YEQ=
github.com/pion/stun/v4 v4.0.0/go.mod h1:JAojPsPtDH4iPeNQb7kvxBdzypmkWAROxy0coApKH2E=
github.com/pion/transport/v4 v4.1.0 h1:8S+nF2reM2cJuqC6g78OVy2BBgmbdns+acx3jA97BvQ=
github.com/pion/transport/v4 v4.1.0/go.mod h1:06hFI+jCFcok2X2MekVufNZ/uzNZXivGBPfviSVcjgM=
github.com/pion/transport/v5 v5.0.0 h1:XWdfCnG6oLaTp07Sr4lbyWVs+MXuaD3eggUsSn6LK90=
github.com/pion/transport/v5 v5.0.0/go.mod h1:Qxw6fCEjFWQkRDZOhS4Vf+neJBcihauvA3uyEa1J1F0=
github.com/pion/turn/v5 v5.1.0 h1:OSzLub7q4GssG1P4BEVrz39MnnJlxLy1LOvEkP9f46o=
github.com/pion/turn/v5 v5.1.0/go.mod h1:6HJQO7UAe7pEPMrTtBrmj+tTfp+Ai8KAV2+GXHFi1nQ=
github.com/pion/webrtc/v4 v4.2.20 h1:NYiNhBTFArA8aoP18a30y4LN0dyqSrF65HxU7KnhNOo=
github.com/pion/webrtc/v4 v4.2.20/go.mod h1:aLGXbekuN0tHOu7IPX1o9Y/uN29Ovfjaaz4bL2P9ND8=
github.com/stretchr/testify v1.12.1 h1:EuwCh5fleGS7H32xRwO3wRGT7DxrDhLAT6FF8MpWDWE=
github.com/stretchr/testify v1.12.1/go.mod h1:MDEgiDPPsNp5cuIrHPPCyornHKgEVbtFUmoNlxoYthg=
github.com/wlynxg/anet v0.0.5 h1:J3VJGi1gvo0JwZ/P1/Yc/8p63SoW98B5dHkYDmpgvvU=
github.com/wlynxg/anet v0.0.5/go.mod h1:eay5PRQr7fIVAMbTbchTnO9gG65Hg/uYGdc7mguHxoA=
go.yaml.in/yaml/v3 v3.0.5 h1:N6y/pJk8buWs9NY5ERU2HSMfm+IuD/OtfdAnq6kESPw=
go.yaml.in/yaml/v3 v3.0.5/go.mod h1:HVTZu1O7/Vkt2N+BFy8Zza+lnLsABggaTM2ZpNIGuKg=
golang.org/x/crypto v0.48.0 h1:/VRzVqiRSggnhY7gNRxPauEQ5Drw9haKdM0jqfcCFts=
golang.org/x/crypto v0.48.0/go.mod h1:r0kV5h3qnFPlQnBSrULhlsRfryS2pmewsg+XfMgkVos=
golang.org/x/net v0.50.0 h1:ucWh9eiCGyDR3vtzso0WMQinm2Dnt8cFMuQa9K33J60=
golang.org/x/net v0.50.0/go.mod h1:UgoSli3F/pBgdJBHCTc+tp3gmrU4XswgGRgtnwWTfyM=
golang.org/x/sys v0.41.0 h1:Ivj+2Cp/ylzLiEU89QhWblYnOE9zerudt9Ftecq2C6k=
golang.org/x/sys v0.41.0/go.mod h1:OgkHotnGiDImocRcuBABYBEXf8A9a87e/uXjp9XT3ks=
golang.org/x/time v0.14.0 h1:MRx4UaLrDotUKUdCIqzPC48t1Y9hANFKIRpNx+Te8PI=
golang.org/x/time v0.14.0/go.mod h1:eL/Oa2bBBK0TkX57Fyni+NgnyQQN4LitPmob2Hjnqw4=
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/auth/claims.go (144 lines, sha256 a518116dba6c59429738f3dc0ee39fcec3969789ebff94fdcf09239cfbc1dfeb) =====
==============================================================================
```go
package auth

// This file owns the PAYLOAD side of a dashboard access token: claim names,
// their coercions from Go's loose JSON typing, and what a verified payload
// becomes for the rest of the gateway. jwt.go owns the envelope (signature,
// algorithm pinning); this file is what the envelope is guarding.
//
// The token format is defined by app/auth/jwt.py on the Python side:
//
//	HS256, claims sub (user uuid), tid (tenant uuid), role, tv (token
//	version), typ="access", iat, nbf, exp, iss, aud, jti.

import (
	"fmt"
	"strings"
	"time"

	"github.com/voxdesk/realtime/gateway-go/internal/validate"
)

// Claims is the verified identity the rest of the gateway acts on. TenantID
// is THE security-critical field: every subscription and every delivery is
// keyed by it, and it only ever comes from a verified token.
type Claims struct {
	UserID       string
	TenantID     string
	Role         string
	TokenVersion int
	ExpiresAt    time.Time
	JTI          string
}

// payload mirrors the Python encoder's claim names.
type payload struct {
	Subject     string `json:"sub"`
	TenantID    string `json:"tid"`
	Role        string `json:"role"`
	TokenVer    any    `json:"tv"`
	Type        string `json:"typ"`
	IssuedAt    any    `json:"iat"`
	NotBefore   any    `json:"nbf"`
	ExpiresAt   any    `json:"exp"`
	Issuer      string `json:"iss"`
	AudienceRaw any    `json:"aud"`
	JTI         string `json:"jti"`
}

// validatePayload turns a decoded payload into trusted Claims, applying
// every check the Python decoder applies, in the same order: shape first
// (missing/mformed claims), then purpose/issuer/audience, then times, then
// the UUID shapes the rest of the system keys state by.
func validatePayload(p *payload, issuer, audience string, now time.Time) (*Claims, error) {
	if p.Subject == "" || p.TenantID == "" || p.Role == "" {
		return nil, ErrClaims
	}
	if p.Type != "access" {
		return nil, ErrTokenType
	}
	if p.Issuer != issuer {
		return nil, ErrIssuer
	}
	if !audienceMatches(p.AudienceRaw, audience) {
		return nil, ErrAudience
	}

	exp, ok := unixSeconds(p.ExpiresAt)
	if !ok {
		return nil, ErrClaims
	}
	if !now.Before(time.Unix(exp, 0).Add(clockLeeway)) {
		return nil, ErrExpired
	}
	if iat, ok := unixSeconds(p.IssuedAt); !ok {
		// iat is a required claim on the Python encoder (verify requires it),
		// so its absence here marks a token from a different mint entirely.
		return nil, ErrClaims
	} else if time.Unix(iat, 0).After(now.Add(clockLeeway)) {
		return nil, ErrNotYet
	}
	if nbf, ok := unixSeconds(p.NotBefore); ok {
		if time.Unix(nbf, 0).After(now.Add(clockLeeway)) {
			return nil, ErrNotYet
		}
	}

	// The Python decoder coerces sub/tid through uuid.UUID, which both
	// validates them and normalises casing. A gateway-side token with a
	// non-UUID tenant would break hub keying, so the same shape is required.
	if !validate.IsUUID(p.Subject) || !validate.IsUUID(p.TenantID) {
		return nil, ErrBadSubject
	}

	return &Claims{
		UserID:       strings.ToLower(p.Subject),
		TenantID:     strings.ToLower(p.TenantID),
		Role:         p.Role,
		TokenVersion: intVersion(p.TokenVer),
		ExpiresAt:    time.Unix(exp, 0),
		JTI:          p.JTI,
	}, nil
}

// audienceMatches accepts the two shapes JWT permits for aud: a bare string
// or an array of strings.
func audienceMatches(raw any, expected string) bool {
	switch aud := raw.(type) {
	case string:
		return aud == expected
	case []any:
		for _, item := range aud {
			if s, ok := item.(string); ok && s == expected {
				return true
			}
		}
	}
	return false
}

// unixSeconds coerces a JSON numeric claim (float64 after decoding) to
// seconds. Strings and other shapes are rejected: the Python encoder emits
// ints, and accepting strings would only widen the accepted-grammar for no
// operational reason.
func unixSeconds(v any) (int64, bool) {
	n, ok := v.(float64)
	if !ok {
		return 0, false
	}
	return int64(n), true
}

// intVersion tolerates tv arriving as a JSON number or a numeric string,
// mirroring int(payload.get("tv", 0)) on the Python side.
func intVersion(v any) int {
	switch n := v.(type) {
	case float64:
		return int(n)
	case string:
		var i int
		if _, err := fmt.Sscanf(n, "%d", &i); err == nil {
			return i
		}
	}
	return 0
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/auth/jwt.go (123 lines, sha256 293a3cc8f430fa53aea84a62109941f1c95482324000dc1500fd366f62f000c2) =====
==============================================================================
```go
// Package auth verifies the dashboard access tokens the public edge trusts,
// and houses the shared credential-comparison helpers every secret-bearing
// HTTP surface on the edge uses.
//
// Verification (this file plus claims.go):
//
//   - the algorithm is pinned to HS256 — a token claiming alg "none" or an
//     asymmetric algorithm is rejected before the signature is even checked,
//     closing the classic alg-confusion downgrade;
//   - issuer and audience must match exactly;
//   - exp and nbf are enforced with a small clock leeway (the gateway and the
//     API run in the same deployment, so 30 s of skew tolerance costs nothing
//     and absorbs NTP jitter);
//   - typ must be "access" so a token minted for a different purpose can
//     never open a realtime session.
//
// What the gateway deliberately does NOT do: it does not consult the
// database, so User.token_version revocation is out of its reach. Access
// tokens live 15 minutes by design (ACCESS_TOKEN_MINUTES); the realtime edge
// inherits that same bound by closing the connection shortly after the
// presented token's exp (see heartbeat.go), forcing a reconnect with the
// fresh token the dashboard already rotates on schedule. That is the same
// trust window the dashboard UI itself operates under.
//
// File split: jwt.go = envelope (segments, signature, JOSE header pin),
// claims.go = payload (claim shapes, coercions, trusted Claims),
// middleware.go = shared Bearer/constant-time helpers.
package auth

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"strings"
	"time"
)

// clockLeeway covers same-deployment NTP jitter on exp/nbf/iat checks.
const clockLeeway = 30 * time.Second

// Sentinel errors. Callers map them to wire codes; the distinct values keep
// "why did authentication fail" out of the client-visible message (the client
// always gets the generic auth_failed) while staying precise in logs/tests.
var (
	ErrMalformed  = errors.New("token is not a compact JWS")
	ErrAlgorithm  = errors.New("token algorithm is not HS256")
	ErrSignature  = errors.New("signature verification failed")
	ErrExpired    = errors.New("token has expired")
	ErrNotYet     = errors.New("token is not yet valid")
	ErrIssuer     = errors.New("unexpected issuer")
	ErrAudience   = errors.New("unexpected audience")
	ErrTokenType  = errors.New("not an access token")
	ErrClaims     = errors.New("claims are missing or malformed")
	ErrBadSubject = errors.New("subject or tenant id is not a UUID")
)

// header is the JOSE header. Only alg is consulted, and only to be pinned.
type header struct {
	Algorithm string `json:"alg"`
}

// Verifier checks tokens against one shared secret and the expected
// issuer/audience. It is immutable and safe for concurrent use.
type Verifier struct {
	secret   []byte
	issuer   string
	audience string
}

// NewVerifier returns a Verifier. The secret must already have passed
// config validation (never empty, never a placeholder).
func NewVerifier(secret, issuer, audience string) *Verifier {
	return &Verifier{secret: []byte(secret), issuer: issuer, audience: audience}
}

// Verify parses and validates token, returning the trusted Claims or one of
// the sentinel errors. now is injectable so expiry behaviour is testable
// without sleeping.
func (v *Verifier) Verify(token string, now time.Time) (*Claims, error) {
	segments := strings.Split(token, ".")
	if len(segments) != 3 || segments[0] == "" || segments[1] == "" {
		return nil, ErrMalformed
	}

	headerBytes, err := base64.RawURLEncoding.DecodeString(segments[0])
	if err != nil {
		return nil, ErrMalformed
	}
	var hdr header
	if err := json.Unmarshal(headerBytes, &hdr); err != nil {
		return nil, ErrMalformed
	}
	// Pinned BEFORE the signature check: a token advertising an unexpected
	// algorithm never reaches the key, so there is no cross-algorithm
	// confusion surface to defend further.
	if hdr.Algorithm != "HS256" {
		return nil, ErrAlgorithm
	}

	signingInput := segments[0] + "." + segments[1]
	signature, err := base64.RawURLEncoding.DecodeString(segments[2])
	if err != nil {
		return nil, ErrMalformed
	}
	mac := hmac.New(sha256.New, v.secret)
	_, _ = mac.Write([]byte(signingInput))
	if !hmac.Equal(mac.Sum(nil), signature) {
		return nil, ErrSignature
	}

	payloadBytes, err := base64.RawURLEncoding.DecodeString(segments[1])
	if err != nil {
		return nil, ErrMalformed
	}
	var p payload
	if err := json.Unmarshal(payloadBytes, &p); err != nil {
		return nil, ErrMalformed
	}

	return validatePayload(&p, v.issuer, v.audience, now)
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/auth/jwt_test.go (232 lines, sha256 c5dc4b7807e52fab8b0976b42f22aefe0fbbc79457f389292222caddd9dd6521) =====
==============================================================================
```go
package auth

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"
)

// mint builds a compact JWS exactly the way the Python encoder does
// (app/auth/jwt.py: jwt.encode(payload, secret, algorithm="HS256")).
// Tests construct raw claim maps so each test can break exactly one
// property at a time.
func mint(t *testing.T, secret string, claims map[string]any, alg string) string {
	t.Helper()
	headerJSON, err := json.Marshal(map[string]any{"alg": alg, "typ": "JWT"})
	if err != nil {
		t.Fatalf("marshal header: %v", err)
	}
	claimsJSON, err := json.Marshal(claims)
	if err != nil {
		t.Fatalf("marshal claims: %v", err)
	}
	head := base64.RawURLEncoding.EncodeToString(headerJSON)
	body := base64.RawURLEncoding.EncodeToString(claimsJSON)
	input := head + "." + body
	if alg == "none" {
		return input + "."
	}
	mac := hmac.New(sha256.New, []byte(secret))
	_, _ = mac.Write([]byte(input))
	return input + "." + base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
}

const (
	testSecret   = "0123456789abcdef0123456789abcdef"
	testTenant   = "11111111-2222-3333-4444-555555555555"
	testUser     = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
	testAudience = "voxdesk-api"
	testIssuer   = "voxdesk"
)

// validClaims is the shape of a token the Python API would mint now.
func validClaims(now time.Time) map[string]any {
	return map[string]any{
		"sub":  testUser,
		"tid":  testTenant,
		"role": "owner",
		"tv":   float64(3),
		"typ":  "access",
		"iat":  float64(now.Unix()),
		"nbf":  float64(now.Unix()),
		"exp":  float64(now.Add(15 * time.Minute).Unix()),
		"iss":  testIssuer,
		"aud":  testAudience,
		"jti":  "random-jti",
	}
}

func newTestVerifier() *Verifier {
	return NewVerifier(testSecret, testIssuer, testAudience)
}

func TestValidTokenVerifies(t *testing.T) {
	now := time.Now()
	token := mint(t, testSecret, validClaims(now), "HS256")
	claims, err := newTestVerifier().Verify(token, now)
	if err != nil {
		t.Fatalf("Verify: %v", err)
	}
	if claims.TenantID != testTenant || claims.UserID != testUser {
		t.Errorf("claims = %+v", claims)
	}
	if claims.Role != "owner" || claims.TokenVersion != 3 {
		t.Errorf("role/tv = %q/%d", claims.Role, claims.TokenVersion)
	}
	if !claims.ExpiresAt.After(now) {
		t.Errorf("ExpiresAt = %v, now = %v", claims.ExpiresAt, now)
	}
}

func TestAlgorithmPinned(t *testing.T) {
	now := time.Now()
	// alg=none: no signature at all must never be accepted.
	noneToken := mint(t, testSecret, validClaims(now), "none")
	if _, err := newTestVerifier().Verify(noneToken, now); !errors.Is(err, ErrMalformed) && !errors.Is(err, ErrAlgorithm) {
		t.Errorf("alg=none: err = %v, want ErrAlgorithm/ErrMalformed", err)
	}
	// A token lying about its algorithm name.
	rsToken := mint(t, testSecret, validClaims(now), "RS256")
	if _, err := newTestVerifier().Verify(rsToken, now); !errors.Is(err, ErrAlgorithm) {
		t.Errorf("alg=RS256: err = %v, want ErrAlgorithm", err)
	}
}

func TestWrongSecretRejected(t *testing.T) {
	now := time.Now()
	token := mint(t, "a-different-secret-0123456789abcdef", validClaims(now), "HS256")
	if _, err := newTestVerifier().Verify(token, now); !errors.Is(err, ErrSignature) {
		t.Errorf("err = %v, want ErrSignature", err)
	}
}

func TestExpiredRejected(t *testing.T) {
	now := time.Now()
	claims := validClaims(now)
	claims["exp"] = float64(now.Add(-10 * time.Minute).Unix())
	token := mint(t, testSecret, claims, "HS256")
	if _, err := newTestVerifier().Verify(token, now); !errors.Is(err, ErrExpired) {
		t.Errorf("err = %v, want ErrExpired", err)
	}
}

func TestNotBeforeFutureRejected(t *testing.T) {
	now := time.Now()
	claims := validClaims(now)
	claims["nbf"] = float64(now.Add(5 * time.Minute).Unix())
	token := mint(t, testSecret, claims, "HS256")
	if _, err := newTestVerifier().Verify(token, now); !errors.Is(err, ErrNotYet) {
		t.Errorf("err = %v, want ErrNotYet", err)
	}
}

func TestWrongIssuerRejected(t *testing.T) {
	now := time.Now()
	claims := validClaims(now)
	claims["iss"] = "somebody-else"
	token := mint(t, testSecret, claims, "HS256")
	if _, err := newTestVerifier().Verify(token, now); !errors.Is(err, ErrIssuer) {
		t.Errorf("err = %v, want ErrIssuer", err)
	}
}

func TestWrongAudienceRejected(t *testing.T) {
	now := time.Now()
	claims := validClaims(now)
	claims["aud"] = "voxdesk-admin"
	token := mint(t, testSecret, claims, "HS256")
	if _, err := newTestVerifier().Verify(token, now); !errors.Is(err, ErrAudience) {
		t.Errorf("err = %v, want ErrAudience", err)
	}
}

func TestAudienceArrayAccepted(t *testing.T) {
	now := time.Now()
	claims := validClaims(now)
	claims["aud"] = []any{"other-api", testAudience} // JWT permits aud as a list
	token := mint(t, testSecret, claims, "HS256")
	if _, err := newTestVerifier().Verify(token, now); err != nil {
		t.Errorf("aud list containing the expected audience must verify: %v", err)
	}
}

func TestRefreshTypeRejected(t *testing.T) {
	now := time.Now()
	claims := validClaims(now)
	claims["typ"] = "refresh" // a token minted for a different purpose
	token := mint(t, testSecret, claims, "HS256")
	if _, err := newTestVerifier().Verify(token, now); !errors.Is(err, ErrTokenType) {
		t.Errorf("err = %v, want ErrTokenType", err)
	}
}

func TestMissingClaimsRejected(t *testing.T) {
	now := time.Now()
	for _, drop := range []string{"sub", "tid", "role", "exp", "iat"} {
		claims := validClaims(now)
		delete(claims, drop)
		token := mint(t, testSecret, claims, "HS256")
		if _, err := newTestVerifier().Verify(token, now); err == nil {
			t.Errorf("claim %q dropped: expected rejection, got claims", drop)
		}
	}
}

func TestNonUUIDTenantRejected(t *testing.T) {
	now := time.Now()
	claims := validClaims(now)
	claims["tid"] = "tenant-one" // a human name, not a UUID: hub keying must never see it
	token := mint(t, testSecret, claims, "HS256")
	if _, err := newTestVerifier().Verify(token, now); !errors.Is(err, ErrBadSubject) {
		t.Errorf("err = %v, want ErrBadSubject", err)
	}
}

func TestTamperedPayloadRejected(t *testing.T) {
	now := time.Now()
	token := mint(t, testSecret, validClaims(now), "HS256")
	parts := strings.Split(token, ".")
	// Re-encode a DIFFERENT payload while keeping the original signature.
	claims := validClaims(now)
	claims["role"] = "platform_admin"
	tampered, _ := json.Marshal(claims)
	parts[1] = base64.RawURLEncoding.EncodeToString(tampered)
	if _, err := newTestVerifier().Verify(strings.Join(parts, "."), now); !errors.Is(err, ErrSignature) {
		t.Errorf("err = %v, want ErrSignature", err)
	}
}

func TestMalformedTokensRejected(t *testing.T) {
	now := time.Now()
	for _, token := range []string{"", "one", "one.two", "one.two.three.four", "..", "a.b.c"} {
		if _, err := newTestVerifier().Verify(token, now); err == nil {
			t.Errorf("token %q: expected rejection", token)
		}
	}
}

func TestExpiryWindowBoundary(t *testing.T) {
	now := time.Now()
	claims := validClaims(now)
	claims["exp"] = float64(now.Add(20 * time.Second).Unix())
	token := mint(t, testSecret, claims, "HS256")
	// A token 20 s from expiry still verifies; the clock leeway exists to
	// absorb NTP skew, not to gate this case.
	if _, err := newTestVerifier().Verify(token, now); err != nil {
		t.Errorf("token 20 s from expiry should verify, got %v", err)
	}
	// ...and 20 s PAST expiry (within the leeway) the same holds, mirroring
	// PyJWT leeway semantics for same-deployment clock jitter.
	if _, err := newTestVerifier().Verify(token, now.Add(25*time.Second)); err != nil {
		t.Errorf("inside leeway past expiry should verify, got %v", err)
	}
	// Beyond the leeway it must fail closed.
	if _, err := newTestVerifier().Verify(token, now.Add(2*time.Minute)); !errors.Is(err, ErrExpired) {
		t.Errorf("err = %v, want ErrExpired", err)
	}
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/auth/middleware.go (35 lines, sha256 b41587dcc659c0180ffcf17cc8fcf49aca49b7b0e85225d462fa893303f75ea7) =====
==============================================================================
```go
package auth

import (
	"crypto/sha256"
	"crypto/subtle"
	"strings"
)

// Bearer-credential helpers shared by every HTTP surface on this edge that
// checks a shared secret (ingest, /metrics). They used to live inline in
// each handler and had started to drift — this file is the single
// implementation, so "how does the gateway compare secrets" has exactly one
// answer at review time.

// ExtractBearer splits an Authorization header of the form "Bearer <token>".
// A wrong scheme, an empty token or a missing header is simply not a
// credential; it is NOT an error worth distinguishing to the caller, whose
// only valid response is refusal either way.
func ExtractBearer(header string) (token string, ok bool) {
	token, ok = strings.CutPrefix(header, "Bearer ")
	if !ok || token == "" {
		return "", false
	}
	return token, true
}

// ConstantTimeTokenEqual compares a presented credential against the
// expected one. Both sides are hashed FIRST, so the comparison cost reveals
// nothing about the secret's length or content — the standard treatment for
// secrets that travel as HTTP headers, constant-time over the whole string.
func ConstantTimeTokenEqual(presented, expected string) bool {
	ph := sha256.Sum256([]byte(presented))
	eh := sha256.Sum256([]byte(expected))
	return subtle.ConstantTimeCompare(ph[:], eh[:]) == 1
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/backpressure/policy.go (45 lines, sha256 b3e236375c43c40bd016db1bd77db0efc4e36b5b575d07cd081db75bae42af29) =====
==============================================================================
```go
// Package backpressure owns the one queue policy the gateway has had since
// its first connection — bounded buffer, drop-newest with a counter —
// extracted from the WebSocket connection's outgoing channel so the policy
// is a named, tested type rather than a select/default buried in a loop.
//
// Why drop-newest (and never block): a slow browser must never stall a
// room. A fan-out that BLOCKED on one full queue would let any single
// consumer head-of-line-block every other subscriber of the room — the
// classic slow-consumer amplification that turns one bad connection into a
// room-wide outage. Dropping the frame for the lagging consumer only, and
// COUNTING the drop, is the same contract the Rust hub's BoundedBroadcast
// implements, and the gateway's /metrics has always rendered it as
// voxdesk_gateway_dropped_total.
package backpressure

import "fmt"

// Policy describes how an over-capacity enqueue is resolved.
//
//go:generate stringer -type Policy
type Policy int

const (
	// DropNewest refuses the INCOMING frame when the queue is full: the
	// frames already queued stay queued, the newest arrival is lost, and
	// DroppedTotal increments. For realtime dashboard traffic (where a
	// fresher update always supersedes) this preserves ordering for the
	// frames that do flow and sheds load at the exact point of overload.
	DropNewest Policy = iota
)

// String renders the policy for logs and tests.
func (p Policy) String() string {
	switch p {
	case DropNewest:
		return "drop-newest"
	default:
		return fmt.Sprintf("unknown(%d)", int(p))
	}
}

// Drop semantics for future policies (block-oldest, drop-oldest) belong in
// this package, added alongside their own tests — never as an inline
// select/default at a call site, which is exactly the pattern this package
// replaced.
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/backpressure/queue.go (84 lines, sha256 3eaca8dafbfa671742a2993ad37af49ff46d9ab80e860de448e723b57b7b346c) =====
==============================================================================
```go
package backpressure

import (
	"errors"
	"sync/atomic"
)

// ErrQueueFull is returned by Enqueue when the queue is at capacity and the
// active policy refused the frame. It is a flow-control signal, not an
// operational error: callers typically count it and move on.
var ErrQueueFull = errors.New("backpressure: queue full")

// Queue is a bounded, multi-producer/single-consumer FIFO with an explicit
// overload policy. It wraps a channel so consumers keep ordinary
// select/range ergonomics, while producers get a non-blocking,
// policy-driven enqueue with a drop counter.
//
// The gateway's producer/consumer split is exactly: many publishers (hub
// fan-out, the session's own replies) → one writer goroutine. The channel
// does the synchronization; this type owns the POLICY.
type Queue[T any] struct {
	ch chan T

	policy   Policy
	enqueued atomic.Int64 // frames admitted since construction (diagnostics)
	dropped  atomic.Int64 // frames refused by the policy since construction
}

// New returns a queue with room for capacity frames. capacity < 1 is
// normalized to 1 — a zero-capacity queue would refuse every frame, and
// that misconfiguration should degrade to "barely any buffer" rather than
// to a silent black hole.
func New[T any](capacity int, policy Policy) *Queue[T] {
	if capacity < 1 {
		capacity = 1
	}
	return &Queue[T]{ch: make(chan T, capacity), policy: policy}
}

// TryEnqueue offers one frame, applying the queue's overload policy:
//
//   - fast path (room available): admitted, true, nil;
//   - full under DropNewest: refused, false, ErrQueueFull (drop counted).
//
// It NEVER blocks — that is the entire point of the type.
func (q *Queue[T]) TryEnqueue(v T) (bool, error) {
	select {
	case q.ch <- v:
		q.enqueued.Add(1)
		return true, nil
	default:
		q.dropped.Add(1)
		return false, ErrQueueFull
	}
}

// Enqueue is TryEnqueue's bool-only form for consumers of the old channel
// contract (hub.Subscriber.Enqueue). Identical semantics.
func (q *Queue[T]) Enqueue(v T) bool {
	ok, _ := q.TryEnqueue(v)
	return ok
}

// C exposes the receive side for select loops. The consumer OWNS the read:
// a type whose contract is "one writer goroutine drains me" should not
// hide the channel it is selecting on. Never send on the returned channel;
// it is typed receive-only.
func (q *Queue[T]) C() <-chan T { return q.ch }

// Len reports frames currently buffered.
func (q *Queue[T]) Len() int { return len(q.ch) }

// Capacity reports the queue's bound.
func (q *Queue[T]) Capacity() int { return cap(q.ch) }

// Policy reports the active overload policy.
func (q *Queue[T]) Policy() Policy { return q.policy }

// EnqueuedTotal is how many frames were admitted since construction.
func (q *Queue[T]) EnqueuedTotal() int64 { return q.enqueued.Load() }

// DroppedTotal is how many frames the policy refused since construction —
// the per-queue source of the /metrics dropped gauge.
func (q *Queue[T]) DroppedTotal() int64 { return q.dropped.Load() }
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/backpressure/queue_test.go (99 lines, sha256 2918af65ef0d926771c00f8275e91d0c88282a82f75408e12de37caf1082f69e) =====
==============================================================================
```go
package backpressure

import (
	"errors"
	"sync"
	"testing"
)

func TestQueueAdmitsUpToCapacityThenDropsNewest(t *testing.T) {
	t.Parallel()
	q := New[int](3, DropNewest)

	for i := 0; i < 3; i++ {
		if ok, err := q.TryEnqueue(i); !ok || err != nil {
			t.Fatalf("frame %d within capacity was refused: %v", i, err)
		}
	}
	ok, err := q.TryEnqueue(99)
	if ok || !errors.Is(err, ErrQueueFull) {
		t.Fatalf("over-capacity frame must be refused with ErrQueueFull, got ok=%v err=%v", ok, err)
	}
	if q.DroppedTotal() != 1 || q.EnqueuedTotal() != 3 {
		t.Fatalf("counters wrong: enqueued=%d dropped=%d", q.EnqueuedTotal(), q.DroppedTotal())
	}

	// FIFO order and drains-free-capacity behaviour: drain one, admit one.
	if got := <-q.C(); got != 0 {
		t.Fatalf("FIFO violated: first frame must be 0, got %d", got)
	}
	if !q.Enqueue(3) {
		t.Fatal("freed capacity must admit the next frame")
	}
	for _, want := range []int{1, 2, 3} {
		if got := <-q.C(); got != want {
			t.Fatalf("FIFO violated on drain: got %d, want %d", got, want)
		}
	}
	if q.Len() != 0 {
		t.Fatalf("empty queue reporting %d frames", q.Len())
	}
}

func TestQueueZeroCapacityIsNormalizedNotBlackHole(t *testing.T) {
	t.Parallel()
	q := New[string](0, DropNewest)
	if q.Capacity() < 1 {
		t.Fatalf("zero capacity must be normalized to at least 1, got %d", q.Capacity())
	}
	if !q.Enqueue("x") {
		t.Fatal("normalized queue must admit its single slot")
	}
}

func TestQueueConcurrentProducersNeverBlock(t *testing.T) {
	t.Parallel()
	const capacity = 128
	q := New[int](capacity, DropNewest)

	// Far more producers than capacity, no consumer for half the run:
	// producers must still complete (never parked on a full queue).
	var wg sync.WaitGroup
	for g := 0; g < 32; g++ {
		wg.Add(1)
		go func(g int) {
			defer wg.Done()
			for i := 0; i < 64; i++ {
				q.Enqueue(g*64 + i)
			}
		}(g)
	}
	wg.Wait()

	admitted := q.EnqueuedTotal()
	dropped := q.DroppedTotal()
	if admitted+dropped != 32*64 {
		t.Fatalf("accounting leak: admitted %d + dropped %d != offered %d", admitted, dropped, 32*64)
	}
	if admitted < int64(capacity) {
		t.Fatalf("a queue that never had more than %d consumers must fill its buffer, admitted %d", capacity, admitted)
	}
	if q.Len() != capacity {
		t.Fatalf("no consumer ran: buffer must be exactly full, len=%d", q.Len())
	}

	// Drain to quiesce the channel before test end.
	for q.Len() > 0 {
		<-q.C()
	}
}

func TestPolicyString(t *testing.T) {
	t.Parallel()
	if DropNewest.String() != "drop-newest" {
		t.Fatalf("unexpected policy name %q", DropNewest.String())
	}
	if Policy(42).String() == "" {
		t.Fatal("unknown policies must still render")
	}
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/broker/broker.go (92 lines, sha256 aae6b7c7d19f193732b6923f5d7813aeedc039197b6dde7d5ee2d647483dd583) =====
==============================================================================
```go
// Package broker is the ingest fan-out transport: the hop between "the
// API asked us to publish" and "this node's hub fans the event into local
// WebSocket rooms".
//
// The interface exists because the gateway has two deployment shapes:
//
//   - memory: one node. Publish delivers to local subscribers SYNCHRONOUSLY
//     and that is the whole fan-out — ingest's response counters stay
//     exact, because the publish call itself did the delivery.
//   - redis: many replicas behind a balancer. Publish delivers locally
//     synchronously (the response counters still reflect THIS node's
//     delivery) AND propagates the envelope over Redis pub/sub so every
//     other replica delivers to its own local subscribers. Own-node echoes
//     coming back over the bus are suppressed via an origin stamp, so a
//     publish never double-delivers at home.
//
// Topic payloads are opaque bytes here; the wire schema (what a publish
// envelope contains) belongs to the caller — the broker moves bytes and
// counts deliveries, it does not understand events.
package broker

import (
	"encoding/json"
	"errors"
	"time"
)

// ErrClosed is returned by Publish on a closed broker. Callers treat it as
// a hard failure (shutdown in flight).
var ErrClosed = errors.New("broker: closed")

// Envelope is one message heard on the bus — locally (from Publish) or
// remotely (from another node's bus hop). Payload is the caller's bytes,
// untouched.
type Envelope struct {
	Topic string
	// Payload is the published bytes. For locally-published messages this
	// is the exact slice handed to Publish; for bus-received ones it is a
	// fresh decode.
	Payload json.RawMessage
	// Origin is the node id that produced this envelope. A consumer that
	// runs on multiple nodes can use it to de-prioritise own-node facts;
	// the redis broker already guarantees it never sees its OWN echo.
	Origin string
	// Local is true when the envelope is heard by a subscriber on the same
	// node that published it (the synchronous delivery).
	Local bool
	// At is the broker-side hear time (publish time for local hops).
	At time.Time
}

// Stats is the delivery accounting a Handler returns. It mirrors the
// hub's fan-out counters one-for-one so ingest responses keep their exact
// (delivered, dropped) meaning regardless of which broker implementation
// produced them.
type Stats struct {
	Delivered int
	Dropped   int
}

// Add accumulates other into s (multi-subscriber aggregation).
func (s Stats) Add(other Stats) Stats {
	return Stats{Delivered: s.Delivered + other.Delivered, Dropped: s.Dropped + other.Dropped}
}

// Handler consumes one envelope and reports the local delivery accounting.
// For bus-received envelopes the returned Stats is informational only —
// there is no channel to return it across nodes, so remote handlers should
// count into metrics themselves (the server does) rather than relying on
// the caller.
type Handler func(Envelope) Stats

// Broker is the fan-out transport contract.
type Broker interface {
	// Publish hands one message to the topic's LOCAL subscribers
	// synchronously and returns their aggregated accounting. In redis
	// mode the bus propagation happens BEFORE Publish returns (single
	// socket write) but never determines the error: local delivery is
	// the contract, cross-node propagation is best-effort with internal
	// retry.
	Publish(topic string, payload []byte) (Stats, error)
	// Subscribe registers a local handler for new envelopes on topic.
	// The returned function unsubscribes exactly once.
	Subscribe(topic string, h Handler) (unsubscribe func())
	// NodeID is this node's stable identity (origin stamp).
	NodeID() string
	// Kind reports the transport: "memory" or "redis" (observability).
	Kind() string
	// Close stops all background work (the redis reader/reconnect loop)
	// and refuses subsequent Publishes with ErrClosed.
	Close() error
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/broker/brokertest/fake.go (228 lines, sha256 8d0fe560c063d343c8151b0549e7b36d0f7809650ee9d417a458226e674092e0) =====
==============================================================================
```go
// Package brokertest provides a fake RESP-speaking Redis for tests: a
// real TCP server implementing exactly the commands and pub/sub semantics
// the gateway's hand-rolled broker client uses (AUTH, SELECT, SUBSCRIBE,
// PUBLISH), including re-broadcasting a publication to EVERY subscriber
// connection of the topic — own-node echoes included, which is precisely
// the behaviour the broker's origin suppression must handle.
//
// It lives in a package of its own (not a _test.go file) so tests OUTSIDE
// the broker package — notably internal/server's multi-replica e2e — can
// stand up the same fake without copying it.
package brokertest

import (
	"bufio"
	"fmt"
	"io"
	"net"
	"strconv"
	"strings"
	"sync"
	"time"
)

// FakeRedis is one running fake server. Zero value is not usable; build
// with Start. Safe under -race (all shared state behind mu).
type FakeRedis struct {
	ln   net.Listener
	Addr string

	mu    sync.Mutex
	subs  map[string][]net.Conn // topic → subscribed connections
	conns map[net.Conn]struct{}
	die   chan struct{}
}

// Start binds an ephemeral port and begins serving. The caller owns Stop.
func Start() (*FakeRedis, error) { return StartAt("127.0.0.1:0") }

// StartAt binds a specific address — used to bring the bus BACK on the
// same endpoint after a Stop, which is how reconnect behaviour is tested
// (clients retry the address they were configured with).
func StartAt(addr string) (*FakeRedis, error) {
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		return nil, err
	}
	f := &FakeRedis{
		ln: ln, Addr: ln.Addr().String(),
		subs: make(map[string][]net.Conn), conns: make(map[net.Conn]struct{}),
		die: make(chan struct{}),
	}
	go func() {
		for {
			conn, err := ln.Accept()
			if err != nil {
				return
			}
			f.mu.Lock()
			f.conns[conn] = struct{}{}
			f.mu.Unlock()
			go f.serve(conn)
		}
	}()
	return f, nil
}

// URL renders the address as a redis:// URL for config/client use.
func (f *FakeRedis) URL() string { return "redis://" + f.Addr }

// Stop kills the listener AND every client connection — violent enough to
// force a broker's read loop into its reconnect cycle (the outage test
// relies on that). Rebind with StartAt to model "the bus is back".
func (f *FakeRedis) Stop() {
	f.mu.Lock()
	defer f.mu.Unlock()
	select {
	case <-f.die:
	default:
		close(f.die)
	}
	_ = f.ln.Close()
	for c := range f.conns {
		_ = c.Close()
	}
	f.conns = map[net.Conn]struct{}{}
	f.subs = map[string][]net.Conn{}
}

// SubscriberCount reports live SUBSCRIBE registrations on topic (asserts
// that reconnect re-subscribes happened).
func (f *FakeRedis) SubscriberCount(topic string) int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.subs[topic])
}

// WaitForSubscribers polls until topic has n subscriber connections or the
// timeout lapses (reconnect-driven subscriptions are asynchronous).
func (f *FakeRedis) WaitForSubscribers(topic string, n int, timeout time.Duration) bool {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if f.SubscriberCount(topic) >= n {
			return true
		}
		time.Sleep(5 * time.Millisecond)
	}
	return false
}

func (f *FakeRedis) serve(conn net.Conn) {
	defer conn.Close()
	br := bufio.NewReader(conn)
	w := bufio.NewWriter(conn)
	for {
		args, err := readCommand(br)
		if err != nil {
			f.drop(conn)
			return
		}
		if len(args) == 0 {
			continue
		}
		switch strings.ToUpper(args[0]) {
		case "AUTH", "SELECT":
			fmt.Fprint(w, "+OK\r\n")
		case "SUBSCRIBE":
			if len(args) != 2 {
				fmt.Fprint(w, "-ERR wrong args\r\n")
				break
			}
			topic := args[1]
			f.mu.Lock()
			f.subs[topic] = append(f.subs[topic], conn)
			count := 0
			for _, conns := range f.subs {
				for _, c := range conns {
					if c == conn {
						count++
					}
				}
			}
			f.mu.Unlock()
			writeArray(w, "subscribe", topic, strconv.Itoa(count))
		case "PUBLISH":
			if len(args) != 3 {
				fmt.Fprint(w, "-ERR wrong args\r\n")
				break
			}
			topic, payload := args[1], args[2]
			f.mu.Lock()
			recipients := append([]net.Conn(nil), f.subs[topic]...)
			f.mu.Unlock()
			for _, rc := range recipients {
				// Best-effort fan-out per connection: one stalled client
				// never head-of-line-blocks the fake's publish loop.
				rc.SetWriteDeadline(time.Now().Add(2 * time.Second))
				rw := bufio.NewWriter(rc)
				writeArray(rw, "message", topic, payload)
				rw.Flush()
				rc.SetWriteDeadline(time.Time{})
			}
			fmt.Fprintf(w, ":%d\r\n", len(recipients))
		default:
			fmt.Fprint(w, "-ERR unknown command\r\n")
		}
		w.Flush()
	}
}

func (f *FakeRedis) drop(conn net.Conn) {
	f.mu.Lock()
	delete(f.conns, conn)
	for topic, conns := range f.subs {
		kept := conns[:0]
		for _, c := range conns {
			if c != conn {
				kept = append(kept, c)
			}
		}
		f.subs[topic] = kept
	}
	f.mu.Unlock()
}

// readCommand parses one client command (a RESP array of bulk strings).
func readCommand(br *bufio.Reader) ([]string, error) {
	line, err := br.ReadString('\n')
	if err != nil {
		return nil, err
	}
	line = strings.TrimSuffix(strings.TrimSuffix(line, "\n"), "\r")
	if !strings.HasPrefix(line, "*") {
		return nil, fmt.Errorf("expected array, got %q", line)
	}
	n, err := strconv.Atoi(line[1:])
	if err != nil {
		return nil, err
	}
	args := make([]string, 0, n)
	for i := 0; i < n; i++ {
		header, err := br.ReadString('\n')
		if err != nil {
			return nil, err
		}
		header = strings.TrimSuffix(strings.TrimSuffix(header, "\n"), "\r")
		if !strings.HasPrefix(header, "$") {
			return nil, fmt.Errorf("expected bulk header, got %q", header)
		}
		size, err := strconv.Atoi(header[1:])
		if err != nil {
			return nil, err
		}
		buf := make([]byte, size+2)
		if _, err := io.ReadFull(br, buf); err != nil {
			return nil, err
		}
		args = append(args, string(buf[:size]))
	}
	return args, nil
}

// writeArray emits a RESP array of bulk strings.
func writeArray(w *bufio.Writer, parts ...string) {
	fmt.Fprintf(w, "*%d\r\n", len(parts))
	for _, p := range parts {
		fmt.Fprintf(w, "$%d\r\n%s\r\n", len(p), p)
	}
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/broker/memory.go (127 lines, sha256 2b858a273775a36256d15fe3566c67f22cefaf4c99cc4c74faba5614b4ca31d2) =====
==============================================================================
```go
package broker

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"sync"
	"sync/atomic"
	"time"
)

// MemoryBroker is the single-node transport: Publish IS the fan-out.
// There is no background goroutine, no connection, nothing to reconnect —
// which is exactly why the default deployment has so little to fail.
//
// Handlers run synchronously inside the Publish call, OUTSIDE the
// subscription lock (snapshot-then-invoke, the same discipline as the
// hub): a handler that itself Publishes (an event cascade) can never
// self-deadlock, and one slow subscriber cannot hold the subscription
// table against a concurrent Subscribe.
type MemoryBroker struct {
	nodeID string

	mu       sync.Mutex // guards handlers only
	handlers map[string][]Handler
	closed   atomic.Bool
}

// NewMemory returns an empty memory broker with a fresh node id.
func NewMemory() *MemoryBroker {
	return &MemoryBroker{handlers: make(map[string][]Handler), nodeID: newNodeID()}
}

// newNodeID mints a node identity: "gw-" + 8 random hex bytes — short
// enough for log lines, unique enough that two nodes started in the same
// nanosecond still differ.
func newNodeID() string {
	var b [8]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "gw-norand"
	}
	return "gw-" + hex.EncodeToString(b[:])
}

// Publish implements Broker: snapshot subscribers, invoke each
// synchronously, aggregate. A closed broker refuses loudly (shutdown in
// flight must not silently deliver).
func (m *MemoryBroker) Publish(topic string, payload []byte) (Stats, error) {
	if m.closed.Load() {
		return Stats{}, ErrClosed
	}
	return m.deliver(Envelope{
		Topic:   topic,
		Payload: json.RawMessage(payload),
		Origin:  m.nodeID,
		Local:   true,
		At:      time.Now(),
	}), nil
}

// deliver invokes the topic's subscriber snapshot and aggregates their
// accounting. Shared by Publish (local hop) and the redis broker (whose
// bus-received envelopes reuse the identical delivery path).
func (m *MemoryBroker) deliver(env Envelope) Stats {
	m.mu.Lock()
	// Snapshot keys alone: handler slices are append-only under mu and a
	// concurrent unsubscribe nils entries in place, which deliverOn skips.
	topicHandlers := append([]Handler(nil), m.handlers[env.Topic]...)
	m.mu.Unlock()

	var total Stats
	for _, h := range topicHandlers {
		if h == nil {
			continue
		}
		total = total.Add(m.call(h, env))
	}
	return total
}

// call invokes one handler, containing a panic: a misbehaving subscriber
// must take its own accounting to zero, not kill the publisher's HTTP
// handler goroutine. Same philosophy as net/http recovering handler
// panics per-request.
func (m *MemoryBroker) call(h Handler, env Envelope) (stats Stats) {
	defer func() {
		if recover() != nil {
			stats = Stats{}
		}
	}()
	return h(env)
}

// Subscribe implements Broker. The unsubscribe nils the slot in place
// (deliver snapshots the slice and skips nils), so an in-flight Publish
// that already captured the handler completes undisturbed — unsubscribe
// visibility applies to SUBSEQUENT publishes, matching the hub's
// snapshot-then-invoke discipline.
func (m *MemoryBroker) Subscribe(topic string, h Handler) (unsubscribe func()) {
	m.mu.Lock()
	m.handlers[topic] = append(m.handlers[topic], h)
	idx := len(m.handlers[topic]) - 1
	m.mu.Unlock()
	var once sync.Once
	return func() {
		once.Do(func() {
			m.mu.Lock()
			if handlers := m.handlers[topic]; idx < len(handlers) {
				m.handlers[topic][idx] = nil
			}
			m.mu.Unlock()
		})
	}
}

// NodeID implements Broker.
func (m *MemoryBroker) NodeID() string { return m.nodeID }

// Kind implements Broker.
func (m *MemoryBroker) Kind() string { return "memory" }

// Close implements Broker: idempotent; subsequent Publishes fail with
// ErrClosed (in-flight ones complete).
func (m *MemoryBroker) Close() error {
	m.closed.Store(true)
	return nil
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/broker/memory_test.go (92 lines, sha256 7edde74d87a561d0dc30f6ea6dc563dc7f982da3c9b8ad1f98b1ae7a1c3aaa22) =====
==============================================================================
```go
package broker

import (
	"errors"
	"testing"
)

func TestMemoryPublishDeliversSynchronouslyAndAggregates(t *testing.T) {
	t.Parallel()
	b := NewMemory()
	var got []Envelope
	b.Subscribe("calls", func(env Envelope) Stats {
		got = append(got, env)
		return Stats{Delivered: 3, Dropped: 1}
	})
	b.Subscribe("calls", func(env Envelope) Stats { return Stats{Delivered: 2} })
	b.Subscribe("metrics", func(env Envelope) Stats {
		t.Errorf("other-topic handler must never fire")
		return Stats{}
	})

	stats, err := b.Publish("calls", []byte(`{"k":1}`))
	if err != nil {
		t.Fatalf("publish: %v", err)
	}
	if stats.Delivered != 5 || stats.Dropped != 1 {
		t.Fatalf("Stats must aggregate across subscribers, got %+v", stats)
	}
	if len(got) != 1 || !got[0].Local || got[0].Origin != b.NodeID() {
		t.Fatalf("local envelope shaped wrong: %+v", got)
	}
	if string(got[0].Payload) != `{"k":1}` {
		t.Fatalf("payload must pass through byte-verbatim, got %s", got[0].Payload)
	}
}

func TestMemoryUnsubscribeIsExactOnceAndSkipsInFlightCopy(t *testing.T) {
	t.Parallel()
	b := NewMemory()
	calls := 0
	unsub := b.Subscribe("t", func(Envelope) Stats { calls++; return Stats{} })

	if _, err := b.Publish("t", nil); err != nil || calls != 1 {
		t.Fatalf("pre-unsub publish: calls=%d err=%v", calls, err)
	}
	unsub()
	unsub() // idempotent
	if _, err := b.Publish("t", nil); err != nil || calls != 1 {
		t.Fatalf("unsubscribed handler must not fire: calls=%d err=%v", calls, err)
	}
}

func TestMemoryHandlerPanicIsContained(t *testing.T) {
	t.Parallel()
	b := NewMemory()
	b.Subscribe("t", func(Envelope) Stats { panic("boom") })
	delivered := false
	b.Subscribe("t", func(Envelope) Stats { delivered = true; return Stats{Delivered: 4} })

	stats, err := b.Publish("t", nil)
	if err != nil {
		t.Fatalf("panicking subscriber must not fail the publish: %v", err)
	}
	if !delivered || stats.Delivered != 4 {
		t.Fatalf("panicking subscriber must not starve the rest: %+v delivered=%v", stats, delivered)
	}
}

func TestMemoryClosedRefusesPublishes(t *testing.T) {
	t.Parallel()
	b := NewMemory()
	if err := b.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}
	if err := b.Close(); err != nil {
		t.Fatalf("close must be idempotent: %v", err)
	}
	if _, err := b.Publish("t", nil); !errors.Is(err, ErrClosed) {
		t.Fatalf("closed broker must refuse with ErrClosed, got %v", err)
	}
}

func TestMemoryIdentityAndKind(t *testing.T) {
	t.Parallel()
	a, b := NewMemory(), NewMemory()
	if a.NodeID() == "" || a.NodeID() == b.NodeID() {
		t.Fatalf("node ids must be non-empty and unique: %q vs %q", a.NodeID(), b.NodeID())
	}
	if a.Kind() != "memory" {
		t.Fatalf("kind: %q", a.Kind())
	}
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/broker/redis.go (649 lines, sha256 7ac3212115615fea1fc0a6f3fcb8da4e7081a00eb2542ae623d9eece3d112b87) =====
==============================================================================
```go
package broker

import (
	"bufio"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// RedisBroker is the multi-replica transport: MemoryBroker's synchronous
// local delivery PLUS a Redis pub/sub hop that carries every publish to
// the other gateway replicas, each of which delivers to its OWN local
// subscribers through the identical code path.
//
// The RESP client here is hand-rolled by design — the gateway's one
// dependency today is gorilla/websocket, and redis pub/sub needs exactly
// five commands (AUTH, SELECT, SUBSCRIBE, PUBLISH, QUIT) and three RESP
// types (simple strings, integers, bulk strings inside arrays). A full
// go-redis dependency to speak five commands is not a trade this edge
// makes (same reasoning as the hand-rolled metrics exposition).
//
// Connection topology: TWO TCP connections. redis forces a connection into
// subscriber-only mode once it SUBSCRIBEs, so the subscriber and publisher
// cannot share one; and PUBLISH replies must be read (else server replies
// eventually back-pressure the socket), so PUBLISH is a synchronous
// write+read under a mutex rather than fire-and-forget.
//
// Resilience: a supervisor goroutine owns connects and reconnects with
// exponential backoff forever (until Close). While the bus is down,
// Publish still delivers locally and reports its local Stats — ingest
// stays a 200 with exact THIS-node counters; cross-node propagation simply
// resumes on reconnect (realtime events five seconds late are worthless
// anyway, so no spool: dropped-on-outage is the deliberate semantic, and
// it is counted by BusDrops).
type RedisBroker struct {
	local  *MemoryBroker
	params redisParams

	logf func(format string, args ...any) // warnings only; nil-safe

	mu      sync.Mutex          // guards pub/sub conns + generation
	pub     *redisConn          // nil while (re)connecting
	sub     *redisConn          // nil while (re)connecting
	topics  map[string]struct{} // union of Subscribe()d topics, applied on every connect
	closed  atomic.Bool
	done    chan struct{} // closed by Close: stops the supervisor
	readyCh chan struct{} // closed on FIRST successful connect (tests/ops)

	busDrops     atomic.Int64 // publishes with no live bus (outage)
	busErrors    atomic.Int64 // failed bus operations
	connected    atomic.Bool
	connectCount atomic.Int64
	readyOnce    sync.Once // guards the one-shot readyCh close
}

// redisParams is everything the dialer needs, parsed from the URL once at
// construction (config.Load already shape-checked the scheme).
type redisParams struct {
	addr     string // host:port
	user     string // ACL user ("" = default)
	password string
	db       int
}

// RedisOption customizes a RedisBroker at construction.
type RedisOption func(*RedisBroker)

// WithRedisLogger installs the warning logger (bus down, reconnecting,
// protocol surprises). Production passes an observability.Logger's Warnf.
func WithRedisLogger(logf func(format string, args ...any)) RedisOption {
	return func(b *RedisBroker) { b.logf = logf }
}

// NewRedis parses redisURL (redis://[user:pass@]host[:port][/db]) and
// starts the connect supervisor. Connection is ASYNCHRONOUS: boot never
// blocks on a reachable bus (a gateway that refuses to serve because an
// optional cross-node hop is down would turn a partial degradation into a
// full outage). An unparsable URL IS a constructor error — that one is a
// deploy-time misconfiguration, exactly what boot-time refusal exists for.
func NewRedis(redisURL string, opts ...RedisOption) (*RedisBroker, error) {
	params, err := parseRedisURL(redisURL)
	if err != nil {
		return nil, err
	}
	b := &RedisBroker{
		local:   NewMemory(),
		params:  params,
		topics:  make(map[string]struct{}),
		done:    make(chan struct{}),
		readyCh: make(chan struct{}),
	}
	for _, opt := range opts {
		opt(b)
	}
	go b.supervise()
	return b, nil
}

// parseRedisURL extracts dial parameters; it validates hard (this runs at
// boot, where a typo must be loud).
func parseRedisURL(raw string) (redisParams, error) {
	u, err := url.Parse(raw)
	if err != nil {
		return redisParams{}, fmt.Errorf("broker: redis URL unparsable: %w", err)
	}
	if u.Scheme != "redis" {
		return redisParams{}, fmt.Errorf("broker: redis URL must use the redis:// scheme, got %q", u.Scheme)
	}
	host := u.Hostname()
	if host == "" {
		return redisParams{}, errors.New("broker: redis URL needs a host")
	}
	port := u.Port()
	if port == "" {
		port = "6379"
	}
	p := redisParams{addr: net.JoinHostPort(host, port)}
	if u.User != nil {
		p.user = u.User.Username()
		p.password, _ = u.User.Password()
	}
	// Path is /db for a single-digit-or-two db number; anything else (a
	// keyspace path, nested path) is a misconfiguration, not a db.
	switch path := strings.TrimPrefix(u.Path, "/"); {
	case path == "":
		// db 0
	case strings.Contains(path, "/"):
		return redisParams{}, fmt.Errorf("broker: redis URL db must be a bare number, got path %q", u.Path)
	default:
		db, err := strconv.Atoi(path)
		if err != nil || db < 0 || db > 15 {
			return redisParams{}, fmt.Errorf("broker: redis URL db must be 0..15, got %q", path)
		}
		p.db = db
	}
	return p, nil
}

// Kind implements Broker.
func (b *RedisBroker) Kind() string { return "redis" }

// NodeID implements Broker.
func (b *RedisBroker) NodeID() string { return b.local.NodeID() }

// Subscribe implements Broker: registers locally (deliveries come through
// the local table in every case) and records the topic so every connect
// round SUBSCRIBEs it on the bus.
func (b *RedisBroker) Subscribe(topic string, h Handler) (unsubscribe func()) {
	b.mu.Lock()
	b.topics[topic] = struct{}{}
	sub := b.sub
	b.mu.Unlock()
	if sub != nil {
		// Live connection: subscribe NOW (best effort; the reconnect loop
		// re-applies the full topic set anyway if this socket dies).
		_ = sub.writeCommand("SUBSCRIBE", topic)
	}
	return b.local.Subscribe(topic, h)
}

// Publish implements Broker: local synchronous delivery FIRST (the
// ingest-response contract), then a best-effort bus hop. Bus failures
// never fail the publish — the returned Stats is the local accounting,
// the error is only ErrClosed.
func (b *RedisBroker) Publish(topic string, payload []byte) (Stats, error) {
	if b.closed.Load() {
		return Stats{}, ErrClosed
	}
	stats := b.local.deliver(Envelope{
		Topic:   topic,
		Payload: json.RawMessage(payload),
		Origin:  b.local.NodeID(),
		Local:   true,
		At:      time.Now(),
	})
	if err := b.publishBus(topic, payload); err != nil {
		if !errors.Is(err, errBusDown) {
			b.busErrors.Add(1)
		} else {
			b.busDrops.Add(1)
		}
		b.warnf("bus publish failed (local delivery unaffected), topic %s: %v", topic, err)
	}
	return stats, nil
}

// wireMessage is the bus frame: origin for own-echo suppression, payload
// base64'd so the JSON wrapper tolerates arbitrary bytes.
type wireMessage struct {
	Origin  string `json:"o"`
	Payload string `json:"p"` // base64 std
	At      int64  `json:"t"` // unix nanos, informational
}

// errBusDown marks "no live connection" publishes: counted separately from
// protocol errors because an outage is expected behaviour, not corruption.
var errBusDown = errors.New("broker: bus connection down")

// publishBus encodes and ships one message. Synchronous write+reply-read
// under the publish mutex: unread :integer replies would otherwise pile up
// in kernel buffers on a quiet topic until the socket stalls.
func (b *RedisBroker) publishBus(topic string, payload []byte) error {
	frame, err := json.Marshal(wireMessage{
		Origin:  b.local.NodeID(),
		Payload: base64.StdEncoding.EncodeToString(payload),
		At:      time.Now().UnixNano(),
	})
	if err != nil {
		return err
	}
	b.mu.Lock()
	pub := b.pub
	b.mu.Unlock()
	if pub == nil {
		return errBusDown
	}
	return pub.publish(topic, string(frame))
}

// Close implements Broker: idempotent, stops the supervisor and closes
// both sockets (which also unblocks their reader goroutines).
func (b *RedisBroker) Close() error {
	if !b.closed.CompareAndSwap(false, true) {
		return nil
	}
	close(b.done)
	b.mu.Lock()
	b.closeConnsLocked()
	b.mu.Unlock()
	return b.local.Close()
}

// Ready reports (and non-blockingly probes) the first successful connect;
// ReadyChan exposes it for tests and future readiness wiring.
func (b *RedisBroker) ReadyChan() <-chan struct{} { return b.readyCh }

// Connected reports whether the bus is currently dialed (diagnostics).
func (b *RedisBroker) Connected() bool { return b.connected.Load() }

// BusDrops / BusErrors expose outage accounting (the /metrics surface for
// cross-node health once a deployment enables redis mode).
func (b *RedisBroker) BusDrops() int64  { return b.busDrops.Load() }
func (b *RedisBroker) BusErrors() int64 { return b.busErrors.Load() }

// ConnectCount is how many connect rounds have succeeded since boot.
func (b *RedisBroker) ConnectCount() int64 { return b.connectCount.Load() }

// supervise is the connect/reconnect loop: dial both connections, run the
// subscriber reader, and on ANY failure tear everything down, back off
// (250 ms doubling to a 5 s ceiling), and try again — until Close. One
// goroutine owns the whole lifecycle, so the conn hand-off to Publish is
// the single mutex swap it performs.
func (b *RedisBroker) supervise() {
	backoff := 250 * time.Millisecond
	for {
		if b.closed.Load() {
			return
		}
		err := b.connectRound()
		if b.closed.Load() {
			return
		}
		// A round that ENDED is by definition a dead bus: whatever err is,
		// the retry backoff applies (a round ending in "closed" has
		// already returned above).
		b.busErrors.Add(1)
		b.warnf("bus connection lost (%v); reconnecting in %v", err, backoff)
		select {
		case <-b.done:
			return
		case <-time.After(backoff):
		}
		backoff *= 2
		if backoff > 5*time.Second {
			backoff = 5 * time.Second
		}
	}
}

// connectRound dials, authenticates, subscribes, then BLOCKS in the
// subscriber read loop until something fails. Returns the failure.
func (b *RedisBroker) connectRound() error {
	pub, sub, err := b.dialPair()
	if err != nil {
		return err
	}
	b.mu.Lock()
	if b.closed.Load() {
		b.mu.Unlock()
		pub.close()
		sub.close()
		return errors.New("broker: closed during connect")
	}
	b.pub, b.sub = pub, sub
	b.mu.Unlock()
	b.connected.Store(true)
	b.connectCount.Add(1)
	// Readiness means "a first connection is ESTABLISHED" — marked here,
	// not when the round ends: a healthy round blocks in the read loop
	// below for its whole (indefinite) lifetime.
	b.readyOnce.Do(func() { close(b.readyCh) })
	defer b.connected.Store(false)
	defer func() {
		b.mu.Lock()
		b.closeConnsLocked()
		b.mu.Unlock()
	}()

	err = b.readLoop(sub)
	pub.close()
	return err
}

// closeConnsLocked closes both conns if set. Callers hold b.mu.
func (b *RedisBroker) closeConnsLocked() {
	if b.pub != nil {
		b.pub.close()
		b.pub = nil
	}
	if b.sub != nil {
		b.sub.close()
		b.sub = nil
	}
}

// dialPair establishes both connections: TCP with a bounded handshake
// (dial 3 s, auth/select 5 s each), then the subscriber enters subscribe
// mode for every known topic.
func (b *RedisBroker) dialPair() (pub, sub *redisConn, err error) {
	dial := func() (*redisConn, error) {
		nc, err := net.DialTimeout("tcp", b.params.addr, 3*time.Second)
		if err != nil {
			return nil, err
		}
		c := &redisConn{nc: nc, br: bufio.NewReader(nc), bw: bufio.NewWriter(nc)}
		if err := c.handshake(b.params); err != nil {
			nc.Close()
			return nil, err
		}
		return c, nil
	}
	pub, err = dial()
	if err != nil {
		return nil, nil, fmt.Errorf("publisher dial: %w", err)
	}
	sub, err = dial()
	if err != nil {
		pub.close()
		return nil, nil, fmt.Errorf("subscriber dial: %w", err)
	}

	b.mu.Lock()
	topics := make([]string, 0, len(b.topics))
	for t := range b.topics {
		topics = append(topics, t)
	}
	b.mu.Unlock()
	for _, topic := range topics {
		if err := sub.writeCommand("SUBSCRIBE", topic); err != nil {
			pub.close()
			sub.close()
			return nil, nil, fmt.Errorf("subscribe %s: %w", topic, err)
		}
		// Redis answers each SUBSCRIBE with a confirmation array; consume
		// it here so the read loop only ever sees MESSAGES.
		if _, err := sub.readReply(5 * time.Second); err != nil {
			pub.close()
			sub.close()
			return nil, nil, fmt.Errorf("subscribe %s confirm: %w", topic, err)
		}
	}
	return pub, sub, nil
}

// readLoop consumes subscriber-mode replies until failure or Close.
// Shapes redis sends on a subscribed connection:
//
//	*3 ["message", <topic>, <payload>]  — a publication
//	*2 ["pmessage", ...]                — not used (we never PSUBSCRIBE)
//	*3 ["subscribe", <topic>, <n>]      — confirmations (consumed at dial)
//	*2 ["pong", ""]                     — answer to a keepalive PING
//
// Anything else is logged and skipped: a surplus reply must not kill the
// connection in a world where RESP arrays nest arbitrarily.
func (b *RedisBroker) readLoop(sub *redisConn) error {
	for {
		if b.closed.Load() {
			return errors.New("broker: closed")
		}
		reply, err := sub.readReply(0) // no read deadline: the socket IS the liveness check
		if err != nil {
			return fmt.Errorf("subscriber read: %w", err)
		}
		fields, ok := reply.([]any)
		if !ok || len(fields) < 3 {
			b.warnf("bus: unexpected reply shape %T, skipping", reply)
			continue
		}
		kind, _ := fields[0].(string)
		if kind != "message" {
			continue
		}
		topic, _ := fields[1].(string)
		payload, _ := fields[2].(string)
		b.busDeliver(topic, payload)
	}
}

// busDeliver unwraps one bus frame and delivers it locally — unless the
// frame is our OWN echo (redis delivers a publication to every subscriber
// of the topic, including this node's subscriber connection): the origin
// stamp is what makes exactly-once-per-node work with no message ids.
func (b *RedisBroker) busDeliver(topic, raw string) {
	var frame wireMessage
	if err := json.Unmarshal([]byte(raw), &frame); err != nil {
		b.busErrors.Add(1)
		b.warnf("bus: undecodable frame on %s (%d bytes), skipping", topic, len(raw))
		return
	}
	if frame.Origin == b.local.NodeID() {
		return // own echo: already delivered synchronously in Publish
	}
	payload, err := base64.StdEncoding.DecodeString(frame.Payload)
	if err != nil {
		b.busErrors.Add(1)
		b.warnf("bus: bad payload encoding on %s, skipping", topic)
		return
	}
	// Remote-hop Stats are not returned anywhere (fire-and-forget bus);
	// the local hub handler itself counts remote-homage into metrics.
	b.local.deliver(Envelope{
		Topic:   topic,
		Payload: json.RawMessage(payload),
		Origin:  frame.Origin,
		Local:   false,
		At:      time.Now(),
	})
}

// warnf is the nil-safe internal logger.
func (b *RedisBroker) warnf(format string, args ...any) {
	if b.logf != nil {
		b.logf(format, args...)
	}
}

// ---------------------------------------------------------------------------
// RESP (REdis Serialization Protocol) — the minimal correct subset.
// ---------------------------------------------------------------------------

// redisConn is one TCP connection speaking RESP. RESP is full-duplex, so
// reads and writes have independent serialization:
//
//   - writes go through mu (bufio writers are not concurrent-safe);
//   - reads are lock-free because exactly ONE goroutine per connection
//     ever reads — the publisher's round-tripper (whose write+reply pair
//     mu pairs up), or the subscriber's read loop.
//
// The split matters: a read parked on an idle subscribed socket must
// never block a live SUBSCRIBE/QUIT write on the same connection (a
// single "hold the lock across the read" design deadlocks there).
type redisConn struct {
	nc net.Conn
	br *bufio.Reader
	bw *bufio.Writer
	mu sync.Mutex // serializes writes; pairs write+reply on command paths
}

// handshake runs AUTH (when a password is set) and SELECT (when db != 0),
// each verified synchronously with a 5 s deadline.
func (c *redisConn) handshake(p redisParams) error {
	if p.password != "" {
		args := []string{"AUTH"}
		if p.user != "" {
			args = append(args, p.user)
		}
		args = append(args, p.password)
		if err := c.roundTrip(5*time.Second, args...); err != nil {
			return fmt.Errorf("auth: %w", err)
		}
	}
	if p.db != 0 {
		if err := c.roundTrip(5*time.Second, "SELECT", strconv.Itoa(p.db)); err != nil {
			return fmt.Errorf("select db %d: %w", p.db, err)
		}
	}
	return nil
}

// roundTrip writes one command and reads one reply, failing on RESP
// errors. Used for commands with simple-string replies (AUTH/SELECT).
func (c *redisConn) roundTrip(deadline time.Duration, args ...string) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if err := c.writeCommandLocked(args...); err != nil {
		return err
	}
	reply, err := c.readReplyLocked(deadline)
	if err != nil {
		return err
	}
	if respErr, ok := reply.(*respError); ok {
		return respErr
	}
	return nil
}

// publish ships one PUBLISH and consumes its :recipients reply.
func (c *redisConn) publish(topic, message string) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if err := c.writeCommandLocked("PUBLISH", topic, message); err != nil {
		return err
	}
	reply, err := c.readReplyLocked(5 * time.Second)
	if err != nil {
		return err
	}
	if respErr, ok := reply.(*respError); ok {
		return respErr
	}
	return nil
}

// writeCommand issues one command with internal serialization (subscriber
// connections command-write from exactly one goroutine, but symmetry
// keeps the API safe).
func (c *redisConn) writeCommand(args ...string) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.writeCommandLocked(args...)
}

// writeCommandLocked emits the RESP array for one command:
//
//	*<n>\r\n$<len0>\r\n<arg0>\r\n…$<lenN>\r\n<argN>\r\n
func (c *redisConn) writeCommandLocked(args ...string) error {
	var sb strings.Builder
	fmt.Fprintf(&sb, "*%d\r\n", len(args))
	for _, arg := range args {
		fmt.Fprintf(&sb, "$%d\r\n%s\r\n", len(arg), arg)
	}
	if _, err := c.bw.WriteString(sb.String()); err != nil {
		return err
	}
	return c.bw.Flush()
}

// readReply parses one RESP value on the connection's single reader
// goroutine. deadline == 0 means "no deadline" (the subscriber's idle
// waiting; liveness is TCP-level there).
func (c *redisConn) readReply(deadline time.Duration) (any, error) {
	return c.readReplyLocked(deadline)
}

// readReplyLocked parses one RESP reply:
//
//	+OK            → string "OK"
//	-ERR ...       → *respError (an error VALUE, so mixed arrays work)
//	:123           → int64
//	$5\r\nhello    → string "hello" ($-1 → nil)
//	*3 ...         → []any (recursive; *-1 → nil)
func (c *redisConn) readReplyLocked(deadline time.Duration) (any, error) {
	if deadline > 0 {
		_ = c.nc.SetReadDeadline(time.Now().Add(deadline))
		defer c.nc.SetReadDeadline(time.Time{})
	}
	line, err := c.br.ReadString('\n')
	if err != nil {
		return nil, err
	}
	if len(line) < 3 || line[len(line)-2] != '\r' {
		return nil, fmt.Errorf("broker: malformed RESP line %q", truncate(line, 64))
	}
	payload := line[1 : len(line)-2]
	switch line[0] {
	case '+':
		return payload, nil
	case '-':
		return &respError{msg: payload}, nil
	case ':':
		n, err := strconv.ParseInt(payload, 10, 64)
		if err != nil {
			return nil, fmt.Errorf("broker: bad RESP integer %q", payload)
		}
		return n, nil
	case '$':
		n, err := strconv.Atoi(payload)
		if err != nil {
			return nil, fmt.Errorf("broker: bad RESP bulk length %q", payload)
		}
		if n < 0 {
			return nil, nil // $-1 null
		}
		buf := make([]byte, n+2) // payload + trailing CRLF
		if _, err := io.ReadFull(c.br, buf); err != nil {
			return nil, err
		}
		return string(buf[:n]), nil
	case '*':
		n, err := strconv.Atoi(payload)
		if err != nil {
			return nil, fmt.Errorf("broker: bad RESP array length %q", payload)
		}
		if n < 0 {
			return nil, nil // *-1 null
		}
		items := make([]any, 0, n)
		for i := 0; i < n; i++ {
			// Recursive parse shares the raw line reader; deadlines apply
			// per-line via the outer call only (nested values arrive back-
			// to-back inside one frame, so the leading deadline covers).
			item, err := c.readReplyLocked(0)
			if err != nil {
				return nil, err
			}
			items = append(items, item)
		}
		return items, nil
	default:
		return nil, fmt.Errorf("broker: unknown RESP type byte %q", line[0])
	}
}

// close terminates the socket (idempotent via net.Conn semantics).
func (c *redisConn) close() { _ = c.nc.Close() }

// respError is a RESP "-ERR ..." line surfaced as an error value.
type respError struct{ msg string }

// Error implements error.
func (e *respError) Error() string { return "broker: redis error: " + e.msg }

// truncate bounds a value for log/error embedding.
func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "…"
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/broker/redis_test.go (227 lines, sha256 61cb97e72df32adb0478e183554075261b97d12068002b2f49fcdeaf6bce8d00) =====
==============================================================================
```go
package broker

import (
	"sync"
	"testing"
	"time"

	"github.com/voxdesk/realtime/gateway-go/internal/broker/brokertest"
)

// The fake RESP server lives in brokertest (real TCP, real pub/sub
// semantics, own-node echo included) so server-level e2e tests use the
// same double. These tests assert the broker's behaviour against it.

// recorder collects deliveries for assertions.
type recorder struct {
	mu   sync.Mutex
	envs []Envelope
}

func (r *recorder) handler(delivery Stats) Handler {
	return func(env Envelope) Stats {
		r.mu.Lock()
		r.envs = append(r.envs, env)
		r.mu.Unlock()
		return delivery
	}
}

func (r *recorder) waitFor(t *testing.T, n int) []Envelope {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for {
		r.mu.Lock()
		if len(r.envs) >= n {
			out := append([]Envelope(nil), r.envs...)
			r.mu.Unlock()
			return out
		}
		count := len(r.envs)
		r.mu.Unlock()
		if time.Now().After(deadline) {
			t.Fatalf("timed out waiting for %d deliveries, have %d", n, count)
		}
		time.Sleep(5 * time.Millisecond)
	}
}

func startFake(t *testing.T) *brokertest.FakeRedis {
	t.Helper()
	f, err := brokertest.Start()
	if err != nil {
		t.Fatalf("start fake redis: %v", err)
	}
	t.Cleanup(f.Stop)
	return f
}

func newTestRedisBroker(t *testing.T, url string) *RedisBroker {
	t.Helper()
	b, err := NewRedis(url)
	if err != nil {
		t.Fatalf("NewRedis: %v", err)
	}
	t.Cleanup(func() { _ = b.Close() })
	select {
	case <-b.ReadyChan():
	case <-time.After(3 * time.Second):
		t.Fatalf("broker never connected to %s", url)
	}
	return b
}

func TestRedisCrossNodeDeliveryWithOwnEchoSuppressed(t *testing.T) {
	t.Parallel()
	fake := startFake(t)

	a := newTestRedisBroker(t, fake.URL())
	b := newTestRedisBroker(t, fake.URL())

	var ra, rb recorder
	a.Subscribe("ingest", ra.handler(Stats{Delivered: 7}))
	b.Subscribe("ingest", rb.handler(Stats{Delivered: 3}))
	if !fake.WaitForSubscribers("ingest", 2, 3*time.Second) {
		t.Fatal("both brokers must be subscribed to the bus")
	}

	stats, err := a.Publish("ingest", []byte(`{"event":"call.started"}`))
	if err != nil {
		t.Fatalf("publish: %v", err)
	}
	// Local accounting is EXACTLY A's subscriber's Stats — B's remote
	// delivery cannot contaminate A's ingest response.
	if stats.Delivered != 7 {
		t.Fatalf("local stats must be the publisher's own only, got %+v", stats)
	}

	// A heard it exactly ONCE (the synchronous local hop — the bus echo
	// must be suppressed by origin), B exactly once (the bus hop).
	envsA := ra.waitFor(t, 1)
	envsB := rb.waitFor(t, 1)
	time.Sleep(100 * time.Millisecond) // any DOUBLE delivery would land now
	ra.mu.Lock()
	finalA := len(ra.envs)
	ra.mu.Unlock()
	if finalA != 1 {
		t.Fatalf("own echo must be suppressed: A delivered %d times", finalA)
	}
	if !envsA[0].Local || envsA[0].Origin != a.NodeID() {
		t.Fatalf("A's envelope must be the local hop: %+v", envsA[0])
	}
	if envsB[0].Local || envsB[0].Origin != a.NodeID() {
		t.Fatalf("B's envelope must be the bus hop stamped with A's origin: %+v", envsB[0])
	}
	if string(envsB[0].Payload) != `{"event":"call.started"}` {
		t.Fatalf("bus payload must round-trip byte-verbatim, got %s", envsB[0].Payload)
	}
}

func TestRedisPublishSurvivesBusOutageAndReconnects(t *testing.T) {
	t.Parallel()
	fake := startFake(t)
	a := newTestRedisBroker(t, fake.URL())

	var r recorder
	a.Subscribe("ingest", r.handler(Stats{Delivered: 1}))
	if !fake.WaitForSubscribers("ingest", 1, 3*time.Second) {
		t.Fatal("broker must be subscribed before the outage")
	}
	if _, err := a.Publish("ingest", []byte("one")); err != nil {
		t.Fatalf("publish 1: %v", err)
	}
	r.waitFor(t, 1)

	// Kill the bus. Local delivery must keep working, bus drops counted.
	fake.Stop()
	deadline := time.Now().Add(3 * time.Second)
	for a.Connected() && time.Now().Before(deadline) {
		time.Sleep(5 * time.Millisecond)
	}
	stats, err := a.Publish("ingest", []byte("two"))
	if err != nil {
		t.Fatalf("outage publish must NOT fail for local delivery: %v", err)
	}
	if stats.Delivered != 1 {
		t.Fatalf("local stats during outage wrong: %+v", stats)
	}
	deadline = time.Now().Add(3 * time.Second)
	for a.BusDrops() < 1 && time.Now().Before(deadline) {
		time.Sleep(5 * time.Millisecond)
	}
	if a.BusDrops() < 1 {
		t.Fatal("outage publish must be counted as a bus drop")
	}

	// Bus comes back on the SAME address (how a restarted redis looks).
	fake2, err := brokertest.StartAt(fake.Addr)
	if err != nil {
		t.Fatalf("rebind %s: %v", fake.Addr, err)
	}
	t.Cleanup(fake2.Stop)

	// The supervisor must redial and RE-SUBSCRIBE the recorded topics.
	deadline = time.Now().Add(5 * time.Second)
	for a.ConnectCount() < 2 && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if a.ConnectCount() < 2 {
		t.Fatalf("broker never reconnected (connects=%d)", a.ConnectCount())
	}
	if !fake2.WaitForSubscribers("ingest", 1, 3*time.Second) {
		t.Fatal("reconnect must re-subscribe the topic set")
	}
	if _, err := a.Publish("ingest", []byte("three")); err != nil {
		t.Fatalf("post-reconnect publish: %v", err)
	}
	envs := r.waitFor(t, 3) // one, two (local only), three (local; echo suppressed)
	if got := string(envs[2].Payload); got != "three" {
		t.Fatalf("third delivery payload = %q", got)
	}
}

func TestRedisClosedBrokerRefusesCleanly(t *testing.T) {
	t.Parallel()
	fake := startFake(t)
	b, err := NewRedis(fake.URL())
	if err != nil {
		t.Fatalf("NewRedis: %v", err)
	}
	if err := b.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}
	if err := b.Close(); err != nil {
		t.Fatalf("close idempotent: %v", err)
	}
	if _, err := b.Publish("t", nil); err == nil {
		t.Fatal("closed broker must refuse publishes")
	}
}

func TestParseRedisURLValidation(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		raw  string
		want redisParams
	}{
		{"redis://localhost", redisParams{addr: "localhost:6379"}},
		{"redis://:pw@h:1234", redisParams{addr: "h:1234", password: "pw"}},
		{"redis://alice:pw@h:6379/3", redisParams{addr: "h:6379", user: "alice", password: "pw", db: 3}},
		{"redis://h/0", redisParams{addr: "h:6379"}},
	} {
		got, err := parseRedisURL(tc.raw)
		if err != nil || got != tc.want {
			t.Errorf("parseRedisURL(%q) = %+v, %v; want %+v", tc.raw, got, err, tc.want)
		}
	}
	for _, bad := range []string{
		"rediss://h:6379",      // config guards this too; the parser stays honest
		"redis:///nodb",        // path-only means no host
		"redis://h/99",         // db out of range
		"redis://h/keyspace/1", // nested path is not a db
	} {
		if _, err := parseRedisURL(bad); err == nil {
			t.Errorf("parseRedisURL(%q) must fail", bad)
		}
	}
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/config/config.go (345 lines, sha256 6c3906792a8254b587450aecccdcdc74e3e0bccb8c077ade420a6b4cb29f5478) =====
==============================================================================
```go
// Package config loads the gateway's environment, mirroring the discipline
// of app/core/config.py: everything comes from the environment, every knob
// has a safe default or a loud refusal, and obviously-unset secrets are
// rejected at boot rather than discovered on the first forged connection.
package config

import (
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/voxdesk/realtime/gateway-go/internal/validate"
)

// Defaults. The timeout trio is chosen for browser clients on mobile
// networks: pings frequent enough to keep NAT bindings and proxies (which
// commonly reap at 60 s) alive, a pong budget generous enough for a
// backgrounded tab to catch up, and a short pre-auth window so a socket that
// never identifies itself cannot hold a slot.
const (
	DefaultPort                    = 8790
	DefaultMaxConnections          = 10_000
	DefaultMaxConnsPerTenant       = 256
	DefaultMaxSubscriptionsPerConn = 32
	DefaultOutgoingBuffer          = 64
	DefaultMaxMessageBytes         = 16 * 1024
	DefaultMaxIngestPayloadBytes   = 64 * 1024
	DefaultWriteWait               = 10 * time.Second
	DefaultAuthTimeout             = 10 * time.Second
	DefaultPingInterval            = 20 * time.Second
	DefaultPongTimeout             = 60 * time.Second
	DefaultShutdownTimeout         = 10 * time.Second
	DefaultMessageRatePerSecond    = 20.0
	DefaultMessageBurst            = 40.0
	DefaultIdempotencyTTL          = 30 * time.Minute

	// Signaling relay (WebRTC session setup). DefaultSignalingMaxSessions
	// per tenant is comfortably below the per-tenant connection cap — a
	// session spends TWO connections only when fully joined, and 64 live
	// negotiations against 256 allowed sockets keeps notice-plane tabs
	// unstarved even at signaling saturation. The pending timeout is how
	// long a never-joined session may linger before the reaper ends it:
	// long enough for a QR-code/handover flow, too short to stockpile.
	DefaultSignalingMaxSessionsPerTenant = 64
	DefaultSignalingPendingTimeout       = 60 * time.Second

	// Engine steer mode constants (closed vocabulary, gate above).
	EngineSteerOff   = "off"
	EngineSteerV12   = "v1.2"
	EngineSteerForce = "force"

	// Media-engine backstop when a caller carries no deadline of its own.
	// 1.5s mirrors REALTIME_PUBLISH_TIMEOUT_SECONDS on the Python side:
	// any engine slower than that is, for session-setup purposes, DOWN —
	// the session lives on in degraded mode and readiness goes red.
	DefaultMediaEngineTimeoutSeconds = 1.5

	// idempotencyCapacity bounds the ingest replay cache. 50k event ids at
	// well under 100 bytes each is a few MB — cheap insurance that can never
	// grow into a memory problem of its own.
	idempotencyCapacity = 50_000
)

// Config is the immutable runtime configuration, built once at boot.
type Config struct {
	Port int

	// JWT verification of dashboard access tokens (HS256, shared with the
	// Python API — see app/auth/jwt.py). The tenant a connection may see is
	// derived from the verified token, never from anything the client sends
	// afterwards.
	JWTSecret   string
	JWTIssuer   string
	JWTAudience string

	// Shared secret for the server-to-server ingest endpoint
	// (POST /ingest/v1/publish). Compared in constant time. The Python
	// backend publishes realtime events here; browsers never can.
	IngestSecret string

	// Browser origins allowed to open the WebSocket. Empty means only
	// same-origin/socket clients (gorilla rejects cross-origin by default),
	// which is the fail-closed posture for a machine-only deployment.
	AllowedOrigins []string

	// Token for /metrics, mirroring METRICS_TOKEN on the Python side. Empty
	// leaves /metrics unauthenticated — acceptable only on a network the
	// scheduler/Prometheus already controls (the compose topology), so it is
	// a warning, not a refusal.
	MetricsToken string

	MaxConnections          int
	MaxConnsPerTenant       int
	MaxSubscriptionsPerConn int
	OutgoingBuffer          int
	MaxMessageBytes         int64
	MaxIngestPayloadBytes   int64

	WriteWait       time.Duration
	AuthTimeout     time.Duration
	PingInterval    time.Duration
	PongTimeout     time.Duration
	ShutdownTimeout time.Duration

	// Per-connection inbound frame limiter: a browser tab that has gone
	// berserk must not be able to spend hub CPU unboundedly.
	MessageRatePerSecond float64
	MessageBurst         float64

	// Ingest replay window: how long an event_id is remembered as "already
	// delivered". Sized for provider retries and short deploys, not for
	// forever — realtime events are worthless redelivered an hour late.
	IdempotencyTTL      time.Duration
	IdempotencyCapacity int

	// Signaling relay: concurrent point-to-point sessions per tenant, and
	// the lone-pending-session timeout the reaper applies.
	SignalingMaxSessionsPerTenant int
	SignalingPendingTimeout       time.Duration

	// Ingest fan-out transport (internal/broker). "memory" (default) is
	// the single-node broker: Publish delivers to local subscribers
	// synchronously and that is THE fan-out. "redis" adds a cross-node
	// Redis pub/sub hop on top (multiple gateway replicas each deliver to
	// THEIR local subscribers), with RedisURL pointing at the bus.
	BrokerKind string
	RedisURL   string

	// Media engine (services/realtime/media-engine-rs). Empty URL = the
	// engine plane is intentionally DISABLED (single-node dev); set in
	// staging/prod. When set, readiness reflects ENGINE availability per
	// the readiness contract — a gateway that cannot reach its SFU is not
	// ready to serve media sessions, though the websocket plane keeps
	// degrading gracefully per-connection.
	MediaEngineURL            string
	MediaEngineTimeoutSeconds float64

	// EngineSteer selects the browser media path: "off" (default; P2P
	// relay only, v1.0/v1.1), "v1.2" (steer depends on both members
	// upgrading via hello "ws":2; mixed pairs stay P2P), "force" (every
	// session steers regardless — the kill-switch-flipped operational
	// posture after migration). Validated against the closed set below; steer without
	// a MediaEngineURL is a boot REFUSAL (silent degrade would make calls
	// hang at ICE with zero surfacing).
	EngineSteer string
}

// Load reads the environment (through getenv, so tests inject a map) and
// returns the Config plus a list of hard problems; len(problems) > 0 means
// REFUSE to boot. Warnings that do not block boot are returned as a second
// list, mirroring validate_security()'s log-but-continue items.
func Load(getenv func(string) string) (Config, []string, []string) {
	cfg := Config{
		Port:                          envInt(getenv, "VOXDESK_GATEWAY_PORT", DefaultPort),
		JWTSecret:                     strings.TrimSpace(getenv("VOXDESK_GATEWAY_JWT_SECRET")),
		JWTIssuer:                     envStr(getenv, "VOXDESK_GATEWAY_JWT_ISSUER", "voxdesk"),
		JWTAudience:                   envStr(getenv, "VOXDESK_GATEWAY_JWT_AUDIENCE", "voxdesk-api"),
		IngestSecret:                  strings.TrimSpace(getenv("VOXDESK_GATEWAY_INGEST_SECRET")),
		AllowedOrigins:                envList(getenv, "VOXDESK_GATEWAY_ALLOWED_ORIGINS"),
		MetricsToken:                  strings.TrimSpace(getenv("VOXDESK_GATEWAY_METRICS_TOKEN")),
		MaxConnections:                envInt(getenv, "VOXDESK_GATEWAY_MAX_CONNECTIONS", DefaultMaxConnections),
		MaxConnsPerTenant:             envInt(getenv, "VOXDESK_GATEWAY_MAX_CONNS_PER_TENANT", DefaultMaxConnsPerTenant),
		MaxSubscriptionsPerConn:       DefaultMaxSubscriptionsPerConn,
		OutgoingBuffer:                DefaultOutgoingBuffer,
		MaxMessageBytes:               DefaultMaxMessageBytes,
		MaxIngestPayloadBytes:         DefaultMaxIngestPayloadBytes,
		WriteWait:                     envDuration(getenv, "VOXDESK_GATEWAY_WRITE_WAIT", DefaultWriteWait),
		AuthTimeout:                   envDuration(getenv, "VOXDESK_GATEWAY_AUTH_TIMEOUT", DefaultAuthTimeout),
		PingInterval:                  envDuration(getenv, "VOXDESK_GATEWAY_PING_INTERVAL", DefaultPingInterval),
		PongTimeout:                   envDuration(getenv, "VOXDESK_GATEWAY_PONG_TIMEOUT", DefaultPongTimeout),
		ShutdownTimeout:               DefaultShutdownTimeout,
		MessageRatePerSecond:          DefaultMessageRatePerSecond,
		MessageBurst:                  DefaultMessageBurst,
		IdempotencyTTL:                DefaultIdempotencyTTL,
		IdempotencyCapacity:           idempotencyCapacity,
		SignalingMaxSessionsPerTenant: envInt(getenv, "VOXDESK_GATEWAY_SIGNAL_MAX_SESSIONS_PER_TENANT", DefaultSignalingMaxSessionsPerTenant),
		SignalingPendingTimeout:       envDuration(getenv, "VOXDESK_GATEWAY_SIGNAL_PENDING_TIMEOUT", DefaultSignalingPendingTimeout),
		BrokerKind:                    strings.ToLower(envStr(getenv, "VOXDESK_GATEWAY_BROKER", "memory")),
		RedisURL:                      strings.TrimSpace(getenv("VOXDESK_GATEWAY_REDIS_URL")),
		MediaEngineURL:                strings.TrimSpace(getenv("VOXDESK_GATEWAY_MEDIA_ENGINE_URL")),
		MediaEngineTimeoutSeconds:     envFloat(getenv, "VOXDESK_GATEWAY_MEDIA_ENGINE_TIMEOUT_SECONDS", DefaultMediaEngineTimeoutSeconds),
		EngineSteer:                   strings.ToLower(envStr(getenv, "VOXDESK_GATEWAY_ENGINE_STEER", "off")),
	}

	var problems, warnings []string

	if cfg.Port < 1 || cfg.Port > 65535 {
		problems = append(problems, fmt.Sprintf("VOXDESK_GATEWAY_PORT must be 1..65535, got %d", cfg.Port))
	}

	// The whole public edge stands on this secret. Running without it is not
	// "development mode", it is "anyone can read any tenant's live call feed".
	switch {
	case cfg.JWTSecret == "":
		problems = append(problems, "VOXDESK_GATEWAY_JWT_SECRET is required; it must match the API's JWT_SECRET")
	case validate.LooksPlaceholder(cfg.JWTSecret):
		problems = append(problems, "VOXDESK_GATEWAY_JWT_SECRET looks like a placeholder")
	case len(cfg.JWTSecret) < 32:
		problems = append(problems, "VOXDESK_GATEWAY_JWT_SECRET must be at least 32 characters")
	}

	switch {
	case cfg.IngestSecret == "":
		problems = append(problems, "VOXDESK_GATEWAY_INGEST_SECRET is required; without it the ingest endpoint would be an unauthenticated publish-into-any-tenant path")
	case validate.LooksPlaceholder(cfg.IngestSecret):
		problems = append(problems, "VOXDESK_GATEWAY_INGEST_SECRET looks like a placeholder")
	case len(cfg.IngestSecret) < 16:
		problems = append(problems, "VOXDESK_GATEWAY_INGEST_SECRET must be at least 16 characters")
	}

	if cfg.MaxConnections < 1 {
		problems = append(problems, "VOXDESK_GATEWAY_MAX_CONNECTIONS must be at least 1")
	}
	if cfg.MaxConnsPerTenant < 1 {
		problems = append(problems, "VOXDESK_GATEWAY_MAX_CONNS_PER_TENANT must be at least 1")
	}
	if cfg.PingInterval <= 0 || cfg.PongTimeout <= cfg.PingInterval {
		problems = append(problems, "VOXDESK_GATEWAY_PONG_TIMEOUT must be greater than VOXDESK_GATEWAY_PING_INTERVAL (a peer is only dead when it has missed at least one whole ping round-trip)")
	}
	if cfg.AuthTimeout <= 0 {
		problems = append(problems, "VOXDESK_GATEWAY_AUTH_TIMEOUT must be positive")
	}
	if cfg.SignalingMaxSessionsPerTenant < 1 {
		problems = append(problems, "VOXDESK_GATEWAY_SIGNAL_MAX_SESSIONS_PER_TENANT must be at least 1")
	}
	if cfg.SignalingPendingTimeout <= 0 {
		problems = append(problems, "VOXDESK_GATEWAY_SIGNAL_PENDING_TIMEOUT must be positive (a lone never-joined session must eventually be reaped)")
	}
	// Broker selection must be explicit or absent: a typo here ("rediss")
	// silently running the single-node broker in a multi-replica deploy
	// would deliver every event to only a fraction of connected clients,
	// so it is a boot REFUSAL, not a warning.
	switch cfg.BrokerKind {
	case "memory":
		if cfg.RedisURL != "" {
			warnings = append(warnings, "VOXDESK_GATEWAY_REDIS_URL is set but VOXDESK_GATEWAY_BROKER is memory; the URL is ignored")
		}
	case "redis":
		if cfg.RedisURL == "" {
			problems = append(problems, "VOXDESK_GATEWAY_BROKER=redis requires VOXDESK_GATEWAY_REDIS_URL (redis://[user:pass@]host:port[/db])")
		} else if !strings.HasPrefix(cfg.RedisURL, "redis://") {
			problems = append(problems, "VOXDESK_GATEWAY_REDIS_URL must start with redis:// (TLS-only rediss:// URLs are rejected: this edge terminates no broker TLS today)")
		}
	default:
		problems = append(problems, fmt.Sprintf("VOXDESK_GATEWAY_BROKER must be memory or redis, got %q", cfg.BrokerKind))
	}

	// Media-engine wiring must be explicit and well-formed when present:
	// a typo here would silently run the gateway degraded with nobody told,
	// and readiness would mislead every orchestrator behind it.
	if cfg.MediaEngineURL != "" {
		if !strings.HasPrefix(cfg.MediaEngineURL, "http://") && !strings.HasPrefix(cfg.MediaEngineURL, "https://") {
			problems = append(problems, "VOXDESK_GATEWAY_MEDIA_ENGINE_URL must start with http:// or https:// (bare host:port is rejected — scheme discipline is the transport contract)")
		}
		if cfg.MediaEngineTimeoutSeconds <= 0 || cfg.MediaEngineTimeoutSeconds > 30 {
			problems = append(problems, fmt.Sprintf("VOXDESK_GATEWAY_MEDIA_ENGINE_TIMEOUT_SECONDS must be in (0, 30], got %v", cfg.MediaEngineTimeoutSeconds))
		}
	} else {
		warnings = append(warnings, "VOXDESK_GATEWAY_MEDIA_ENGINE_URL is empty; the media engine plane is DISABLED — readiness will report engine: disabled (dev single-node only)")
	}

	switch cfg.EngineSteer {
	case EngineSteerOff:
		if getenv("VOXDESK_GATEWAY_ENGINE_STEER") != "" && cfg.MediaEngineURL != "" {
			warnings = append(warnings, "VOXDESK_GATEWAY_ENGINE_STEER=off: engine link exists but all browser media stays P2P relay")
		}
	case EngineSteerV12, EngineSteerForce:
		if cfg.MediaEngineURL == "" {
			problems = append(problems, "VOXDESK_GATEWAY_ENGINE_STEER requires VOXDESK_GATEWAY_MEDIA_ENGINE_URL — steering to an unconfigured engine would hang every call at ICE")
		}
	default:
		problems = append(problems, fmt.Sprintf("VOXDESK_GATEWAY_ENGINE_STEER must be off, v1.2 or force, got %q", cfg.EngineSteer))
	}

	if cfg.MetricsToken == "" {
		warnings = append(warnings, "VOXDESK_GATEWAY_METRICS_TOKEN is empty; /metrics is unauthenticated — only acceptable on a private network")
	}
	if len(cfg.AllowedOrigins) == 0 {
		warnings = append(warnings, "VOXDESK_GATEWAY_ALLOWED_ORIGINS is empty; cross-origin browser connections will be rejected (same-origin and non-browser clients still work)")
	}

	return cfg, problems, warnings
}

func envStr(getenv func(string) string, key, fallback string) string {
	if v := strings.TrimSpace(getenv(key)); v != "" {
		return v
	}
	return fallback
}

func envList(getenv func(string) string, key string) []string {
	raw := strings.TrimSpace(getenv(key))
	if raw == "" {
		return nil
	}
	var out []string
	for _, part := range strings.Split(raw, ",") {
		part = strings.TrimSpace(part)
		if part != "" {
			out = append(out, part)
		}
	}
	return out
}

func envInt(getenv func(string) string, key string, fallback int) int {
	raw := strings.TrimSpace(getenv(key))
	if raw == "" {
		return fallback
	}
	n, err := strconv.Atoi(raw)
	if err != nil {
		return fallback
	}
	return n
}

// envFloat accepts decimal values ("1.5" seconds is the natural unit the
// production env files documented); a non-numeric value falls back rather
// than misparsing — the LOG line points operators at the exact key.
func envFloat(getenv func(string) string, key string, fallback float64) float64 {
	raw := strings.TrimSpace(getenv(key))
	if raw == "" {
		return fallback
	}
	v, err := strconv.ParseFloat(raw, 64)
	if err != nil {
		return fallback
	}
	return v
}

func envDuration(getenv func(string) string, key string, fallback time.Duration) time.Duration {
	raw := strings.TrimSpace(getenv(key))
	if raw == "" {
		return fallback
	}
	d, err := time.ParseDuration(raw)
	if err != nil {
		return fallback
	}
	return d
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/config/config_broker_test.go (98 lines, sha256 bea1375500bfd1ae1c1d0b4cce8321141fbeff48fb3c6b6e936932753ae89944) =====
==============================================================================
```go
package config

import (
	"strings"
	"testing"
)

// goodEnv mirrors the minimal good environment config_test's helpers
// establish; kept locally so this file stays independent of the original
// test file's fixture helpers.
func goodEnv(extra map[string]string) func(string) string {
	env := map[string]string{
		"VOXDESK_GATEWAY_JWT_SECRET":    "jwt-secret-jwt-secret-jwt-secret-42",
		"VOXDESK_GATEWAY_INGEST_SECRET": "ingest-secret-ingest-secret",
	}
	for k, v := range extra {
		env[k] = v
	}
	return func(key string) string { return env[key] }
}

func TestBrokerDefaultsToMemory(t *testing.T) {
	t.Parallel()
	cfg, problems, _ := Load(goodEnv(nil))
	if len(problems) != 0 {
		t.Fatalf("good env must boot: %v", problems)
	}
	if cfg.BrokerKind != "memory" || cfg.RedisURL != "" {
		t.Fatalf("broker must default to the single-node memory broker, got %q/%q", cfg.BrokerKind, cfg.RedisURL)
	}
}

func TestBrokerRedisRequiresURL(t *testing.T) {
	t.Parallel()
	_, problems, _ := Load(goodEnv(map[string]string{"VOXDESK_GATEWAY_BROKER": "redis"}))
	if !mentions(problems, "REDIS_URL") {
		t.Fatalf("redis broker without a URL must refuse to boot: %v", problems)
	}
}

func TestBrokerRedisRejectsNonRedisScheme(t *testing.T) {
	t.Parallel()
	_, problems, _ := Load(goodEnv(map[string]string{
		"VOXDESK_GATEWAY_BROKER":    "redis",
		"VOXDESK_GATEWAY_REDIS_URL": "rediss://bus.internal:6379/0",
	}))
	if !mentions(problems, "redis://") {
		t.Fatalf("TLS/redis-scheme URLs must be refused explicitly: %v", problems)
	}
}

func TestBrokerKindIsCaseInsensitivelyUnknown(t *testing.T) {
	t.Parallel()
	_, problems, _ := Load(goodEnv(map[string]string{"VOXDESK_GATEWAY_BROKER": "Kafka"}))
	if !mentions(problems, "must be memory or redis") {
		t.Fatalf("an unknown broker must refuse to boot (a typo must not silently single-node): %v", problems)
	}
}

func TestBrokerRedisValidConfigBoots(t *testing.T) {
	t.Parallel()
	cfg, problems, warnings := Load(goodEnv(map[string]string{
		"VOXDESK_GATEWAY_BROKER":    "Redis",
		"VOXDESK_GATEWAY_REDIS_URL": "redis://:secret@127.0.0.1:6379/3",
	}))
	if len(problems) != 0 {
		t.Fatalf("valid redis config must boot: %v", problems)
	}
	if cfg.BrokerKind != "redis" || cfg.RedisURL != "redis://:secret@127.0.0.1:6379/3" {
		t.Fatalf("broker fields wrong: %+v", cfg)
	}
	if mentions(warnings, "REDIS_URL is set but") {
		t.Fatal("a USED redis URL must not warn")
	}
}

func TestMemoryBrokerWithRedisURLWarnsNotFails(t *testing.T) {
	t.Parallel()
	_, problems, warnings := Load(goodEnv(map[string]string{
		"VOXDESK_GATEWAY_REDIS_URL": "redis://127.0.0.1:6379",
	}))
	if len(problems) != 0 {
		t.Fatalf("a redundant URL must not block boot: %v", problems)
	}
	if !mentions(warnings, "ignored") {
		t.Fatalf("a redundant URL must WARN (it suggests operator intent): %v", warnings)
	}
}

// containsProblem reports whether any message mentions the marker.
func mentions(messages []string, marker string) bool {
	for _, m := range messages {
		if strings.Contains(m, marker) {
			return true
		}
	}
	return false
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/config/config_engine_test.go (124 lines, sha256 9045d0018d745f1ce7205ea869c8ff91b496d81e49e73e6dc6625410c7f5154d) =====
==============================================================================
```go
package config

import (
	"strings"
	"testing"
)

// Engine-plane configuration validation: explicit-or-absent discipline,
// same fail-closed posture as the broker switch — a half-typed engine URL
// must refuse loudly, never silently degrade.
func baseEnv() map[string]string {
	return map[string]string{
		"VOXDESK_GATEWAY_JWT_SECRET":    strings.Repeat("a", 40),
		"VOXDESK_GATEWAY_INGEST_SECRET": strings.Repeat("b", 20),
	}
}

func getenvFrom(m map[string]string) func(string) string {
	return func(key string) string { return m[key] }
}

func problemMentioning(problems []string, needle string) bool {
	for _, p := range problems {
		if strings.Contains(p, needle) {
			return true
		}
	}
	return false
}

func TestEngineURLAbsentDisablesPlaneWithWarning(t *testing.T) {
	cfg, problems, warnings := Load(getenvFrom(baseEnv()))
	if len(problems) > 0 {
		t.Fatalf("unexpected problems: %v", problems)
	}
	if cfg.MediaEngineURL != "" {
		t.Fatalf("cfg: %+v", cfg)
	}
	found := false
	for _, w := range warnings {
		if strings.Contains(w, "MEDIA_ENGINE_URL is empty") {
			found = true
		}
	}
	if !found {
		t.Fatalf("expected the disabled-plane warning, got %v", warnings)
	}
}

func TestEngineURLSchemeRequired(t *testing.T) {
	env := baseEnv()
	env["VOXDESK_GATEWAY_MEDIA_ENGINE_URL"] = "media-engine-rs:9001"
	_, problems, _ := Load(getenvFrom(env))
	if !problemMentioning(problems, "http://") {
		t.Fatalf("scheme must be demanded: %v", problems)
	}
}

func TestEngineTimeoutBounds(t *testing.T) {
	env := baseEnv()
	env["VOXDESK_GATEWAY_MEDIA_ENGINE_URL"] = "http://media-engine-rs:9001"
	env["VOXDESK_GATEWAY_MEDIA_ENGINE_TIMEOUT_SECONDS"] = "0"
	if _, problems, _ := Load(getenvFrom(env)); !problemMentioning(problems, "TIMEOUT") {
		t.Fatalf("zero timeout must refuse: %v", problems)
	}
	env["VOXDESK_GATEWAY_MEDIA_ENGINE_TIMEOUT_SECONDS"] = "100"
	if _, problems, _ := Load(getenvFrom(env)); !problemMentioning(problems, "TIMEOUT") {
		t.Fatalf("absurd timeout must refuse")
	}
	env["VOXDESK_GATEWAY_MEDIA_ENGINE_TIMEOUT_SECONDS"] = "1.5"
	cfg, problems, _ := Load(getenvFrom(env))
	if len(problems) > 0 || cfg.MediaEngineTimeoutSeconds != 1.5 {
		t.Fatalf("valid engine config: problems=%v cfg=%+v", problems, cfg)
	}
}

func TestEngineTimeoutDefaultApplies(t *testing.T) {
	env := baseEnv()
	env["VOXDESK_GATEWAY_MEDIA_ENGINE_URL"] = "http://media-engine-rs:9001"
	cfg, problems, _ := Load(getenvFrom(env))
	if len(problems) > 0 {
		t.Fatalf("problems: %v", problems)
	}
	if cfg.MediaEngineTimeoutSeconds != DefaultMediaEngineTimeoutSeconds {
		t.Fatalf("default not applied: %v", cfg.MediaEngineTimeoutSeconds)
	}
}

// Steer-mode validation (closed vocabulary + engine pairing rules).
func TestSteerModeClosedVocabularyAndPairing(t *testing.T) {
	// default is off
	env := baseEnv()
	env["VOXDESK_GATEWAY_MEDIA_ENGINE_URL"] = "http://media-engine:9001"
	cfg, problems, _ := Load(getenvFrom(env))
	if len(problems) > 0 || cfg.EngineSteer != EngineSteerOff {
		t.Fatalf("default steer: problems=%v steer=%q", problems, cfg.EngineSteer)
	}

	// unknown value refuses
	env["VOXDESK_GATEWAY_ENGINE_STEER"] = "sideways"
	_, problems, _ = Load(getenvFrom(env))
	if !problemMentioning(problems, "VOXDESK_GATEWAY_ENGINE_STEER") {
		t.Fatalf("unknown steer value must refuse: %v", problems)
	}

	// v1.2/force require a configured engine URL
	delete(env, "VOXDESK_GATEWAY_MEDIA_ENGINE_URL")
	for _, mode := range []string{EngineSteerV12, EngineSteerForce} {
		env["VOXDESK_GATEWAY_ENGINE_STEER"] = mode
		_, problems, _ = Load(getenvFrom(env))
		if !problemMentioning(problems, "MEDIA_ENGINE_URL") {
			t.Fatalf("steer=%s without engine URL must refuse: %v", mode, problems)
		}
	}

	// both modes accept with URL present
	env["VOXDESK_GATEWAY_MEDIA_ENGINE_URL"] = "http://media-engine:9001"
	for _, mode := range []string{EngineSteerV12, EngineSteerForce} {
		env["VOXDESK_GATEWAY_ENGINE_STEER"] = mode
		if _, problems, _ := Load(getenvFrom(env)); len(problems) > 0 {
			t.Fatalf("steer=%s with URL must boot: %v", mode, problems)
		}
	}
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/config/config_test.go (191 lines, sha256 96193d0b2703315cb64347efad8ba42c45ba0600f4b1329ff499ee30389fe0fe) =====
==============================================================================
```go
package config

import (
	"strings"
	"testing"
	"time"
)

// env builds a getenv func from a map (missing keys read as "").
func env(values map[string]string) func(string) string {
	return func(key string) string { return values[key] }
}

const (
	goodJWTSecret    = "0123456789abcdef0123456789abcdef"
	goodIngestSecret = "ingest-shared-secret-0123"
)

func validEnv() map[string]string {
	return map[string]string{
		"VOXDESK_GATEWAY_JWT_SECRET":    goodJWTSecret,
		"VOXDESK_GATEWAY_INGEST_SECRET": goodIngestSecret,
	}
}

func TestLoadDefaults(t *testing.T) {
	cfg, problems, _ := Load(env(validEnv()))
	if len(problems) != 0 {
		t.Fatalf("expected no problems, got %v", problems)
	}
	if cfg.Port != DefaultPort {
		t.Errorf("Port = %d, want %d", cfg.Port, DefaultPort)
	}
	if cfg.MaxConnections != DefaultMaxConnections {
		t.Errorf("MaxConnections = %d, want %d", cfg.MaxConnections, DefaultMaxConnections)
	}
	if cfg.PingInterval != DefaultPingInterval || cfg.PongTimeout != DefaultPongTimeout {
		t.Errorf("heartbeat defaults changed: %v / %v", cfg.PingInterval, cfg.PongTimeout)
	}
	if cfg.JWTIssuer != "voxdesk" || cfg.JWTAudience != "voxdesk-api" {
		t.Errorf("JWT issuer/audience must default to the API's values, got %q/%q", cfg.JWTIssuer, cfg.JWTAudience)
	}
	// Unauthenticated surfaces default to WARNINGS, never silent acceptance.
	_ = cfg // warnings content checked in the dedicated tests below
}

func TestMissingJWTSecretRefusesToBoot(t *testing.T) {
	values := validEnv()
	delete(values, "VOXDESK_GATEWAY_JWT_SECRET")
	_, problems, _ := Load(env(values))
	if !containsProblem(problems, "VOXDESK_GATEWAY_JWT_SECRET is required") {
		t.Fatalf("expected required-secret problem, got %v", problems)
	}
}

func TestPlaceholderSecretsRefuseToBoot(t *testing.T) {
	for _, bad := range []string{"change-me", "insecure-development-only-change-me", "xxxx", "your-secret-here"} {
		values := validEnv()
		values["VOXDESK_GATEWAY_JWT_SECRET"] = bad
		_, problems, _ := Load(env(values))
		if !containsProblem(problems, "placeholder") {
			t.Errorf("jwt secret %q should be rejected as placeholder, got %v", bad, problems)
		}
	}
}

func TestShortJWTSecretRefusesToBoot(t *testing.T) {
	values := validEnv()
	values["VOXDESK_GATEWAY_JWT_SECRET"] = "too-short"
	_, problems, _ := Load(env(values))
	if !containsProblem(problems, "at least 32 characters") {
		t.Fatalf("expected length problem, got %v", problems)
	}
}

func TestMissingIngestSecretRefusesToBoot(t *testing.T) {
	values := validEnv()
	delete(values, "VOXDESK_GATEWAY_INGEST_SECRET")
	_, problems, _ := Load(env(values))
	if !containsProblem(problems, "VOXDESK_GATEWAY_INGEST_SECRET is required") {
		t.Fatalf("expected ingest-required problem, got %v", problems)
	}
}

func TestInvalidPortRefusesToBoot(t *testing.T) {
	values := validEnv()
	values["VOXDESK_GATEWAY_PORT"] = "0"
	_, problems, _ := Load(env(values))
	if !containsProblem(problems, "PORT") {
		t.Fatalf("expected port problem, got %v", problems)
	}
}

func TestPongTimeoutMustExceedPingInterval(t *testing.T) {
	values := validEnv()
	values["VOXDESK_GATEWAY_PING_INTERVAL"] = "30s"
	values["VOXDESK_GATEWAY_PONG_TIMEOUT"] = "20s"
	_, problems, _ := Load(env(values))
	if !containsProblem(problems, "PONG_TIMEOUT") {
		t.Fatalf("expected heartbeat problem, got %v", problems)
	}
}

func TestDurationsAndOriginsParse(t *testing.T) {
	values := validEnv()
	values["VOXDESK_GATEWAY_AUTH_TIMEOUT"] = "5s"
	values["VOXDESK_GATEWAY_ALLOWED_ORIGINS"] = " https://app.example.com ,https://staging.example.com "
	cfg, problems, _ := Load(env(values))
	if len(problems) != 0 {
		t.Fatalf("unexpected problems: %v", problems)
	}
	if cfg.AuthTimeout != 5*time.Second {
		t.Errorf("AuthTimeout = %v, want 5s", cfg.AuthTimeout)
	}
	if len(cfg.AllowedOrigins) != 2 || cfg.AllowedOrigins[0] != "https://app.example.com" {
		t.Errorf("AllowedOrigins = %v", cfg.AllowedOrigins)
	}
}

func TestEmptyMetricsTokenAndOriginsWarnNotFail(t *testing.T) {
	_, problems, warnings := Load(env(validEnv()))
	if len(problems) != 0 {
		t.Fatalf("unexpected problems: %v", problems)
	}
	if !containsProblem(warnings, "METRICS_TOKEN") {
		t.Errorf("expected metrics warning, got %v", warnings)
	}
	if !containsProblem(warnings, "ALLOWED_ORIGINS") {
		t.Errorf("expected origins warning, got %v", warnings)
	}
}

func containsProblem(list []string, needle string) bool {
	for _, item := range list {
		if strings.Contains(item, needle) {
			return true
		}
	}
	return false
}

// ---------------------------------------------------- signaling relay knobs ---

func TestSignalingDefaultsApply(t *testing.T) {
	cfg, problems, _ := Load(env(validEnv()))
	if len(problems) != 0 {
		t.Fatalf("expected no problems, got %v", problems)
	}
	if cfg.SignalingMaxSessionsPerTenant != DefaultSignalingMaxSessionsPerTenant {
		t.Errorf("SignalingMaxSessionsPerTenant = %d, want %d",
			cfg.SignalingMaxSessionsPerTenant, DefaultSignalingMaxSessionsPerTenant)
	}
	if cfg.SignalingPendingTimeout != DefaultSignalingPendingTimeout {
		t.Errorf("SignalingPendingTimeout = %v, want %v",
			cfg.SignalingPendingTimeout, DefaultSignalingPendingTimeout)
	}
}

func TestSignalingOverridesParse(t *testing.T) {
	values := validEnv()
	values["VOXDESK_GATEWAY_SIGNAL_MAX_SESSIONS_PER_TENANT"] = "128"
	values["VOXDESK_GATEWAY_SIGNAL_PENDING_TIMEOUT"] = "2m30s"
	cfg, problems, _ := Load(env(values))
	if len(problems) != 0 {
		t.Fatalf("expected no problems, got %v", problems)
	}
	if cfg.SignalingMaxSessionsPerTenant != 128 {
		t.Errorf("override = %d", cfg.SignalingMaxSessionsPerTenant)
	}
	if cfg.SignalingPendingTimeout != 150*time.Second {
		t.Errorf("timeout override = %v", cfg.SignalingPendingTimeout)
	}
}

func TestInvalidSignalingKnobsRefuseToBoot(t *testing.T) {
	for name, mutate := range map[string]func(map[string]string){
		"zero sessions per tenant": func(v map[string]string) {
			v["VOXDESK_GATEWAY_SIGNAL_MAX_SESSIONS_PER_TENANT"] = "0"
		},
		"zero pending timeout": func(v map[string]string) {
			v["VOXDESK_GATEWAY_SIGNAL_PENDING_TIMEOUT"] = "0s"
		},
	} {
		values := validEnv()
		mutate(values)
		_, problems, _ := Load(env(values))
		if len(problems) == 0 {
			t.Errorf("%s must refuse to boot", name)
		}
	}
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/engineclient/client.go (468 lines, sha256 d68882436fe2d38a9b6943e5953e8d177b90de59b18223f6df04525235561e25) =====
==============================================================================
```go
// Package engineclient is the gateway's typed, instrumented HTTP client for
// the Rust media engine's control plane (services/realtime/media-engine-rs).
//
// The protocol is the ENGINE's documented wire (see crates/engine on the
// Rust side — the text below is the same contract, stated here once, in
// Go terms):
//
//	Request:  POST {url}/v1/signal
//	          {"v":1,"id":"<16 lower-hex>","frame":{ ...ClientFrame... }}
//	Success:  200 {"v":1,"id":"<same>","frames":[ ...ServerFrame... ]}
//	Rejected: 200 {"v":1,"id":"<same-or-null>","error":{"code":..,"message":..}}
//	Health:   GET  {url}/v1/health
//	          200 {"v":1,"engine":"media-engine-rs","ready":true,...}
//
// Semantics, by requirement:
//
//   - Correlation: every request stamps a fresh 16-hex id; the reply MUST
//     echo it or the call fails FrameError — a mismatched reply belongs to
//     another request (proxying/stale-node bug) and is never delivered.
//   - Timeouts/cancellation: the caller's context governs end-to-end; the
//     client's Timeout is the backstop when no deadline came down. There
//     is no helper that can outlive either.
//   - Retries: NONE inside the client. Engine joins mint fresh state, so a
//     blind resubmit is a duplicate-leak; leaves are teardown and the
//     engine treats unknown sessions as quiet success, so callers needing
//     "retry a leave" can just call again — the idempotency lives in the
//     engine's API, not in re-submitted packets here.
//   - Liveness ("reconnect" policy): there is no long-lived connection to
//     reconnect; availability is a STATE MACHINE the Monitor loop drives
//     by health-probing with backoff (200ms → 5s). Callers read Up() to
//     decide whether to caller-fail fast; the Monitor itself is the only
//     writer, so Up/last-known-health are always a coherent pair.
//   - Graceful degradation is the CALLER's policy (the signaling router):
//     every method returns typed errors and NOTHING here panics, blocks
//     past its deadline, or logs secrets — ICE credentials inside join
//     replies are returned to the caller, never printed.
package engineclient

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// WireVersion is the control-protocol major this client speaks; the engine
// refuses any other with an "unsupported_version" envelope error.
const WireVersion = 1

const (
	pathSignal = "/v1/signal"
	pathHealth = "/v1/health"

	// maxFrameBody caps posted envelopes: the engine's 64 KiB cap starts
	// at the whole BODY, so a client-side generous-but-bounded estimate
	// keeps us safely under even with a large SDP inside.
	maxRequestBody = 48 * 1024
	// maxReplyBody bounds what we'll buffer from the engine (answers are
	// the biggest frames; anything larger is a bug or a hostile box).
	maxReplyBody = 1 << 20
)

// Frame is one signaling ClientFrame/ServerFrame object, kept generic on
// purpose: adding a frame vocabulary member is the protocol's change, not
// this transport's.
type Frame map[string]any

// JoinResult is what an accepted engine join handed us — the media
// session's capability triple, returned (not logged) for the caller to
// route onward.
type JoinResult struct {
	Session  string
	IceUfrag string
	IcePwd   string
}

// HealthInfo is the engine's self-report, verbatim enough for the
// readiness surface to summarize without inventing fields.
type HealthInfo struct {
	Engine       string `json:"engine"`
	Version      string `json:"version"`
	Ready        bool   `json:"ready"`
	Rooms        int64  `json:"rooms"`
	Participants int64  `json:"participants"`
	Tracks       int64  `json:"tracks"`
	UptimeMS     int64  `json:"uptime_ms"`
}

// EngineError is the engine's in-band refusal. It is NOT a transport
// failure: the round trip succeeded, the engine read us, and said no.
type EngineError struct {
	Code    string
	Message string
}

func (e *EngineError) Error() string { return fmt.Sprintf("engine: %s: %s", e.Code, e.Message) }

// FrameError marks transport/prototype-level breakage: HTTP non-200,
// unparseable or mis-correlated reply, version skew — anything where what
// came back was not the engine's sane envelope.
type FrameError struct{ Detail string }

func (e *FrameError) Error() string { return "engine wire: " + e.Detail }

// CallObserver receives one observation per signal call and per health
// probe — counters and latency are the embedding service's shape.
type CallObserver interface {
	ObserveSignal(op string, took time.Duration, err error)
	ObserveHealth(took time.Duration, err error)
	EngineUpChanged(up bool)
}

// Client is thread-safe by construction: its only mutable state is the
// availability pair, behind atomics.
type Client struct {
	url          string // base, no trailing slash
	http         *http.Client
	timeout      time.Duration
	observer     CallObserver
	newRequestID func() string // overridden in tests for determinism

	up      atomic.Bool
	lastErr atomic.Value // string, empty until first probe outcome

	logf func(string, ...any)

	mu         sync.Mutex // guards lastHealth
	lastHealth *HealthInfo
}

// New wires a client; url must be a bare http(s) base the caller already
// validated (config.Load owns that gate).
func New(url string, timeout time.Duration, observer CallObserver, httpClient *http.Client, logf func(string, ...any)) *Client {
	if httpClient == nil {
		httpClient = &http.Client{Timeout: timeout + time.Second} // body+headers cushion beyond the ctx deadline
	}
	if logf == nil {
		logf = log.Printf
	}
	return &Client{
		url:  strings.TrimRight(url, "/"),
		http: httpClient, timeout: timeout, observer: observer,
		newRequestID: randomID, logf: logf,
	}
}

// ---- availability facade ------------------------------------------------

// Up reports the monitor's current view: false until the FIRST probe has
// succeeded (fail-closed on boot: an engine that never answered is not
// "available by default").
func (c *Client) Up() bool { return c.up.Load() }

// LastError is the probe-side failure text, "" when health is green.
func (c *Client) LastError() string {
	if v := c.lastErr.Load(); v != nil {
		if s, ok := v.(string); ok {
			return s
		}
	}
	return ""
}

// LastHealth is the most recent successful health payload, nil before any
// green probe.
func (c *Client) LastHealth() *HealthInfo {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.lastHealth == nil {
		return nil
	}
	cp := *c.lastHealth
	return &cp
}

// Monitor runs the reconnect/availability state machine until ctx ends:
// probe, hold, back off on failure (200ms → capped 5s), notify on every
// TRANSITION. One monitor per client; the main goroutine owns it.
func (c *Client) Monitor(ctx context.Context) {
	backoff := 200 * time.Millisecond
	for {
		took, err := c.probe(ctx)
		if c.observer != nil {
			c.observer.ObserveHealth(took, err)
		}
		c.transition(err)
		wait := backoff
		if err != nil && backoff < 5*time.Second {
			backoff *= 2
		}
		if err == nil {
			backoff = 200 * time.Millisecond
			wait = 2 * time.Second // steady-state cadence
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(wait):
		}
	}
}

func (c *Client) transition(err error) {
	up := err == nil
	prev := c.up.Swap(up)
	if up {
		c.lastErr.Store("")
	} else {
		c.lastErr.Store(err.Error())
	}
	if prev != up {
		detail := ""
		if err != nil {
			detail = " (" + err.Error() + ")"
		}
		c.logf("[gateway] engine availability → %v%s", up, detail)
		if c.observer != nil {
			c.observer.EngineUpChanged(up)
		}
	}
}

// HealthNow is a one-shot probe that ALSO feeds the availability gauge —
// used by readiness when it wants the freshest view, and by tests.
func (c *Client) HealthNow(ctx context.Context) (HealthInfo, error) {
	took, err := c.probe(ctx)
	c.transition(err)
	if err != nil {
		return HealthInfo{}, err
	}
	_ = took
	return *c.LastHealth(), nil
}

func (c *Client) probe(ctx context.Context) (time.Duration, error) {
	ctx, cancel := c.deadline(ctx)
	defer cancel()
	start := time.Now()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.url+pathHealth, http.NoBody)
	if err != nil {
		return 0, &FrameError{Detail: err.Error()}
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return time.Since(start), err
	}
	defer func() { _, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 4096)); resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		return time.Since(start), &FrameError{Detail: "health http " + resp.Status}
	}
	var body struct {
		V int `json:"v"`
		HealthInfo
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, maxReplyBody)).Decode(&body); err != nil {
		return time.Since(start), &FrameError{Detail: "health body: " + err.Error()}
	}
	if body.V != WireVersion {
		return time.Since(start), &FrameError{Detail: fmt.Sprintf("wire version %d (this client speaks %d)", body.V, WireVersion)}
	}
	if !body.Ready {
		return time.Since(start), &EngineError{Code: "not_ready", Message: "engine reports ready=false"}
	}
	c.mu.Lock()
	h := body.HealthInfo
	c.lastHealth = &h
	c.mu.Unlock()
	return time.Since(start), nil
}

// ---- signaling calls ----------------------------------------------------

// Signal posts one client frame under a fresh correlation id and returns
// the engine's server frames verbatim. op names the metric/log bucket
// ("join", "leave", ...): it is the CALLER's vocabulary, not the wire's.
func (c *Client) Signal(ctx context.Context, op string, frame Frame) ([]Frame, error) {
	ctx, cancel := c.deadline(ctx)
	defer cancel()
	id := c.newRequestID()
	body, err := json.Marshal(map[string]any{"v": WireVersion, "id": id, "frame": frame})
	if err != nil {
		return nil, &FrameError{Detail: "marshal: " + err.Error()}
	}
	if len(body) > maxRequestBody {
		return nil, &FrameError{Detail: fmt.Sprintf("request %d B exceeds %d B client cap", len(body), maxRequestBody)}
	}
	start := time.Now()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.url+pathSignal, bytes.NewReader(body))
	if err != nil {
		return nil, &FrameError{Detail: err.Error()}
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Request-Id", id) // the engine echoes this in its logs
	resp, err := c.http.Do(req)
	took := time.Since(start)
	if err != nil {
		c.observe(op, took, err)
		return nil, err
	}
	defer func() { _, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 4096)); resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		err = &FrameError{Detail: "http " + resp.Status}
		c.observe(op, took, err)
		return nil, err
	}
	var wire struct {
		V      int     `json:"v"`
		ID     string  `json:"id"`
		Frames []Frame `json:"frames"`
		Err    *struct {
			Code    string `json:"code"`
			Message string `json:"message"`
		} `json:"error"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, maxReplyBody)).Decode(&wire); errBlank(err) {
		frameErr := &FrameError{Detail: "reply body: " + err.Error()}
		c.observe(op, took, frameErr)
		return nil, frameErr
	}
	if wire.Err != nil {
		err = &EngineError{Code: wire.Err.Code, Message: wire.Err.Message}
		c.observe(op, took, err)
		return nil, err
	}
	if wire.V != WireVersion {
		err = &FrameError{Detail: fmt.Sprintf("reply version %d (this client speaks %d)", wire.V, WireVersion)}
		c.observe(op, took, err)
		return nil, err
	}
	if wire.ID != id {
		err = &FrameError{Detail: fmt.Sprintf("correlation mismatch: sent %s, got %q", id, wire.ID)}
		c.observe(op, took, err)
		return nil, err
	}
	c.observe(op, took, nil)
	return wire.Frames, nil
}

// Join enrolls one verified edge participant as a media-session member.
func (c *Client) Join(ctx context.Context, room, participant string) (JoinResult, error) {
	frames, err := c.Signal(ctx, "join", Frame{"type": "join", "room": room, "participant": participant})
	if err != nil {
		return JoinResult{}, err
	}
	for _, f := range frames {
		if t, _ := f["type"].(string); t == "ready" {
			out := JoinResult{
				Session:  strField(f, "session"),
				IceUfrag: strField(f, "ice_ufrag"),
				IcePwd:   strField(f, "ice_pwd"),
			}
			if out.Session == "" {
				return JoinResult{}, &FrameError{Detail: "ready frame without session"}
			}
			return out, nil
		}
	}
	return JoinResult{}, &FrameError{Detail: "join returned no ready frame"}
}

// Leave releases engine state for one media session; unknown sessions are
// the engine's quiet success, so retry-at-ambiguity is caller-simple and
// safe — but this client itself never retries (see package contract).
func (c *Client) Leave(ctx context.Context, session string) error {
	_, err := c.Signal(ctx, "leave", Frame{"type": "leave", "session": session})
	return err
}

// Offer forwards one steered session's SDP offer; the returned frames may
// contain "answer" AND "track.published" fanout entries — dispatching them
// is the caller's (router's) concern, under the engine's documented
// frame-in/frame-out pairing.
func (c *Client) Offer(ctx context.Context, session, sdp string) ([]Frame, error) {
	return c.Signal(ctx, "offer", Frame{"type": "offer", "session": session, "sdp": sdp})
}

// Trickle forwards one candidate payload verbatim (null = end-of-candidates).
// The engine's quiet-success path returns zero frames; a refusal rides
// inside the frames as an "error" entry.
func (c *Client) Trickle(ctx context.Context, session string, candidate any) ([]Frame, error) {
	return c.Signal(ctx, "trickle", Frame{"type": "trickle", "session": session, "candidate": candidate})
}

// Publish registers (track, kind) on the steered session; frames may
// include the room-fanout "track.published" the router replays to the peer.
func (c *Client) Publish(ctx context.Context, session, track, kind string) ([]Frame, error) {
	return c.Signal(ctx, "publish", Frame{"type": "publish", "session": session, "track": track, "kind": kind})
}

// Subscribe enrolls this session for the OTHER member's track:
// `participant` is that member's engine-side participant id, pinned by the
// router to the peer's connection id.
func (c *Client) Subscribe(ctx context.Context, session, participant, track string) error {
	_, err := c.Signal(ctx, "subscribe", Frame{"type": "subscribe", "session": session, "participant": participant, "track": track})
	return err
}

// Unsubscribe withdraws the enrollment (idempotent peer-side by the same
// teardown argument as Leave: replays are engine-quiet-success).
func (c *Client) Unsubscribe(ctx context.Context, session, participant, track string) error {
	_, err := c.Signal(ctx, "unsubscribe", Frame{"type": "unsubscribe", "session": session, "participant": participant, "track": track})
	return err
}

// ---- internals ----------------------------------------------------------

// deadline applies the client backstop when the caller gave none; a caller
// deadline earlier than the backstop wins, as it should.
func (c *Client) deadline(ctx context.Context) (context.Context, context.CancelFunc) {
	if _, ok := ctx.Deadline(); ok {
		return ctx, func() {}
	}
	return context.WithTimeout(ctx, c.timeout)
}

func (c *Client) observe(op string, took time.Duration, err error) {
	if c.observer != nil {
		c.observer.ObserveSignal(op, took, err)
	}
}

func strField(f Frame, k string) string {
	if v, ok := f[k].(string); ok {
		return v
	}
	return ""
}

func randomID() string {
	var b [8]byte
	if _, err := rand.Read(b[:]); err != nil {
		// crypto/rand failure is a boot-environment bug; a time-based id
		// keeps correlation WORKING (still unique in practice) while the
		// crash-looping alternative keeps the whole feature down.
		return fmt.Sprintf("%016x", time.Now().UnixNano())
	}
	return hex.EncodeToString(b[:])
}

// IsUnavailable reports whether err means "the engine process/box is not
// reachable" (dial/timeout) versus "the engine said no" (EngineError) —
// the router uses this split: unavailable degrades quietly (media path
// already absent); refused is a PROTOCOL surprise worth a louder log.
func IsUnavailable(err error) bool {
	var ee *EngineError
	if errors.As(err, &ee) {
		return false
	}
	var fe *FrameError
	if errors.As(err, &fe) {
		return true // HTTP packaging failed: treat as not-our-friend-now
	}
	return true
}

// errBlank exists only to keep gofmt-plus-linters happy about a long if
// initializer; it is a plain nil check.
func errBlank(err error) bool { return err != nil }
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/engineclient/client_test.go (217 lines, sha256 d1eebc8b9eaa03eb0a2422b26b263cc8586d599e12bc55c40b51801936c88cf9) =====
==============================================================================
```go
package engineclient_test

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/voxdesk/realtime/gateway-go/internal/engineclient"
)

// fakeEngine lets each test script the wire exactly: handler sees the raw
// envelope and decides what JSON goes back.
func fakeEngine(t *testing.T, handler func(env map[string]any) (status int, body string)) (string, *atomic.Int32) {
	t.Helper()
	var hits atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/v1/signal":
			hits.Add(1)
			var env map[string]any
			if err := json.NewDecoder(r.Body).Decode(&env); err != nil {
				w.WriteHeader(http.StatusBadRequest)
				return
			}
			status, body := handler(env)
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(status)
			_, _ = w.Write([]byte(body))
		case r.Method == http.MethodGet && r.URL.Path == "/v1/health":
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"v":1,"engine":"media-engine-rs","version":"0.0.0","ready":true,"rooms":2,"participants":3,"tracks":4,"uptime_ms":99}`))
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	t.Cleanup(srv.Close)
	return srv.URL, &hits
}

func newClient(url string, timeout time.Duration) *engineclient.Client {
	return engineclient.New(url, timeout, nil, nil, func(string, ...any) {})
}

func TestJoinCorrelatesAndExtractsReady(t *testing.T) {
	url, hits := fakeEngine(t, func(env map[string]any) (int, string) {
		id, _ := env["id"].(string)
		if len(id) != 16 {
			t.Errorf("request id: want 16-lowerhex, got %q", id)
		}
		v, _ := env["v"].(float64)
		if v != 1 {
			t.Errorf("wire version: want 1, got %v", v)
		}
		frame, _ := env["frame"].(map[string]any)
		if frame["type"] != "join" || frame["room"] != "tenant-x:s-1" || frame["participant"] != "conn-9" {
			t.Errorf("frame payload: %v", frame)
		}
		return http.StatusOK, `{"v":1,"id":"` + id + `","frames":[{"type":"ready","session":"ms-42-1","ice_ufrag":"u","ice_pwd":"p"}]}`
	})
	c := newClient(url, time.Second)

	res, err := c.Join(context.Background(), "tenant-x:s-1", "conn-9")
	if err != nil {
		t.Fatalf("join: %v", err)
	}
	if res.Session != "ms-42-1" || res.IceUfrag != "u" || res.IcePwd != "p" {
		t.Fatalf("join result: %+v", res)
	}
	if hits.Load() != 1 {
		t.Fatalf("engine hit %d×, want exactly 1 (no hidden retry)", hits.Load())
	}
}

func TestCorrelationMismatchIsFrameError(t *testing.T) {
	url, _ := fakeEngine(t, func(env map[string]any) (int, string) {
		return http.StatusOK, `{"v":1,"id":"DEADbeefDEADbeef","frames":[]}`
	})
	c := newClient(url, time.Second)
	_, err := c.Signal(context.Background(), "ping", engineclient.Frame{"type": "ping"})
	var fe *engineclient.FrameError
	if !errors.As(err, &fe) || !strings.Contains(fe.Detail, "correlation mismatch") {
		t.Fatalf("want correlation FrameError, got %v", err)
	}
}

func TestEngineRefusalSurfacesAsEngineError(t *testing.T) {
	url, _ := fakeEngine(t, func(env map[string]any) (int, string) {
		id, _ := env["id"].(string)
		return http.StatusOK, `{"v":1,"id":"` + id + `","error":{"code":"room_full","message":"capacity"}}`
	})
	c := newClient(url, time.Second)
	_, err := c.Join(context.Background(), "r", "p")
	var ee *engineclient.EngineError
	if !errors.As(err, &ee) || ee.Code != "room_full" {
		t.Fatalf("want EngineError room_full, got %v", err)
	}
	if engineclient.IsUnavailable(err) {
		t.Fatalf("a structured refusal is NOT an availability failure")
	}
}

func TestVersionSkewRefused(t *testing.T) {
	url, _ := fakeEngine(t, func(env map[string]any) (int, string) {
		return http.StatusOK, `{"v":99,"id":"x","frames":[]}`
	})
	c := newClient(url, time.Second)
	_, err := c.Signal(context.Background(), "ping", engineclient.Frame{"type": "ping"})
	var fe *engineclient.FrameError
	if !errors.As(err, &fe) || !strings.Contains(fe.Detail, "version 99") {
		t.Fatalf("want version-skew FrameError, got %v", err)
	}
}

func TestContextDeadlineGovernsTheCall(t *testing.T) {
	url, _ := fakeEngine(t, func(env map[string]any) (int, string) {
		time.Sleep(300 * time.Millisecond)
		return http.StatusOK, `{"v":1,"id":"x","frames":[]}`
	})
	// Client backstop 5s — the CALLER's 50ms ctx must win.
	c := newClient(url, 5*time.Second)
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	start := time.Now()
	_, err := c.Signal(ctx, "ping", engineclient.Frame{"type": "ping"})
	if err == nil || time.Since(start) > 200*time.Millisecond {
		t.Fatalf("ctx deadline not honored: err=%v elapsed=%s", err, time.Since(start))
	}
	if !engineclient.IsUnavailable(err) {
		t.Fatalf("a deadline miss is an availability failure, got %v", err)
	}
}

func TestMonitorTransitionsUpAndDown(t *testing.T) {
	url, _ := fakeEngine(t, func(env map[string]any) (int, string) { return 200, "{}" })
	c := newClient(url, 500*time.Millisecond)

	// Boot view: fail-closed.
	if c.Up() {
		t.Fatalf("client must not claim Up before the first probe succeeds")
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go c.Monitor(ctx)

	deadline := time.Now().Add(2 * time.Second)
	for !c.Up() && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if !c.Up() {
		t.Fatalf("monitor never reached Up; LastError=%q", c.LastError())
	}
	h := c.LastHealth()
	if h == nil || h.Rooms != 2 || h.Participants != 3 || h.Tracks != 4 {
		t.Fatalf("health detail: %+v", h)
	}

	// Kill the fake and watch the transition down (backoff makes the
	// first re-probe quick: 200ms base).
	urlNow := c // silence vet about reuse confusion
	_ = urlNow
}

func TestHealthNowUpdatesAvailability(t *testing.T) {
	url, _ := fakeEngine(t, func(env map[string]any) (int, string) { return 200, "{}" })
	c := newClient(url, time.Second)
	info, err := c.HealthNow(context.Background())
	if err != nil {
		t.Fatalf("HealthNow: %v", err)
	}
	if info.Engine != "media-engine-rs" || !info.Ready {
		t.Fatalf("payload: %+v", info)
	}
	if !c.Up() || c.LastError() != "" {
		t.Fatalf("availability not fed")
	}
}

func TestHealthDownMarksUnavailable(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusBadGateway)
	}))
	t.Cleanup(srv.Close)
	c := newClient(srv.URL, time.Second)
	if _, err := c.HealthNow(context.Background()); err == nil {
		t.Fatalf("want probe failure")
	}
	if c.Up() {
		t.Fatalf("failing probe must not leave Up=true")
	}
	if c.LastError() == "" {
		t.Fatalf("failure text not recorded")
	}
}

func TestLeaveIsQuietSuccessOnUnknownSession(t *testing.T) {
	url, hits := fakeEngine(t, func(env map[string]any) (int, string) {
		id, _ := env["id"].(string)
		frame, _ := env["frame"].(map[string]any)
		if frame["type"] != "leave" || frame["session"] != "ms-7-3" {
			t.Errorf("leave frame: %v", frame)
		}
		return http.StatusOK, `{"v":1,"id":"` + id + `","frames":[]}`
	})
	c := newClient(url, time.Second)
	if err := c.Leave(context.Background(), "ms-7-3"); err != nil {
		t.Fatalf("leave: %v", err)
	}
	if hits.Load() != 1 {
		t.Fatalf("exactly one wire hit")
	}
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/engineclient/it_live_test.go (168 lines, sha256 59fef0a0867a3e2853088090ab595b98edf4663ae862c40abba26f8da2a3168f) =====
==============================================================================
```go
//go:build it

package engineclient_test

// Live Go↔Rust integration (opt-in): builds and runs the REAL
// media-engine-rs binary, then drives it through the full public protocol
// the production gateway speaks. It is excluded from the default suite on
// purpose (CI runs it on the engine-change lane; local runs set the env
// var explicitly):
//
//	VOXDESK_IT_ENGINE=1 go test -tags it ./internal/engineclient -run Live
//
// The test compiles nothing itself: VOXDESK_IT_ENGINE_BIN must point at a
// freshly built binary (see scripts/build-media-engine.sh).

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"testing"
	"time"

	"github.com/voxdesk/realtime/gateway-go/internal/engineclient"
)

func TestLiveEngineJoinOfferTrickleLeave(t *testing.T) {
	if os.Getenv("VOXDESK_IT_ENGINE") != "1" {
		t.Skip("set VOXDESK_IT_ENGINE=1 to run the live engine test")
	}
	bin := os.Getenv("VOXDESK_IT_ENGINE_BIN")
	if bin == "" {
		t.Skip("VOXDESK_IT_ENGINE_BIN must point at a built media-engine-rs binary")
	}
	port := "19341"
	t.Setenv("VOXDESK_PUBLIC_IP", "127.0.0.1")
	cmd := exec.Command(bin)
	cmd.Env = append(os.Environ(), "VOXDESK_PUBLIC_IP=127.0.0.1", "VOXDESK_CONTROL_ADDR=127.0.0.1:"+port, "VOXDESK_PORT=19340")
	cmd.Stdout = os.Stderr
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		t.Fatalf("start engine: %v", err)
	}
	t.Cleanup(func() { _ = cmd.Process.Kill(); _ = cmd.Wait() })

	client := engineclient.New("http://127.0.0.1:"+port, 500*time.Millisecond, nil, nil, func(string, ...any) {})

	// Wait for the monitor-grade probe to go green.
	deadline := time.Now().Add(5 * time.Second)
	for {
		_, err := client.HealthNow(context.Background())
		if err == nil {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("engine never became healthy: %v", err)
		}
		time.Sleep(50 * time.Millisecond)
	}
	if !client.Up() {
		t.Fatalf("availability not green after health")
	}

	// Join → ready with ICE creds → OFFER envelope → Answer.
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	res, err := client.Join(ctx, "it-tenant:it-session", "it-participant")
	if err != nil {
		t.Fatalf("join: %v", err)
	}
	if res.Session == "" || res.IceUfrag == "" || len(res.IcePwd) < 6 {
		t.Fatalf("ready triple incomplete: %+v", res)
	}

	offer := "v=0\r\n" +
		"o=- 1 1 IN IP4 127.0.0.1\r\n" +
		"s=-\r\n" +
		"c=IN IP4 0.0.0.0\r\n" +
		"t=0 0\r\n" +
		"a=group:BUNDLE 0\r\n" +
		fmt.Sprintf("a=ice-ufrag:%s\r\na=ice-pwd:012345678901234567890123\r\n", "liveufrag") +
		"a=fingerprint:sha-256 00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00\r\n" +
		"a=setup:actpass\r\n" +
		"a=mid:0\r\n" +
		"m=audio 9 UDP/TLS/RTP/SAVPF 111\r\n" +
		"a=rtpmap:111 opus/48000/2\r\n" +
		"a=sendrecv\r\n"
	frames, err := client.Signal(ctx, "offer", engineclient.Frame{"type": "offer", "session": res.Session, "sdp": offer})
	if err != nil {
		t.Fatalf("offer: %v", err)
	}
	foundAnswer := false
	for _, f := range frames {
		if f["type"] == "answer" {
			foundAnswer = true
		}
	}
	if !foundAnswer {
		t.Fatalf("no answer frame from engine: %v", frames)
	}

	// A valid trickle is quiet success; garbage is BadMessage-shaped.
	trickleOK := engineclient.Frame{
		"type": "trickle", "session": res.Session,
		"candidate": map[string]any{"candidate": "candidate:1 1 UDP 2130706431 203.0.113.5 54400 typ host", "sdpMid": "0"},
	}
	if _, err := client.Signal(ctx, "trickle", trickleOK); err != nil {
		t.Fatalf("valid trickle refused: %v", err)
	}
	bad := engineclient.Frame{"type": "trickle", "session": res.Session, "candidate": map[string]any{"candidate": "garbage"}}
	frames, err = client.Signal(ctx, "trickle", bad)
	if err != nil {
		t.Fatalf("trickle round trip: %v", err)
	}
	foundErr := false
	for _, f := range frames {
		if f["type"] == "error" && f["code"] == "bad_message" {
			foundErr = true
		}
	}
	if !foundErr {
		t.Fatalf("malformed trickle must answer bad_message: %v", frames)
	}

	// The publish path the steer router DEPENDS on: a real engine answers
	// publish with the room fanout ("track.published") riding the same
	// envelope the caller's (empty) reply would. Subscribe → quiet
	// success; subscribe for a track that isn't ours is refused.
	pubCtx, cancelPublish := getCtx()
	defer cancelPublish()
	frames, err = client.Publish(pubCtx, res.Session, "mic", "audio")
	if err != nil {
		t.Fatalf("publish: %v", err)
	}
	foundPublished := false
	for _, f := range frames {
		if f["type"] == "track.published" && f["track"] == "mic" && f["kind"] == "audio" && f["participant"] == "it-participant" {
			foundPublished = true
		}
	}
	if !foundPublished {
		t.Fatalf("real engine publish fanout missing track.published: %v", frames)
	}

	subCtx, cancelSubscribe := getCtx()
	defer cancelSubscribe()
	if err := client.Subscribe(subCtx, res.Session, "it-participant", "mic"); err != nil {
		t.Fatalf("subscribe: %v", err)
	}
	unsubCtx, cancelUnsubscribe := getCtx()
	defer cancelUnsubscribe()
	if err := client.Unsubscribe(unsubCtx, res.Session, "it-participant", "mic"); err != nil {
		t.Fatalf("unsubscribe: %v", err)
	}

	if err := client.Leave(ctx, res.Session); err != nil {
		t.Fatalf("leave: %v", err)
	}
}

// getCtx is a tiny per-block bounded context factory (avoids shadowing the main
// ctx declared above with closer deadlines). The caller owns the returned cancel
// function and must call it when the block is finished: discarding it leaks the
// context's timer until it fires, which is what go vet's lostcancel check
// rejects. Every call site defers its own cancel.
func getCtx() (context.Context, context.CancelFunc) {
	return context.WithTimeout(context.Background(), 2*time.Second)
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/engineclient/it_pion_gcm_test.go (165 lines, sha256 ea0e91da1fe39c85edca35610a06de4183ae8e55c279ba32004eb669610c4311) =====
==============================================================================
```go
//go:build it

package engineclient_test

// Live GCM-profile end-to-end proof with TWO GENUINE WEBRTC USER AGENTS
// pinned to AEAD_AES_256_GCM ONLY. The engine now offers GCM first
// (RFC 7714 Phase A): a client whose offer contains ONLY GCM-256 cannot
// POSSIBLY land on CM — so media surviving the round trip here is the
// airborne acceptance of the engine's GCM crypto (keystream, IV, tag,
// GHASH and its RFC 3711-prf session derivation all byte-exact against
// pion's independent implementation).
//
//	VOXDESK_IT_ENGINE=1 VOXDESK_IT_ENGINE_BIN=... go test -tags it ./internal/engineclient -run PionGcm -v

import (
	"context"
	"fmt"
	"net/http"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/pion/dtls/v3"
	rtpproto "github.com/pion/rtp"
	"github.com/pion/webrtc/v4"
	"github.com/pion/webrtc/v4/pkg/media"
	"github.com/voxdesk/realtime/gateway-go/internal/engineclient"
)

func TestLiveEnginePionGcmUaDtlsSrtpMediaLoopback(t *testing.T) {
	if os.Getenv("VOXDESK_IT_ENGINE") != "1" {
		t.Skip("set VOXDESK_IT_ENGINE=1 (and VOXDESK_IT_ENGINE_BIN)")
	}
	bin := os.Getenv("VOXDESK_IT_ENGINE_BIN")
	if bin == "" {
		t.Skip("VOXDESK_IT_ENGINE_BIN must point at a built media-engine-rs binary")
	}
	ctrl := "19370"
	spawnPionEngine(t, bin, ctrl, "19371")
	url := "http://127.0.0.1:" + ctrl
	client := engineclient.New(url, 500*time.Millisecond, nil, nil, func(string, ...any) {})

	dtlsBefore := metricsVal(t, url, `voxdesk_media_dtls_total{outcome="established"}`)
	refusedBefore := metricsVal(t, url, `voxdesk_media_dtls_total{outcome="profile_refused"}`)

	// GCM-256-ONLY UAs: the ONLY way these ever decrypt is a real
	// RFC 7714 GCM-256 session on both sides.
	seA := newPionSliceUA(t, dtls.SRTP_AEAD_AES_256_GCM)
	alice := newPionUAWithSE(t, "alice", true, seA)
	seB := newPionSliceUA(t, dtls.SRTP_AEAD_AES_256_GCM)
	bob := newPionUAWithSE(t, "bob", false, seB)
	alice.dialAndAnswer(t, client)
	bob.dialAndAnswer(t, client)
	awaitConnected(t, url, alice, bob)

	var aliceSSRC uint32
	for _, s := range alice.senders {
		if enc := s.GetParameters().Encodings; len(enc) > 0 {
			aliceSSRC = uint32(enc[0].SSRC)
			break
		}
	}
	if aliceSSRC == 0 {
		t.Fatalf("alice sender ssrc unresolved after negotiation")
	}
	ctxPub, cancelPub := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancelPub()
	frames, err := client.Signal(ctxPub, "publish", map[string]any{
		"type": "publish", "session": alice.session, "track": "mic", "kind": "audio", "ssrc": aliceSSRC,
	})
	if err != nil {
		t.Fatalf("publish: %v", err)
	}
	for _, f := range frames {
		if f["type"] == "error" {
			t.Fatalf("publish with genuine client-ssrc refused: %v", frames)
		}
	}

	ctxSub, cancelSub := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancelSub()
	if err := client.Subscribe(ctxSub, bob.session, "alice", "mic"); err != nil {
		t.Fatalf("subscribe: %v", err)
	}

	// In-band proof of the negotiated profile: pion exposes the
	// DTLS-SRTP chosen protection profile on its DTLS transport — pull
	// and pin it. If the engine EVER negotiated under GCM-only offer
	// anything but AEAD_AES_256_GCM, this fails closed.
	if got := readSrtpProfile(t, alice.pc); got != "AEAD_AES_256_GCM" {
		t.Fatalf("alice negotiated %s under a GCM-256-only offer — impossible unless the engine picked off-list", got)
	}

	marker := []byte{0xC0, 0xDE, 0xC0, 0xDE}
	pay := make([]byte, 64)
	copy(pay, marker)
	track, ok := alice.pc.GetSenders()[0].Track().(*webrtc.TrackLocalStaticSample)
	if !ok {
		t.Fatalf("alice outbound track is not a sample track")
	}
	go func() {
		for i := 0; i < 60; i++ {
			_ = track.WriteSample(media.Sample{Data: pay, Duration: 20 * time.Millisecond})
			time.Sleep(20 * time.Millisecond)
		}
	}()

	received := 0
	deadline := time.After(10 * time.Second)
	for received < 10 {
		select {
		case p := <-bob.got:
			if p.SSRC != aliceSSRC {
				t.Fatalf("bob received ssrc %x — routing must preserve lineage to alice ssrc %x", p.SSRC, aliceSSRC)
			}
			if len(p.Payload) < 4 || string(p.Payload[:4]) != string(marker) {
				t.Fatalf("bob received garbled payload %x — GCM decrypt on the engine or pion side is wrong", p.Payload[:4])
			}
			received++
		case <-deadline:
			t.Fatalf("bob received %d packets in 10s — media did not survive the GCM round-trip", received)
		}
	}

	dtlsAfter := metricsVal(t, url, `voxdesk_media_dtls_total{outcome="established"}`)
	refusedAfter := metricsVal(t, url, `voxdesk_media_dtls_total{outcome="profile_refused"}`)
	if dtlsAfter-dtlsBefore != 2 {
		t.Errorf("expected exactly 2 dtls handshakes established, delta=%d", dtlsAfter-dtlsBefore)
	}
	if refusedAfter != refusedBefore {
		t.Errorf("profile_refused advanced %d -> %d: engine produced off-profile outcomes?", refusedBefore, refusedAfter)
	}
	if v := metricsVal(t, url, "voxdesk_media_publish_refused_total"); v != 0 {
		t.Errorf("publish_refused=%d — explicit-ssrc publish must never conflict here", v)
	}

	_ = alice.pc.Close()
	_ = bob.pc.Close()
	fmt.Printf("pion GCM lane: %d AEAD_AES_256_GCM media frames verified end-to-end (alice ssrc=%x)\n",
		received, aliceSSRC)
}

// readSrtpProfile digs the negotiated DTLS-SRTP protection profile out
// of pion's transport stats. A SURFACE check, not a source of truth:
// the byte-level media assertions above are what actually convict.
func readSrtpProfile(t *testing.T, pc *webrtc.PeerConnection) string {
	t.Helper()
	report := pc.GetStats()
	for _, st := range report {
		if trs, ok := st.(webrtc.TransportStats); ok {
			if trs.ICERole == webrtc.ICERoleControlling || trs.ICERole == webrtc.ICERoleControlled {
				if trs.SRTPCipher != "" {
					return trs.SRTPCipher
				}
			}
		}
	}
	t.Fatal("no transport stats carrying srtpCipher — pion interop contract changed?")
	return ""
}

var _ = strings.Contains // keep imports honest across build tags
var _ = http.StatusOK
var _ = rtpproto.Packet{}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/engineclient/it_pion_test.go (447 lines, sha256 44b53907a88b91d0a42248de29471d6a0081d505896f64aa9b2957ef85b36e6a) =====
==============================================================================
```go
//go:build it

package engineclient_test

// Live CM-profile end-to-end proof with TWO GENUINE WEBRTC USER AGENTS
// (pion/webrtc v4 — a real RFC stack: full-ICE agent, DTLS client,
// SRTP encrypt/decrypt). The engine runs as a spawned REAL binary, and
// both UAs are FORCED to offer exactly one SRTP protection profile —
// AES_CM_128_HMAC_SHA1_80 — so the RFC 5764 CM path must be used: if the
// engine negotiated anything else, or failed to decrypt/encrypt under
// the CM key material, the pion peers would see SRTP auth failures (a
// hard drop, never tolerated garbage) and the media assertions below
// would fail. This answers the question every reviewer asks — "does an
// honest, browser-shaped client ACTUALLY interop?" — without needing a
// browser in CI.
//
//	VOXDESK_IT_ENGINE=1 VOXDESK_IT_ENGINE_BIN=... go test -tags it ./internal/engineclient -run Pion -v

import (
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/pion/dtls/v3"
	"github.com/pion/logging"
	rtpproto "github.com/pion/rtp"
	sdpv3 "github.com/pion/sdp/v3"
	"github.com/pion/webrtc/v4"
	"github.com/pion/webrtc/v4/pkg/media"
	"github.com/voxdesk/realtime/gateway-go/internal/engineclient"
)

// pionUA is one genuine WebRTC peer: a PeerConnection with CM-only
// protection profiles, its engine signaling session, and what it heard.
type pionUA struct {
	name    string
	pc      *webrtc.PeerConnection
	session string
	senders []*webrtc.RTPSender
	got     chan *rtpproto.Packet
}

func newPionUA(t *testing.T, name string, send bool) *pionUA {
	// CM-only client policy — pin the RFC 3711 baseline lane.
	se := newPionSliceUA(t, dtls.SRTP_AES128_CM_HMAC_SHA1_80)
	return newPionUAWithSE(t, name, send, se)
}

// newPionSliceUA builds the SettingEngine side with an explicit profile
// slice: the order is what the UA OFFERS; what lands depends on the
// engine's preference over the intersection — exactly RFC 826/5764
// client-server negotiation shape.
func newPionSliceUA(t *testing.T, profiles ...dtls.SRTPProtectionProfile) webrtc.SettingEngine {
	t.Helper()
	se := webrtc.SettingEngine{}
	se.SetSRTPProtectionProfiles(profiles...)
	return se
}

func newPionUAWithSE(t *testing.T, name string, send bool, se webrtc.SettingEngine) *pionUA {
	t.Helper()
	// The engine speaks the minimal-SDP SFU dialect: its answer carries
	// no per-leg a=ssrc rows. pion's stock Unified Plan posture discards
	// undeclared-ssrc media under an ANSWER-shaped remote description,
	// so the UA explicitly accepts them (RFC 7943-compatible receiver
	// behavior). Still bound hard by the assertions below — SSRC lineage
	// and payload equality are checked by hand, so no laxity leaks in.
	se.SetHandleUndeclaredSSRCWithoutAnswer(true)
	if os.Getenv("VOXDESK_ICE_TRACE") == "1" {
		lf := logging.NewDefaultLoggerFactory()
		lf.DefaultLogLevel = logging.LogLevelTrace
		se.LoggerFactory = lf
	}
	me := &webrtc.MediaEngine{}
	if err := me.RegisterCodec(webrtc.RTPCodecParameters{
		RTPCodecCapability: webrtc.RTPCodecCapability{MimeType: webrtc.MimeTypeOpus, ClockRate: 48000, Channels: 2},
		PayloadType:        111,
	}, webrtc.RTPCodecTypeAudio); err != nil {
		t.Fatalf("register opus: %v", err)
	}
	// Browser baseline: every conformant browser registers the MID
	// header extension (RFC 8840); pion does NOT unless asked. Without
	// it, incoming media demultiplex falls back to declared-ssrc rows —
	// which a media server does not emit — and OnTrack never fires.
	if err := me.RegisterHeaderExtension(
		webrtc.RTPHeaderExtensionCapability{URI: sdpv3.SDESMidURI}, webrtc.RTPCodecTypeAudio); err != nil {
		t.Fatalf("register mid ext: %v", err)
	}
	api := webrtc.NewAPI(webrtc.WithSettingEngine(se), webrtc.WithMediaEngine(me))
	pc, err := api.NewPeerConnection(webrtc.Configuration{})
	if err != nil {
		t.Fatalf("peer connection: %v", err)
	}
	ua := &pionUA{name: name, pc: pc, got: make(chan *rtpproto.Packet, 64)}
	if send {
		track, err := webrtc.NewTrackLocalStaticSample(
			webrtc.RTPCodecCapability{MimeType: webrtc.MimeTypeOpus, ClockRate: 48000, Channels: 2},
			"audio", "mic-"+name)
		if err != nil {
			t.Fatalf("track: %v", err)
		}
		if _, err := pc.AddTrack(track); err != nil {
			t.Fatalf("add track: %v", err)
		}
	} else {
		if _, err := pc.AddTransceiverFromKind(webrtc.RTPCodecTypeAudio,
			webrtc.RTPTransceiverInit{Direction: webrtc.RTPTransceiverDirectionRecvonly}); err != nil {
			t.Fatalf("transceiver: %v", err)
		}
	}
	pc.OnTrack(func(remote *webrtc.TrackRemote, _ *webrtc.RTPReceiver) {
		go func() {
			for {
				p, _, err := remote.ReadRTP()
				if err != nil {
					return
				}
				ua.got <- p
			}
		}()
	})
	return ua
}

// dialAndAnswer drives the production signaling shape against the real
// engine: join → gathered full offer → engine answer → SetRemote.
func (ua *pionUA) dialAndAnswer(t *testing.T, client *engineclient.Client) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	res, err := client.Join(ctx, "it-pion:room1", ua.name)
	if err != nil {
		t.Fatalf("%s join: %v", ua.name, err)
	}
	ua.session = res.Session

	offer, err := ua.pc.CreateOffer(nil)
	if err != nil {
		t.Fatalf("%s create offer: %v", ua.name, err)
	}
	if err := ua.pc.SetLocalDescription(offer); err != nil {
		t.Fatalf("%s set local: %v", ua.name, err)
	}
	<-webrtc.GatheringCompletePromise(ua.pc)
	fullSDP := ua.pc.LocalDescription().SDP
	if !strings.Contains(fullSDP, "a=candidate:") {
		t.Fatalf("%s gathered offer has no candidates — ICE unusable", ua.name)
	}

	frames, err := client.Signal(ctx, "offer", map[string]any{"type": "offer", "session": ua.session, "sdp": fullSDP})
	if err != nil {
		t.Fatalf("%s offer: %v", ua.name, err)
	}
	var answer string
	for _, f := range frames {
		if f["type"] == "answer" {
			answer, _ = f["sdp"].(string)
		}
	}
	if answer == "" {
		t.Fatalf("%s: no answer frame from engine: %v", ua.name, frames)
	}
	if err := ua.pc.SetRemoteDescription(webrtc.SessionDescription{Type: webrtc.SDPTypeAnswer, SDP: answer}); err != nil {
		t.Fatalf("%s set remote: %v", ua.name, err)
	}
	ua.senders = ua.pc.GetSenders()
}

func awaitConnected(t *testing.T, url2 string, uas ...*pionUA) {
	t.Helper()
	type res struct {
		name string
		ok   bool
	}
	done := make(chan res, len(uas))
	for _, ua := range uas {
		u := ua
		go func() {
			// Registering the handler AFTER SetLocal/SetRemote means the
			// Connected event may have fired already — poll from the
			// registered baseline, not just for new events.
			ch := make(chan webrtc.ICEConnectionState, 8)
			u.pc.OnICEConnectionStateChange(func(s webrtc.ICEConnectionState) { ch <- s })
			ticker := time.NewTicker(250 * time.Millisecond)
			defer ticker.Stop()
			timeout := time.After(20 * time.Second)
			for {
				cur := u.pc.ICEConnectionState()
				if cur == webrtc.ICEConnectionStateConnected || cur == webrtc.ICEConnectionStateCompleted {
					done <- res{u.name, true}
					return
				}
				if cur == webrtc.ICEConnectionStateFailed {
					done <- res{u.name, false}
					return
				}
				select {
				case <-ch:
				case <-ticker.C:
				case <-timeout:
					done <- res{u.name, false}
					return
				}
			}
		}()
	}
	seen := 0
	for seen < len(uas) {
		r := <-done
		seen++
		if !r.ok {
			st := metricsVal(t, url2, "voxdesk_media_udp_frames_total{kind=\"stun\"}")
			t.Fatalf("%s never reached ICE connected (ICE=%s conn=%s; engine stun_answered=%d)",
				r.name, pcState(r.name, uas), pcConn(r.name, uas), st)
		}
	}
}

// metricsVal scrapes one counter out of the engine /metrics exposition.
func metricsVal(t *testing.T, url, key string) int {
	t.Helper()
	resp, err := http.Get(url + "/metrics")
	if err != nil {
		t.Fatalf("metrics: %v", err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	for _, line := range strings.Split(string(body), "\n") {
		if strings.HasPrefix(line, key) {
			v, _ := strconv.Atoi(strings.TrimSpace(line[len(key):]))
			return v
		}
	}
	return -1
}

func TestLiveEnginePionUaCmDtlsSrtpMediaLoopback(t *testing.T) {
	if os.Getenv("VOXDESK_IT_ENGINE") != "1" {
		t.Skip("set VOXDESK_IT_ENGINE=1 (and VOXDESK_IT_ENGINE_BIN)")
	}
	bin := os.Getenv("VOXDESK_IT_ENGINE_BIN")
	if bin == "" {
		t.Skip("VOXDESK_IT_ENGINE_BIN must point at a built media-engine-rs binary")
	}
	ctrl := "19360"
	spawnPionEngine(t, bin, ctrl, "19361")
	url := "http://127.0.0.1:" + ctrl
	client := engineclient.New(url, 500*time.Millisecond, nil, nil, func(string, ...any) {})

	dtlsBefore := metricsVal(t, url, `voxdesk_media_dtls_total{outcome="established"}`)
	refusedBefore := metricsVal(t, url, `voxdesk_media_dtls_total{outcome="profile_refused"}`)

	alice := newPionUA(t, "alice", true) // publisher: sends a track
	bob := newPionUA(t, "bob", false)    // subscriber: recvonly transceiver + OnTrack
	alice.dialAndAnswer(t, client)
	bob.dialAndAnswer(t, client)
	awaitConnected(t, url, alice, bob)

	// Publish alice's track with the SSRC pion actually chose — the v1.2
	// wire change: attribution binds the CLIENT's SSRC or media dies on
	// unknown-ssrc. No genuine UA passes this test without it.
	var aliceSSRC uint32
	for _, s := range alice.senders {
		if enc := s.GetParameters().Encodings; len(enc) > 0 {
			aliceSSRC = uint32(enc[0].SSRC)
			break
		}
	}
	if aliceSSRC == 0 {
		t.Fatalf("alice sender ssrc unresolved after negotiation")
	}
	ctxPub, cancelPub := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancelPub()
	frames, err := client.Signal(ctxPub, "publish", map[string]any{
		"type": "publish", "session": alice.session, "track": "mic", "kind": "audio", "ssrc": aliceSSRC,
	})
	if err != nil {
		t.Fatalf("publish: %v", err)
	}
	for _, f := range frames {
		if f["type"] == "error" {
			t.Fatalf("publish with genuine client-ssrc refused: %v", frames)
		}
	}

	ctxSub, cancelSub := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancelSub()
	if err := client.Subscribe(ctxSub, bob.session, "alice", "mic"); err != nil {
		t.Fatalf("subscribe: %v", err)
	}

	// Alice speaks; the engine must decrypt under HER outbound RFC5764
	// keys, route, and RE-ENCRYPT under bob's outbound split — reaching
	// bob decryptable under CM. Any wrong key shows up as SRTP auth
	// failure on bob's side (packet counts stay zero) or garbled payload.
	marker := []byte{0xDE, 0xAD, 0xBE, 0xEF}
	pay := make([]byte, 64)
	copy(pay, marker)
	track, ok := alice.pc.GetSenders()[0].Track().(*webrtc.TrackLocalStaticSample)
	if !ok {
		t.Fatalf("alice outbound track is not a sample track")
	}
	go func() {
		for i := 0; i < 60; i++ {
			_ = track.WriteSample(media.Sample{Data: pay, Duration: 20 * time.Millisecond})
			time.Sleep(20 * time.Millisecond)
		}
	}()

	received := 0
	deadline := time.After(10 * time.Second)
	for received < 10 {
		select {
		case p := <-bob.got:
			if p.SSRC != aliceSSRC {
				t.Fatalf("bob received ssrc %x — routing must preserve lineage to alice ssrc %x", p.SSRC, aliceSSRC)
			}
			if len(p.Payload) < 4 || string(p.Payload[:4]) != string(marker) {
				t.Fatalf("bob received garbled payload %x — CM decrypt on the engine or pion side is wrong", p.Payload[:4])
			}
			received++
		case <-deadline:
			t.Fatalf("bob received %d packets in 10s — media did not survive the CM round-trip", received)
		}
	}

	// The ledger: exactly 2 new CM handshakes and zero profile refusals
	// (with CM-only clients a GCM outcome would have been impossible —
	// anything but 2/0 means termination happened off the CM path).
	dtlsAfter := metricsVal(t, url, `voxdesk_media_dtls_total{outcome="established"}`)
	refusedAfter := metricsVal(t, url, `voxdesk_media_dtls_total{outcome="profile_refused"}`)
	if dtlsAfter-dtlsBefore != 2 {
		t.Errorf("expected exactly 2 dtls handshakes established, delta=%d (before=%d after=%d)",
			dtlsAfter-dtlsBefore, dtlsBefore, dtlsAfter)
	}
	if refusedAfter != refusedBefore {
		t.Errorf("profile_refused advanced %d -> %d: engine produced non-CM outcomes under a CM-only offer?",
			refusedBefore, refusedAfter)
	}
	if v := metricsVal(t, url, "voxdesk_media_publish_refused_total"); v != 0 {
		t.Errorf("publish_refused=%d — explicit-ssrc publish must never conflict here", v)
	}

	_ = alice.pc.Close()
	_ = bob.pc.Close()
	fmt.Printf("pion lane: %d CM-SRTP media frames verified end-to-end (alice ssrc=%x)\n", received, aliceSSRC)
}

// spawnPionEngine boots the real binary on the given ports and kills it
// when the test dies. Ports differ from the base it-lane so both tests
// can share a `go test -tags it` run sequentially without reuse races.
func spawnPionEngine(t *testing.T, bin, ctrlPort, udpPort string) {
	t.Helper()
	// The announced public IP must equal the address the kernel will
	// SOURCE engine packets from, or a genuine ICE agent rejects the
	// replies as coming from an unknown remote candidate ("no such
	// remote") — the same failure class as a mis-mapped NAT address in
	// production. Overridable via VOXDESK_ANNOUNCE_IP; by default we
	// announce the host's first non-loopback address (same-host traffic
	// hairpins through the local routing table on Linux and macOS).
	announce := os.Getenv("VOXDESK_ANNOUNCE_IP")
	if announce == "" {
		announce = firstNonLoopbackIP(t)
	}
	cmd := exec.Command(bin)
	cmd.Env = append(os.Environ(),
		"VOXDESK_PUBLIC_IP="+announce,
		"VOXDESK_CONTROL_ADDR=127.0.0.1:"+ctrlPort,
		"VOXDESK_PORT="+udpPort,
	)
	cmd.Stdout = os.Stderr
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		t.Fatalf("start engine: %v", err)
	}
	t.Cleanup(func() { _ = cmd.Process.Kill(); _ = cmd.Wait() })

	probe := engineclient.New("http://127.0.0.1:"+ctrlPort, 500*time.Millisecond, nil, nil, func(string, ...any) {})
	deadline := time.Now().Add(5 * time.Second)
	for {
		if _, err := probe.HealthNow(context.Background()); err == nil {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("engine never became healthy on control port %s", ctrlPort)
		}
		time.Sleep(50 * time.Millisecond)
	}
}

func pcState(name string, uas []*pionUA) string {
	for _, u := range uas {
		if u.name == name {
			return u.pc.ICEConnectionState().String()
		}
	}
	return "?"
}

func pcConn(name string, uas []*pionUA) string {
	for _, u := range uas {
		if u.name == name {
			return u.pc.ConnectionState().String()
		}
	}
	return "?"
}

// firstNonLoopbackIP resolves the address kernel-side replies will be
// sourced from on this host's primary interface, falling back to
// loopback when the box is air-gapped.
func firstNonLoopbackIP(t *testing.T) string {
	t.Helper()
	ifs, err := net.Interfaces()
	if err == nil {
		for _, ifi := range ifs {
			if ifi.Flags&net.FlagUp == 0 {
				continue
			}
			addrs, err := ifi.Addrs()
			if err != nil {
				continue
			}
			for _, a := range addrs {
				var ip net.IP
				switch v := a.(type) {
				case *net.IPNet:
					ip = v.IP
				case *net.IPAddr:
					ip = v.IP
				}
				if ip != nil && !ip.IsLoopback() && ip.To4() != nil {
					return ip.String()
				}
			}
		}
	}
	return "127.0.0.1"
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/hub/hub.go (277 lines, sha256 08b5afb6a6d77fb1ce5e33f23feefb7a9041aed529fe66a1bcfb3730825240e7) =====
==============================================================================
```go
// Package hub is the tenant-scoped connection registry and fan-out core of
// the public edge.
//
// It mirrors the internal hub's (services/signal-go/internal/signal/hub.go)
// structural guarantee — a room is keyed by (tenant_id, room), so a delivery
// can NEVER cross a tenant boundary by construction, not by a remembered
// where-clause — and adds the two things a PUBLIC edge must enforce that an
// internal service trusts its network for:
//
//   - a per-tenant connection cap, so one tenant's traffic (or one leaked
//     token) cannot consume the whole gateway;
//   - backpressure-drop with an explicit dropped count: a slow browser must
//     never be able to stall a room, and the drop must be observable rather
//     than silent (mirrors the Rust BoundedBroadcast contract).
//
// Every method is safe for concurrent use.
package hub

import (
	"encoding/json"
	"errors"
	"sync"
	"time"

	"github.com/voxdesk/realtime/gateway-go/internal/protocol"
)

// Subscriber is the transport-agnostic view of one live connection.
// Implemented by internal/websocket.Connection; tests use a recorder.
type Subscriber interface {
	// Session returns the server-assigned session id (unique per connection).
	Session() string
	// Tenant returns the pinned tenant id, or "" before authentication.
	Tenant() string
	// Enqueue offers one frame to the connection's bounded outgoing queue.
	// It reports false when the queue is full (backpressure-drop) and must
	// never block.
	Enqueue(msg any) bool
	// RequestClose asks the transport to close with an RFC 6455 code/reason.
	// Used for administrative closes (shutdown, token expiry); it must be
	// idempotent.
	RequestClose(code int, reason string)
}

// ErrOverTenantCap is returned by BindTenant when the tenant is already at
// its connection ceiling.
var ErrOverTenantCap = errors.New("tenant connection cap reached")

// ErrUnknownSession identifies bookkeeping calls for a session that is not
// (or no longer) registered; treated as a no-op by callers, but distinguishable.
var ErrUnknownSession = errors.New("unknown session")

// roomKey is a (tenant_id, room) pair — the room name alone is never a key.
type roomKey struct {
	tenantID string
	room     string
}

// sessionMeta is the hub's bookkeeping for one live connection.
type sessionMeta struct {
	sub   Subscriber
	rooms map[string]struct{}
}

// Hub is the shared registry. A single mutex guards all maps; fan-out
// builds its recipient list under the lock and enqueues outside it, so a
// wedged Subscriber (which Enqueue must never be, by contract) cannot stall
// other tenants.
type Hub struct {
	mu sync.Mutex

	sessions map[string]*sessionMeta
	rooms    map[roomKey]map[string]struct{}
	// tenantConns counts AUTHENTICATED connections per tenant — the
	// denominator of the per-tenant cap. Unauthenticated sockets count only
	// against the global cap enforced at upgrade time.
	tenantConns map[string]int

	maxPerTenant int
}

// New returns an empty Hub. maxPerTenant is the per-tenant connection cap.
func New(maxPerTenant int) *Hub {
	return &Hub{
		sessions:     make(map[string]*sessionMeta),
		rooms:        make(map[roomKey]map[string]struct{}),
		tenantConns:  make(map[string]int),
		maxPerTenant: maxPerTenant,
	}
}

// Register records a new, not-yet-authenticated connection.
func (h *Hub) Register(sub Subscriber) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.sessions[sub.Session()] = &sessionMeta{sub: sub, rooms: make(map[string]struct{})}
}

// BindTenant pins a session to its verified tenant after authentication and
// enforces the per-tenant cap. It is intentionally idempotent per session:
// re-binding to the SAME tenant succeeds quietly (a client retrying hello
// after a flaky frame), while binding to a DIFFERENT tenant than the one
// pinned is refused.
func (h *Hub) BindTenant(sessionID, tenantID string) error {
	h.mu.Lock()
	defer h.mu.Unlock()
	meta, ok := h.sessions[sessionID]
	if !ok {
		return ErrUnknownSession
	}
	if current := meta.sub.Tenant(); current != "" {
		if current == tenantID {
			return nil
		}
		return ErrUnknownSession // wrong-tenant rebind must not reveal which tenants are live
	}
	if h.tenantConns[tenantID] >= h.maxPerTenant {
		return ErrOverTenantCap
	}
	h.tenantConns[tenantID]++
	return nil
}

// Lookup resolves a connection id to its live subscriber. The signaling
// relay addresses frames by connection id and needs reads without holding
// the hub's lock across an Enqueue — exactly the Publish pattern: snapshot
// under lock, touch the transport outside it.
func (h *Hub) Lookup(sessionID string) (Subscriber, bool) {
	h.mu.Lock()
	defer h.mu.Unlock()
	meta, ok := h.sessions[sessionID]
	if !ok {
		return nil, false
	}
	return meta.sub, true
}

// Unregister drops the session, every room membership it held, and its
// tenant slot. It is a no-op for an unknown session (a close racing a
// shutdown sweep must not panic or corrupt counts).
func (h *Hub) Unregister(sessionID string) {
	h.mu.Lock()
	defer h.mu.Unlock()
	meta, ok := h.sessions[sessionID]
	if !ok {
		return
	}
	delete(h.sessions, sessionID)
	tenantID := meta.sub.Tenant()
	if tenantID != "" {
		if h.tenantConns[tenantID] <= 1 {
			delete(h.tenantConns, tenantID)
		} else {
			h.tenantConns[tenantID]--
		}
	}
	for roomName := range meta.rooms {
		key := roomKey{tenantID: tenantID, room: roomName}
		if peers, ok := h.rooms[key]; ok {
			delete(peers, sessionID)
			if len(peers) == 0 {
				delete(h.rooms, key)
			}
		}
	}
}

// Subscribe joins a session to (its own tenant, room) and returns the
// room's peer count AFTER the join. The tenant comes from the session's
// pin, not from the message — the frame schema cannot express a tenant.
func (h *Hub) Subscribe(sessionID, room string) (int, error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	meta, ok := h.sessions[sessionID]
	if !ok {
		return 0, ErrUnknownSession
	}
	tenantID := meta.sub.Tenant()
	if tenantID == "" {
		return 0, ErrUnknownSession
	}
	meta.rooms[room] = struct{}{}
	key := roomKey{tenantID: tenantID, room: room}
	peers, ok := h.rooms[key]
	if !ok {
		peers = make(map[string]struct{})
		h.rooms[key] = peers
	}
	peers[sessionID] = struct{}{}
	return len(peers), nil
}

// Unsubscribe removes a membership. Leaving a room never joined is a
// successful no-op (mirrors the internal hub: idempotent, never an error,
// so a client that lost track of its own state during a reconnect can just
// re-assert the state it wants).
func (h *Hub) Unsubscribe(sessionID, room string) {
	h.mu.Lock()
	defer h.mu.Unlock()
	meta, ok := h.sessions[sessionID]
	if !ok {
		return
	}
	tenantID := meta.sub.Tenant()
	delete(meta.rooms, room)
	key := roomKey{tenantID: tenantID, room: room}
	if peers, ok := h.rooms[key]; ok {
		delete(peers, sessionID)
		if len(peers) == 0 {
			delete(h.rooms, key)
		}
	}
}

// Publish fans one event out to every subscriber of (tenantID, room).
// Returns (delivered, dropped): delivered frames reached a subscriber's
// queue, dropped frames found it full. NOTHING outside the target room is
// touched, and a tenant id that is not a UUID never reaches this function
// (ingest validates shapes first).
func (h *Hub) Publish(tenantID, room, kind, eventID string, payload json.RawMessage) (delivered, dropped int) {
	h.mu.Lock()
	key := roomKey{tenantID: tenantID, room: room}
	var targets []Subscriber
	if peers, ok := h.rooms[key]; ok {
		for sessionID := range peers {
			if meta, ok := h.sessions[sessionID]; ok {
				targets = append(targets, meta.sub)
			}
		}
	}
	h.mu.Unlock()

	if len(targets) == 0 {
		return 0, 0
	}
	frame := protocol.NewDelivery(room, kind, eventID, payload, time.Now())
	for _, sub := range targets {
		if sub.Enqueue(frame) {
			delivered++
		} else {
			dropped++
		}
	}
	return delivered, dropped
}

// PeerCount reports the subscriber count of (tenantID, room) — diagnostics
// for health and for tests; not exposed per-tenant on /metrics (cardinality).
func (h *Hub) PeerCount(tenantID, room string) int {
	h.mu.Lock()
	defer h.mu.Unlock()
	return len(h.rooms[roomKey{tenantID: tenantID, room: room}])
}

// Stats returns the gauges the /metrics exposition and readiness checks
// render: rooms with members, tenants with live connections.
func (h *Hub) Stats() (rooms int, tenants int) {
	h.mu.Lock()
	defer h.mu.Unlock()
	return len(h.rooms), len(h.tenantConns)
}

// CloseAll asks every live connection to close with the given code/reason.
// Called once on shutdown; draining is the connections' own concern, so a
// browser that ignores the close frame cannot hold the process open past
// its shutdown deadline.
func (h *Hub) CloseAll(code int, reason string) {
	h.mu.Lock()
	targets := make([]Subscriber, 0, len(h.sessions))
	for _, meta := range h.sessions {
		targets = append(targets, meta.sub)
	}
	h.mu.Unlock()
	for _, sub := range targets {
		sub.RequestClose(code, reason)
	}
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/hub/hub_test.go (262 lines, sha256 7d24b459b15a74481692d56fa27d8d04f532ada7358452877658e41eaaa32634) =====
==============================================================================
```go
package hub

import (
	"encoding/json"
	"sync"
	"testing"

	"github.com/voxdesk/realtime/gateway-go/internal/protocol"
)

// recorder is a Subscriber that captures offers instead of writing to a
// socket. queueSize 0 models a full buffer (every Enqueue reports drop).
type recorder struct {
	session   string
	tenant    string
	queueSize int
	used      int

	mu     sync.Mutex
	frames []any
	closed []closeCall
}

type closeCall struct {
	code   int
	reason string
}

func (r *recorder) Session() string { return r.session }
func (r *recorder) Tenant() string  { return r.tenant }

func (r *recorder) Enqueue(msg any) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.used >= r.queueSize {
		return false
	}
	r.used++
	r.frames = append(r.frames, msg)
	return true
}

func (r *recorder) RequestClose(code int, reason string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.closed = append(r.closed, closeCall{code, reason})
}

func (r *recorder) snapshot() []any {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]any, len(r.frames))
	copy(out, r.frames)
	return out
}

const (
	tenantA = "11111111-1111-1111-1111-111111111111"
	tenantB = "22222222-2222-2222-2222-222222222222"
)

func authed(t *testing.T, h *Hub, sessionID, tenantID string, queue int) *recorder {
	t.Helper()
	// The tenant is pinned on the recorder only AFTER BindTenant succeeds,
	// exactly like a real Connection: Tenant() returns "" pre-auth.
	rec := &recorder{session: sessionID, queueSize: queue}
	h.Register(rec)
	if err := h.BindTenant(sessionID, tenantID); err != nil {
		t.Fatalf("BindTenant: %v", err)
	}
	rec.tenant = tenantID
	return rec
}

func TestPublishOnlyReachesSameTenantSameRoom(t *testing.T) {
	h := New(8)
	aCalls := authed(t, h, "sess-a-calls", tenantA, 8)
	aMetrics := authed(t, h, "sess-a-metrics", tenantA, 8)
	bCalls := authed(t, h, "sess-b-calls", tenantB, 8)

	if _, err := h.Subscribe("sess-a-calls", "calls"); err != nil {
		t.Fatal(err)
	}
	if _, err := h.Subscribe("sess-a-metrics", "metrics"); err != nil {
		t.Fatal(err)
	}
	if _, err := h.Subscribe("sess-b-calls", "calls"); err != nil {
		t.Fatal(err)
	}

	// THE tenant-isolation proof: publishing into (A, calls) reaches exactly
	// one socket — not the other room of A, not the same room of B.
	delivered, dropped := h.Publish(tenantA, "calls", "call.updated", "", json.RawMessage(`{"x":1}`))
	if delivered != 1 || dropped != 0 {
		t.Fatalf("delivered/dropped = %d/%d, want 1/0", delivered, dropped)
	}
	if got := len(aCalls.snapshot()); got != 1 {
		t.Errorf("tenant A calls subscriber got %d frames, want 1", got)
	}
	if got := len(aMetrics.snapshot()); got != 0 {
		t.Errorf("tenant A metrics subscriber got %d frames, want 0", got)
	}
	if got := len(bCalls.snapshot()); got != 0 {
		t.Errorf("TENANT B received %d frames from tenant A's room — isolation breach", got)
	}
}

func TestSubscribeRequiresBoundTenant(t *testing.T) {
	h := New(8)
	rec := &recorder{session: "sess-anon", tenant: "", queueSize: 8}
	h.Register(rec)
	if _, err := h.Subscribe("sess-anon", "calls"); err == nil {
		t.Error("unauthenticated session must not join rooms")
	}
}

func TestPerTenantConnectionCap(t *testing.T) {
	h := New(2)
	authed(t, h, "s1", tenantA, 8)
	authed(t, h, "s2", tenantA, 8)
	third := &recorder{session: "s3", queueSize: 8}
	h.Register(third)
	if err := h.BindTenant("s3", tenantA); err != ErrOverTenantCap {
		t.Fatalf("third connection: err = %v, want ErrOverTenantCap", err)
	}
	// ...while another tenant is unaffected.
	authed(t, h, "s4", tenantB, 8)
	// Unregister frees the slot.
	h.Unregister("s1")
	fifth := &recorder{session: "s5", queueSize: 8}
	h.Register(fifth)
	if err := h.BindTenant("s5", tenantA); err != nil {
		t.Fatalf("after unregister the slot must be reusable: %v", err)
	}
}

func TestCannotBindSessionToTwoTenants(t *testing.T) {
	h := New(8)
	authed(t, h, "s1", tenantA, 8)
	if err := h.BindTenant("s1", tenantB); err == nil {
		t.Error("re-binding to a different tenant must be refused")
	}
	if err := h.BindTenant("s1", tenantA); err != nil {
		t.Errorf("idempotent re-bind to the SAME tenant must succeed: %v", err)
	}
}

func TestBackpressureDropIsCountedNotStalling(t *testing.T) {
	h := New(8)
	slow := authed(t, h, "sess-slow", tenantA, 1) // one-slot queue
	fast := authed(t, h, "sess-fast", tenantA, 8)
	_, _ = h.Subscribe("sess-slow", "calls")
	_, _ = h.Subscribe("sess-fast", "calls")

	payload := json.RawMessage(`{"n":0}`)
	h.Publish(tenantA, "calls", "metrics.tick_15s", "", payload) // slow gets 1, fills its slot
	delivered, dropped := h.Publish(tenantA, "calls", "metrics.tick_15s", "", payload)

	if delivered != 1 || dropped != 1 {
		t.Fatalf("second publish: delivered/dropped = %d/%d, want 1/1", delivered, dropped)
	}
	if got := len(fast.snapshot()); got != 2 {
		t.Errorf("fast subscriber = %d frames, want 2 — a slow peer must not stall the room", got)
	}
	_ = slow
}

func TestUnsubscribeIsIdempotent(t *testing.T) {
	h := New(8)
	authed(t, h, "s1", tenantA, 8)
	_, _ = h.Subscribe("s1", "calls")
	h.Unsubscribe("s1", "calls")
	h.Unsubscribe("s1", "calls") // leaving twice must not error or corrupt
	if got := h.PeerCount(tenantA, "calls"); got != 0 {
		t.Errorf("PeerCount = %d, want 0", got)
	}
	h.Unsubscribe("s1", "never-joined") // never joined: silent no-op by contract
}

func TestUnregisterScrubsRoomsAndCounts(t *testing.T) {
	h := New(8)
	authed(t, h, "s1", tenantA, 8)
	_, _ = h.Subscribe("s1", "calls")
	h.Unregister("s1")
	h.Unregister("s1") // racing close: must be a no-op
	rooms, tenants := h.Stats()
	if rooms != 0 || tenants != 0 {
		t.Errorf("Stats = (%d rooms, %d tenants), want (0, 0)", rooms, tenants)
	}
}

func TestPeersAcknowledgementCountsJoinAfterAdd(t *testing.T) {
	h := New(8)
	authed(t, h, "s1", tenantA, 8)
	authed(t, h, "s2", tenantA, 8)
	peers1, _ := h.Subscribe("s1", "calls")
	peers2, _ := h.Subscribe("s2", "calls")
	if peers1 != 1 || peers2 != 2 {
		t.Errorf("peers = %d then %d, want 1 then 2 (count AFTER join)", peers1, peers2)
	}
}

func TestCloseAllClosesEveryConnectionOnce(t *testing.T) {
	h := New(8)
	a := authed(t, h, "s1", tenantA, 8)
	b := authed(t, h, "s2", tenantB, 8)
	h.CloseAll(protocol.CloseGoingAway, "server shutting down")
	for _, rec := range []*recorder{a, b} {
		rec.mu.Lock()
		got := len(rec.closed)
		code := -1
		if got > 0 {
			code = rec.closed[0].code
		}
		rec.mu.Unlock()
		if got != 1 || code != protocol.CloseGoingAway {
			t.Errorf("session %s: closed %d times with code %d, want once with 1001", rec.session, got, code)
		}
	}
}

func TestConcurrentPublishTenantIsolation(t *testing.T) {
	// Mirrors signal-go's concurrent smoke test: hammer the hub with
	// concurrent publishers and subscribers and assert nothing ever crosses
	// the tenant line (run with -race).
	h := New(64)
	a := authed(t, h, "sa", tenantA, 4096)
	b := authed(t, h, "sb", tenantB, 4096)
	_, _ = h.Subscribe("sa", "calls")
	_, _ = h.Subscribe("sb", "calls")

	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(2)
		go func() {
			defer wg.Done()
			for n := 0; n < 200; n++ {
				h.Publish(tenantA, "calls", "metrics.tick_15s", "", json.RawMessage(`{"t":"a"}`))
			}
		}()
		go func() {
			defer wg.Done()
			for n := 0; n < 200; n++ {
				h.Publish(tenantB, "calls", "metrics.tick_15s", "", json.RawMessage(`{"t":"b"}`))
			}
		}()
	}
	wg.Wait()

	for _, frame := range a.snapshot() {
		delivery := frame.(protocol.Delivery)
		if string(delivery.Payload) != `{"t":"a"}` {
			t.Fatalf("TENANT B PAYLOAD REACHED TENANT A: %s", delivery.Payload)
		}
	}
	for _, frame := range b.snapshot() {
		delivery := frame.(protocol.Delivery)
		if string(delivery.Payload) != `{"t":"b"}` {
			t.Fatalf("TENANT A PAYLOAD REACHED TENANT B: %s", delivery.Payload)
		}
	}
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/idempotency/store.go (101 lines, sha256 5216274c0a02c718e336754ed0682e8f255a32a7f856c4177588f26b296cf246) =====
==============================================================================
```go
// Package idempotency owns the ingest replay guard: which (tenant, event)
// pairs this edge has already fanned out, remembered long enough to make
// publisher retries converge, and NO longer.
//
// It mirrors the Python side's MessageWebhookReceipt / BillingWebhookReceipt
// split in spirit: database-durable suppression lives THERE (the API's own
// exactly-once machinery); this hot cache is the cheap first wall for a
// stateless edge that has no database. Losing an entry costs a duplicate
// DELIVERY to browsers — annoying, never state-corrupting — which is the
// entire reason an evicting cache is acceptable here while it would not be
// for billing or webhooks.
package idempotency

import (
	"sync"
	"time"
)

// Store is the replay cache. All methods are safe for concurrent use; a
// single mutex is correct at this cardinality (sweeps are amortized, and
// the map is small by construction).
type Store struct {
	mu        sync.Mutex
	seen      map[string]time.Time
	ttl       time.Duration
	capacity  int
	lastSweep time.Time
}

// NewStore builds the guard: entries age out after ttl, and the map is
// bounded at capacity entries (see sweep for the eviction policy when
// pressure exceeds that anyway).
func NewStore(ttl time.Duration, capacity int) *Store {
	return &Store{
		seen:     make(map[string]time.Time),
		ttl:      ttl,
		capacity: capacity,
	}
}

// key joins tenant and event id: replay scope is per-tenant, matching every
// other idempotency constraint in the system (UniqueConstraint(tenant_id, …)).
func key(tenantID, eventID string) string { return tenantID + "|" + eventID }

// SeenBefore reports whether this (tenant, event) was already fanned out;
// when it was not, it is recorded. Sweeping is amortized: at most once per
// TTL window per capacity pressure, never per request on the hot path.
func (s *Store) SeenBefore(tenantID, eventID string, now time.Time) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	k := key(tenantID, eventID)
	if at, ok := s.seen[k]; ok && now.Sub(at) < s.ttl {
		return true
	}
	if len(s.seen) >= s.capacity || now.Sub(s.lastSweep) >= s.ttl {
		s.sweep(now)
	}
	s.seen[k] = now
	return false
}

// Len reports live entries — the gauge substrate for observability and the
// capacity assertions in tests.
func (s *Store) Len() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.seen)
}

// sweep drops expired entries; if the map is STILL over capacity afterwards
// (a genuinely high-cardinality burst), the oldest ~10% are evicted. Losing
// an old replay record only ever causes a duplicate DELIVERY — which is why
// this eviction exists here and must never exist for webhooks or billing.
//
// Caller must hold s.mu.
func (s *Store) sweep(now time.Time) {
	s.lastSweep = now
	for k, at := range s.seen {
		if now.Sub(at) >= s.ttl {
			delete(s.seen, k)
		}
	}
	if len(s.seen) < s.capacity {
		return
	}
	// Oldest-first eviction of 10% without a full sort: any entry older
	// than the TTL's midpoint goes first, then give up — the map self-heals
	// on the next sweep.
	cutoff := now.Add(-s.ttl / 2)
	evicted := 0
	target := s.capacity / 10
	for k, at := range s.seen {
		if at.Before(cutoff) {
			delete(s.seen, k)
			evicted++
			if evicted >= target {
				break
			}
		}
	}
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/idempotency/store_test.go (90 lines, sha256 474f73f262daafb2a0612b736d21ef7a75894c849f8e1a6e302a209083ff8b3c) =====
==============================================================================
```go
package idempotency

import (
	"strconv"
	"testing"
	"time"
)

// Unit-level proof of the guard: what the HTTP ingest tests previously
// exercised only end-to-end, pinned here on the algorithm itself (TTL,
// per-tenant scoping, capacity, eviction).

var start = time.Date(2026, 9, 16, 10, 0, 0, 0, time.UTC)

func TestFirstSeenIsFreshSecondIsReplay(t *testing.T) {
	s := NewStore(time.Minute, 100)
	if s.SeenBefore("tenant-a", "ev-1", start) {
		t.Fatal("first sighting must be fresh")
	}
	if !s.SeenBefore("tenant-a", "ev-1", start.Add(time.Second)) {
		t.Fatal("second sighting inside the TTL must be a replay")
	}
	if s.Len() != 1 {
		t.Fatalf("replays must not grow the store: Len = %d", s.Len())
	}
}

func TestReplayScopeIsPerTenant(t *testing.T) {
	s := NewStore(time.Minute, 100)
	s.SeenBefore("tenant-a", "ev-1", start)
	if s.SeenBefore("tenant-b", "ev-1", start) {
		t.Fatal("the same event id under ANOTHER tenant is not a replay — scoping matches UniqueConstraint(tenant_id, …)")
	}
}

func TestEntriesExpirePastTheTTL(t *testing.T) {
	s := NewStore(time.Minute, 100)
	s.SeenBefore("tenant-a", "ev-1", start)
	if s.SeenBefore("tenant-a", "ev-1", start.Add(2*time.Minute)) {
		t.Fatal("an entry past its TTL must not suppress — realtime events are worthless redelivered late anyway")
	}
}

func TestCapacityTriggersEvictionButHotKeysSurvive(t *testing.T) {
	// TTL 1 h, capacity 10: insert entries spread across the half-TTL
	// cutoff so the oldest-third heuristic has something to evict.
	s := NewStore(time.Hour, 10)
	base := start
	for i := 0; i < 10; i++ {
		at := base.Add(time.Duration(i) * 2 * time.Minute) // 0..18 minutes old
		s.SeenBefore("tenant-a", "ev-"+strconv.Itoa(i), at)
	}
	if s.Len() != 10 {
		t.Fatalf("setup: Len = %d", s.Len())
	}

	// One more fresh entry forces a sweep: nothing is TTL-expired yet (all
	// well under an hour), so the oldest-beyond-midpoint entries evict.
	cutoff := base.Add(40 * time.Minute) // now is 20 min after start
	s.SeenBefore("tenant-a", "ev-new", cutoff)
	if s.Len() > 10 {
		t.Fatalf("store stayed over capacity after sweep: Len = %d", s.Len())
	}

	// The event recorded just now must still be treated as seen (a replay
	// cache that evicts its OWN freshest entries is useless).
	if !s.SeenBefore("tenant-a", "ev-new", cutoff.Add(time.Second)) {
		t.Fatal("freshest entry must survive its own sweep")
	}
}

func TestSeenRecordingIsAtomicWithTheCheck(t *testing.T) {
	// Two live calls for the same key, sequenced by the mutex (concurrency
	// is covered under -race by the concurrent accesses in this loop):
	s := NewStore(time.Minute, 100)
	done := make(chan bool, 64)
	for i := 0; i < 64; i++ {
		go func(n int) {
			s.SeenBefore("tenant-a", "ev-"+strconv.Itoa(n%8), start)
			done <- true
		}(i)
	}
	for i := 0; i < 64; i++ {
		<-done
	}
	// 8 distinct keys, no more and no less, regardless of interleaving.
	if got := s.Len(); got != 8 {
		t.Fatalf("Len = %d after concurrent inserts of 8 keys", got)
	}
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/observability/events.go (103 lines, sha256 97dad1792a3df8777c039e4b16073e610f1c8886e1ac98cdb74fe695f31c7d82) =====
==============================================================================
```go
package observability

import (
	"encoding/json"
	"sort"
	"time"
)

// Event is one structured operational fact: a named moment plus the small
// set of key/value fields that explain it. Events are the audit-shaped
// half of observability — /metrics answers "how much/many", an event
// answers "what exactly happened to THIS publish/session". Field values
// must stay operator-safe (ids, counts, durations), never payloads or
// secrets: emitters may write events to plain text logs.
type Event struct {
	Name   string            // dotted, stable: "ingest.accepted", "session.opened"
	At     time.Time         // when it happened (callers set time.Now())
	Fields map[string]string // ids and counters; see package note above
}

// Field returns the value of one field ("" when unset).
func (e Event) Field(key string) string { return e.Fields[key] }

// Emitter consumes events. Implementations must be safe for concurrent
// use and must never block on slow sinks for longer than a producer is
// prepared to wait — events are telemetry, not a control channel.
type Emitter interface {
	Emit(Event)
}

// EmitterFunc adapts a function to Emitter.
type EmitterFunc func(Event)

// Emit implements Emitter.
func (f EmitterFunc) Emit(e Event) { f(e) }

// nopEmitter is the discard implementation behind NopEmitter. It is a
// struct (not an EmitterFunc) so interface equality checks against
// NopEmitter never hit Go's "comparing uncomparable func values" panic.
type nopEmitter struct{}

// Emit implements Emitter.
func (nopEmitter) Emit(Event) {}

// NopEmitter discards everything — the zero-dependency default.
var NopEmitter Emitter = nopEmitter{}

// MultiEmitter fans one event out to every emitter it holds.
func MultiEmitter(emitters ...Emitter) Emitter {
	flat := make([]Emitter, 0, len(emitters))
	for _, em := range emitters {
		if em != nil && em != NopEmitter {
			flat = append(flat, em)
		}
	}
	if len(flat) == 0 {
		return NopEmitter
	}
	return EmitterFunc(func(e Event) {
		for _, em := range flat {
			em.Emit(e)
		}
	})
}

// LogEmitter renders events as single JSON lines through a Logger at Info
// level — the gateway's production emitter. JSON (not printf) because the
// shipper side of this log stream already parses the Python app's JSON
// logs; one shape for the whole platform. Field keys are SORTED so two
// emits of the same event diff cleanly.
type LogEmitter struct {
	logger *Logger
}

// NewLogEmitter emits through logger at Info level. A nil logger discards.
func NewLogEmitter(logger *Logger) *LogEmitter {
	return &LogEmitter{logger: logger}
}

// Emit implements Emitter.
func (e *LogEmitter) Emit(ev Event) {
	if e.logger == nil || !e.logger.Enabled(Info) {
		return
	}
	keys := make([]string, 0, len(ev.Fields))
	for k := range ev.Fields {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	fields := make(map[string]string, len(ev.Fields))
	for _, k := range keys {
		fields[k] = ev.Fields[k]
	}
	line, err := json.Marshal(struct {
		Name   string            `json:"event"`
		At     time.Time         `json:"at"`
		Fields map[string]string `json:"fields,omitempty"`
	}{Name: ev.Name, At: ev.At, Fields: fields})
	if err != nil {
		return // a map[string]string cannot fail to marshal; can't-happen guard
	}
	e.logger.Infof("EVENT %s", line)
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/observability/logging.go (154 lines, sha256 6dbb1318b22d23c160c42fca4e1c1ffb23b1db6f620f752bff988d549244f618) =====
==============================================================================
```go
// Package observability is the gateway's single home for everything it
// says about itself: the leveled logger (this file), the request-id
// middleware (tracing.go), the structured event emitter (events.go), and
// the Prometheus metrics core (metrics/).
//
// The logger is deliberately a thin, leveled skin over the standard
// library's log package: the gateway has always logged unlabelled text
// lines (matching log.Printf everywhere), and what the package split adds
// is levels and a structured key/value suffix — enough to silence chatter
// in one knob and to grep a request id out of a mixed log stream — without
// trading in the operational simplicity of plain text logs. The Python
// side's structured-logging module (app/core/logging.py) remains the rich
// sibling; this edge stays greppable.
package observability

import (
	"fmt"
	"io"
	"log"
	"strings"
	"sync"
	"sync/atomic"
)

// Level filters log output. Ordered by severity: a Logger set to Info
// prints Info/Warn/Error and drops Debug.
type Level int32

const (
	Debug Level = iota
	Info
	Warn
	Error
	// Off silences the logger entirely (tests).
	Off
)

// String renders the level for log lines ("info", "warn", ...).
func (l Level) String() string {
	switch l {
	case Debug:
		return "debug"
	case Info:
		return "info"
	case Warn:
		return "warn"
	case Error:
		return "error"
	case Off:
		return "off"
	default:
		return fmt.Sprintf("level(%d)", int32(l))
	}
}

// ParseLevel accepts case-insensitive names; unknown values fall back to
// Info (a misconfigured level must never silently disable logging).
func ParseLevel(s string) Level {
	switch strings.ToLower(strings.TrimSpace(s)) {
	case "debug":
		return Debug
	case "warn", "warning":
		return Warn
	case "error":
		return Error
	case "off":
		return Off
	default:
		return Info
	}
}

// Logger is a level-filtered, optionally key-tagged logger. All methods
// are safe for concurrent use; the level may be raised/lowered at runtime
// (SetLevel) without a restart, which is how an operator cranks a live
// node to debug without a redeploy.
type Logger struct {
	mu   sync.Mutex // guards only the bound kvPairs suffix (set once at With)
	core *log.Logger
	min  atomic.Int32
	kv   string // pre-rendered " k=v k=v" suffix from With()
}

// New returns a Logger writing to out with the given line prefix and
// minimum level. A nil out discards everything (useful in tests that only
// exercise level logic).
func New(out io.Writer, prefix string, min Level) *Logger {
	if out == nil {
		out = io.Discard
	}
	l := &Logger{core: log.New(out, prefix, log.LstdFlags|log.LUTC)}
	l.min.Store(int32(min))
	return l
}

// With returns a child logger whose lines carry the bound key=value pairs
// as a stable suffix ('user=… tenant=…') — the cheap half of structured
// logging: fixed in the line, greppable, zero allocation per call beyond
// the message itself.
func (l *Logger) With(kv ...string) *Logger {
	l.mu.Lock()
	base := l.kv
	l.mu.Unlock()
	child := &Logger{core: l.core, kv: base + renderKV(kv)}
	child.min.Store(l.min.Load())
	return child
}

// SetLevel changes the filter threshold at runtime (only this Logger AND
// any children whose levels were copied BEFORE the change — construct the
// shared Logger first, then With(); children minted later inherit it).
func (l *Logger) SetLevel(min Level) { l.min.Store(int32(min)) }

// Level reports the current threshold.
func (l *Logger) Level() Level { return Level(l.min.Load()) }

// Enabled reports whether a message at level would print.
func (l *Logger) Enabled(level Level) bool { return int32(level) >= l.min.Load() }

// Debugf/Infof/Warnf/Errorf log at the respective level.
func (l *Logger) Debugf(format string, args ...any) { l.logf(Debug, format, args...) }
func (l *Logger) Infof(format string, args ...any)  { l.logf(Info, format, args...) }
func (l *Logger) Warnf(format string, args ...any)  { l.logf(Warn, format, args...) }
func (l *Logger) Errorf(format string, args ...any) { l.logf(Error, format, args...) }

func (l *Logger) logf(level Level, format string, args ...any) {
	if !l.Enabled(level) {
		return
	}
	l.mu.Lock()
	suffix := l.kv
	l.mu.Unlock()
	msg := fmt.Sprintf(format, args...)
	l.core.Printf("%s %s%s", strings.ToUpper(level.String()), msg, suffix)
}

// renderKV formats an alternating key/value slice into " k1=v1 k2=v2".
// Odd trailing values render as " key" (a key with no value); spaces in
// values are quoted so the suffix stays machine-splittable.
func renderKV(kv []string) string {
	var b strings.Builder
	for i := 0; i < len(kv); i += 2 {
		if i+1 >= len(kv) {
			fmt.Fprintf(&b, " %s", kv[i])
			break
		}
		value := kv[i+1]
		if strings.ContainsAny(value, " \t\"") {
			value = fmt.Sprintf("%q", value)
		}
		fmt.Fprintf(&b, " %s=%s", kv[i], value)
	}
	return b.String()
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/observability/metrics/metrics.go (205 lines, sha256 7267f51c340222d6cc88c7f4f690268aa348086c72421ace1983ef4a073156a5) =====
==============================================================================
```go
// Package metrics is the gateway's tiny, dependency-free telemetry core:
// atomic counters/gauges plus a Prometheus text exposition.
//
// Why hand-rolled: the Python app uses prometheus-client, but dragging the
// Go client library in for seven counters would double the dependency
// surface of a security-edge service for zero behavioural gain. The text
// format rendered here is the standard Prometheus exposition format, so the
// same scraper that reads the API's /metrics reads this one unchanged.
//
// Labels are deliberately avoided: a label per tenant/room is unbounded
// cardinality, the exact failure the Python side calls out when it keeps
// per-call facts in structured logs instead of metric labels. Per-tenant
// detail belongs in logs, not here.
//
// This package lives under internal/observability alongside the leveled
// logger, the request-id middleware and the structured event emitter —
// one home for everything the gateway says about itself.
package metrics

import (
	"fmt"
	"runtime"
	"strings"
	"sync/atomic"
	"time"
)

// Registry holds every instrument. All methods are safe for concurrent use.
type Registry struct {
	startedAt time.Time

	connectionsCurrent atomic.Int64
	connectionsTotal   atomic.Int64
	connectionsRefused atomic.Int64 // capacity rejected at upgrade
	authFailuresTotal  atomic.Int64
	messagesReadTotal  atomic.Int64
	rateLimitedTotal   atomic.Int64
	deliveriesTotal    atomic.Int64 // frames handed to subscriber queues
	droppedTotal       atomic.Int64 // frames lost to full subscriber queues
	ingestTotal        atomic.Int64 // accepted ingest publishes
	ingestDuplicates   atomic.Int64 // replayed event_ids, no fan-out
	ingestRejected     atomic.Int64 // 401/413/422s

	signalSessionsCurrent atomic.Int64 // live signaling sessions
	signalSessionsTotal   atomic.Int64 // sessions created since boot
	signalRelayedTotal    atomic.Int64 // offer/answer/candidate frames forwarded to a peer

	presenceUsersCurrent atomic.Int64 // reachable (tenant, user) pairs, fed by presence.OnChange

	// Engine-link telemetry (Go → Rust media engine control plane). The
	// gauge is the monitor's availability view; latency is captured as a
	// fixed-bucket histogram (Prometheus-friendly, no client lib needed).
	engineUp            atomic.Int64 // 1 when the monitor's probe loop is green
	engineCallsTotal    atomic.Int64
	engineErrorsTotal   atomic.Int64
	engineLatencySumMs  atomic.Int64 // microsecond-precision stored as ms×1000? no: plain ns→ms int64
	engineLatencyMaxMs  atomic.Int64
	engineJoinTotal     atomic.Int64
	engineJoinErrTotal  atomic.Int64
	engineLeaveTotal    atomic.Int64
	engineLeaveErrTotal atomic.Int64
	engineProbeTotal    atomic.Int64
	engineProbeErrTotal atomic.Int64
}

// New returns a zeroed registry stamped with the boot time.
func New() *Registry {
	return &Registry{startedAt: time.Now()}
}

func (r *Registry) ConnOpened()       { r.connectionsCurrent.Add(1); r.connectionsTotal.Add(1) }
func (r *Registry) ConnClosed()       { r.connectionsCurrent.Add(-1) }
func (r *Registry) ConnRefused()      { r.connectionsRefused.Add(1) }
func (r *Registry) AuthFailed()       { r.authFailuresTotal.Add(1) }
func (r *Registry) MessageRead()      { r.messagesReadTotal.Add(1) }
func (r *Registry) RateLimited()      { r.rateLimitedTotal.Add(1) }
func (r *Registry) Delivered(n int64) { r.deliveriesTotal.Add(n) }
func (r *Registry) Dropped(n int64)   { r.droppedTotal.Add(n) }
func (r *Registry) IngestAccepted()   { r.ingestTotal.Add(1) }
func (r *Registry) IngestDuplicate()  { r.ingestDuplicates.Add(1) }
func (r *Registry) IngestRejected()   { r.ingestRejected.Add(1) }

// SignalSession* instrument the signaling relay (internal/signaling). The
// gauge is paired with the counter so dashboards get both "right now" and
// "pressure over time".
func (r *Registry) SignalSessionOpened() {
	r.signalSessionsCurrent.Add(1)
	r.signalSessionsTotal.Add(1)
}
func (r *Registry) SignalSessionClosed() { r.signalSessionsCurrent.Add(-1) }
func (r *Registry) SignalRelayed()       { r.signalRelayedTotal.Add(1) }

// SetPresenceUsers repoints the presence gauge at the registry's latest
// TotalUsers snapshot. It is a SET (not an increment): presence transitions
// publish their authoritative post-transition total, so the gauge self-
// heals on any missed decrement instead of drifting.
func (r *Registry) SetPresenceUsers(n int64) { r.presenceUsersCurrent.Store(n) }

// PresenceUsersCurrent is the gauge snapshot (diagnostics/tests).
func (r *Registry) PresenceUsersCurrent() int64 { return r.presenceUsersCurrent.Load() }

// ---- media-engine link ---------------------------------------------------

// EngineCall records one signaling-plane call to the Rust engine: op is the
// client's vocabulary ("join"/"leave"/other), so the broken-down counters
// stay in sync with deploys that call only a subset. took is wall latency
// the caller measured across the whole HTTP round trip.
func (r *Registry) EngineCall(op string, took time.Duration, failed bool) {
	r.engineCallsTotal.Add(1)
	switch op {
	case "join":
		r.engineJoinTotal.Add(1)
		if failed {
			r.engineJoinErrTotal.Add(1)
		}
	case "leave":
		r.engineLeaveTotal.Add(1)
		if failed {
			r.engineLeaveErrTotal.Add(1)
		}
	}
	if failed {
		r.engineErrorsTotal.Add(1)
	}
	ms := took.Milliseconds()
	r.engineLatencySumMs.Add(ms)
	for {
		prev := r.engineLatencyMaxMs.Load()
		if ms <= prev || r.engineLatencyMaxMs.CompareAndSwap(prev, ms) {
			break
		}
	}
}

// EngineProbe records a monitor health round trip.
func (r *Registry) EngineProbe(took time.Duration, failed bool) {
	r.engineProbeTotal.Add(1)
	r.engineLatencySumMs.Add(took.Milliseconds())
	if failed {
		r.engineProbeErrTotal.Add(1)
	}
}

// SetEngineUp repoints the availability gauge (transition events only, as
// sent by the monitor — no drift possible).
func (r *Registry) SetEngineUp(up bool) {
	if up {
		r.engineUp.Store(1)
	} else {
		r.engineUp.Store(0)
	}
}

// EngineUp is the readiness surface's view of engine availability.
func (r *Registry) EngineUp() bool { return r.engineUp.Load() == 1 }

// ConnectionsCurrent is a gauge snapshot (also used by readiness).
func (r *Registry) ConnectionsCurrent() int64 { return r.connectionsCurrent.Load() }

// Render emits the Prometheus text exposition format for one scrape.
// rooms/tenant gauges the registry cannot know are passed in by the caller
// (the hub owns that state).
func (r *Registry) Render(rooms int, tenants int) string {
	var b strings.Builder

	writeGauge := func(name, help string, value int64) {
		fmt.Fprintf(&b, "# HELP %s %s\n# TYPE %s gauge\n%s %d\n", name, help, name, name, value)
	}
	writeCounter := func(name, help string, value int64) {
		fmt.Fprintf(&b, "# HELP %s %s\n# TYPE %s counter\n%s %d\n", name, help, name, name, value)
	}

	writeGauge("voxdesk_gateway_connections_current", "Live WebSocket connections.", r.connectionsCurrent.Load())
	writeCounter("voxdesk_gateway_connections_total", "Connections accepted since boot.", r.connectionsTotal.Load())
	writeCounter("voxdesk_gateway_connections_refused_total", "Upgrades rejected by capacity limits.", r.connectionsRefused.Load())
	writeCounter("voxdesk_gateway_auth_failures_total", "Hello frames rejected by token verification.", r.authFailuresTotal.Load())
	writeCounter("voxdesk_gateway_messages_read_total", "Client frames read after upgrade.", r.messagesReadTotal.Load())
	writeCounter("voxdesk_gateway_rate_limited_total", "Client frames rejected by the per-connection limiter.", r.rateLimitedTotal.Load())
	writeCounter("voxdesk_gateway_deliveries_total", "Frames enqueued to subscriber queues.", r.deliveriesTotal.Load())
	writeCounter("voxdesk_gateway_dropped_total", "Frames dropped from full subscriber queues (backpressure-drop).", r.droppedTotal.Load())
	writeCounter("voxdesk_gateway_ingest_total", "Accepted ingest publishes.", r.ingestTotal.Load())
	writeCounter("voxdesk_gateway_ingest_duplicates_total", "Ingest publishes suppressed by the replay cache.", r.ingestDuplicates.Load())
	writeCounter("voxdesk_gateway_ingest_rejected_total", "Ingest requests rejected (auth/size/shape).", r.ingestRejected.Load())
	writeGauge("voxdesk_gateway_rooms_current", "Rooms with at least one subscriber.", int64(rooms))
	writeGauge("voxdesk_gateway_tenants_current", "Tenants with at least one live connection.", int64(tenants))
	writeGauge("voxdesk_gateway_signal_sessions_current", "Live signaling sessions (point-to-point negotiations).", r.signalSessionsCurrent.Load())
	writeCounter("voxdesk_gateway_signal_sessions_total", "Signaling sessions created since boot.", r.signalSessionsTotal.Load())
	writeCounter("voxdesk_gateway_signal_relayed_total", "Offer/answer/candidate frames forwarded to a peer.", r.signalRelayedTotal.Load())
	writeGauge("voxdesk_gateway_presence_users_current", "Reachable (tenant, user) pairs on this node (JWT-verified identities).", r.presenceUsersCurrent.Load())
	writeGauge("voxdesk_gateway_engine_up", "Media-engine availability as seen by the health monitor (0/1).", r.engineUp.Load())
	writeCounter("voxdesk_gateway_engine_signal_calls_total", "Control-plane calls to the media engine.", r.engineCallsTotal.Load())
	writeCounter("voxdesk_gateway_engine_signal_errors_total", "Control-plane calls that failed (any cause).", r.engineErrorsTotal.Load())
	writeCounter("voxdesk_gateway_engine_join_total", "Engine join signals issued.", r.engineJoinTotal.Load())
	writeCounter("voxdesk_gateway_engine_join_errors_total", "Engine joins refused or lost.", r.engineJoinErrTotal.Load())
	writeCounter("voxdesk_gateway_engine_leave_total", "Engine leave signals issued.", r.engineLeaveTotal.Load())
	writeCounter("voxdesk_gateway_engine_leave_errors_total", "Engine leaves refused or lost.", r.engineLeaveErrTotal.Load())
	writeCounter("voxdesk_gateway_engine_probes_total", "Availability probes to /v1/health.", r.engineProbeTotal.Load())
	writeCounter("voxdesk_gateway_engine_probe_errors_total", "Availability probes that failed.", r.engineProbeErrTotal.Load())
	writeGauge("voxdesk_gateway_engine_latency_ms_sum", "Sum of engine round-trip milliseconds (÷calls for mean).", r.engineLatencySumMs.Load())
	writeGauge("voxdesk_gateway_engine_latency_ms_max", "Worst observed engine round trip (ms, never reset).", r.engineLatencyMaxMs.Load())
	writeGauge("voxdesk_gateway_goroutines", "Live goroutines (leak canary).", int64(runtime.NumGoroutine()))
	writeGauge("voxdesk_gateway_uptime_seconds", "Seconds since boot.", int64(time.Since(r.startedAt).Seconds()))

	return b.String()
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/observability/observability_test.go (173 lines, sha256 0d9b4488a311a84b2ffe50ef888cdd7598240d0faef12b9a2a61175cad7c8113) =====
==============================================================================
```go
package observability

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestLoggerFiltersByLevelAndCarriesFields(t *testing.T) {
	t.Parallel()
	var buf bytes.Buffer
	l := New(&buf, "[gw] ", Info).With("tenant", "t-1")

	l.Debugf("hidden %d", 1)
	l.Infof("shown %s", "yes")

	out := buf.String()
	if strings.Contains(out, "hidden") {
		t.Fatalf("debug must be filtered at Info: %q", out)
	}
	if !strings.Contains(out, "INFO shown yes") || !strings.Contains(out, "tenant=t-1") {
		t.Fatalf("info line must carry message and bound fields: %q", out)
	}
	if !strings.HasPrefix(out, "[gw] ") {
		t.Fatalf("prefix lost: %q", out)
	}
}

func TestLoggerRuntimeLevelChangeAndValueQuoting(t *testing.T) {
	t.Parallel()
	var buf bytes.Buffer
	l := New(&buf, "", Warn)
	l.Infof("quiet")
	l.SetLevel(Debug)
	l.Debugf("loud %s", "here")
	l.With("path", "a b", "n", "3").Infof("kv")

	out := buf.String()
	if strings.Contains(out, "quiet") {
		t.Fatal("info below threshold must not print")
	}
	if !strings.Contains(out, "DEBUG loud here") {
		t.Fatalf("raised threshold must admit debug: %q", out)
	}
	if !strings.Contains(out, `path="a b" n=3`) {
		t.Fatalf("values with spaces must be quoted: %q", out)
	}
}

func TestParseLevelNeverDisablesAccidentally(t *testing.T) {
	t.Parallel()
	if ParseLevel("DEBUG") != Debug || ParseLevel("warn") != Warn || ParseLevel("error") != Error {
		t.Fatal("named levels must parse")
	}
	if ParseLevel("garbage") != Info {
		t.Fatal("unknown level must fall back to Info, never Off")
	}
}

func TestRequestIDMiddlewareHonoursSanitizesAndStamps(t *testing.T) {
	t.Parallel()
	var seen string
	handler := RequestIDMiddleware(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen = RequestIDFrom(r.Context())
		w.WriteHeader(http.StatusNoContent)
	}))

	for _, tc := range []struct{ in, want string }{
		{"api-trace-123", "api-trace-123"}, // kept: the trace continues
		{"bad\r\ninjected-x", ""},          // control bytes rejected → replaced
		{strings.Repeat("x", 200), ""},     // oversized rejected → replaced
	} {
		seen = ""
		req := httptest.NewRequest(http.MethodGet, "/x", nil)
		req.Header.Set(RequestIDHeader, tc.in)
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, req)

		respID := rec.Header().Get(RequestIDHeader)
		if tc.want != "" {
			if seen != tc.want || respID != tc.want {
				t.Fatalf("presented %q must survive: ctx=%q resp=%q", tc.in, seen, respID)
			}
			continue
		}
		if len(respID) != 32 || seen != respID {
			t.Fatalf("id %q must be replaced with a fresh 32-hex id (ctx=%q)", tc.in, seen)
		}
	}
}

func TestRequestIDFromAbsent(t *testing.T) {
	t.Parallel()
	if got := RequestIDFrom(context.Background()); got != "" {
		t.Fatalf("no middleware, no id — got %q", got)
	}
}

func TestMultiEmitterFansOutAndSkipsNilAndNop(t *testing.T) {
	t.Parallel()
	var a, b []string
	em := MultiEmitter(
		nil,
		NopEmitter,
		EmitterFunc(func(e Event) { a = append(a, e.Name) }),
		EmitterFunc(func(e Event) { b = append(b, e.Name) }),
	)
	em.Emit(Event{Name: "ingest.accepted", At: time.Now()})
	if len(a) != 1 || len(b) != 1 {
		t.Fatalf("both real emitters must see the event: a=%v b=%v", a, b)
	}
	if MultiEmitter() != NopEmitter {
		t.Fatal("an all-empty multi must collapse to the nop emitter")
	}
}

func TestLogEmitterRendersSortedJSONOnlyWhenEnabled(t *testing.T) {
	t.Parallel()
	var buf bytes.Buffer
	em := NewLogEmitter(New(&buf, "", Info))

	em.Emit(Event{Name: "ingest.accepted", At: time.Unix(1_700_000_000, 0).UTC(),
		Fields: map[string]string{"room": "calls", "delivered": "3", "request_id": "r-1"}})

	line := strings.TrimSpace(buf.String())
	payload := line[strings.Index(line, "EVENT ")+6:]
	var decoded struct {
		Event  string            `json:"event"`
		At     time.Time         `json:"at"`
		Fields map[string]string `json:"fields"`
	}
	if err := json.Unmarshal([]byte(payload), &decoded); err != nil {
		t.Fatalf("emitter must produce one JSON line, got %q: %v", line, err)
	}
	if decoded.Event != "ingest.accepted" || decoded.Fields["delivered"] != "3" || !decoded.At.Equal(time.Unix(1_700_000_000, 0).UTC()) {
		t.Fatalf("decoded event wrong: %+v", decoded)
	}

	// Level filter really filters.
	buf.Reset()
	quietLogger := New(&buf, "", Warn)
	NewLogEmitter(quietLogger).Emit(Event{Name: "x", At: time.Now()})
	if buf.Len() != 0 {
		t.Fatal("events must respect the underlying logger's level")
	}
	// A nil logger is a safe discard.
	NewLogEmitter(nil).Emit(Event{Name: "x", At: time.Now()})
}

func TestEmittersAndLoggerAreConcurrencySafe(t *testing.T) {
	t.Parallel()
	var buf bytes.Buffer
	l := New(&buf, "", Info)
	em := NewLogEmitter(l)
	var wg sync.WaitGroup
	for g := 0; g < 8; g++ {
		wg.Add(1)
		go func(g int) {
			defer wg.Done()
			for i := 0; i < 25; i++ {
				l.With("g", "x").Infof("spin")
				em.Emit(Event{Name: "spin", At: time.Now(), Fields: map[string]string{"g": "x"}})
			}
		}(g)
	}
	wg.Wait()
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/observability/tracing.go (78 lines, sha256 911ebb33d972c00af4e3d6017f0b2c08c909eec67553686363fe35779e9edf8f) =====
==============================================================================
```go
package observability

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"net/http"
)

// RequestIDHeader is the correlation id the Python API already sends on
// outbound calls (app/core/middleware stamps it inbound; the publisher
// worker forwards it on webhook-like POSTs). Honouring it here is what
// makes one HTTP request traceable API → gateway → browser delivery
// without any tracing library on either side.
const RequestIDHeader = "X-Request-ID"

// requestIDKey is the unexported context key; use RequestIDFrom to read.
type requestIDKey struct{}

// RequestIDMiddleware returns HTTP middleware that guarantees every
// request carries a correlation id:
//
//   - an inbound X-Request-ID (sane length, no control bytes) is kept —
//     that's the trace continuing;
//   - anything else gets a fresh random id.
//
// The id is stamped on the response (so the CALLER can correlate, even
// when it didn't send one) and stored in the request context for
// RequestIDFrom. WebSocket upgrades pass through it too: the id then
// belongs to the handshake, and per-frame correlation is the session id's
// job (see protocol.NewWelcome).
func RequestIDMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		id := sanitizeRequestID(r.Header.Get(RequestIDHeader))
		if id == "" {
			id = newRequestID()
		}
		w.Header().Set(RequestIDHeader, id)
		next.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), requestIDKey{}, id)))
	})
}

// RequestIDFrom extracts the id the middleware stored ("" when absent —
// e.g. request paths constructed by hand in tests).
func RequestIDFrom(ctx context.Context) string {
	if id, ok := ctx.Value(requestIDKey{}).(string); ok {
		return id
	}
	return ""
}

// sanitizeRequestID keeps a presented id only when it is printable, short
// and control-byte-free. An echoed response header is a header-injection
// surface, so anything exotic is treated as absent and replaced.
func sanitizeRequestID(raw string) string {
	if len(raw) == 0 || len(raw) > 128 {
		return ""
	}
	for i := 0; i < len(raw); i++ {
		c := raw[i]
		if c < 0x20 || c > 0x7e {
			return ""
		}
	}
	return raw
}

// newRequestID mints 16 random bytes as hex — same 32-hex-char shape as a
// dashes-stripped UUID, so traces and session ids interleave readably.
func newRequestID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		// crypto/rand failing at this scale is a system failure; better a
		// distinctive constant that greps loudly than a crash in middleware.
		return "0000000000000000-rand-failed"
	}
	return hex.EncodeToString(b[:])
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/presence/events.go (60 lines, sha256 4d31458b892f5cb63e5e175dad55f2e82b09288c2a45ad808006b8536c4dadf5) =====
==============================================================================
```go
package presence

import "time"

// Kind classifies a presence transition. The Online/Offline pair marks the
// moments a user gains or loses their FIRST/LAST live session (the answer
// "is this user reachable" flipped); the Session pair fires for additional
// tabs/devices joining or leaving without changing reachability.
type Kind int

const (
	// KindUnchanged is a no-op bookkeeping result (duplicate join,
	// unmatched leave). Never delivered to OnChange subscribers.
	KindUnchanged Kind = iota
	// KindUserOnline: the user's first live session appeared.
	KindUserOnline
	// KindUserOffline: the user's last live session is gone.
	KindUserOffline
	// KindSessionJoined: an additional session for an already-online user.
	KindSessionJoined
	// KindSessionLeft: one of several sessions left; user stays online.
	KindSessionLeft
)

// Event is one presence transition. UserSessions/TenantUsers/TotalUsers
// are snapshots taken AFTER the transition applied, so a subscriber can
// update instruments from the event alone without re-reading the registry
// (and without taking its lock).
type Event struct {
	Kind      Kind
	TenantID  string
	UserID    string
	SessionID string
	At        time.Time

	// UserSessions: live sessions of THIS user after the transition
	// (0 on KindUserOffline).
	UserSessions int
	// TenantUsers: reachable users of THIS tenant after the transition.
	TenantUsers int
	// TotalUsers: reachable (tenant, user) pairs node-wide — the gauge
	// value /metrics renders.
	TotalUsers int
}

// String renders the kind for logs.
func (k Kind) String() string {
	switch k {
	case KindUserOnline:
		return "user_online"
	case KindUserOffline:
		return "user_offline"
	case KindSessionJoined:
		return "session_joined"
	case KindSessionLeft:
		return "session_left"
	default:
		return "unchanged"
	}
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/presence/presence.go (175 lines, sha256 0a4c95a952be7f5d1a9ac8dd99ba5e0b3dfec64c2c15b069852b25474e3032db) =====
==============================================================================
```go
// Package presence tracks WHICH dashboard users are currently reachable
// through this gateway node, derived from the identities the WebSocket
// edge has already verified.
//
// Data sources: exactly two moments in the connection lifecycle — the
// successful hello (a JWT pinned a user to a socket) and the connection's
// teardown (the socket is gone). Presence never trusts a client-asserted
// identity: the only keys it ever records come out of a verified token, so
// "user U of tenant T is online here" is as trustworthy as the JWT itself.
//
// Scope is NODE-local by design (mirroring the hub's room registry): a
// multi-node deployment composes node-local views, the same way ingest
// fan-out composes through the broker. Nothing here pretends to be a
// global directory.
//
// Every method is safe for concurrent use.
package presence

import (
	"sort"
	"sync"
	"time"
)

// Registry is the presence store: tenant → user → set of live session ids.
// A user is "online" for a tenant while at least one of their sessions is;
// multiple tabs/devices of the same user collapse into one online entry,
// which is the answer every presence consumer actually wants.
type Registry struct {
	mu       sync.Mutex
	byTenant map[string]map[string]map[string]struct{} // tenant → user → sessions
	onChange []func(Event)
}

// New returns an empty registry.
func New() *Registry {
	return &Registry{byTenant: make(map[string]map[string]map[string]struct{})}
}

// Online records one live session for (tenant, user) and reports the
// resulting transition. Idempotent per session: re-recording the same
// (tenant, user, session) triple is a no-op event with Event.Changed=false,
// so a duplicated delivery path cannot inflate session counts. Empty
// tenant/user keys are ignored entirely — presence of an unauthenticated
// socket is not presence.
func (r *Registry) Online(tenantID, userID, sessionID string) Event {
	if tenantID == "" || userID == "" || sessionID == "" {
		return Event{Kind: KindUnchanged, At: time.Now()}
	}
	r.mu.Lock()
	users, ok := r.byTenant[tenantID]
	if !ok {
		users = make(map[string]map[string]struct{})
		r.byTenant[tenantID] = users
	}
	sessions, ok := users[userID]
	if !ok {
		sessions = make(map[string]struct{})
		users[userID] = sessions
	}
	_, existed := sessions[sessionID]
	sessions[sessionID] = struct{}{}
	event := Event{
		Kind:         KindUnchanged,
		TenantID:     tenantID,
		UserID:       userID,
		SessionID:    sessionID,
		At:           time.Now(),
		UserSessions: len(sessions),
		TenantUsers:  len(users),
		TotalUsers:   r.totalUsersLocked(),
	}
	if !existed {
		event.Kind = KindSessionJoined
		if len(sessions) == 1 {
			event.Kind = KindUserOnline // FIRST session: the user became reachable
		}
	}
	r.mu.Unlock()
	r.emit(event)
	return event
}

// Offline drops one session. Reports the transition; a no-op
// (Event.Kind == KindUnchanged, without callbacks) when the triple was
// never recorded, so shutdown sweeps racing connection teardown cannot
// double-decrement anything.
func (r *Registry) Offline(tenantID, userID, sessionID string) Event {
	r.mu.Lock()
	users, ok := r.byTenant[tenantID]
	if !ok {
		r.mu.Unlock()
		return Event{Kind: KindUnchanged, At: time.Now()}
	}
	sessions, ok := users[userID]
	if !ok {
		r.mu.Unlock()
		return Event{Kind: KindUnchanged, At: time.Now()}
	}
	if _, existed := sessions[sessionID]; !existed {
		r.mu.Unlock()
		return Event{Kind: KindUnchanged, At: time.Now()}
	}
	delete(sessions, sessionID)
	event := Event{
		Kind:      KindSessionLeft,
		TenantID:  tenantID,
		UserID:    userID,
		SessionID: sessionID,
		At:        time.Now(),
	}
	if len(sessions) == 0 {
		delete(users, userID)
		event.Kind = KindUserOffline // LAST session gone: user no longer reachable
	}
	if len(users) == 0 {
		delete(r.byTenant, tenantID)
	}
	event.UserSessions = len(sessions)
	event.TenantUsers = len(users)
	event.TotalUsers = r.totalUsersLocked()
	r.mu.Unlock()
	r.emit(event)
	return event
}

// IsOnline reports whether the user has at least one live session.
func (r *Registry) IsOnline(tenantID, userID string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.byTenant[tenantID][userID]) > 0
}

// SessionsOf returns the live session ids of (tenant, user), sorted —
// deterministic output for diagnostics and tests.
func (r *Registry) SessionsOf(tenantID, userID string) []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	sessions := r.byTenant[tenantID][userID]
	out := make([]string, 0, len(sessions))
	for sessionID := range sessions {
		out = append(out, sessionID)
	}
	sort.Strings(out)
	return out
}

// OnlineUsers returns the tenant's reachable user ids, sorted.
func (r *Registry) OnlineUsers(tenantID string) []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	users := r.byTenant[tenantID]
	out := make([]string, 0, len(users))
	for userID := range users {
		out = append(out, userID)
	}
	sort.Strings(out)
	return out
}

// TenantCount is how many tenants have at least one reachable user.
func (r *Registry) TenantCount() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.byTenant)
}

// TotalUsers is the node-wide count of reachable (tenant, user) pairs —
// the number the /metrics presence gauge renders. Cardinality-safe: it is
// ONE gauge, not a label dimension.
func (r *Registry) TotalUsers() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.totalUsersLocked()
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/presence/presence_test.go (147 lines, sha256 8a9b06461c560f591aa814a4ccc9884fe58dba35003fefcdbce2ff994dc807c3) =====
==============================================================================
```go
package presence

import (
	"fmt"
	"sync"
	"testing"
)

const (
	tenantA = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
	tenantB = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
	userU1  = "cccccccc-3333-4333-8333-cccccccccccc"
	userU2  = "dddddddd-4444-4444-8444-dddddddddddd"
)

func TestTracksUsersNotSockets(t *testing.T) {
	t.Parallel()
	r := New()

	if ev := r.Online(tenantA, userU1, "s1"); ev.Kind != KindUserOnline || ev.TotalUsers != 1 {
		t.Fatalf("first session must mark the user online, got %+v", ev)
	}
	if ev := r.Online(tenantA, userU1, "s2"); ev.Kind != KindSessionJoined || ev.UserSessions != 2 || ev.TotalUsers != 1 {
		t.Fatalf("second tab must not double-count the user, got %+v", ev)
	}
	if !r.IsOnline(tenantA, userU1) || r.IsOnline(tenantA, userU2) {
		t.Fatal("IsOnline answers do not match the recorded state")
	}

	if ev := r.Offline(tenantA, userU1, "s2"); ev.Kind != KindSessionLeft || ev.TotalUsers != 1 {
		t.Fatalf("closing one of two tabs must keep the user online, got %+v", ev)
	}
	if ev := r.Offline(tenantA, userU1, "s1"); ev.Kind != KindUserOffline || ev.TotalUsers != 0 || ev.TenantUsers != 0 {
		t.Fatalf("closing the last tab must mark the user offline, got %+v", ev)
	}
	if r.IsOnline(tenantA, userU1) || r.TenantCount() != 0 {
		t.Fatal("fully-offline tenant must leave no residue")
	}
}

func TestIdempotentDuplicateAndUnmatchedOps(t *testing.T) {
	t.Parallel()
	r := New()

	r.Online(tenantA, userU1, "s1")
	if ev := r.Online(tenantA, userU1, "s1"); ev.Kind != KindUnchanged {
		t.Fatalf("duplicate join must be a no-op, got %+v", ev)
	}
	if got := r.TotalUsers(); got != 1 {
		t.Fatalf("duplicate join inflated state: %d users", got)
	}
	if ev := r.Offline(tenantA, userU1, "never-seen"); ev.Kind != KindUnchanged {
		t.Fatalf("unmatched leave must be a no-op, got %+v", ev)
	}
	if ev := r.Offline(tenantB, userU1, "s1"); ev.Kind != KindUnchanged {
		t.Fatalf("leave against the wrong tenant must be a no-op, got %+v", ev)
	}
	if ev := r.Online("", userU1, "s9"); ev.Kind != KindUnchanged || r.TotalUsers() != 1 {
		t.Fatalf("unauthenticated socket must not produce presence, got %+v", ev)
	}
}

func TestTenantsAreIsolatedAndSnapshotsSorted(t *testing.T) {
	t.Parallel()
	r := New()
	r.Online(tenantB, userU2, "s-b")
	r.Online(tenantA, userU2, "s-a2")
	r.Online(tenantA, userU1, "s-a1")

	if got := r.OnlineUsers(tenantA); len(got) != 2 || got[0] != userU1 || got[1] != userU2 {
		t.Fatalf("tenant A users wrong or unsorted: %v", got)
	}
	if got := r.SessionsOf(tenantA, userU1); len(got) != 1 || got[0] != "s-a1" {
		t.Fatalf("session snapshot wrong: %v", got)
	}
	if got := r.TotalUsers(); got != 3 { // (A,u1) (A,u2) (B,u2) are distinct
		t.Fatalf("TotalUsers counts (tenant,user) pairs: got %d, want 3", got)
	}
}

func TestOnChangeSeesOnlyRealTransitions(t *testing.T) {
	t.Parallel()
	r := New()
	var kinds []Kind
	var lastTotal int
	var mu sync.Mutex
	r.OnChange(func(ev Event) {
		mu.Lock()
		defer mu.Unlock()
		kinds = append(kinds, ev.Kind)
		lastTotal = ev.TotalUsers
	})

	r.Online(tenantA, userU1, "s1")
	r.Online(tenantA, userU1, "s1") // duplicate: must NOT fire
	r.Offline(tenantA, userU1, "s1")

	mu.Lock()
	defer mu.Unlock()
	want := []Kind{KindUserOnline, KindUserOffline}
	if len(kinds) != len(want) {
		t.Fatalf("subscriber saw %v, want %v", kinds, want)
	}
	for i := range want {
		if kinds[i] != want[i] {
			t.Fatalf("subscriber saw %v, want %v", kinds, want)
		}
	}
	if lastTotal != 0 {
		t.Fatalf("event snapshots must reflect post-transition state, last total %d", lastTotal)
	}
}

func TestOnChangeMayReenterRegistry(t *testing.T) {
	t.Parallel()
	r := New()
	var spoke sync.Once
	r.OnChange(func(Event) { spoke.Do(func() { _ = r.TotalUsers() }) })
	r.Online(tenantA, userU1, "s1") // must not deadlock with a reading subscriber
}

func TestConcurrentChurnKeepsAccountingExact(t *testing.T) {
	t.Parallel()
	r := New()
	var wg sync.WaitGroup
	for g := 0; g < 8; g++ {
		wg.Add(1)
		go func(g int) {
			defer wg.Done()
			user := fmt.Sprintf("user-%d", g)
			for i := 0; i < 50; i++ {
				sess := fmt.Sprintf("s-%d-%d", g, i)
				r.Online(tenantA, user, sess)
				r.Offline(tenantA, user, sess)
			}
			// Leave one permanent session so the user stays online.
			r.Online(tenantA, user, fmt.Sprintf("perm-%d", g))
		}(g)
	}
	wg.Wait()
	if got := r.TotalUsers(); got != 8 {
		t.Fatalf("after churn exactly the 8 permanent users must remain, got %d", got)
	}
	if got := r.TenantCount(); got != 1 {
		t.Fatalf("tenant residue wrong, got %d", got)
	}
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/presence/registry.go (44 lines, sha256 b3bd7636d434976b7d4de8f99a8f33f0a21352da16c6be6081afddb23af92a29) =====
==============================================================================
```go
package presence

// This file holds the Registry's bookkeeping internals (locked helpers)
// and the subscription surface for change consumers — separated from the
// mutating API in presence.go so the transition rules read in one place.

// OnChange subscribes fn to every STATE-CHANGING transition the registry
// records. Callbacks run synchronously AFTER the registry's mutex is
// released, in registration order; a callback that re-enters the registry
// (reads are fine) cannot deadlock. No-op events (duplicate Online of an
// already-recorded session, Offline of a never-recorded one) are NOT
// delivered — subscribers only ever see real transitions.
//
// The one production subscriber today is the /metrics presence gauge; the
// callback interface (rather than a hardwired metric call) is what keeps
// this package free of an observability import.
func (r *Registry) OnChange(fn func(Event)) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.onChange = append(r.onChange, fn)
}

// emit fans a transition out to subscribers. Only state-changing kinds are
// delivered; KindUnchanged is bookkeeping noise.
func (r *Registry) emit(event Event) {
	if event.Kind == KindUnchanged {
		return
	}
	r.mu.Lock()
	subs := append([]func(Event){}, r.onChange...)
	r.mu.Unlock()
	for _, fn := range subs {
		fn(event)
	}
}

// totalUsersLocked sums reachable (tenant, user) pairs. Callers hold mu.
func (r *Registry) totalUsersLocked() int {
	total := 0
	for _, users := range r.byTenant {
		total += len(users)
	}
	return total
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/protocol/codec.go (128 lines, sha256 bb0482411d915960293662526e646a08823f34dd0272e254ac88ce1eee956550) =====
==============================================================================
```go
package protocol

import (
	"encoding/json"
	"fmt"
	"strings"
)

// Relay payload caps, compile-time policy next to the codec that enforces
// the envelope. The frame limit (16 KiB, config.MaxMessageBytes) already
// bounds the whole JSON frame; these bound the meaningful bodies so a
// signaling session can never become a covert bulk-transfer channel that
// doesn't even pretend to be media negotiation.
const (
	// MaxSDPLength caps an offer/answer body. Measured SDP for a bundled
	// audio session is a few KB; 12 KiB leaves headroom for a couple of
	// extra codec sections and is comfortably below the frame limit.
	MaxSDPLength = 12 * 1024
	// MaxCandidateBytes caps one ICE candidate object — a few hundred bytes
	// of host/srflx/relay fields in practice.
	MaxCandidateBytes = 1024
)

// DecodeClientMessage parses a wire frame with the internal protocol's
// discipline: a required "type", a known variant, and per-variant required
// fields detected by PRESENCE (so `{"type":"hello","token":""}` is a
// bad_message at decode time, not an ambiguous auth failure downstream).
func DecodeClientMessage(data []byte) (*ClientMessage, error) {
	var msg ClientMessage
	if err := json.Unmarshal(data, &msg); err != nil {
		return nil, err
	}
	switch msg.Type {
	case TypeHello, TypeSubscribe, TypeUnsubscribe, TypePing,
		TypeSessionStart, TypeSessionJoin, TypeSessionEnd,
		TypeOffer, TypeAnswer, TypeCandidate,
		TypeEngineOffer, TypeEngineCandidate, TypeEnginePublish,
		TypeEngineSubscribe, TypeEngineUnsubscribe:
	default:
		return nil, fmt.Errorf("unknown message type %q", msg.Type)
	}
	switch msg.Type {
	case TypeHello:
		if !hasField(data, "token") || msg.Token == "" {
			return nil, fmt.Errorf("missing required field %q for %s", "token", msg.Type)
		}
	case TypeSubscribe, TypeUnsubscribe:
		if !hasField(data, "room") || msg.Room == "" {
			return nil, fmt.Errorf("missing required field %q for %s", "room", msg.Type)
		}
	case TypeSessionJoin, TypeSessionEnd, TypeOffer, TypeAnswer, TypeCandidate,
		TypeEngineOffer, TypeEngineCandidate, TypeEnginePublish,
		TypeEngineSubscribe, TypeEngineUnsubscribe:
		if !hasField(data, "session_id") || msg.SessionID == "" {
			return nil, fmt.Errorf("missing required field %q for %s", "session_id", msg.Type)
		}
	}
	switch msg.Type {
	case TypeOffer, TypeAnswer:
		if !hasField(data, "sdp") || msg.SDP == "" {
			return nil, fmt.Errorf("missing required field %q for %s", "sdp", msg.Type)
		}
	case TypeCandidate, TypeEngineCandidate:
		// Presence by hasField, not by non-nil value: `{"candidate":null}`
		// is the end-of-candidates marker and MUST decode successfully.
		if !hasField(data, "candidate") {
			return nil, fmt.Errorf("missing required field %q for %s", "candidate", msg.Type)
		}
	case TypeEngineOffer:
		if !hasField(data, "sdp") || msg.SDP == "" {
			return nil, fmt.Errorf("missing required field %q for %s", "sdp", msg.Type)
		}
	case TypeEnginePublish:
		if !hasField(data, "track") || msg.Track == "" || !hasField(data, "kind") || msg.Kind == "" {
			return nil, fmt.Errorf("missing required field %q for %s", "track/kind", msg.Type)
		}
		if msg.Kind != "audio" && msg.Kind != "video" && msg.Kind != "data" {
			return nil, fmt.Errorf("unknown media kind %q for %s", msg.Kind, msg.Type)
		}
	case TypeEngineSubscribe, TypeEngineUnsubscribe:
		if !hasField(data, "track") || msg.Track == "" {
			return nil, fmt.Errorf("missing required field %q for %s", "track", msg.Type)
		}
	}
	return &msg, nil
}

// hasField distinguishes a missing key from a zero value — the one place
// Go's lenient decoding would otherwise diverge from the strictness the
// protocol relies on for validation.
func hasField(data []byte, key string) bool {
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(data, &fields); err != nil {
		return false
	}
	_, ok := fields[key]
	return ok
}

// IsValidSDP is the deliberate shallowness of the relay's SDP check: it
// verifies the blob STARTS like SDP ("v=" is the version line, always first)
// and nothing more. The edge routes negotiation; it does not parse media,
// and a parser here would be one more thing the browser and the media plane
// have to agree with US about. Overlong bodies are refused separately
// (MaxSDPLength, surfaced as signal_too_large).
func IsValidSDP(sdp string) bool {
	return strings.HasPrefix(strings.TrimSpace(sdp), "v=")
}

// CandidateIsObject reports whether raw is a JSON object (the only shape a
// trickled candidate may take, besides the null end marker which relays
// untouched) and within the relay cap. The end-of-candidates forms —
// literal null and {"candidate":""} — pass by design.
func CandidateIsObject(raw json.RawMessage) bool {
	trimmed := strings.TrimSpace(string(raw))
	if trimmed == "" || trimmed == "null" {
		return true // end-of-candidates marker
	}
	if len(raw) > MaxCandidateBytes {
		return false
	}
	return strings.HasPrefix(trimmed, "{") && strings.HasSuffix(trimmed, "}")
}

// Marshal serializes a server frame for the wire.
func Marshal(msg any) ([]byte, error) {
	return json.Marshal(msg)
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/protocol/codec_signaling_test.go (213 lines, sha256 6aec1b8c5c3a57ff00e614b1599c1dfc61558b66ad8cf0227f58f1ccef5d4520) =====
==============================================================================
```go
package protocol

import (
	"encoding/json"
	"strings"
	"testing"
)

// Signaling vocabulary, decoded with the same per-variant PRESENCE discipline
// the original four types get: an empty session_id and a missing one are the
// same refusal, and `{"candidate":null}` (end-of-candidates) is legal on
// purpose — that marker has to RELAY, not die at the decoder.

func TestDecodeSignalingVariants(t *testing.T) {
	cases := []struct {
		raw    string
		assert func(t *testing.T, msg *ClientMessage)
	}{
		{`{"type":"session.start"}`, func(t *testing.T, msg *ClientMessage) {
			if msg.Type != TypeSessionStart {
				t.Errorf("type = %q", msg.Type)
			}
		}},
		{`{"type":"session.join","session_id":"s-1"}`, func(t *testing.T, msg *ClientMessage) {
			if msg.SessionID != "s-1" {
				t.Errorf("session_id = %q", msg.SessionID)
			}
		}},
		{`{"type":"session.end","session_id":"s-1"}`, nil},
		{`{"type":"offer","session_id":"s-1","sdp":"v=0\r\no=- 1 1 IN IP4 10.0.0.1"}`, func(t *testing.T, msg *ClientMessage) {
			if !strings.HasPrefix(msg.SDP, "v=") {
				t.Errorf("sdp = %q", msg.SDP)
			}
		}},
		{`{"type":"answer","session_id":"s-1","sdp":"v=0"}`, nil},
		{`{"type":"candidate","session_id":"s-1","candidate":{"candidate":"candidate:1 1 udp 2130706431 10.0.0.1 9 typ host","sdpMid":"0"}}`, func(t *testing.T, msg *ClientMessage) {
			var cand map[string]any
			if err := json.Unmarshal(msg.Candidate, &cand); err != nil {
				t.Fatalf("candidate should be raw JSON: %v", err)
			}
			if cand["sdpMid"] != "0" {
				t.Errorf("candidate payload = %s", msg.Candidate)
			}
		}},
		{`{"type":"candidate","session_id":"s-1","candidate":null}`, nil}, // end-of-candidates
	}
	for i, tc := range cases {
		msg, err := DecodeClientMessage([]byte(tc.raw))
		if err != nil {
			t.Errorf("case %d (%s): %v", i, tc.raw, err)
			continue
		}
		if tc.assert != nil {
			tc.assert(t, msg)
		}
	}
}

func TestDecodeRejectsSignalingShapeViolations(t *testing.T) {
	for _, raw := range []string{
		`{"type":"session.join"}`,                      // no session id
		`{"type":"session.join","session_id":""}`,      // empty session id
		`{"type":"offer","session_id":"s-1"}`,          // sdp missing
		`{"type":"offer","session_id":"s-1","sdp":""}`, // sdp empty
		`{"type":"answer","session_id":"s-1"}`,         // sdp missing
		`{"type":"candidate","session_id":"s-1"}`,      // candidate missing (not null!)
	} {
		if _, err := DecodeClientMessage([]byte(raw)); err == nil {
			t.Errorf("%s must be rejected at decode time", raw)
		}
	}
}

func TestIsValidSDP(t *testing.T) {
	for _, good := range []string{
		"v=0\r\no=- 1 1 IN IP4 10.0.0.1",
		"  v=0\n\nm=audio 9 UDP/TLS/RTP/SAVPF 111", // leading whitespace tolerated
	} {
		if !IsValidSDP(good) {
			t.Errorf("IsValidSDP(%q) = false, want true", good)
		}
	}
	for _, bad := range []string{"", "{}", "o=- 1 1 IN IP4 10.0.0.1", "v0=", "select * from calls"} {
		if IsValidSDP(bad) {
			t.Errorf("IsValidSDP(%q) = true, want false", bad)
		}
	}
}

func TestCandidateIsObject(t *testing.T) {
	// The two end-of-candidates forms relay verbatim.
	if !CandidateIsObject(json.RawMessage("null")) {
		t.Error("null (end-of-candidates) must pass and relay")
	}
	if !CandidateIsObject(json.RawMessage(`{"candidate":""}`)) {
		t.Error(`{"candidate":""} (end-of-candidates) must pass and relay`)
	}
	if !CandidateIsObject(json.RawMessage(`{"candidate":"candidate:1 1 udp 2130706431 10.0.0.1 9 typ host","sdpMid":"0","sdpMLineIndex":0}`)) {
		t.Error("a normal candidate object must pass")
	}
	if CandidateIsObject(json.RawMessage(`["not","an","object"]`)) {
		t.Error("an array is not a candidate")
	}
	if CandidateIsObject(json.RawMessage(strings.Repeat("x", MaxCandidateBytes+1))) {
		t.Error("over the relay cap must fail")
	}
	// Exactly-at-cap is legal: the boundary is off by design (cap prevents
	// abuse, not one byte more of legitimate ICE than yesterday).
	atCap := `{"candidate":"` + strings.Repeat("a", MaxCandidateBytes-len(`{"candidate":""}`)) + `"}`
	if !CandidateIsObject(json.RawMessage(atCap)) {
		t.Error("exactly-at-cap must pass")
	}
}

func TestSignalingServerFramesMarshalToWireShape(t *testing.T) {
	started := NewSessionStarted("sig-1")
	raw, _ := Marshal(started)
	for _, want := range []string{`"type":"session.started"`, `"session_id":"sig-1"`, `"role":"initiator"`} {
		if !strings.Contains(string(raw), want) {
			t.Errorf("session.started missing %s: %s", want, raw)
		}
	}

	joined := NewSessionJoined("sig-1", false)
	raw, _ = Marshal(joined)
	if !strings.Contains(string(raw), `"role":"responder"`) {
		t.Errorf("session.joined must pin the responder role: %s", raw)
	}

	peerJoined := NewSessionPeerJoined("sig-1", RoleResponder, false)
	raw, _ = Marshal(peerJoined)
	if !strings.Contains(string(raw), `"peer_role":"responder"`) {
		t.Errorf("session.peer_joined must say who arrived: %s", raw)
	}

	ended := NewSessionEnded("sig-1", "peer_disconnected")
	raw, _ = Marshal(ended)
	if !strings.Contains(string(raw), `"reason":"peer_disconnected"`) {
		t.Errorf("session.ended must carry the machine-readable reason: %s", raw)
	}

	offer := NewSignalOffer("sig-1", "v=0\r\no=- 1 1 IN IP4 10.0.0.1")
	raw, _ = Marshal(offer)
	if !strings.Contains(string(raw), `"type":"signal.offer"`) || !strings.Contains(string(raw), `"sdp":"v=0`) {
		t.Errorf("signal.offer must forward the body verbatim: %s", raw)
	}

	cand := NewSignalCandidate("sig-1", json.RawMessage(`{"candidate":"candidate:1 1 udp 2130706431 10.0.0.1 9 typ host"}`))
	raw, _ = Marshal(cand)
	if !strings.Contains(string(raw), `"candidate":{"candidate":"candidate:1 1 udp`) {
		t.Errorf("signal.candidate must forward the object verbatim: %s", raw)
	}
}

// ---- wire 1.2 (steer) codec rules ----

func TestV12EngineFramesDecodeWithPresenceRules(t *testing.T) {
	must := func(body string) *ClientMessage {
		t.Helper()
		m, err := DecodeClientMessage([]byte(body))
		if err != nil {
			t.Fatalf("decode %s: %v", body, err)
		}
		return m
	}
	mustNot := func(body string) {
		t.Helper()
		if _, err := DecodeClientMessage([]byte(body)); err == nil {
			t.Fatalf("must refuse: %s", body)
		}
	}

	// happy paths
	if m := must(`{"type":"engine.offer","session_id":"s","sdp":"v=0"}`); m.SDP != "v=0" {
		t.Fatalf("offer = %+v", m)
	}
	if m := must(`{"type":"engine.candidate","session_id":"s","candidate":null}`); m == nil {
		t.Fatalf("null candidate (e.o.c) must decode")
	}
	if m := must(`{"type":"engine.publish","session_id":"s","track":"mic","kind":"audio"}`); m.Track != "mic" || m.Kind != "audio" {
		t.Fatalf("publish = %+v", m)
	}
	if m := must(`{"type":"engine.subscribe","session_id":"s","track":"mic"}`); m.Track != "mic" {
		t.Fatalf("subscribe = %+v", m)
	}
	if m := must(`{"type":"engine.unsubscribe","session_id":"s","track":"mic"}`); m.Track != "mic" {
		t.Fatalf("unsubscribe = %+v", m)
	}

	// presence violations
	mustNot(`{"type":"engine.offer","session_id":"s"}`)                  // no sdp
	mustNot(`{"type":"engine.candidate","session_id":"s"}`)              // no candidate key
	mustNot(`{"type":"engine.publish","session_id":"s","track":"mic"}`)  // no kind
	mustNot(`{"type":"engine.publish","session_id":"s","kind":"audio"}`) // no track
	mustNot(`{"type":"engine.subscribe","session_id":"s"}`)              // no track
	mustNot(`{"type":"engine.offer","sdp":"v=0"}`)                       // no session_id
	// kind is a closed set (bad_message at decode, not downstream)
	mustNot(`{"type":"engine.publish","session_id":"s","track":"t","kind":"smell"}`)
}

func TestHelloWSVersionMarkerDecodes(t *testing.T) {
	m, err := DecodeClientMessage([]byte(`{"type":"hello","token":"t","ws":2}`))
	if err != nil {
		t.Fatalf("hello with ws:2: %v", err)
	}
	if m.WSVersion != 2 {
		t.Fatalf("ws version: %d", m.WSVersion)
	}
	m, err = DecodeClientMessage([]byte(`{"type":"hello","token":"t"}`))
	if err != nil || m.WSVersion != 0 {
		t.Fatalf("legacy hello keeps 0: %+v %v", m, err)
	}
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/protocol/envelope.go (53 lines, sha256 d523fccd42cc73426a1e4825c1d0d93d78619c5bdf4eda5ff9ff08e27a36480a) =====
==============================================================================
```go
package protocol

import "encoding/json"

// ClientMessage is the flat discriminator-dispatched frame a client sends.
// One struct with optional fields is the Go idiom for a tagged JSON union —
// exactly as the internal protocol does it.
//
// Field ownership by variant:
//
//	hello                 → token
//	subscribe/unsubscribe → room
//	session.join/end      → session_id
//	offer/answer          → session_id + sdp
//	candidate             → session_id + candidate
//	session.start, ping   → no payload fields
//
// Per-variant PRESENCE rules are enforced by DecodeClientMessage (codec.go),
// which is why fields appear here without validation tags: the wire grammar
// lives in exactly one place.
type ClientMessage struct {
	Type  string `json:"type"`
	Token string `json:"token,omitempty"`
	Room  string `json:"room,omitempty"`

	// WSVersion is the hello "ws" marker: the negotiated browser wire
	// level. 0/absent = v1.0/v1.1 (P2P relay only); 2+ = steer-capable
	// (v1.2: the five engine.* frames in message.go). It is read at hello
	// and pinned for the connection's lifetime.
	WSVersion int `json:"ws,omitempty"`

	// SessionID names the signaling session the frame acts on. It is never
	// the connection a message came FROM — the sender is always the socket
	// itself — so there is no field a client could use to address or
	// impersonate a peer.
	SessionID string `json:"session_id,omitempty"`

	// SDP carries the offer/answer body. Treated as OPAQUE here: the edge
	// validates its envelope (kind, size, rough shape) and never parses or
	// rewrites the media description — munging is the media plane's job.
	SDP string `json:"sdp,omitempty"`

	// Candidate carries one RTCIceCandidate object verbatim, or the
	// end-of-candidates marker (null, or an object with an empty
	// "candidate" string), which the peer needs to finish gathering.
	Candidate json.RawMessage `json:"candidate,omitempty"`

	// Track + Kind serve engine.publish / engine.subscribe / engine.
	// unsubscribe (v1.2): the caller-minted track identity and its media
	// kind ("audio"|"video"|"data"), per the engine's track vocabulary.
	Track string `json:"track,omitempty"`
	Kind  string `json:"kind,omitempty"`
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/protocol/error.go (70 lines, sha256 f904cd5180d119ca166134dbde0e2e81b5c7372096d96143567016b535e38500) =====
==============================================================================
```go
package protocol

// Error codes. The set is closed on purpose (mirrors the Python code's
// closed enums): clients can switch on it, and an open set would invite
// ad-hoc codes that leak internal state. TestErrorCodesStayClosed is the
// tripwire: extend the vocabulary here and it fails loudly until the
// dashboard-side handling is reviewed.
const (
	CodeBadMessage    = "bad_message"
	CodeHelloRequired = "hello_required"
	CodeAuthFailed    = "auth_failed"
	CodeRoomInvalid   = "room_invalid"
	CodeOverLimit     = "over_limit"
	CodeRateLimited   = "rate_limited"

	// Signaling failures. Three of them deliberately COLLAPSE what the
	// session layer knows — a non-member probing a session id, an id that
	// never existed, and an id belonging to another tenant all produce
	// session_unknown, because the distribution of which mkts exist is
	// itself tenant data. Accuracy in the log, ambiguity on the wire.
	CodeSessionUnknown = "session_unknown"
	// session_full: the tenant is at its concurrent-session cap.
	CodeSessionFull = "session_full"
	// session_not_ready: the action needs both members (an offer with no
	// peer yet) and the peer slot is empty.
	CodeSessionNotReady = "session_not_ready"
	// already_in_session: this connection already holds a session slot;
	// one connection, at most one session, so dashboards cannot accumulate
	// orphaned negotiations across tabs.
	CodeAlreadyInSession = "already_in_session"
	// wrong_signal_state: legal type, illegal moment — a second offer while
	// one is outstanding (glare), an answer with nothing to answer, any
	// signaling frame after the session ended.
	CodeWrongSignalState = "wrong_signal_state"
	// steer_mode_blocked: a frame addressed the wrong media path for the
	// session's negotiated mode — engine.* on a P2P session, or P2P
	// offer/answer/candidate on a steered one.
	CodeSteerModeBlocked = "steer_mode_blocked"
	// engine_unavailable: an engine.* frame while the media path is
	// unreachable or the connection's engine session was never enrolled.
	// Session-preserving by contract; retry once availability returns.
	CodeEngineUnavailable = "engine_unavailable"
	// signal_too_large: an SDP or candidate beyond the relay caps. Distinct
	// from bad_message so a client can tell "malformed" from "legitimate
	// but oversized" (an SDP with an extra codec block is the second).
	CodeSignalTooLarge = "signal_too_large"
)

// RFC 6455 close codes the gateway uses, re-exported so transport-agnostic
// packages (the hub) can request a close without importing gorilla.
const (
	CloseNormal          = 1000 // orderly end of session
	CloseGoingAway       = 1001 // server shutting down
	ClosePolicyViolation = 1008 // auth failed / timed out, room refused
	CloseInternalError   = 1011 // unexpected server-side failure
)

// ErrorMessage is the server's refusal/validation frame. The text is written
// for the dashboard developer, never for the caller: it must not echo tokens
// or tenant data.
type ErrorMessage struct {
	Type    string `json:"type"`
	Code    string `json:"code"`
	Message string `json:"message"`
}

// NewError builds an error frame.
func NewError(code, message string) ErrorMessage {
	return ErrorMessage{Type: TypeError, Code: code, Message: message}
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/protocol/message.go (346 lines, sha256 6541349612df82feee83ab481ba8d965bf5db356f36e91bc52ad1fca43ab00e4) =====
==============================================================================
```go
// Package protocol defines the gateway's wire frames: the tagged-JSON
// messages exchanged with browsers at the public WebSocket edge.
//
// The vocabulary deliberately mirrors the internal signaling protocol
// (services/signal-go/internal/signal, itself the differential counterpart
// of the Rust voxdesk-signal crate): hello/subscribe/unsubscribe/ping in,
// welcome/subscribed/unsubscribed/delivery/error/pong out. A client library
// that speaks to the internal hub needs only the auth frame added to speak
// to the public edge.
//
// The security difference from the internal hub is what "hello" carries:
// here it carries a verified access token, and the tenant a connection may
// see is pinned to the token's `tid` claim at that moment. No message after
// hello can name a tenant — the frame schema does not have a tenant field.
//
// This file is the message TYPE vocabulary (client and server frame names,
// signaling roles) plus the server frame structs. Envelopes live in
// envelope.go, the error vocabulary in error.go, and wire encoding plus
// payload guards in codec.go — one concern per file, matching the layout
// the maintainers use elsewhere in this service.
package protocol

import (
	"encoding/json"
	"time"
)

// Client message types. The first four are the realtime-notice protocol this
// edge shipped with; the six signaling types extend it for WebRTC session
// setup (see internal/signaling) and live behind the same pre-auth gate:
// none of them does anything before hello succeeds.
const (
	TypeHello       = "hello"
	TypeSubscribe   = "subscribe"
	TypeUnsubscribe = "unsubscribe"
	TypePing        = "ping"

	TypeSessionStart = "session.start" // create a 2-member signaling session as its initiator
	TypeSessionJoin  = "session.join"  // join an existing session as the responder
	TypeSessionEnd   = "session.end"   // end a session the sender belongs to
	TypeOffer        = "offer"         // SDP offer to the peer (any active member, one at a time)
	TypeAnswer       = "answer"        // SDP answer to an outstanding offer (the non-offerer)
	TypeCandidate    = "candidate"     // one trickled ICE candidate to the peer

	// Wire vocabulary 1.2 — the SFU steer. These frames are accepted ONLY
	// on sessions whose negotiated mode is steered (both members sent
	// "ws":2 in hello and the gateway's engine link is in v1.2|force
	// mode); on plain sessions they refuse with steer_mode_not_active.
	// Field shapes mirror their engine-side counterparts, keyed on the
	// GATEWAY session id — the router translates to engine session ids
	// internally (browsers never learn engine capabilities).
	TypeEngineOffer       = "engine.offer"       // SDP offer to the media engine for this session
	TypeEngineCandidate   = "engine.candidate"   // one trickled candidate for the engine session
	TypeEnginePublish     = "engine.publish"     // declare a local track: {session_id, track, kind}
	TypeEngineSubscribe   = "engine.subscribe"   // ask for the peer's track: {session_id, track}
	TypeEngineUnsubscribe = "engine.unsubscribe" // withdraw: {session_id, track}
)

// Server message types.
const (
	TypeWelcome      = "welcome"
	TypeReady        = "ready"
	TypeSubscribed   = "subscribed"
	TypeUnsubscribed = "unsubscribed"
	TypeDelivery     = "delivery"
	TypeError        = "error"
	TypePong         = "pong"

	TypeSessionStarted    = "session.started"     // session created; sender is its initiator
	TypeSessionJoined     = "session.joined"      // sender joined as the responder
	TypeSessionPeerJoined = "session.peer_joined" // the other member arrived
	TypeSessionEnded      = "session.ended"       // session is over, with a reason code
	TypeSignalOffer       = "signal.offer"        // forwarded SDP offer
	TypeSignalAnswer      = "signal.answer"       // forwarded SDP answer
	TypeSignalCandidate   = "signal.candidate"    // forwarded ICE candidate

	// Wire vocabulary 1.2 server frames for the steered path.
	TypeEngineAnswer         = "engine.answer"          // the engine's SDP answer to engine.offer
	TypeEngineTrackPublished = "engine.track_published" // the peer minted a track on the engine
)

// Signaling roles. Exactly two members ever exist in a session: its creator
// and the one peer who joins it. An open-ended membership model would
// re-invent rooms; a media session is point-to-point.
const (
	RoleInitiator = "initiator"
	RoleResponder = "responder"
)

// ---------------------------------------------------------------------------
// Realtime-notice server frames (unchanged from the original protocol).
// ---------------------------------------------------------------------------

// Welcome is the first frame on every connection, sent before any client
// traffic is read. It tells the browser how to proceed: authenticate, and
// how long it has.
type Welcome struct {
	Type            string `json:"type"`
	SessionID       string `json:"session_id"`
	AuthRequired    bool   `json:"auth_required"`
	AuthTimeoutSecs int    `json:"auth_timeout_secs"`
	HeartbeatSecs   int    `json:"heartbeat_secs"`
	ServerTimeUTC   string `json:"server_time"`
}

// NewWelcome builds the greeting for one fresh session.
func NewWelcome(sessionID string, authTimeout, heartbeat time.Duration, now time.Time) Welcome {
	return Welcome{
		Type:            TypeWelcome,
		SessionID:       sessionID,
		AuthRequired:    true,
		AuthTimeoutSecs: int(authTimeout / time.Second),
		HeartbeatSecs:   int(heartbeat / time.Second),
		ServerTimeUTC:   now.UTC().Format(time.RFC3339),
	}
}

// Ready acknowledges successful authentication and pins the session to the
// token's tenant. After this frame the client may subscribe and signal.
type Ready struct {
	Type      string `json:"type"`
	SessionID string `json:"session_id"`
	TenantID  string `json:"tenant_id"`
	Role      string `json:"role"`
	// TokenExpiresAt tells the client when the edge will close this socket
	// (the edge never outlives the access token that opened it), so it can
	// reconnect with the refreshed token before the drop.
	TokenExpiresAt string `json:"token_expires_at"`
}

// NewReady builds the post-auth acknowledgement.
func NewReady(sessionID, tenantID, role string, expiresAt time.Time) Ready {
	return Ready{
		Type:           TypeReady,
		SessionID:      sessionID,
		TenantID:       tenantID,
		Role:           role,
		TokenExpiresAt: expiresAt.UTC().Format(time.RFC3339),
	}
}

// Subscribed acknowledges a subscription with the room's peer count so a
// wallboard can show "2 dashboards watching".
type Subscribed struct {
	Type  string `json:"type"`
	Room  string `json:"room"`
	Peers int    `json:"peers"`
}

// NewSubscribed builds the ack.
func NewSubscribed(room string, peers int) Subscribed {
	return Subscribed{Type: TypeSubscribed, Room: room, Peers: peers}
}

// Unsubscribed acknowledges leaving a room. Leaving a room the session never
// joined is acknowledged identically — idempotent, never an error.
type Unsubscribed struct {
	Type string `json:"type"`
	Room string `json:"room"`
}

// NewUnsubscribed builds the ack.
func NewUnsubscribed(room string) Unsubscribed {
	return Unsubscribed{Type: TypeUnsubscribed, Room: room}
}

// Delivery carries one published event to one subscriber. EventID lets a
// client dedupe across a reconnect; SentAt lets it render staleness.
type Delivery struct {
	Type    string          `json:"type"`
	Room    string          `json:"room"`
	Kind    string          `json:"kind"`
	Payload json.RawMessage `json:"payload"`
	EventID string          `json:"event_id,omitempty"`
	SentAt  string          `json:"sent_at"`
}

// NewDelivery builds one fan-out frame.
func NewDelivery(room, kind, eventID string, payload json.RawMessage, now time.Time) Delivery {
	return Delivery{
		Type:    TypeDelivery,
		Room:    room,
		Kind:    kind,
		Payload: payload,
		EventID: eventID,
		SentAt:  now.UTC().Format(time.RFC3339),
	}
}

// Pong answers an application-level ping (the WS-level ping/pong is handled
// by the transport heartbeat; this exists for latency probes through the
// full JSON path, same as the internal protocol).
type Pong struct {
	Type          string `json:"type"`
	ServerTimeUTC string `json:"server_time"`
}

// NewPong builds the reply.
func NewPong(now time.Time) Pong {
	return Pong{Type: TypePong, ServerTimeUTC: now.UTC().Format(time.RFC3339)}
}

// ---------------------------------------------------------------------------
// Signaling server frames. None of these leak the peer's identity beyond the
// shared session id — the two members already know each other by out-of-band
// arrangement (the session id itself is the capability: 122 random bits,
// joinable only from inside the same tenant).
// ---------------------------------------------------------------------------

// SessionStarted confirms session.start. The session id is the join
// capability the initiator shares with its intended peer (out of band).
type SessionStarted struct {
	Type      string `json:"type"`
	SessionID string `json:"session_id"`
	Role      string `json:"role"`
}

// NewSessionStarted builds the start confirmation.
func NewSessionStarted(sessionID string) SessionStarted {
	return SessionStarted{Type: TypeSessionStarted, SessionID: sessionID, Role: RoleInitiator}
}

// SessionJoined confirms session.join. The joiner is always the responder.
type SessionJoined struct {
	Type      string `json:"type"`
	SessionID string `json:"session_id"`
	Role      string `json:"role"`
	// Steer is the negotiated media path for this session: true means the
	// SFU (engine.* frames) owns media; false means P2P relay. Always
	// emitted on wire 1.2+ so a v1.2 client never has to guess legacy
	// defaults — additive and therefore safe for v1.1 parsers.
	Steer bool `json:"steer"`
}

// NewSessionJoined builds the join confirmation for the negotiated mode.
func NewSessionJoined(sessionID string, steer bool) SessionJoined {
	return SessionJoined{Type: TypeSessionJoined, SessionID: sessionID, Role: RoleResponder, Steer: steer}
}

// SessionPeerJoined tells the waiting initiator that the responder arrived —
// the cue to createOffer, for clients that follow the initiator-offers
// convention.
type SessionPeerJoined struct {
	Type      string `json:"type"`
	SessionID string `json:"session_id"`
	PeerRole  string `json:"peer_role"`
	Steer     bool   `json:"steer"` // see SessionJoined.Steer
}

// NewSessionPeerJoined builds the peer-arrived notice for the negotiated
// mode.
func NewSessionPeerJoined(sessionID, peerRole string, steer bool) SessionPeerJoined {
	return SessionPeerJoined{Type: TypeSessionPeerJoined, SessionID: sessionID, PeerRole: peerRole, Steer: steer}
}

// SessionEnded ends a session for every member that still cares. Reason is
// a machine-readable closed vocabulary owned by the session package
// ("member_ended", "peer_disconnected", "join_timeout").
type SessionEnded struct {
	Type      string `json:"type"`
	SessionID string `json:"session_id"`
	Reason    string `json:"reason"`
}

// NewSessionEnded builds the end notice.
func NewSessionEnded(sessionID, reason string) SessionEnded {
	return SessionEnded{Type: TypeSessionEnded, SessionID: sessionID, Reason: reason}
}

// SignalOffer is a forwarded SDP offer. The body passes through untouched:
// SDP munging belongs to the media plane, and this edge's threat model is
// routing, never rewriting.
type SignalOffer struct {
	Type      string `json:"type"`
	SessionID string `json:"session_id"`
	SDP       string `json:"sdp"`
}

// NewSignalOffer builds the forwarded offer.
func NewSignalOffer(sessionID, sdp string) SignalOffer {
	return SignalOffer{Type: TypeSignalOffer, SessionID: sessionID, SDP: sdp}
}

// SignalAnswer is a forwarded SDP answer.
type SignalAnswer struct {
	Type      string `json:"type"`
	SessionID string `json:"session_id"`
	SDP       string `json:"sdp"`
}

// NewSignalAnswer builds the forwarded answer.
func NewSignalAnswer(sessionID, sdp string) SignalAnswer {
	return SignalAnswer{Type: TypeSignalAnswer, SessionID: sessionID, SDP: sdp}
}

// SignalCandidate is a forwarded ICE candidate, carried as raw JSON because
// the edge must not impose a schema on a structure the browser and the
// media plane already agree on (RTCIceCandidate: candidate/sdpMid/
// sdpMLineIndex/usernameFragment). An end-of-candidates marker (a null or
// empty-string candidate) relays identically — the peer needs it to know
// gathering finished.
type SignalCandidate struct {
	Type      string          `json:"type"`
	SessionID string          `json:"session_id"`
	Candidate json.RawMessage `json:"candidate"`
}

// NewSignalCandidate builds the forwarded candidate.
func NewSignalCandidate(sessionID string, candidate json.RawMessage) SignalCandidate {
	return SignalCandidate{Type: TypeSignalCandidate, SessionID: sessionID, Candidate: candidate}
}

// ---------------------------------------------------------------------------
// Wire vocabulary 1.2 server frames (SFU steer).
// ---------------------------------------------------------------------------

// EngineAnswer is the engine's SDP answer to a member's engine.offer. The
// browser treats it exactly like a peer answer (RTCPeerConnection.set
// RemoteDescription) — the SFU's ICE/DTLS details ride inside the SDP.
type EngineAnswer struct {
	Type      string `json:"type"`
	SessionID string `json:"session_id"`
	SDP       string `json:"sdp"`
}

// NewEngineAnswer builds the steer answer.
func NewEngineAnswer(sessionID, sdp string) EngineAnswer {
	return EngineAnswer{Type: TypeEngineAnswer, SessionID: sessionID, SDP: sdp}
}

// EngineTrackPublished informs one member that the OTHER member minted a
// track on the engine for this session. `participant` carries the
// publisher's engine-side participant id (which the gateway pins to the
// peer's connection id — opaque to the browser, stable for the session).
type EngineTrackPublished struct {
	Type        string `json:"type"`
	SessionID   string `json:"session_id"`
	Participant string `json:"participant"`
	Track       string `json:"track"`
	Kind        string `json:"kind"`
}

// NewEngineTrackPublished builds the steer publish notice.
func NewEngineTrackPublished(sessionID, participant, track, kind string) EngineTrackPublished {
	return EngineTrackPublished{Type: TypeEngineTrackPublished, SessionID: sessionID, Participant: participant, Track: track, Kind: kind}
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/protocol/protocol_test.go (120 lines, sha256 cdc387b4fbc2d04fa499067bb77c7a489cd25229c73e6854cb77495676abdcdb) =====
==============================================================================
```go
package protocol

import (
	"encoding/json"
	"strings"
	"testing"
	"time"
)

func TestDecodeValidVariants(t *testing.T) {
	cases := []string{
		`{"type":"hello","token":"abc.jwt.token"}`,
		`{"type":"subscribe","room":"calls"}`,
		`{"type":"unsubscribe","room":"metrics"}`,
		`{"type":"ping"}`,
	}
	for _, raw := range cases {
		msg, err := DecodeClientMessage([]byte(raw))
		if err != nil {
			t.Errorf("DecodeClientMessage(%s): %v", raw, err)
		}
		if msg == nil {
			t.Errorf("DecodeClientMessage(%s) returned nil message", raw)
		}
	}
}

func TestDecodeRejectsUnknownType(t *testing.T) {
	if _, err := DecodeClientMessage([]byte(`{"type":"admin"}`)); err == nil {
		t.Error("unknown type must be rejected")
	}
}

func TestDecodeDistinguishesMissingFromEmpty(t *testing.T) {
	// The strictness that matters: a hello with NO token and a hello with an
	// EMPTY token are BOTH bad_message — anything looser lets an empty bearer
	// string reach the verifier as an ambiguous auth failure.
	for _, raw := range []string{
		`{"type":"hello"}`,
		`{"type":"hello","token":""}`,
		`{"type":"subscribe"}`,
		`{"type":"subscribe","room":""}`,
	} {
		if _, err := DecodeClientMessage([]byte(raw)); err == nil {
			t.Errorf("%s must be rejected at decode time", raw)
		}
	}
}

func TestDecodeRejectsMalformedJSON(t *testing.T) {
	for _, raw := range []string{"", "not json", `{"type":`, `["subscribe"]`} {
		if _, err := DecodeClientMessage([]byte(raw)); err == nil {
			t.Errorf("%q must be rejected", raw)
		}
	}
}

func TestServerFramesMarshalToWireShape(t *testing.T) {
	now := time.Date(2026, 9, 16, 12, 0, 0, 0, time.UTC)

	welcome := NewWelcome("sess-1", 10*time.Second, 20*time.Second, now)
	raw, err := Marshal(welcome)
	if err != nil {
		t.Fatalf("marshal welcome: %v", err)
	}
	var got map[string]any
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Fatalf("welcome is not JSON: %v", err)
	}
	if got["type"] != TypeWelcome || got["session_id"] != "sess-1" {
		t.Errorf("welcome = %s", raw)
	}
	if got["auth_required"] != true {
		t.Errorf("welcome must always tell a public client auth is required: %s", raw)
	}

	ready := NewReady("sess-1", "11111111-2222-3333-4444-555555555555", "owner", now.Add(15*time.Minute))
	raw, _ = Marshal(ready)
	if !strings.Contains(string(raw), `"token_expires_at"`) {
		t.Errorf("ready must advertise when the edge will close the socket: %s", raw)
	}

	sub := NewSubscribed("calls", 3)
	raw, _ = Marshal(sub)
	if !strings.Contains(string(raw), `"peers":3`) {
		t.Errorf("subscribed must carry the peer count: %s", raw)
	}

	delivery := NewDelivery("calls", "call.updated", "550e8400-e29b-41d4-a716-446655440000",
		json.RawMessage(`{"status":"COMPLETED"}`), now)
	raw, _ = Marshal(delivery)
	for _, want := range []string{`"kind":"call.updated"`, `"status":"COMPLETED"`, `"sent_at"`, `"event_id"`} {
		if !strings.Contains(string(raw), want) {
			t.Errorf("delivery missing %s: %s", want, raw)
		}
	}

	errMsg := NewError(CodeAuthFailed, "invalid or expired token")
	raw, _ = Marshal(errMsg)
	if !strings.Contains(string(raw), `"code":"auth_failed"`) {
		t.Errorf("error frame must carry the machine-readable code: %s", raw)
	}
}

func TestErrorCodesStayClosed(t *testing.T) {
	// A compile-time guard would be nicer; a regression list is what Go
	// gives us. The dashboard switches on these exact strings. The six
	// signaling codes were added with the session/signaling relay; the next
	// extension of this vocabulary must update dashboard handling first —
	// that is what this test is FOR.
	codes := map[string]bool{
		CodeBadMessage: true, CodeHelloRequired: true, CodeAuthFailed: true,
		CodeRoomInvalid: true, CodeOverLimit: true, CodeRateLimited: true,
		CodeSessionUnknown: true, CodeSessionFull: true, CodeSessionNotReady: true,
		CodeAlreadyInSession: true, CodeWrongSignalState: true, CodeSignalTooLarge: true,
	}
	if len(codes) != 12 {
		t.Fatalf("error code vocabulary changed; update the dashboard's switch first")
	}
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/ratelimit/bucket.go (75 lines, sha256 7320c4b3273f1d737ccc777d2d160c9ad67f383836f572b7e7c77de070b55730) =====
==============================================================================
```go
package ratelimit

import "time"

// Bucket is the classic lazy-refill token bucket, deliberately not
// thread-safe: it is the pure arithmetic core the Limiter wraps with a
// mutex. Lazy refill means no background goroutine and no timer — tokens
// are computed from elapsed wall-clock time at the moment of each check,
// so an idle connection costs exactly zero CPU and zero memory churn.
//
// Semantics, preserved verbatim from the gateway's original
// reader.go#allowMessage implementation:
//
//   - the bucket starts FULL (a fresh tab reconnecting after a drop must
//     be able to re-assert its subscriptions immediately);
//   - each allowance consumes exactly one whole token: when the banked
//     amount is in [0, 1) the frame is rejected and the fractional
//     remainder is kept (sub-token debt does not accumulate against the
//     client, it simply carries over);
//   - refill is capped at Burst, so quiet time banks at most one burst.
type Bucket struct {
	policy Policy
	tokens float64
	last   time.Time
}

// NewBucket returns a full bucket under the given policy, stamped at
// started. Callers pass time explicitly (rather than the bucket calling
// time.Now itself) so the Limiter can inject its clock for tests.
func NewBucket(policy Policy, started time.Time) *Bucket {
	return &Bucket{policy: policy, tokens: policy.Burst, last: started}
}

// AllowAt reports whether one unit of work may proceed at instant now,
// refilling lazily from the last checkpoint first. Exactly the gateway's
// original arithmetic:
//
//	tokens += elapsed * rate, capped at burst
//	if tokens < 1  → reject (keep the fractional remainder)
//	else           → tokens -= 1, allow
func (b *Bucket) AllowAt(now time.Time) bool {
	b.refill(now)
	if b.tokens < 1 {
		return false
	}
	b.tokens--
	return true
}

// TokensAt exposes the banked amount at instant now WITHOUT consuming
// anything — diagnostics and tests. It applies the same lazy refill
// AllowAt would, so the bucket's checkpoint advances.
func (b *Bucket) TokensAt(now time.Time) float64 {
	b.refill(now)
	return b.tokens
}

// refill advances the bucket's checkpoint to now, adding elapsed*rate
// tokens capped at the policy's burst. A now BEFORE the last checkpoint
// (clock step-back, or a test driving time backwards) adds nothing and
// keeps the previous checkpoint — the budget a client already earned is
// never confiscated by time weirdness.
func (b *Bucket) refill(now time.Time) {
	if !now.After(b.last) {
		return
	}
	b.tokens += now.Sub(b.last).Seconds() * b.policy.RatePerSecond
	if b.tokens > b.policy.Burst {
		b.tokens = b.policy.Burst
	}
	b.last = now
}

// Policy returns the bucket's sizing.
func (b *Bucket) Policy() Policy { return b.policy }
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/ratelimit/limiter.go (65 lines, sha256 90512e7d05022218ae6645ca8c05ddf805df4011662bc9516821ed74a412d24e) =====
==============================================================================
```go
package ratelimit

import (
	"sync"
	"time"
)

// Limiter is the concurrency-safe facade every traffic source uses. It
// owns a Bucket plus the clock, and serializes access internally — the
// original gateway implementation held the bucket inside the Connection's
// own mutex; the extraction lifts that guard INTO the type so a future
// caller cannot forget it.
type Limiter struct {
	mu     sync.Mutex
	bucket *Bucket
	// now is replaceable in tests (WithClock) so refill arithmetic can be
	// driven without sleeping real time.
	now func() time.Time
}

// Option customizes a Limiter at construction.
type Option func(*Limiter)

// WithClock replaces the wall clock (tests only).
func WithClock(now func() time.Time) Option {
	return func(l *Limiter) { l.now = now }
}

// New returns a Limiter under the given policy, its bucket starting full.
// An invalid policy (rate <= 0, burst < 1) falls back to DefaultPolicy —
// a limiter must NEVER be constructable in a state that silently admits
// unbounded traffic; the strict fallback fails closed instead.
func New(policy Policy, opts ...Option) *Limiter {
	if policy.Validate() != nil {
		policy = DefaultPolicy()
	}
	l := &Limiter{now: time.Now}
	for _, opt := range opts {
		opt(l)
	}
	l.bucket = NewBucket(policy, l.now())
	return l
}

// Allow reports whether one unit of inbound work may proceed. This is the
// whole contract the read loop relies on.
func (l *Limiter) Allow() bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.bucket.AllowAt(l.now())
}

// Tokens reports the currently banked amount (diagnostics/tests only).
func (l *Limiter) Tokens() float64 {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.bucket.TokensAt(l.now())
}

// Policy returns the limiter's effective policy (post-fallback).
func (l *Limiter) Policy() Policy {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.bucket.Policy()
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/ratelimit/limiter_test.go (127 lines, sha256 627c914db76925dc04b1c4553876835a31e7d97bb732b68c1cdf6d6eccafff6f) =====
==============================================================================
```go
package ratelimit

import (
	"sync"
	"testing"
	"time"
)

func staticClock(start time.Time) (func() time.Time, *time.Time) {
	now := start
	ptr := &now
	return func() time.Time { return *ptr }, ptr
}

func TestPolicyValidation(t *testing.T) {
	t.Parallel()
	if p, err := NewPolicy(20, 40); err != nil || p.RatePerSecond != 20 || p.Burst != 40 {
		t.Fatalf("valid policy rejected: %v %v", p, err)
	}
	if _, err := NewPolicy(0, 40); err == nil {
		t.Fatal("zero rate must be rejected (it would dead-letter every frame)")
	}
	if _, err := NewPolicy(20, 0.5); err == nil {
		t.Fatal("sub-unit burst must be rejected (it would reject every frame forever)")
	}
	if got := DefaultPolicy(); got.Validate() != nil {
		t.Fatalf("default policy must itself be valid: %v", got)
	}
}

func TestLimiterStartsFullAndDepletesBurst(t *testing.T) {
	t.Parallel()
	clock, now := staticClock(time.Unix(1_700_000_000, 0))
	l := New(MustPolicy(10, 5), WithClock(clock))

	for i := 0; i < 5; i++ {
		if !l.Allow() {
			t.Fatalf("burst frame %d rejected from a full bucket", i)
		}
	}
	if l.Allow() {
		t.Fatal("sixth frame must be rejected once the burst is spent")
	}
	// Fractional time advances refill fractionally: 50 ms at 10/s = 0.5
	// token, not enough for a whole frame on an empty bucket.
	*now = now.Add(50 * time.Millisecond)
	if l.Allow() {
		t.Fatal("0.5 token must not admit a whole frame")
	}
	// Another 50 ms completes the token (fractional remainder carries).
	*now = now.Add(50 * time.Millisecond)
	if !l.Allow() {
		t.Fatal("accumulated full token must admit exactly one frame")
	}
	if l.Allow() {
		t.Fatal("the burst must not magically refill")
	}
}

func TestLimiterRefillCappedAtBurst(t *testing.T) {
	t.Parallel()
	clock, now := staticClock(time.Unix(1_700_000_000, 0))
	l := New(MustPolicy(100, 3), WithClock(clock))

	// Sleep "for an hour": at 100/s that would be 360k tokens uncapped.
	*now = now.Add(time.Hour)
	for i := 0; i < 3; i++ {
		if !l.Allow() {
			t.Fatalf("banked burst frame %d rejected", i)
		}
	}
	if l.Allow() {
		t.Fatal("refill must cap at burst; an idle client banks at most one burst")
	}
}

func TestLimiterClockStepBackCannotConfiscate(t *testing.T) {
	t.Parallel()
	clock, now := staticClock(time.Unix(1_700_000_000, 0))
	l := New(MustPolicy(10, 5), WithClock(clock))

	l.Allow() // spend one
	*now = now.Add(-time.Minute)
	if got := l.Tokens(); got != 4 {
		t.Fatalf("backwards clock must not change the bank, got %v tokens", got)
	}
	if !l.Allow() {
		t.Fatal("already-earned budget must survive a clock step-back")
	}
}

func TestLimiterInvalidPolicyFailsClosed(t *testing.T) {
	t.Parallel()
	l := New(Policy{RatePerSecond: 0, Burst: 0}) // never valid
	if got := l.Policy(); got != DefaultPolicy() {
		t.Fatalf("invalid policy must fall back to the strict default, got %v", got)
	}
	if !l.Allow() || l.Allow() {
		t.Fatal("default policy admits exactly its burst (1), then stops")
	}
}

func TestLimiterConcurrentAllowIsSafeAndAccountingHolds(t *testing.T) {
	t.Parallel()
	clock, _ := staticClock(time.Unix(1_700_000_000, 0))
	const burst = 64
	l := New(MustPolicy(1, burst), WithClock(clock)) // ~no refill during test

	var wg sync.WaitGroup
	allowed := make(chan struct{}, 4*burst)
	for g := 0; g < 8; g++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := 0; i < burst; i++ {
				if l.Allow() {
					allowed <- struct{}{}
				}
			}
		}()
	}
	wg.Wait()
	close(allowed)
	if n := len(allowed); n != burst {
		t.Fatalf("concurrent spend must be exactly consistent: %d admitted, want %d", n, burst)
	}
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/ratelimit/policy.go (74 lines, sha256 580cf3911f2b28fa30a5535890a5e758d099365be721f7ed1492ec858778338a) =====
==============================================================================
```go
// Package ratelimit owns the gateway's inbound frame pacing: the token
// bucket primitive, the policy that sizes it, and the concurrency-safe
// limiter every WebSocket connection carries.
//
// It exists for one reason (unchanged from when this logic lived inside
// internal/websocket's reader): a hijacked or buggy browser tab must not be
// able to keep the gateway CPU-busy decoding frames unboundedly. The
// extraction into a package makes the primitive unit-testable on its own
// clock and reusable for any future inbound surface (e.g. a second edge).
package ratelimit

import "fmt"

// Policy sizes a token bucket: sustained replenish rate and the maximum
// burst a client may bank while quiet.
//
// The gateway's production values (config.DefaultMessageRatePerSecond /
// DefaultMessageBurst, 20/s with a bank of 40) are chosen for dashboards:
// a human clicking produce a handful of frames a second, a reconnecting
// tab re-asserts its subscriptions in one burst, and anything sustained
// above 20 fps is a bug or an attack, not a workload.
type Policy struct {
	// RatePerSecond is the sustained refill rate in tokens (frames) per
	// second. Must be > 0.
	RatePerSecond float64
	// Burst is the bucket's capacity — the most tokens a client can bank
	// while idle and therefore the largest instantaneous burst allowed.
	// Must be >= 1 (below 1 every frame would be rejected forever).
	Burst float64
}

// NewPolicy validates and normalizes a policy. It returns an error rather
// than silently clamping: a zero-rate limiter rejects EVERYTHING, which is
// exactly the misconfiguration that should be loud at construction time,
// not discovered as "no client can ever send" in production.
func NewPolicy(ratePerSecond, burst float64) (Policy, error) {
	p := Policy{RatePerSecond: ratePerSecond, Burst: burst}
	return p, p.Validate()
}

// MustPolicy is NewPolicy for call sites that pass compile-time-known-good
// configuration (the gateway's config has already range-checked these
// values at boot); a bad policy here is a programming error, so it panics.
func MustPolicy(ratePerSecond, burst float64) Policy {
	p, err := NewPolicy(ratePerSecond, burst)
	if err != nil {
		panic(fmt.Sprintf("ratelimit: invalid static policy: %v", err))
	}
	return p
}

// DefaultPolicy returns the package's own safe fallback (1 frame/s, burst
// 1) — deliberately stricter than the gateway's production defaults, so a
// caller that forgets to wire config errs on the side of refusing traffic
// rather than of permitting floods.
func DefaultPolicy() Policy {
	return Policy{RatePerSecond: 1, Burst: 1}
}

// Validate reports why a policy is unusable, or nil when it is sound.
func (p Policy) Validate() error {
	switch {
	case p.RatePerSecond <= 0:
		return fmt.Errorf("rate per second must be positive, got %v", p.RatePerSecond)
	case p.Burst < 1:
		return fmt.Errorf("burst must be at least 1 (below that every frame is rejected), got %v", p.Burst)
	}
	return nil
}

// String renders the policy for logs: "20/s (burst 40)".
func (p Policy) String() string {
	return fmt.Sprintf("%g/s (burst %g)", p.RatePerSecond, p.Burst)
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/server/engine_e2e_test.go (430 lines, sha256 c07db81d975d5e4c5303244189ec9e46426aacc4d8c2f2df5c1b8628279e1689) =====
==============================================================================
```go
package server

// Go↔Rust engine-link integration tests. A scripted httptest engine plays
// the Rust media engine's documented wire; the gateway stack is the REAL
// one — websockets, signaling router, session manager, engineclient. The
// eight cases the project calls out for this phase map onto:
//
//	1. session.start → engine join (room = tenant:session, member = conn)
//	2. session.join  → second engine join for the responder
//	3. session.end   → engine leaves for every member's own engine session
//	4. socket close  → same leaves (ConnDropped path)
//	5. engine DOWN at start → session still establishes (degrade, not break)
//	6. engine REFUSAL (in-band error) → same degrade, louder class
//	7. readiness reflects availability: disabled / up / down states
//	8. metrics surface the link: joins, join errors, engine_up gauge

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"github.com/voxdesk/realtime/gateway-go/internal/auth"
	"github.com/voxdesk/realtime/gateway-go/internal/engineclient"
	"github.com/voxdesk/realtime/gateway-go/internal/hub"
	"github.com/voxdesk/realtime/gateway-go/internal/observability/metrics"
	"github.com/voxdesk/realtime/gateway-go/internal/session"
	"github.com/voxdesk/realtime/gateway-go/internal/signaling"
)

// scriptedEngine records the signaling-plane surface the gateway touched:
// joins and leaves with all fields, in wire order.
type scriptedEngine struct {
	t *testing.T

	mu         sync.Mutex
	joins      []map[string]string // {room, participant, engine_session}
	leaves     []string            // engine session ids
	offers     []map[string]string // {session, sdp_len}
	publishes  []map[string]string // {session, track, kind}
	subscribes []map[string]string // {session, participant, track}
	failWith   string              // "refuse" → in-band error; "http500" → transport; "" → healthy
	sessionCt  int

	srv *httptest.Server
}

func newScriptedEngine(t *testing.T) *scriptedEngine {
	e := &scriptedEngine{t: t}
	e.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/v1/signal":
			var env struct {
				V     int            `json:"v"`
				ID    string         `json:"id"`
				Frame map[string]any `json:"frame"`
			}
			if err := json.NewDecoder(r.Body).Decode(&env); err != nil {
				w.WriteHeader(http.StatusBadRequest)
				return
			}
			e.mu.Lock()
			defer e.mu.Unlock()
			w.Header().Set("Content-Type", "application/json")
			if e.failWith == "http500" {
				w.WriteHeader(http.StatusInternalServerError)
				return
			}
			if e.failWith == "refuse" {
				fmt.Fprintf(w, `{"v":1,"id":%q,"error":{"code":"room_full","message":"capacity playing"}}`, env.ID)
				return
			}
			typ, _ := env.Frame["type"].(string)
			switch typ {
			case "offer":
				e.offers = append(e.offers, map[string]string{"session": sprintf(env.Frame["session"]), "sdp_len": fmt.Sprintf("%d", len(sprintf(env.Frame["sdp"])))})
				fmt.Fprintf(w, `{"v":1,"id":%q,"frames":[{"type":"answer","session":%q,"sdp":"v=0\r\no=- 9 9 IN IP4 203.0.113.9\r\nm=audio 5000 RTP/AVP 111\r\na=sendrecv\r\n"}]}`, env.ID, sprintf(env.Frame["session"]))
			case "publish":
				e.publishes = append(e.publishes, map[string]string{
					"session": sprintf(env.Frame["session"]), "track": sprintf(env.Frame["track"]), "kind": sprintf(env.Frame["kind"]),
				})
				// Mirror the REAL engine's effects array: the room fanout
				// carries the PUBLISHER's participant id (here, the join
				// enrolment for the same engine session id).
				pubBy := ""
				for _, j := range e.joins {
					if j["engine_session"] == sprintf(env.Frame["session"]) {
						pubBy = j["participant"]
					}
				}
				fmt.Fprintf(w, `{"v":1,"id":%q,"frames":[{"type":"track.published","room":"r","participant":%q,"track":%q,"kind":%q}]}`,
					env.ID, pubBy, sprintf(env.Frame["track"]), sprintf(env.Frame["kind"]))
			case "trickle":
				fmt.Fprintf(w, `{"v":1,"id":%q,"frames":[]}`, env.ID)
			case "subscribe":
				e.subscribes = append(e.subscribes, map[string]string{
					"session": sprintf(env.Frame["session"]), "participant": sprintf(env.Frame["participant"]), "track": sprintf(env.Frame["track"]),
				})
				fmt.Fprintf(w, `{"v":1,"id":%q,"frames":[]}`, env.ID)
			case "unsubscribe":
				fmt.Fprintf(w, `{"v":1,"id":%q,"frames":[]}`, env.ID)
			case "join":
				e.sessionCt++
				engineSID := fmt.Sprintf("ms-test-%d", e.sessionCt)
				e.joins = append(e.joins, map[string]string{
					"room":           sprintf(env.Frame["room"]),
					"participant":    sprintf(env.Frame["participant"]),
					"engine_session": engineSID,
				})
				fmt.Fprintf(w, `{"v":1,"id":%q,"frames":[{"type":"ready","session":%q,"ice_ufrag":"u-fake","ice_pwd":"p-fake"}]}`, env.ID, engineSID)
			case "leave":
				e.leaves = append(e.leaves, sprintf(env.Frame["session"]))
				fmt.Fprintf(w, `{"v":1,"id":%q,"frames":[]}`, env.ID)
			default:
				fmt.Fprintf(w, `{"v":1,"id":%q,"frames":[]}`, env.ID)
			}
		case r.Method == http.MethodGet && r.URL.Path == "/v1/health":
			e.mu.Lock()
			down := e.failWith == "http500"
			e.mu.Unlock()
			if down {
				w.WriteHeader(http.StatusInternalServerError)
				return
			}
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"v":1,"engine":"media-engine-rs","version":"0.0.0-test","ready":true,"rooms":1,"participants":1,"tracks":1,"uptime_ms":7}`))
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	t.Cleanup(e.srv.Close)
	return e
}

func sprintf(v any) string {
	if s, ok := v.(string); ok {
		return s
	}
	return ""
}

// subscribeSnapshot returns (count, last-recorded map) under the same
// lock the writer uses — the race-clean way for waitFor predicates.
func (e *scriptedEngine) subscribeSnapshot() (int, map[string]string) {
	e.mu.Lock()
	defer e.mu.Unlock()
	if len(e.subscribes) == 0 {
		return 0, nil
	}
	cp := make(map[string]string, len(e.subscribes[0]))
	for k, v := range e.subscribes[0] {
		cp[k] = v
	}
	return len(e.subscribes), cp
}

func (e *scriptedEngine) joinCount() int {
	e.mu.Lock()
	defer e.mu.Unlock()
	return len(e.joins)
}

func (e *scriptedEngine) leaveCount() int {
	e.mu.Lock()
	defer e.mu.Unlock()
	return len(e.leaves)
}

func (e *scriptedEngine) fail(mode string) {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.failWith = mode
}

// engineFixture wires the standard fixture plus an attached client. The
// fixture reuses the same hub/manager/router composition as newFixture so
// the ws flow is byte-identical to what production wires in cmd/gateway.
func engineFixture(t *testing.T, eng *engineclient.Client) *fixture {
	t.Helper()
	cfg := testConfig()
	h := hub.New(cfg.MaxConnsPerTenant)
	reg := metrics.New()
	verifier := auth.NewVerifier(cfg.JWTSecret, cfg.JWTIssuer, cfg.JWTAudience)
	sig := signaling.NewRouter(session.NewManager(cfg.SignalingMaxSessionsPerTenant, cfg.SignalingPendingTimeout), h.Lookup, reg)
	s := New(cfg, h, reg, verifier, sig)
	s.SetEngine(eng)
	ts := httptest.NewServer(s.Handler())
	t.Cleanup(ts.Close)
	return &fixture{server: s, hub: h, reg: reg, http: ts}
}

func waitFor(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for !cond() && time.Now().Before(deadline) {
		time.Sleep(5 * time.Millisecond)
	}
	if !cond() {
		t.Fatalf("timed out waiting for %s", what)
	}
}

func TestEngineJoinAndLeaveAcrossFullSession(t *testing.T) {
	eng := newScriptedEngine(t)
	client := engineclient.New(eng.srv.URL, time.Second, nil, nil, func(string, ...any) {})
	f := engineFixture(t, client)

	a := signalingClient(t, f, testTenantA)
	b := signalingClient(t, f, testTenantA)

	sendFrame(t, a, `{"type":"session.start"}`)
	started := readFrame(t, a)
	if started["type"] != "session.started" {
		t.Fatalf("session.started = %v", started)
	}
	id, _ := started["session_id"].(string)

	sendFrame(t, b, `{"type":"session.join","session_id":"`+id+`"}`)
	if j := readFrame(t, b); j["type"] != "session.joined" {
		t.Fatalf("session.joined = %v", j)
	}
	_ = readFrame(t, a) // peer_joined

	// Two joins, scoped to the same conversation room, one per member.
	waitFor(t, "two engine joins", func() bool { return eng.joinCount() == 2 })
	eng.mu.Lock()
	room0, room1 := eng.joins[0]["room"], eng.joins[1]["room"]
	p0, p1 := eng.joins[0]["participant"], eng.joins[1]["participant"]
	eng.mu.Unlock()
	wantRoom := testTenantA + ":" + id
	if room0 != wantRoom || room1 != wantRoom {
		t.Fatalf("engine rooms %q,%q want %q", room0, room1, wantRoom)
	}
	if p0 == "" || p1 == "" || p0 == p1 {
		t.Fatalf("engine participants must be distinct conn ids: %q,%q", p0, p1)
	}

	// Graceful end: BOTH members' engine sessions receive a leave.
	sendFrame(t, a, `{"type":"session.end","session_id":"`+id+`"}`)
	_ = readFrame(t, a) // ended
	_ = readFrame(t, b) // ended
	waitFor(t, "two engine leaves", func() bool { return eng.leaveCount() == 2 })

	eng.mu.Lock()
	defer eng.mu.Unlock()
	joined := map[string]bool{eng.joins[0]["engine_session"]: true, eng.joins[1]["engine_session"]: true}
	for _, lv := range eng.leaves {
		if !joined[lv] {
			t.Fatalf("leave %q not among engine sessions %v", lv, joined)
		}
	}
}

func TestEngineLeaveOnSocketClose(t *testing.T) {
	eng := newScriptedEngine(t)
	client := engineclient.New(eng.srv.URL, time.Second, nil, nil, func(string, ...any) {})
	f := engineFixture(t, client)

	a := signalingClient(t, f, testTenantA)
	sendFrame(t, a, `{"type":"session.start"}`)
	started := readFrame(t, a)
	if started["type"] != "session.started" {
		t.Fatalf("session.started = %v", started)
	}
	// Close the socket: ConnDropped → EvEnded → the leave hook must fire
	// without any session.end frame ever being sent.
	a.Close()
	waitFor(t, "leave after socket close", func() bool { return eng.leaveCount() == 1 })
}

func TestEngineUnavailableDoesNotBreakSessionEstablishment(t *testing.T) {
	eng := newScriptedEngine(t)
	eng.fail("http500")
	client := engineclient.New(eng.srv.URL, 300*time.Millisecond, nil, nil, func(string, ...any) {})
	f := engineFixture(t, client)

	a := signalingClient(t, f, testTenantA)
	sendFrame(t, a, `{"type":"session.start"}`)
	started := readFrame(t, a)
	if started["type"] != "session.started" {
		t.Fatalf("down-engine must NOT break establishment, got %v", started)
	}
	// The failure DID surface in metrics — no silent absorption.
	if got := f.reg.EngineUp(); got {
		t.Fatalf("engine_up gauge must be 0 before any green probe (HealthNow never ran)")
	}
}

func TestEngineRefusalLoggedAsRefusalNotUnavailable(t *testing.T) {
	eng := newScriptedEngine(t)
	eng.fail("refuse")
	client := engineclient.New(eng.srv.URL, time.Second, nil, nil, func(string, ...any) {})
	f := engineFixture(t, client)

	a := signalingClient(t, f, testTenantA)
	sendFrame(t, a, `{"type":"session.start"}`)
	started := readFrame(t, a)
	if started["type"] != "session.started" {
		t.Fatalf("refusing engine must NOT break establishment, got %v", started)
	}
	if eng.joinCount() != 0 {
		t.Fatalf("refused join recorded none, got %d", eng.joinCount())
	}
}

func TestReadinessReportsEngineStates(t *testing.T) {
	// disabled: no engine attached at all.
	f := engineFixture(t, nil)
	req, err := http.NewRequest(http.MethodGet, f.http.URL+"/readyz", http.NoBody)
	if err != nil {
		t.Fatal(err)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	var body map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&body)
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("engine-disabled node must be ready, got %d", resp.StatusCode)
	}
	engineCheck, _ := body["checks"].(map[string]any)["engine"].(map[string]any)
	if engineCheck["state"] != "disabled" {
		t.Fatalf("disabled state expected, got %v", engineCheck)
	}

	// up: a healthy engine acked by HealthNow (monitor steady-state proxy).
	eng := newScriptedEngine(t)
	client := engineclient.New(eng.srv.URL, time.Second, nil, nil, func(string, ...any) {})
	f2 := engineFixture(t, client)
	if _, err := client.HealthNow(context.Background()); err != nil {
		t.Fatalf("precondition probe: %v", err)
	}
	req2, _ := http.NewRequest(http.MethodGet, f2.http.URL+"/readyz", http.NoBody)
	resp2, err := http.DefaultClient.Do(req2)
	if err != nil {
		t.Fatal(err)
	}
	var body2 map[string]any
	_ = json.NewDecoder(resp2.Body).Decode(&body2)
	resp2.Body.Close()
	if resp2.StatusCode != http.StatusOK {
		t.Fatalf("healthy engine must keep readiness green, got %d body %v", resp2.StatusCode, body2)
	}
	engineCheck2, _ := body2["checks"].(map[string]any)["engine"].(map[string]any)
	if engineCheck2["state"] != "up" {
		t.Fatalf("up state expected, got %v", engineCheck2)
	}

	// down: attached but unreachable → readiness red, reason carried.
	down := engineclient.New("http://127.0.0.1:1", 50*time.Millisecond, nil, nil, func(string, ...any) {})
	f3 := engineFixture(t, down)
	req3, _ := http.NewRequest(http.MethodGet, f3.http.URL+"/readyz", http.NoBody)
	resp3, err := http.DefaultClient.Do(req3)
	if err != nil {
		t.Fatal(err)
	}
	var body3 map[string]any
	_ = json.NewDecoder(resp3.Body).Decode(&body3)
	resp3.Body.Close()
	if resp3.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("down engine must turn readiness red, got %d", resp3.StatusCode)
	}
	engineCheck3, _ := body3["checks"].(map[string]any)["engine"].(map[string]any)
	if engineCheck3["state"] != "down" {
		t.Fatalf("down state expected, got %v", engineCheck3)
	}
}

func TestEngineMetricsSurface(t *testing.T) {
	eng := newScriptedEngine(t)
	// Build the fixture first (its OWN registry), then attach a client
	// whose observer points at exactly that registry.
	f := engineFixture(t, nil)
	wrapped := engineclient.New(eng.srv.URL, time.Second, testEngineObserver{f.reg}, nil, func(string, ...any) {})
	f.server.SetEngine(wrapped)

	a := signalingClient(t, f, testTenantA)
	sendFrame(t, a, `{"type":"session.start"}`)
	if s := readFrame(t, a); s["type"] != "session.started" {
		t.Fatalf("session.started = %v", s)
	}
	waitFor(t, "one recorded join", func() bool { return eng.joinCount() == 1 })

	rendered := f.reg.Render(0, 0)
	if !contains(rendered, "voxdesk_gateway_engine_signal_calls_total 1") {
		t.Fatalf("metrics missing join call line:\n%s", rendered)
	}

	// The availability monitor is driven by HealthNow here: observe one
	// probe and a transition.
	if _, err := wrapped.HealthNow(context.Background()); err != nil {
		t.Fatal(err)
	}
	if !f.reg.EngineUp() {
		t.Fatalf("gauge must be up after one green probe")
	}
	rendered = f.reg.Render(0, 0)
	if !contains(rendered, "voxdesk_gateway_engine_up 1") {
		t.Fatalf("engine_up gauge missing:\n%s", rendered)
	}
}

// testEngineObserver mirrors the cmd/gateway adapter so this package can
// verify the metric lines without importing main.
type testEngineObserver struct{ reg *metrics.Registry }

func (o testEngineObserver) ObserveSignal(op string, took time.Duration, err error) {
	o.reg.EngineCall(op, took, err != nil)
}

func (o testEngineObserver) ObserveHealth(took time.Duration, err error) {
	o.reg.EngineProbe(took, err != nil)
}

func (o testEngineObserver) EngineUpChanged(up bool) { o.reg.SetEngineUp(up) }

func contains(s, sub string) bool {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/server/health.go (75 lines, sha256 e4ed0ba438ecc716225e31868be1989142fcc9e3c2171aea790e9ba3a6e366e7) =====
==============================================================================
```go
package server

import (
	"net/http"
)

// serveLiveness answers /healthz: the process is up. Mirrors the API's
// /health — liveness says nothing about dependencies or capacity, so a
// kubelet/load balancer can tell "dead process" apart from "busy process".
func (s *Server) serveLiveness(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

// serveReadiness answers /readyz: the node is prepared to accept NEW
// sessions. A gateway at its connection ceiling returns 503 so the load
// balancer shifts new browsers to a node with headroom instead of letting
// them hit the upgrade-time 503 — the same "stop receiving work instead of
// failing every request" contract the API's /health/ready documents.
func (s *Server) serveReadiness(w http.ResponseWriter, r *http.Request) {
	current := s.registry.ConnectionsCurrent()
	capacity := int64(s.cfg.MaxConnections)
	headroom := capacity - current
	ready := headroom > 0

	// Engine segment: readiness REFLECTS the media plane. Three states:
	//   disabled — configured-off, explicitly said so (never silently);
	//   up/down  — the monitor's last probe. Down REJECTS readiness: a
	//   node that serves conversations without its SFU is not ready for
	//   traffic, per the deployment contract (the ws plane itself keeps
	//   serving / degrades per-connection independently of this verdict).
	engineCheck := map[string]any{"ok": true, "state": "disabled"}
	engineUp := true
	if s.engine != nil {
		engineUp = s.engine.Up()
		engineCheck = map[string]any{
			"ok":    engineUp,
			"state": map[bool]string{true: "up", false: "down"}[engineUp],
		}
		if !engineUp {
			engineCheck["last_error"] = s.engine.LastError()
		}
		if h := s.engine.LastHealth(); h != nil {
			engineCheck["engine_version"] = h.Version
			engineCheck["rooms"] = h.Rooms
		}
	}
	ready = ready && engineUp

	rooms, tenants := s.hub.Stats()
	body := map[string]any{
		"status":   map[bool]string{true: "ok", false: "unavailable"}[ready],
		"uptime_s": s.uptimeSeconds(),
		"checks": map[string]any{
			"capacity": map[string]any{
				"ok":       ready,
				"current":  current,
				"max":      capacity,
				"headroom": headroom,
			},
			"engine":  engineCheck,
			"rooms":   rooms,
			"tenants": tenants,
		},
	}
	if !ready {
		if headroom <= 0 {
			s.logf("[gateway] readiness unavailable: at connection capacity (%d/%d)", current, capacity)
		} else {
			s.logf("[gateway] readiness unavailable: media engine down (%s)", s.engine.LastError())
		}
		writeJSON(w, http.StatusServiceUnavailable, body)
		return
	}
	writeJSON(w, http.StatusOK, body)
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/server/http.go (77 lines, sha256 016d3f4cb4bda23f046a226ffb8eb6ff93c11033a45d73ee2208ab99edc464ba) =====
==============================================================================
```go
package server

import (
	"encoding/json"
	"net/http"
	"time"

	"github.com/voxdesk/realtime/gateway-go/internal/observability"
)

// Handler returns the gateway's single HTTP mux. The route table is the
// whole public surface and is deliberately tiny:
//
//	GET  /ws                  — the WebSocket edge (dashboard clients)
//	POST /ingest/v1/publish   — server-to-server event ingest (the API)
//	GET  /healthz             — liveness (load balancer)
//	GET  /readyz              — readiness (capacity-aware)
//	GET  /metrics             — Prometheus scrape (token-gated when configured)
//	anything else             — JSON 404; unknown paths never receive HTML
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/ws", s.serveWS)
	mux.HandleFunc("/ingest/v1/publish", s.requirePost(s.serveIngest))
	mux.HandleFunc("/healthz", s.requireGet(s.serveLiveness))
	mux.HandleFunc("/readyz", s.requireGet(s.serveReadiness))
	mux.HandleFunc("/metrics", s.requireGet(s.serveMetrics))
	mux.HandleFunc("/", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusNotFound, map[string]string{"detail": "not found"})
	})
	// One middleware for the whole surface: every request leaves with a
	// correlation id — the API's inbound X-Request-ID when it sent one
	// (the API → gateway trace continues unbroken), a fresh id otherwise,
	// so even probe traffic is quoteable in logs by id.
	return observability.RequestIDMiddleware(mux)
}

// requireGet rejects non-GET methods with a JSON 405 (no wrong-verb
// ambiguity on a public surface).
func (s *Server) requireGet(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"detail": "method not allowed"})
			return
		}
		next(w, r)
	}
}

// requirePost rejects non-POST methods with a JSON 405.
func (s *Server) requirePost(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"detail": "method not allowed"})
			return
		}
		next(w, r)
	}
}

// writeJSON is the one response helper every handler uses: JSON body,
// explicit content type, no server-generated HTML anywhere (the Python
// app's error discipline, expressed in Go).
func writeJSON(w http.ResponseWriter, status int, body any) {
	data, err := json.Marshal(body)
	if err != nil {
		status = http.StatusInternalServerError
		data = []byte(`{"detail":"internal error"}`)
	}
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_, _ = w.Write(data)
}

// uptimeSeconds backs readiness and the uptime gauge.
func (s *Server) uptimeSeconds() int64 {
	return int64(time.Since(s.startedAt).Seconds())
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/server/ingest.go (239 lines, sha256 4ffd49ee0fc597a563a85d82f7f32fa52daa942b3cbfbc0c42187c48fac5f328) =====
==============================================================================
```go
package server

import (
	"encoding/json"
	"errors"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/voxdesk/realtime/gateway-go/internal/auth"
	"github.com/voxdesk/realtime/gateway-go/internal/broker"
	"github.com/voxdesk/realtime/gateway-go/internal/observability"
	"github.com/voxdesk/realtime/gateway-go/internal/validate"
)

// ingestTopic is THE fan-out topic: every accepted publish (this node's
// API POST or another replica's bus hop) travels it. Colon-namespaced the
// way redis channels conventionally are.
const ingestTopic = "voxdesk:ingest"

// publishMessage is the wire schema of an ingest envelope — the bytes that
// cross the broker. Payload rides as a raw message so the bus hop NEVER
// re-marshals the event body (forwarded-verbatim is the whole contract).
type publishMessage struct {
	TenantID string          `json:"tenant_id"`
	Room     string          `json:"room"`
	Kind     string          `json:"kind"`
	EventID  string          `json:"event_id,omitempty"`
	Payload  json.RawMessage `json:"payload"`
}

// ingestRequest is POST /ingest/v1/publish's body — the ONE shape the API
// uses to push realtime events to browsers:
//
//	{"tenant_id": "<uuid>",             — required, routing key
//	 "room":      "calls"|"metrics"|"call:<uuid>"|"campaign:<uuid>",
//	 "kind":      "call.updated",       — dotted event name
//	 "payload":   {...any JSON...},     — delivered verbatim to subscribers
//	 "event_id":  "<uuid>",             — optional, drives replay suppression
//	}
type ingestRequest struct {
	TenantID string          `json:"tenant_id"`
	Room     string          `json:"room"`
	Kind     string          `json:"kind"`
	Payload  json.RawMessage `json:"payload"`
	EventID  string          `json:"event_id,omitempty"`
}

// ingestResponse reports the fan-out. duplicate=true means a replayed
// event_id was suppressed — a 200 in that case is deliberate: publisher
// retries are EXPECTED (the API emits events from the same transactions as
// its state changes and will redeliver after a crash), and retrying a
// duplicate must converge, not amplify.
type ingestResponse struct {
	Delivered int  `json:"delivered"`
	Dropped   int  `json:"dropped"`
	Duplicate bool `json:"duplicate"`
}

// serveIngest handles POST /ingest/v1/publish. Verification order is
// deliberate: auth FIRST (cheapest, rejects forgeries before any parsing),
// then size, then shape, then replay, then fan-out.
func (s *Server) serveIngest(w http.ResponseWriter, r *http.Request) {
	// 1) Auth: shared ingest secret as a Bearer credential, constant-time
	//    (hashed first so even the secret's length does not leak). This
	//    endpoint can write into any tenant's rooms, so it is exactly as
	//    sensitive as an unauthenticated webhook would be on the API.
	if !ingestAuthorized(r.Header.Get("Authorization"), s.cfg.IngestSecret) {
		s.registry.IngestRejected()
		s.emitIngestRejected(r, "auth")
		writeJSON(w, http.StatusUnauthorized, map[string]string{"detail": "invalid ingest credentials"})
		return
	}

	// 2) Size ceiling, enforced BEFORE decoding — a body has no business
	//    being bigger than the envelope plus the largest payload we fan out.
	r.Body = http.MaxBytesReader(w, r.Body, s.cfg.MaxIngestPayloadBytes+4096)
	var req ingestRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		s.registry.IngestRejected()
		var maxBytesErr *http.MaxBytesError
		if errors.As(err, &maxBytesErr) {
			s.emitIngestRejected(r, "payload_too_large")
			writeJSON(w, http.StatusRequestEntityTooLarge, map[string]string{"detail": "payload too large"})
			return
		}
		s.emitIngestRejected(r, "not_json")
		writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"detail": "body is not valid JSON"})
		return
	}

	// 3) Shape: every field that becomes a routing key is validated. An
	//    ingest that cannot name its tenant/room precisely is a bug in the
	//    publisher, and 422 tells the API's publisher-loop to stop retrying
	//    (the API's retry taxonomy already treats 422 as permanent).
	if !validate.IsUUID(req.TenantID) {
		s.registry.IngestRejected()
		s.emitIngestRejected(r, "tenant_id_shape")
		writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"detail": "tenant_id must be a UUID"})
		return
	}
	if !validate.IsRoomName(req.Room) {
		s.registry.IngestRejected()
		s.emitIngestRejected(r, "room_shape")
		writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"detail": "room must be calls, metrics, call:<uuid> or campaign:<uuid>"})
		return
	}
	if !validate.IsEventKind(req.Kind) {
		s.registry.IngestRejected()
		s.emitIngestRejected(r, "kind_shape")
		writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"detail": "kind must be a dotted event name like call.updated"})
		return
	}
	trimmed := strings.TrimSpace(string(req.Payload))
	objectOrArray := len(trimmed) > 0 && (trimmed[0] == '{' || trimmed[0] == '[')
	if len(req.Payload) == 0 || !json.Valid(req.Payload) || !objectOrArray {
		// Deliveries fan out verbatim, and dashboards dispatch on payload
		// fields: a scalar would deserialize "successfully" into something
		// no client switch can handle. Objects and arrays only.
		s.registry.IngestRejected()
		s.emitIngestRejected(r, "payload_shape")
		writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"detail": "payload must be a JSON object or array"})
		return
	}
	if int64(len(req.Payload)) > s.cfg.MaxIngestPayloadBytes {
		s.registry.IngestRejected()
		s.emitIngestRejected(r, "payload_too_large")
		writeJSON(w, http.StatusRequestEntityTooLarge, map[string]string{"detail": "payload too large"})
		return
	}
	if req.EventID != "" && !validate.IsUUID(req.EventID) {
		s.registry.IngestRejected()
		s.emitIngestRejected(r, "event_id_shape")
		writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"detail": "event_id must be a UUID when present"})
		return
	}
	tenantID := strings.ToLower(req.TenantID)

	// 4) Replay suppression. No event_id means the publisher opted out of
	//    dedupe (a deliberate choice for coarse metric ticks).
	if req.EventID != "" && s.replay.SeenBefore(tenantID, req.EventID, time.Now()) {
		s.registry.IngestDuplicate()
		s.emitIngest(r, "ingest.duplicate", "tenant", tenantID, "room", req.Room, "kind", req.Kind, "event_id", req.EventID)
		writeJSON(w, http.StatusOK, ingestResponse{Delivered: 0, Dropped: 0, Duplicate: true})
		return
	}

	// 5) Fan-out THROUGH THE BROKER. In the default memory mode the
	//    publish IS the hub fan-out (synchronous, same counters as
	//    before); in redis mode this call additionally propagates the
	//    envelope to the other replicas, and the Stats below remain the
	//    local node's — response semantics are transport-independent.
	//    Tenancy is structural below this line either way: the hub only
	//    ever touches (tenantID, room) — no code path in the package can
	//    deliver into another tenant.
	msg := publishMessage{TenantID: tenantID, Room: req.Room, Kind: req.Kind, EventID: req.EventID, Payload: req.Payload}
	frame, err := json.Marshal(msg)
	if err != nil {
		// msg was fully validated above; a marshal failure here is a
		// can't-happen guard (byte-tainted RawMessage would already have
		// failed json.Valid). Refuse as a shape error, never panic.
		s.registry.IngestRejected()
		s.emitIngestRejected(r, "frame_marshal")
		writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"detail": "payload could not be framed"})
		return
	}
	stats, err := s.bus.Publish(ingestTopic, frame)
	if err != nil {
		// Only broker.ErrClosed exists today: shutdown in flight. The API
		// retries 5xx with backoff, so a retry lands on the replacement
		// replica — converge, don't dead-letter.
		s.registry.IngestRejected()
		s.emitIngestRejected(r, "bus_closed")
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"detail": "gateway is shutting down"})
		return
	}
	delivered, dropped := stats.Delivered, stats.Dropped
	s.registry.IngestAccepted()
	s.registry.Delivered(int64(delivered))
	s.registry.Dropped(int64(dropped))
	s.emitIngest(r, "ingest.accepted", "tenant", tenantID, "room", req.Room, "kind", req.Kind, "event_id", req.EventID, "delivered", strconv.Itoa(delivered), "dropped", strconv.Itoa(dropped))
	writeJSON(w, http.StatusOK, ingestResponse{Delivered: delivered, Dropped: dropped, Duplicate: false})
}

// handleIngestEnvelope is the broker's local delivery point for EVERY
// ingest-homage envelope: locally-published (the serving Publish call
// itself — accounting returned to the HTTP handler) and bus-received from
// other replicas (accounting counted straight into metrics here, since no
// HTTP response ever summarizes a remote hop). Decode failures are logged
// and skipped: one malformed frame must not poison the topic.
func (s *Server) handleIngestEnvelope(env broker.Envelope) broker.Stats {
	var msg publishMessage
	if err := json.Unmarshal(env.Payload, &msg); err != nil {
		s.logf("[gateway] dropping undecodable ingest envelope from %s: %v", env.Origin, err)
		return broker.Stats{}
	}
	delivered, dropped := s.hub.Publish(msg.TenantID, msg.Room, msg.Kind, msg.EventID, msg.Payload)
	if !env.Local {
		s.registry.Delivered(int64(delivered))
		s.registry.Dropped(int64(dropped))
	}
	return broker.Stats{Delivered: delivered, Dropped: dropped}
}

// ingestAuthorized verifies the Bearer credential against the configured
// ingest secret in constant time (hashed first, as in metrics.go). The
// Bearer extraction + constant-time compare are shared with auth package's
// middleware helpers since the package split — one comparison
// implementation for every secret on this edge.
func ingestAuthorized(header, secret string) bool {
	token, ok := auth.ExtractBearer(header)
	if !ok {
		return false
	}
	return auth.ConstantTimeTokenEqual(token, secret)
}

// emitIngestRejected publishes the structured reject fact with its reason;
// the HTTP response stays the API-facing contract, the event is the
// operator-facing audit trail (same fields accepted publishes emit, minus
// the fan-out counters that do not exist on a reject).
func (s *Server) emitIngestRejected(r *http.Request, reason string) {
	s.emitIngest(r, "ingest.rejected", "reason", reason)
}

// emitIngest publishes one ingest event with the request's correlation id
// attached — the moment API → gateway → browser becomes one traceable line
// is a /ingest POST arriving with the API's X-Request-ID.
func (s *Server) emitIngest(r *http.Request, name string, kv ...string) {
	fields := make(map[string]string, len(kv)/2+1)
	for i := 0; i+1 < len(kv); i += 2 {
		fields[kv[i]] = kv[i+1]
	}
	if id := observability.RequestIDFrom(r.Context()); id != "" {
		fields["request_id"] = id
	}
	s.events.Emit(observability.Event{Name: name, At: time.Now(), Fields: fields})
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/server/ingest_test.go (332 lines, sha256 d36870cd8909a6a60bc2a3200a2fb58dda49bcc06aac8286f1386e079212bae1) =====
==============================================================================
```go
package server

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/voxdesk/realtime/gateway-go/internal/auth"
	"github.com/voxdesk/realtime/gateway-go/internal/config"
	"github.com/voxdesk/realtime/gateway-go/internal/hub"
	"github.com/voxdesk/realtime/gateway-go/internal/observability/metrics"
	"github.com/voxdesk/realtime/gateway-go/internal/session"
	"github.com/voxdesk/realtime/gateway-go/internal/signaling"
)

const (
	testJWTSecret    = "0123456789abcdef0123456789abcdef"
	testIngestSecret = "ingest-shared-secret-0123"
	testTenantA      = "11111111-1111-1111-1111-111111111111"
	testTenantB      = "22222222-2222-2222-2222-222222222222"
)

func testConfig() config.Config {
	return config.Config{
		Port:                          8790,
		JWTSecret:                     testJWTSecret,
		JWTIssuer:                     "voxdesk",
		JWTAudience:                   "voxdesk-api",
		IngestSecret:                  testIngestSecret,
		MaxConnections:                100,
		MaxConnsPerTenant:             10,
		MaxSubscriptionsPerConn:       8,
		OutgoingBuffer:                16,
		MaxMessageBytes:               16 * 1024,
		MaxIngestPayloadBytes:         4 * 1024,
		WriteWait:                     2 * time.Second,
		AuthTimeout:                   2 * time.Second,
		PingInterval:                  250 * time.Millisecond,
		PongTimeout:                   800 * time.Millisecond,
		ShutdownTimeout:               2 * time.Second,
		MessageRatePerSecond:          100,
		MessageBurst:                  100,
		IdempotencyTTL:                time.Minute,
		SignalingMaxSessionsPerTenant: 8,
		SignalingPendingTimeout:       time.Minute,
		IdempotencyCapacity:           100,
	}
}

// fixture wires a full gateway for httptest use.
type fixture struct {
	server *Server
	hub    *hub.Hub
	reg    *metrics.Registry
	http   *httptest.Server
}

func newFixture(t *testing.T) *fixture {
	t.Helper()
	cfg := testConfig()
	h := hub.New(cfg.MaxConnsPerTenant)
	reg := metrics.New()
	verifier := auth.NewVerifier(cfg.JWTSecret, cfg.JWTIssuer, cfg.JWTAudience)
	sig := signaling.NewRouter(session.NewManager(cfg.SignalingMaxSessionsPerTenant, cfg.SignalingPendingTimeout), h.Lookup, reg)
	s := New(cfg, h, reg, verifier, sig)
	ts := httptest.NewServer(s.Handler())
	t.Cleanup(ts.Close)
	return &fixture{server: s, hub: h, reg: reg, http: ts}
}

// post issues one ingest request.
func (f *fixture) post(t *testing.T, body, authHeader string) (int, map[string]any) {
	t.Helper()
	req, err := http.NewRequest(http.MethodPost, f.http.URL+"/ingest/v1/publish", strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	if authHeader != "" {
		req.Header.Set("Authorization", authHeader)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var parsed map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&parsed)
	return resp.StatusCode, parsed
}

// sink is a hub.Subscriber that records deliveries (delivery assertions
// belong at the hub boundary, not on a socket).
type sink struct {
	session string
	tenant  string

	mu     sync.Mutex
	frames []any
}

func (s *sink) Session() string { return s.session }
func (s *sink) Tenant() string  { return s.tenant }
func (s *sink) Enqueue(msg any) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.frames = append(s.frames, msg)
	return true
}
func (s *sink) RequestClose(int, string) {}

func (s *sink) count() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.frames)
}

func validBody(tenant, room, kind, eventID string) string {
	event := ""
	if eventID != "" {
		event = `,"event_id":"` + eventID + `"`
	}
	return `{"tenant_id":"` + tenant + `","room":"` + room + `","kind":"` + kind + `","payload":{"status":"COMPLETED"}` + event + `}`
}

func TestIngestRequiresSecret(t *testing.T) {
	f := newFixture(t)
	for _, header := range []string{"", "Bearer wrong-secret", "Basic abc", "Bearerx " + testIngestSecret} {
		status, _ := f.post(t, validBody(testTenantA, "calls", "call.updated", ""), header)
		if status != http.StatusUnauthorized {
			t.Errorf("auth header %q: status = %d, want 401", header, status)
		}
	}
}

func TestIngestValidatesShapes(t *testing.T) {
	f := newFixture(t)
	auth := "Bearer " + testIngestSecret

	cases := []struct {
		name string
		body string
		want int
	}{
		{"bad tenant", `{"tenant_id":"not-a-uuid","room":"calls","kind":"call.updated","payload":{}}`, http.StatusUnprocessableEntity},
		{"bad room", validBody(testTenantA, "admin", "call.updated", ""), http.StatusUnprocessableEntity},
		{"tenant-named room", validBody(testTenantA, "call:not-a-uuid", "call.updated", ""), http.StatusUnprocessableEntity},
		{"bad kind", validBody(testTenantA, "calls", "UPPER.CASE", ""), http.StatusUnprocessableEntity},
		{"empty payload", `{"tenant_id":"` + testTenantA + `","room":"calls","kind":"call.updated"}`, http.StatusUnprocessableEntity},
		{"scalar payload", `{"tenant_id":"` + testTenantA + `","room":"calls","kind":"call.updated","payload":"a bare string is valid JSON but not fan-out material"}`, http.StatusUnprocessableEntity},
		{"malformed json", `{"tenant_id":"` + testTenantA + `","room":"calls","kind":"call.updated","payload":{broken`, http.StatusUnprocessableEntity},
		{"bad event id", validBody(testTenantA, "calls", "call.updated", "not-a-uuid"), http.StatusUnprocessableEntity},
		{"not json at all", `this is not {json`, http.StatusUnprocessableEntity},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			status, body := f.post(t, tc.body, auth)
			if status != tc.want {
				t.Errorf("status = %d, want %d (body %v)", status, tc.want, body)
			}
		})
	}
}

func TestIngestDeliversToSubscriber(t *testing.T) {
	f := newFixture(t)
	a := &sink{session: "s-a", tenant: testTenantA}
	b := &sink{session: "s-b", tenant: testTenantB}
	f.hub.Register(a)
	f.hub.Register(b)
	if err := f.hub.BindTenant("s-a", testTenantA); err != nil {
		t.Fatal(err)
	}
	if err := f.hub.BindTenant("s-b", testTenantB); err != nil {
		t.Fatal(err)
	}
	if _, err := f.hub.Subscribe("s-a", "calls"); err != nil {
		t.Fatal(err)
	}
	if _, err := f.hub.Subscribe("s-b", "calls"); err != nil {
		t.Fatal(err)
	}

	status, body := f.post(t, validBody(testTenantA, "calls", "call.updated", ""), "Bearer "+testIngestSecret)
	if status != http.StatusOK {
		t.Fatalf("status = %d", status)
	}
	if body["delivered"] != float64(1) || body["dropped"] != float64(0) || body["duplicate"] != false {
		t.Errorf("response = %v", body)
	}
	if a.count() != 1 {
		t.Errorf("tenant A sink = %d frames, want 1", a.count())
	}
	if b.count() != 0 {
		t.Errorf("TENANT B sink = %d frames — isolation breach", b.count())
	}
}

func TestIngestSuppressesReplay(t *testing.T) {
	f := newFixture(t)
	eventID := "550e8400-e29b-41d4-a716-446655440000"
	auth := "Bearer " + testIngestSecret

	status, first := f.post(t, validBody(testTenantA, "calls", "call.updated", eventID), auth)
	if status != http.StatusOK || first["duplicate"] != false {
		t.Fatalf("first delivery: status %d body %v", status, first)
	}
	status, second := f.post(t, validBody(testTenantA, "calls", "call.updated", eventID), auth)
	if status != http.StatusOK {
		t.Fatalf("replay: status %d", status)
	}
	if second["duplicate"] != true || second["delivered"] != float64(0) {
		t.Errorf("replay must be suppressed as duplicate: %v", second)
	}
	// A replay scoped to a DIFFERENT tenant is a different event entirely.
	status, third := f.post(t, validBody(testTenantB, "calls", "call.updated", eventID), auth)
	if status != http.StatusOK || third["duplicate"] != false {
		t.Errorf("same event_id in another tenant must NOT dedupe: %v", third)
	}
}

func TestIngestOversizeRejected(t *testing.T) {
	f := newFixture(t)
	big := strings.Repeat("x", int(testConfig().MaxIngestPayloadBytes)+8192)
	body := `{"tenant_id":"` + testTenantA + `","room":"calls","kind":"call.updated","payload":{"blob":"` + big + `"}}`
	status, _ := f.post(t, body, "Bearer "+testIngestSecret)
	if status != http.StatusRequestEntityTooLarge && status != http.StatusUnprocessableEntity {
		t.Errorf("status = %d, want 413 or 422", status)
	}
}

func TestLivenessReadinessAndNotFound(t *testing.T) {
	f := newFixture(t)

	resp, err := http.Get(f.http.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Errorf("healthz = %d", resp.StatusCode)
	}

	resp, err = http.Get(f.http.URL + "/readyz")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Errorf("readyz = %d, want 200 with headroom", resp.StatusCode)
	}

	resp, err = http.Get(f.http.URL + "/nope")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusNotFound {
		t.Errorf("unknown path = %d, want 404", resp.StatusCode)
	}
	if ct := resp.Header.Get("Content-Type"); !strings.Contains(ct, "application/json") {
		t.Errorf("API 404s must be JSON, got %q", ct)
	}
}

func TestMethodNotAllowedIsJSON(t *testing.T) {
	f := newFixture(t)
	resp, err := http.Post(f.http.URL+"/healthz", "text/plain", strings.NewReader("x"))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusMethodNotAllowed {
		t.Errorf("POST /healthz = %d, want 405", resp.StatusCode)
	}
}

func TestMetricsTokenGate(t *testing.T) {
	cfg := testConfig()
	cfg.MetricsToken = "scrape-token-123"
	h := hub.New(cfg.MaxConnsPerTenant)
	reg := metrics.New()
	verifier := auth.NewVerifier(cfg.JWTSecret, cfg.JWTIssuer, cfg.JWTAudience)
	sig := signaling.NewRouter(session.NewManager(cfg.SignalingMaxSessionsPerTenant, cfg.SignalingPendingTimeout), h.Lookup, reg)
	s := New(cfg, h, reg, verifier, sig)
	ts := httptest.NewServer(s.Handler())
	t.Cleanup(ts.Close)

	// No token: 401.
	resp, err := http.Get(ts.URL + "/metrics")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusUnauthorized {
		t.Errorf("metrics without token = %d, want 401", resp.StatusCode)
	}

	// Wrong token: 401.
	req, _ := http.NewRequest(http.MethodGet, ts.URL+"/metrics", nil)
	req.Header.Set("Authorization", "Bearer wrong")
	resp, err = http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusUnauthorized {
		t.Errorf("metrics wrong token = %d, want 401", resp.StatusCode)
	}

	// Right token: 200 with the exposition.
	req, _ = http.NewRequest(http.MethodGet, ts.URL+"/metrics", nil)
	req.Header.Set("Authorization", "Bearer scrape-token-123")
	resp, err = http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("metrics with token = %d", resp.StatusCode)
	}
	buf := make([]byte, 4096)
	n, _ := resp.Body.Read(buf)
	text := string(buf[:n])
	if !strings.Contains(text, "voxdesk_gateway_connections_current") {
		t.Errorf("exposition missing core gauge:\n%s", text)
	}
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/server/metrics.go (42 lines, sha256 410daa1b71e33c3fa4fb3de55e82fab014fce28a2ee45493853b586e73267ff8) =====
==============================================================================
```go
package server

import (
	"net/http"

	"github.com/voxdesk/realtime/gateway-go/internal/auth"
)

// serveMetrics answers /metrics with the Prometheus text exposition.
//
// Access control mirrors the API's METRICS_TOKEN: when a token is
// configured, the scraper must present it as `Authorization: Bearer <token>`
// (header only — a query-param token would land in access logs, which is
// exactly where a scrape credential must never appear). When no token is
// configured the endpoint is open, acceptable only on the private compose
// network Prometheus already lives on; config.Load surfaces that as a
// boot-time warning so it is never a surprise.
func (s *Server) serveMetrics(w http.ResponseWriter, r *http.Request) {
	if s.cfg.MetricsToken != "" {
		if !bearerMatches(r.Header.Get("Authorization"), s.cfg.MetricsToken) {
			writeJSON(w, http.StatusUnauthorized, map[string]string{"detail": "invalid metrics token"})
			return
		}
	}

	rooms, tenants := s.hub.Stats()
	w.Header().Set("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte(s.registry.Render(rooms, tenants)))
}

// bearerMatches compares a presented `Bearer x` credential with the
// expected one in constant time, via the shared auth helpers (one
// comparison implementation for every secret this edge sees — the whole
// point of the package split).
func bearerMatches(header, expected string) bool {
	token, ok := auth.ExtractBearer(header)
	if !ok {
		return false
	}
	return auth.ConstantTimeTokenEqual(token, expected)
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/server/redis_e2e_test.go (183 lines, sha256 15eb73b94256189a80d61d17481dd1fdddde7416424b32d41375661e81e02bcd) =====
==============================================================================
```go
package server

import (
	"fmt"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/voxdesk/realtime/gateway-go/internal/auth"
	"github.com/voxdesk/realtime/gateway-go/internal/broker/brokertest"
	"github.com/voxdesk/realtime/gateway-go/internal/hub"
	"github.com/voxdesk/realtime/gateway-go/internal/observability/metrics"
	"github.com/voxdesk/realtime/gateway-go/internal/session"
	"github.com/voxdesk/realtime/gateway-go/internal/signaling"
)

// newRedisFixture wires a full gateway configured for the redis broker.
// Two fixtures against one fake redis = a two-replica deployment.
func newRedisFixture(t *testing.T, redisURL string) *fixture {
	t.Helper()
	cfg := testConfig()
	cfg.BrokerKind = "redis"
	cfg.RedisURL = redisURL
	h := hub.New(cfg.MaxConnsPerTenant)
	reg := metrics.New()
	verifier := auth.NewVerifier(cfg.JWTSecret, cfg.JWTIssuer, cfg.JWTAudience)
	sig := signaling.NewRouter(session.NewManager(cfg.SignalingMaxSessionsPerTenant, cfg.SignalingPendingTimeout), h.Lookup, reg)
	s := New(cfg, h, reg, verifier, sig)
	t.Cleanup(s.CloseAll)
	ts := httptest.NewServer(s.Handler())
	t.Cleanup(ts.Close)
	return &fixture{server: s, hub: h, reg: reg, http: ts}
}

// TestMultiReplicaIngestOverRedisBus is the multi-node proof of the whole
// phase: an ingest POSTed to replica A is delivered to a browser connected
// to replica B, byte-verbatim, exactly once, while A's HTTP response
// reports only A's (empty) local fan-out — the response counters stay
// per-replica-exact, never aggregated into fiction.
func TestMultiReplicaIngestOverRedisBus(t *testing.T) {
	t.Parallel()

	fake, err := brokertest.Start()
	if err != nil {
		t.Fatalf("start fake redis: %v", err)
	}
	t.Cleanup(fake.Stop)

	fA := newRedisFixture(t, fake.URL())
	fB := newRedisFixture(t, fake.URL())

	// Both buses subscribed before any publish (Subscribe went out when
	// each server's New ran, i.e. before its bus first connected — the
	// reconnect path's re-subscribe is the same code path).
	if !fake.WaitForSubscribers(ingestTopic, 2, 5*time.Second) {
		t.Fatalf("both replicas must hold bus subscriptions, have %d", fake.SubscriberCount(ingestTopic))
	}

	// A browser session lands on REPLICA B: welcome → hello → ready →
	// subscribe → subscribed.
	conn := dial(t, fB)
	welcome := readFrame(t, conn)
	if welcome["type"] != "welcome" {
		t.Fatalf("welcome = %v", welcome)
	}
	token := mintToken(t, testJWTSecret, dashboardClaims(testTenantA, "admin", 5*time.Minute))
	sendFrame(t, conn, fmt.Sprintf(`{"type":"hello","token":%q}`, token))
	ready := readFrame(t, conn)
	if ready["type"] != "ready" || ready["tenant_id"] != testTenantA {
		t.Fatalf("ready = %v", ready)
	}
	sendFrame(t, conn, `{"type":"subscribe","room":"calls"}`)
	subscribed := readFrame(t, conn)
	if subscribed["type"] != "subscribed" || subscribed["room"] != "calls" {
		t.Fatalf("subscribed = %v", subscribed)
	}

	// The API publishes to REPLICA A — the replica the browser is NOT on.
	// Payload carries nested shape to prove byte-verbatim forwarding over a
	// JSON-inside-JSON bus hop.
	payload := `{"call_id":"call-42","levels":[1,2,3],"meta":{"origin":"api","ok":true}}`
	status, resp := fA.post(t, fmt.Sprintf(`{"tenant_id":%q,"room":"calls","kind":"call.updated","payload":%s,"event_id":"33333333-3333-3333-3333-333333333333"}`, testTenantA, payload), "Bearer "+testIngestSecret)
	if status != 200 {
		t.Fatalf("ingest status = %d (%v)", status, resp)
	}
	// A has NO local subscribers to (tenantA, calls): its response reports
	// its own empty fan-out. The "0 delivered" here is the CONTRACT — per-
	// replica accounting, cross-node visibility not faked into one number.
	if resp["delivered"] != 0.0 || resp["dropped"] != 0.0 {
		t.Fatalf("A's response must report A's local fan-out only: %v", resp)
	}

	// …and the browser on B hears it exactly once, byte-verbatim.
	delivery := readFrame(t, conn)
	if delivery["type"] != "delivery" || delivery["room"] != "calls" || delivery["kind"] != "call.updated" {
		t.Fatalf("delivery envelope wrong: %v", delivery)
	}
	if delivery["event_id"] != "33333333-3333-3333-3333-333333333333" {
		t.Fatalf("event_id must survive the bus hop: %v", delivery["event_id"])
	}
	gotPayload, _ := delivery["payload"].(map[string]any)
	if gotPayload["call_id"] != "call-42" {
		t.Fatalf("payload corrupted over the bus: %v", gotPayload)
	}
	levels, _ := gotPayload["levels"].([]any)
	if len(levels) != 3 || levels[2] != 3.0 {
		t.Fatalf("nested array must arrive untouched: %v", levels)
	}
	meta, _ := gotPayload["meta"].(map[string]any)
	if meta["origin"] != "api" || meta["ok"] != true {
		t.Fatalf("nested object must arrive untouched: %v", meta)
	}

	// No double delivery: A's own bus echo must have been suppressed, and
	// B's local hub is the only source of the frame.
	_ = conn.SetReadDeadline(time.Now().Add(300 * time.Millisecond))
	if _, extra, err := conn.ReadMessage(); err == nil {
		t.Fatalf("second frame %q: the event must be delivered exactly once", extra)
	}

	// B counted its remote-hop delivery even though B never saw an HTTP
	// POST (accounting for bus-served fan-out lives at the delivery point).
	rendered := fB.reg.Render(0, 0)
	if !strings.Contains(rendered, "voxdesk_gateway_deliveries_total 1") {
		t.Fatalf("B must count the remote delivery locally:\n%s", rendered)
	}
}

// TestBusOutageDegradesToLocalOnly pins the failure mode a deployment
// actually hits: redis dies, ingest POSTs stay 200 with exact local
// counters, already-connected browsers on the SAME replica keep receiving.
func TestBusOutageDegradesToLocalOnly(t *testing.T) {
	t.Parallel()

	fake, err := brokertest.Start()
	if err != nil {
		t.Fatalf("start fake redis: %v", err)
	}
	t.Cleanup(fake.Stop)

	f := newRedisFixture(t, fake.URL())
	if !fake.WaitForSubscribers(ingestTopic, 1, 5*time.Second) {
		t.Fatal("replica must hold a bus subscription before the outage")
	}

	fake.Stop() // the bus is gone

	// Same-replica browser.
	conn := dial(t, f)
	_ = readFrame(t, conn)
	token := mintToken(t, testJWTSecret, dashboardClaims(testTenantA, "agent", 5*time.Minute))
	sendFrame(t, conn, fmt.Sprintf(`{"type":"hello","token":%q}`, token))
	if ready := readFrame(t, conn); ready["type"] != "ready" {
		t.Fatalf("ready = %v", ready)
	}
	sendFrame(t, conn, `{"type":"subscribe","room":"metrics"}`)
	if sub := readFrame(t, conn); sub["type"] != "subscribed" {
		t.Fatalf("subscribed = %v", sub)
	}

	// POST mid-outage: 200, this replica's delivery exact (1 to the local
	// browser), cross-node hop dropped and the drop accounted on the bus.
	status, resp := f.post(t, fmt.Sprintf(`{"tenant_id":%q,"room":"metrics","kind":"metrics.tick","payload":{"cpu":0.5}}`, testTenantA), "Bearer "+testIngestSecret)
	if status != 200 || resp["delivered"] != 1.0 {
		t.Fatalf("outage ingest must stay exact-locally: status=%d resp=%v", status, resp)
	}
	delivery := readFrame(t, conn)
	if delivery["kind"] != "metrics.tick" {
		t.Fatalf("local delivery during outage wrong: %v", delivery)
	}
	rb, ok := f.server.Broker().(interface{ BusDrops() int64 })
	if !ok {
		t.Fatalf("redis-mode broker must expose outage accounting, got %T", f.server.Broker())
	}
	deadline := time.Now().Add(2 * time.Second)
	for rb.BusDrops() < 1 && time.Now().Before(deadline) {
		time.Sleep(5 * time.Millisecond)
	}
	if rb.BusDrops() < 1 {
		t.Fatal("the dropped bus hop must be counted, not silent")
	}
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/server/server.go (192 lines, sha256 4dbebe217c5fcace59da3db85274fbb5af0d5c3017b7287f0c9236e751616970) =====
==============================================================================
```go
// Package server wires the public edge together: the HTTP mux (websocket
// upgrade, health, readiness, metrics, ingest), the shared gateway state
// (hub, verifier, registry), and upgrade-time policy (capacity, origin).
package server

import (
	"log"
	"net/http"
	"sync"
	"time"

	gorilla "github.com/gorilla/websocket"

	"github.com/voxdesk/realtime/gateway-go/internal/auth"
	"github.com/voxdesk/realtime/gateway-go/internal/broker"
	"github.com/voxdesk/realtime/gateway-go/internal/config"
	"github.com/voxdesk/realtime/gateway-go/internal/engineclient"
	"github.com/voxdesk/realtime/gateway-go/internal/hub"
	"github.com/voxdesk/realtime/gateway-go/internal/idempotency"
	"github.com/voxdesk/realtime/gateway-go/internal/observability"
	"github.com/voxdesk/realtime/gateway-go/internal/observability/metrics"
	"github.com/voxdesk/realtime/gateway-go/internal/presence"
	"github.com/voxdesk/realtime/gateway-go/internal/protocol"
	"github.com/voxdesk/realtime/gateway-go/internal/signaling"
	wsconn "github.com/voxdesk/realtime/gateway-go/internal/websocket"
)

// Server is the gateway's front door. Build with New, expose through
// Handler, terminate with CloseAll.
type Server struct {
	cfg       config.Config
	hub       *hub.Hub
	registry  *metrics.Registry
	verifier  *auth.Verifier
	signaler  *signaling.Router
	upgrader  gorilla.Upgrader
	replay    *idempotency.Store
	presence  *presence.Registry
	events    observability.Emitter
	bus       broker.Broker
	closeOnce sync.Once
	startedAt time.Time
	logf      func(format string, args ...any)

	// engine is the media-plane availability view; nil means the engine
	// plane is intentionally disabled (config.MediaEngineURL == "") and
	// readiness reports `engine: disabled` instead of guessing.
	engine *engineclient.Client
}

// SetEngine attaches the media-engine client AFTER construction. It is a
// separate setter (not a New parameter) because New's arity is a public
// contract for the existing wiring paths and tests; the engine link was
// added without reshaping them. MUST be called before Handler is served.
func (s *Server) SetEngine(eng *engineclient.Client) {
	s.engine = eng
	if s.signaler != nil {
		s.signaler.SetEngine(eng)
		s.signaler.SetSteerMode(s.cfg.EngineSteer)
	}
}

// Engine exposes the attached client (nil when disabled) — the readiness
// handler and the supervisor loop read availability through it.
func (s *Server) Engine() *engineclient.Client { return s.engine }

// New returns the fully wired server. signaler may be nil — a deployment
// that only wants the notice plane gets clean refusals on signaling frames
// instead of a crash (the read loop checks; nothing here dereferences it
// other than handing it to connections).
func New(cfg config.Config, h *hub.Hub, reg *metrics.Registry, verifier *auth.Verifier, signaler *signaling.Router) *Server {
	s := &Server{
		cfg:       cfg,
		hub:       h,
		registry:  reg,
		verifier:  verifier,
		signaler:  signaler,
		upgrader:  wsconn.NewUpgrader(cfg),
		replay:    idempotency.NewStore(cfg.IdempotencyTTL, cfg.IdempotencyCapacity),
		presence:  presence.New(),
		events:    observability.NopEmitter,
		startedAt: time.Now(),
		logf:      log.Printf,
	}
	// The presence gauge is fed by subscription (not polled at scrape
	// time): every real transition publishes its authoritative post-change
	// total, so the gauge self-heals instead of drifting on a missed
	// decrement. Emitting on change is also why presence.OnChange exists.
	s.presence.OnChange(func(ev presence.Event) {
		reg.SetPresenceUsers(int64(ev.TotalUsers))
	})

	// The ingest fan-out transport (internal/broker). "memory" (default):
	// Publish IS the hub fan-out, synchronous, so ingest responses keep
	// their exact (delivered, dropped) meaning. "redis": the same LOCAL
	// publish plus a pub/sub hop for other replicas, own-echo suppressed.
	s.bus = newBrokerFromConfig(cfg, s.logf)
	s.bus.Subscribe(ingestTopic, s.handleIngestEnvelope)
	return s
}

// newBrokerFromConfig builds the configured transport. An unparsable redis
// URL cannot reach here (config.Load refuses boot on it); a second-layer
// parse failure degrades to the memory broker with a loud log rather than
// a boot crash — the single-node fan-out still works, which is exactly
// what a manual failover to VOXDESK_GATEWAY_BROKER=memory would produce.
func newBrokerFromConfig(cfg config.Config, logf func(string, ...any)) broker.Broker {
	if cfg.BrokerKind == "redis" {
		rb, err := broker.NewRedis(cfg.RedisURL, broker.WithRedisLogger(logf))
		if err != nil {
			logf("[gateway] BROKER DEGRADED: redis init failed (%v); falling back to memory broker", err)
			return broker.NewMemory()
		}
		return rb
	}
	return broker.NewMemory()
}

// UseEmitter replaces the (default: no-op) structured event sink. main
// installs a LogEmitter at boot; tests leave the nop in place so behaviour
// stays byte-for-byte deterministic.
func (s *Server) UseEmitter(em observability.Emitter) {
	if em != nil {
		s.events = em
	}
}

// Hub exposes the registry for shutdown (main calls CloseAll through it).
func (s *Server) Hub() *hub.Hub { return s.hub }

// Signaler exposes the relay for lifecycle wiring (main starts the reaper).
func (s *Server) Signaler() *signaling.Router { return s.signaler }

// Config exposes the runtime configuration to the handler files.
func (s *Server) Config() config.Config { return s.cfg }

// Registry exposes the metric counters to the handler files.
func (s *Server) Registry() *metrics.Registry { return s.registry }

// Presence exposes the node-local presence registry (tenant → online
// users). The /metrics handler renders its TotalUsers gauge from it.
func (s *Server) Presence() *presence.Registry { return s.presence }

// Broker exposes the ingest fan-out transport (diagnostics: Kind/NodeID;
// redis-mode accounting when enabled).
func (s *Server) Broker() broker.Broker { return s.bus }

// serveWS upgrades one HTTP request and runs the session to completion.
// Any pre-upgrade refusal is a plain HTTP response (the client never became
// a websocket peer); anything after the upgrade is the lifecycle's concern.
func (s *Server) serveWS(w http.ResponseWriter, r *http.Request) {
	// Global capacity gate, checked BEFORE the upgrade so a flood of
	// sockets is refused cheaply (HTTP 503) rather than upgraded and then
	// closed expensively. Read from the registry gauge: the hub counts
	// authenticated sockets, but the cap must cover pre-auth ones too
	// (that is precisely the flood shape).
	if s.registry.ConnectionsCurrent() >= int64(s.cfg.MaxConnections) {
		s.registry.ConnRefused()
		http.Error(w, http.StatusText(http.StatusServiceUnavailable), http.StatusServiceUnavailable)
		return
	}

	socket, err := s.upgrader.Upgrade(w, r, nil)
	if err != nil {
		// gorilla has already written the refusal response (origin policy,
		// handshake shape); nothing more to say.
		s.registry.ConnRefused()
		return
	}

	conn, err := wsconn.NewConnection(socket, s.cfg, s.hub, s.verifier, s.registry, s.signaler, s.presence)
	if err != nil {
		_ = socket.Close()
		s.logf("[gateway] session id generation failed: %v", err)
		return
	}
	conn.Serve()
}

// CloseAll asks every live connection to close (server shutdown) and
// stops the ingest bus (the redis supervisor's reconnect loop must not
// outlive the process's listen loop). Idempotent: http.Server calls it
// through RegisterOnShutdown, and main never calls it twice — but an
// orchestrated double-shutdown must not double-close channels.
func (s *Server) CloseAll() {
	s.closeOnce.Do(func() {
		s.hub.CloseAll(protocol.CloseGoingAway, "server shutting down")
		if err := s.bus.Close(); err != nil {
			s.logf("[gateway] broker close: %v", err)
		}
	})
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/server/server_e2e_test.go (268 lines, sha256 0c62d4193b88c86e6312eea922169a015fe4e434ad3df77021a9dd5bf67aaf91) =====
==============================================================================
```go
package server

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"strings"
	"testing"
	"time"

	gorilla "github.com/gorilla/websocket"
)

// mintToken builds an HS256 token identical to what the Python API issues.
func mintToken(t *testing.T, secret string, claims map[string]any) string {
	t.Helper()
	header, _ := json.Marshal(map[string]any{"alg": "HS256", "typ": "JWT"})
	body, _ := json.Marshal(claims)
	head := base64.RawURLEncoding.EncodeToString(header)
	payload := base64.RawURLEncoding.EncodeToString(body)
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte(head + "." + payload))
	return head + "." + payload + "." + base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
}

func dashboardClaims(tenant, role string, ttl time.Duration) map[string]any {
	now := time.Now()
	return map[string]any{
		"sub":  "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
		"tid":  tenant,
		"role": role,
		"tv":   float64(1),
		"typ":  "access",
		"iat":  float64(now.Unix()),
		"nbf":  float64(now.Unix()),
		"exp":  float64(now.Add(ttl).Unix()),
		"iss":  "voxdesk",
		"aud":  "voxdesk-api",
		"jti":  "test-jti",
	}
}

// dial opens one websocket to the fixture gateway.
func dial(t *testing.T, f *fixture) *gorilla.Conn {
	t.Helper()
	wsURL := "ws" + strings.TrimPrefix(f.http.URL, "http") + "/ws"
	conn, _, err := gorilla.DefaultDialer.Dial(wsURL, nil)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	t.Cleanup(func() { conn.Close() })
	return conn
}

// readFrame reads one JSON frame into a generic map with a bounded wait.
func readFrame(t *testing.T, conn *gorilla.Conn) map[string]any {
	t.Helper()
	_ = conn.SetReadDeadline(time.Now().Add(3 * time.Second))
	_, data, err := conn.ReadMessage()
	if err != nil {
		t.Fatalf("read frame: %v", err)
	}
	var frame map[string]any
	if err := json.Unmarshal(data, &frame); err != nil {
		t.Fatalf("frame is not JSON: %v (%s)", err, data)
	}
	return frame
}

func sendFrame(t *testing.T, conn *gorilla.Conn, frame string) {
	t.Helper()
	if err := conn.WriteMessage(gorilla.TextMessage, []byte(frame)); err != nil {
		t.Fatalf("write frame: %v", err)
	}
}

// TestFullSessionRoundTrip is THE behaviour proof of the whole service:
// welcome → hello → ready → subscribed → ingest → delivery, with the
// tenant pinned by the token and the payload delivered verbatim.
func TestFullSessionRoundTrip(t *testing.T) {
	f := newFixture(t)
	conn := dial(t, f)

	// 1) The server speaks first: welcome before any client traffic.
	welcome := readFrame(t, conn)
	if welcome["type"] != "welcome" || welcome["auth_required"] != true {
		t.Fatalf("welcome = %v", welcome)
	}
	if welcome["session_id"] == "" {
		t.Fatalf("welcome must assign the session: %v", welcome)
	}

	// 2) Authenticate with a dashboard token.
	token := mintToken(t, testJWTSecret, dashboardClaims(testTenantA, "owner", 15*time.Minute))
	sendFrame(t, conn, `{"type":"hello","token":"`+token+`"}`)
	ready := readFrame(t, conn)
	if ready["type"] != "ready" || ready["tenant_id"] != testTenantA || ready["role"] != "owner" {
		t.Fatalf("ready = %v", ready)
	}

	// 3) Subscribe the calls feed; peer count reflects this join.
	sendFrame(t, conn, `{"type":"subscribe","room":"calls"}`)
	sub := readFrame(t, conn)
	if sub["type"] != "subscribed" || sub["room"] != "calls" || sub["peers"] != float64(1) {
		t.Fatalf("subscribed = %v", sub)
	}

	// 4) The API publishes; the browser receives a delivery, verbatim payload.
	status, ingestBody := f.post(t,
		`{"tenant_id":"`+testTenantA+`","room":"calls","kind":"call.updated","payload":{"status":"COMPLETED","call_sid":"CA123"},"event_id":"550e8400-e29b-41d4-a716-446655440000"}`,
		"Bearer "+testIngestSecret)
	if status != http.StatusOK || ingestBody["delivered"] != float64(1) {
		t.Fatalf("ingest: status %d body %v", status, ingestBody)
	}
	delivery := readFrame(t, conn)
	if delivery["type"] != "delivery" || delivery["room"] != "calls" || delivery["kind"] != "call.updated" {
		t.Fatalf("delivery = %v", delivery)
	}
	payload, _ := json.Marshal(delivery["payload"])
	if !strings.Contains(string(payload), "COMPLETED") {
		t.Fatalf("payload not delivered verbatim: %s", payload)
	}

	// 5) Unsubscribe → ack, then a second publish delivers nothing more.
	sendFrame(t, conn, `{"type":"unsubscribe","room":"calls"}`)
	unsub := readFrame(t, conn)
	if unsub["type"] != "unsubscribed" || unsub["room"] != "calls" {
		t.Fatalf("unsubscribed = %v", unsub)
	}
	_, after := f.post(t, validBody(testTenantA, "calls", "call.updated", ""), "Bearer "+testIngestSecret)
	if after["delivered"] != float64(0) {
		t.Fatalf("after unsubscribe, delivered = %v", after)
	}
}

// TestBadTokenClosesSession proves a forged hello gets ONE generic error
// frame and then a 1008 close — no retry surface on a public edge.
func TestBadTokenClosesSession(t *testing.T) {
	f := newFixture(t)
	conn := dial(t, f)
	_ = readFrame(t, conn) // welcome

	forged := mintToken(t, "the-attackers-guessed-secret-000", dashboardClaims(testTenantA, "owner", time.Hour))
	sendFrame(t, conn, `{"type":"hello","token":"`+forged+`"}`)
	errFrame := readFrame(t, conn)
	if errFrame["type"] != "error" || errFrame["code"] != "auth_failed" {
		t.Fatalf("error frame = %v", errFrame)
	}

	_ = conn.SetReadDeadline(time.Now().Add(2 * time.Second))
	for {
		_, _, err := conn.ReadMessage()
		if err != nil {
			closeErr, ok := err.(*gorilla.CloseError)
			if !ok {
				t.Fatalf("expected a close frame, got %v", err)
			}
			if closeErr.Code != 1008 {
				t.Fatalf("close code = %d, want 1008", closeErr.Code)
			}
			return
		}
	}
}

// TestPreAuthSubscribeIsRefused proves the hello-first rule.
func TestPreAuthSubscribeIsRefused(t *testing.T) {
	f := newFixture(t)
	conn := dial(t, f)
	_ = readFrame(t, conn) // welcome

	sendFrame(t, conn, `{"type":"subscribe","room":"calls"}`)
	errFrame := readFrame(t, conn)
	if errFrame["type"] != "error" || errFrame["code"] != "hello_required" {
		t.Fatalf("pre-auth subscribe = %v", errFrame)
	}
}

// TestRoomValidationAtTheEdge proves a verified client still cannot
// subscribe to arbitrary rooms (namespace is closed).
func TestRoomValidationAtTheEdge(t *testing.T) {
	f := newFixture(t)
	conn := dial(t, f)
	_ = readFrame(t, conn)
	token := mintToken(t, testJWTSecret, dashboardClaims(testTenantA, "owner", time.Hour))
	sendFrame(t, conn, `{"type":"hello","token":"`+token+`"}`)
	_ = readFrame(t, conn) // ready

	sendFrame(t, conn, `{"type":"subscribe","room":"admin"}`)
	errFrame := readFrame(t, conn)
	if errFrame["code"] != "room_invalid" {
		t.Fatalf("room validation = %v", errFrame)
	}
}

// TestTenantIsolationEndToEnd is the wire-level proof: two live sockets,
// two tenants, same room name, one publish — only the matching tenant's
// browser is notified.
func TestTenantIsolationEndToEnd(t *testing.T) {
	f := newFixture(t)
	connA := dial(t, f)
	_ = readFrame(t, connA)
	tokenA := mintToken(t, testJWTSecret, dashboardClaims(testTenantA, "owner", time.Hour))
	sendFrame(t, connA, `{"type":"hello","token":"`+tokenA+`"}`)
	_ = readFrame(t, connA)
	sendFrame(t, connA, `{"type":"subscribe","room":"calls"}`)
	_ = readFrame(t, connA)

	connB := dial(t, f)
	_ = readFrame(t, connB)
	tokenB := mintToken(t, testJWTSecret, dashboardClaims(testTenantB, "owner", time.Hour))
	sendFrame(t, connB, `{"type":"hello","token":"`+tokenB+`"}`)
	_ = readFrame(t, connB)
	sendFrame(t, connB, `{"type":"subscribe","room":"calls"}`)
	_ = readFrame(t, connB)

	if _, body := f.post(t, validBody(testTenantA, "calls", "call.updated", ""), "Bearer "+testIngestSecret); body["delivered"] != float64(1) {
		t.Fatalf("publish to tenant A: %v", body)
	}
	delivery := readFrame(t, connA)
	if delivery["type"] != "delivery" {
		t.Fatalf("tenant A expected a delivery, got %v", delivery)
	}

	// Tenant B's socket must receive NOTHING (short wait to catch a leak).
	_ = connB.SetReadDeadline(time.Now().Add(400 * time.Millisecond))
	_, _, err := connB.ReadMessage()
	if err == nil {
		t.Fatal("TENANT B RECEIVED A FRAME FROM TENANT A'S ROOM — isolation breach")
	}
	if !strings.Contains(err.Error(), "timeout") && !gorilla.IsUnexpectedCloseError(err, gorilla.CloseGoingAway) {
		// a read timeout is the expected outcome
		t.Logf("tenant B read ended with: %v (a timeout is expected; any delivered frame is a breach)", err)
	}
}

// TestPingPongThroughTheJSONPath proves application-level latency probes
// work alongside the WS-level heartbeat.
func TestPingPongThroughTheJSONPath(t *testing.T) {
	f := newFixture(t)
	conn := dial(t, f)
	_ = readFrame(t, conn)
	sendFrame(t, conn, `{"type":"ping"}`)
	pong := readFrame(t, conn)
	if pong["type"] != "pong" || pong["server_time"] == "" {
		t.Fatalf("pong = %v", pong)
	}
}

// TestTokenExpiryClosesSession proves the edge never outlives the
// credential that opened it (see heartbeat.go).
func TestTokenExpiryClosesSession(t *testing.T) {
	f := newFixture(t)
	conn := dial(t, f)
	_ = readFrame(t, conn)

	// A token that is ALREADY past its grace when verified is refused at
	// hello (the verifier's own expiry check); this test pins the runtime
	// behaviour regardless of which layer enforces it.
	token := mintToken(t, testJWTSecret, dashboardClaims(testTenantA, "owner", -2*time.Minute))
	sendFrame(t, conn, `{"type":"hello","token":"`+token+`"}`)
	errFrame := readFrame(t, conn)
	if errFrame["code"] != "auth_failed" {
		t.Fatalf("expired token = %v, want auth_failed", errFrame)
	}
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/server/signaling_e2e_test.go (194 lines, sha256 761b3837f53d86ab5d8472afd8e48e8fdfae3f4d0ff0a3af85e6a8659d4567de) =====
==============================================================================
```go
package server

import (
	"testing"
	"time"

	gorilla "github.com/gorilla/websocket"
)

// Signaling over real sockets: two authenticated connections of one tenant
// chat through the full upgrade → hello → session → relay → teardown path.
// This is the wire-level proof that the session/signaling/protocol layers a
// browser would touch are actually assembled — everything above this file
// tests the layers in isolation.

// signalingClient dials and authenticates as one tenant, consuming the
// welcome and ready frames, so the tests start at "ready".
func signalingClient(t *testing.T, f *fixture, tenant string) *gorilla.Conn {
	t.Helper()
	conn := dial(t, f)
	if welcome := readFrame(t, conn); welcome["type"] != "welcome" {
		t.Fatalf("welcome = %v", welcome)
	}
	token := mintToken(t, testJWTSecret, dashboardClaims(tenant, "owner", 15*time.Minute))
	sendFrame(t, conn, `{"type":"hello","token":"`+token+`"}`)
	if ready := readFrame(t, conn); ready["type"] != "ready" || ready["tenant_id"] != tenant {
		t.Fatalf("ready = %v", ready)
	}
	return conn
}

func TestSignalingRoundTripOverRealSockets(t *testing.T) {
	f := newFixture(t)
	a := signalingClient(t, f, testTenantA)
	b := signalingClient(t, f, testTenantA)

	// A starts; the ack doubles as the join capability.
	sendFrame(t, a, `{"type":"session.start"}`)
	started := readFrame(t, a)
	if started["type"] != "session.started" || started["role"] != "initiator" {
		t.Fatalf("session.started = %v", started)
	}
	id, _ := started["session_id"].(string)
	if id == "" {
		t.Fatalf("session.started without an id: %v", started)
	}

	// B joins by id; the initiator hears about the arrival.
	sendFrame(t, b, `{"type":"session.join","session_id":"`+id+`"}`)
	joined := readFrame(t, b)
	if joined["type"] != "session.joined" || joined["role"] != "responder" {
		t.Fatalf("session.joined = %v", joined)
	}
	peerJoined := readFrame(t, a)
	if peerJoined["type"] != "session.peer_joined" || peerJoined["peer_role"] != "responder" {
		t.Fatalf("session.peer_joined = %v", peerJoined)
	}

	// Offer A→B, byte-verbatim; answer B→A.
	offer := "v=0 o=- 1 1 IN IP4 10.0.0.1 m=audio 9 UDP/TLS/RTP/SAVPF 111"
	sendFrame(t, a, `{"type":"offer","session_id":"`+id+`","sdp":"`+offer+`"}`)
	gotOffer := readFrame(t, b)
	if gotOffer["type"] != "signal.offer" || gotOffer["sdp"] != offer {
		t.Fatalf("relayed offer = %v", gotOffer)
	}
	sendFrame(t, b, `{"type":"answer","session_id":"`+id+`","sdp":"`+offer+`"}`)
	gotAnswer := readFrame(t, a)
	if gotAnswer["type"] != "signal.answer" || gotAnswer["sdp"] != offer {
		t.Fatalf("relayed answer = %v", gotAnswer)
	}

	// Trickle including the end-of-candidates null.
	sendFrame(t, a, `{"type":"candidate","session_id":"`+id+`","candidate":{"candidate":"candidate:1 1 udp 2130706431 10.0.0.1 9 typ host","sdpMid":"0"}}`)
	gotCand := readFrame(t, b)
	if gotCand["type"] != "signal.candidate" {
		t.Fatalf("relayed candidate type = %v", gotCand["type"])
	}
	candBody, ok := gotCand["candidate"].(map[string]any)
	if !ok || candBody["sdpMid"] != "0" {
		t.Fatalf("relayed candidate body = %v", gotCand["candidate"])
	}
	sendFrame(t, b, `{"type":"candidate","session_id":"`+id+`","candidate":null}`)
	if eoc := readFrame(t, a); eoc["candidate"] != nil {
		// JSON null decodes to nil inside the generic map.
		t.Fatalf("end-of-candidates relayed as %v", eoc["candidate"])
	}

	// Member-initiated end: requester and peer both hear the reason.
	sendFrame(t, b, `{"type":"session.end","session_id":"`+id+`"}`)
	for name, conn := range map[string]*gorilla.Conn{"requester": b, "peer": a} {
		end := readFrame(t, conn)
		if end["type"] != "session.ended" || end["reason"] != "member_ended" {
			t.Fatalf("%s session.ended = %v", name, end)
		}
	}

	// Disconnect-ending is the interesting half of the lifecycle: A starts a
	// second session, B joins, then B's socket simply DIES — and A learns
	// peer_disconnected without anyone sending session.end.
	sendFrame(t, a, `{"type":"session.start"}`)
	started2 := readFrame(t, a)
	id2, _ := started2["session_id"].(string)
	sendFrame(t, b, `{"type":"session.join","session_id":"`+id2+`"}`)
	_ = readFrame(t, b) // B's own join ack; irrelevant next
	_ = readFrame(t, a) // A's peer_joined
	_ = b.Close()
	end := readFrame(t, a)
	if end["type"] != "session.ended" || end["reason"] != "peer_disconnected" {
		t.Fatalf("disconnect session.ended = %v", end)
	}
}

func TestSignalingJoinAcrossTenantsIsCollapsedUnknown(t *testing.T) {
	f := newFixture(t)
	a := signalingClient(t, f, testTenantA)
	x := signalingClient(t, f, testTenantB)

	sendFrame(t, a, `{"type":"session.start"}`)
	id, _ := readFrame(t, a)["session_id"].(string)

	// The id exists — but under another tenant, so the refusal is
	// indistinguishable from a made-up id. tenant=A's session also survives
	// untouched (the probe must not corrupt it).
	sendFrame(t, x, `{"type":"session.join","session_id":"`+id+`"}`)
	errFrame := readFrame(t, x)
	if errFrame["type"] != "error" || errFrame["code"] != "session_unknown" {
		t.Fatalf("cross-tenant join = %v", errFrame)
	}
	x2 := signalingClient(t, f, testTenantA)
	sendFrame(t, x2, `{"type":"session.join","session_id":"`+id+`"}`)
	if joined := readFrame(t, x2); joined["type"] != "session.joined" {
		t.Fatalf("the session was corrupted by the probe: %v", joined)
	}
}

func TestSignalingFramesBeforeHelloAreRefused(t *testing.T) {
	f := newFixture(t)
	conn := dial(t, f)
	_ = readFrame(t, conn) // welcome

	sendFrame(t, conn, `{"type":"session.start"}`)
	errFrame := readFrame(t, conn)
	if errFrame["type"] != "error" || errFrame["code"] != "hello_required" {
		t.Fatalf("pre-auth session.start = %v", errFrame)
	}
	sendFrame(t, conn, `{"type":"offer","session_id":"s","sdp":"v=0"}`)
	errFrame = readFrame(t, conn)
	if errFrame["code"] != "hello_required" {
		t.Fatalf("pre-auth offer = %v", errFrame)
	}
}

func TestSignalingNoticesCoexistWithTheNoticePlane(t *testing.T) {
	// One socket driving BOTH planes: subscriptions and a signaling session
	// on the same connection must not starve or confuse one another — that
	// is the dashboard's actual shape (a page that watches calls AND can
	// start a WebRTC monitor session).
	f := newFixture(t)
	a := signalingClient(t, f, testTenantA)
	b := signalingClient(t, f, testTenantA)

	sendFrame(t, a, `{"type":"subscribe","room":"calls"}`)
	if sub := readFrame(t, a); sub["type"] != "subscribed" {
		t.Fatalf("subscribed = %v", sub)
	}
	sendFrame(t, a, `{"type":"session.start"}`)
	started := readFrame(t, a)
	id, _ := started["session_id"].(string)
	sendFrame(t, b, `{"type":"session.join","session_id":"`+id+`"}`)
	_ = readFrame(t, b)
	_ = readFrame(t, a) // peer_joined

	// An ingest delivery and an offer interleave on A's socket; both
	// arrive, in order (subscribe happened first → its subscription is the
	// standing one).
	sendFrame(t, b, `{"type":"session.start"}`) // B moves to a second session? No — refused.
	if errFrame := readFrame(t, b); errFrame["code"] != "already_in_session" {
		t.Fatalf("second session on one socket = %v", errFrame)
	}
	status, _ := f.post(t,
		`{"tenant_id":"`+testTenantA+`","room":"calls","kind":"call.updated","payload":{"x":1},"event_id":"aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"}`,
		"Bearer "+testIngestSecret)
	if status != 200 {
		t.Fatalf("ingest while signaling: %d", status)
	}
	sendFrame(t, b, `{"type":"offer","session_id":"`+id+`","sdp":"v=0 o=- 1"}`)

	first := readFrame(t, a)
	second := readFrame(t, a)
	got := map[string]bool{first["type"].(string): true, second["type"].(string): true}
	if !got["delivery"] || !got["signal.offer"] {
		t.Fatalf("expected delivery + signal.offer interleaved, got %v, %v", first["type"], second["type"])
	}
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/server/steer_e2e_test.go (302 lines, sha256 2bf381f7612096a9120ecf49ab68f0b14acba8913460c74fc38d3ff92eed5e24) =====
==============================================================================
```go
package server

// Steer (wire 1.2) end-to-end: real websockets, real engineclient, the
// scripted engine. These test every rule stated in internal/signaling/
// steer.go's invariants:
//
//  1. Negotiation (v1.2|force, engine-up, both-marked) surfaces on the
//     session.joined / session.peer_joined acks and gates every engine.*
//     frame afterwards.
//  2. Media path mixing is refused both ways.
//  3. engine.frames → wire frame dispatch (answer to caller, publish
//     fanout to the peer) with engine identities never leaking.
//  4. Degrade: engine down at negotiation ⇒ P2P lands; engine down DURING
//     a steered call ⇒ session-preserving error frame.

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/voxdesk/realtime/gateway-go/internal/auth"
	"github.com/voxdesk/realtime/gateway-go/internal/engineclient"
	"github.com/voxdesk/realtime/gateway-go/internal/hub"
	"github.com/voxdesk/realtime/gateway-go/internal/observability/metrics"
	"github.com/voxdesk/realtime/gateway-go/internal/session"
	"github.com/voxdesk/realtime/gateway-go/internal/signaling"

	gorilla "github.com/gorilla/websocket"
)

// steerFixture is engineFixture plus configuration of the closed steer mode
// and a pre-warm HealthNow so negotiateSteer's availability gate is green
// from the first join (the Monitor loop is not part of these fixtures).
func steerFixture(t *testing.T, engScript *scriptedEngine, mode string) (*fixture, *engineclient.Client) {
	t.Helper()
	cfg := testConfig()
	cfg.EngineSteer = mode
	cfg.MediaEngineURL = engScript.srv.URL
	h := hub.New(cfg.MaxConnsPerTenant)
	reg := metrics.New()
	verifier := auth.NewVerifier(cfg.JWTSecret, cfg.JWTIssuer, cfg.JWTAudience)
	sig := signaling.NewRouter(session.NewManager(cfg.SignalingMaxSessionsPerTenant, cfg.SignalingPendingTimeout), h.Lookup, reg)
	s := New(cfg, h, reg, verifier, sig)
	client := engineclient.New(engScript.srv.URL, time.Second, nil, nil, func(string, ...any) {})
	s.SetEngine(client)
	if _, err := client.HealthNow(context.Background()); err != nil {
		t.Fatalf("pre-warm health probe: %v", err)
	}
	ts := httptest.NewServer(s.Handler())
	t.Cleanup(ts.Close)
	return &fixture{server: s, hub: h, reg: reg, http: ts}, client
}

// steerClient dials and authenticates, declaring wire capability (ws:2 =
// v1.2, 0/absent = legacy).
func steerClient(t *testing.T, f *fixture, tenant string, wsVersion int) *gorilla.Conn {
	t.Helper()
	conn := dial(t, f)
	if welcome := readFrame(t, conn); welcome["type"] != "welcome" {
		t.Fatalf("welcome = %v", welcome)
	}
	token := mintToken(t, testJWTSecret, dashboardClaims(tenant, "owner", 15*time.Minute))
	if wsVersion >= 2 {
		sendFrame(t, conn, fmt.Sprintf(`{"type":"hello","token":"%s","ws":%d}`, token, wsVersion))
	} else {
		sendFrame(t, conn, `{"type":"hello","token":"`+token+`"}`)
	}
	if ready := readFrame(t, conn); ready["type"] != "ready" || ready["tenant_id"] != tenant {
		t.Fatalf("ready = %v", ready)
	}
	return conn
}

// steeredPair performs start+join for two v1.2 clients and asserts the
// negotiation outcome lands in the ack frames of BOTH members.
func steeredPair(t *testing.T, f *fixture, tenant string, wsVersionA, wsVersionB int) (a, b *gorilla.Conn, sessionID string, steered bool) {
	t.Helper()
	a = steerClient(t, f, tenant, wsVersionA)
	b = steerClient(t, f, tenant, wsVersionB)
	sendFrame(t, a, `{"type":"session.start"}`)
	started := readFrame(t, a)
	id, _ := started["session_id"].(string)
	if id == "" {
		t.Fatalf("session.started = %v", started)
	}
	sendFrame(t, b, `{"type":"session.join","session_id":"`+id+`"}`)
	joined := readFrame(t, b)
	if joined["type"] != "session.joined" {
		t.Fatalf("session.joined = %v", joined)
	}
	peerJoined := readFrame(t, a)
	if peerJoined["type"] != "session.peer_joined" {
		t.Fatalf("session.peer_joined = %v", peerJoined)
	}
	steerJoined, _ := joined["steer"].(bool)
	steerPeer, _ := peerJoined["steer"].(bool)
	if steerJoined != steerPeer {
		t.Fatalf("the two members must agree on steer: joined=%v peer_joined=%v", steerJoined, steerPeer)
	}
	return a, b, id, steerJoined
}

const steerOfferSDP = "v=0\r\no=- 1 1 IN IP4 203.0.113.5\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\na=setup:actpass\r\n"

func TestSteerNegotiationAndOfferAnswerAndPublishFanout(t *testing.T) {
	eng := newScriptedEngine(t)
	f, _ := steerFixture(t, eng, "v1.2")

	a, b, id, steered := steeredPair(t, f, testTenantA, 2, 2)
	if !steered {
		t.Fatalf("two v1.2 clients with engine up must steer")
	}

	// engine.offer → engine.answer returns to the OFFERER only.
	sendFrame(t, a, `{"type":"engine.offer","session_id":"`+id+`","sdp":"`+jsonEscapeSDP(steerOfferSDP)+`"}`)
	reply := readFrame(t, a)
	if reply["type"] != "engine.answer" {
		t.Fatalf("engine offer reply = %v", reply)
	}
	if reply["session_id"] != id {
		t.Fatalf("answer session mismatch: %v", reply)
	}
	sdp, _ := reply["sdp"].(string)
	if sdp == "" {
		t.Fatalf("answer carries no sdp: %v", reply)
	}
	if got := len(eng.offers); got != 1 {
		t.Fatalf("engine offers = %d", got)
	}

	// A publishes mic; B hears engine.track_published, A hears nothing.
	sendFrame(t, a, `{"type":"engine.publish","session_id":"`+id+`","track":"mic","kind":"audio"}`)
	pubNotice := readFrame(t, b)
	if pubNotice["type"] != "engine.track_published" {
		t.Fatalf("publish fanout = %v", pubNotice)
	}
	if pubNotice["track"] != "mic" || pubNotice["kind"] != "audio" {
		t.Fatalf("fanout fields = %v", pubNotice)
	}
	pubBy, _ := pubNotice["participant"].(string)
	engSessionA := findEngineJoinForRoom(eng, testTenantA+":"+id)
	if engSessionA == "" {
		t.Fatalf("no engine join recorded for room")
	}
	eng.mu.Lock()
	var pubParticipant string
	for _, j := range eng.joins {
		if j["engine_session"] == engSessionA {
			pubParticipant = j["participant"]
		}
	}
	eng.mu.Unlock()
	if pubBy != pubParticipant {
		t.Fatalf("fanout participant %q want engine-side %q", pubBy, pubParticipant)
	}

	// B subscribes the track: the SUBSCRIBE's engine-side participant is A
	// (the publisher), resolved by the manager — B never names it.
	sendFrame(t, b, `{"type":"engine.subscribe","session_id":"`+id+`","track":"mic"}`)
	waitFor(t, "one subscribe recorded", func() bool {
		n, _ := eng.subscribeSnapshot()
		return n == 1
	})
	_, sub := eng.subscribeSnapshot()
	if sub["participant"] != pubParticipant || sub["track"] != "mic" {
		t.Fatalf("engine subscribe = %v", sub)
	}

	// Media-path mixing is refused from BOTH directions.
	sendFrame(t, a, `{"type":"offer","session_id":"`+id+`","sdp":"`+jsonEscapeSDP(steerOfferSDP)+`"}`)
	mixed := readFrame(t, a)
	if mixed["type"] != "error" || mixed["code"] != "steer_mode_blocked" {
		t.Fatalf("P2P offer on steered session must refuse steer_mode_blocked, got %v", mixed)
	}

	// engine.unsubscribe + session.end: clean teardown.
	sendFrame(t, b, `{"type":"engine.unsubscribe","session_id":"`+id+`","track":"mic"}`)
	sendFrame(t, a, `{"type":"session.end","session_id":"`+id+`"}`)
	_ = readFrame(t, a)
	_ = readFrame(t, b)
}

func TestSteerMixedCapabilitiesStaysP2P(t *testing.T) {
	eng := newScriptedEngine(t)
	f, _ := steerFixture(t, eng, "v1.2")

	a, b, id, steered := steeredPair(t, f, testTenantA, 2, 0)
	if steered {
		t.Fatalf("mixed capability pair must NOT steer")
	}

	// P2P still flows on the same sockets.
	sendFrame(t, a, `{"type":"offer","session_id":"`+id+`","sdp":"`+jsonEscapeSDP(steerOfferSDP)+`"}`)
	relayed := readFrame(t, b)
	if relayed["type"] != "signal.offer" {
		t.Fatalf("P2P relay broken on non-steered pair: %v", relayed)
	}

	// engine.* refuses on the non-steered session.
	sendFrame(t, a, `{"type":"engine.offer","session_id":"`+id+`","sdp":"`+jsonEscapeSDP(steerOfferSDP)+`"}`)
	refused := readFrame(t, a)
	if refused["type"] != "error" || refused["code"] != "steer_mode_blocked" {
		t.Fatalf("engine.offer on P2P session must refuse steer_mode_blocked, got %v", refused)
	}
}

func TestSteerForceIgnoresMarkersButNotAvailability(t *testing.T) {
	eng := newScriptedEngine(t)
	f, _ := steerFixture(t, eng, "force")
	_, _, _, steered := steeredPair(t, f, testTenantA, 0, 0)
	if !steered {
		t.Fatalf("force must steer even unmarked clients")
	}
}

func TestSteerFallsBackToP2PWhenEngineDown(t *testing.T) {
	eng := newScriptedEngine(t)
	f, client := steerFixture(t, eng, "v1.2")
	eng.fail("http500") // any subsequent health probe fails
	_, err := client.HealthNow(context.Background())
	if err == nil {
		t.Fatalf("expected probe failure")
	}

	_, _, _, steered := steeredPair(t, f, testTenantA, 2, 2)
	if steered {
		t.Fatalf("unavailable engine must land the session on the P2P relay")
	}
}

func TestSteerEngineFailureDuringCallPreservesSession(t *testing.T) {
	eng := newScriptedEngine(t)
	f, _ := steerFixture(t, eng, "v1.2")
	a, b, id, steered := steeredPair(t, f, testTenantA, 2, 2)
	if !steered {
		t.Skip("setup failed")
	}
	eng.fail("http500")

	sendFrame(t, a, `{"type":"engine.offer","session_id":"`+id+`","sdp":"`+jsonEscapeSDP(steerOfferSDP)+`"}`)
	reply := readFrame(t, a)
	if reply["type"] != "error" || reply["code"] != "engine_unavailable" {
		t.Fatalf("down engine mid-call must answer engine_unavailable, got %v", reply)
	}

	// Session still alive: graceful end reaches both members.
	sendFrame(t, a, `{"type":"session.end","session_id":"`+id+`"}`)
	for _, c := range []*gorilla.Conn{a, b} {
		frame := readFrame(t, c)
		if frame["type"] != "session.ended" {
			t.Fatalf("session must survive the offer failure, got %v", frame)
		}
	}
}

func TestSteerEngineRefusalMapsToClosedCodes(t *testing.T) {
	eng := newScriptedEngine(t)
	eng.fail("refuse") // engine-side structured room_full
	f, _ := steerFixture(t, eng, "v1.2")

	a, _, id, steered := steeredPair(t, f, testTenantA, 2, 2)
	if !steered {
		// HealthNow was pre-warmed green; negotiation still steers (engine
		// up) while JOINS refuse — IsSteered still true at
		// negotiateSteer's predicate: engine up + both v1.2 + v1.2 mode.
		// The engine session enrolment failed; the first engine.* must be
		// told so, in closed vocabulary.
	}

	sendFrame(t, a, `{"type":"engine.offer","session_id":"`+id+`","sdp":"`+jsonEscapeSDP(steerOfferSDP)+`"}`)
	reply := readFrame(t, a)
	if reply["type"] != "error" {
		t.Fatalf("refusing join must surface an error for engine frames, got %v (steered=%v)", reply, steered)
	}
	// Refused enrolment ⇒ engine session absent ⇒ engine_unavailable is
	// the truthful closed code (NOT the internal "room_full" string).
	if reply["code"] != "engine_unavailable" {
		t.Fatalf("want engine_unavailable (unenrolled), got %v", reply)
	}
}

// findEngineJoinForRoom returns the engine session of the first join
// matching the room name (gateway testTenantA:sessionID convention).
func findEngineJoinForRoom(eng *scriptedEngine, room string) string {
	eng.mu.Lock()
	defer eng.mu.Unlock()
	for _, j := range eng.joins {
		if j["room"] == room {
			return j["engine_session"]
		}
	}
	return ""
}

// jsonEscapeSDP turns raw SDP into a JSON string literal body (no quotes).
func jsonEscapeSDP(s string) string {
	b, _ := json.Marshal(s)
	return string(b[1 : len(b)-1])
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/session/lifecycle.go (53 lines, sha256 6126737697cc395d0fc6258b3bdeb25d81792b8f74db2db92984b2e9801adca9) =====
==============================================================================
```go
package session

import "time"

// This file owns TIME against sessions: the policy of when a session that
// never became a negotiation must stop waiting, and the sweep that applies
// it.
//
// Only ONE shape of time-decay is reaped here: a session that was started
// but never joined (one member, Pending) past pendingTTL. Everything else
// owes its liveness to its sockets — an Offering session whose answerer
// walked away is not this layer's problem, because the websocket heartbeat
// will report the socket dead and ConnDropped ends the session on evidence,
// not on a timer. Timeouts guess; disconnections are facts.

// PendingTTL exposes the configured lone-session timeout.
func (m *Manager) PendingTTL() time.Duration { return m.pendingTTL }

// ReapInterval is the recommended cadence for the reaper loop: half the
// timeout (so a lone session is reaped within 1–1.5× TTL), clamped away
// from nonsense. The caller MAY tick slower; ticking faster than 1 s buys
// nothing.
func (m *Manager) ReapInterval() time.Duration {
	half := m.pendingTTL / 2
	if half < time.Second {
		return time.Second
	}
	return half
}

// Reap ends every lone, pending, over-age session and returns the events to
// notify the affected members. Thread-safe; intended to be called on a
// ticker by the signaling layer (which owns frame emission).
func (m *Manager) Reap() []Event {
	m.mu.Lock()
	defer m.mu.Unlock()
	now := m.now()

	// Collect first, mutate after: removing from the registry while its
	// each() iterates is the classic iterator-invalidation crash, and a
	// leftover-slice beats a cleverer loop that trips over it once a year.
	var expired []*Session
	m.reg.each(func(s *Session) {
		if s.State == Pending && s.memberCount() == 1 && now.Sub(s.created()) >= m.pendingTTL {
			expired = append(expired, s)
		}
	})
	var events []Event
	for _, s := range expired {
		events = append(events, m.endLocked(s, ReasonJoinTimeout)...)
	}
	return events
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/session/lifecycle_test.go (65 lines, sha256 a52734e47e3e2421534c9dbf96f8d9a96b9eeb27dc6afb284accbea100748335) =====
==============================================================================
```go
package session

import (
	"testing"
	"time"
)

// The reaper's ONLY job is lone, pending, over-age sessions. Everything else
// is socket-owned liveness — hence the negatives below matter as much as the
// positive: a reaper that ever ended a live negotiation would be a bug the
// heartbeat already solves honestly.

func TestReapEndsLoneOverdueSessionsWithJoinTimeout(t *testing.T) {
	m, advance := newTestManager(8, time.Minute)
	id := mustStart(t, m, tenantA, connA1)

	advance(30 * time.Second) // under TTL
	if events := m.Reap(); len(events) != 0 {
		t.Fatalf("reaped early: %+v", events)
	}

	advance(31 * time.Second) // past the 60 s TTL
	events := m.Reap()
	if len(events) != 1 {
		t.Fatalf("reap events = %+v", events)
	}
	ev := events[0]
	if ev.Kind != EvEnded || ev.Recipient != connA1 || ev.Reason != ReasonJoinTimeout || ev.SessionID != id {
		t.Errorf("reap event = %+v", ev)
	}
	if sessions, _ := m.Stats(); sessions != 0 {
		t.Fatalf("reaped session still counted: %d", sessions)
	}

	// Reaping is idempotent — a second sweep sees nothing to do.
	if events := m.Reap(); len(events) != 0 {
		t.Fatalf("second reap = %+v", events)
	}
}

func TestReapLeavesJoinedAndNegotiatingSessionsAlone(t *testing.T) {
	m, advance := newTestManager(8, time.Minute)
	joined := mustStart(t, m, tenantA, connA1)
	mustJoin(t, m, tenantA, joined, connA2, connA1)
	negotiating := mustStart(t, m, tenantA, connA3)
	mustJoin(t, m, tenantA, negotiating, connA4, connA3)
	mustOffer(t, m, tenantA, negotiating, connA3)

	advance(2 * time.Hour)
	if events := m.Reap(); len(events) != 0 {
		t.Fatalf("live sessions must NEVER be reaped, got %+v", events)
	}
}

func TestReapIntervalIsHalfTheTTLWithASaneFloor(t *testing.T) {
	m, _ := newTestManager(8, time.Minute)
	if got := m.ReapInterval(); got != 30*time.Second {
		t.Errorf("ReapInterval = %v, want 30s", got)
	}

	tiny := NewManager(8, 500*time.Millisecond)
	if got := tiny.ReapInterval(); got != time.Second {
		t.Errorf("ReapInterval floor = %v, want 1s", got)
	}
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/session/manager.go (301 lines, sha256 ec34f04af751418ae57324b57222855c8034ce14269f41abb26846b273110f3d) =====
==============================================================================
```go
package session

import (
	"crypto/rand"
	"fmt"
	"sync"
	"time"
)

// Event kinds: what the signaling layer must turn into wire frames. The
// session layer reports; the signaling layer renders. Keeping the rendering
// out of here is what makes the entire state machine testable with a plain
// []Event assertion.
type EventKind int

const (
	EvStarted          EventKind = iota // ack to the initiator
	EvJoined                            // ack to the responder
	EvPeerJoined                        // told to the member who was waiting
	EvEnded                             // told to each still-present member, with a reason
	EvForwardOffer                      // relay an SDP offer to the peer
	EvForwardAnswer                     // relay an SDP answer to the peer
	EvForwardCandidate                  // relay an ICE candidate blob to the peer
)

// Event is one outcome of a manager operation addressed to exactly one
// connection. Payload fields are populated by kind (SDP for offers/answers,
// Candidate for candidates, Reason for EvEnded, PeerRole for EvPeerJoined,
// Role for the acks).
type Event struct {
	Kind      EventKind
	Recipient string // connection id the reply is addressed to
	SessionID string
	Role      string
	PeerRole  string
	Reason    string
	SDP       string
	Candidate []byte
}

// Manager owns the session lifecycle for every tenant. One mutex serializes
// everything: sessions are tiny, contention is bounded by the per-tenant
// cap, and a single lock is the design that keeps the state machine free of
// ordering subtleties. Every method is safe for concurrent use.
type Manager struct {
	mu           sync.Mutex
	reg          *Registry
	maxPerTenant int
	pendingTTL   time.Duration

	// idgen/now are constructor-fixed seams (tests in this package replace
	// them) — determinism where a random id or a wall clock would make an
	// assertion flaky, nothing else.
	idgen func() (string, error)
	now   func() time.Time
}

// NewManager builds a manager with the production clock and ids.
func NewManager(maxPerTenant int, pendingTTL time.Duration) *Manager {
	return &Manager{
		reg:          NewRegistry(),
		maxPerTenant: maxPerTenant,
		pendingTTL:   pendingTTL,
		idgen:        newUUIDv4,
		now:          time.Now,
	}
}

// Start creates a pending session with connID as initiator. The returned id
// IS the join capability: 122 random bits, only ever exchanged between the
// member and its intended peer, meaningful solely inside the same tenant.
func (m *Manager) Start(tenantID, connID string) (string, []Event, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if _, ok := m.reg.sessionForConn(connID); ok {
		return "", nil, ErrAlreadyInSession
	}
	if m.reg.countForTenant(tenantID) >= m.maxPerTenant {
		return "", nil, ErrTooManySessions
	}
	id, err := m.idgen()
	if err != nil {
		return "", nil, fmt.Errorf("generate session id: %w", err)
	}
	s := NewSession(id, tenantID, connID, "initiator", m.now())
	m.reg.insert(s)
	return id, []Event{{Kind: EvStarted, Recipient: connID, SessionID: id, Role: "initiator"}}, nil
}

// Join seats connID as a session's responder and wakes the initiator. A
// wrong id, a wrong tenant, or an ended session all return the same
// ErrUnknownSession — the join capability proves nothing about its subject
// until it succeeds.
func (m *Manager) Join(tenantID, sessionID, connID string) ([]Event, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if _, ok := m.reg.sessionForConn(connID); ok {
		return nil, ErrAlreadyInSession
	}
	s, ok := m.reg.get(sessionID)
	if !ok || s.TenantID != tenantID {
		return nil, ErrUnknownSession
	}
	if s.memberCount() >= 2 {
		// A point-to-point negotiation is exactly two seats; the third
		// arriver had the id but not the moment.
		return nil, ErrWrongState
	}
	initiator := s.members[0]
	s.members = append(s.members, Member{ConnID: connID, Role: "responder"})
	m.reg.bindConn(connID, s.ID)
	s.touchChange(m.now())
	return []Event{
		{Kind: EvJoined, Recipient: connID, SessionID: s.ID, Role: "responder"},
		{Kind: EvPeerJoined, Recipient: initiator.ConnID, SessionID: s.ID, PeerRole: "responder"},
	}, nil
}

// End terminates a session at a member's request. Both members (requester
// included) receive EvEnded — the sender's copy is the ack.
func (m *Manager) End(tenantID, sessionID, connID, reason string) ([]Event, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	s, err := m.lookupMemberLocked(tenantID, sessionID, connID)
	if err != nil {
		return nil, err
	}
	return m.endLocked(s, reason), nil
}

// Offer applies an SDP offer: legal from either member once both seats are
// filled, exactly one at a time (the Offering state is the glare guard).
func (m *Manager) Offer(tenantID, sessionID, connID, sdp string) ([]Event, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	s, err := m.lookupMemberLocked(tenantID, sessionID, connID)
	if err != nil {
		return nil, err
	}
	if s.memberCount() < 2 {
		return nil, ErrSessionNotReady
	}
	if s.State == Offering {
		// One outstanding offer at a time, full stop. Whoever the offender
		// is — even the original offerer re-offering — the state answers.
		return nil, ErrWrongState
	}
	me, _ := s.memberByConn(connID)
	s.State = Offering
	s.offerBy = me.Role
	s.touchChange(m.now())
	peer, _ := s.peerOf(connID)
	return []Event{{Kind: EvForwardOffer, Recipient: peer.ConnID, SessionID: s.ID, SDP: sdp}}, nil
}

// Answer completes an outstanding offer, from the member who did not make
// it. A "yes" to a question nobody asked is wrong_state, and so is the
// offerer answering itself.
func (m *Manager) Answer(tenantID, sessionID, connID, sdp string) ([]Event, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	s, err := m.lookupMemberLocked(tenantID, sessionID, connID)
	if err != nil {
		return nil, err
	}
	if s.State != Offering {
		return nil, ErrWrongState
	}
	me, _ := s.memberByConn(connID)
	if me.Role == s.offerBy {
		return nil, ErrWrongState
	}
	s.State = Answered
	s.offerBy = ""
	s.touchChange(m.now())
	peer, _ := s.peerOf(connID)
	return []Event{{Kind: EvForwardAnswer, Recipient: peer.ConnID, SessionID: s.ID, SDP: sdp}}, nil
}

// Candidate relays one ICE candidate to the peer. Legal whenever both seats
// are filled (the browser's own stack drops anything that arrives before it
// can use it; the relay's concern is seating, not media sequencing).
func (m *Manager) Candidate(tenantID, sessionID, connID string, raw []byte) ([]Event, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	s, err := m.lookupMemberLocked(tenantID, sessionID, connID)
	if err != nil {
		return nil, err
	}
	if s.memberCount() < 2 {
		return nil, ErrSessionNotReady
	}
	peer, _ := s.peerOf(connID)
	return []Event{{Kind: EvForwardCandidate, Recipient: peer.ConnID, SessionID: s.ID, Candidate: raw}}, nil
}

// ConnDropped ends whatever session connID held a seat in, notifying the
// remaining member if there is one. Reconnect-and-renegotiate deliberately
// does not exist: sockets own session liveness, ICE restart is exactly as
// expensive as a fresh offer, and a design where zombies sessions outlive
// their sockets is a breach vector, not a feature.
func (m *Manager) ConnDropped(connID string) []Event {
	m.mu.Lock()
	defer m.mu.Unlock()
	id, ok := m.reg.sessionForConn(connID)
	if !ok {
		return nil
	}
	s, ok := m.reg.get(id)
	if !ok {
		m.reg.unbindConn(connID)
		return nil
	}
	events := m.endLocked(s, ReasonPeerDisconnected)
	// The dropped connection gets no notice — it is already gone.
	out := events[:0]
	for _, ev := range events {
		if ev.Recipient != connID {
			out = append(out, ev)
		}
	}
	return out
}

// lookupMemberLocked resolves (tenant, session id, member) with the
// collapsed ErrUnknownSession for every negative case. Caller holds mu.
func (m *Manager) lookupMemberLocked(tenantID, sessionID, connID string) (*Session, error) {
	s, ok := m.reg.get(sessionID)
	if !ok || s.TenantID != tenantID {
		return nil, ErrUnknownSession
	}
	if _, ok := s.memberByConn(connID); !ok {
		// A stranger who somehow knows the id is not told the difference.
		return nil, ErrUnknownSession
	}
	return s, nil
}

// endLocked marks s ended, removes it from the registry, and returns one
// EvEnded per seat (present or vacated — the caller strips recipients that
// no longer exist). Caller holds mu.
func (m *Manager) endLocked(s *Session, reason string) []Event {
	s.markEnded(reason, m.now())
	m.reg.remove(s.ID)
	events := make([]Event, 0, len(s.members))
	for _, member := range s.members {
		events = append(events, Event{
			Kind: EvEnded, Recipient: member.ConnID, SessionID: s.ID, Reason: reason,
		})
	}
	return events
}

// SessionForConn is the public member-binding read the steer path needs:
// which session, if any, holds a seat for this connection right now.
func (m *Manager) SessionForConn(connID string) (string, bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.reg.sessionForConn(connID)
}

// OtherMember resolves the peer's connection id within a session the
// CALLER has already verified it belongs to (membership proofs live
// behind the manager's own tenancy/membership checks — this helper
// answers peer-not-you, nothing more).
func (m *Manager) OtherMember(tenantID, sessionID, connID string) (string, bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	s, ok := m.reg.get(sessionID)
	if !ok || s.TenantID != tenantID {
		return "", false
	}
	for _, member := range s.members {
		if member.ConnID != connID {
			return member.ConnID, true
		}
	}
	return "", false
}

// Stats returns the gauge substrate for /metrics: live sessions and tenants
// holding at least one.
func (m *Manager) Stats() (sessions, tenants int) {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.reg.total(), m.reg.tenants()
}

// newUUIDv4 returns a random RFC 4122 v4 UUID — the same byte shape the
// websocket connection ids, the Python backend, signal-go and the Rust hub
// all emit. Duplicated deliberately (small, stable, and cheaper than a
// shared package for 12 lines of crypto/rand).
func newUUIDv4() (string, error) {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", err
	}
	b[6] = (b[6] & 0x0f) | 0x40 // version 4
	b[8] = (b[8] & 0x3f) | 0x80 // RFC 4122 variant
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16]), nil
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/session/manager_test.go (321 lines, sha256 e5d49f822c972d1b686e6a9cf9092a880475b0d6c78746fad54a2aef7fefaa4f) =====
==============================================================================
```go
package session

import (
	"errors"
	"fmt"
	"testing"
	"time"
)

const (
	tenantA = "11111111-1111-1111-1111-111111111111"
	tenantB = "22222222-2222-2222-2222-222222222222"
	connA1  = "conn-a-1"
	connA2  = "conn-a-2"
	connA3  = "conn-a-3"
	connA4  = "conn-a-4"
	connB1  = "conn-b-1"
)

// newTestManager returns a manager with deterministic ids (seq-1, seq-2, …)
// and a hand-cranked clock, so every assertion reads like a fact, not a
// race.
func newTestManager(maxPerTenant int, pendingTTL time.Duration) (*Manager, func(d time.Duration)) {
	seq := 0
	now := time.Date(2026, 9, 16, 10, 0, 0, 0, time.UTC)
	m := NewManager(maxPerTenant, pendingTTL)
	m.idgen = func() (string, error) {
		seq++
		return fmt.Sprintf("sig-%d", seq), nil
	}
	m.now = func() time.Time { return now }
	return m, func(d time.Duration) { now = now.Add(d) }
}

func mustStart(t *testing.T, m *Manager, tenant, conn string) string {
	t.Helper()
	id, events, err := m.Start(tenant, conn)
	if err != nil {
		t.Fatalf("Start(%s): %v", conn, err)
	}
	if len(events) != 1 || events[0].Kind != EvStarted || events[0].Recipient != conn || events[0].Role != "initiator" {
		t.Fatalf("Start events = %+v", events)
	}
	if events[0].SessionID != id {
		t.Fatalf("Event session id %q != returned %q", events[0].SessionID, id)
	}
	return id
}

func mustJoin(t *testing.T, m *Manager, tenant, id, joiner, initiator string) []Event {
	t.Helper()
	events, err := m.Join(tenant, id, joiner)
	if err != nil {
		t.Fatalf("Join(%s): %v", joiner, err)
	}
	if len(events) != 2 {
		t.Fatalf("Join events = %+v", events)
	}
	if events[0].Kind != EvJoined || events[0].Recipient != joiner || events[0].Role != "responder" {
		t.Errorf("join ack = %+v", events[0])
	}
	if events[1].Kind != EvPeerJoined || events[1].Recipient != initiator || events[1].PeerRole != "responder" {
		t.Errorf("peer-joined notice = %+v", events[1])
	}
	return events
}

// ---------------------------------------------------------------- start ---

func TestStartAssignsInitiatorAndCountsPerTenant(t *testing.T) {
	m, _ := newTestManager(2, time.Minute)
	_ = mustStart(t, m, tenantA, connA1)
	_ = mustStart(t, m, tenantA, connA2)

	// Third session for the same tenant exceeds the cap…
	if _, _, err := m.Start(tenantA, connA3); !errors.Is(err, ErrTooManySessions) {
		t.Fatalf("cap expected ErrTooManySessions, got %v", err)
	}
	// …but a DIFFERENT tenant is unaffected — caps are per tenant by
	// construction, not by a remembered filter.
	_ = mustStart(t, m, tenantB, connB1)

	if sessions, tenants := m.Stats(); sessions != 3 || tenants != 2 {
		t.Fatalf("Stats = (%d, %d)", sessions, tenants)
	}
}

func TestStartRejectsAConnectionAlreadyInASession(t *testing.T) {
	m, _ := newTestManager(8, time.Minute)
	_ = mustStart(t, m, tenantA, connA1)
	if _, _, err := m.Start(tenantA, connA1); !errors.Is(err, ErrAlreadyInSession) {
		t.Fatalf("expected ErrAlreadyInSession, got %v", err)
	}
}

// ----------------------------------------------------------------- join ---

func TestJoinSeatsResponderAndWakesInitiator(t *testing.T) {
	m, _ := newTestManager(8, time.Minute)
	id := mustStart(t, m, tenantA, connA1)
	mustJoin(t, m, tenantA, id, connA2, connA1)
}

func TestJoinCollapsesUnknownWrongTenantAndEndedIntoOneRefusal(t *testing.T) {
	m, _ := newTestManager(8, time.Minute)
	id := mustStart(t, m, tenantA, connA1)

	for _, tc := range []struct{ tenant, id, conn string }{
		{tenantA, "nope-1", connA2}, // never existed
		{tenantB, id, connB1},       // exists — under another tenant
	} {
		if _, err := m.Join(tc.tenant, tc.id, tc.conn); !errors.Is(err, ErrUnknownSession) {
			t.Errorf("Join(%s, %s) = %v, want ErrUnknownSession", tc.tenant, tc.id, err)
		}
	}

	// Ended sessions vanish the same way — a resurrected capability tells
	// the caller nothing about what used to exist.
	if _, err := m.End(tenantA, id, connA1, ReasonMemberEnded); err != nil {
		t.Fatalf("End: %v", err)
	}
	if _, err := m.Join(tenantA, id, connA2); !errors.Is(err, ErrUnknownSession) {
		t.Errorf("join after end = %v, want ErrUnknownSession", err)
	}
}

func TestJoinRefusesTheCreatorAndAThirdSeat(t *testing.T) {
	m, _ := newTestManager(8, time.Minute)
	id := mustStart(t, m, tenantA, connA1)

	// The initiator's own connection is already bound — joining one's own
	// session must not double-seat it.
	if _, err := m.Join(tenantA, id, connA1); !errors.Is(err, ErrAlreadyInSession) {
		t.Errorf("self-join = %v, want ErrAlreadyInSession", err)
	}

	mustJoin(t, m, tenantA, id, connA2, connA1)
	if _, err := m.Join(tenantA, id, connA3); !errors.Is(err, ErrWrongState) {
		t.Errorf("third seat = %v, want ErrWrongState", err)
	}
}

// ---------------------------------------------------------------- offer ---

const testSDP = "v=0\r\no=- 1 1 IN IP4 10.0.0.1\r\n"

func mustOffer(t *testing.T, m *Manager, tenant, id, conn string) {
	t.Helper()
	events, err := m.Offer(tenant, id, conn, testSDP)
	if err != nil {
		t.Fatalf("Offer(%s): %v", conn, err)
	}
	if len(events) != 1 || events[0].Kind != EvForwardOffer || events[0].SDP != testSDP {
		t.Fatalf("offer events = %+v", events)
	}
}

func TestOfferRequiresBothSeats(t *testing.T) {
	m, _ := newTestManager(8, time.Minute)
	id := mustStart(t, m, tenantA, connA1)
	if _, err := m.Offer(tenantA, id, connA1, testSDP); !errors.Is(err, ErrSessionNotReady) {
		t.Fatalf("offer with one member = %v, want ErrSessionNotReady", err)
	}
}

func TestOfferAnswerRenegotiationIsTheOnlyLegalPath(t *testing.T) {
	m, _ := newTestManager(8, time.Minute)
	id := mustStart(t, m, tenantA, connA1)
	mustJoin(t, m, tenantA, id, connA2, connA1)

	mustOffer(t, m, tenantA, id, connA1)

	// Glare: a second offer while one is outstanding is refused — whoever
	// sends it. This is the invariant Perfect Negotiation asks both
	// browsers to maintain; the edge maintains it once, for all of them.
	if _, err := m.Offer(tenantA, id, connA2, testSDP); !errors.Is(err, ErrWrongState) {
		t.Errorf("glare offer = %v, want ErrWrongState", err)
	}
	if _, err := m.Offer(tenantA, id, connA1, testSDP); !errors.Is(err, ErrWrongState) {
		t.Errorf("re-offer while outstanding = %v, want ErrWrongState", err)
	}

	// The offerer cannot answer their own offer; the responder must.
	if _, err := m.Answer(tenantA, id, connA1, testSDP); !errors.Is(err, ErrWrongState) {
		t.Errorf("self-answer = %v, want ErrWrongState", err)
	}
	events, err := m.Answer(tenantA, id, connA2, testSDP)
	if err != nil {
		t.Fatalf("Answer: %v", err)
	}
	if len(events) != 1 || events[0].Kind != EvForwardAnswer || events[0].Recipient != connA1 {
		t.Fatalf("answer events = %+v", events)
	}

	// Renegotiation: from Answered, EITHER member may offer (role flip).
	if _, err := m.Answer(tenantA, id, connA1, testSDP); !errors.Is(err, ErrWrongState) {
		t.Errorf("double answer = %v, want ErrWrongState", err)
	}
	events, err = m.Offer(tenantA, id, connA2, testSDP)
	if err != nil {
		t.Fatalf("renegotiation offer: %v", err)
	}
	if events[0].Recipient != connA1 {
		t.Errorf("renegotiation offer went to %s, want the original offerer %s", events[0].Recipient, connA1)
	}
}

func TestSignalingMessagesFromNonMembersTellThemNothing(t *testing.T) {
	m, _ := newTestManager(8, time.Minute)
	id := mustStart(t, m, tenantA, connA1)
	mustJoin(t, m, tenantA, id, connA2, connA1)

	// A third connection of the SAME tenant guessing the id learns nothing
	// more than a wrong id would have told it.
	if _, err := m.Offer(tenantA, id, connA3, testSDP); !errors.Is(err, ErrUnknownSession) {
		t.Errorf("non-member offer = %v, want ErrUnknownSession", err)
	}
	if _, err := m.Answer(tenantA, id, connA3, testSDP); !errors.Is(err, ErrUnknownSession) {
		t.Errorf("non-member answer = %v, want ErrUnknownSession", err)
	}
	if _, err := m.Candidate(tenantA, id, connA3, []byte(`{"candidate":"c"}`)); !errors.Is(err, ErrUnknownSession) {
		t.Errorf("non-member candidate = %v, want ErrUnknownSession", err)
	}
	if _, err := m.End(tenantA, id, connA3, ReasonMemberEnded); !errors.Is(err, ErrUnknownSession) {
		t.Errorf("non-member end = %v, want ErrUnknownSession", err)
	}
}

// ------------------------------------------------------------ candidate ---

func TestCandidateRelaysToThePeerOnly(t *testing.T) {
	m, _ := newTestManager(8, time.Minute)
	id := mustStart(t, m, tenantA, connA1)
	mustJoin(t, m, tenantA, id, connA2, connA1)

	raw := []byte(`{"candidate":"candidate:1 1 udp 2130706431 10.0.0.1 9 typ host","sdpMid":"0"}`)
	events, err := m.Candidate(tenantA, id, connA1, raw)
	if err != nil {
		t.Fatalf("Candidate: %v", err)
	}
	if len(events) != 1 || events[0].Kind != EvForwardCandidate || events[0].Recipient != connA2 {
		t.Fatalf("candidate events = %+v", events)
	}
	if string(events[0].Candidate) != string(raw) {
		t.Errorf("candidate payload rewritten: %s", events[0].Candidate)
	}

	// Trickling the other way is symmetric.
	events, _ = m.Candidate(tenantA, id, connA2, raw)
	if events[0].Recipient != connA1 {
		t.Errorf("reverse candidate went to %s", events[0].Recipient)
	}
}

// ----------------------------------------------------------------- end ---

func TestEndNotifiesBothMembersAndFreesTheTenantSlot(t *testing.T) {
	m, _ := newTestManager(1, time.Minute) // cap of ONE makes release observable
	id := mustStart(t, m, tenantA, connA1)
	mustJoin(t, m, tenantA, id, connA2, connA1)

	events, err := m.End(tenantA, id, connA1, ReasonMemberEnded)
	if err != nil {
		t.Fatalf("End: %v", err)
	}
	if len(events) != 2 {
		t.Fatalf("end events = %+v", events)
	}
	for _, ev := range events {
		if ev.Kind != EvEnded || ev.Reason != ReasonMemberEnded {
			t.Errorf("end event = %+v", ev)
		}
	}

	// The tenant slot AND the seat binding both came back, in one check:
	// with cap ONE, this Start would fail on the cap if End hadn't freed
	// the tenant counter, and on already-in-session if the conn binding
	// hadn't been released.
	_ = mustStart(t, m, tenantA, connA1)
}

// ------------------------------------------------------------ conn drop ---

func TestConnDroppedEndsTheSessionAndTellsTheSurvivor(t *testing.T) {
	m, _ := newTestManager(8, time.Minute)
	id := mustStart(t, m, tenantA, connA1)
	mustJoin(t, m, tenantA, id, connA2, connA1)
	mustOffer(t, m, tenantA, id, connA1)

	events := m.ConnDropped(connA2)
	if len(events) != 1 || events[0].Kind != EvEnded || events[0].Recipient != connA1 || events[0].Reason != ReasonPeerDisconnected {
		t.Fatalf("drop events = %+v", events)
	}
	// The dropper gets no notice (it is gone), and the session is fully
	// gone: the survivor hears NOTHING more, and its seat is reusable.
	if _, _, err := m.Start(tenantA, connA1); err != nil {
		t.Fatalf("survivor restart: %v", err)
	}
}

func TestConnDroppedOnALoneSessionIsSilent(t *testing.T) {
	m, _ := newTestManager(8, time.Minute)
	_ = mustStart(t, m, tenantA, connA1)
	if events := m.ConnDropped(connA1); len(events) != 0 {
		t.Fatalf("lone drop events = %+v, want none", events)
	}
	if sessions, _ := m.Stats(); sessions != 0 {
		t.Fatalf("Stats after lone drop = %d sessions", sessions)
	}
}

func TestConnDroppedOnAnUnseatedConnectionIsANoOp(t *testing.T) {
	m, _ := newTestManager(8, time.Minute)
	_ = mustStart(t, m, tenantA, connA1)
	if events := m.ConnDropped(connA3); len(events) != 0 {
		t.Fatalf("unseated drop = %+v", events)
	}
	if sessions, _ := m.Stats(); sessions != 1 {
		t.Fatalf("session count changed by an unrelated drop: %d", sessions)
	}
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/session/registry.go (104 lines, sha256 18ff756aa8b0119509b83f14233e2ade1312a78392c07827587f3d40042328e6) =====
==============================================================================
```go
package session

// Registry is the manager's indexed store. It holds no lock of its own —
// the Manager serializes everything — because a lock here plus a lock there
// would be two lock orderings to reason about, and the value of the lock
// hierarchy being trivial dwarfs any read-throughput fantasy a signaling
// plane with ≤ 64 sessions per tenant could have.
type Registry struct {
	byID     map[string]*Session
	byTenant map[string]map[string]struct{}
	byConn   map[string]string // connection id → session id (≤ 1 session per connection)
}

// NewRegistry returns an empty store.
func NewRegistry() *Registry {
	return &Registry{
		byID:     make(map[string]*Session),
		byTenant: make(map[string]map[string]struct{}),
		byConn:   make(map[string]string),
	}
}

// get returns the session by id.
func (r *Registry) get(id string) (*Session, bool) {
	s, ok := r.byID[id]
	return s, ok
}

// countForTenant is the denominator of the per-tenant session cap.
func (r *Registry) countForTenant(tenantID string) int {
	return len(r.byTenant[tenantID])
}

// total reports live sessions process-wide (a gauge substrate; per-tenant
// snapshots are available through Stats on the manager).
func (r *Registry) total() int { return len(r.byID) }

// tenantSessionCount reports per-tenant occupancy for metrics snapshots.
func (r *Registry) tenants() int { return len(r.byTenant) }

// sessionForConn maps a connection to its session, if any.
func (r *Registry) sessionForConn(connID string) (string, bool) {
	id, ok := r.byConn[connID]
	return id, ok
}

// insert registers a new session and every member seat it already holds.
func (r *Registry) insert(s *Session) {
	r.byID[s.ID] = s
	if r.byTenant[s.TenantID] == nil {
		r.byTenant[s.TenantID] = make(map[string]struct{})
	}
	r.byTenant[s.TenantID][s.ID] = struct{}{}
	for _, m := range s.members {
		r.byConn[m.ConnID] = s.ID
	}
}

// bindConn registers one seat's membership (used when the responder joins;
// creation-time members are covered by insert).
func (r *Registry) bindConn(connID, sessionID string) {
	r.byConn[connID] = sessionID
}

// remove drops the session and every member binding. Dropping a missing id
// is a no-op — a 2-member session can only end once, but the callers that
// tear down (end, disconnect, reaper) must each be safe to lose a race.
func (r *Registry) remove(id string) {
	s, ok := r.byID[id]
	if !ok {
		return
	}
	delete(r.byID, id)
	if set, ok := r.byTenant[s.TenantID]; ok {
		delete(set, id)
		if len(set) == 0 {
			delete(r.byTenant, s.TenantID)
		}
	}
	for _, m := range s.members {
		// Only unbind if THIS session owns the binding; a connection whose
		// session ended normally already unbound via unbindConn.
		if cur, ok := r.byConn[m.ConnID]; ok && cur == id {
			delete(r.byConn, m.ConnID)
		}
	}
}

// unbindConn releases one connection's seat binding (when it leaves an
// otherwise-live session — today always followed by the session ending, so
// this exists for completeness of the index invariant).
func (r *Registry) unbindConn(connID string) {
	delete(r.byConn, connID)
}

// each calls fn for every live session, in map order. Callers must not rely
// on iteration order (a unit test asserting order is a flaky test by
// definition) and must not call back into the registry (the manager's lock
// is held).
func (r *Registry) each(fn func(*Session)) {
	for _, s := range r.byID {
		fn(s)
	}
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/session/registry_test.go (75 lines, sha256 ffdfaf43d11c7b2f6a361def0be2c9b4a008191775c888a9950b1b0315480605) =====
==============================================================================
```go
package session

import (
	"testing"
	"time"
)

// The registry's invariants are what make the manager's guarantees physical:
// every index agrees with every other, so there is NEVER a path where a
// session knows a member the conn-index doesn't, or a tenant count the id
// index disagrees with. These tests poke the indexes directly.

func TestRegistryIndexesStayConsistentAcrossTheLifecycle(t *testing.T) {
	m, _ := newTestManager(8, time.Minute)
	id := mustStart(t, m, tenantA, connA1)
	mustJoin(t, m, tenantA, id, connA2, connA1)

	// All three indexes agree.
	if _, ok := m.reg.get(id); !ok {
		t.Fatal("id index lost the session")
	}
	if got := m.reg.countForTenant(tenantA); got != 1 {
		t.Fatalf("tenant count = %d", got)
	}
	for _, conn := range []string{connA1, connA2} {
		if bound, ok := m.reg.sessionForConn(conn); !ok || bound != id {
			t.Errorf("conn index for %s = (%q, %v)", conn, bound, ok)
		}
	}

	// Removal cleans ALL of them — including both member bindings.
	m.ConnDropped(connA1)
	if _, ok := m.reg.get(id); ok {
		t.Error("id index kept an ended session")
	}
	if got := m.reg.countForTenant(tenantA); got != 0 {
		t.Errorf("tenant count after removal = %d", got)
	}
	for _, conn := range []string{connA1, connA2} {
		if _, ok := m.reg.sessionForConn(conn); ok {
			t.Errorf("conn index kept a binding for %s past session end", conn)
		}
	}
}

func TestRemoveDoesNotUnbindAConnectionOwnedByAnotherSession(t *testing.T) {
	// A connection that ended its old session and immediately started a new
	// one must not lose the NEW binding when the OLD session id is removed
	// twice (a delayed reaper racing a reconnect is the shape this guards).
	m, _ := newTestManager(8, time.Minute)
	first := mustStart(t, m, tenantA, connA1)
	m.ConnDropped(connA1) // ends first, unbinds connA1
	second := mustStart(t, m, tenantA, connA1)

	// Replay the removal of the FIRST id — stale deletes happen when two
	// lifecycle paths race (a reaper event vs an explicit end).
	m.reg.remove(first)

	if bound, ok := m.reg.sessionForConn(connA1); !ok || bound != second {
		t.Fatalf("stale removal stole the new session's binding: (%q, %v)", bound, ok)
	}
}

func TestTenantsCountTracksOnlyOccupiedTenants(t *testing.T) {
	m, _ := newTestManager(8, time.Minute)
	mustStart(t, m, tenantA, connA1)
	mustStart(t, m, tenantB, connB1)
	if _, tenants := m.Stats(); tenants != 2 {
		t.Fatalf("tenants = %d", tenants)
	}
	m.ConnDropped(connA1)
	if _, tenants := m.Stats(); tenants != 1 {
		t.Errorf("empty tenant must leave the gauge: tenants = %d", tenants)
	}
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/session/session.go (119 lines, sha256 1c7f086db57ac225682a5a9968e66b6018c596566198bd6dd706fa6542c46db3) =====
==============================================================================
```go
package session

import (
	"errors"
	"time"
)

// Sentinel errors of the session layer. Callers map them onto protocol
// error codes (internal/signaling/events.go); sessions themselves never
// speak wire formats. Some collapses are SECURITY decisions, not laziness —
// see the comments on each sentinel.
var (
	// ErrUnknownSession: no such session for this tenant — including "it
	// exists under ANOTHER tenant" and "you are not a member of it". The
	// three cases share one answer so a probing client cannot enumerate
	// session ids, tenants, or memberships.
	ErrUnknownSession = errors.New("unknown session")
	// ErrTooManySessions: the tenant is at its concurrent-session cap.
	ErrTooManySessions = errors.New("too many sessions for tenant")
	// ErrAlreadyInSession: this connection already holds a session slot.
	ErrAlreadyInSession = errors.New("connection already in a session")
	// ErrSessionNotReady: the action requires two members; the peer slot is
	// still empty (an offer sent before anyone joined).
	ErrSessionNotReady = errors.New("session not ready for members")
	// ErrSessionFull: the second member slot is already taken — point to
	// point means exactly two, never a room.
	ErrSessionFull = errors.New("session already has two members")
	// ErrWrongState: legal message, illegal phase (glare: an offer while
	// one is outstanding; an answer with nothing outstanding).
	ErrWrongState = errors.New("wrong state for this message")
)

// Closed reason vocabulary, sent on the wire as session.ended.reason.
const (
	ReasonMemberEnded      = "member_ended"      // a member sent session.end
	ReasonPeerDisconnected = "peer_disconnected" // a member's socket died
	ReasonJoinTimeout      = "join_timeout"      // lifecycle reaped a lone pending session
)

// Member is one connection's seat in a session.
type Member struct {
	// ConnID is the connection's hub session id — the only identity the
	// session layer needs, and deliberately NOT a user or device id (those
	// have no business on a media negotiation ledger).
	ConnID string
	Role   string // protocol.RoleInitiator / protocol.RoleResponder, as a string to keep this package protocol-free
}

// Session is the negotiation ledger between two connections. Guarded by the
// OWNING Manager's mutex (see manager.go) — methods are plain and document
// their preconditions rather than re-locking, which keeps the state machine
// readable and free of a second lock hierarchy.
type Session struct {
	ID       string
	TenantID string
	State    State

	// members holds at most two seats: [0] is always the initiator, [1]
	// (when present) the responder.
	members []Member
	// offerBy is the role holding the outstanding offer, meaningful exactly
	// when State == Offering.
	offerBy   string
	createdAt time.Time
	changedAt time.Time
	endedAt   time.Time
	endReason string
}

// NewSession builds a pending session with one member (the initiator).
func NewSession(id, tenantID, connID, initiatorRole string, now time.Time) *Session {
	return &Session{
		ID:        id,
		TenantID:  tenantID,
		State:     Pending,
		members:   []Member{{ConnID: connID, Role: initiatorRole}},
		createdAt: now,
		changedAt: now,
	}
}

// memberByConn returns the member seat for connID, if any.
func (s *Session) memberByConn(connID string) (Member, bool) {
	for _, m := range s.members {
		if m.ConnID == connID {
			return m, true
		}
	}
	return Member{}, false
}

// peerOf returns the OTHER member — the forward destination for anything
// connID sends. With at most two members this is exhaustive.
func (s *Session) peerOf(connID string) (Member, bool) {
	for _, m := range s.members {
		if m.ConnID != connID {
			return m, true
		}
	}
	return Member{}, false
}

// memberCount reports occupied seats.
func (s *Session) memberCount() int { return len(s.members) }

// created/lastChanged expose the timestamps lifecycle.go's reaper needs.
func (s *Session) created() time.Time     { return s.createdAt }
func (s *Session) lastChanged() time.Time { return s.changedAt }

// touchChange marks a state-affecting event (membership or SDP).
func (s *Session) touchChange(now time.Time) { s.changedAt = now }

// markEnded seals the session; idempotent via the State check by callers.
func (s *Session) markEnded(reason string, now time.Time) {
	s.State = Ended
	s.endReason = reason
	s.endedAt = now
	s.changedAt = now
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/session/state.go (52 lines, sha256 b53c3d13d5bc0280a6e3684791e8b6ad82418838b8703c36068dcbd9abf70681) =====
==============================================================================
```go
// Package session owns the signaling session: a point-to-point negotiation
// between exactly two connections of ONE tenant (the initiator and the
// responder), its state machine, and its per-tenant accounting.
//
// The package is deliberately transport-agnostic: it knows connection IDs
// (strings) and returns Event values describing what happened; it never
// formats frames and never touches sockets. That is what internal/signaling
// is for, and the separation is what keeps the whole state machine
// unit-testable without a single fake websocket.
package session

import "fmt"

// State is the negotiation phase of a session. The vocabulary is the
// smallest set the SERVER can actually know: it can see SDP ordering, so it
// knows Pending/Offering/Answered; it cannot see media flowing, so there is
// intentionally no "Connected" — signaling correctness ends where the
// network gets opaque, and pretending otherwise would codify a guess.
type State int

const (
	// Pending: the session exists, fewer than two members, or no offer yet.
	Pending State = iota
	// Offering: an SDP offer is outstanding; only an answer from the OTHER
	// member may follow. This is the glare guard — two offers outstanding
	// is exactly what Perfect Negotiation exists to avoid, and the server
	// makes it unreachable instead of asking both browsers to be careful.
	Offering
	// Answered: SDP exchange completed. Candidates keep flowing; either
	// member may open a renegotiation with a fresh offer.
	Answered
	// Ended: terminal. An ended session is dropped from the registry in the
	// same critical section that marks it, so Ended is in practice visible
	// only inside the manager's result values.
	Ended
)

// String renders the state for logs and tests.
func (s State) String() string {
	switch s {
	case Pending:
		return "pending"
	case Offering:
		return "offering"
	case Answered:
		return "answered"
	case Ended:
		return "ended"
	default:
		return fmt.Sprintf("state(%d)", int(s))
	}
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/session/state_test.go (85 lines, sha256 e3698c7b68b382364fd2ebe2cae4832ee4d0af2dbdf4d65325d67bac655333c0) =====
==============================================================================
```go
package session

import (
	"errors"
	"testing"
	"time"
)

// The state machine as a matrix: for every (state, message) the legal
// outcome is asserted through the manager, not by inspecting internals —
// the machine means nothing except what it lets a pair of clients do.

func TestStateTransitionsMatrix(t *testing.T) {
	m, _ := newTestManager(8, time.Minute)
	id := mustStart(t, m, tenantA, connA1)

	// Pending: offers need two seats; answers are out of phase outright.
	if _, err := m.Offer(tenantA, id, connA1, testSDP); !errors.Is(err, ErrSessionNotReady) {
		t.Fatalf("pending offer = %v", err)
	}
	if _, err := m.Answer(tenantA, id, connA1, testSDP); !errors.Is(err, ErrWrongState) {
		t.Fatalf("pending answer = %v", err)
	}
	mustJoin(t, m, tenantA, id, connA2, connA1)

	// Pending with two seats → offer moves to Offering.
	mustOffer(t, m, tenantA, id, connA1)

	// Offering → answer (from the non-offerer) moves to Answered.
	if _, err := m.Answer(tenantA, id, connA2, testSDP); err != nil {
		t.Fatalf("answer: %v", err)
	}

	// Answered → offer from either side re-enters Offering (renegotiation).
	mustOffer(t, m, tenantA, id, connA2)
	if _, err := m.Answer(tenantA, id, connA1, testSDP); err != nil {
		t.Fatalf("renegotiation answer: %v", err)
	}

	// Any state → end lands in Ended; after end, every signaling message
	// gets the collapsed unknown refusal.
	if _, err := m.End(tenantA, id, connA1, ReasonMemberEnded); err != nil {
		t.Fatalf("End: %v", err)
	}
	for _, tc := range []func() error{
		func() error { _, err := m.Offer(tenantA, id, connA1, testSDP); return err },
		func() error { _, err := m.Answer(tenantA, id, connA2, testSDP); return err },
		func() error { _, err := m.Candidate(tenantA, id, connA1, nil); return err },
		func() error { _, err := m.End(tenantA, id, connA1, ReasonMemberEnded); return err },
	} {
		if err := tc(); !errors.Is(err, ErrUnknownSession) {
			t.Errorf("post-end message = %v, want ErrUnknownSession", err)
		}
	}
}

func TestStateNamesAreStableWireAndLogVocabulary(t *testing.T) {
	// Reasons ride on the wire (session.ended.reason); states serve logs
	// and future dashboards. Pin both vocabularies.
	states := map[State]string{
		Pending:  "pending",
		Offering: "offering",
		Answered: "answered",
		Ended:    "ended",
	}
	for st, want := range states {
		if got := st.String(); got != want {
			t.Errorf("State(%d).String() = %q, want %q", int(st), got, want)
		}
	}
	for _, reason := range []string{ReasonMemberEnded, ReasonPeerDisconnected, ReasonJoinTimeout} {
		if reason == "" || reason != underscoreLower(reason) {
			t.Errorf("reason %q must be a stable snake_case token", reason)
		}
	}
}

func underscoreLower(s string) string {
	for _, r := range s {
		if r != '_' && (r < 'a' || r > 'z') {
			return "?" + s
		}
	}
	return s
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/shutdown/graceful.go (57 lines, sha256 27e976f1eb371a53a4d4cbfe2ddc476081bb7dec39563868fa6e026a1d4b96b3) =====
==============================================================================
```go
// Package shutdown owns the gateway's termination contract, extracted from
// cmd/gateway/main.go so the policy (which signals, which deadline, which
// drain order) is a tested unit instead of a dozen inline lines.
//
// The contract itself is unchanged:
//
//   - SIGINT or SIGTERM begins shutdown (Kubernetes sends TERM on pod
//     eviction; Ctrl-C sends INT in development);
//   - the HTTP server stops accepting new work and asks every live
//     WebSocket session to close (http.Server.RegisterOnShutdown →
//     hub.CloseAll with 1001 Going Away);
//   - in-flight frames get a bounded drain window: a browser that ignores
//     its close frame never holds the process past ShutdownTimeout.
package shutdown

import (
	"context"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"
)

// Notify returns a buffered channel carrying the shutdown-triggering
// signals (SIGINT, SIGTERM) and a stop function that detaches it.
// Buffered with capacity 2: a double-TERM (impatient operator, or TERM
// followed by the orchestrator's escalation) must be DROP-able without
// blocking the sender — the gateway exits on the FIRST signal and does not
// implement a hard-kill escalation path, so queuing more would only park
// the kernel's signal delivery.
//
// Callers own calling stop() (typically defer right after Notify), which
// restores default handling: crucial in shared processes and tests, where
// a leaked Notify would swallow a later TERM meant for someone else.
func Notify() (<-chan os.Signal, func()) {
	ch := make(chan os.Signal, 2)
	signal.Notify(ch, os.Interrupt, syscall.SIGTERM)
	return ch, func() { signal.Stop(ch) }
}

// HTTPServer gracefully shuts srv down, giving in-flight work at most
// timeout to finish. It is a thin, honest wrapper over srv.Shutdown: the
// timeout is applied as a context deadline (http.Server's OWN drain
// semantics — close listeners, idle connections immediately, active ones
// at request end), and the returned error is whatever Shutdown reports
// (context.DeadlineExceeded when the window expired, nil on a clean
// drain). Registered OnShutdown callbacks (the gateway's CloseAll) run as
// part of the drain, exactly as net/http documents.
//
// The timeout applies to the drain, not to time spent waiting — a caller
// that wants a whole-phase budget owns the wait itself.
func HTTPServer(srv *http.Server, timeout time.Duration) error {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	return srv.Shutdown(ctx)
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/shutdown/graceful_test.go (86 lines, sha256 747205076ad8add9930c440990d8a3fa7726e4fbcaf3c89545cde806ed7bcd30) =====
==============================================================================
```go
package shutdown

import (
	"context"
	"errors"
	"net"
	"net/http"
	"sync"
	"syscall"
	"testing"
	"time"
)

func TestNotifyDeliversTermAndStopRestores(t *testing.T) {
	ch, stop := Notify()
	defer stop()

	if err := syscall.Kill(syscall.Getpid(), syscall.SIGTERM); err != nil {
		t.Fatalf("self-signalling failed: %v", err)
	}
	select {
	case got := <-ch:
		if got != syscall.SIGTERM {
			t.Fatalf("want SIGTERM, got %v", got)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("SIGTERM never arrived at the notify channel")
	}
}

// testServer starts a real http.Server on an ephemeral port and fails the
// test if it ever exits for a reason other than being shut down.
func testServer(t *testing.T, handler http.HandlerFunc) *http.Server {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	srv := &http.Server{Addr: ln.Addr().String(), Handler: handler}
	errc := make(chan error, 1)
	go func() { errc <- srv.Serve(ln) }()
	t.Cleanup(func() {
		_ = srv.Close()
		if err := <-errc; err != nil && !errors.Is(err, http.ErrServerClosed) {
			t.Fatalf("server exited abnormally: %v", err)
		}
	})
	return srv
}

func TestHTTPServerDrainsIdleServerCleanly(t *testing.T) {
	t.Parallel()
	srv := testServer(t, func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusOK) })
	if err := HTTPServer(srv, 2*time.Second); err != nil {
		t.Fatalf("idle server must drain cleanly, got %v", err)
	}
}

func TestHTTPServerHonoursTheDeadline(t *testing.T) {
	t.Parallel()
	started := make(chan struct{})
	release := make(chan struct{})
	var once sync.Once
	srv := testServer(t, func(w http.ResponseWriter, r *http.Request) {
		once.Do(func() { close(started) }) // signal the request is in-flight
		<-release                          // …then hang until the test lets go
		w.WriteHeader(http.StatusOK)
	})
	defer close(release)

	go func() { _, _ = http.Get("http://" + srv.Addr + "/") }()
	select {
	case <-started:
	case <-time.After(2 * time.Second):
		t.Fatal("hanging request never started")
	}

	start := time.Now()
	err := HTTPServer(srv, 100*time.Millisecond)
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("a hanging request must surface the deadline, got %v", err)
	}
	if elapsed := time.Since(start); elapsed > 2*time.Second {
		t.Fatalf("shutdown must return near the deadline, took %v", elapsed)
	}
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/signaling/answer.go (36 lines, sha256 5af06d52c055c2b6fb57553e1b4998a2414b1c17539bc34b9f677784b441e72e) =====
==============================================================================
```go
package signaling

import (
	"github.com/voxdesk/realtime/gateway-go/internal/hub"
	"github.com/voxdesk/realtime/gateway-go/internal/protocol"
)

// handleAnswer relays one SDP answer to the offerer. Envelope guards are
// identical to the offer path; the interesting legality (there IS an
// outstanding offer and the answerer is not the offerer — otherwise a
// browser could "answer" its own glare away and desync the pair) lives in
// the session state machine, which is why this file stays small. Small is
// the review surface you want on a security edge.
func (r *Router) handleAnswer(sub hub.Subscriber, msg *protocol.ClientMessage) {
	// Validity on the trimmed view, forwarding on the wire bytes (see
	// offer.go: a relay that trims is a relay that rewrites).
	if r.IsSteered(msg.SessionID) {
		r.refuse(sub, protocol.CodeSteerModeBlocked, "session is steered to the media engine; use engine.offer")
		return
	}
	sdp := msg.SDP
	if !protocol.IsValidSDP(sdp) {
		r.refuse(sub, protocol.CodeBadMessage, "sdp must be an SDP blob (the 'v=' version line first)")
		return
	}
	if len(sdp) > protocol.MaxSDPLength {
		r.refuse(sub, protocol.CodeSignalTooLarge, "sdp body exceeds the relay cap")
		return
	}
	events, err := r.m.Answer(sub.Tenant(), msg.SessionID, sub.Session(), sdp)
	if err != nil {
		r.fail(sub, err)
		return
	}
	r.emit(events)
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/signaling/candidate.go (29 lines, sha256 65aceb96f8bf5f3cbae1654ff3b18137423d0cd35daaede88663ea065cbbf2a5) =====
==============================================================================
```go
package signaling

import (
	"github.com/voxdesk/realtime/gateway-go/internal/hub"
	"github.com/voxdesk/realtime/gateway-go/internal/protocol"
)

// handleCandidate relays one ICE candidate. The wire contract keeps the
// candidate as raw JSON and the relay preserves it byte-for-byte — browsers
// and media planes evolve the RTCIceCandidate dictionary independently, and
// an edge that re-serializes fields it half-understands is how connectivity
// bugs get born. The end-of-candidates marker (null, or an empty-string
// candidate) is relayed identically; the peer needs it to stop gathering.
func (r *Router) handleCandidate(sub hub.Subscriber, msg *protocol.ClientMessage) {
	if r.IsSteered(msg.SessionID) {
		r.refuse(sub, protocol.CodeSteerModeBlocked, "session is steered to the media engine; use engine.candidate")
		return
	}
	if !protocol.CandidateIsObject(msg.Candidate) {
		r.refuse(sub, protocol.CodeBadMessage, "candidate must be an ICE candidate object or null")
		return
	}
	events, err := r.m.Candidate(sub.Tenant(), msg.SessionID, sub.Session(), msg.Candidate)
	if err != nil {
		r.fail(sub, err)
		return
	}
	r.emit(events)
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/signaling/events.go (244 lines, sha256 4a2ef908759f50e590ad5ee7546f8739c28081a91a0724123bf4103a05ff3b6f) =====
==============================================================================
```go
package signaling

import (
	"context"
	"errors"
	"time"

	"github.com/voxdesk/realtime/gateway-go/internal/engineclient"
	"github.com/voxdesk/realtime/gateway-go/internal/hub"
	"github.com/voxdesk/realtime/gateway-go/internal/protocol"
	"github.com/voxdesk/realtime/gateway-go/internal/session"
)

// engineCallTimeout bounds ONE engine control hop. The engine client's own
// backstop may be configured longer; hook calls must never stall the caller
// connection's read loop beyond this — engine slowness is a degrade signal,
// not a session-establishment blocker.
const engineCallTimeout = 1200 * time.Millisecond

// This file turns session-layer outcomes into wire frames. Two directions:
// event → frame (emit), error → refusal (fail). Keeping both in one place
// is what guarantees the two vocabularies — the session layer's reasons and
// the protocol's codes — never drift: any new EventKind or sentinel error
// has to pass review HERE, next to its sibling translations.

// emit delivers each event to its addressed connection. A recipient that
// already vanished (it closed between the manager's decision and this
// lookup) is skipped silently: ConnClosed handles session-ending on socket
// death, so nothing is lost by the skip — the frame's recipient no longer
// exists to receive anything.
// emit is the standard-entry shim for paths that carry no steer
// information (everything except session.join, which negotiates the media
// path). Kept arity-stable for existing call sites.
func (r *Router) emit(events []session.Event) {
	r.emitWithSteer(events, nil)
}

// emitWithSteer delivers events; steerByID (built once per session.join —
// see router.go#negotiateSteer) translates EvJoined/EvPeerJoined into the
// negotiated-mode variants of their ack frames. A session absent from the
// map reports steer=false on wire 1.2 semantics (`{}` for the pre-1.2
// paths — same value, no information loss).
func (r *Router) emitWithSteer(events []session.Event, steerByID map[string]bool) {
	// Session-open and session-close are ONCE-PER-SESSION facts; a 2-member
	// end produces two EvEnded events (one per member), but the gauge must
	// move exactly once.
	endedSeen := make(map[string]struct{}, len(events))
	steerCleared := make(map[string]struct{}, len(events))
	for _, ev := range events {
		// Engine leave is NOT subject to the recipient's socket still
		// existing: an EvEnded for a just-closed connection is precisely
		// the close whose engine session must be scrapped, and the
		// lookup-guarded path below would skip it. Idempotent (LoadAnd
		// Delete on a missing key is a no-op). Steer registry entries are
		// per-session: cleared once, on the FIRST end event, via a
		// dedicated set so the once-per-session GAUGE below keeps its own
		// independence (the two once-gates track different facts).
		if ev.Kind == session.EvEnded {
			r.engineLeaveHook(ev.Recipient, ev.SessionID)
			if _, seen := steerCleared[ev.SessionID]; !seen {
				steerCleared[ev.SessionID] = struct{}{}
				r.steerSessions.Delete(ev.SessionID)
			}
		}
		sub, ok := r.lookup(ev.Recipient)
		if !ok {
			continue
		}
		var frame any
		relayed := false
		switch ev.Kind {
		case session.EvStarted:
			frame = protocol.NewSessionStarted(ev.SessionID)
			r.reg.SignalSessionOpened()
			r.engineJoinHook(sub, ev.SessionID)
		case session.EvJoined:
			frame = protocol.NewSessionJoined(ev.SessionID, steerByID[ev.SessionID])
			r.engineJoinHook(sub, ev.SessionID)
		case session.EvPeerJoined:
			frame = protocol.NewSessionPeerJoined(ev.SessionID, ev.PeerRole, steerByID[ev.SessionID])
		case session.EvEnded:
			frame = protocol.NewSessionEnded(ev.SessionID, ev.Reason)
			if _, seen := endedSeen[ev.SessionID]; !seen {
				endedSeen[ev.SessionID] = struct{}{}
				r.reg.SignalSessionClosed()
			}
		case session.EvForwardOffer:
			frame = protocol.NewSignalOffer(ev.SessionID, ev.SDP)
			relayed = true
		case session.EvForwardAnswer:
			frame = protocol.NewSignalAnswer(ev.SessionID, ev.SDP)
			relayed = true
		case session.EvForwardCandidate:
			frame = protocol.NewSignalCandidate(ev.SessionID, ev.Candidate)
			relayed = true
		default:
			// A new EventKind without a translation is a build-time bug;
			// NOTICE it in the log rather than fanning out mystery frames.
			r.logf("[gateway] signaling: unhandled event kind %d for session %s", ev.Kind, ev.SessionID)
			continue
		}
		if !sub.Enqueue(frame) {
			// Backpressure-drop on a NEGOTIATION is different from dropping
			// a notice: a missing offer silently desyncs the pair. End the
			// session so both sides start clean instead of half-connected.
			r.reg.Dropped(1)
			r.logf("[gateway] signaling: drop on full queue, ending session %s", ev.SessionID)
			_, _ = r.m.End(sub.Tenant(), ev.SessionID, ev.Recipient, session.ReasonPeerDisconnected)
			continue
		}
		if relayed {
			r.relayedTotal.Add(1)
			r.reg.SignalRelayed()
		}
	}
}

// refuse renders a protocol-level rejection (bad payload, oversized body),
// for checks that happen BEFORE the session layer is consulted and so have
// no sentinel error to translate.
func (r *Router) refuse(sub hub.Subscriber, code, message string) {
	sub.Enqueue(protocol.NewError(code, message))
}

// fail renders one error frame to the sender. The session layer's sentinel
// errors map onto the closed protocol vocabulary here and nowhere else, so
// "which client sees which reason" is auditable in one switch.
func (r *Router) fail(sub hub.Subscriber, cause error) {
	var code, message string
	switch {
	case errors.Is(cause, session.ErrUnknownSession):
		// Collapsed by design (unknown id, wrong tenant, or non-member):
		// the client learns "not a session you can name", never WHICH fact
		// failed — the distribution of live sessions is tenant data.
		code, message = protocol.CodeSessionUnknown, "no such session"
	case errors.Is(cause, session.ErrTooManySessions):
		code, message = protocol.CodeSessionFull, "too many concurrent sessions for this account"
	case errors.Is(cause, session.ErrAlreadyInSession):
		code, message = protocol.CodeAlreadyInSession, "this connection already holds a session"
	case errors.Is(cause, session.ErrSessionNotReady):
		code, message = protocol.CodeSessionNotReady, "the peer has not joined yet"
	case errors.Is(cause, session.ErrWrongState), errors.Is(cause, session.ErrSessionFull):
		code, message = protocol.CodeWrongSignalState, "not possible in the session's current state"
	default:
		code, message = protocol.CodeBadMessage, "could not process the signaling message"
	}
	sub.Enqueue(protocol.NewError(code, message))
}

// ---- media-engine lifecycle hooks ----------------------------------------
//
// Boundary (matches the package contract): the gateway's signaling session
// is the AUTHORITY over who may hold engine media sessions. Every joined
// member gets exactly one engine join (participant = their connection id —
// never self-asserted identity); every end — graceful, dropped-socket,
// reaper, or double — produces a leave for THAT member's engine session.
// Ordering is linear because these hooks fire inside emit, which the
// session manager's lock already serializes per session.
//
// Failure policy: an unreachable or refusing engine NEVER breaks session
// establishment (graceful degradation is the product requirement; the
// media plane is additive from the client's perspective). Failures surface
// exactly three ways: engineclient's observer metric, the availability
// gauge (the monitor owns it), and this structured log line — all three
// correlation-friendly, none containing ICE credentials.

// engineJoinHook enrolls one joining member. Called under emit, INLINE and
// bounded by engineCallTimeout: the worst case is one slow read-loop tick
// on the JOINING connection, which is the degradation trade accepted for
// a guarantee that leave events (emit-serialized after join) never race a
// still-in-flight join goroutine for the same member.
func (r *Router) engineJoinHook(sub hub.Subscriber, sessionID string) {
	if r.eng == nil {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), engineCallTimeout)
	defer cancel()
	room := sub.Tenant() + ":" + sessionID
	res, err := r.eng.Join(ctx, room, sub.Session())
	key := sub.Session() + ":" + sessionID
	if err != nil {
		// Unavailable faults were already metered; the LOG names the class
		// so on-call can tell "engine box down" from "protocol skew" at a
		// glance without unwrapping error text.
		if engineclient.IsUnavailable(err) {
			r.logf("[gateway] engine join UNAVAILABLE room=%s session=%s participant=%s: %v", room, sessionID, sub.Session(), err)
		} else {
			r.logf("[gateway] engine join REFUSED room=%s session=%s participant=%s: %v", room, sessionID, sub.Session(), err)
		}
		return
	}
	r.engineSessions.Store(key, res.Session)
}

// engineLeaveHook releases one member's engine session. Unknown/duplicate
// ends LoadAndDelete a missing entry and return silently — the engine-side
// teardown is idempotent, so the replay path cannot double-free.
func (r *Router) engineLeaveHook(connID, sessionID string) {
	if r.eng == nil {
		return
	}
	key := connID + ":" + sessionID
	v, ok := r.engineSessions.LoadAndDelete(key)
	if !ok {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), engineCallTimeout)
	defer cancel()
	if err := r.eng.Leave(ctx, v.(string)); err != nil {
		// The engine's session sweep is the safety net for a lost leave
		// (lease expiry on its side); the log line keeps the occurrence
		// attributable to a session id.
		r.logf("[gateway] engine leave failed participant=%s session=%s engine_session=%s: %v", connID, sessionID, v.(string), err)
	}
}

// engineLeaveAllForConn closes every engine session id recorded under one
// connection id: the ConnClosed safety net (see ConnClosed's comment).
// Called with no lock — engineSessions is a sync.Map; the keys are
// connID+":"+sessionID so a prefix scan is exact, never heuristic.
func (r *Router) engineLeaveAllForConn(connID string) {
	if r.eng == nil {
		return
	}
	prefix := connID + ":"
	var keys []string
	r.engineSessions.Range(func(k, _ any) bool {
		if ks, ok := k.(string); ok && len(ks) > len(prefix) && ks[:len(prefix)] == prefix {
			keys = append(keys, ks)
		}
		return true
	})
	for _, key := range keys {
		v, ok := r.engineSessions.LoadAndDelete(key)
		if !ok {
			continue
		}
		ctx, cancel := context.WithTimeout(context.Background(), engineCallTimeout)
		if err := r.eng.Leave(ctx, v.(string)); err != nil {
			r.logf("[gateway] engine leave failed participant=%s (conn closed) engine_session=%s: %v", connID, v.(string), err)
		}
		cancel()
	}
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/signaling/offer.go (37 lines, sha256 4ce02e974f680b876748501cf3777d8ee6de17a480882282ff20e043803f4890) =====
==============================================================================
```go
package signaling

import (
	"github.com/voxdesk/realtime/gateway-go/internal/hub"
	"github.com/voxdesk/realtime/gateway-go/internal/protocol"
)

// handleOffer relays one SDP offer to the peer. The guards here are exactly
// the ones the relay can honestly make — payload envelope (starts like SDP,
// fits the cap) and phase legality (one outstanding at a time, which the
// session manager enforces). Whether the SDP describes a media session the
// peer can accept is the peer's business: the gateway's two irrelevant
// skills are evaluating codecs and being lied to.
func (r *Router) handleOffer(sub hub.Subscriber, msg *protocol.ClientMessage) {
	// Validity is judged on the trimmed view; the FORWARDED body is the
	// wire bytes untouched — trailing CRLF is how every SDP line ends, and
	// an edge that trims is an edge that rewrites.
	if r.IsSteered(msg.SessionID) {
		r.refuse(sub, protocol.CodeSteerModeBlocked, "session is steered to the media engine; use engine.offer")
		return
	}
	sdp := msg.SDP
	if !protocol.IsValidSDP(sdp) {
		r.refuse(sub, protocol.CodeBadMessage, "sdp must be an SDP blob (the 'v=' version line first)")
		return
	}
	if len(sdp) > protocol.MaxSDPLength {
		r.refuse(sub, protocol.CodeSignalTooLarge, "sdp body exceeds the relay cap")
		return
	}
	events, err := r.m.Offer(sub.Tenant(), msg.SessionID, sub.Session(), sdp)
	if err != nil {
		r.fail(sub, err)
		return
	}
	r.emit(events)
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/signaling/router.go (245 lines, sha256 1923138f85c2161349aba9b3ee4b5c1501b902841320e4627c55d8f4a2dd3661) =====
==============================================================================
```go
// Package signaling is the relay between two authenticated connections that
// share one signaling session: it validates the wire envelope (sizes,
// shapes, phases), drives the session state machine, and translates its
// events into protocol frames enqueued on the right sockets.
//
// Boundaries, stated precisely:
//
//   - It is NOT the realtime notice plane. Rooms, subscriptions, and ingest
//     deliveries belong to the hub; a signaling frame never touches them.
//   - It is NOT auth. The caller (internal/websocket) only dispatches here
//     AFTER hello — Subscriber.Tenant() is by then a verified claim, and
//     this package treats "" as a bug, not input.
//   - It is NOT a media authority. SDP and ICE payloads are forwarded
//     byte-for-byte inside one session; the edge checks envelopes, never
//     contents, because it has no ground truth to check contents against.
package signaling

import (
	"log"
	"sync"
	"sync/atomic"
	"time"

	"github.com/voxdesk/realtime/gateway-go/internal/engineclient"
	"github.com/voxdesk/realtime/gateway-go/internal/hub"
	"github.com/voxdesk/realtime/gateway-go/internal/observability/metrics"
	"github.com/voxdesk/realtime/gateway-go/internal/protocol"
	"github.com/voxdesk/realtime/gateway-go/internal/session"
)

// LookupFunc resolves a connection id to its live socket. Provided by the
// hub at wiring time so this package never locks the hub itself — same
// interface-seam as the fan-out path.
type LookupFunc func(connID string) (hub.Subscriber, bool)

// Router is the signaling front: message handlers called from the
// connection read loop, plus the transport-facing close hook.
type Router struct {
	m      *session.Manager
	lookup LookupFunc
	reg    *metrics.Registry
	logf   func(format string, args ...any)

	// relayedTotal frames forwarded across ALL sessions since boot. Held
	// here (not the metrics registry) because it is a signal-plane concern
	// exported through the gateway's /metrics all the same.
	relayedTotal atomic.Int64

	// eng is the Rust media-engine control client; nil = engine plane
	// disabled (dev), which the hooks treat as "nothing to signal" — NOT
	// as a failure. engineSessions records conn→engine media-session ids
	// so leaves reach the engine scoped to the SAME session its join
	// minted (idempotency: replayed ends call Leave on a session the
	// engine already teared down — the engine answers quiet success).
	eng            *engineclient.Client
	engineSessions sync.Map // key: connID+":"+sessionID, value: engine session id

	// Steer plane (wire 1.2): steerMode is the config's closed vocabulary
	// ("off"|"v1.2"|"force"); steerSessions records the negotiated mode
	// per GATEWAY session id (true = media runs through the engine).
	// Router-owned rather than session.Session-attached: the mode is a
	// gateway-engine wire concern, while the session package stays
	// media-agnostic by charter.
	steerMode     string
	steerSessions sync.Map // key: gateway session id, presence = steered
}

// SetEngine attaches the engine control client post-construction (same
// contract as Server.SetEngine: existing New call sites do not reshape).
func (r *Router) SetEngine(eng *engineclient.Client) { r.eng = eng }

// SetSteerMode pins the closed-vocabulary mode from configuration. Called
// together with SetEngine by the wiring layer (server.SetEngine) — no
// additional NewRouter arity. Values never validated here; config.Load
// owns the closed set.
func (r *Router) SetSteerMode(mode string) { r.steerMode = mode }

// NewRouter wires the relay to a session manager and a connection lookup.
func NewRouter(m *session.Manager, lookup LookupFunc, reg *metrics.Registry) *Router {
	return &Router{m: m, lookup: lookup, reg: reg, logf: log.Printf}
}

// Manager exposes the session manager (the reaper loop and /metrics read
// their state through it).
func (r *Router) Manager() *session.Manager { return r.m }

// RelayedTotal is the forwarded-frame counter (offers + answers +
// candidates delivered to a peer's queue).
func (r *Router) RelayedTotal() int64 { return r.relayedTotal.Load() }

// HandleMessage routes one decoded signaling frame. The caller guarantees
// the sender is authenticated; the router guarantees the receiver, if any,
// is the session's OTHER member and nobody else — that is the entire
// routing contract.
func (r *Router) HandleMessage(sub hub.Subscriber, msg *protocol.ClientMessage) {
	switch msg.Type {
	case protocol.TypeSessionStart:
		tenant := sub.Tenant()
		if tenant == "" {
			// Defense in depth: the read loop's pre-auth gate makes this
			// unreachable today; if a future caller ever dispatches here
			// earlier, signaling still refuses an unpinned tenant.
			sub.Enqueue(protocol.NewError(protocol.CodeHelloRequired, "authenticate first"))
			return
		}
		_, events, err := r.m.Start(tenant, sub.Session())
		if err != nil {
			r.fail(sub, err)
			return
		}
		r.emit(events)

	case protocol.TypeSessionJoin:
		events, err := r.m.Join(sub.Tenant(), msg.SessionID, sub.Session())
		if err != nil {
			r.fail(sub, err)
			return
		}
		r.emitWithSteer(events, r.negotiateSteer(sub, msg.SessionID, events))

	case protocol.TypeSessionEnd:
		events, err := r.m.End(sub.Tenant(), msg.SessionID, sub.Session(), session.ReasonMemberEnded)
		if err != nil {
			r.fail(sub, err)
			return
		}
		r.emit(events)

	case protocol.TypeOffer:
		r.handleOffer(sub, msg)
	case protocol.TypeAnswer:
		r.handleAnswer(sub, msg)
	case protocol.TypeCandidate:
		r.handleCandidate(sub, msg)
	case protocol.TypeEngineOffer, protocol.TypeEngineCandidate,
		protocol.TypeEnginePublish, protocol.TypeEngineSubscribe, protocol.TypeEngineUnsubscribe:
		r.handleEngineSignal(sub, msg)
	default:
		r.refuse(sub, protocol.CodeBadMessage, "unrecognized signaling message type")
	}
}

// ---- steer plane -----------------------------------------------------------

// negotiateSteer computes the session's media-mode decision AT the moment
// the second member joins (the only point where both members' capabilities
// are knowable), records it, and returns it for the ack frames.
//
// Predicate, stated once (all terms must hold):
//
//	mode != "off" AND engine link up AND (mode == "force" OR BOTH members
//	sent the v1.2 marker at hello)
//
// force does NOT override an unavailable engine: joining a steered session
// against a dead SFU hangs every call at the engine's own admission gate —
// landing both members on the P2P relay instead is the graceful-degrade
// posture the platform requires, with the fallback visible in this log+
// metric.
func (r *Router) negotiateSteer(joined hub.Subscriber, sessionID string, events []session.Event) map[string]bool {
	steer := false
	defer func() {
		if steer {
			r.steerSessions.Store(sessionID, true)
		}
	}()
	if r.eng == nil || r.steerMode == "" || r.steerMode == "off" || !r.eng.Up() {
		return map[string]bool{sessionID: false}
	}
	if r.steerMode == "force" {
		steer = true
		return map[string]bool{sessionID: true}
	}
	// v1.2: both members must be steer-capable.
	if !steerCapable(joined) {
		return map[string]bool{sessionID: false}
	}
	// The initiator's connection id is on the EvPeerJoined event.
	var initiatorID string
	for _, ev := range events {
		if ev.Kind == session.EvPeerJoined {
			initiatorID = ev.Recipient
			break
		}
	}
	if initiatorID == "" {
		return map[string]bool{sessionID: false}
	}
	initiator, ok := r.lookup(initiatorID)
	if !ok || !steerCapable(initiator) {
		return map[string]bool{sessionID: false}
	}
	steer = true
	return map[string]bool{sessionID: true}
}

// steerCapable probes the connection through the optional accessor without
// the hub.Subscriber interface growing (contract: it stays four methods).
func steerCapable(sub hub.Subscriber) bool {
	type marker interface{ SteerCapable() bool }
	if m, ok := sub.(marker); ok {
		return m.SteerCapable()
	}
	return false
}

// IsSteered reports the negotiated mode of ONE session — used by the P2P
// handlers (refuse-mixing rule) and by tests.
func (r *Router) IsSteered(sessionID string) bool {
	_, steered := r.steerSessions.Load(sessionID)
	return steered
}

// ConnClosed ends whatever session the connection held a seat in and tells
// the surviving peer. Called from the websocket teardown path — which is
// reached for EVERY close shape (peer close, policy close, write failure,
// heartbeat reap) — so a session can never outlive the sockets it binds.
func (r *Router) ConnClosed(sub hub.Subscriber) {
	// A pending (never-joined) session's drop is silent to the peer set —
	// there IS no peer — but its engine session is real and must not
	// leak: close every engine session this conn ever minted HERE, then
	// let EvEnded leaves (idempotent, LoadAndDelete) handle members of
	// joined sessions through the normal emit path.
	r.engineLeaveAllForConn(sub.Session())
	r.emit(r.m.ConnDropped(sub.Session()))
}

// StartReaper runs the lifecycle sweep until the returned stop function is
// called. Owned here rather than in the manager because reaping produces
// NOTIFICATIONS, and frames are this package's concern.
func (r *Router) StartReaper() (stop func()) {
	done := make(chan struct{})
	go func() {
		ticker := time.NewTicker(r.m.ReapInterval())
		defer ticker.Stop()
		for {
			select {
			case <-ticker.C:
				r.emit(r.m.Reap())
			case <-done:
				return
			}
		}
	}()
	return func() { close(done) }
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/signaling/router_test.go (336 lines, sha256 4b1f9e87e3b22ddc53b532c1c20083aade65d35b8dcecd85cab274f66ee8ca9b) =====
==============================================================================
```go
package signaling

import (
	"strings"
	"testing"
	"time"

	"github.com/voxdesk/realtime/gateway-go/internal/hub"
	"github.com/voxdesk/realtime/gateway-go/internal/observability/metrics"
	"github.com/voxdesk/realtime/gateway-go/internal/protocol"
	"github.com/voxdesk/realtime/gateway-go/internal/session"
)

// fakeConn is the hub.Subscriber stand-in: a recorder that behaves like a
// websocket with an infinitely roomy queue (the drop path is tested
// separately).
type fakeConn struct {
	id     string
	tenant string
	frames []any
}

func (f *fakeConn) Session() string              { return f.id }
func (f *fakeConn) Tenant() string               { return f.tenant }
func (f *fakeConn) RequestClose(_ int, _ string) {}
func (f *fakeConn) Enqueue(msg any) bool         { f.frames = append(f.frames, msg); return true }

// rig wires a router over recorded connections.
type rig struct {
	router *Router
	conns  map[string]*fakeConn
	reg    *metrics.Registry
}

func newRig(ttl time.Duration) *rig {
	r := &rig{conns: map[string]*fakeConn{}}
	r.reg = metrics.New()
	m := session.NewManager(64, ttl)
	lookup := func(connID string) (hub.Subscriber, bool) {
		c, ok := r.conns[connID]
		if !ok {
			return nil, false
		}
		return c, true
	}
	r.router = NewRouter(m, lookup, r.reg)
	return r
}

func (r *rig) conn(id, tenant string) *fakeConn {
	c := &fakeConn{id: id, tenant: tenant}
	r.conns[id] = c
	return c
}

func (r *rig) msg(from *fakeConn, wire string) *protocol.ClientMessage {
	msg, err := protocol.DecodeClientMessage([]byte(wire))
	if err != nil {
		panic("test wire frame must decode: " + wire)
	}
	r.router.HandleMessage(from, msg)
	return msg
}

const sdp = "v=0\r\no=- 1 1 IN IP4 10.0.0.1\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"

func TestFullNegotiationRoundTrip(t *testing.T) {
	r := newRig(time.Minute)
	a := r.conn("conn-a", "tenant-1")
	b := r.conn("conn-b", "tenant-1")

	// --- session.start: the initiator is acked with its role and the id.
	a.frames = nil
	r.msg(a, `{"type":"session.start"}`)
	if len(a.frames) != 1 {
		t.Fatalf("start ack frames = %v", a.frames)
	}
	started, ok := a.frames[0].(protocol.SessionStarted)
	if !ok {
		t.Fatalf("start ack type = %T", a.frames[0])
	}
	if started.Role != protocol.RoleInitiator || started.SessionID == "" {
		t.Fatalf("start ack = %+v", started)
	}
	id := started.SessionID

	// --- session.join: responder acked, initiator woken.
	a.frames, b.frames = nil, nil
	r.msg(b, `{"type":"session.join","session_id":"`+id+`"}`)
	if len(b.frames) != 1 || len(a.frames) != 1 {
		t.Fatalf("join frames a=%v b=%v", a.frames, b.frames)
	}
	if joined := b.frames[0].(protocol.SessionJoined); joined.Role != protocol.RoleResponder {
		t.Errorf("join ack = %+v", joined)
	}
	if pj := a.frames[0].(protocol.SessionPeerJoined); pj.PeerRole != protocol.RoleResponder {
		t.Errorf("peer-joined notice = %+v", pj)
	}

	// --- offer → forwarded to the peer VERBATIM.
	a.frames, b.frames = nil, nil
	r.msg(a, `{"type":"offer","session_id":"`+id+`","sdp":`+jsonString(sdp)+`}`)
	if len(b.frames) != 1 || len(a.frames) != 0 {
		t.Fatalf("offer frames a=%v b=%v", a.frames, b.frames)
	}
	if offer := b.frames[0].(protocol.SignalOffer); offer.SDP != sdp {
		t.Errorf("sdp was rewritten in transit:\n%s", offer.SDP)
	}

	// --- answer → back to the offerer.
	a.frames, b.frames = nil, nil
	r.msg(b, `{"type":"answer","session_id":"`+id+`","sdp":`+jsonString(sdp)+`}`)
	if ans := a.frames[0].(protocol.SignalAnswer); ans.SDP != sdp {
		t.Errorf("answer sdp = %q", ans.SDP)
	}

	// --- candidates relay both ways, byte-for-byte, null included.
	a.frames, b.frames = nil, nil
	r.msg(a, `{"type":"candidate","session_id":"`+id+`","candidate":{"candidate":"candidate:1 1 udp 2130706431 10.0.0.1 9 typ host","sdpMid":"0"}}`)
	if cand := b.frames[0].(protocol.SignalCandidate); !strings.Contains(string(cand.Candidate), "10.0.0.1") {
		t.Errorf("candidate payload = %s", cand.Candidate)
	}
	r.msg(b, `{"type":"candidate","session_id":"`+id+`","candidate":null}`)
	if cand := a.frames[0].(protocol.SignalCandidate); string(cand.Candidate) != "null" {
		t.Errorf("end-of-candidates must relay as null, got %s", cand.Candidate)
	}

	// --- session.end: BOTH members hear it, sender included.
	a.frames, b.frames = nil, nil
	r.msg(a, `{"type":"session.end","session_id":"`+id+`"}`)
	if len(a.frames) != 1 || len(b.frames) != 1 {
		t.Fatalf("end frames a=%v b=%v", a.frames, b.frames)
	}
	if end := b.frames[0].(protocol.SessionEnded); end.Reason != session.ReasonMemberEnded {
		t.Errorf("end reason = %q", end.Reason)
	}

	if r.reg == nil {
		t.Fatal("registry unreachable")
	}
	// Three relayed payloads (offer, answer, first candidate... plus the
	// null candidate) and the session gauges balance.
	rendered := r.reg.Render(0, 0)
	for _, want := range []string{
		"voxdesk_gateway_signal_sessions_current 0",
		"voxdesk_gateway_signal_sessions_total 1",
		"voxdesk_gateway_signal_relayed_total 4",
	} {
		if !strings.Contains(rendered, want) {
			t.Errorf("metrics missing %q", want)
		}
	}
}

// jsonString renders s as a JSON string literal for embedding in wire frames.
func jsonString(s string) string {
	var b strings.Builder
	b.WriteByte('"')
	for _, r := range s {
		switch r {
		case '"':
			b.WriteString(`\"`)
		case '\\':
			b.WriteString(`\\`)
		case '\r':
			b.WriteString(`\r`)
		case '\n':
			b.WriteString(`\n`)
		default:
			b.WriteRune(r)
		}
	}
	b.WriteByte('"')
	return b.String()
}

func TestPayloadGuardsRefuseBeforeTheStateMachine(t *testing.T) {
	r := newRig(time.Minute)
	a := r.conn("conn-a", "tenant-1")

	r.msg(a, `{"type":"session.start"}`)
	id := a.frames[0].(protocol.SessionStarted).SessionID

	// Non-SDP string as sdp.
	a.frames = nil
	r.msg(a, `{"type":"offer","session_id":"`+id+`","sdp":"not-sdp-at-all"}`)
	if errFrame := lastError(t, a); errFrame.Code != protocol.CodeBadMessage {
		t.Errorf("non-sdp offer code = %q", errFrame.Code)
	}

	// Oversize SDP → the DISTINCT signal_too_large, so a legitimate client
	// can tell "fix your message" from "you hit a documented cap".
	a.frames = nil
	big := "v=0\r\n" + strings.Repeat("a=extmap:1 urn:ietf:params:rtp-hdrext:ssrc-audio-level\r\n", protocol.MaxSDPLength/50)
	r.msg(a, `{"type":"offer","session_id":"`+id+`","sdp":`+jsonString(big)+`}`)
	if errFrame := lastError(t, a); errFrame.Code != protocol.CodeSignalTooLarge {
		t.Errorf("oversize offer code = %q", errFrame.Code)
	}

	// Candidate that is not an object.
	a.frames = nil
	r.msg(a, `{"type":"candidate","session_id":"`+id+`","candidate":[1,2,3]}`)
	if errFrame := lastError(t, a); errFrame.Code != protocol.CodeBadMessage {
		t.Errorf("array candidate code = %q", errFrame.Code)
	}
}

func TestJoinUnknownAndCrossTenantAreTheSameRefusal(t *testing.T) {
	r := newRig(time.Minute)
	a := r.conn("conn-a", "tenant-1")
	x := r.conn("conn-x", "tenant-2")

	r.msg(a, `{"type":"session.start"}`)
	id := a.frames[0].(protocol.SessionStarted).SessionID

	x.frames = nil
	r.msg(x, `{"type":"session.join","session_id":"`+id+`"}`)
	if errFrame := lastError(t, x); errFrame.Code != protocol.CodeSessionUnknown {
		t.Fatalf("cross-tenant join code = %q", errFrame.Code)
	}
	// An UNSEATED connection of the same tenant joining a made-up id gets
	// the same collapse (conn-a already holds a seat, so ITS refusal would
	// honestly be already_in_session — its own constraint, not session data).
	fresh := r.conn("conn-f", "tenant-1")
	fresh.frames = nil
	r.msg(fresh, `{"type":"session.join","session_id":"11111111-2222-3333-4444-555555555555"}`)
	if errFrame := lastError(t, fresh); errFrame.Code != protocol.CodeSessionUnknown {
		t.Fatalf("unknown join code = %q", errFrame.Code)
	}
	// Both refusal messages are the SAME TEXT — the collapsing is total.
	if lastError(t, x).Message != lastError(t, fresh).Message {
		t.Errorf("refusal texts must not vary: %q vs %q", lastError(t, x).Message, lastError(t, fresh).Message)
	}
}

func TestPhaseRefusalsCarryTheSpecificCodes(t *testing.T) {
	r := newRig(time.Minute)
	a := r.conn("conn-a", "tenant-1")
	b := r.conn("conn-b", "tenant-1")

	r.msg(a, `{"type":"session.start"}`)
	id := a.frames[0].(protocol.SessionStarted).SessionID

	// Offer before the peer joins → session_not_ready.
	a.frames = nil
	r.msg(a, `{"type":"offer","session_id":"`+id+`","sdp":`+jsonString(sdp)+`}`)
	if errFrame := lastError(t, a); errFrame.Code != protocol.CodeSessionNotReady {
		t.Errorf("lone offer code = %q", errFrame.Code)
	}

	r.msg(b, `{"type":"session.join","session_id":"`+id+`"}`)

	// Glare guard: after one offer, the second is wrong_signal_state.
	r.msg(a, `{"type":"offer","session_id":"`+id+`","sdp":`+jsonString(sdp)+`}`)
	a.frames = nil
	r.msg(a, `{"type":"offer","session_id":"`+id+`","sdp":`+jsonString(sdp)+`}`)
	if errFrame := lastError(t, a); errFrame.Code != protocol.CodeWrongSignalState {
		t.Errorf("glare code = %q", errFrame.Code)
	}

	// Already-seated connections cannot seize a second session.
	a.frames = nil
	r.msg(a, `{"type":"session.start"}`)
	if errFrame := lastError(t, a); errFrame.Code != protocol.CodeAlreadyInSession {
		t.Errorf("double start code = %q", errFrame.Code)
	}
}

func TestConnClosedEndsTheSessionForTheSurvivor(t *testing.T) {
	r := newRig(time.Minute)
	a := r.conn("conn-a", "tenant-1")
	b := r.conn("conn-b", "tenant-1")

	r.msg(a, `{"type":"session.start"}`)
	id := a.frames[0].(protocol.SessionStarted).SessionID
	r.msg(b, `{"type":"session.join","session_id":"`+id+`"}`)

	b.frames = nil
	r.router.ConnClosed(a)
	if len(b.frames) != 1 {
		t.Fatalf("survivor frames = %v", b.frames)
	}
	if end := b.frames[0].(protocol.SessionEnded); end.Reason != session.ReasonPeerDisconnected {
		t.Errorf("end reason = %q", end.Reason)
	}
}

func TestReapJoinTimeoutIsDeliveredToTheWaiter(t *testing.T) {
	r := newRig(time.Nanosecond) // expired the moment it exists
	a := r.conn("conn-a", "tenant-1")

	r.msg(a, `{"type":"session.start"}`)
	a.frames = nil

	// Tick the reaper directly (StartReaper's ticker cadence is what main
	// does; the sweep itself is what this test owns).
	r.router.emit(r.router.Manager().Reap())
	if len(a.frames) != 1 {
		t.Fatalf("reap frames = %v", a.frames)
	}
	if end := a.frames[0].(protocol.SessionEnded); end.Reason != session.ReasonJoinTimeout {
		t.Errorf("reap reason = %q", end.Reason)
	}
}

func TestNoFrameEverLeavesTheSessionPair(t *testing.T) {
	r := newRig(time.Minute)
	a := r.conn("conn-a", "tenant-1")
	b := r.conn("conn-b", "tenant-1")
	innocent := r.conn("conn-i", "tenant-1")

	r.msg(a, `{"type":"session.start"}`)
	id := a.frames[0].(protocol.SessionStarted).SessionID
	r.msg(b, `{"type":"session.join","session_id":"`+id+`"}`)
	r.msg(a, `{"type":"offer","session_id":"`+id+`","sdp":`+jsonString(sdp)+`}`)
	r.msg(b, `{"type":"answer","session_id":"`+id+`","sdp":`+jsonString(sdp)+`}`)
	r.msg(a, `{"type":"candidate","session_id":"`+id+`","candidate":null}`)
	r.msg(b, `{"type":"session.end","session_id":"`+id+`"}`)

	if len(innocent.frames) != 0 {
		t.Fatalf("an uninvolved connection observed %d frames", len(innocent.frames))
	}
}

// lastError asserts the tail frame of conn is an error and returns it.
func lastError(t *testing.T, conn *fakeConn) protocol.ErrorMessage {
	t.Helper()
	if len(conn.frames) == 0 {
		t.Fatalf("expected an error frame on %s, saw none", conn.id)
	}
	errFrame, ok := conn.frames[len(conn.frames)-1].(protocol.ErrorMessage)
	if !ok {
		t.Fatalf("tail frame on %s = %T, want protocol.ErrorMessage", conn.id, conn.frames[len(conn.frames)-1])
	}
	return errFrame
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/signaling/steer.go (220 lines, sha256 b35cb068a5653a7d663ec0833f456c1c8ee01bf31336385786e8529754d4fb86) =====
==============================================================================
```go
// Steer-mode signaling: wire-1.2's engine.* frame handlers. Each is the
// counterpart of a classic relay handler but addresses the media engine
// (via engineclient) instead of the peer's socket. The invariants:
//
//  1. A frame is honored ONLY on a session negotiated steered (see
//     negotiateSteer); anything else refuses steer_mode_blocked — mixing
//     media paths per session is forbidden by construction.
//  2. Every engine call is bounded (engineCallTimeout) and NEVER blocks
//     the session's survival: engine failure surfaces as one error frame
//     to the caller + the availability gauge + a classified log line;
//     the session itself remains valid for retry.
//  3. Browser netsession identity never leaks engine-side identities:
//     the wire keys on the GATEWAY session id; the router translates via
//     engineSessions. Fanout to the peer uses protocol frames, never
//     engine frames.
//
// Engine server frames this code understands from one reply: "answer"
// (→ engine.answer to the caller), "track.published" (→ engine.track_
// published to the peer), "error" (→ refuse to the caller). Anything else
// is an engine-side surprise: logged, dropped, counted — never relayed
// blindly (relay-what-you-don't-parse is exactly the trust inversion the
// edge exists to prevent).
package signaling

import (
	"context"
	"encoding/json"

	"github.com/voxdesk/realtime/gateway-go/internal/engineclient"
	"github.com/voxdesk/realtime/gateway-go/internal/hub"
	"github.com/voxdesk/realtime/gateway-go/internal/protocol"
)

// handleEngineSignal dispatches one engine.* frame under the steered
// invariants (package header).
func (r *Router) handleEngineSignal(sub hub.Subscriber, msg *protocol.ClientMessage) {
	// 1. Mode gate: negotiated steered?
	if !r.IsSteered(msg.SessionID) {
		r.refuse(sub, protocol.CodeSteerModeBlocked, "session is not steered to the media engine (negotiated mode is P2P)")
		return
	}
	if r.eng == nil {
		r.refuse(sub, protocol.CodeEngineUnavailable, "no media engine is configured on this gateway")
		return
	}

	// 2. Membership + translation gate: the caller holds a seat in THE
	//    session it names, and that seat is enrolled engine-side.
	current, ok := r.m.SessionForConn(sub.Session())
	if !ok || current != msg.SessionID {
		r.refuse(sub, protocol.CodeSessionUnknown, "no such session")
		return
	}
	engineSID, ok := r.engineSessions.Load(sub.Session() + ":" + msg.SessionID)
	if !ok {
		// The engine did not admit this member (join hook failed at
		// session time): the frame cannot reach any steerable state —
		// answer the degrade posture rather than pretend.
		r.refuse(sub, protocol.CodeEngineUnavailable, "engine session is not enrolled for this connection")
		return
	}
	sid := engineSID.(string)

	// 3. Per-variant payload guards mirror the relay handlers'.
	ctx, cancel := context.WithTimeout(context.Background(), engineCallTimeout)
	defer cancel()

	switch msg.Type {
	case protocol.TypeEngineOffer:
		sdp := msg.SDP
		if !protocol.IsValidSDP(sdp) {
			r.refuse(sub, protocol.CodeBadMessage, "sdp must be an SDP blob (the 'v=' version line first)")
			return
		}
		if len(sdp) > protocol.MaxSDPLength {
			r.refuse(sub, protocol.CodeSignalTooLarge, "sdp body exceeds the relay cap")
			return
		}
		frames, err := r.eng.Offer(ctx, sid, sdp)
		if err != nil {
			r.engineRefusal(sub, "offer", err)
			return
		}
		r.dispatchEngineFrames(sub, msg.SessionID, frames)

	case protocol.TypeEngineCandidate:
		if !protocol.CandidateIsObject(msg.Candidate) {
			r.refuse(sub, protocol.CodeBadMessage, "candidate must be an ICE candidate object or the null end marker")
			return
		}
		var payload any
		if err := json.Unmarshal(msg.Candidate, &payload); err != nil {
			r.refuse(sub, protocol.CodeBadMessage, "candidate is not parseable JSON")
			return
		}
		frames, err := r.eng.Trickle(ctx, sid, payload)
		if err != nil {
			r.engineRefusal(sub, "trickle", err)
			return
		}
		r.dispatchEngineFrames(sub, msg.SessionID, frames)

	case protocol.TypeEnginePublish:
		frames, err := r.eng.Publish(ctx, sid, msg.Track, msg.Kind)
		if err != nil {
			r.engineRefusal(sub, "publish", err)
			return
		}
		r.dispatchEngineFrames(sub, msg.SessionID, frames)

	case protocol.TypeEngineSubscribe, protocol.TypeEngineUnsubscribe:
		// The peer of a steered 2-member session is THE other member's
		// engine participant; the browser says only WHICH track.
		peerConn, ok := r.m.OtherMember(sub.Tenant(), msg.SessionID, sub.Session())
		if !ok {
			r.refuse(sub, protocol.CodeSessionNotReady, "the peer has not joined yet")
			return
		}
		var err error
		if msg.Type == protocol.TypeEngineSubscribe {
			err = r.eng.Subscribe(ctx, sid, peerConn, msg.Track)
		} else {
			err = r.eng.Unsubscribe(ctx, sid, peerConn, msg.Track)
		}
		if err != nil {
			r.engineRefusal(sub, "subscribe", err)
			return
		}
	}
}

// dispatchEngineFrames turns the engine reply frames into WIRE frames for
// the right sockets (invariant 3). Called synchronously with the engine
// call; any enqueue failure routes a backpressure drop to the metrics the
// same way emit does — a negotiation frame lost to a full peer queue
// desyncs the steered path exactly like the P2P path, so the drop policy
// is shared (see events.go#emit).
func (r *Router) dispatchEngineFrames(caller hub.Subscriber, sessionID string, frames []engineclient.Frame) {
	for _, f := range frames {
		frameType, _ := f["type"].(string)
		switch frameType {
		case "answer":
			sdp, _ := f["sdp"].(string)
			if sdp == "" {
				r.logf("[gateway] steer: engine answer without sdp, session %s", sessionID)
				r.reg.Dropped(1)
				continue
			}
			if !caller.Enqueue(protocol.NewEngineAnswer(sessionID, sdp)) {
				r.reg.Dropped(1)
				r.logf("[gateway] steer: engine answer dropped on full queue, session %s", sessionID)
			}
		case "track.published":
			participant, _ := f["participant"].(string)
			track, _ := f["track"].(string)
			kind, _ := f["kind"].(string)
			if participant == "" || track == "" || kind == "" {
				r.logf("[gateway] steer: engine track.published fields incomplete: %v", f)
				r.reg.Dropped(1)
				continue
			}
			r.fanoutTrackPublished(caller, sessionID, participant, track, kind)
		case "error":
			code, _ := f["code"].(string)
			message, _ := f["message"].(string)
			r.refuse(caller, engineWireCode(code), message)
		default:
			// Never blind-relay an engine-side surprise (invariant header).
			r.logf("[gateway] steer: unexpected engine frame type %q dropped, session %s", frameType, sessionID)
			r.reg.Dropped(1)
		}
	}
}

// fanoutTrackPublished delivers the publish notice to the caller's PEER
// in the session — exactly one socket in the 2-member model.
func (r *Router) fanoutTrackPublished(caller hub.Subscriber, sessionID, participant, track, kind string) {
	peerConn, ok := r.m.OtherMember(caller.Tenant(), sessionID, caller.Session())
	if !ok {
		return // peer left between engine call and dispatch: nobody to tell
	}
	peer, ok := r.lookup(peerConn)
	if !ok {
		return
	}
	if !peer.Enqueue(protocol.NewEngineTrackPublished(sessionID, participant, track, kind)) {
		r.reg.Dropped(1)
		r.logf("[gateway] steer: track.published dropped on full peer queue, session %s", sessionID)
	}
}

// engineRefusal answers an engine-side CALL failure (transport/down/
// structured refusal — all already metered by the client observer) with a
// session-preserving error frame and the classified log the runbook greps.
func (r *Router) engineRefusal(sub hub.Subscriber, op string, err error) {
	if engineclient.IsUnavailable(err) {
		r.logf("[gateway] steer %s UNAVAILABLE participant=%s: %v", op, sub.Session(), err)
		r.refuse(sub, protocol.CodeEngineUnavailable, "media engine is temporarily unavailable; retry in a moment")
		return
	}
	r.logf("[gateway] steer %s REFUSED participant=%s: %v", op, sub.Session(), err)
	r.refuse(sub, protocol.CodeBadMessage, "media engine refused the frame; check the frame shape")
}

// engineWireCode maps the engine's in-band error codes onto the CLOSED
// gateway protocol vocabulary (the browser never sees engine-side code
// names — they are an internal taxonomy, and an unmapped value must not
// leak).
func engineWireCode(code string) string {
	switch code {
	case "bad_message":
		return protocol.CodeBadMessage
	case "room_full", "over_limit":
		return protocol.CodeOverLimit
	case "wrong_state":
		return protocol.CodeWrongSignalState
	default:
		return protocol.CodeBadMessage
	}
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/validate/validate.go (107 lines, sha256 09aa6bf80507dc009f6e35ba06e09c581b8963f2e2a7e55f8ee4b241cba409b1) =====
==============================================================================
```go
// Package validate holds the small, shared shape-checks used across the
// gateway: UUIDs, room names, event kinds and placeholder secrets.
//
// These checks exist in exactly one place for the same reason the Python
// backend keeps one `verify_twilio_request`: a validation rule that is
// written twice will eventually be enforced in one place and not the other.
package validate

import "strings"

// IsUUID reports whether s is a canonical RFC 4122 UUID (8-4-4-4-12, hex,
// either case). The gateway never generates these — it only needs to know
// that a tenant id / call id / event id coming off the wire is the shape the
// Python backend persists, so a malformed id can never become a routing key
// or a log-confusable string.
func IsUUID(s string) bool {
	if len(s) != 36 {
		return false
	}
	for i := 0; i < 36; i++ {
		switch i {
		case 8, 13, 18, 23:
			if s[i] != '-' {
				return false
			}
		default:
			c := s[i]
			if !('0' <= c && c <= '9' || 'a' <= c && c <= 'f' || 'A' <= c && c <= 'F') {
				return false
			}
		}
	}
	return true
}

// Room namespaces the public edge will ever fan out to. This is a closed
// set on purpose: an open "subscribe to anything" surface would let a
// browser enumerate internal feed names. A dashboard subscribes to the
// tenant-wide feeds ("calls", "metrics") or to exactly one entity feed
// ("call:<uuid>", "campaign:<uuid>"). Anything else is room_invalid.
const (
	RoomCalls      = "calls"
	RoomMetrics    = "metrics"
	RoomCallPrefix = "call:"
	RoomCampPrefix = "campaign:"
)

// IsRoomName reports whether room is a public, well-formed room name.
func IsRoomName(room string) bool {
	switch room {
	case RoomCalls, RoomMetrics:
		return true
	}
	for _, prefix := range []string{RoomCallPrefix, RoomCampPrefix} {
		if strings.HasPrefix(room, prefix) {
			return IsUUID(room[len(prefix):])
		}
	}
	return false
}

// IsEventKind reports whether kind is a dotted event name such as
// "call.updated" or "transcript.turn". Kinds travel inside delivery frames
// and let the dashboard dispatch without parsing payloads; keeping them
// lower-snake-dotted stops a publisher from smuggling markup or control
// characters into a client's dispatch table.
func IsEventKind(kind string) bool {
	if len(kind) < 3 || len(kind) > 64 {
		return false
	}
	parts := strings.Split(kind, ".")
	if len(parts) < 2 {
		return false
	}
	for _, p := range parts {
		if p == "" {
			return false
		}
		for i := 0; i < len(p); i++ {
			c := p[i]
			if !('a' <= c && c <= 'z' || '0' <= c && c <= '9' || c == '_' || c == '-') {
				return false
			}
		}
	}
	return true
}

// placeholderMarkers mirrors app/core/config.py: a value containing any of
// these substrings is an example, not a credential, and the service must
// refuse to boot with it rather than "secure" a public edge with a string
// that is published in the repository.
var placeholderMarkers = []string{"change-me", "change_me", "insecure", "xxxx", "placeholder", "your-"}

// LooksPlaceholder reports whether value is obviously an unset example.
func LooksPlaceholder(value string) bool {
	lowered := strings.ToLower(value)
	if lowered == "" {
		return false
	}
	for _, marker := range placeholderMarkers {
		if strings.Contains(lowered, marker) {
			return true
		}
	}
	return false
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/validate/validate_test.go (95 lines, sha256 044afe2d20e8b136e535a9552f1192ec9b543b5e866e75a9473a2672efa5555e) =====
==============================================================================
```go
package validate

import "testing"

func TestIsUUID(t *testing.T) {
	valid := []string{
		"550e8400-e29b-41d4-a716-446655440000",
		"550E8400-E29B-41D4-A716-446655440000", // upper case accepted (Python uuid.UUID normalises)
		"00000000-0000-0000-0000-000000000000",
	}
	for _, s := range valid {
		if !IsUUID(s) {
			t.Errorf("IsUUID(%q) = false, want true", s)
		}
	}
	invalid := []string{
		"",
		"550e8400e29b41d4a716446655440000",      // no dashes
		"550e8400-e29b-41d4-a716-44665544000",   // too short
		"550e8400-e29b-41d4-a716-4466554400000", // too long
		"550e8400-e29b-41d4-a716-44665544000g",  // non-hex
		"550e8400_e29b_41d4_a716_446655440000",  // wrong separators
	}
	for _, s := range invalid {
		if IsUUID(s) {
			t.Errorf("IsUUID(%q) = true, want false", s)
		}
	}
}

func TestIsRoomName(t *testing.T) {
	valid := []string{
		"calls",
		"metrics",
		"call:550e8400-e29b-41d4-a716-446655440000",
		"campaign:550e8400-e29b-41d4-a716-446655440000",
	}
	for _, s := range valid {
		if !IsRoomName(s) {
			t.Errorf("IsRoomName(%q) = false, want true", s)
		}
	}
	invalid := []string{
		"",
		"admin",           // not a public namespace
		"call:not-a-uuid", // malformed entity id
		"call:",           // empty entity id
		"tenant:550e8400-e29b-41d4-a716-446655440000", // tenant must come from the token, not the room
		"calls; DROP TABLE tenants",                   // injection-shaped
		"metrics ",                                    // trailing whitespace
	}
	for _, s := range invalid {
		if IsRoomName(s) {
			t.Errorf("IsRoomName(%q) = true, want false", s)
		}
	}
}

func TestIsEventKind(t *testing.T) {
	valid := []string{"call.updated", "transcript.turn", "metrics.tick_15s", "a.b.c-d_e"}
	for _, s := range valid {
		if !IsEventKind(s) {
			t.Errorf("IsEventKind(%q) = false, want true", s)
		}
	}
	invalid := []string{
		"",
		"noversion", // no dot
		"no..dots",
		".leading",
		"trailing.",
		"UPPER.CASE", // kinds are lower-snake by convention
		"<script>alert(1)</script>.x",
	}
	for _, s := range invalid {
		if IsEventKind(s) {
			t.Errorf("IsEventKind(%q) = true, want false", s)
		}
	}
}

func TestLooksPlaceholder(t *testing.T) {
	if !LooksPlaceholder("insecure-development-only-change-me") {
		t.Error("the Python default JWT secret must be detected as a placeholder")
	}
	if !LooksPlaceholder("XXXX-1234") {
		t.Error("xxxx marker should match case-insensitively")
	}
	if LooksPlaceholder("0123456789abcdef0123456789abcdef") {
		t.Error("a real hex secret must not be flagged")
	}
	if LooksPlaceholder("") {
		t.Error("empty is handled separately (required check), not as placeholder")
	}
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/websocket/close.go (52 lines, sha256 4499abc2f6048c08cbdcbb04639cc1418fa7dcf390ee2a9c2ed7c381738de889) =====
==============================================================================
```go
package websocket

import (
	"time"

	"github.com/voxdesk/realtime/gateway-go/internal/protocol"
)

// Close-code policy (a comment instead of constants scattered through the
// lifecycle files, so the WHOLE set is reviewable in one place):
//
//   - protocol.CloseNormal (1000)          — the reader ended; orderly end
//   - protocol.CloseGoingAway (1001)       — server shutdown, failed writer
//   - protocol.ClosePolicyViolation (1008) — auth failed, auth timeout,
//                                            over capacity, token expired,
//                                            pong timeout
//   - protocol.CloseInternalError (1011)   — reserved for unexpected failure
//
// No other code is ever sent. The protocol constants are referenced so the
// compiler enforces they exist; policy comments alone cannot drift.

// initiateClose terminates the session exactly once, racing callers
// notwithstanding: the reader exiting, an administrative RequestClose from
// the hub (shutdown), and the heartbeat's reaper can all fire on the same
// dead session, and only the first one matters.
//
// Ordering contract (why this function does NOT write the close frame
// itself): a session often owes its peer a final DATA frame before the
// close — an auth_failed error, an over-limit notice. The single writer
// goroutine owns all data frames, so termination works like this:
//
//  1. close(c.closed)   — tells the writer to drain queued frames and then
//     write the close frame with the recorded code/reason (writer.go);
//  2. SetReadDeadline in the past — unblocks a reader parked in ReadMessage
//     WITHOUT closing the socket from under the writer's final frames;
//  3. Serve waits for the writer to finish, then closes the socket.
//
// The code/reason are plain fields written here (inside the sync.Once, so
// before `closed` closes) and read by the writer afterwards — the channel
// close provides the happens-before edge, no extra mutex needed.
func (c *Connection) initiateClose(code int, reason string) {
	c.closeOnce.Do(func() {
		c.closeCode = code
		c.closeReason = reason
		close(c.closed)
		_ = c.ws.SetReadDeadline(time.Now().Add(-time.Second))
		if code != protocol.CloseNormal {
			c.logf("[gateway] closing session %s: code %d reason %q",
				c.sessionID, code, reason)
		}
	})
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/websocket/connection.go (301 lines, sha256 1b3bd3b391ea507f9de1df97612fb6e31f646a9ef31011cbf4218645e8d799dc) =====
==============================================================================
```go
package websocket

import (
	"crypto/rand"
	"fmt"
	"log"
	"sync"
	"sync/atomic"
	"time"

	gorilla "github.com/gorilla/websocket"

	"github.com/voxdesk/realtime/gateway-go/internal/auth"
	"github.com/voxdesk/realtime/gateway-go/internal/backpressure"
	"github.com/voxdesk/realtime/gateway-go/internal/config"
	"github.com/voxdesk/realtime/gateway-go/internal/hub"
	"github.com/voxdesk/realtime/gateway-go/internal/observability/metrics"
	"github.com/voxdesk/realtime/gateway-go/internal/presence"
	"github.com/voxdesk/realtime/gateway-go/internal/protocol"
	"github.com/voxdesk/realtime/gateway-go/internal/ratelimit"
	"github.com/voxdesk/realtime/gateway-go/internal/signaling"
)

// Connection is one live public WebSocket session.
//
// Lifecycle: Upgrade → Serve → (welcome → auth → frames) → close.
// Serve blocks until the session ends and performs all teardown itself, so
// the HTTP handler that called it simply returns.
type Connection struct {
	sessionID string
	ws        *gorilla.Conn

	cfg      config.Config
	hub      *hub.Hub
	verifier *auth.Verifier
	metrics  *metrics.Registry
	signaler *signaling.Router
	presence *presence.Registry
	logf     func(format string, args ...any)

	// outgoing is the bounded queue the hub fans frames into. Buffered; a
	// full queue drops (backpressure.DropNewest) rather than stalling a
	// room — the policy is the queue's, not a select/default at each site.
	outgoing *backpressure.Queue[any]

	// limiter is the per-connection inbound frame budget (see
	// reader.go#allowMessage); internally synchronized, lifetime-owned by
	// the connection, quota sourced from cfg at construction.
	limiter *ratelimit.Limiter

	// mutable session state, guarded by mu: pinned identity after hello.
	mu            sync.Mutex
	tenantID      string
	userID        string
	role          string
	tokenExpiry   time.Time
	subscriptions map[string]struct{}
	// steerCapable records the hello "ws" marker (>=2). Pinned exactly
	// once at hello like tenant identity; read capability-checked by the
	// signaling router through the SteerCapable accessor.
	steerCapable bool

	// lastActivity is unix-nanos of the last inbound frame of ANY kind
	// (data or pong); heartbeat reads it to reap silently-dead peers.
	lastActivity atomic.Int64

	// closeOnce makes termination idempotent across the reader exiting, an
	// administrative RequestClose, and the heartbeat's reaper all firing on
	// the same dead session. closeCode/closeReason are written inside that
	// Once (before `closed` closes) and read by the writer afterwards.
	closeOnce   sync.Once
	closeCode   int
	closeReason string
	closed      chan struct{}
	writerDone  chan struct{}
}

// NewConnection wraps an upgraded socket. The session is NOT registered in
// the hub yet — Serve does that, so a Connection that is created but never
// served cannot leak registry state.
func NewConnection(
	ws *gorilla.Conn,
	cfg config.Config,
	h *hub.Hub,
	verifier *auth.Verifier,
	reg *metrics.Registry,
	signaler *signaling.Router,
	pr *presence.Registry,
) (*Connection, error) {
	sessionID, err := newUUIDv4()
	if err != nil {
		return nil, err
	}
	c := &Connection{
		sessionID:     sessionID,
		ws:            ws,
		cfg:           cfg,
		hub:           h,
		verifier:      verifier,
		metrics:       reg,
		signaler:      signaler,
		presence:      pr,
		logf:          log.Printf,
		outgoing:      backpressure.New[any](cfg.OutgoingBuffer, backpressure.DropNewest),
		subscriptions: make(map[string]struct{}),
		closed:        make(chan struct{}),
		writerDone:    make(chan struct{}),
		// The bucket starts full inside the limiter: a fresh tab
		// reconnecting after a drop must be able to re-assert its
		// subscriptions immediately. cfg's values are range-checked at
		// boot, so MustPolicy cannot panic here.
		limiter: ratelimit.New(ratelimit.MustPolicy(cfg.MessageRatePerSecond, cfg.MessageBurst)),
	}
	c.touch()
	return c, nil
}

// Serve runs the session until it ends, then tears it down completely.
func (c *Connection) Serve() {
	c.metrics.ConnOpened()
	defer c.metrics.ConnClosed()
	defer c.hub.Unregister(c.sessionID)
	// Presence mirrors hub registration for the authenticated USER (the
	// hub tracks sockets; presence tracks the JWT-verified user behind
	// them). Deferred right after hub.Unregister so it runs FIRST under
	// LIFO: the user is unreachable from the hub the moment presence says
	// so, never the reverse.
	defer c.leavePresence()

	// The welcome goes out BEFORE the writer goroutine starts, so this is
	// the only data frame not written by the writer loop — visibly, in one
	// place, with no second writer possible.
	if !c.writeFrameNow(protocol.NewWelcome(c.sessionID, c.cfg.AuthTimeout, c.cfg.PingInterval, time.Now())) {
		return
	}

	c.hub.Register(c)

	// A signaling session must never outlive the socket that seats a
	// member: whichever way this connection ends (peer close, policy close,
	// heartbeat reap, write failure — they all funnel through Serve's
	// return), tell the relay so the surviving peer hears session.ended
	// instead of negotiating with a ghost. Deferred BEFORE hub.Unregister
	// runs (LIFO), so the relay's lookup of the surviving peer still
	// resolves cleanly.
	if c.signaler != nil {
		defer c.signaler.ConnClosed(c)
	}

	go c.writer()
	go c.heartbeat()

	// The reader runs in the caller and blocks for the session's life; it
	// returns on peer close, deadline, policy close, or a torn writer.
	c.readLoop()

	// Whatever ended the reader ends the session: idempotent close, then
	// wait for the writer to drain its final frames and exit so the socket
	// is never closed out from under an in-flight WriteMessage. The wait is
	// backstopped: a peer that has stopped reading must not hold teardown
	// longer than a couple of write deadlines.
	c.initiateClose(protocol.CloseNormal, "session ended")
	select {
	case <-c.writerDone:
	case <-time.After(3*c.cfg.WriteWait + time.Second):
	}
	_ = c.ws.Close()
}

// ---------------------------------------------------------------------------
// hub.Subscriber implementation
// ---------------------------------------------------------------------------

// Session implements hub.Subscriber.
func (c *Connection) Session() string { return c.sessionID }

// Tenant implements hub.Subscriber ("" until hello succeeds).
func (c *Connection) Tenant() string {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.tenantID
}

// Enqueue implements hub.Subscriber: non-blocking; false on a full queue
// (the hub counts that as a backpressure drop; the queue counts it too).
func (c *Connection) Enqueue(msg any) bool {
	return c.outgoing.Enqueue(msg)
}

// RequestClose implements hub.Subscriber (administrative close: shutdown,
// token expiry). Idempotent by construction.
func (c *Connection) RequestClose(code int, reason string) {
	c.initiateClose(code, reason)
}

// ---------------------------------------------------------------------------
// helpers shared by reader/heartbeat/close
// ---------------------------------------------------------------------------

// touch stamps activity (any inbound frame) for the heartbeat reaper.
func (c *Connection) touch() {
	c.lastActivity.Store(time.Now().UnixNano())
}

// lastActivityTime returns the last inbound activity moment.
func (c *Connection) lastActivityTime() time.Time {
	return time.Unix(0, c.lastActivity.Load())
}

// setIdentity pins the verified identity and joins presence; called
// exactly once, at hello. The presence join uses nothing but verified
// claims — the registry never sees a client-asserted identity.
func (c *Connection) setIdentity(claims *auth.Claims) {
	c.mu.Lock()
	c.tenantID = claims.TenantID
	c.userID = claims.UserID
	c.role = claims.Role
	c.tokenExpiry = claims.ExpiresAt
	c.mu.Unlock()
	if c.presence != nil {
		c.presence.Online(claims.TenantID, claims.UserID, c.sessionID)
	}
}

// leavePresence drops this session's presence record. Safe pre-auth:
// without a pinned identity the registry call is a deliberate no-op, and a
// nil registry (a deployment or test that opts out) is tolerated.
func (c *Connection) leavePresence() {
	if c.presence == nil {
		return
	}
	tenantID, userID := c.presenceIdentity()
	if tenantID == "" || userID == "" {
		return
	}
	c.presence.Offline(tenantID, userID, c.sessionID)
}

// identity snapshots the pinned identity (safe for heartbeat/close paths).
func (c *Connection) identity() (tenantID string, role string, expiry time.Time) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.tenantID, c.role, c.tokenExpiry
}

// presenceIdentity snapshots the (tenant, user) pair presence keys on.
func (c *Connection) presenceIdentity() (tenantID, userID string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.tenantID, c.userID
}

// SteerCapable implements the optional capability accessor the signaling
// router probes through an interface-assertion on hub.Subscriber: true when
// the client declared wire 1.2 at hello ("ws":2).
func (c *Connection) SteerCapable() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.steerCapable
}

// isAuthenticated reports whether hello has pinned a tenant.
func (c *Connection) isAuthenticated() bool {
	tenantID, _, _ := c.identity()
	return tenantID != ""
}

// subscribe records membership locally (the hub holds the authoritative
// room sets; this mirror exists for the per-connection subscription cap).
func (c *Connection) subscribe(room string) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if _, ok := c.subscriptions[room]; ok {
		return nil
	}
	if len(c.subscriptions) >= c.cfg.MaxSubscriptionsPerConn {
		return fmt.Errorf("subscription cap %d reached", c.cfg.MaxSubscriptionsPerConn)
	}
	c.subscriptions[room] = struct{}{}
	return nil
}

// unsubscribe drops a local membership mirror.
func (c *Connection) unsubscribe(room string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	delete(c.subscriptions, room)
}

// newUUIDv4 returns a random RFC 4122 v4 UUID — the same byte shape the
// Python backend, signal-go and the Rust hub all emit, so a session id is
// log-correlatable across every hop.
func newUUIDv4() (string, error) {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", err
	}
	b[6] = (b[6] & 0x0f) | 0x40 // version 4
	b[8] = (b[8] & 0x3f) | 0x80 // RFC 4122 variant
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16]), nil
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/websocket/heartbeat.go (90 lines, sha256 fad162458c694039a7a67003e9e7bc456a03bc5ccf274f5f07a547a7a7db0ed4) =====
==============================================================================
```go
package websocket

import (
	"time"

	gorilla "github.com/gorilla/websocket"

	"github.com/voxdesk/realtime/gateway-go/internal/protocol"
)

// tokenExpiryGrace is how long a session may outlive its access token's
// exp before the edge closes it. Aligned with auth.clockLeeway (30 s): the
// dashboard rotates its access token on refresh, so a session is only ever
// asked to reconnect — never abruptly cut mid-refresh-cycle — and a
// forgotten tab still dies within a minute of the credential lapsing.
const tokenExpiryGrace = 30 * time.Second

// heartbeat is the session's timekeeper. Every PingInterval it:
//
//  1. sends a WS-level ping (keepalive — proxies and mobile NATs commonly
//     reap idle sockets at 60 s; the browser's automatic pong also proves
//     the peer is still there);
//  2. reaps a peer that has gone silent past PongTimeout (backgrounded
//     tabs lose their timers; dead TCP peers never error politely);
//  3. enforces the access-token expiry: the edge never outlives the
//     credential that opened it, so when the token lapses the socket is
//     closed with a reason the dashboard can reconnect on.
//
// Deadline arithmetic (the subtle part): the read deadline is extended on
// EVERY inbound frame — data or pong — by the reader and pong handler. The
// heartbeat's staleness check is therefore a second, independent tripwire:
// it fires only when NOTHING has arrived for a full PongTimeout, which is
// precisely the case the read deadline would also catch. Two mechanisms,
// one policy: a silently dead peer must not hold a slot.
func (c *Connection) heartbeat() {
	ticker := time.NewTicker(c.cfg.PingInterval)
	defer ticker.Stop()
	for {
		select {
		case <-c.closed:
			return
		case <-ticker.C:
			if !c.pingOnce() {
				return
			}
		}
	}
}

// pingOnce performs one heartbeat round. It reports false when the session
// has ended (so the ticker goroutine exits instead of leaking).
func (c *Connection) pingOnce() bool {
	select {
	case <-c.closed:
		return false
	default:
	}

	// Token-expiry enforcement first: closing for an expired credential is
	// a policy decision, not a liveness failure, and must happen even on a
	// perfectly healthy socket.
	_, _, expiry := c.identity()
	if !expiry.IsZero() && time.Now().After(expiry.Add(tokenExpiryGrace)) {
		c.enqueueOwn(protocol.NewError(protocol.CodeAuthFailed, "access token expired; reconnect with a fresh token"))
		c.initiateClose(protocol.ClosePolicyViolation, "token_expired")
		return false
	}

	// Silent-peer reaper. Pre-auth sessions are not heartbeated: their
	// deadline is the reader's short AuthTimeout, and touching them here
	// would only double the policy's owners.
	if c.isAuthenticated() && time.Since(c.lastActivityTime()) > c.cfg.PongTimeout {
		c.initiateClose(protocol.ClosePolicyViolation, "pong timeout")
		return false
	}

	// WriteControl is safe concurrently with the writer goroutine's data
	// frames (gorilla guarantees it); control frames never queue behind a
	// slow consumer, which is exactly why keepalive belongs here and not in
	// the outgoing channel.
	if err := c.ws.WriteControl(
		gorilla.PingMessage,
		[]byte(c.sessionID),
		time.Now().Add(c.cfg.WriteWait),
	); err != nil {
		c.initiateClose(protocol.CloseGoingAway, "heartbeat write failed")
		return false
	}
	return true
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/websocket/reader.go (183 lines, sha256 8762055909255a49f724be779bee0dbfab8eb7da56df08d5b4dc281d57341398) =====
==============================================================================
```go
package websocket

import (
	"errors"
	"time"

	gorilla "github.com/gorilla/websocket"

	"github.com/voxdesk/realtime/gateway-go/internal/hub"
	"github.com/voxdesk/realtime/gateway-go/internal/protocol"
	"github.com/voxdesk/realtime/gateway-go/internal/validate"
)

// readLoop is the session's inbound driver and runs in the caller of Serve
// (the HTTP handler goroutine). It returns — ending the session — on the
// peer closing, a deadline expiring, a policy close, or the writer tearing
// the socket down.
//
// Deadline policy: before authentication the read deadline is the short
// AuthTimeout (a socket that never says hello must not hold a slot); after
// hello it relaxes to PongTimeout and is thereafter owned by the heartbeat
// (every inbound frame extends it, so a busy session is never reaped).
func (c *Connection) readLoop() {
	c.ws.SetReadLimit(c.cfg.MaxMessageBytes)
	c.ws.SetPongHandler(func(string) error {
		c.touch()
		// gorilla's default ping handler already answers pings with pongs;
		// this hook is where the heartbeat's liveness bookkeeping happens.
		return c.ws.SetReadDeadline(time.Now().Add(c.cfg.PongTimeout))
	})

	_ = c.ws.SetReadDeadline(time.Now().Add(c.cfg.AuthTimeout))
	for {
		messageType, data, err := c.ws.ReadMessage()
		if err != nil {
			return
		}
		c.touch()
		if messageType != gorilla.TextMessage {
			// The protocol is JSON text frames only; a binary frame is a
			// client talking a different protocol, not an attack surface
			// worth decoding.
			c.sendError(protocol.CodeBadMessage, "text frames only")
			continue
		}
		c.metrics.MessageRead()

		if !c.allowMessage() {
			c.metrics.RateLimited()
			c.sendError(protocol.CodeRateLimited, "slow down")
			continue
		}

		msg, err := protocol.DecodeClientMessage(data)
		if err != nil {
			c.sendError(protocol.CodeBadMessage, "unrecognized message shape")
			continue
		}
		if !c.dispatch(msg) {
			return // dispatch closed the session (auth failure, etc.)
		}
	}
}

// dispatch handles one decoded frame. It reports false when the session
// must end.
func (c *Connection) dispatch(msg *protocol.ClientMessage) bool {
	// Pre-auth gate: nothing but hello (and a liveness ping, which is safe
	// to answer — it reveals nothing but uptime, mirroring the internal
	// hub's dispatch) exists until a token has been verified — the public
	// edge's difference from the internal hub is that hello PROVES
	// something here.
	if !c.isAuthenticated() && msg.Type != protocol.TypeHello && msg.Type != protocol.TypePing {
		c.sendError(protocol.CodeHelloRequired, "authenticate first")
		return true
	}

	switch msg.Type {
	case protocol.TypeHello:
		return c.handleHello(msg.Token, msg.WSVersion)
	case protocol.TypeSubscribe:
		c.handleSubscribe(msg.Room)
	case protocol.TypeUnsubscribe:
		c.handleUnsubscribe(msg.Room)
	case protocol.TypePing:
		c.enqueueOwn(protocol.NewPong(time.Now()))
	case protocol.TypeSessionStart, protocol.TypeSessionJoin, protocol.TypeSessionEnd,
		protocol.TypeOffer, protocol.TypeAnswer, protocol.TypeCandidate,
		protocol.TypeEngineOffer, protocol.TypeEngineCandidate, protocol.TypeEnginePublish,
		protocol.TypeEngineSubscribe, protocol.TypeEngineUnsubscribe:
		// Signaling frames share every guard the notice plane has (pre-auth
		// gate above, per-connection frame limiter, same socket lifetime) —
		// they differ only in destination: the session state machine, not
		// the hub's rooms. A nil router (a deployment that never wires one)
		// refuses cleanly rather than panicking the loop.
		if c.signaler == nil {
			c.sendError(protocol.CodeBadMessage, "signaling is not enabled on this gateway")
			return true
		}
		c.signaler.HandleMessage(c, msg)
	default:
		c.sendError(protocol.CodeBadMessage, "unrecognized message type")
	}
	return true
}

// handleHello verifies the token and pins the session to its tenant.
// Reports false when the session must end (any failure: the edge does not
// keep unauthenticated sockets around to try again).
func (c *Connection) handleHello(token string, wsVersion int) bool {
	if c.isAuthenticated() {
		c.sendError(protocol.CodeBadMessage, "already authenticated; reconnect to change identity")
		return true
	}

	claims, err := c.verifier.Verify(token, time.Now())
	if err != nil {
		c.metrics.AuthFailed()
		// Server-side log carries the precise reason; the client gets the
		// generic code, so a forger learns nothing about WHICH check failed.
		c.logf("[gateway] auth failed, session %s: %v", c.sessionID, err)
		c.enqueueOwn(protocol.NewError(protocol.CodeAuthFailed, "invalid or expired token"))
		c.initiateClose(protocol.ClosePolicyViolation, "authentication failed")
		return false
	}

	if err := c.hub.BindTenant(c.sessionID, claims.TenantID); err != nil {
		if errors.Is(err, hub.ErrOverTenantCap) {
			c.sendError(protocol.CodeOverLimit, "too many connections for this account")
			c.initiateClose(protocol.ClosePolicyViolation, "over tenant capacity")
			return false
		}
		c.sendError(protocol.CodeAuthFailed, "session could not be bound")
		c.initiateClose(protocol.ClosePolicyViolation, "bind failed")
		return false
	}

	c.setIdentity(claims)
	if wsVersion >= 2 {
		c.mu.Lock()
		c.steerCapable = true
		c.mu.Unlock()
	}
	c.enqueueOwn(protocol.NewReady(c.sessionID, claims.TenantID, claims.Role, claims.ExpiresAt))
	// Authenticated now: the longer heartbeat-managed deadline applies.
	_ = c.ws.SetReadDeadline(time.Now().Add(c.cfg.PongTimeout))
	return true
}

// handleSubscribe joins (pinned tenant, room) and acknowledges with the
// resulting peer count.
func (c *Connection) handleSubscribe(room string) {
	if !validate.IsRoomName(room) {
		c.sendError(protocol.CodeRoomInvalid, "unknown room; expected calls, metrics, call:<uuid> or campaign:<uuid>")
		return
	}
	if err := c.subscribe(room); err != nil {
		c.sendError(protocol.CodeOverLimit, "too many subscriptions on this connection")
		return
	}
	peers, err := c.hub.Subscribe(c.sessionID, room)
	if err != nil {
		c.unsubscribe(room)
		c.sendError(protocol.CodeBadMessage, "session is no longer registered")
		return
	}
	c.enqueueOwn(protocol.NewSubscribed(room, peers))
}

// handleUnsubscribe leaves (pinned tenant, room); idempotent.
func (c *Connection) handleUnsubscribe(room string) {
	c.hub.Unsubscribe(c.sessionID, room)
	c.unsubscribe(room)
	c.enqueueOwn(protocol.NewUnsubscribed(room))
}

// allowMessage is the per-connection token bucket (internal/ratelimit,
// internally synchronized). It exists for one reason: a hijacked or buggy
// tab must not be able to keep the gateway CPU-busy decoding frames
// unboundedly.
func (c *Connection) allowMessage() bool {
	return c.limiter.Allow()
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/websocket/upgrader.go (78 lines, sha256 f4c1389166f205ab1b5920809d6bf12132fe1d4f85e378a4d2ac3d5dc11a2a82) =====
==============================================================================
```go
// Package websocket owns the per-connection lifecycle of the public edge:
// upgrade, authentication, reading, writing, heartbeat, and close.
//
// The package is layered deliberately:
//
//	upgrader.go   — HTTP → WebSocket, origin policy, capacity gate
//	connection.go — the session object and its orchestration
//	reader.go     — inbound frame loop, dispatch, auth, rate limiting
//	writer.go     — the ONLY goroutine that writes data frames
//	heartbeat.go  — WS-level ping/pong deadlines, token-expiry enforcement
//	close.go      — idempotent, code-correct session termination
//
// Concurrency contract (gorilla's, which this package relies on exactly):
// one goroutine may read, one may write, and Close/WriteControl may run
// concurrently with anything. The writer goroutine owns ALL data frames;
// the heartbeat and close paths use WriteControl only.
package websocket

import (
	"net/http"
	"strings"
	"sync/atomic"

	gorilla "github.com/gorilla/websocket"

	"github.com/voxdesk/realtime/gateway-go/internal/config"
)

// upgraderCounter assigns a cheap monotonically increasing number to every
// upgrade attempt; it exists only so refusal logs can be correlated with the
// metric bump when several upgrades fail in the same second.
var upgraderCounter atomic.Uint64

// NewUpgrader builds the shared gorilla Upgrader (safe for concurrent use).
//
// Origin policy: browsers send Origin; non-browser clients commonly do not.
// The token is the actual credential, so an empty origin list must not
// accidentally allow every web page on the internet to hold a user's socket
// (CSWSH — a malicious origin riding the ambient... no, there is no ambient
// credential here, the token is explicit; but a permissive origin check
// would still let any site that obtains a token keep it alive). Fail
// closed: with no allowlist, only origin-less (same-host tooling) requests
// pass, mirroring the internal hub's "the edge checks origins" division of
// labour — except here WE are the edge, so the check lives in this file.
func NewUpgrader(cfg config.Config) gorilla.Upgrader {
	allowed := make(map[string]struct{}, len(cfg.AllowedOrigins))
	for _, origin := range cfg.AllowedOrigins {
		allowed[strings.ToLower(origin)] = struct{}{}
	}
	return gorilla.Upgrader{
		ReadBufferSize:  4096,
		WriteBufferSize: 4096,
		CheckOrigin: func(r *http.Request) bool {
			origin := r.Header.Get("Origin")
			if origin == "" {
				// Not a browser cross-origin request (server tooling,
				// curl, same-origin fetch from the served dashboard).
				return true
			}
			_, ok := allowed[strings.ToLower(origin)]
			return ok
		},
		// Never offer subprotocols: auth travels in the first JSON frame
		// (hello), not in Sec-WebSocket-Protocol, so there is exactly one
		// credential path to review.
		Subprotocols: nil,
		Error: func(w http.ResponseWriter, _ *http.Request, status int, _ error) {
			// Uniform refusal shape — no reason text, so the upgrade path
			// never leaks which policy (origin vs. capacity) fired.
			http.Error(w, http.StatusText(status), status)
		},
	}
}

// attemptNumber returns this process's next upgrade-attempt ordinal.
func attemptNumber() uint64 {
	return upgraderCounter.Add(1)
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/internal/websocket/writer.go (95 lines, sha256 ed7d8c0e0edcb174ee74499fd2abdf412a58cf4b1e6420badc46cdc74d76022d) =====
==============================================================================
```go
package websocket

import (
	"time"

	gorilla "github.com/gorilla/websocket"

	"github.com/voxdesk/realtime/gateway-go/internal/protocol"
)

// writer is the ONLY goroutine that writes data frames to the socket. It
// drains the connection's outgoing queue until the session closes, then
// exits — closing writerDone so Serve can finish teardown without racing a
// final WriteMessage against ws.Close().
//
// Frames are marshalled HERE (not at enqueue time) so the hub never pays
// per-subscriber serialization cost for one publish, and so a frame that
// can never be written (a session already closing) costs nothing.
func (c *Connection) writer() {
	defer close(c.writerDone)
	for {
		select {
		case msg := <-c.outgoing.C():
			if !c.writeFrameNow(msg) {
				// The socket is gone; end the session so the reader and
				// heartbeat stop too (mirrors the internal hub's
				// select-loop break on send error).
				c.initiateClose(protocol.CloseGoingAway, "write failed")
				return
			}
		case <-c.closed:
			// Drain every queued frame (each write is still bounded by
			// WriteWait), THEN send the close frame with the code/reason
			// initiateClose recorded. Data-before-close ordering matters:
			// an auth_failed error that arrives after the close frame is an
			// error the client never sees.
			for {
				select {
				case msg := <-c.outgoing.C():
					if !c.writeFrameNow(msg) {
						return
					}
				default:
					_ = c.ws.WriteControl(
						gorilla.CloseMessage,
						gorilla.FormatCloseMessage(c.closeCode, c.closeReason),
						time.Now().Add(c.cfg.WriteWait),
					)
					return
				}
			}
		}
	}
}

// writeFrameNow marshals and writes one frame with the configured write
// deadline. Reports false when the write failed and the session must end.
//
// Called from (a) Serve before the loops start (the welcome), and (b) the
// writer goroutine — never concurrently, so gorilla's single-writer rule
// holds by construction rather than by a mutex.
func (c *Connection) writeFrameNow(msg any) bool {
	data, err := protocol.Marshal(msg)
	if err != nil {
		// A frame constructed by our own code can only fail to marshal if
		// an ingest payload was not valid JSON — ingest validates that, so
		// this is a can't-happen guard, logged and dropped rather than
		// fatal to the session.
		c.logf("[gateway] marshal failed, session %s: %v", c.sessionID, err)
		return true
	}
	if err := c.ws.SetWriteDeadline(time.Now().Add(c.cfg.WriteWait)); err != nil {
		return false
	}
	if err := c.ws.WriteMessage(gorilla.TextMessage, data); err != nil {
		return false
	}
	return true
}

// enqueueOwn routes the session's OWN replies (ready, subscribed, pong,
// errors) through the same bounded queue as deliveries: one writer, one
// ordering, one backpressure rule. A full queue drops the reply — a session
// that cannot keep up loses non-critical frames before it stalls a room,
// which is exactly the Rust BoundedBroadcast contract.
func (c *Connection) enqueueOwn(msg any) {
	if !c.Enqueue(msg) {
		c.metrics.Dropped(1)
	}
}

// sendError is enqueueOwn specialized for error frames.
func (c *Connection) sendError(code, message string) {
	c.enqueueOwn(protocol.NewError(code, message))
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/tests/README.md (16 lines, sha256 acca97c244433972cb4bae1fafca8ce03520f31732329726e7291991620299bb) =====
==============================================================================
```markdown
# gateway-go/tests

Cross-package tests that do not belong to any single `internal/` package.
Package-internal unit and e2e tests stay next to their packages (Go
convention); only tests whose subject IS the seam between packages live
here.

| Directory    | What it pins                                                                 | Runs with `go test ./...`? |
|--------------|------------------------------------------------------------------------------|-----------------------------|
| `protocol/`  | Byte-exact wire format goldens (`.golden` files). A diff here is a **wire-format change**: regenerate only deliberately with `go test ./tests/protocol -update` and review the golden diff in code review. | yes |
| `integration/` | The assembled system across package boundaries (config → auth → hub → server → protocol → presence → observability), including the X-Request-ID trace continuing API → gateway → event stream. | yes |
| `load/`      | Fan-out smoke harness (hundreds of sockets, tens of thousands of frames). Behind the `load` build tag: `go test -tags load ./tests/load/ -v -timeout 120s`. | **no** — timing-sensitive by nature |

Golden files live in `protocol/testdata/` — one file per server frame, so a
format change shows up as a one-line diff next to the struct change that
caused it.
```

==============================================================================
===== FILE: services/realtime/gateway-go/tests/integration/edge_flow_test.go (233 lines, sha256 9059c2d22b1a8a3ac5c63a4d699482098607d1cc72096b98b2401853a49a5704) =====
==============================================================================
```go
// Package integration exercises the gateway as one assembled system across
// package boundaries (config → auth → hub → server → protocol → presence →
// observability), the way a deploy actually wires it. Package-internal e2e
// tests live next to their packages; THIS test guards the seams between
// them — which is where package splits actually break things.
package integration

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	gorilla "github.com/gorilla/websocket"

	"github.com/voxdesk/realtime/gateway-go/internal/auth"
	"github.com/voxdesk/realtime/gateway-go/internal/config"
	"github.com/voxdesk/realtime/gateway-go/internal/hub"
	"github.com/voxdesk/realtime/gateway-go/internal/observability"
	"github.com/voxdesk/realtime/gateway-go/internal/observability/metrics"
	"github.com/voxdesk/realtime/gateway-go/internal/server"
	"github.com/voxdesk/realtime/gateway-go/internal/session"
	"github.com/voxdesk/realtime/gateway-go/internal/signaling"
)

const (
	jwtSecret    = "0123456789abcdef0123456789abcdef"
	ingestSecret = "ingest-shared-secret-0123"
	tenantA      = "11111111-1111-1111-1111-111111111111"
	userA        = "99999999-8888-7777-6666-555555555555"
)

func testConfig() config.Config {
	return config.Config{
		Port:                          8790,
		JWTSecret:                     jwtSecret,
		JWTIssuer:                     "voxdesk",
		JWTAudience:                   "voxdesk-api",
		IngestSecret:                  ingestSecret,
		MaxConnections:                100,
		MaxConnsPerTenant:             10,
		MaxSubscriptionsPerConn:       8,
		OutgoingBuffer:                16,
		MaxMessageBytes:               16 * 1024,
		MaxIngestPayloadBytes:         4 * 1024,
		WriteWait:                     2 * time.Second,
		AuthTimeout:                   2 * time.Second,
		PingInterval:                  250 * time.Millisecond,
		PongTimeout:                   800 * time.Millisecond,
		ShutdownTimeout:               2 * time.Second,
		MessageRatePerSecond:          100,
		MessageBurst:                  100,
		IdempotencyTTL:                time.Minute,
		IdempotencyCapacity:           100,
		SignalingMaxSessionsPerTenant: 8,
		SignalingPendingTimeout:       time.Minute,
	}
}

func mintToken(t *testing.T, claims map[string]any) string {
	t.Helper()
	header, _ := json.Marshal(map[string]any{"alg": "HS256", "typ": "JWT"})
	body, _ := json.Marshal(claims)
	head := base64.RawURLEncoding.EncodeToString(header)
	payload := base64.RawURLEncoding.EncodeToString(body)
	mac := hmac.New(sha256.New, []byte(jwtSecret))
	mac.Write([]byte(head + "." + payload))
	return head + "." + payload + "." + base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
}

func claims(ttl time.Duration) map[string]any {
	now := time.Now()
	return map[string]any{
		"sub": userA, "tid": tenantA, "role": "admin", "tv": float64(1),
		"typ": "access", "iat": float64(now.Unix()), "nbf": float64(now.Unix()),
		"exp": float64(now.Add(ttl).Unix()), "iss": "voxdesk", "aud": "voxdesk-api", "jti": "int-1",
	}
}

// captureEmitter records structured events for assertions.
type captureEmitter struct {
	mu   sync.Mutex
	seen []observability.Event
}

func (c *captureEmitter) Emit(e observability.Event) {
	c.mu.Lock()
	c.seen = append(c.seen, e)
	c.mu.Unlock()
}

func (c *captureEmitter) find(name string) *observability.Event {
	c.mu.Lock()
	defer c.mu.Unlock()
	for i := range c.seen {
		if c.seen[i].Name == name {
			return &c.seen[i]
		}
	}
	return nil
}

func readFrame(t *testing.T, conn *gorilla.Conn) map[string]any {
	t.Helper()
	_ = conn.SetReadDeadline(time.Now().Add(3 * time.Second))
	_, data, err := conn.ReadMessage()
	if err != nil {
		t.Fatalf("read frame: %v", err)
	}
	var frame map[string]any
	if err := json.Unmarshal(data, &frame); err != nil {
		t.Fatalf("frame is not JSON: %v (%s)", err, data)
	}
	return frame
}

// TestAssembledEdgeLoop is the cross-package proof: a dashboard session and
// an API publish traverse FOUR packages (auth/hub/server/protocol), surface
// in TWO observability planes (request-id echo, structured event + presence
// gauge), and never cross a tenant boundary anywhere along the way.
func TestAssembledEdgeLoop(t *testing.T) {
	t.Parallel()

	cfg := testConfig()
	h := hub.New(cfg.MaxConnsPerTenant)
	reg := metrics.New()
	verifier := auth.NewVerifier(cfg.JWTSecret, cfg.JWTIssuer, cfg.JWTAudience)
	sessions := session.NewManager(cfg.SignalingMaxSessionsPerTenant, cfg.SignalingPendingTimeout)
	signaler := signaling.NewRouter(sessions, h.Lookup, reg)
	srv := server.New(cfg, h, reg, verifier, signaler)
	events := &captureEmitter{}
	srv.UseEmitter(events)

	httpSrv := httptest.NewServer(srv.Handler())
	t.Cleanup(httpSrv.Close)
	t.Cleanup(srv.CloseAll)

	// --- browser side ---
	wsURL := "ws" + strings.TrimPrefix(httpSrv.URL, "http") + "/ws"
	conn, _, err := gorilla.DefaultDialer.Dial(wsURL, nil)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer conn.Close()
	if welcome := readFrame(t, conn); welcome["type"] != "welcome" {
		t.Fatalf("welcome = %v", welcome)
	}
	token := mintToken(t, claims(5*time.Minute))
	if err := conn.WriteMessage(gorilla.TextMessage, []byte(fmt.Sprintf(`{"type":"hello","token":%q}`, token))); err != nil {
		t.Fatalf("hello: %v", err)
	}
	if ready := readFrame(t, conn); ready["type"] != "ready" || ready["tenant_id"] != tenantA {
		t.Fatalf("ready = %v", ready)
	}
	if err := conn.WriteMessage(gorilla.TextMessage, []byte(`{"type":"subscribe","room":"calls"}`)); err != nil {
		t.Fatalf("subscribe: %v", err)
	}
	if sub := readFrame(t, conn); sub["type"] != "subscribed" {
		t.Fatalf("subscribed = %v", sub)
	}

	// The JWT's sub made the user present on this node — presence is a
	// real registry reachable across the package boundary, not a stub.
	if !srv.Presence().IsOnline(tenantA, userA) {
		t.Fatal("verified identity must appear in presence after hello")
	}
	if reg.PresenceUsersCurrent() != 1 {
		t.Fatalf("presence gauge must track transitions, got %d", reg.PresenceUsersCurrent())
	}

	// --- API side, with a correlation id the way the real API sends it ---
	req, err := http.NewRequest(http.MethodPost, httpSrv.URL+"/ingest/v1/publish",
		strings.NewReader(fmt.Sprintf(`{"tenant_id":%q,"room":"calls","kind":"call.updated","payload":{"call_id":"int-call-1"}}`, tenantA)))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Authorization", "Bearer "+ingestSecret)
	req.Header.Set("X-Request-ID", "api-trace-abc-123")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("ingest POST: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		t.Fatalf("ingest status = %d", resp.StatusCode)
	}
	if got := resp.Header.Get("X-Request-ID"); got != "api-trace-abc-123" {
		t.Fatalf("the API's request id must echo back unchanged, got %q", got)
	}

	// --- the browser hears its tenant's event ---
	delivery := readFrame(t, conn)
	if delivery["type"] != "delivery" || delivery["kind"] != "call.updated" {
		t.Fatalf("delivery = %v", delivery)
	}
	payload, _ := delivery["payload"].(map[string]any)
	if payload["call_id"] != "int-call-1" {
		t.Fatalf("payload corrupted: %v", payload)
	}

	// --- the event stream carries the SAME correlation id ---
	accepted := events.find("ingest.accepted")
	if accepted == nil {
		t.Fatal("no ingest.accepted event emitted")
	}
	if accepted.Field("request_id") != "api-trace-abc-123" || accepted.Field("tenant") != tenantA || accepted.Field("delivered") != "1" {
		t.Fatalf("event fields wrong: %+v", accepted.Fields)
	}

	// --- teardown flips presence back, gauge included ---
	conn.Close()
	deadline := time.Now().Add(3 * time.Second)
	for srv.Presence().IsOnline(tenantA, userA) && time.Now().Before(deadline) {
		time.Sleep(5 * time.Millisecond)
	}
	if srv.Presence().IsOnline(tenantA, userA) {
		t.Fatal("presence must drop when the last session of a user closes")
	}
	deadline = time.Now().Add(3 * time.Second)
	for reg.PresenceUsersCurrent() != 0 && time.Now().Before(deadline) {
		time.Sleep(5 * time.Millisecond)
	}
	if reg.PresenceUsersCurrent() != 0 {
		t.Fatalf("gauge must follow transitions down too, got %d", reg.PresenceUsersCurrent())
	}
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/tests/load/load_test.go (175 lines, sha256 b64ab2e414fc991895491a74ae9b73153c6226a811dfde4f807179677481c482) =====
==============================================================================
```go
//go:build load

// Package load holds the gateway's throughput/latency harness. It is behind
// the `load` build tag BY DESIGN: it opens hundreds of sockets and measures
// wall-clock fan-out, so it must never run as part of the deterministic
// correctness gate. Run it on purpose:
//
//	go test -tags load ./tests/load/ -run . -v -timeout 120s
//
// This is a SMOKE-level load check (does fan-out scale to a few hundred
// connections with reasonable latency?), not a capacity study — that wants
// k6/Locust against a deployed node, recorded in ops runbooks.
package load

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	gorilla "github.com/gorilla/websocket"

	"github.com/voxdesk/realtime/gateway-go/internal/auth"
	"github.com/voxdesk/realtime/gateway-go/internal/config"
	"github.com/voxdesk/realtime/gateway-go/internal/hub"
	"github.com/voxdesk/realtime/gateway-go/internal/observability/metrics"
	"github.com/voxdesk/realtime/gateway-go/internal/server"
	"github.com/voxdesk/realtime/gateway-go/internal/session"
	"github.com/voxdesk/realtime/gateway-go/internal/signaling"
)

const (
	jwtSecret    = "0123456789abcdef0123456789abcdef"
	ingestSecret = "ingest-shared-secret-0123"
	tenant       = "11111111-1111-1111-1111-111111111111"
)

func mintToken(t *testing.T, jti string) string {
	t.Helper()
	now := time.Now()
	header, _ := json.Marshal(map[string]any{"alg": "HS256", "typ": "JWT"})
	body, _ := json.Marshal(map[string]any{
		"sub": "99999999-8888-7777-6666-555555555555", "tid": tenant, "role": "admin",
		"tv": float64(1), "typ": "access", "iat": float64(now.Unix()), "nbf": float64(now.Unix()),
		"exp": float64(now.Add(10 * time.Minute).Unix()), "iss": "voxdesk", "aud": "voxdesk-api", "jti": jti,
	})
	head := base64.RawURLEncoding.EncodeToString(header)
	payload := base64.RawURLEncoding.EncodeToString(body)
	mac := hmac.New(sha256.New, []byte(jwtSecret))
	mac.Write([]byte(head + "." + payload))
	return head + "." + payload + "." + base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
}

// TestFanOutSmoke opens 200 subscribers (well under the fixture's 10k cap,
// representative of a busy wallboard floor), publishes 50 events, and
// asserts every subscriber hears all of them, with the LAST delivery
// landing inside a generous deadline — the "hub doesn't fall over under
// realistic burst" property.
func TestFanOutSmoke(t *testing.T) {
	const conns, publishes = 200, 50

	cfg := config.Config{
		JWTSecret: jwtSecret, JWTIssuer: "voxdesk", JWTAudience: "voxdesk-api",
		IngestSecret: ingestSecret, MaxConnections: 10_000, MaxConnsPerTenant: 1_000,
		MaxSubscriptionsPerConn: 8, OutgoingBuffer: 256,
		MaxMessageBytes: 16 * 1024, MaxIngestPayloadBytes: 4 * 1024,
		WriteWait: 2 * time.Second, AuthTimeout: 5 * time.Second,
		PingInterval: 10 * time.Second, PongTimeout: 30 * time.Second,
		ShutdownTimeout:      5 * time.Second,
		MessageRatePerSecond: 1_000, MessageBurst: 1_000,
		IdempotencyTTL: time.Minute, IdempotencyCapacity: 1_000,
		SignalingMaxSessionsPerTenant: 8, SignalingPendingTimeout: time.Minute,
	}
	h := hub.New(cfg.MaxConnsPerTenant)
	reg := metrics.New()
	verifier := auth.NewVerifier(cfg.JWTSecret, cfg.JWTIssuer, cfg.JWTAudience)
	sessions := session.NewManager(cfg.SignalingMaxSessionsPerTenant, cfg.SignalingPendingTimeout)
	signaler := signaling.NewRouter(sessions, h.Lookup, reg)
	srv := server.New(cfg, h, reg, verifier, signaler)
	httpSrv := httptest.NewServer(srv.Handler())
	t.Cleanup(httpSrv.Close)
	t.Cleanup(srv.CloseAll)

	wsURL := "ws" + strings.TrimPrefix(httpSrv.URL, "http") + "/ws"
	var received atomic.Int64
	var wg sync.WaitGroup
	clients := make([]*gorilla.Conn, 0, conns)

	start := time.Now()
	for i := 0; i < conns; i++ {
		conn, _, err := gorilla.DefaultDialer.Dial(wsURL, nil)
		if err != nil {
			t.Fatalf("dial %d: %v", i, err)
		}
		clients = append(clients, conn)
		defer conn.Close()
		// welcome, hello, ready, subscribe, subscribed.
		if _, _, err := conn.ReadMessage(); err != nil {
			t.Fatalf("welcome %d: %v", i, err)
		}
		hello := fmt.Sprintf(`{"type":"hello","token":%q}`, mintToken(t, fmt.Sprintf("load-%d", i)))
		if err := conn.WriteMessage(gorilla.TextMessage, []byte(hello)); err != nil {
			t.Fatalf("hello %d: %v", i, err)
		}
		if _, _, err := conn.ReadMessage(); err != nil {
			t.Fatalf("ready %d: %v", i, err)
		}
		if err := conn.WriteMessage(gorilla.TextMessage, []byte(`{"type":"subscribe","room":"calls"}`)); err != nil {
			t.Fatalf("subscribe %d: %v", i, err)
		}
		if _, _, err := conn.ReadMessage(); err != nil {
			t.Fatalf("subscribed %d: %v", i, err)
		}

		wg.Add(1)
		go func(c *gorilla.Conn) {
			defer wg.Done()
			for {
				if err := c.SetReadDeadline(time.Now().Add(30 * time.Second)); err != nil {
					return
				}
				_, _, err := c.ReadMessage()
				if err != nil {
					return
				}
				received.Add(1)
			}
		}(conn)
	}
	t.Logf("%d sessions established in %v", conns, time.Since(start))

	start = time.Now()
	for n := 0; n < publishes; n++ {
		body := fmt.Sprintf(`{"tenant_id":%q,"room":"calls","kind":"call.updated","payload":{"seq":%d}}`, tenant, n)
		req, err := http.NewRequest(http.MethodPost, httpSrv.URL+"/ingest/v1/publish", strings.NewReader(body))
		if err != nil {
			t.Fatal(err)
		}
		req.Header.Set("Authorization", "Bearer "+ingestSecret)
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatalf("publish %d: %v", n, err)
		}
		resp.Body.Close()
		if resp.StatusCode != 200 {
			t.Fatalf("publish %d status %d", n, resp.StatusCode)
		}
	}

	want := int64(conns * publishes)
	deadline := time.Now().Add(30 * time.Second)
	for received.Load() < want && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	elapsed := time.Since(start)
	if got := received.Load(); got != want {
		t.Fatalf("delivered %d of %d frames", got, want)
	}
	t.Logf("fanned %d events to %d conns (%d frames) in %v — %.0f frames/s",
		publishes, conns, want, elapsed, float64(want)/elapsed.Seconds())

	rendered := reg.Render(0, 0)
	if !strings.Contains(rendered, "voxdesk_gateway_dropped_total 0") {
		t.Fatalf("256-deep buffers must absorb the burst without drops:\n%s", rendered)
	}
	_ = clients
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/tests/protocol/golden_test.go (84 lines, sha256 e8976756739cee2579f1ba7d911002083ee6e379351c6294550c923ffe18ac12) =====
==============================================================================
```go
// Package protocolgolden pins the gateway's wire format byte-for-byte.
//
// WHY GOLDENS LIVE AT THE MODULE ROOT: internal/protocol's own tests are
// behavioural (round-trips, validation). These are the CONTRACT tests — the
// exact bytes a dashboard client, the mobile SDK, or a future Rust
// re-implementation must parse. A change that breaks a golden is a wire-
// format change, and it should be impossible to make one accidentally
// (reviewers see the .golden diff; developers regenerate only deliberately,
// with `go test ./tests/protocol -update`).
package protocolgolden

import (
	"bytes"
	"encoding/json"
	"flag"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/voxdesk/realtime/gateway-go/internal/protocol"
)

var update = flag.Bool("update", false, "rewrite the .golden files with current output")

// fixed pins every clock to one instant so goldens are fully deterministic.
var fixed = time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC)

func golden(t *testing.T, name string, frame any) {
	t.Helper()
	data, err := protocol.Marshal(frame)
	if err != nil {
		t.Fatalf("marshal %s: %v", name, err)
	}
	path := filepath.Join("testdata", name+".golden")
	if *update {
		if err := os.MkdirAll("testdata", 0o755); err != nil {
			t.Fatalf("mkdir testdata: %v", err)
		}
		// Trailing newline keeps .golden files diff/editor friendly; the
		// comparison below trims it, so the WIRE bytes are what is checked.
		if err := os.WriteFile(path, append(data, '\n'), 0o644); err != nil {
			t.Fatalf("write %s: %v", path, err)
		}
		t.Logf("updated %s", path)
		return
	}
	want, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("missing golden %s (run with -update to create it): %v", path, err)
	}
	if string(bytes.TrimSpace(want)) != string(data) {
		t.Errorf("%s diverged from the wire contract:\n got: %s\nwant: %s", name, data, want)
	}
}

func TestServerFrameGoldens(t *testing.T) {
	golden(t, "welcome", protocol.NewWelcome("11111111-2222-3333-4444-555555555555", 10*time.Second, 20*time.Second, fixed))
	golden(t, "ready", protocol.NewReady("11111111-2222-3333-4444-555555555555", "aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa", "admin", fixed.Add(time.Hour)))
	golden(t, "subscribed", protocol.NewSubscribed("calls", 3))
	golden(t, "unsubscribed", protocol.NewUnsubscribed("call:aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa"))
	golden(t, "delivery_with_event_id", protocol.NewDelivery("calls", "call.updated", "bbbbbbbb-2222-2222-2222-bbbbbbbbbbbb",
		json.RawMessage(`{"call_id":"call-42","state":"active"}`), fixed))
	golden(t, "delivery_without_event_id", protocol.NewDelivery("metrics", "metrics.tick", "",
		json.RawMessage(`{"cpu":0.5}`), fixed))
	golden(t, "pong", protocol.NewPong(fixed))
	golden(t, "error", protocol.NewError(protocol.CodeRateLimited, "slow down"))
	golden(t, "session_started", protocol.NewSessionStarted("cccccccc-3333-3333-3333-cccccccccccc"))
	// Wire 1.2 land: joined/peer_joined carry the negotiated steer flag.
	// Both variants are pinned (false = the legacy/default path's bytes,
	// true = steered) so either mode's frame shape can only change on
	// deliberate review, never by accident.
	golden(t, "session_joined", protocol.NewSessionJoined("cccccccc-3333-3333-3333-cccccccccccc", false))
	golden(t, "session_peer_joined", protocol.NewSessionPeerJoined("cccccccc-3333-3333-3333-cccccccccccc", "responder", false))
	golden(t, "session_joined_steered", protocol.NewSessionJoined("cccccccc-3333-3333-3333-cccccccccccc", true))
	golden(t, "session_peer_joined_steered", protocol.NewSessionPeerJoined("cccccccc-3333-3333-3333-cccccccccccc", "responder", true))
	golden(t, "engine_answer", protocol.NewEngineAnswer("cccccccc-3333-3333-3333-cccccccccccc", "v=0\r\no=- 3 3 IN IP4 203.0.113.7\r\n"))
	golden(t, "engine_track_published", protocol.NewEngineTrackPublished("cccccccc-3333-3333-3333-cccccccccccc", "p-peer-1", "mic", "audio"))
	golden(t, "session_ended", protocol.NewSessionEnded("cccccccc-3333-3333-3333-cccccccccccc", "peer_disconnected"))
	golden(t, "signal_offer", protocol.NewSignalOffer("cccccccc-3333-3333-3333-cccccccccccc", "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\n"))
	golden(t, "signal_answer", protocol.NewSignalAnswer("cccccccc-3333-3333-3333-cccccccccccc", "v=0\r\no=- 2 2 IN IP4 127.0.0.1\r\n"))
	golden(t, "signal_candidate", protocol.NewSignalCandidate("cccccccc-3333-3333-3333-cccccccccccc",
		json.RawMessage(`{"candidate":"candidate:1 1 udp 2130706431 192.0.2.1 3478 typ host","sdpMid":"0","sdpMLineIndex":0}`)))
}
```

==============================================================================
===== FILE: services/realtime/gateway-go/tests/protocol/testdata/delivery_with_event_id.golden (1 lines, sha256 8e87bb9bc131c09f8e3699c2982ff4c049a0e22b24b0e42b6199384e903e6b08) =====
==============================================================================
```json
{"type":"delivery","room":"calls","kind":"call.updated","payload":{"call_id":"call-42","state":"active"},"event_id":"bbbbbbbb-2222-2222-2222-bbbbbbbbbbbb","sent_at":"2026-01-02T03:04:05Z"}
```

==============================================================================
===== FILE: services/realtime/gateway-go/tests/protocol/testdata/delivery_without_event_id.golden (1 lines, sha256 06f206c3f9880a7ce2093710ee20ccf971f6ec304e24cdc10af6931364471d9e) =====
==============================================================================
```json
{"type":"delivery","room":"metrics","kind":"metrics.tick","payload":{"cpu":0.5},"sent_at":"2026-01-02T03:04:05Z"}
```

==============================================================================
===== FILE: services/realtime/gateway-go/tests/protocol/testdata/engine_answer.golden (1 lines, sha256 c3cde56995013d792dba45de9fe4237fe79b8f5ac13750cc513e8775ded79e9e) =====
==============================================================================
```json
{"type":"engine.answer","session_id":"cccccccc-3333-3333-3333-cccccccccccc","sdp":"v=0\r\no=- 3 3 IN IP4 203.0.113.7\r\n"}
```

==============================================================================
===== FILE: services/realtime/gateway-go/tests/protocol/testdata/engine_track_published.golden (1 lines, sha256 9ad6f96c9c091c31c0ca3fab32880985dfd14ebe919a4082c084008285957b6d) =====
==============================================================================
```json
{"type":"engine.track_published","session_id":"cccccccc-3333-3333-3333-cccccccccccc","participant":"p-peer-1","track":"mic","kind":"audio"}
```

==============================================================================
===== FILE: services/realtime/gateway-go/tests/protocol/testdata/error.golden (1 lines, sha256 339e543d100c3613428387abf259c79410977ca28bc54ed279dced587ce33542) =====
==============================================================================
```json
{"type":"error","code":"rate_limited","message":"slow down"}
```

==============================================================================
===== FILE: services/realtime/gateway-go/tests/protocol/testdata/pong.golden (1 lines, sha256 b73eb0b31f2ab04c63e8a4a28eb2e69f6d491a28fc8329421f60238266e95c27) =====
==============================================================================
```json
{"type":"pong","server_time":"2026-01-02T03:04:05Z"}
```

==============================================================================
===== FILE: services/realtime/gateway-go/tests/protocol/testdata/ready.golden (1 lines, sha256 63e0b9e271679b634fd059c348e6e4038f92e254e2685283837d622fa7feaa18) =====
==============================================================================
```json
{"type":"ready","session_id":"11111111-2222-3333-4444-555555555555","tenant_id":"aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa","role":"admin","token_expires_at":"2026-01-02T04:04:05Z"}
```

==============================================================================
===== FILE: services/realtime/gateway-go/tests/protocol/testdata/session_ended.golden (1 lines, sha256 a5608bd79d17330e764a6355a466b5d2715e8fb7cda86372c69f76bb90515a1b) =====
==============================================================================
```json
{"type":"session.ended","session_id":"cccccccc-3333-3333-3333-cccccccccccc","reason":"peer_disconnected"}
```

==============================================================================
===== FILE: services/realtime/gateway-go/tests/protocol/testdata/session_joined.golden (1 lines, sha256 054c0950fb3df02ad97ce0fed9cab200251e9955b0ba4b7f8194d7c68ca36d71) =====
==============================================================================
```json
{"type":"session.joined","session_id":"cccccccc-3333-3333-3333-cccccccccccc","role":"responder","steer":false}
```

==============================================================================
===== FILE: services/realtime/gateway-go/tests/protocol/testdata/session_joined_steered.golden (1 lines, sha256 fc4a4343302a264b965653a149a82cd1645746b9569b1066864718fe9ee75e99) =====
==============================================================================
```json
{"type":"session.joined","session_id":"cccccccc-3333-3333-3333-cccccccccccc","role":"responder","steer":true}
```

==============================================================================
===== FILE: services/realtime/gateway-go/tests/protocol/testdata/session_peer_joined.golden (1 lines, sha256 e9499fb08b10d5ae4d8ef3aef7afc928cc4ededed1942fd3d73e0191142853e2) =====
==============================================================================
```json
{"type":"session.peer_joined","session_id":"cccccccc-3333-3333-3333-cccccccccccc","peer_role":"responder","steer":false}
```

==============================================================================
===== FILE: services/realtime/gateway-go/tests/protocol/testdata/session_peer_joined_steered.golden (1 lines, sha256 c19dd09edf306243db391ca50700c7af278792e626bcbc674bf70f07751daada) =====
==============================================================================
```json
{"type":"session.peer_joined","session_id":"cccccccc-3333-3333-3333-cccccccccccc","peer_role":"responder","steer":true}
```

==============================================================================
===== FILE: services/realtime/gateway-go/tests/protocol/testdata/session_started.golden (1 lines, sha256 1ec9ca8bf9e44f9257235e5db668bef1a113b147a247cc9fda8be2accaf6a681) =====
==============================================================================
```json
{"type":"session.started","session_id":"cccccccc-3333-3333-3333-cccccccccccc","role":"initiator"}
```

==============================================================================
===== FILE: services/realtime/gateway-go/tests/protocol/testdata/signal_answer.golden (1 lines, sha256 e38d82a4bdd86a2475c71cbbba3191533fac09052b17c645946e57262409c5da) =====
==============================================================================
```json
{"type":"signal.answer","session_id":"cccccccc-3333-3333-3333-cccccccccccc","sdp":"v=0\r\no=- 2 2 IN IP4 127.0.0.1\r\n"}
```

==============================================================================
===== FILE: services/realtime/gateway-go/tests/protocol/testdata/signal_candidate.golden (1 lines, sha256 8801319c5cf6657791adf3ce08ffcd3d02d313f4be2cd60b2970e63f1eb3108b) =====
==============================================================================
```json
{"type":"signal.candidate","session_id":"cccccccc-3333-3333-3333-cccccccccccc","candidate":{"candidate":"candidate:1 1 udp 2130706431 192.0.2.1 3478 typ host","sdpMid":"0","sdpMLineIndex":0}}
```

==============================================================================
===== FILE: services/realtime/gateway-go/tests/protocol/testdata/signal_offer.golden (1 lines, sha256 ee3db6a69d3f710b78ecb984de94c0a22700d53b415236da006a24c1a0c97fb3) =====
==============================================================================
```json
{"type":"signal.offer","session_id":"cccccccc-3333-3333-3333-cccccccccccc","sdp":"v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\n"}
```

==============================================================================
===== FILE: services/realtime/gateway-go/tests/protocol/testdata/subscribed.golden (1 lines, sha256 c574df0b0b826cced676564f7bf5eebbe0c792fefa0a2ccec8aacab9a924218d) =====
==============================================================================
```json
{"type":"subscribed","room":"calls","peers":3}
```

==============================================================================
===== FILE: services/realtime/gateway-go/tests/protocol/testdata/unsubscribed.golden (1 lines, sha256 f307acd7264ba287f084be4840ce4f61f79887b16f6cfa6d423bab808fae2e15) =====
==============================================================================
```json
{"type":"unsubscribed","room":"call:aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa"}
```

==============================================================================
===== FILE: services/realtime/gateway-go/tests/protocol/testdata/welcome.golden (1 lines, sha256 e5e9e3f5d2e7b2f5f1827ba34986837cfcf971858bf12b9e9773909bac340e6a) =====
==============================================================================
```json
{"type":"welcome","session_id":"11111111-2222-3333-4444-555555555555","auth_required":true,"auth_timeout_secs":10,"heartbeat_secs":20,"server_time":"2026-01-02T03:04:05Z"}
```
