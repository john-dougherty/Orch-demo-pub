"""Tool registry for the agent loop.

Each tool declares its JSON-schema parameters, its tier (1=auto, 2=approval,
3=blocked), and an execute() that performs the action against the DB or an
external service.

Tier-2 tools short-circuit in the agent loop: the agent emits the tool call,
the loop enqueues it on the approval queue, and execution waits on a human.
"""

from __future__ import annotations

import json
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


@register
class CreateClient(Tool):
    name = "create_client"
    description = (
        "Create a new client record. Use ONLY after lookup_client returns zero "
        "matches. Returns the new client_id for use by subsequent tools."
    )
    parameters = {
        "type": "object",
        "properties": {
            "display_name": {"type": "string"},
            "kind": {"type": "string", "enum": ["individual", "business"]},
            "email": {"type": "string"},
            "phone": {"type": "string"},
            "notes": {"type": "string"},
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
            )
            s.add(client)
            s.flush()
            return ToolResult(
                ok=True,
                data={"client_id": client.id, "display_name": client.display_name},
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
        # Tier-2 execution runs ONLY after approval. The loop normally halts
        # before reaching here; this path is invoked by the approval handler.
        with session_scope() as s:
            email = s.get(Email, args["email_id"])
            if not email:
                return ToolResult(ok=False, error=f"no email {args['email_id']}")
            # Actual SMTP/Gmail send happens in a later task. For now, mark sent.
            email.status = "sent"
            from datetime import datetime as _dt
            email.sent_at = _dt.utcnow()
            s.flush()
            return ToolResult(
                ok=True,
                data={"email_id": email.id, "status": "sent", "note": "stub send"},
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
            external_customer_id = client.external_customer_id

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
        return req.id
