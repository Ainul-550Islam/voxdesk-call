# VoxDesk — বাদ পড়া ফাইলের পূর্ণ অডিট ও সম্পূর্ণ ফাইল-ব্যাচ

**তারিখ:** 2026-09-21 · **HEAD:** `13ea4e0` · **স্কোপ:** গোটা রিপো, ফাইল-বাই-ফাইল, বাদ-পড়া কিছু নেই

---

## ০. এক নজরে (বাংলা সারসংক্ষেপ)

আপনার রিপোতে আসলে **দুটো কপি** ছিল:

| ট্রি | কী আছে | অবস্থা |
|---|---|---|
| রুট (`/`) | অ্যাপ, ড্যাশবোর্ড, ডকুমেন্ট, ডেলিভারেবল | পুরোনো (১৪ সেপ্টেম্বর) |
| ভেতরের কপি (`voxdesk/`) | একই প্রজেক্ট **+ পুরো রিয়েলটাইম স্ট্যাক** (Prompt-1/Prompt-2, ১৭–১৯ সেপ্টেম্বর) | নতুন |

ফলে **রুট ট্রি থেকে ২১৫টা ফাইল হারিয়ে গিয়েছিল** — ২০৫টা ফাইল শুধুই ভেতরের কপিতে ছিল, আর ১০টা ফাইলের নতুন ভার্সন রুটে ছিল না। মোট **৩৩,৮০৮ লাইন**। সবচেয়ে বড় ক্ষতি: `services/realtime/gateway-go/` (Go WebSocket edge, ১১০ ফাইল), `services/realtime/media-engine-rs/` (Rust SFU, ৮০ ফাইল), `app/realtime/` — যার কারণে রুটে `import app.realtime` **একেবারে ফেল করত** এবং `docker compose` রিয়েলটাইম গেটওয়ে ছাড়াই চলত।

তার উপর আরও তিনটা ফাঁক ছিল:

1. **Batch 01-এর ৩টা রাউট মডিউল কোথাও রেজিস্টার করা ছিল না** — কোড নিজেই ডকস্ট্রিং-এ লিখে রেখেছিল "reported as an integration dependency"। অর্থাৎ `/api/agents`, `/api/workflows`, `/api/campaigns` ফাইলে ছিল, অ্যাপে ছিল না।
2. **তিনটা সার্ভিসের কোনো HTTP সারফেসই ছিল না** — `automation_service`, `notification_service`, `inbox_service` (ফাইল = মিসিং)।
3. **ডেলিভারেবল ডকুমেন্ট নিজেই ড্রিফট করেছিল** — `STEP17_BATCH01_DELIVERABLE.md`-এ `tests/test_enterprise_batch01.py`-এর পুরোনো রিভিশন ছাপা আছে (৫টা জায়গায় কোড আলাদা)।

**এই টার্নে যা করা হয়েছে**

- ✅ ভেতরের কপি থেকে **২৯৪টা ফাইল** রুটে আনা (২১৫ ফার্স্ট-পার্টি + ৭৯ ভেন্ডরড), বাইট-বাই-বাইট যাচাই — **০ mismatch**।
- ✅ মিসিং **৩টা রাউট মডিউল** নতুন লেখা (automation / notification / inbox)।
- ✅ `app/main.py`-তে **৬টা এন্টারপ্রাইজ রাউটার** রেজিস্টার (Batch 01-এর ফাঁক বন্ধ)।
- ✅ নতুন টেস্ট লিখতে গিয়ে আপনার কোডে **১টা আসল বাগ** ধরা পড়ল ও ফিক্স হলো (`inbox_service._channel_of` — `web`/`crm` চ্যানেল কখনোই সেট হত না)।
- ✅ **৪৩টা নতুন টেস্ট** লেখা — **সব পাস**; গোটা স্যুট: **২৪৯০ passed, ৪৩ skipped** (২:২৩ মিনিট)।
- ✅ **২২১টা ফাইলের ফুল কনটেন্ট** ৪টা ব্যাচ ফাইলে — প্রতিটা ব্লকের sha256 ডিস্কের ফাইলের সাথে যাচাই করা (**২২১/২২১ identical**)। কোনো "… existing code …" নেই, কোনো লাইন বাদ নেই।

---

## ১. যেভাবে চেক করা হয়েছে (method + প্রমাণ)

কোনো ফাইল "হাতে" দেখে তালিকা বানানো হয়নি — প্রতিটা ধাপ স্ক্রিপ্ট দিয়ে, হ্যাশ দিয়ে যাচাই করা:

```bash
# ১) দুটো ট্রির প্রতিটা ফাইলের sha256 বের করে তুলনা (ক্যাশ/ভেন্ডর বাদ দিয়ে)
python3 /tmp/plan_merge.py          # -> ADD 284 | REPLACE 10

# ২) কোনো ফাইলের অর্ডার-ভিন্নতা বা লাইন-লস নেই কিনা যাচাই (REPLACE-লিস্টের ১০টা ফাইল)
#    difflib SequenceMatcher দিয়ে: nested-এ না-থাকা/রুট-only লাইন আছে কি না

# ৩) প্রতিটা পাইথন ফাইল AST পার্স + ইমপোর্ট রেজোলিউশন (missing module আছে কি না)
#    ৭২৭ ফাইল স্ক্যান -> 0 syntax error, missing module: শুধু app.realtime (ঠিক হয়েছে)

# ৪) deploy/CI ফাইল কোন কোন পাথ রেফার করে, সেগুলো আছে কি না
#    (docker-compose build context, Caddyfile, workflows)

# ৫) মার্জের পর: ভেতরের প্রতিটা ফাইল রুটে হুবহু আছে কি না
#    nested 1001 files, mismatched/missing in root: 0

# ৬) মার্জ করা ফাইলগুলোর কনটেন্ট ব্লক আবার এক্সট্রাক্ট করে sha256 মিলিয়ে দেখা
python3 /home/user/deliverables/verify_batches.py
#    -> 221 file blocks verified; identical=221; mismatched=0
```

---

## ২. ক্যাটাগরি A — যে ২০৫টা ফাইল শুধুই ভেতরের কপিতে ছিল

মোট **২০৫ ফাইল / ৩১,৫৩৬ লাইন** (ভেন্ডরড থার্ড-পার্টি বাদ দিয়ে)।

| area | files | lines |
|---|---:|---:|
| `.dockerignore` | 1 | 44 |
| `.env.example` | 1 | 73 |
| `.env.staging.example` | 1 | 68 |
| `.github/workflows` | 3 | 216 |
| `.gitignore` | 1 | 26 |
| `Caddyfile` | 1 | 92 |
| `PROMPT1-FINAL-REPORT.md` | 1 | 150 |
| `PROMPT2-DESIGN.md` | 1 | 158 |
| `app/core` | 1 | 681 |
| `app/realtime` | 3 | 269 |
| `app/telephony` | 1 | 581 |
| `dashboard/src/lib` | 2 | 652 |
| `dashboard/tests` | 1 | 308 |
| `dashboard/vite.config.js` | 1 | 36 |
| `docker-compose.prod.yml` | 1 | 249 |
| `docs/REALTIME.md` | 1 | 186 |
| `observability/prometheus.yml` | 1 | 54 |
| `services/README.md` | 1 | 95 |
| `services/realtime/gateway-go` | 110 | 14428 |
| `services/realtime/media-engine-rs` | 80 | 14987 |
| `tests/test_realtime_publisher.py` | 1 | 282 |
| `tests/test_realtime_wiring.py` | 1 | 173 |

