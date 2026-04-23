"""Twilio Voice webhooks and post-call agent handoff.

Flow (v1, single-turn intake):

  1. POST /webhook/twilio/voice     — call arrives. We greet + <Gather> speech.
  2. POST /webhook/twilio/gather    — Twilio finished transcribing. We persist
                                      the transcript, say goodbye, <Hangup>.
                                      The post-call agent run is kicked off
                                      as a BackgroundTask (fire-and-forget).
  3. POST /webhook/twilio/status    — Twilio's call-status callback (optional);
                                      used to capture duration and final state.

A multi-turn conversation is a Task 4b upgrade — we deliberately stop at one
gather to keep the demo's moving parts small.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from urllib.parse import urlencode

from fastapi import APIRouter, BackgroundTasks, Form, Header, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import select
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import Gather, VoiceResponse

from hermes.agent import AgentMode, run_agent
from hermes.config import settings
from hermes.db import Call, Firm, session_scope

log = logging.getLogger(__name__)

router = APIRouter()


GREETING = (
    "Thank you for calling Oak and Partners. This is our AI intake assistant. "
    "After the tone, please share your name, the best way to reach you, and a "
    "brief description of your legal matter. Take your time — I am listening."
)

REPROMPT = (
    "I did not catch that. When you are ready, please share your name, the best "
    "way to reach you, and a brief description of your matter."
)

FAREWELL = (
    "Thank you. I have recorded your matter. An attorney will be in touch "
    "within one business day. Goodbye."
)

FAREWELL_NO_SPEECH = (
    "I am sorry, I did not hear anything. Please call back when you are ready. "
    "Goodbye."
)

VOICE = settings.tts_voice_id or "Polly.Joanna-Neural"


# --- signature validation (optional, on by default) ---

def _validator() -> RequestValidator | None:
    if not settings.twilio_auth_token:
        return None
    return RequestValidator(settings.twilio_auth_token)


async def _validate(request: Request) -> None:
    """Raise 403 if the Twilio-Signature header doesn't check out.

    Relies on the URL Twilio saw. Behind ngrok/CF tunnels this is the public
    URL — FastAPI sees the same via x-forwarded-proto/host if the tunnel sets
    them. We rebuild using request.url which preserves host from the forwarded
    headers starlette honors by default with ProxyHeadersMiddleware.
    """
    val = _validator()
    if val is None:
        return  # not configured → skip (useful for local dev)
    sig = request.headers.get("X-Twilio-Signature", "")
    if not sig:
        raise HTTPException(status_code=403, detail="missing Twilio-Signature")

    url = str(request.url)
    form = await request.form()
    params = {k: v for k, v in form.multi_items()}
    if not val.validate(url, params, sig):
        log.warning("Twilio signature failed for %s", url)
        raise HTTPException(status_code=403, detail="invalid Twilio-Signature")


# --- routes ---

@router.post("/webhook/twilio/voice")
async def voice(request: Request) -> Response:
    await _validate(request)
    form = await request.form()
    call_sid = form.get("CallSid") or ""
    from_number = form.get("From") or ""
    to_number = form.get("To") or ""

    # Create a Call row (or reuse if a retry fires the same SID).
    with session_scope() as s:
        existing = s.scalar(select(Call).where(Call.twilio_sid == call_sid)) if call_sid else None
        if existing is None:
            firm = s.scalar(select(Firm).limit(1))
            s.add(
                Call(
                    firm_id=firm.id if firm else 1,
                    caller_phone=from_number,
                    twilio_sid=call_sid,
                    status="in_progress",
                    started_at=datetime.utcnow(),
                )
            )

    log.info("inbound call %s from %s to %s", call_sid, from_number, to_number)

    vr = VoiceResponse()
    gather = Gather(
        input="speech",
        action="/webhook/twilio/gather",
        method="POST",
        speech_timeout="auto",
        speech_model="phone_call",
        language="en-US",
        # 30s hard cap on listening so a silent line can't hang forever.
        timeout=30,
    )
    gather.say(GREETING, voice=VOICE)
    vr.append(gather)

    # Fallthrough if Gather times out with no speech → one reprompt, then bye.
    vr.redirect(url="/webhook/twilio/voice_reprompt", method="POST")
    return Response(content=str(vr), media_type="application/xml")


@router.post("/webhook/twilio/voice_reprompt")
async def voice_reprompt(request: Request) -> Response:
    await _validate(request)
    vr = VoiceResponse()
    gather = Gather(
        input="speech",
        action="/webhook/twilio/gather",
        method="POST",
        speech_timeout="auto",
        speech_model="phone_call",
        language="en-US",
        timeout=20,
    )
    gather.say(REPROMPT, voice=VOICE)
    vr.append(gather)
    vr.say(FAREWELL_NO_SPEECH, voice=VOICE)
    vr.hangup()
    return Response(content=str(vr), media_type="application/xml")


@router.post("/webhook/twilio/gather")
async def gather(
    request: Request,
    background_tasks: BackgroundTasks,
) -> Response:
    await _validate(request)
    form = await request.form()
    call_sid = form.get("CallSid") or ""
    speech_result = (form.get("SpeechResult") or "").strip()
    confidence = form.get("Confidence") or "0"

    log.info(
        "gather call_sid=%s conf=%s result_len=%d",
        call_sid, confidence, len(speech_result),
    )

    # Empty transcript → reprompt once.
    if not speech_result:
        vr = VoiceResponse()
        vr.redirect(url="/webhook/twilio/voice_reprompt", method="POST")
        return Response(content=str(vr), media_type="application/xml")

    # Persist transcript on the Call row; schedule post-call agent run.
    payload: dict[str, str | int] | None = None
    with session_scope() as s:
        call = s.scalar(select(Call).where(Call.twilio_sid == call_sid))
        if call:
            existing = (call.transcript or "").strip()
            call.transcript = (existing + ("\n" if existing else "") + speech_result).strip()
            call.caller_phone = call.caller_phone or form.get("From") or ""
            payload = {
                "call_id": call.id,
                "caller_phone": call.caller_phone or "",
                "transcript": call.transcript,
            }

    if payload:
        background_tasks.add_task(
            _run_post_call_agent,
            call_id=int(payload["call_id"]),
            caller_phone=str(payload["caller_phone"]),
            transcript=str(payload["transcript"]),
        )

    vr = VoiceResponse()
    vr.say(FAREWELL, voice=VOICE)
    vr.hangup()
    return Response(content=str(vr), media_type="application/xml")


@router.post("/webhook/twilio/status")
async def status(request: Request) -> Response:
    await _validate(request)
    form = await request.form()
    call_sid = form.get("CallSid") or ""
    call_status = form.get("CallStatus") or ""
    duration = form.get("CallDuration")

    if call_sid and call_status in ("completed", "canceled", "failed", "no-answer", "busy"):
        with session_scope() as s:
            call = s.scalar(select(Call).where(Call.twilio_sid == call_sid))
            if call:
                call.ended_at = datetime.utcnow()
                if call.status == "in_progress":
                    call.status = "completed" if call_status == "completed" else call_status

    return Response(status_code=204)


# --- background agent ---

def _build_live_call_prompt(call_id: int, caller_phone: str, transcript: str) -> str:
    # Kept in-module to avoid circular imports with hermes.web.
    return (
        f"A phone call just ended (call_id={call_id}). Handle the intake "
        "end-to-end.\n\n"
        f"Caller phone: {caller_phone}\n"
        f"Transcript:\n\"\"\"\n{transcript}\n\"\"\"\n\n"
        "Do ALL of the following steps in order. Do not stop early.\n"
        "1. lookup_client — search by any name you can infer from the "
        "transcript, or by phone number.\n"
        "2. If step 1 returned zero matches, extract the caller's name from "
        "the transcript and call create_client with kind=individual, phone, "
        "and an email only if the caller stated one aloud.\n"
        "3. log_call_summary — include caller_name extracted from the "
        "transcript, a 1-2 sentence summary, pick matter_type from the enum, "
        "and set urgency based on any deadlines mentioned.\n"
        "4. draft_intake_email — professional, acknowledges what was said, "
        "commits to a specific next step. If no email was stated in the "
        "transcript, use to_address='callback-required@oakandpartners.example' "
        "so the operator knows to collect one.\n"
        "5. send_email — queue the draft (you will see queued_for_approval; "
        "continue).\n"
        "6. create_invoice — $250 consultation fee, one-line description. "
        "Use the client_id from step 1 or 2.\n"
        "When the six steps are done, give a one-sentence summary."
    )


def _run_post_call_agent(call_id: int, caller_phone: str, transcript: str) -> None:
    """Synchronous wrapper; called via BackgroundTasks (new thread)."""
    prompt = _build_live_call_prompt(call_id, caller_phone, transcript)
    try:
        run_agent(prompt, mode=AgentMode.NATIVE, max_iters=10)
    except Exception as e:
        log.exception("post-call agent run failed: %s", e)
