"""FastAPI routes for the operator dashboard + approval queue + simulated calls.

Mounted by hermes.main. Pages are Jinja partials swapped with HTMX so we
avoid a JS build step for the demo.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select

from hermes.agent import AgentMode, run_agent
from hermes.db import (
    ApprovalRequest,
    AuditLog,
    Call,
    Client,
    Email,
    Firm,
    Invoice,
    Matter,
    session_scope,
)
from hermes.tools import get_tool, log_event


TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter()


# --- preset simulated calls ---

SIMULATED_SCENARIOS: dict[str, dict[str, str]] = {
    "urgent_employment": {
        "label": "Urgent employment (new prospect)",
        "caller_name": "Amara Chen",
        "caller_phone": "+1-415-555-0108",
        "transcript": (
            "Hi, my name is Amara Chen, you can reach me at amara.chen@post.example "
            "or this number. I got your name from Marshall Goodwin. I was "
            "terminated last Thursday from Vector Labs and my manager is claiming "
            "I violated a non-solicitation clause when I reached out to two "
            "former clients on LinkedIn. I need to understand my options fast "
            "because my severance is contingent on signing a release by Friday. "
            "Can someone call me back today?"
        ),
    },
    "new_corp_formation": {
        "label": "New entity formation (existing client-adjacent)",
        "caller_name": "Reyna Holtz",
        "caller_phone": "+1-628-555-0155",
        "transcript": (
            "Hi there, I'm a cofounder at a new robotics startup, Reyna Holtz. "
            "We're two weeks away from closing a pre-seed round and our lead "
            "investor is asking for clean formation docs, a founders IP "
            "assignment, and a 409A. Can you give us a quote and timeline? "
            "You were referred by Halide Labs."
        ),
    },
    "billing_question": {
        "label": "Existing client — billing question (simple)",
        "caller_name": "Sierra Construction — AP",
        "caller_phone": "+1-415-555-0118",
        "transcript": (
            "Hi, this is Sierra Construction accounts payable. We got an "
            "invoice dated last week for the Harrison lien dispute and the "
            "matter number doesn't match what we have in our system. Can "
            "someone send over a corrected invoice referencing matter "
            "2411-H? Same total is fine."
        ),
    },
}


# --- data helpers ---

def _fmt_dt(dt: datetime | None) -> str:
    if not dt:
        return "—"
    return dt.strftime("%Y-%m-%d %H:%M")


def _activity_payload() -> dict[str, list[dict[str, Any]]]:
    with session_scope() as s:
        calls = [
            {
                "id": c.id,
                "caller_name": c.caller_name or "(unknown)",
                "caller_phone": c.caller_phone or "—",
                "summary": (c.summary or "")[:180],
                "matter_type_guess": c.matter_type_guess or "—",
                "urgency": c.urgency or "—",
                "started_at": _fmt_dt(c.started_at),
                "status": c.status,
            }
            for c in s.scalars(select(Call).order_by(desc(Call.started_at)).limit(8))
        ]
        emails = [
            {
                "id": e.id,
                "to": e.to_address,
                "subject": e.subject,
                "status": e.status,
                "created_at": _fmt_dt(e.created_at),
                "body_preview": (e.body or "")[:220],
            }
            for e in s.scalars(select(Email).order_by(desc(Email.created_at)).limit(8))
        ]
        invoices = [
            {
                "id": i.id,
                "client": s.get(Client, i.client_id).display_name if i.client_id else "—",
                "amount": f"${i.amount_cents/100:,.2f}",
                "description": (i.description or "")[:160],
                "status": i.status,
                "created_at": _fmt_dt(i.created_at),
                "external_url": i.external_invoice_url,
                "doc_number": i.external_invoice_id,
            }
            for i in s.scalars(select(Invoice).order_by(desc(Invoice.created_at)).limit(8))
        ]
    return {"calls": calls, "emails": emails, "invoices": invoices}


def _approvals_payload() -> list[dict[str, Any]]:
    with session_scope() as s:
        rows = s.scalars(
            select(ApprovalRequest)
            .where(ApprovalRequest.status == "pending")
            .order_by(desc(ApprovalRequest.created_at))
        ).all()
        out: list[dict[str, Any]] = []
        for r in rows:
            preview = _render_approval_preview(s, r)
            out.append(
                {
                    "id": r.id,
                    "tool_name": r.tool_name,
                    "tool_args": r.tool_args,
                    "created_at": _fmt_dt(r.created_at),
                    "preview": preview,
                    "rationale": r.rationale or "",
                }
            )
    return out


def _render_approval_preview(session, req: ApprovalRequest) -> dict[str, Any]:
    """Build a human-legible preview of what will happen if this is approved."""
    args = req.tool_args or {}
    if req.tool_name == "send_email" and "email_id" in args:
        e = session.get(Email, args["email_id"])
        if e:
            return {
                "kind": "email",
                "to": e.to_address,
                "subject": e.subject,
                "body": e.body,
            }
    if req.tool_name == "create_invoice":
        client = session.get(Client, args.get("client_id")) if args.get("client_id") else None
        return {
            "kind": "invoice",
            "client": client.display_name if client else "—",
            "amount": f"${float(args.get('amount_dollars', 0)):,.2f}",
            "description": args.get("description", ""),
        }
    return {"kind": "raw", "body": json.dumps(args, indent=2, default=str)}


# --- routes: pages ---

@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    with session_scope() as s:
        firm = s.scalar(select(Firm).limit(1))
        firm_name = firm.name if firm else "Oak & Partners"
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "firm_name": firm_name,
            "activity": _activity_payload(),
            "approvals": _approvals_payload(),
            "scenarios": SIMULATED_SCENARIOS,
            "default_scenario": "urgent_employment",
        },
    )


@router.get("/partials/activity", response_class=HTMLResponse)
def activity_partial(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "_activity.html", {"activity": _activity_payload()}
    )


@router.get("/partials/approvals", response_class=HTMLResponse)
def approvals_partial(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "_approvals.html", {"approvals": _approvals_payload()}
    )


# --- routes: approvals ---

@router.post("/approvals/{req_id}/approve", response_class=HTMLResponse)
async def approve(request: Request, req_id: int) -> HTMLResponse:
    with session_scope() as s:
        req = s.get(ApprovalRequest, req_id)
        if not req or req.status != "pending":
            raise HTTPException(status_code=404, detail="approval not pending")
        tool = get_tool(req.tool_name)
        if not tool:
            raise HTTPException(status_code=400, detail=f"unknown tool {req.tool_name}")

        # Execute the previously-tier2 tool now that a human has approved it.
        result = await asyncio.to_thread(tool.execute, req.tool_args, req.session_id)
        req.status = "executed" if result.ok else "rejected"
        req.result = {"ok": result.ok, "data": result.data, "error": result.error}
        req.decided_at = datetime.utcnow()
        req.decided_by = "operator"

    log_event(
        req.session_id,
        "approval_executed" if result.ok else "approval_exec_failed",
        tool_name=req.tool_name,
        tool_args=req.tool_args,
        tool_result=req.result,
        note=f"approval_id={req_id}",
    )
    return templates.TemplateResponse(
        request, "_approvals.html", {"approvals": _approvals_payload()}
    )


@router.post("/approvals/{req_id}/reject", response_class=HTMLResponse)
async def reject(request: Request, req_id: int) -> HTMLResponse:
    with session_scope() as s:
        req = s.get(ApprovalRequest, req_id)
        if not req or req.status != "pending":
            raise HTTPException(status_code=404, detail="approval not pending")
        req.status = "rejected"
        req.decided_at = datetime.utcnow()
        req.decided_by = "operator"
    log_event(
        req.session_id,
        "approval_rejected",
        tool_name=req.tool_name,
        tool_args=req.tool_args,
        note=f"approval_id={req_id}",
    )
    return templates.TemplateResponse(
        request, "_approvals.html", {"approvals": _approvals_payload()}
    )


# --- routes: simulate inbound call ---

@router.post("/simulate/call", response_class=HTMLResponse)
async def simulate_call(
    request: Request,
    scenario: str = Form("custom"),
    caller_name: str = Form(...),
    caller_phone: str = Form(...),
    transcript: str = Form(...),
    mode: str = Form("native"),
) -> HTMLResponse:
    # 1) Create a Call row representing the just-ended call.
    with session_scope() as s:
        firm = s.scalar(select(Firm).limit(1))
        firm_id = firm.id if firm else 1
        call = Call(
            firm_id=firm_id,
            caller_name=caller_name,
            caller_phone=caller_phone,
            transcript=transcript,
            status="in_progress",
            started_at=datetime.utcnow(),
        )
        s.add(call)
        s.flush()
        call_id = call.id

    # 2) Feed the agent a call-handling prompt.
    session_id = uuid.uuid4().hex[:12]
    prompt = _build_call_prompt(call_id, caller_name, caller_phone, transcript)

    agent_mode = AgentMode(mode) if mode in ("native", "json") else AgentMode.NATIVE
    await asyncio.to_thread(
        run_agent, prompt, mode=agent_mode, session_id=session_id, max_iters=10
    )

    # 3) Redirect back to dashboard to show new state.
    return RedirectResponse(url="/", status_code=303)


def _build_call_prompt(
    call_id: int, caller_name: str, caller_phone: str, transcript: str
) -> str:
    return (
        f"An intake call just ended (call_id={call_id}). Handle it end-to-end.\n\n"
        f"Caller: {caller_name}\n"
        f"Phone: {caller_phone}\n"
        f"Transcript:\n\"\"\"\n{transcript}\n\"\"\"\n\n"
        "Do ALL of the following steps in order. Do not stop early.\n"
        "1. lookup_client — search for the caller by name or phone.\n"
        "2. If step 1 returned zero matches, call create_client with the "
        "caller's details (use the email from the transcript if given, "
        "otherwise leave email unset). Remember the returned client_id.\n"
        "3. log_call_summary — write a 1–2 sentence summary, pick matter_type "
        "from the enum, set urgency.\n"
        "4. draft_intake_email — professional, acknowledges what was said, "
        "commits to a specific next step. Use the caller's real email from the "
        "transcript as to_address.\n"
        "5. send_email — queue the drafted email (you will see it marked "
        "queued_for_approval; that is fine, continue).\n"
        "6. create_invoice — $250 consultation fee, one line description "
        '(e.g. "Initial legal consultation — employment matter"). Use the '
        "client_id from step 1 or 2.\n"
        "When all six steps are done, give a one-sentence summary."
    )