এগুলোর মধ্যে **সবচেয়ে জরুরি**:

- `services/realtime/gateway-go/**` — পাবলিক WebSocket edge (JWT hello, tenant-pinned rooms, ingest fan-out, WebRTC signaling relay, golden wire fixtures, `-tags it` live lanes, ১০৭ টেস্ট)। **১১০ ফাইল / ১৪,৪২৮ লাইন**।
- `services/realtime/media-engine-rs/**` — Rust SFU: protocol, signaling, ICE/STUN, DTLS-SRTP, routing, engine, media-engine/loadgen/sdp-tool বাইনারি, pipeline+DTLS+pion ইন্টারঅপ টেস্ট। **৮০ ফাইল / ১৪,৯৮৭ লাইন** (এর সাথে ৭৯টা ভেন্ডরড `dimpl` ফাইল সিংক করা হয়েছে, কিন্তু সেগুলো থার্ড-পার্টি তাই এই ব্যাচে রিপ্রোডিউস করা হয়নি — ব্যাচ ফাইলে নাম সহ তালিকা আছে)।
- `app/realtime/{__init__,events,publisher}.py` — API → gateway পাবলিশার (যেটা `twilio_handler.py` ইমপোর্ট করে; রুটে না থাকায় অ্যাপ ইমপোর্ট ক্র্যাশ করত)।
- `tests/test_realtime_publisher.py`, `tests/test_realtime_wiring.py`, `dashboard/src/lib/realtime.js`, `dashboard/tests/realtime.test.js`, `docs/REALTIME.md`।
- `.github/workflows/{ci,security-scan,real-integrations}.yml`, `.dockerignore`, `.env.staging.example`, `PROMPT1-FINAL-REPORT.md`, `PROMPT2-DESIGN.md`।

## ৩. ক্যাটাগরি B — যে ১০টা ফাইল ভেতরের কপিতে নতুন (রুটে পুরোনো)

প্রতিটার জন্য লাইন-বাই-লাইন যাচাই করে দেখা হয়েছে nested ভার্সন আসলে **রুটের সুপারসেট** (শুধু রিয়েলটাইম সংযোজন; রুট-অনলি কোনো লজিক হারায়নি — শুধু ৪টা কমেন্ট/লাইন নতুন করে লেখা হয়েছে, প্রমাণ স্ক্রিপ্টে আছে)।

| file | lines (nested) | রুটে যা ছিল না |
|---|---:|---|
| `app/core/config.py` | 681 | `REALTIME_GATEWAY_URL`, `REALTIME_GATEWAY_INGEST_SECRET`, `REALTIME_PUBLISH_TIMEOUT_SECONDS`, `realtime_enabled`, বুট-ভ্যালিডেশন (হাফ-কনফিগ = বুট রিফিউজ) |
| `app/telephony/twilio_handler.py` | 581 | `call.created` / `call.updated` / `call.transfer` ইভেন্ট এমিট (idempotent `created_call` ও `changed` গার্ড সহ) |
| `docker-compose.prod.yml` | 249 | `realtime-gateway` (Go, 127.0.0.1:8790) + `media-engine` (Rust, UDP 5000 / কন্ট্রোল 9001) সার্ভিস, হেলথচেক, ডিপেন্ডেন্সি, `REALTIME_GATEWAY_*` env, Caddy depends_on |
| `Caddyfile` | 92 | `/realtime/ws` → `realtime-gateway:8790` রুট + বাকি সব `/` → `api:8000` |
| `.env.example` | 73 | `REALTIME_*`, `MEDIA_ENGINE_*` ব্লক |
| `.gitignore` | 26 | `backups/`, `*.dump`, `*.pem`, `*.key`, `.netrc`, IDE ফাইল, গেটওয়ে বাইনারি |
| `observability/prometheus.yml` | 54 | `voxdesk-realtime-gateway` + `voxdesk-media-engine` স্ক্র্যাপ জব |
| `services/README.md` | 95 | `services/realtime/gateway-go` সেকশন + ১০৭ টেস্টের স্টেটাস |
| `dashboard/src/lib/api.js` | 385 | `getAccessToken()`, `refreshAccessToken()` (WebSocket hello/১০০৮ রিকানেক্টের জন্য) |
| `dashboard/vite.config.js` | 36 | `/realtime/ws` প্রোক্সি (ws: true) — প্রড Caddy রুলের মিরর |

## ৪. ক্যাটাগরি C — ৩টা রাউট মডিউল কোথাও রেজিস্টারই করা ছিল না

প্রমাণ (ফাইলের নিজের ডকস্ট্রিং, Batch 01):

> "Route registration (`app/main.py`) is outside the allowed file set for this batch and **is reported as an integration dependency**."
> — `app/api/agent_management_routes.py`, `app/api/workflow_routes.py`, `app/api/campaign_routes.py`

`app/main.py`-তে ছিল মাত্র ১৫টা `include_router`; এন্টারপ্রাইজ রাউটার শূন্য। এখন ৬টা যোগ হয়েছে এবং টেস্ট দিয়ে পিন করা (`tests/test_enterprise_batch02.py::TestRouterWiring`)। রাউট সংখ্যা আগে **১০৭**, এখন **১৮৯** — `/api/agents`, `/api/workflows`, `/api/campaigns`, `/api/automations`, `/api/notifications`, `/api/inbox` সব লাইভ।

## ৫. ক্যাটাগরি D — যে তিনটা সার্ভিসের API ফাইলই অস্তিত্বে ছিল না (নতুন লেখা)

| নতুন ফাইল | lines | কী দেয় |
|---|---:|---|
| `app/api/automation_routes.py` | 494 | `/api/automations` — events/actions ভোকাবুলারি, create (idempotent), list/get/put, enable/disable, dry-run evaluate, run (dedup+cooldown সার্ভিসের হাতেই), runs, stats, dedupe-key |
| `app/api/notification_routes.py` | 511 | `/api/notifications` — template CRUD + render, preferences/check (quiet hours ব্যাখ্যা সহ), create (dedupe), list/get, read/unread, deliver, retry, states/summary |
| `app/api/inbox_routes.py` | 485 | `/api/inbox` — threads list/open/get, messages, notes, assign, priority, tags, read/unread, escalate, close/reopen, counts, unread-counts, identity |

