# DECISIONS

Running log of decisions, shortcuts, and deferrals taken while building the
investor-demo version of Orchestrator. Each entry: what we chose, why, and
what production needs to revisit.

Pitch date: 2026-04-25 (Saturday).

---

## Scope

- **Demo is not v1 of production.** This repo is a movie prop that actually works.
  A separate production repo gets spun up post-pitch.
- **One hero flow, not four.** Legal vertical (fake firm "Oak & Partners"):
  prospective client call → AVR intake → drafted intake email → consultation
  invoice in Wave → dashboard shows activity.
  - Email/Telegram/accounting dashboard: demoed as secondary flows or
    screenshots if time permits.
- **Target vertical: legal** (not medical). Same privacy pitch, far less
  compliance surface. Production can revisit medical once HIPAA posture is real.

## Infrastructure

- **Orchestrator host:** LXC container on Proxmox at `192.168.10.22`.
  - **Provisioned: CTID 303, hostname `HermesOrch`, IP `192.168.10.232` (DHCP).**
  - Ubuntu 24.04, 4 cores / 8GB RAM / 40GB disk, Python 3.12, uv 0.11.7.
  - Rationale: faster than VM, lower overhead, isolation is not a demo concern.
  - Production revisit: multi-tenant isolation model, probably VM-per-tenant or
    proper container sandboxing with seccomp/AppArmor profiles.
- **LLM inference:** Ollama on desktop at `192.168.10.33:11434`, reached over LAN.
  - **Primary model: `gemma4:31b` (confirmed available, Q4_K_M, 31.3B params).**
  - Alt local models on the same host: `gemma4:26b`, `qwen3:32b`, `all-minilm`
    (embeddings). `qwen3:32b` is a hot-swap option if Gemma fumbles tool use.
  - End-to-end JSON generation verified from inside the container.
  - Cloud fallback TBD (Grok or Gemini).
  - Production: customer runs their own H100 + ~81B-class model on-prem.
- **HA / replication:** none in demo. Single point of failure on purpose.
  - Production revisit: active-passive with proper DB replication (Postgres WAL
    or Litestream for SQLite). NOT rsync for stateful data — rsync during a
    write produces a torn copy.

## Security posture (demo)

- Fake law firm, fake clients, fake matters. No real PII anywhere.
- Tiered action model — even in demo, so it shows well:
  - **Tier 1 (auto):** read-only actions (classify, summarize, retrieve).
  - **Tier 2 (draft + approve):** outbound email, invoice creation.
  - **Tier 3 (blocked):** anything that could be construed as legal advice.
- Secrets in `.env`, gitignored. No secrets manager in demo.
  - Production revisit: real secrets manager, per-deployment key rotation,
    audit logging.

## Integrations

- **Phone:** Twilio Voice → webhook → orchestrator. Public URL via ngrok
  free tier (see Public URL section). Cloudflare Tunnel is the production
  target.
- **STT:** Twilio built-in (`speech_model=phone_call`) — free, zero-latency.
  Whisper deferred until we have a GPU in the deploy target.
- **TTS:** Twilio `Polly.Joanna-Neural` (free with Twilio). Upgrade path to
  Cartesia / ElevenLabs is pre-planned in config.
- **Accounting: QuickBooks Online (sandbox)** — *pivoted from Wave on
  2026-04-23*. Wave's May-2025 policy change gated third-party OAuth
  behind a Wave Pro subscription and reshuffled the UI (the Dev Portal
  link now redirects to the dashboard for most accounts), so we swapped
  to QBO Sandbox. QBO Sandbox: free Intuit developer account, larger SMB
  install base, cleaner demo story. Integration uses OAuth2 with
  in-memory refresh; client lives at `hermes/qbo.py`.
- **Accounting fallback:** if QBO env vars are unset, `create_invoice`
  still persists a local Invoice row as status=draft — keeps the demo
  working even if tokens expire on stage.

## Deferred / faked for demo

- Auth: none. Orchestrator trusts its network.
- Multi-tenancy: none. One fake firm.
- Observability: console logs only. No metrics, no tracing.
- Tests: best-effort. We're optimizing for Saturday, not coverage.
- Language support: English only.
- Context DB / long-term memory: skipped for demo per founder. SQLite is
  sufficient for the call/email/invoice records we need.

---

## Twilio AVR pipeline — built & validated 2026-04-23

- Routes: `POST /webhook/twilio/{voice,voice_reprompt,gather,status}`.
  Mounted on the same uvicorn app as the dashboard.
- Single-turn intake: greet → `<Gather input="speech" speech_timeout="auto"
  speech_model="phone_call">` → persist transcript → farewell + `<Hangup>`.
  Post-call agent run fires as a FastAPI BackgroundTask.
- TTS: Twilio `<Say voice="Polly.Joanna-Neural">` (free, decent). Upgrade
  path: Cartesia or ElevenLabs via `<Play>` of pre-synthesized audio.
