"""Tool registry for the agent loop.

Each tool declares its JSON-schema parameters, its tier (1=auto, 2=approval,
3=blocked), and an execute() that performs the action against the DB or an
external service.

Tier-2 tools short-circuit in the agent loop: the agent emits the tool call,
the loop enqueues it on the approval queue, and execution waits on a human.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, ClassVar

from sqlalchemy import or_, select

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


@dataclass
class ToolResult:
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_message(self) -> str:
        payload = {"ok": self.ok, "data": self.data}
        if self.error:
            payload["error"] = self.error
        return json.dumps(payload, default=str)


class Tool:
    """Base class. Subclasses set the class-level attrs + implement execute()."""

    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {},
        "required": [],
    }
    tier: ClassVar[int] = 1  # 1=auto, 2=needs approval, 3=blocked

    def execute(self, args: dict[str, Any], session_id: str) -> ToolResult:
        raise NotImplementedError

    # --- serialization helpers ---

    def to_ollama_spec(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def to_prompt_block(self) -> str:
        """Compact description for injection into a system prompt (JSON mode)."""
        schema = json.dumps(self.parameters, separators=(",", ":"))
        tier_hint = {1: "auto", 2: "needs-approval", 3: "blocked"}[self.tier]
        return f"- {self.name} ({tier_hint}): {self.description}\n  params: {schema}"


# --- registry ---

_REGISTRY: dict[str, Tool] = {}


def register(tool_cls: type[Tool]) -> type[Tool]:
    _REGISTRY[tool_cls.name] = tool_cls()
    return tool_cls


def all_tools() -> list[Tool]:
    return list(_REGISTRY.values())


def get_tool(name: str) -> Tool | None:
    return _REGISTRY.get(name)


# --- Tier 1: safe read/log ---


@register
class LookupClient(Tool):
    name = "lookup_client"
    description = (
        "Find a client by name, email, or phone. Returns up to 5 matches with "
        "their id, display_name, kind, email, phone, and matter count. Use "
        "before creating a new client record to avoid duplicates."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Name, email, or phone fragment"}
        },
        "required": ["query"],
    }
    tier = 1

    def execute(self, args, session_id):
        q = (args.get("query") or "").strip()
        if not q:
            return ToolResult(ok=False, error="query is required")
        like = f"%{q}%"
        with session_scope() as s:
            stmt = (
                select(Client)
                .where(
                    or_(
                        Client.display_name.ilike(like),
                        Client.email.ilike(like),
                        Client.phone.ilike(like),
                    )
                )
                .limit(5)
            )
            rows = s.scalars(stmt).all()
            return ToolResult(
                ok=True,
                data={
                    "matches": [
                        {
                            "id": c.id,
                            "display_name": c.display_name,
                            "kind": c.kind,
                            "email": c.email,
                            "phone": c.phone,
                            "matter_count": len(c.matters),
                        }
                        for c in rows
                    ]
                },
            )


@register
class ListMatters(Tool):
    name = "list_matters_for_client"
    description = "List all matters (cases) associated with a client by their id."
    parameters = {
        "type": "object",
        "properties": {"client_id": {"type": "integer"}},
        "required": ["client_id"],
    }
    tier = 1

    def execute(self, args, session_id):
        cid = args.get("client_id")
        if not isinstance(cid, int):
            return ToolResult(ok=False, error="client_id must be an integer")
        with session_scope() as s:
            rows = s.scalars(select(Matter).where(Matter.client_id == cid)).all()
            return ToolResult(
                ok=True,
                data={
                    "matters": [
                        {
                            "id": m.id,
                            "title": m.title,
                            "matter_type": m.matter_type,
                            "status": m.status,
                            "opened_at": m.opened_at.isoformat(),
                        }
                        for m in rows
                    ]
                },
            )


def _qbo_customer_row(c: dict, *, matched_by: str) -> dict:
    return {
        "matched": True,
        "matched_by": matched_by,
        "qbo_customer_id": str(c["Id"]),
        "display_name": c.get("DisplayName"),
        "email": (c.get("PrimaryEmailAddr") or {}).get("Address"),
        "phone": (c.get("PrimaryPhone") or {}).get("FreeFormNumber"),
    }


def resolve_qbo_contact(email: str | None, phone: str | None) -> dict:
    """Deterministic server-side QBO lookup by email (then phone).

    Used by the intake pipeline BEFORE the agent runs, because LLMs
    re-parse emails unreliably ("X at Y.com" produces surprising tool
    arguments). Returns the same shape as `find_qbo_customer_by_contact`
    for ergonomic parity with the tool (which remains available for
    Telegram operator queries).
    """
    from hermes.qbo import qbo
    if not qbo.configured:
        return {"matched": False, "reason": "qbo_not_configured"}
    email = (email or "").strip()
    phone = (phone or "").strip()
    if not email and not phone:
        return {"matched": False, "reason": "no_contact_provided"}

    # email — exact match
    if email:
        safe = email.replace("'", "\\'")
        resp = qbo._request(
            "GET",
            f"/v3/company/{qbo._realm_id}/query",
            params={
                "query": f"select * from Customer where PrimaryEmailAddr = '{safe}' maxresults 5",
                "minorversion": "73",
            },
        )
        if resp.status_code == 200:
            rows = resp.json().get("QueryResponse", {}).get("Customer", [])
            if rows:
                return _qbo_customer_row(rows[0], matched_by="email")

    # phone — fuzzy, trailing 10 digits as substring
    if phone:
        digits = re.sub(r"\D", "", phone)
        if len(digits) >= 10:
            tail = digits[-10:]
            resp = qbo._request(
                "GET",
                f"/v3/company/{qbo._realm_id}/query",
                params={
                    "query": f"select * from Customer where PrimaryPhone like '%{tail}%' maxresults 5",
                    "minorversion": "73",
                },
            )
            if resp.status_code == 200:
                rows = resp.json().get("QueryResponse", {}).get("Customer", [])
                if rows:
                    return _qbo_customer_row(rows[0], matched_by="phone")

    return {"matched": False}


@register
class FindQBOCustomerByContact(Tool):
    """Kept available for the Telegram operator-query agent, NOT used by the
    intake pipeline — that path runs resolve_qbo_contact() server-side."""
    name = "find_qbo_customer_by_contact"
    description = (
        "Search QuickBooks Online for a customer matching a given email or "
        "phone. Used for operator queries."
    )
    parameters = {
        "type": "object",
        "properties": {
            "email": {"type": "string"},
            "phone": {"type": "string"},
        },
        "required": [],
    }
    tier = 1

    def execute(self, args, session_id):
        res = resolve_qbo_contact(args.get("email"), args.get("phone"))
        return ToolResult(ok=True, data=res)


@register
class CreateClient(Tool):
    name = "create_client"
    description = (
        "Create a new client record locally. Use ONLY after lookup_client "
        "returns zero LOCAL matches AND find_qbo_customer_by_contact has been "
        "attempted. If find_qbo_customer_by_contact found a match, pass its "
        "qbo_customer_id as external_customer_id to link the records and "
        "prevent QBO duplicates at invoice time. Returns the new client_id."
    )
    parameters = {
        "type": "object",
        "properties": {
            "display_name": {"type": "string"},
            "kind": {"type": "string", "enum": ["individual", "business"]},
            "email": {"type": "string"},
            "phone": {"type": "string"},
            "notes": {"type": "string"},
            "external_customer_id": {
                "type": "string",
                "description": (
                    "QBO customer id if find_qbo_customer_by_contact returned "
                    "a match. Omit if no QBO match."
                ),
            },
        },
        "required": ["display_name", "kind"],
    }
    tier = 1

    def execute(self, args, session_id):
        with session_scope() as s:
            firm_id = s.scalar(select(Firm.id).limit(1)) or 1
            client = Client(
                firm_id=firm_id,
                display_name=args["display_name"],
                kind=args["kind"],
                email=args.get("email"),
                phone=args.get("phone"),
                notes=args.get("notes"),
                external_customer_id=args.get("external_customer_id") or None,
            )
            s.add(client)
            s.flush()
            return ToolResult(
                ok=True,
                data={
                    "client_id": client.id,
                    "display_name": client.display_name,
                    "external_customer_id": client.external_customer_id,
                },
            )


@register
class MarkCallNeedsFollowUp(Tool):
    name = "mark_call_needs_followup"
    description = (
        "Flag a call as needing human follow-up because required intake "
        "details (typically email) could not be captured during the call. "
        "Call this INSTEAD of create_invoice when the caller's email is "
        "missing or unusable. Leaves the call visible in the dashboard "
        "with a follow-up flag so an operator can reach out."
    )
    parameters = {
        "type": "object",
        "properties": {
            "call_id": {"type": "integer"},
            "reason": {
                "type": "string",
                "description": "Short reason (e.g. 'missing email', 'unclear matter', 'abandoned').",
            },
        },
        "required": ["call_id", "reason"],
    }
    tier = 1

    def execute(self, args, session_id):
        with session_scope() as s:
            call = s.get(Call, args["call_id"])
            if not call:
                return ToolResult(ok=False, error=f"no call with id {args['call_id']}")
            call.follow_up_reason = args["reason"][:200]
            call.status = "needs_followup"
            return ToolResult(
                ok=True,
                data={"call_id": call.id, "follow_up_reason": call.follow_up_reason},
            )


@register
class LogCallSummary(Tool):
    name = "log_call_summary"
    description = (
        "Record or update a phone call's summary, guessed matter type, and urgency. "
        "Use after handling an inbound call to persist the intake details."
    )
    parameters = {
        "type": "object",
        "properties": {
            "call_id": {"type": "integer", "description": "Existing call row id"},
            "caller_name": {"type": "string"},
            "caller_phone": {"type": "string"},
            "summary": {"type": "string"},
            "matter_type_guess": {
                "type": "string",
                "description": (
                    "One of: corporate_formation, contract_review, employment, "
                    "ip, landlord_tenant, estate_planning, litigation, other"
                ),
            },
            "urgency": {
                "type": "string",
                "enum": ["low", "normal", "high", "urgent"],
            },
        },
        "required": ["call_id", "summary"],
    }
    tier = 1

    def execute(self, args, session_id):
        with session_scope() as s:
            call = s.get(Call, args["call_id"])
            if not call:
                return ToolResult(ok=False, error=f"no call with id {args['call_id']}")
            for field_name in ("caller_name", "caller_phone", "summary", "matter_type_guess", "urgency"):
                if field_name in args and args[field_name] is not None:
                    setattr(call, field_name, args[field_name])
            call.status = "completed"
            s.flush()
            return ToolResult(ok=True, data={"call_id": call.id})


# --- Tier 2: drafts + enqueue for approval ---


@register
class DraftIntakeEmail(Tool):
    name = "draft_intake_email"
    description = (
        "Draft a follow-up email to a prospective client after an intake call. "
        "The email is saved as a draft linked to the call; it is NOT sent. Use "
        "send_email to queue the draft for approval and sending."
    )
    parameters = {
        "type": "object",
        "properties": {
            "to_address": {"type": "string"},
            "subject": {"type": "string"},
            "body": {"type": "string"},
            "related_call_id": {"type": "integer"},
        },
        "required": ["to_address", "subject", "body"],
    }
    tier = 1  # drafting is safe; sending is tier 2

    def execute(self, args, session_id):
        with session_scope() as s:
            firm_id = s.scalar(select(Client.firm_id).limit(1)) or 1
            email = Email(
                firm_id=firm_id,
                direction="outbound",
                to_address=args["to_address"],
                from_address="intake@oakandpartners.example",
                subject=args["subject"],
                body=args["body"],
                related_call_id=args.get("related_call_id"),
                status="draft",
            )
            s.add(email)
            s.flush()
            return ToolResult(ok=True, data={"email_id": email.id, "status": "draft"})


@register
class SendEmail(Tool):
    name = "send_email"
    description = (
        "Send a drafted email to the recipient. This is a Tier-2 action — it will "
        "be queued for human approval before actually sending."
    )
    parameters = {
        "type": "object",
        "properties": {"email_id": {"type": "integer"}},
        "required": ["email_id"],
    }
    tier = 2

    def execute(self, args, session_id):
        from datetime import datetime as _dt

        from hermes.email_sender import email_sender

        # Load the drafted email.
        with session_scope() as s:
            email = s.get(Email, args["email_id"])
            if not email:
                return ToolResult(ok=False, error=f"no email {args['email_id']}")
            to_address = email.to_address
            subject = email.subject
            body = email.body
            email_id_local = email.id

        result = email_sender.send(to_address=to_address, subject=subject, body=body)

        with session_scope() as s:
            email = s.get(Email, email_id_local)
            if email is None:
                return ToolResult(ok=False, error="email vanished mid-send")
            if result.ok:
                email.status = "sent"
                email.sent_at = _dt.utcnow()
            else:
                email.status = "failed"

        return ToolResult(
            ok=result.ok,
            error=result.error,
            data={
                "email_id": email_id_local,
                "status": "sent" if result.ok else "failed",
                "delivered": result.delivered,
                "redirected_to": result.redirected_to,
                "message_id": result.message_id,
            },
        )


@register
class CreateInvoice(Tool):
    name = "create_invoice"
    description = (
        "Create an invoice in QuickBooks Online for a client for legal services. "
        "Tier-2 action — requires approval. Amount is in whole dollars."
    )
    parameters = {
        "type": "object",
        "properties": {
            "client_id": {"type": "integer"},
            "matter_id": {"type": "integer"},
            "amount_dollars": {"type": "number"},
            "description": {"type": "string"},
        },
        "required": ["client_id", "amount_dollars", "description"],
    }
    tier = 2

    def execute(self, args, session_id):
        from hermes.qbo import qbo  # lazy import to avoid boot-time creds check

        with session_scope() as s:
            client = s.get(Client, args["client_id"])
            if not client:
                return ToolResult(ok=False, error=f"no client {args['client_id']}")

            firm_id = client.firm_id
            client_id_local = client.id
            display_name = client.display_name
            email = client.email
            phone = client.phone
            external_customer_id_pre = client.external_customer_id

        # If QBO isn't configured, persist as draft (demo-safe fallback).
        if not qbo.configured:
            with session_scope() as s:
                inv = Invoice(
                    firm_id=firm_id,
                    client_id=client_id_local,
                    matter_id=args.get("matter_id"),
                    amount_cents=int(round(float(args["amount_dollars"]) * 100)),
                    description=args["description"],
                    status="draft",
                )
                s.add(inv)
                s.flush()
                inv_id = inv.id
            return ToolResult(
                ok=True,
                data={
                    "invoice_id": inv_id,
                    "status": "draft",
                    "note": "QBO not configured — invoice saved as local draft",
                },
            )

        result = qbo.create_invoice(
            customer_display_name=display_name,
            customer_email=email,
            customer_phone=phone,
            amount=float(args["amount_dollars"]),
            description=args["description"],
            customer_id=external_customer_id_pre,
        )

        with session_scope() as s:
            client_obj = s.get(Client, client_id_local)
            inv = Invoice(
                firm_id=firm_id,
                client_id=client_id_local,
                matter_id=args.get("matter_id"),
                amount_cents=int(round(float(args["amount_dollars"]) * 100)),
                description=args["description"],
                status="sent" if result.ok else "failed",
                external_invoice_id=result.invoice_id,
                external_invoice_url=result.invoice_url,
            )
            s.add(inv)
            # Remember the QBO customer id on our client record so later
            # invoices reuse it without a round-trip search.
            if result.ok and client_obj is not None and result.customer_id:
                client_obj.external_customer_id = result.customer_id
            s.flush()
            inv_id = inv.id

        if not result.ok:
            return ToolResult(
                ok=False,
                error=result.error or "QBO invoice create failed",
                data={"invoice_id": inv_id, "status": "failed"},
            )

        return ToolResult(
            ok=True,
            data={
                "invoice_id": inv_id,
                "status": "sent",
                "qbo_invoice_id": result.invoice_id,
                "qbo_doc_number": result.doc_number,
                "qbo_invoice_url": result.invoice_url,
            },
        )


# --- Tier 1 read tools for Telegram operator queries ---


@register
class ListPendingApprovals(Tool):
    name = "list_pending_approvals"
    description = (
        "List all Tier-2 actions currently waiting for operator approval. "
        "Use to answer 'what do I need to approve?' or 'what's pending?'"
    )
    parameters = {"type": "object", "properties": {}, "required": []}
    tier = 1

    def execute(self, args, session_id):
        from sqlalchemy import desc
        with session_scope() as s:
            rows = s.scalars(
                select(ApprovalRequest)
                .where(ApprovalRequest.status == "pending")
                .order_by(desc(ApprovalRequest.created_at))
                .limit(20)
            ).all()
            out = []
            for r in rows:
                entry = {
                    "approval_id": r.id,
                    "tool_name": r.tool_name,
                    "created_at": r.created_at.isoformat(),
                    "args": r.tool_args or {},
                }
                if r.tool_name == "send_email":
                    e = s.get(Email, (r.tool_args or {}).get("email_id"))
                    if e:
                        entry["email_to"] = e.to_address
                        entry["email_subject"] = e.subject
                if r.tool_name == "create_invoice":
                    cid = (r.tool_args or {}).get("client_id")
                    c = s.get(Client, cid) if cid else None
                    entry["client_name"] = c.display_name if c else None
                    entry["amount_dollars"] = (r.tool_args or {}).get("amount_dollars")
                out.append(entry)
            return ToolResult(ok=True, data={"approvals": out})


@register
class ListRecentCalls(Tool):
    name = "list_recent_calls"
    description = (
        "List the most recent phone calls handled by the intake system, "
        "newest first. Each call returns caller, matter type, urgency, "
        "and a short summary."
    )
    parameters = {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "Max results (default 5)"}
        },
        "required": [],
    }
    tier = 1

    def execute(self, args, session_id):
        from sqlalchemy import desc
        limit = int(args.get("limit") or 5)
        with session_scope() as s:
            rows = s.scalars(
                select(Call).order_by(desc(Call.started_at)).limit(limit)
            ).all()
            return ToolResult(
                ok=True,
                data={
                    "calls": [
                        {
                            "call_id": c.id,
                            "caller_name": c.caller_name,
                            "caller_phone": c.caller_phone,
                            "matter_type": c.matter_type_guess,
                            "urgency": c.urgency,
                            "summary": c.summary,
                            "started_at": c.started_at.isoformat() if c.started_at else None,
                        }
                        for c in rows
                    ]
                },
            )


@register
class ListRecentInvoices(Tool):
    name = "list_recent_invoices"
    description = (
        "List the most recent invoices drafted by the system, newest first. "
        "Includes QBO invoice number + URL when available."
    )
    parameters = {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "Max results (default 5)"}
        },
        "required": [],
    }
    tier = 1

    def execute(self, args, session_id):
        from sqlalchemy import desc
        limit = int(args.get("limit") or 5)
        with session_scope() as s:
            rows = s.scalars(
                select(Invoice).order_by(desc(Invoice.created_at)).limit(limit)
            ).all()
            out = []
            for i in rows:
                c = s.get(Client, i.client_id) if i.client_id else None
                out.append(
                    {
                        "invoice_id": i.id,
                        "client_name": c.display_name if c else None,
                        "amount_dollars": round(i.amount_cents / 100, 2),
                        "description": i.description,
                        "status": i.status,
                        "qbo_invoice_id": i.external_invoice_id,
                        "qbo_invoice_url": i.external_invoice_url,
                    }
                )
            return ToolResult(ok=True, data={"invoices": out})


@register
class SummarizeDay(Tool):
    name = "summarize_day"
    description = (
        "Summarize activity in the last 24 hours: how many calls came in, "
        "how many emails drafted/sent, how many invoices, what's pending."
    )
    parameters = {"type": "object", "properties": {}, "required": []}
    tier = 1

    def execute(self, args, session_id):
        from datetime import datetime, timedelta
        cutoff = datetime.utcnow() - timedelta(hours=24)
        with session_scope() as s:
            calls = s.scalars(select(Call).where(Call.started_at >= cutoff)).all()
            emails = s.scalars(select(Email).where(Email.created_at >= cutoff)).all()
            invoices = s.scalars(
                select(Invoice).where(Invoice.created_at >= cutoff)
            ).all()
            pending = s.scalars(
                select(ApprovalRequest).where(ApprovalRequest.status == "pending")
            ).all()
            return ToolResult(
                ok=True,
                data={
                    "window_hours": 24,
                    "calls_handled": len(calls),
                    "emails_drafted": len(emails),
                    "emails_sent": sum(1 for e in emails if e.status == "sent"),
                    "invoices_created": len(invoices),
                    "approvals_pending": len(pending),
                    "pending_tool_names": sorted({p.tool_name for p in pending}),
                },
            )


@register
class QBOCustomerLookup(Tool):
    name = "qbo_customer_lookup"
    description = (
        "Look up customers in QuickBooks Online by name fragment. Returns "
        "QBO customer IDs and contact info."
    )
    parameters = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }
    tier = 1

    def execute(self, args, session_id):
        from hermes.qbo import qbo
        if not qbo.configured:
            return ToolResult(ok=False, error="QBO not configured")
        q = (args.get("query") or "").replace("'", "\\'")
        resp = qbo._request(
            "GET",
            f"/v3/company/{qbo._realm_id}/query",
            params={
                "query": f"select * from Customer where DisplayName like '%{q}%' maxresults 10",
                "minorversion": "73",
            },
        )
        if resp.status_code != 200:
            return ToolResult(ok=False, error=f"QBO error {resp.status_code}: {resp.text[:300]}")
        rows = resp.json().get("QueryResponse", {}).get("Customer", [])
        return ToolResult(
            ok=True,
            data={
                "customers": [
                    {
                        "qbo_customer_id": str(c["Id"]),
                        "display_name": c.get("DisplayName"),
                        "email": (c.get("PrimaryEmailAddr") or {}).get("Address"),
                        "phone": (c.get("PrimaryPhone") or {}).get("FreeFormNumber"),
                        "balance": c.get("Balance"),
                    }
                    for c in rows
                ]
            },
        )


@register
class QBOInvoiceStatus(Tool):
    name = "qbo_invoice_status"
    description = (
        "Check the status of a QuickBooks Online invoice: total, balance, "
        "paid/unpaid, due date."
    )
    parameters = {
        "type": "object",
        "properties": {"qbo_invoice_id": {"type": "string"}},
        "required": ["qbo_invoice_id"],
    }
    tier = 1

    def execute(self, args, session_id):
        from hermes.qbo import qbo
        if not qbo.configured:
            return ToolResult(ok=False, error="QBO not configured")
        inv_id = str(args["qbo_invoice_id"])
        resp = qbo._request(
            "GET",
            f"/v3/company/{qbo._realm_id}/invoice/{inv_id}",
            params={"minorversion": "73"},
        )
        if resp.status_code != 200:
            return ToolResult(ok=False, error=f"QBO error {resp.status_code}")
        inv = resp.json()["Invoice"]
        total = float(inv.get("TotalAmt") or 0)
        balance = float(inv.get("Balance") or 0)
        return ToolResult(
            ok=True,
            data={
                "qbo_invoice_id": inv_id,
                "doc_number": inv.get("DocNumber"),
                "customer": (inv.get("CustomerRef") or {}).get("name"),
                "total_dollars": total,
                "balance_dollars": balance,
                "paid": balance == 0 and total > 0,
                "due_date": inv.get("DueDate"),
                "invoice_url": f"https://sandbox.qbo.intuit.com/app/invoice?txnId={inv_id}",
            },
        )


@register
class ApproveRequest(Tool):
    name = "approve_request"
    description = (
        "Approve a pending Tier-2 action by its approval_id. This will "
        "execute the queued tool (send the email, create the invoice, etc.) "
        "immediately. Use only when the operator explicitly confirms."
    )
    parameters = {
        "type": "object",
        "properties": {"approval_id": {"type": "integer"}},
        "required": ["approval_id"],
    }
    tier = 1  # the user IS the approver — no further gate.

    def execute(self, args, session_id):
        from datetime import datetime
        aid = args.get("approval_id")
        with session_scope() as s:
            req = s.get(ApprovalRequest, aid)
            if not req:
                return ToolResult(ok=False, error=f"no approval #{aid}")
            if req.status != "pending":
                return ToolResult(
                    ok=False,
                    error=f"approval #{aid} is {req.status}, not pending",
                )
            tool_name = req.tool_name
            tool_args = req.tool_args or {}

        tool = get_tool(tool_name)
        if tool is None:
            return ToolResult(ok=False, error=f"unknown tool {tool_name}")
        result = tool.execute(tool_args, session_id)

        with session_scope() as s:
            req = s.get(ApprovalRequest, aid)
            req.status = "executed" if result.ok else "rejected"
            req.result = {"ok": result.ok, "data": result.data, "error": result.error}
            req.decided_at = datetime.utcnow()
            req.decided_by = "telegram"

        return ToolResult(
            ok=result.ok,
            error=result.error,
            data={
                "approval_id": aid,
                "tool_name": tool_name,
                "executed": result.ok,
                "result": result.data,
            },
        )


@register
class RejectRequest(Tool):
    name = "reject_request"
    description = "Reject a pending Tier-2 approval by its approval_id."
    parameters = {
        "type": "object",
        "properties": {"approval_id": {"type": "integer"}},
        "required": ["approval_id"],
    }
    tier = 1

    def execute(self, args, session_id):
        from datetime import datetime
        aid = args.get("approval_id")
        with session_scope() as s:
            req = s.get(ApprovalRequest, aid)
            if not req:
                return ToolResult(ok=False, error=f"no approval #{aid}")
            if req.status != "pending":
                return ToolResult(
                    ok=False, error=f"approval #{aid} is {req.status}, not pending"
                )
            req.status = "rejected"
            req.decided_at = datetime.utcnow()
            req.decided_by = "telegram"
        return ToolResult(ok=True, data={"approval_id": aid, "status": "rejected"})


# --- audit + approval helpers ---


def log_event(
    session_id: str,
    event_type: str,
    *,
    tool_name: str | None = None,
    tool_args: dict | None = None,
    tool_result: dict | None = None,
    llm_source: str | None = None,
    note: str | None = None,
) -> None:
    with session_scope() as s:
        s.add(
            AuditLog(
                session_id=session_id,
                event_type=event_type,
                tool_name=tool_name,
                tool_args=tool_args,
                tool_result=tool_result,
                llm_source=llm_source,
                note=note,
            )
        )


def enqueue_for_approval(
    session_id: str,
    tool_name: str,
    tool_args: dict,
    rationale: str | None = None,
) -> int:
    with session_scope() as s:
        req = ApprovalRequest(
            session_id=session_id,
            tool_name=tool_name,
            tool_args=tool_args,
            rationale=rationale,
            status="pending",
        )
        s.add(req)
        s.flush()
        req_id = req.id

        # Build a preview for the Telegram notification while the session is open.
        preview_bits: list[str] = []
        if tool_name == "send_email":
            e = s.get(Email, (tool_args or {}).get("email_id"))
            if e:
                preview_bits.append(f"to: {e.to_address}")
                preview_bits.append(f"subject: {e.subject}")
        elif tool_name == "create_invoice":
            cid = (tool_args or {}).get("client_id")
            c = s.get(Client, cid) if cid else None
            if c:
                preview_bits.append(f"client: {c.display_name}")
            amt = (tool_args or {}).get("amount_dollars")
            if amt is not None:
                preview_bits.append(f"amount: ${float(amt):.2f}")

    # Outside the session — lazy import to avoid any import-time cycles.
    try:
        from hermes.telegram_bot import notify_allowlist
        preview = " · ".join(preview_bits) if preview_bits else ""
        msg = (
            f"📋 Approval needed #{req_id}: {tool_name}"
            + (f"\n{preview}" if preview else "")
            + f"\n\nReply to approve: 'approve {req_id}'  ·  reject: 'reject {req_id}'"
        )
        notify_allowlist(msg)
    except Exception:
        pass  # never let notification failure break the queue write

    return req_id