সবগুলোতে Batch 01-এর নিয়ম হুবহু মানা হয়েছে: টেন্যান্ট কখনোই প্যারামিটার নয় (টোকেন থেকে), `extra="forbid"` মডেল, রেসপন্স অ্যালাওলিস্ট, ফোন/ইমেইল **মাস্কড**, কোনো ডায়ালিং নেই, RBAC ব্যাখ্যা সহ (notification deliver/retry = `CAMPAIGN_RUN` কারণ SMS-এ টাকা লাগে)।

## ৬. ক্যাটাগরি E — ডেলিভারেবল ডকুমেন্টের নিজের ড্রিফট

`STEP17_BATCH01_DELIVERABLE.md`-এর ২০টা ফাইলের মধ্যে **১৯টা ডিস্কের সাথে বাইট-হুবহু**; কিন্তু `tests/test_enterprise_batch01.py` পুরোনো রিভিশন (৯২৮ বনাম ডিস্কে ৯৩৬ লাইন)। ডিস্কের ভার্সনই নতুন — ৫টা জায়গায় পার্থক্য:

1. `ToolConfig` ইমপোর্ট সরানো হয়েছে।
2. `test_validation_rejects_unknown_tool` ও `test_config_hash_stable_and_distinct` সরিয়ে `test_id_stable_across_content_edits`।
3. `test_publish_transition_rules`-এ `PUBLISHED→PUBLISHED` যোগ, শেষে `RETIRED→PUBLISHED`।
4. `TestConversationService.test_start_and_project` যোগ; controlled-actions টেস্টে `eval/exec/import/os.system` লুপ; tautological assert সরানো।
5. হেল্পার ফাংশন দুটো `_campaign()`-এর উপরে সরানো, ডুপ্লিকেট লাইন সরানো, টেস্ট রিনেম।

**নিয়ম:** এই ফাইলের জন্য ডিস্কের ভার্সনই সোর্স অব ট্রুথ (এবং Batch 02/03/04 ব্যাচ ফাইলগুলোর প্রতিটা ব্লক sha256 দিয়ে ডিস্কের সাথে বাঁধা)।

## ৭. ক্যাটাগরি F — নতুন টেস্ট লিখতে গিয়ে ধরা পড়া আসল বাগ

`app/services/inbox_service.py::_channel_of` — চেকের ক্রম ভুল ছিল: `chat:` প্রিফিক্স চেক `chat:web` / `chat:crm`-এর আগে বসে থাকায় **WEB আর CRM চ্যানেল কখনোই ফেরত আসত না** (সব web থ্রেড "sms" হিসেবে দেখাত, আর থ্রেড আইডি ভুল চ্যানেল হ্যাশ করত → ড্যাশবোর্ডের ইনবক্স ফিল্টার ভুল)। ফিক্স: exact match আগে, প্রিফিক্স পরে (সেমান্টিক্স অপরিবর্তিত)। প্রমাণ: `tests/test_enterprise_batch02.py::TestInboxApi::test_open_thread_masks_participants` — ফিক্সের আগে `assert 'sms' == 'web'` ফেল করছিল।

## ৮. ভেরিফিকেশন (সংখ্যা)

| যাচাই | ফল |
|---|---|
| nested → root সিংক | **১০০১ nested ফাইল, ০ missing/mismatched** |
| ব্যাচ ফাইলের ব্লক ↔ ডিস্ক (sha256) | **২২১/২২১ identical, ০ mismatched** (batch01-এর ১৯/২০ আলাদা করে যাচাই) |
| নতুন টেস্ট | `tests/test_enterprise_batch02.py` → **৪৩ passed** |
| পুরোনো এন্টারপ্রাইজ টেস্ট | `tests/test_enterprise_batch01.py` → **৮০ passed** |
| কনট্রাক্ট/ডিপ্লয়মেন্ট/কনফিগ | `test_api_contract + test_deployment + test_config` → একসাথে **১২৮ passed** |
| সিনট্যাক্স/AST | ৭২৭ পাইথন ফাইল — **০ syntax error** |
| **সম্পূর্ণ টেস্ট স্যুট** | `python3 -m pytest tests -q` → **২৪৯০ passed, ৪৩ skipped** (১৪৩ সেকেন্ড) |
| deploy রেফারেন্স অডিট | build context + রেফার করা পাথ সব আছে (`dashboard/dist` ছাড়া — সেটা বিল্ড আউটপুট) |
| **ফ্রেশ ক্লোনে প্যাচ টেস্ট** | `git clone` → `git apply voxdesk-code-sync.patch` → **clean apply**, নতুন টেস্ট **৪৩ passed**, নতুন রাউট মডিউল ইমপোর্ট OK |

## ৯. সৎভাবে যা বাকি (gaps — লুকানো হয়নি)

- **Persistence:** automation / notification / inbox এখনো ইন-প্রসেস রেজিস্ট্রি (সার্ভিসগুলোর নিজের ডকস্ট্রিং-এ লেখা আছে — DB টেবিল + অ্যালেমবিক মাইগ্রেশন লাগবে)। আমি ইচ্ছে করে সেটা করিনি: এটা ডেটা-মডেল সিদ্ধান্ত, Batch 01-এর ডিজাইন পরিবর্তন, আর আপনার সম্মতি ছাড়া করা উচিত নয়।
- **WEBHOOK delivery worker** ও **EMAIL provider** — ঘোষিত-কিন্তু-বন্ধ (service নিজেই "suppressed"/"pending" রিপোর্ট করে)।
- **Inbox per-assignee scoping** নেই → তাই আমার রুটগুলোতে mutation = `CALL_READ_ALL` (ম্যানেজার+)। এজেন্টরা রিপ্লাই দিতে পারবে তখনই যখন per-assignee পারমিশন মডেল আসবে।
- **Go/Rust গেট:** এই স্যান্ডবক্সে `cargo`/`go` টুলচেইন নেই, তাই `cargo test`/`go test -race` এখানে রি-রান করা যায়নি — কিন্তু ওই ফাইলগুলোর কোনো বাইট আমি বদলাইনি (শুধু সিংক করেছি), আর Prompt-1 রিপোর্টে ১০৯ Rust + ১০৭ Go টেস্টের ফল রেকর্ড করা আছে।
- **ড্যাশবোর্ড build** (`npm run build`, ৩৬২ ফ্রন্টএন্ড টেস্ট) এখানে চালানো হয়নি (নোড আছে কিন্তু ডিপেন্ডেন্সি ইনস্টল ~ভারী এবং কাজটার সাথে সম্পর্ক নেই)।

## ১০. রিপো-হাইজিন সতর্কতা (এগুলো "মিসিং" না, কিন্তু বিপজ্জনক)