- STT: Twilio's `speech_model=phone_call` (built-in, zero-latency). Whisper
  deferred — would add turn latency without a GPU in the LXC.
- Signature validation (Twilio HMAC-SHA1) ON by default; hooked via
  `uvicorn.middleware.proxy_headers.ProxyHeadersMiddleware` so the public
  (ngrok) URL is what we verify against, not the LAN URL.
- **Smoke-tested end to end** with a locally-signed fake Twilio POST: real
  call row created with `twilio_sid`, transcript captured, background agent
  ran all 7 steps (lookup, create_client, log_call_summary,
  draft_intake_email, tier2-queue send_email, tier2-queue create_invoice,
  final), 2 approvals pending in dashboard.

## QBO sandbox wired — closed 2026-04-23

- Sandbox company: "Sandbox Company US f3c5" (US).
- Dev app: "HermesOrch Demo" — client_id and secret stored only in
  `/opt/hermes/.env` (gitignored).
- Tokens: refresh_token + realm_id stored in `.env`. Access token
  deliberately NOT persisted — client refreshes on first request. This
  survives a restart after the 1-hour access-token expiry.
- End-to-end verified: simulated intake call (Reyna Holtz, corporate
  formation), approved `create_invoice` → QBO created customer #58 and
  invoice #145 (`https://sandbox.qbo.intuit.com/app/invoice?txnId=145`).
  Dashboard shows "View in QBO · #145 ↗" link.
- Local client row gets `external_customer_id` cached so subsequent
  invoices for the same client skip the lookup round-trip.

## Public URL + Twilio webhook (closed 2026-04-23)

- ngrok 3.38 installed on HermesOrch via `scripts/install_ngrok.sh`.
- Tunnel runs as `ngroktunnel.service` (transient systemd unit). Note:
  `systemd-run` must be invoked with `--setenv=HOME=/root` and ngrok with
  `--config=/root/.config/ngrok/ngrok.yml` — without these the authtoken
  config isn't found.
- Current public URL: **https://zookeeper-passport-crank.ngrok-free.dev**
  (free tier → URL changes if the tunnel restarts; plan to leave it up
  through the pitch).
- Twilio number `+18556293890` now points at:
    voice_url       = $PUBLIC_BASE_URL/webhook/twilio/voice
    status_callback = $PUBLIC_BASE_URL/webhook/twilio/status
- Signature validation verified via locally-signed fake POSTs before and
  via real public reachability (healthz) after.

## Dashboard — verified 2026-04-23

- Running as `hermesweb.service` (transient systemd unit) on the LXC at
  `http://192.168.10.232:8000`. Reachable from LAN / VPN.
- Stack: FastAPI + Jinja + HTMX (CDN) + Tailwind (CDN). Zero JS build step.
- Three panels: simulate-inbound-call form (with 3 preset scenarios and a
  mode toggle for native vs. json), pending approvals (rich preview of the
  would-be email/invoice), activity feed (calls/emails/invoices).
- End-to-end verified: clicking "Run intake" on `urgent_employment` preset
  drives a ~25 s agent run that performs 7 tool calls (lookup_client,
  create_client, log_call_summary, draft_intake_email, tier2-queue
  send_email, tier2-queue create_invoice) and produces 2 pending approvals.
  Approving both via dashboard → email marked sent, invoice row created.
- **Design choice**: tier-2 calls no longer halt the agent by default. They
  enqueue an approval, the loop gets a `queued_for_approval` tool result,
  and the agent continues through remaining steps. This produces a
  complete set of approvals per call — better demo narrative, and the
  approval gate is still binding. Override with `halt_on_tier2=True`.

## Agent core — verified 2026-04-23

- Two tool-use modes in parallel: `AgentMode.NATIVE` (Ollama built-in
  `tool_calls`) and `AgentMode.JSON` (strict JSON output parsed by the loop).
  Both passed the same smoke test (find Marshall → list matters → final
  response) in 3 iterations against `gemma4:31b`.
- Tier-2 gate verified: requesting "draft AND send email" drafted Email #1
  (real body content generated by Gemma), attempted `send_email`, halted with
  `approval_required`, and created ApprovalRequest #1.
- Audit log wrote 13 rows per the test run; observability baseline is in
  place without needing an APM.
- Deploy path: `rsync Mac → /opt/hermes on container (192.168.10.232)`,
  then `uv sync` + `uv run python -m hermes.cli ...`. Iteration cycle is
  ~seconds.

## Open questions (as of 2026-04-23)

- Programming language: Python (decided).
- Twilio credentials — pending.
- Cloud LLM fallback API key — pending (xAI path wired, just needs key).
- Wave API token — user to generate after work.
- Pitch venue: determines whether Ollama-on-desktop is reachable day-of.
