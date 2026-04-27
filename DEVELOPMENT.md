# 🤖 AI Development Guide

This document provides context for AI agents (Claude, Gemini, GPT) to safely modify and maintain the HermesOrch project.

## 🏗️ Architectural Mandates

1.  **Tiered Tools:** Side-effects are isolated in `hermes/tools.py`. 
    -   **Tier 1:** Read-only or internal DB writes.
    -   **Tier 2:** Enqueues to `ApprovalRequest` table. *Never* trigger outbound effects (email/invoice) directly from an agent loop.
    -   **Tier 3:** Blocked logic.
2.  **Environment over Code:** Configuration must stay in `hermes/config.py` (Pydantic Settings). Do not hardcode URLs or keys.
3.  **Deterministic Pre-processing:** Use `twilio_voice.py` logic to extract data (email/phone) before the LLM starts to minimize hallucinations.

## 🧪 Verification Workflow

Before proposing changes, ensure the following pass:

1.  **Linting:** `ruff check .`
2.  **Schema:** If modifying `hermes/db.py`, ensure `scripts/seed_fake_data.py` is updated to match.
3.  **Simulated Call:** Use the Dashboard "Simulate Call" button to verify the end-to-end agent loop without burning Twilio credits.

## 📂 Key Context for Ingestion

-   **Agent Loop:** See `hermes/agent.py`. It supports both Ollama Native tool-calling and a JSON-fallback mode.
-   **UI Patterns:** Uses HTMX for all interactivity. If adding a button, it likely belongs in `hermes/templates/partials/`.
-   **Mocking:** `scripts/seed_fake_data.py` is the source of truth for the "Oak & Partners" demo data.

## 🚦 Safety Boundaries

-   **Secrets:** Never commit values to `.env`. Always mask examples in `.env.example`.
-   **Firm Identity:** Keep "Oak & Partners" as the demo identity unless explicitly instructed to rebrand.