| সমস্যা | কেন গুরুত্বপূর্ণ | কমান্ড |
|---|---|---|
| `backups/voxdesk-*.dump` (৪টা, ৯০০KB) গিটে কমিট করা | **পুরো ডেটাবেস ডাম্প — কাস্টমার কল, ট্রান্সক্রিপ্ট, ফোন নম্বর (PII) সহ**। `.gitignore`-এ `*.dump` আছে কিন্তু যেহেতু আগেই ট্র্যাক করা, ignore কাজ করে না | `git rm --cached backups/*.dump && git commit -m "remove committed DB dumps"` (+ Git-এ থাকা ইতিহাস পরিষ্কার করা: `git filter-repo`) |
| `voxdesk/` (ভেতরের ডুপ্লিকেট ট্রি, ১০০১ ফাইল) | এটাই মূল কারণ যাতে ফাইল "হারিয়ে" গিয়েছিল; দুটো সোর্স অব ট্রুথ থাকলে আবার একই ভুল হবে | (আমার সুপারিশ) sync এখন প্রমাণিত, তাই: `git rm -r voxdesk` — হিস্টরিতে (কমিট `13ea4e0`) সব সেফ থাকে, দরকার হলে ফেরত আনা যাবে |
| `toolchains/go/**` (সম্পূর্ণ Go টুলচেইন), `go/`, `go-work/`, `gop/` | রিপো ~৯,০০০ ফাইল/শত মেগাবাইট ফুলে গেছে; এগুলোর মধ্যে প্রথম-পার্টি কোড নেই | `git rm -r --cached toolchains go go-work gop && echo "toolchains/" >> .gitignore` (লোকাল ফাইল থাকবে, গিট থেকে বাদ) |
| `dashboard/dist` রেফারেন্স | বিল্ড আউটপুট — ঠিক আছে, ডকারে তৈরি হয় | — |

## ১১. প্যাচ কীভাবে লাগাবেন

```bash
git clone https://github.com/Ainul-550Islam/voxdesk.git && cd voxdesk
git apply --check /path/to/voxdesk-code-sync.patch    # শুধু যাচাই
git apply /path/to/voxdesk-code-sync.patch            # প্রয়োগ
```

প্যাচে **৩০০ ফাইল** (২৮৮ নতুন, ১২ পরিবর্তিত — ৪৯,৮৩৭ insertions, ১১ deletions)। অথবা ব্যাচ ফাইল থেকে কপি করুন — প্রতিটা ফাইলের ফুল কনটেন্ট ওখানেই আছে।

---

# পরিশিষ্ট A — প্রতিটা ব্যাচের পূর্ণ ফাইল-তালিকা (path · lines · sha256)

## Batch 02 — মিসিং HTTP সারফেস + ওয়্যারিং (6 ফাইল)

| file | lines | sha256 (first 16) |
|---|---:|---|
| `app/api/automation_routes.py` | 494 | `fe24361813c0cb3d` |
| `app/api/notification_routes.py` | 475 | `4e0206fa9cffb877` |
| `app/api/inbox_routes.py` | 521 | `7c5f4b24972b3d40` |
| `app/main.py` | 258 | `915170c43fb866c4` |
| `app/services/inbox_service.py` | 425 | `1f560aadfc7a05d4` |
| `tests/test_enterprise_batch02.py` | 675 | `6ea94d97d5b23cd9` |

## Batch 03 — রিয়েলটাইম অ্যাপ্লিকেশন সারফেস + ডিপ্লয় ওয়্যারিং (25 ফাইল)

| file | lines | sha256 (first 16) |
|---|---:|---|
| `app/realtime/__init__.py` | 27 | `85d73c7b0d0675a5` |
| `app/realtime/events.py` | 131 | `c187832bf58b827d` |
| `app/realtime/publisher.py` | 111 | `a91659a80d4d8203` |
| `app/core/config.py` | 681 | `ef157954b2066d06` |
| `app/telephony/twilio_handler.py` | 581 | `1de09f5d750c3019` |
| `tests/test_realtime_publisher.py` | 282 | `4c3f71acc630dd1d` |
| `tests/test_realtime_wiring.py` | 173 | `4fe3c2a9931c7f14` |
| `dashboard/src/lib/realtime.js` | 267 | `ec94f4bdfc9793fc` |
| `dashboard/src/lib/api.js` | 385 | `fd955642d171d785` |
| `dashboard/vite.config.js` | 36 | `0b17fdbff1ae8fdb` |
| `dashboard/tests/realtime.test.js` | 308 | `d580f47fd1f8707a` |
| `docs/REALTIME.md` | 186 | `8138df0b105abe6b` |
| `.github/workflows/ci.yml` | 112 | `bdff1f28ad3760b4` |
| `.github/workflows/security-scan.yml` | 57 | `f69b11c547a003f2` |
| `.github/workflows/real-integrations.yml` | 47 | `28f7484e9e593cf8` |
| `.dockerignore` | 44 | `dc2981ba41f8f0bb` |
| `.env.staging.example` | 68 | `2fa862b14aab14b4` |
| `.env.example` | 73 | `abd49f9a5a16e85d` |
| `.gitignore` | 26 | `7163021fca1bdb4d` |
| `Caddyfile` | 92 | `00c9c21a5a55ba2e` |
| `docker-compose.prod.yml` | 249 | `40a96ac5826f172a` |
| `observability/prometheus.yml` | 54 | `c483f326f5b5b027` |
| `services/README.md` | 95 | `b4684e94ff6c6ef3` |
| `PROMPT1-FINAL-REPORT.md` | 150 | `f1d4384ac3f7e65d` |
| `PROMPT2-DESIGN.md` | 158 | `85ef3c1ac7f86505` |

## Batch 04A — `services/realtime/gateway-go` (110 ফাইল)

