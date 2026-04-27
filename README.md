# HermesOrch

**Local-first AI intake orchestrator for small professional-services firms** (legal,
accounting, medical). Runs on a customer-owned box. Answers the phone, drafts the
follow-up, files the invoice — with a human-in-the-loop approval gate between
"draft" and "send" on anything consequential.

## 🚀 Handoff Summary (Public Fork)

This fork has been prepared for external sharing. Key changes for new environment setup:
- **Credential Masking:** All API keys in `.env.example` and `config.py` replaced with `XXX-000` patterns.
- **AI Runbook:** Added [`DEVELOPMENT.md`](./DEVELOPMENT.md) with architectural mandates for AI-assisted maintenance.
- **Auto-Scaffolding:** `main.py` now automatically creates the `./data` directory on startup.
- **Validation:** Use `python scripts/check_env.py` to verify your credentials before launching.

This repository is the **investor-demo build**
, modeled on a fictitious law firm
("Oak & Partners"). Production deliberately lives in a separate repo so demo
shortcuts don't contaminate production decisions. Tracked shortcuts and
deferrals live in [`DECISIONS.md`](./DECISIONS.md). For AI-assisted development 
and project ingestion guidelines, see [`DEVELOPMENT.md`](./DEVELOPMENT.md).

---

## What it does

- **Answers inbound phone calls** via a multi-turn AVR — greet, gather, re-prompt
  specifically for email if it wasn't captured, close.
- **Drafts a follow-up email and a consultation invoice** from the call transcript
  using a local LLM.
- **Queues every outbound action for one-click operator approval.** Nothing leaves
  the building on its own.
- **Real email delivery** via SMTP (Gmail for the demo; any SMTP in production).
- **Real invoice creation** in QuickBooks Online (sandbox in the demo), with
  cross-system customer dedup so repeat callers never produce duplicate QBO
  records.
- **Operators can also work the system over Telegram** — "what's pending?",
  "approve 3", "look up Reyna Holtz in QBO", "is invoice 146 paid?" — proactive
  push on new approvals included.

---

## High-level architecture

```
INBOUND
 ├─ Phone ────────── Twilio Voice ──→ /webhook/twilio/{voice,gather,status}
 ├─ Telegram DM ─────────────────→ /webhook/telegram
 └─ Dashboard "Simulate call" ───→ /simulate/call

                            │
                            ▼

┌───────────────────────────────────────────────────────────────────┐
│  Pre-agent pipeline  (deterministic, server-side)                 │
│     · extract_email() — literal-first regex, spoken-form fallback │
│     · resolve_qbo_contact() — email primary, phone fallback       │
│     · authoritative "QBO LOOKUP RESULT" block fed into the prompt │
└───────────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌───────────────────────────────────────────────────────────────────┐
│  Agent loop  (per-entry-point AgentConfig)                        │
│                                                                   │
│    Ollama (local Gemma 4 31B, primary)                            │
│         └── fallback ── xAI Grok-4 (cloud)                        │
│                                                                   │
│    Tool-use loop supports both modes side-by-side:                │
│       · NATIVE — Ollama's built-in tool_calls                     │
│       · JSON   — strict-JSON output parser                        │
│                                                                   │
│    Tool registry, tiered by side effect:                          │
│       · Tier 1 — auto  (reads + internal writes)                  │
│       · Tier 2 — approval-gated (outbound email, invoice)         │
│       · Tier 3 — blocked  (legal advice, substantive answers)     │
└───────────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌───────────────────────────────────────────────────────────────────┐
│  Persistence + side-effects                                       │
│     · SQLite operational state                                    │
│         firms, clients, matters, calls, emails, invoices,         │
│         approval_queue, audit_log                                 │
│     · Approval queue notifies operator (dashboard + Telegram)     │
│     · On approval: SMTP send, QBO invoice create                  │
│     · Append-only audit log: every tool call, LLM source, result  │
└───────────────────────────────────────────────────────────────────┘
```

---

## Design choices worth calling out

**Tiered actions are structural, not a prompt convention.** Every tool declares
its tier. Tier-2 tools automatically enqueue to the approval queue; Tier-3
tools short-circuit with a block reason. An operator cannot accidentally let
the model send an email by phrasing a prompt differently — the gate is in the
tool registry.

**Determinism where it matters.** Email parsing from a phone transcript is
unreliable when left to an LLM (a caller saying *"…Smith at gmail.com"*
regularly produces hallucinated addresses). Email extraction, phone
normalization, and QBO cross-system lookup all run server-side before the
agent starts; the result is piped into the prompt as an authoritative block
the model cannot fabricate.

**Local-first LLM.** Firms in regulated industries (law, medicine,
accounting) want matter details to never leave the premises. Gemma 4 31B
runs on the customer's own GPU via Ollama. The cloud fallback triggers only
on local failure or on an explicit caller-opt-in escalation.

