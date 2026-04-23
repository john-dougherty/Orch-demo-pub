"""Telegram bot — conversational operator interface for HermesOrch.

Webhook mode: Telegram POSTs updates to /webhook/telegram, we reply via the
Bot API. Allowlist-gated by chat_id. Same LLM pipeline as the call agent but
a distinct AgentConfig (read-focused tools + approve/reject, conversational
prompt, fewer iterations).

One-time setup: POST to /setWebhook with our public URL. A helper endpoint
/webhook/telegram/bootstrap handles that, callable by a trusted admin.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse

from hermes.agent import AgentConfig, AgentMode, run_agent
from hermes.config import settings

log = logging.getLogger(__name__)

router = APIRouter()


_BASE = "https://api.telegram.org"


TELEGRAM_SYSTEM_PROMPT = """You are the operator-facing assistant for Oak & Partners' \
AI intake orchestrator. You are chatting over Telegram with a firm operator (paralegal \
or attorney). Your job is to help them understand what the intake system has been \
doing, answer questions about calls/emails/invoices/QBO, and approve or reject \
pending actions when the operator asks you to.

Style:
- Be concise. One short paragraph or a tight bullet list. No preamble.
- Never invent data. If you don't have it, call a tool or say you can't find it.
- For approvals: confirm the specific item back to the operator in your final \
response (e.g. "Approved #3 — send_email to amara.chen@post.example. Sent.").
- Never provide legal advice.

Tools you have are read-only except for approve_request and reject_request, which \
execute Tier-2 actions IMMEDIATELY. Only call those when the operator has clearly \
asked to approve or reject a specific approval_id.
"""


TELEGRAM_TOOLS = [
    "list_pending_approvals",
    "list_recent_calls",
    "list_recent_invoices",
    "summarize_day",
    "lookup_client",
    "list_matters_for_client",
    "qbo_customer_lookup",
    "qbo_invoice_status",
    "approve_request",
    "reject_request",
]


TELEGRAM_AGENT_CONFIG = AgentConfig(
    mode=AgentMode.NATIVE,
    max_iters=5,
    system_prompt=TELEGRAM_SYSTEM_PROMPT,
    tool_allowlist=TELEGRAM_TOOLS,
    halt_on_tier2=False,
    temperature=0.3,
)


# --- allowlist helpers ---

def _allowed_chat_ids() -> set[int]:
    raw = (settings.telegram_allowed_chat_ids or "").strip()
    if not raw:
        return set()
    out: set[int] = set()
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            out.add(int(piece))
        except ValueError:
            log.warning("ignoring non-integer chat id in allowlist: %r", piece)
    return out


def _is_allowed(chat_id: int) -> bool:
    allowed = _allowed_chat_ids()
    # Empty allowlist = locked down (no one allowed). This is the safe default
    # until the operator explicitly configures their chat id.
    return chat_id in allowed


# --- bot API calls ---

def _bot_url(method: str) -> str:
    token = settings.telegram_bot_token
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    return f"{_BASE}/bot{token}/{method}"


def send_message(chat_id: int, text: str, *, parse_mode: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text[:4096]}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    resp = httpx.post(_bot_url("sendMessage"), json=payload, timeout=15)
    if resp.status_code != 200:
        log.warning("telegram sendMessage failed: %s %s", resp.status_code, resp.text[:300])
    return resp.json() if resp.content else {}


def notify_allowlist(text: str) -> None:
    """Push a message to every allowlisted chat. No-op if the bot isn't
    configured or no chats are allowlisted. Exceptions are swallowed so
    this can be safely called from anywhere in the agent path."""
    if not settings.telegram_bot_token:
        return
    chats = _allowed_chat_ids()
    if not chats:
        return
    for cid in chats:
        try:
            send_message(cid, text)
        except Exception:
            log.debug("telegram notify to %s failed (ignored)", cid, exc_info=True)


# --- webhook handler ---

@router.post("/webhook/telegram")
async def telegram_update(
    request: Request,
    background_tasks: BackgroundTasks,
) -> JSONResponse:
    # Telegram doesn't sign payloads; we rely on the obscurity of our ngrok
    # URL + the bot token. For production we'd move to a secret_token header
    # (passed on setWebhook) and validate it here.
    try:
        update = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")

    message = update.get("message") or update.get("edited_message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text = (message.get("text") or "").strip()

    if not chat_id:
        return JSONResponse({"ok": True})

    if not _is_allowed(int(chat_id)):
        # Silent drop in production; here we reply so the operator knows why.
        background_tasks.add_task(
            send_message, int(chat_id),
            "Unauthorized. This bot is locked to approved chat IDs.",
        )
        return JSONResponse({"ok": True})

    if not text:
        background_tasks.add_task(send_message, int(chat_id), "Send me a text message.")
        return JSONResponse({"ok": True})

    # Fire the agent in the background so the webhook returns quickly.
    background_tasks.add_task(_handle_message, int(chat_id), text)
    return JSONResponse({"ok": True})


def _handle_message(chat_id: int, text: str) -> None:
    try:
        run = run_agent(text, config=TELEGRAM_AGENT_CONFIG)
        reply = run.final_text.strip() or "Done."
    except Exception as e:
        log.exception("telegram agent run failed")
        reply = f"Error: {e}"
    send_message(chat_id, reply)


# --- admin: set webhook URL ---

@router.post("/webhook/telegram/bootstrap")
async def bootstrap(request: Request) -> JSONResponse:
    """One-time: register the webhook URL with Telegram.

    Protected by a matching `token` query string equal to the current
    `telegram_bot_token` — good enough for a demo-only admin endpoint.
    """
    q_token = request.query_params.get("token")
    if not settings.telegram_bot_token or q_token != settings.telegram_bot_token:
        raise HTTPException(status_code=403, detail="forbidden")
    base = settings.public_base_url.rstrip("/")
    if not base.startswith("https://"):
        raise HTTPException(status_code=400, detail="public_base_url must be https")
    target = f"{base}/webhook/telegram"
    resp = httpx.post(
        _bot_url("setWebhook"),
        json={"url": target, "allowed_updates": ["message", "edited_message"]},
        timeout=15,
    )
    return JSONResponse({"telegram_status": resp.status_code, "body": resp.json(), "webhook_url": target})