| file | lines | sha256 (first 16) |
|---|---:|---|
| `services/realtime/gateway-go/Dockerfile` | 50 | `1ef1fd3cc8d005a6` |
| `services/realtime/gateway-go/README.md` | 274 | `4c29cd97e5527aa0` |
| `services/realtime/gateway-go/cmd/gateway/main.go` | 160 | `cfc09a211b0a56ef` |
| `services/realtime/gateway-go/go.mod` | 33 | `601acbb0c4713385` |
| `services/realtime/gateway-go/go.sum` | 52 | `ea176a35f37a1829` |
| `services/realtime/gateway-go/internal/auth/claims.go` | 144 | `a518116dba6c5942` |
| `services/realtime/gateway-go/internal/auth/jwt.go` | 123 | `293a3cc8f430fa53` |
| `services/realtime/gateway-go/internal/auth/jwt_test.go` | 232 | `c5dc4b7807e52fab` |
| `services/realtime/gateway-go/internal/auth/middleware.go` | 35 | `b41587dcc659c018` |
| `services/realtime/gateway-go/internal/backpressure/policy.go` | 45 | `b3e236375c43c40b` |
| `services/realtime/gateway-go/internal/backpressure/queue.go` | 84 | `3eaca8dafbfa6717` |
| `services/realtime/gateway-go/internal/backpressure/queue_test.go` | 99 | `2918af65ef0d9267` |
| `services/realtime/gateway-go/internal/broker/broker.go` | 92 | `aae6b7c7d19f1937` |
| `services/realtime/gateway-go/internal/broker/brokertest/fake.go` | 228 | `8d0fe560c063d343` |
| `services/realtime/gateway-go/internal/broker/memory.go` | 127 | `2b858a273775a362` |
| `services/realtime/gateway-go/internal/broker/memory_test.go` | 92 | `7edde74d87a561d0` |
| `services/realtime/gateway-go/internal/broker/redis.go` | 649 | `7ac3212115615fea` |
| `services/realtime/gateway-go/internal/broker/redis_test.go` | 227 | `61cb97e72df32adb` |
| `services/realtime/gateway-go/internal/config/config.go` | 345 | `6c3906792a8254b5` |
| `services/realtime/gateway-go/internal/config/config_broker_test.go` | 98 | `bea1375500bfd1ae` |
| `services/realtime/gateway-go/internal/config/config_engine_test.go` | 124 | `9045d0018d745f1c` |
| `services/realtime/gateway-go/internal/config/config_test.go` | 191 | `96193d0b2703315c` |
| `services/realtime/gateway-go/internal/engineclient/client.go` | 468 | `d68882436fe2d38a` |
| `services/realtime/gateway-go/internal/engineclient/client_test.go` | 217 | `d1eebc8b9eaa03eb` |
| `services/realtime/gateway-go/internal/engineclient/it_live_test.go` | 160 | `f852cb460cca0dc0` |
| `services/realtime/gateway-go/internal/engineclient/it_pion_gcm_test.go` | 165 | `261e39695e1487ff` |
| `services/realtime/gateway-go/internal/engineclient/it_pion_test.go` | 447 | `44b53907a88b91d0` |
| `services/realtime/gateway-go/internal/hub/hub.go` | 277 | `08b5afb6a6d77fb1` |
| `services/realtime/gateway-go/internal/hub/hub_test.go` | 262 | `7d24b459b15a7448` |
| `services/realtime/gateway-go/internal/idempotency/store.go` | 101 | `5216274c0a02c718` |
| `services/realtime/gateway-go/internal/idempotency/store_test.go` | 90 | `474f73f262daafb2` |
| `services/realtime/gateway-go/internal/observability/events.go` | 103 | `97dad1792a3df877` |
| `services/realtime/gateway-go/internal/observability/logging.go` | 154 | `6dbb1318b22d23c1` |
| `services/realtime/gateway-go/internal/observability/metrics/metrics.go` | 205 | `7267f51c340222d6` |
| `services/realtime/gateway-go/internal/observability/observability_test.go` | 173 | `0d9b4488a311a84b` |
| `services/realtime/gateway-go/internal/observability/tracing.go` | 78 | `911ebb33d972c00a` |
| `services/realtime/gateway-go/internal/presence/events.go` | 60 | `4d31458b892f5cb6` |
| `services/realtime/gateway-go/internal/presence/presence.go` | 175 | `0a4c95a952be7f5d` |
| `services/realtime/gateway-go/internal/presence/presence_test.go` | 147 | `8a9b06461c560f59` |
| `services/realtime/gateway-go/internal/presence/registry.go` | 44 | `b3bd7636d434976b` |
| `services/realtime/gateway-go/internal/protocol/codec.go` | 128 | `bb0482411d915960` |
| `services/realtime/gateway-go/internal/protocol/codec_signaling_test.go` | 213 | `6aec1b8c5c3a57ff` |
| `services/realtime/gateway-go/internal/protocol/envelope.go` | 53 | `d523fccd42cc7342` |
| `services/realtime/gateway-go/internal/protocol/error.go` | 70 | `f904cd5180d119ca` |
| `services/realtime/gateway-go/internal/protocol/message.go` | 346 | `6541349612df82fe` |
| `services/realtime/gateway-go/internal/protocol/protocol_test.go` | 120 | `cdc387b4fbc2d04f` |
| `services/realtime/gateway-go/internal/ratelimit/bucket.go` | 75 | `7320c4b3273f1d73` |
| `services/realtime/gateway-go/internal/ratelimit/limiter.go` | 65 | `90512e7d05022218` |
| `services/realtime/gateway-go/internal/ratelimit/limiter_test.go` | 127 | `627c914db76925dc` |
| `services/realtime/gateway-go/internal/ratelimit/policy.go` | 74 | `580cf3911f2b28fa` |
| `services/realtime/gateway-go/internal/server/engine_e2e_test.go` | 430 | `c07db81d975d5e4c` |
| `services/realtime/gateway-go/internal/server/health.go` | 75 | `e4ed0ba438ecc716` |
| `services/realtime/gateway-go/internal/server/http.go` | 77 | `016d3f4cb4bda23f` |
| `services/realtime/gateway-go/internal/server/ingest.go` | 239 | `4ffd49ee0fc597a5` |
| `services/realtime/gateway-go/internal/server/ingest_test.go` | 332 | `d36870cd8909a6a6` |
| `services/realtime/gateway-go/internal/server/metrics.go` | 42 | `410daa1b71e33c3f` |
| `services/realtime/gateway-go/internal/server/redis_e2e_test.go` | 183 | `15eb73b94256189a` |
| `services/realtime/gateway-go/internal/server/server.go` | 192 | `4dbebe217c5fcace` |
| `services/realtime/gateway-go/internal/server/server_e2e_test.go` | 268 | `0c62d4193b88c86e` |
| `services/realtime/gateway-go/internal/server/signaling_e2e_test.go` | 194 | `761b3837f53d86ab` |
| `services/realtime/gateway-go/internal/server/steer_e2e_test.go` | 302 | `2bf381f7612096a9` |
| `services/realtime/gateway-go/internal/session/lifecycle.go` | 53 | `6126737697cc395d` |
| `services/realtime/gateway-go/internal/session/lifecycle_test.go` | 65 | `a52734e47e3e2421` |
| `services/realtime/gateway-go/internal/session/manager.go` | 301 | `ec34f04af751418a` |
| `services/realtime/gateway-go/internal/session/manager_test.go` | 321 | `e5d49f822c972d1b` |
| `services/realtime/gateway-go/internal/session/registry.go` | 104 | `18ff756aa8b01195` |
| `services/realtime/gateway-go/internal/session/registry_test.go` | 75 | `ffdfaf43d11c7b2f` |
| `services/realtime/gateway-go/internal/session/session.go` | 119 | `1c7f086db57ac225` |
| `services/realtime/gateway-go/internal/session/state.go` | 52 | `b53c3d13d5bc0280` |
| `services/realtime/gateway-go/internal/session/state_test.go` | 85 | `e3698c7b68b38236` |
| `services/realtime/gateway-go/internal/shutdown/graceful.go` | 57 | `27e976f1eb371a53` |
| `services/realtime/gateway-go/internal/shutdown/graceful_test.go` | 86 | `747205076ad8add9` |
| `services/realtime/gateway-go/internal/signaling/answer.go` | 36 | `5af06d52c055c2b6` |
| `services/realtime/gateway-go/internal/signaling/candidate.go` | 29 | `65aceb96f8bf5f3c` |
| `services/realtime/gateway-go/internal/signaling/events.go` | 244 | `4a2ef908759f50e5` |
| `services/realtime/gateway-go/internal/signaling/offer.go` | 37 | `4ce02e974f680b87` |
| `services/realtime/gateway-go/internal/signaling/router.go` | 245 | `1923138f85c21613` |
| `services/realtime/gateway-go/internal/signaling/router_test.go` | 336 | `4b1f9e87e3b22ddc` |
| `services/realtime/gateway-go/internal/signaling/steer.go` | 220 | `b35cb068a5653a7d` |
| `services/realtime/gateway-go/internal/validate/validate.go` | 107 | `09aa6bf80507dc00` |
| `services/realtime/gateway-go/internal/validate/validate_test.go` | 95 | `044afe2d20e8b136` |
| `services/realtime/gateway-go/internal/websocket/close.go` | 52 | `4499abc2f6048c08` |
| `services/realtime/gateway-go/internal/websocket/connection.go` | 301 | `1b3bd3b391ea507f` |
| `services/realtime/gateway-go/internal/websocket/heartbeat.go` | 90 | `fad162458c694039` |
| `services/realtime/gateway-go/internal/websocket/reader.go` | 183 | `8762055909255a49` |
| `services/realtime/gateway-go/internal/websocket/upgrader.go` | 78 | `f4c1389166f205ab` |
| `services/realtime/gateway-go/internal/websocket/writer.go` | 95 | `ed7d8c0e0edcb174` |
| `services/realtime/gateway-go/tests/README.md` | 16 | `acca97c244433972` |
| `services/realtime/gateway-go/tests/integration/edge_flow_test.go` | 233 | `9059c2d22b1a8a3a` |
| `services/realtime/gateway-go/tests/load/load_test.go` | 175 | `b64ab2e414fc9918` |
| `services/realtime/gateway-go/tests/protocol/golden_test.go` | 84 | `e8976756739cee25` |
| `services/realtime/gateway-go/tests/protocol/testdata/delivery_with_event_id.golden` | 1 | `8e87bb9bc131c09f` |
| `services/realtime/gateway-go/tests/protocol/testdata/delivery_without_event_id.golden` | 1 | `06f206c3f9880a7c` |
| `services/realtime/gateway-go/tests/protocol/testdata/engine_answer.golden` | 1 | `c3cde56995013d79` |
| `services/realtime/gateway-go/tests/protocol/testdata/engine_track_published.golden` | 1 | `9ad6f96c9c091c31` |
| `services/realtime/gateway-go/tests/protocol/testdata/error.golden` | 1 | `339e543d100c3613` |
| `services/realtime/gateway-go/tests/protocol/testdata/pong.golden` | 1 | `b73eb0b31f2ab04c` |
| `services/realtime/gateway-go/tests/protocol/testdata/ready.golden` | 1 | `63e0b9e271679b63` |
| `services/realtime/gateway-go/tests/protocol/testdata/session_ended.golden` | 1 | `a5608bd79d17330e` |
| `services/realtime/gateway-go/tests/protocol/testdata/session_joined.golden` | 1 | `054c0950fb3df02a` |
| `services/realtime/gateway-go/tests/protocol/testdata/session_joined_steered.golden` | 1 | `fc4a4343302a264b` |
| `services/realtime/gateway-go/tests/protocol/testdata/session_peer_joined.golden` | 1 | `e9499fb08b10d5ae` |
| `services/realtime/gateway-go/tests/protocol/testdata/session_peer_joined_steered.golden` | 1 | `c19dd09edf306243` |
| `services/realtime/gateway-go/tests/protocol/testdata/session_started.golden` | 1 | `1ec9ca8bf9e44f92` |
| `services/realtime/gateway-go/tests/protocol/testdata/signal_answer.golden` | 1 | `e38d82a4bdd86a24` |
| `services/realtime/gateway-go/tests/protocol/testdata/signal_candidate.golden` | 1 | `8801319c5cf66577` |
| `services/realtime/gateway-go/tests/protocol/testdata/signal_offer.golden` | 1 | `ee3db6a69d3f710b` |
| `services/realtime/gateway-go/tests/protocol/testdata/subscribed.golden` | 1 | `c574df0b0b826cce` |
| `services/realtime/gateway-go/tests/protocol/testdata/unsubscribed.golden` | 1 | `f307acd7264ba287` |
| `services/realtime/gateway-go/tests/protocol/testdata/welcome.golden` | 1 | `e5e9e3f5d2e7b2f5` |