**Per-entry-point agent configs.** The same tool registry drives the
call-handler, the Telegram operator agent, and the simulate-a-call path —
each with its own system prompt, tool allowlist, max-iteration cap, and
fallback policy. One LLM client, diverged behavior.

**Same agent code runs the real phone call and the "simulate inbound call"
dashboard button.** The only thing that changes is the transport. Means we
can develop, test, and demo without burning live Twilio minutes, and means
a production issue in the agent manifests identically in both paths.

**Append-only audit log.** Every tool call, every LLM source (ollama vs.
fallback), every approval decision writes a row. Each intake lands roughly
15–25 entries. Production auditors in regulated verticals would recognize
the shape immediately.

---

## 🛠️ Setup & Configuration

### 1. Environment Preparation
Copy the example environment file and fill in your credentials:
```bash
cp .env.example .env
```

### 2. Credential Mapping
The system requires keys from the following vendors. Replace the `XXXXX` placeholders in your `.env` with real values:

| Service | Variable | Purpose | Where to find |
| :--- | :--- | :--- | :--- |
| **Ollama** | `OLLAMA_BASE_URL` | Local LLM host | Your Ollama server address (default: `http://localhost:11434`) |
| **xAI** | `FALLBACK_API_KEY` | Fallback LLM (Grok) | [x.ai Console](https://console.x.ai/) |
| **Twilio** | `TWILIO_AUTH_TOKEN` | Phone/SMS logic | [Twilio Console](https://www.twilio.com/console) |
| **Cartesia** | `TTS_API_KEY` | Text-to-Speech | [Cartesia Play](https://play.cartesia.ai/) |
| **Intuit** | `QBO_CLIENT_ID` | QuickBooks Online | [Intuit Developer Portal](https://developer.intuit.com/) |
| **Telegram** | `TELEGRAM_BOT_TOKEN` | Bot interface | Talk to [@BotFather](https://t.me/botfather) |

### 3. Database Initialization
```bash
# Install dependencies
pip install -e .

# Seed initial firm and fake data
python scripts/seed_fake_data.py
```

## 🏗️ Technical Stack

| Layer | Choice | Notes |
|---|---|---|
| Language | Python 3.12 | |
| Web framework | FastAPI + uvicorn | `app.include_router` per concern (web / twilio / telegram) |
| Persistence | SQLite via SQLAlchemy 2.0 | Operational state; production would swap to Postgres with streaming replication |
| LLM — primary | Ollama + Gemma 4 31B | On-prem, customer-owned GPU |
| LLM — fallback | xAI (`grok-4-fast-reasoning`) | OpenAI-compatible API |
| Telephony | Twilio Voice | Demo only; production tiers use SIP trunk + Asterisk on Proxmox |
| Speech | Twilio phone-call STT + Polly Neural TTS | Demo; local Whisper + Piper on the enterprise tier |
| UI | Jinja2 + HTMX + Tailwind CDN | No JS build step; typography via Bodoni Moda + Libre Franklin |
| Accounting | QuickBooks Online | OAuth2 with automatic refresh; dedup guard prevents duplicate customers |
| Email | Gmail SMTP | Demo-redirect env var reroutes all outbound to a single inbox for the pitch |
| Bot | Telegram Bot API | Webhook mode, chat-ID allowlist, proactive push on new approvals |
| Deployment | LXC on Proxmox + ngrok tunnel | Production uses cloudflared for stable URLs |

---

## Repository layout

```
hermes/
 ├── main.py             FastAPI app entry, router wiring
 ├── config.py           pydantic-settings (env-driven)
 ├── db.py               SQLAlchemy models + session helpers
 ├── llm.py              Ollama primary + xAI fallback
 ├── agent.py            Tool-use loop; AgentConfig; NATIVE + JSON modes
 ├── tools.py            Tool registry + all tier-1/2/3 implementations
 ├── twilio_voice.py     Multi-turn voice webhooks + email extraction
 ├── telegram_bot.py     Webhook, allowlist, proactive push
 ├── email_sender.py     SMTP with demo-redirect
 ├── qbo.py              QuickBooks Online REST client + OAuth2 refresh
 ├── web.py              Dashboard, approval endpoints, simulate-call
 └── templates/          Jinja templates (base, dashboard, partials)

scripts/
 ├── provision_lxc.sh           Proxmox LXC bootstrap
 ├── install_ngrok.sh           ngrok install + auth
 ├── configure_twilio_webhook.sh Point the number at the public URL
 ├── seed_fake_data.py          Oak & Partners seed (8 clients, 10 matters)
 └── seed_qbo_customers.py      QBO sandbox seed for dedup demo

DECISIONS.md                    Shortcuts, deferrals, tracked context
README.md                       This file
pyproject.toml                  uv-managed dependencies
```

---

## Not in this repository

- **The production-target codebase.** This repo is the movie-prop-that-works.
- **Customer-specific configuration** (per-deployment prompts, SIP trunk
  credentials, on-prem GPU profiles, etc.).
- **The pitch deck, pricing, and go-to-market.** Those live elsewhere.
