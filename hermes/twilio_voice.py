"""Twilio Voice webhooks and post-call agent handoff.

Multi-turn intake (v2):

  1. POST /webhook/twilio/voice       — call arrives. Greet + initial <Gather>.
  2. POST /webhook/twilio/gather      — first utterance. We accumulate the
                                        transcript on the Call row. If we
                                        have the caller's email, close;
                                        otherwise re-prompt specifically for
                                        the email address (spell-out invited).
  3. POST /webhook/twilio/gather?state=email — second utterance. Whether or
                                        not we found an email, we close the
                                        call here (no infinite loops).
                                        Background agent runs on aggregate.
  4. POST /webhook/twilio/status      — Twilio's call-status callback.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
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
    "After the tone, please share your name, the best email to reach you at, "
    "and a brief description of your legal matter. Take your time."
)

REPROMPT_FOR_EMAIL = (
    "Thank you. I did not catch an email address — could you please share "
    "yours now? Say it naturally, like 'jane at gmail dot com'. If you do "
    "not have one, just say 'I don't have one' and we will follow up by "
    "phone instead. Take your time."
)

REPROMPT_ON_SILENCE = (
    "I did not catch that. When you are ready, please share your name, email, "
    "and a brief description of your matter."
)

FAREWELL_COMPLETE = (
    "Thank you. I have recorded your matter. An attorney will be in touch "
    "within one business day. Goodbye."
)

FAREWELL_INCOMPLETE = (
    "Thank you. I will flag your matter for follow-up and someone from our "
    "team will reach out on this number. Goodbye."
)

FAREWELL_NO_SPEECH = (
    "I am sorry, I did not hear anything. Please call back when you are ready. "
    "Goodbye."
)

VOICE = settings.tts_voice_id or "Polly.Joanna-Neural"


# --- email extraction from spoken transcript ---

# TLD allowlist + non-greedy domain capture means `assist@hotmail.com.thank`
# gets parsed as `assist@hotmail.com` (stopping at the first real TLD),
# not treated as a single absurd domain.
_KNOWN_TLDS = (
    r"(?:com|net|org|co|io|ai|edu|gov|mil|info|biz|me|us|uk|ca|au|de|fr|"
    r"jp|it|es|nl|ru|br|in|mx|cn|se|no|dk|fi|pl|cz|tv|app|dev|xyz|example)"
)
_EMAIL_RE = re.compile(
    rf"\b([a-z0-9][a-z0-9._+-]*)@([a-z0-9][a-z0-9.-]*?\.{_KNOWN_TLDS})\b"
)


def extract_email(text: str) -> str | None:
    """Pull an email address out of a spoken Twilio transcript.

    Priority:
      1. A LITERAL email already present (`foo@bar.com`) — most reliable.
         This runs BEFORE any spoken-form substitution so we don't corrupt
         an already-good address by turning nearby ' at ' into '@'.
      2. Spoken form — 'amara at post dot example' → 'amara@post.example'.
    """
    if not text:
        return None
    lowered = text.lower()
    m = _EMAIL_RE.search(lowered)
    if m:
        return m.group(0)
    # Fall back to spoken-form substitution. Deliberately NOT collapsing
    # `\s*\.\s*` because that eats sentence boundaries (e.g. "hotmail.com.
    # thank you" → "hotmail.com.thank you" → bogus domain match).
    t = " " + lowered + " "
    t = re.sub(r"\s+(?:at)\s+", "@", t)
    t = re.sub(r"\s+(?:dot|period|point)\s+", ".", t)
    t = re.sub(r"\s*@\s*", "@", t)
    m = _EMAIL_RE.search(t)
    return m.group(0) if m else None


# --- signature validation (optional, on by default) ---

def _validator() -> RequestValidator | None:
    if not settings.twilio_auth_token:
        return None
    return RequestValidator(settings.twilio_auth_token)


async def _validate(request: Request) -> None:
    val = _validator()
    if val is None:
        return
    sig = request.headers.get("X-Twilio-Signature", "")
    if not sig:
        raise HTTPException(status_code=403, detail="missing Twilio-Signature")
    url = str(request.url)
    form = await request.form()
    params = {k: v for k, v in form.multi_items()}
    if not val.validate(url, params, sig):
        log.warning("Twilio signature failed for %s", url)
        raise HTTPException(status_code=403, detail="invalid Twilio-Signature")


# --- TwiML helpers ---

def _gather(action: str, say_text: str) -> VoiceResponse:
    vr = VoiceResponse()
    g = Gather(
        input="speech",
        action=action,
        method="POST",
        # 8s of silence after detected speech before Twilio considers the
        # caller done. Real callers pause to think mid-sentence; 4s was too
        # tight (we observed single-syllable transcripts in real calls).
        # Trade-off is slightly longer call duration; cost impact pennies.
        speech_timeout="8",
        speech_model="phone_call",
        # Enhanced model is ~4× the cost of standard phone_call but
        # noticeably more accurate on proper nouns and structured strings
        # like emails. Pennies per call, load-bearing for a demo.
        enhanced="true",
        language="en-US",
        # Hints bias STT toward recognizing tokens common in intake calls.
        hints=(
            "at, dot, email, my email, my email address, "
            "gmail, yahoo, hotmail, outlook, icloud, aol, proton, fastmail, "
            "com, org, net, io, co, edu, "
            "my name is, my phone number is, call me back, consultation, "
            "I don't have one, I don't have an email"
        ),
        timeout=20,          # wait 20s for speech to START after the prompt
        max_speech_time=90,  # up to 90s once speaking (was 60s)
    )
    g.say(say_text, voice=VOICE)
    vr.append(g)
    return vr


def _say_and_hangup(text: str) -> VoiceResponse:
    vr = VoiceResponse()
    vr.say(text, voice=VOICE)
    vr.hangup()
    return vr


# --- routes ---

@router.post("/webhook/twilio/voice")
async def voice(request: Request) -> Response:
    await _validate(request)
    form = await request.form()
    call_sid = form.get("CallSid") or ""
    from_number = form.get("From") or ""
    to_number = form.get("To") or ""

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

    vr = _gather(action="/webhook/twilio/gather", say_text=GREETING)
    vr.redirect(url="/webhook/twilio/voice_reprompt", method="POST")
    return Response(content=str(vr), media_type="application/xml")


@router.post("/webhook/twilio/voice_reprompt")
async def voice_reprompt(request: Request) -> Response:
    await _validate(request)
    vr = _gather(action="/webhook/twilio/gather", say_text=REPROMPT_ON_SILENCE)
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
    state = request.query_params.get("state", "initial")

    log.info("gather sid=%s state=%s result_len=%d", call_sid, state, len(speech_result))

    # Empty transcript → fall back to the silence re-prompt (same as before).
    if not speech_result:
        vr = VoiceResponse()
        vr.redirect(url="/webhook/twilio/voice_reprompt", method="POST")
        return Response(content=str(vr), media_type="application/xml")

    # Accumulate transcript on the Call row + try to capture email.
    aggregate_transcript = ""
    captured_email_str: str | None = None
    call_id = 0
    with session_scope() as s:
        call = s.scalar(select(Call).where(Call.twilio_sid == call_sid))
        if call:
            prior = (call.transcript or "").strip()
            aggregate_transcript = (prior + ("\n" if prior else "") + speech_result).strip()
            call.transcript = aggregate_transcript
            call.turns = (call.turns or 0) + 1
            if not call.captured_email:
                email = extract_email(aggregate_transcript)
                if email:
                    call.captured_email = email
            captured_email_str = call.captured_email
            call_id = call.id
    have_email = bool(captured_email_str)

    # Decide next TwiML.
    if state == "initial" and not have_email:
        # Re-prompt once, specifically for the email.
        vr = _gather(action="/webhook/twilio/gather?state=email", say_text=REPROMPT_FOR_EMAIL)
        vr.say(FAREWELL_INCOMPLETE, voice=VOICE)
        vr.hangup()
        return Response(content=str(vr), media_type="application/xml")

    # Close the call. Pick the farewell based on whether we have an email now.
    farewell = FAREWELL_COMPLETE if have_email else FAREWELL_INCOMPLETE
    vr = _say_and_hangup(farewell)

    # Kick off the post-call agent with whatever we've got.
    if call_id:
        caller_phone = form.get("From") or ""
        background_tasks.add_task(
            _run_post_call_agent,
            call_id=call_id,
            caller_phone=caller_phone,
            transcript=aggregate_transcript,
            captured_email=captured_email_str,
        )

    return Response(content=str(vr), media_type="application/xml")


@router.post("/webhook/twilio/status")
async def status(request: Request) -> Response:
    await _validate(request)
    form = await request.form()
    call_sid = form.get("CallSid") or ""
    call_status = form.get("CallStatus") or ""
    if call_sid and call_status in ("completed", "canceled", "failed", "no-answer", "busy"):
        with session_scope() as s:
            call = s.scalar(select(Call).where(Call.twilio_sid == call_sid))
            if call:
                call.ended_at = datetime.utcnow()
                if call.status == "in_progress":
                    call.status = "completed" if call_status == "completed" else call_status
    return Response(status_code=204)


# --- background agent ---

def _build_live_call_prompt(
    call_id: int,
    caller_phone: str,
    transcript: str,
    captured_email: str | None,
) -> str:
    from hermes.tools import resolve_qbo_contact
    qbo_hit = resolve_qbo_contact(captured_email, caller_phone)
    if captured_email:
        email_line = (
            f"CALLER EMAIL (extracted by the system, canonical): {captured_email}\n"
            "Use this EXACT string verbatim wherever you need an email — "
            "for find_qbo_customer_by_contact, create_client, "
            "draft_intake_email, etc. Do NOT re-parse the transcript for the "
            "email; trust this extracted value."
        )
    else:
        email_line = (
            "The caller's email was NOT captured — no usable email address "
            "was obtained during the call."
        )
    final_step = (
        "6. create_invoice — $250 consultation fee, one-line description. "
        "Use the client_id from step 1 or 2. If the client has an "
        "external_customer_id (because the QBO LOOKUP above matched), the "
        "invoice reuses the QBO customer directly — no duplicate is created."
        if captured_email
        else "6. mark_call_needs_followup with reason 'missing email'. Do NOT "
             "call create_invoice — we do not have a way to deliver or bill "
             "this prospect yet. The call still gets a draft email so the "
             "operator sees what WOULD go out; that draft uses "
             "'callback-required@oakandpartners.example' as to_address."
    )
    from hermes.web import _format_qbo_lookup_block
    return (
        f"A phone call just ended (call_id={call_id}). Handle the intake "
        "end-to-end.\n\n"
        f"Caller phone: {caller_phone}\n"
        f"Intake completeness: {email_line}\n\n"
        f"{_format_qbo_lookup_block(qbo_hit)}"
        f"Transcript:\n\"\"\"\n{transcript}\n\"\"\"\n\n"
        "Do ALL of the following steps in order. Do not stop early.\n"
        "1. lookup_client — search LOCAL records by any name, email, or phone "
        "you can infer from the transcript.\n"
        "2. If step 1 returned zero local matches, call create_client with "
        "the caller's name, kind=individual, phone, and email (only if "
        "captured). IMPORTANT: if the QBO LOOKUP above reported matched=true, "
        "you MUST pass its qbo_customer_id as external_customer_id so the "
        "records link.\n"
        "3. log_call_summary — include caller_name extracted from the "
        "transcript, a 1-2 sentence summary, pick matter_type from the enum, "
        "and set urgency based on any deadlines mentioned.\n"
        "4. draft_intake_email — professional, acknowledges what was said, "
        "commits to a specific next step. If no email was captured, use "
        "to_address='callback-required@oakandpartners.example'.\n"
        "5. send_email — queue the draft (you will see queued_for_approval; "
        "continue).\n"
        f"{final_step}\n"
        "When done, give a one-sentence summary that notes whether the "
        "client was already in QBO."
    )


def _run_post_call_agent(
    call_id: int,
    caller_phone: str,
    transcript: str,
    captured_email: str | None = None,
) -> None:
    prompt = _build_live_call_prompt(call_id, caller_phone, transcript, captured_email)
    try:
        run_agent(prompt, mode=AgentMode.NATIVE, max_iters=10)
    except Exception as e:
        log.exception("post-call agent run failed: %s", e)