## Batch 04B — `services/realtime/media-engine-rs` (80 ফাইল, ভেন্ডর বাদে)

| file | lines | sha256 (first 16) |
|---|---:|---|
| `services/realtime/media-engine-rs/Cargo.lock` | 991 | `76c331334ebc7a8f` |
| `services/realtime/media-engine-rs/Cargo.toml` | 67 | `aa0dda7b8c5fca86` |
| `services/realtime/media-engine-rs/Dockerfile` | 49 | `47f3005cd22bf023` |
| `services/realtime/media-engine-rs/benches/Cargo.toml` | 7 | `71961172dab2d61b` |
| `services/realtime/media-engine-rs/benches/src/main.rs` | 1 | `536e506bb90914c2` |
| `services/realtime/media-engine-rs/bins/loadgen/Cargo.toml` | 17 | `da3c29f523c9c830` |
| `services/realtime/media-engine-rs/bins/loadgen/src/main.rs` | 197 | `5a1f42fcfccba2b4` |
| `services/realtime/media-engine-rs/bins/media-engine/Cargo.toml` | 17 | `b458870447c4160a` |
| `services/realtime/media-engine-rs/bins/media-engine/src/main.rs` | 340 | `10c32ee4846504ff` |
| `services/realtime/media-engine-rs/bins/sdp-tool/Cargo.toml` | 13 | `d972dde975bbcaae` |
| `services/realtime/media-engine-rs/bins/sdp-tool/src/main.rs` | 115 | `a6beba7dc758412a` |
| `services/realtime/media-engine-rs/crates/audio/Cargo.toml` | 7 | `1a6838e34b069beb` |
| `services/realtime/media-engine-rs/crates/audio/src/g711.rs` | 135 | `b5a9b550ca2421aa` |
| `services/realtime/media-engine-rs/crates/audio/src/lib.rs` | 11 | `6bc81fce0b71c769` |
| `services/realtime/media-engine-rs/crates/audio/src/mixer.rs` | 169 | `d312798de56d4bfa` |
| `services/realtime/media-engine-rs/crates/audio/tests/codecs.rs` | 169 | `9e446bd1f10c8937` |
| `services/realtime/media-engine-rs/crates/concurrency/Cargo.toml` | 7 | `81b8a0770bc35df9` |
| `services/realtime/media-engine-rs/crates/concurrency/src/lib.rs` | 323 | `1bf2831725d32b97` |
| `services/realtime/media-engine-rs/crates/concurrency/tests/supervisor.rs` | 119 | `91cb9fae41920c33` |
| `services/realtime/media-engine-rs/crates/dtls/Cargo.toml` | 28 | `47befc774c743a78` |
| `services/realtime/media-engine-rs/crates/dtls/src/lib.rs` | 564 | `9769c50f7fc3abd5` |
| `services/realtime/media-engine-rs/crates/dtls/tests/loopback.rs` | 357 | `a6539e365d08e195` |
| `services/realtime/media-engine-rs/crates/engine/Cargo.toml` | 19 | `49756bfdb1bb0580` |
| `services/realtime/media-engine-rs/crates/engine/src/lib.rs` | 1210 | `7ff794fbf8ed8d6c` |
| `services/realtime/media-engine-rs/crates/engine/tests/dtls.rs` | 724 | `ecbcbcff97f1a763` |
| `services/realtime/media-engine-rs/crates/engine/tests/pipeline.rs` | 459 | `721daf60ea5a2ba5` |
| `services/realtime/media-engine-rs/crates/idempotency/Cargo.toml` | 7 | `83401aed13ebc235` |
| `services/realtime/media-engine-rs/crates/idempotency/src/lib.rs` | 153 | `5b465079dd6eadd7` |
| `services/realtime/media-engine-rs/crates/idempotency/tests/race.rs` | 33 | `a1abef72e8c86c82` |
| `services/realtime/media-engine-rs/crates/idempotency/tests/store.rs` | 97 | `6cf1c242758bf552` |
| `services/realtime/media-engine-rs/crates/livekit/Cargo.toml` | 9 | `a6ab4f69dd0ee3f2` |
| `services/realtime/media-engine-rs/crates/livekit/src/lib.rs` | 356 | `538734c04e9e588a` |
| `services/realtime/media-engine-rs/crates/livekit/tests/jwt.rs` | 184 | `2f8e8aea3d892ec4` |
| `services/realtime/media-engine-rs/crates/media/Cargo.toml` | 9 | `06389b78c3085f84` |
| `services/realtime/media-engine-rs/crates/media/src/lib.rs` | 194 | `ae82decf42e83aef` |
| `services/realtime/media-engine-rs/crates/protocol/Cargo.toml` | 7 | `571d34a4f24dd5f3` |
| `services/realtime/media-engine-rs/crates/protocol/src/json.rs` | 440 | `05f00d985b173718` |
| `services/realtime/media-engine-rs/crates/protocol/src/lib.rs` | 370 | `ee9eb95e414df9df` |
| `services/realtime/media-engine-rs/crates/protocol/tests/wire.rs` | 146 | `607bf8037771026e` |
| `services/realtime/media-engine-rs/crates/rate-limit/Cargo.toml` | 7 | `5b99aa8b51378172` |
| `services/realtime/media-engine-rs/crates/rate-limit/src/lib.rs` | 177 | `744d26e11ced21dc` |
| `services/realtime/media-engine-rs/crates/rate-limit/tests/limiter.rs` | 96 | `96eacd2db06f02e4` |
| `services/realtime/media-engine-rs/crates/routing/Cargo.toml` | 9 | `9dd453606d80c9b6` |
| `services/realtime/media-engine-rs/crates/routing/src/lib.rs` | 421 | `5f28fdd87d30b265` |
| `services/realtime/media-engine-rs/crates/routing/tests/table.rs` | 197 | `b092ff6a3a00c87e` |
| `services/realtime/media-engine-rs/crates/sessions/Cargo.toml` | 9 | `be9662bae0d2edd0` |
| `services/realtime/media-engine-rs/crates/sessions/src/lib.rs` | 292 | `fac4ad2cb02ec0ee` |
| `services/realtime/media-engine-rs/crates/sessions/tests/lifecycle.rs` | 177 | `63f02e2794b9b576` |
| `services/realtime/media-engine-rs/crates/signaling/Cargo.toml` | 11 | `6a7cdc34075e96e4` |
| `services/realtime/media-engine-rs/crates/signaling/src/lib.rs` | 584 | `90269b38dc176148` |
| `services/realtime/media-engine-rs/crates/signaling/tests/flows.rs` | 359 | `5642782ccf4b4bbd` |
| `services/realtime/media-engine-rs/crates/streams/Cargo.toml` | 7 | `54cb1a62b16cd7a5` |
| `services/realtime/media-engine-rs/crates/streams/src/jitter.rs` | 160 | `f210fc120e2d2e9f` |
| `services/realtime/media-engine-rs/crates/streams/src/lib.rs` | 18 | `7c84ffbf71ae0a7b` |
| `services/realtime/media-engine-rs/crates/streams/src/packet.rs` | 164 | `b6cdd3a6a017c4fd` |
| `services/realtime/media-engine-rs/crates/streams/src/replay.rs` | 82 | `9f8bdf9924cfba08` |
| `services/realtime/media-engine-rs/crates/streams/src/rtcp.rs` | 182 | `838acf5fcba35737` |
| `services/realtime/media-engine-rs/crates/streams/src/seq.rs` | 183 | `d5a44314bb612fe3` |
| `services/realtime/media-engine-rs/crates/streams/tests/rtp.rs` | 244 | `a983b46bf0e40b27` |
| `services/realtime/media-engine-rs/crates/telemetry/Cargo.toml` | 7 | `8c5f4bdc109211a2` |
| `services/realtime/media-engine-rs/crates/telemetry/src/lib.rs` | 229 | `a3c46458c4609a0e` |
| `services/realtime/media-engine-rs/crates/telemetry/tests/render.rs` | 40 | `15ec8d1683fdb60e` |
| `services/realtime/media-engine-rs/crates/transport/Cargo.toml` | 9 | `b338f94813bc5a85` |
| `services/realtime/media-engine-rs/crates/transport/src/lib.rs` | 227 | `10dd9af4a03fc342` |
| `services/realtime/media-engine-rs/crates/transport/tests/io.rs` | 109 | `289df5b207e48f52` |
| `services/realtime/media-engine-rs/crates/webrtc/Cargo.toml` | 11 | `23c0e3a594f2766d` |
| `services/realtime/media-engine-rs/crates/webrtc/src/crypto/aes128.rs` | 192 | `66fe1f8326de2360` |
| `services/realtime/media-engine-rs/crates/webrtc/src/crypto/aes256.rs` | 101 | `d06dff59676a4378` |
| `services/realtime/media-engine-rs/crates/webrtc/src/crypto/gcm.rs` | 228 | `cb4891231034f596` |
| `services/realtime/media-engine-rs/crates/webrtc/src/crypto/hmac.rs` | 44 | `c3c11cbce6de1521` |
| `services/realtime/media-engine-rs/crates/webrtc/src/crypto/mod.rs` | 6 | `5ca079e9de6a7278` |
| `services/realtime/media-engine-rs/crates/webrtc/src/crypto/sha1.rs` | 65 | `7edc6fe3906e046d` |
| `services/realtime/media-engine-rs/crates/webrtc/src/crypto/sha256.rs` | 86 | `01716b54f19c3592` |
| `services/realtime/media-engine-rs/crates/webrtc/src/ice.rs` | 439 | `3f4159ac32691c2c` |
| `services/realtime/media-engine-rs/crates/webrtc/src/lib.rs` | 27 | `6f4a103b30ccc030` |
| `services/realtime/media-engine-rs/crates/webrtc/src/sdp.rs` | 263 | `2a1ea77e018ed030` |
| `services/realtime/media-engine-rs/crates/webrtc/src/srtp.rs` | 530 | `51f9712c07865a57` |
| `services/realtime/media-engine-rs/crates/webrtc/src/stun.rs` | 333 | `c797e48a8b6e45cd` |
| `services/realtime/media-engine-rs/crates/webrtc/tests/web.rs` | 529 | `3149d360e1008ed3` |
| `services/realtime/media-engine-rs/scripts/build-media-engine.sh` | 13 | `c9beea2042aaa0e3` |

---

**মোট:** ২১৫টা ফাইল যেগুলো বাদ পড়েছিল/পুরোনো ছিল (৩৩,৮০৮ লাইন) + ৬টা নতুন/ফিক্সড ফাইল = **২২১টা ফাইল, ৩৬,৬৫৬ লাইন** — সব ফুল কনটেন্ট আকারে, sha256 দিয়ে যাচাই করা।

---

## ১২. পরিশিষ্ট B — ব্যাচ ০৫ (Batch 05): automation / notification / inbox-এর ডিউরেবল স্টোরেজ

**কেন:** Batch 01-এর নিজের ডকস্ট্রিং-এ লেখা ছিল তিনটা সার্ভিসের state প্রসেসের ভেতরে (dict) থাকে, আর "a migration is reported at the end of the batch". Batch 02 সেগুলোকে HTTP-তে যুক্ত করার পর সেই ঘাটতি ব্যবহারকারীর কাছে দৃশ্যমান হয়ে যায় — একটা রিস্টার্টে সব automation, notification, unread badge হাওয়া।

**এই ব্যাচে যা এল (৯টা ফাইল, ৫,২৯৫ লাইন, সবই ফুল কনটেন্ট):**

| ফাইল | লাইন | sha256 (১৬) | ধরন |
|---|---:|---|---|
| `alembic/versions/0012_enterprise_persistence.py` | 207 | `2ff619ed2c0e0957` | নতুন |
| `app/services/enterprise_store.py` | 423 | `418be21aa72d532a` | নতুন |
| `app/db/models.py` | 2084 | `868a96d6dd5549d1` | বাদ পড়া/পুরোনো |
| `app/api/automation_routes.py` | 500 | `a52b3f437ef5c085` | বাদ পড়া/পুরোনো |
| `app/api/notification_routes.py` | 480 | `bfe8de590667ad1c` | বাদ পড়া/পুরোনো |
| `app/api/inbox_routes.py` | 528 | `3e2260a5b88122a1` | বাদ পড়া/পুরোনো |
| `tests/test_enterprise_persistence.py` | 415 | `300cf64461fc926e` | নতুন |
| `tests/test_deployment.py` | 359 | `768f79be85b18fa2` | বাদ পড়া/পুরোনো |
| `docs/DEPLOYMENT.md` | 299 | `da8d2c0bb2048516` | বাদ পড়া/পুরোনো |

**যা যোগ হল:**

- `alembic/versions/0012_enterprise_persistence.py` — ৫টা নতুন টেবিল: `automations`, `automation_runs`, `notification_templates`, `notifications`, `inbox_thread_states`। সবগুলোই tenant-scoped, `tenants` থেকে cascade; inbox overlay আবার `calls` থেকেও cascade করে (retention purge-এ কলে-ও-যায়)। state কলামগুলো সাধারণ string — নতুন PG enum টাইপ বানানো হয়নি, যাতে `tests/test_enum_consistency.py`-এর চুক্তি অকারণে না বাড়ে; বন্ধ vocabulary ডোমেইন লেয়ারে আগের মতোই আছে।
- `app/services/enterprise_store.py` — Batch 01-এর রেজিস্ট্রিগুলোকে রিকোয়েস্টের আগে ডাটাবেজ থেকে hydrate করে, পরে flush করে। ফলে নিয়মের (validation, idempotency key, cooldown/max-per-event, রেন্ডারিং, delivery state, thread transition টেবিল, SLA ঘড়ি) **একটাই ইমপ্লিমেন্টেশন** থাকে — আগেরটাই; নতুন করে কিছু লেখা হয়নি, তাই ড্রিফট অসম্ভব।
- `app/db/models.py` — ৫টা মডেল (আগের কোনো লাইন বদলানো হয়নি, শেষে যোগ)।
- তিনটা রাউট মডিউলে এক লাইন করে রাউটার-লেভেল `Depends(durable_state)`।
- `tests/test_enterprise_persistence.py` — রিস্টার্ট সিমুলেট করে প্রমাণ: **সব** in-process state মুছে দিয়ে API দিয়ে আবার পড়া হয়, তাই যা ফেরে তা ডাটাবেজ থেকেই এসেছে। ১৫টা টেস্ট।

**যাচাই:** `alembic` চেইন এখন `0001_baseline → … → 0011_side_effect_exactly_once → 0012_enterprise_persistence`, linear, হেড একটাই। `tests/test_deployment.py`-এর হেড-অ্যাসার্শন আপডেট করা হয়েছে। গোটা স্যুট: **২৫০৫ passed, ৪৩ skipped** (২:৩৯ মিনিট)। ডেলিভারেবল ব্লক ভেরিফিকেশন: **২৩০/২৩০ byte-identical**।

**সৎভাবে যা বাকি (গোপন করা হয়নি):**

- লোকাল ডেভ-টেস্ট পাথ SQLite (`create_all`); প্রোডাকশনে `scripts/migrate.py` (advisory lock) `alembic upgrade head` চালায় — অর্থাৎ ডিপ্লয়ে এই মাইগ্রেশন স্বাভাবিক পথেই বসবে।
- একই tenant-এ দুটো আলাদা API **প্রসেস** একসাথে লিখলে রো-লেভেল লকিং (`SELECT … FOR UPDATE`) লাগবে; এখন one-worker (Caddy → এক API কন্টেইনার) ডিপ্লয়মেন্টে in-process lock-এ সিরিয়ালাইজ হয়। এটা পরের কাজ, দাবি নয়।

**মোট (Batch 05 সহ):** ব্যাচ ০২–০৫ মিলিয়ে **২৩০টা ফাইল-ব্লক, সব ফুল কনটেন্ট, sha256 দিয়ে যাচাই করা — ২৩০/২৩০ byte-identical**।
