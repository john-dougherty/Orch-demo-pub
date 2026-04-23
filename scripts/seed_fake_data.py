"""Seed the demo database with Oak & Partners fixtures.

8 clients spanning the matter types the intake agent is most likely to see,
each with 1-2 matters. Deterministic: re-running replaces existing fixtures.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete

from hermes.db import Client, Firm, Matter, init_db, session_scope


FIRM_NAME = "Oak & Partners"


CLIENTS: list[dict] = [
    {
        "display_name": "Marshall Goodwin",
        "kind": "individual",
        "email": "marshall.goodwin@example.com",
        "phone": "+1-415-555-0142",
        "notes": "Referred by Laura Benoit. Needs help with a non-compete.",
    },
    {
        "display_name": "Sierra Construction LLC",
        "kind": "business",
        "email": "ap@sierraconstruction.example",
        "phone": "+1-415-555-0118",
        "notes": "Repeat client — ongoing subcontractor agreements.",
    },
    {
        "display_name": "Dr. Priya Raman",
        "kind": "individual",
        "email": "praman@ramanderm.example",
        "phone": "+1-628-555-0177",
        "notes": "Solo practitioner; incorporated Jan. Routine corp governance.",
    },
    {
        "display_name": "Brewhaven Cafe Inc.",
        "kind": "business",
        "email": "owner@brewhaven.example",
        "phone": "+1-510-555-0120",
        "notes": "Landlord/tenant dispute with building owner.",
    },
    {
        "display_name": "Jacob Okafor",
        "kind": "individual",
        "email": "jokafor@example.com",
        "phone": "+1-415-555-0191",
        "notes": "Estate plan refresh — two minor children.",
    },
    {
        "display_name": "Halide Labs Inc.",
        "kind": "business",
        "email": "legal@halidelabs.example",
        "phone": "+1-650-555-0134",
        "notes": "Seed-stage; needs IP assignment cleanup pre-Series A.",
    },
    {
        "display_name": "Luis Ibarra",
        "kind": "individual",
        "email": "luis.ibarra@example.com",
        "phone": "+1-707-555-0126",
        "notes": "Employment termination; wrongful termination consult.",
    },
    {
        "display_name": "Monarch Florists LLC",
        "kind": "business",
        "email": "hello@monarchflorists.example",
        "phone": "+1-415-555-0163",
        "notes": "New entity formation — partnership conversion to LLC.",
    },
]


MATTERS: list[dict] = [
    {
        "client_ref": "Marshall Goodwin",
        "title": "Non-compete enforceability review",
        "matter_type": "employment",
        "status": "active",
        "opened_days_ago": 12,
        "description": "Review CA-based non-compete; prior employer threatening litigation.",
    },
    {
        "client_ref": "Sierra Construction LLC",
        "title": "Subcontractor MSA template refresh",
        "matter_type": "contract_review",
        "status": "active",
        "opened_days_ago": 4,
    },
    {
        "client_ref": "Sierra Construction LLC",
        "title": "Lien dispute — 2411 Harrison project",
        "matter_type": "litigation",
        "status": "active",
        "opened_days_ago": 28,
    },
    {
        "client_ref": "Dr. Priya Raman",
        "title": "Annual corp maintenance + minutes",
        "matter_type": "corporate_formation",
        "status": "active",
        "opened_days_ago": 67,
    },
    {
        "client_ref": "Brewhaven Cafe Inc.",
        "title": "Commercial lease dispute",
        "matter_type": "landlord_tenant",
        "status": "active",
        "opened_days_ago": 19,
    },
    {
        "client_ref": "Jacob Okafor",
        "title": "Will + trust refresh",
        "matter_type": "estate_planning",
        "status": "intake",
        "opened_days_ago": 3,
    },
    {
        "client_ref": "Halide Labs Inc.",
        "title": "IP assignment cleanup",
        "matter_type": "ip",
        "status": "active",
        "opened_days_ago": 41,
    },
    {
        "client_ref": "Luis Ibarra",
        "title": "Wrongful termination consult",
        "matter_type": "employment",
        "status": "intake",
        "opened_days_ago": 6,
    },
    {
        "client_ref": "Monarch Florists LLC",
        "title": "Partnership-to-LLC conversion",
        "matter_type": "corporate_formation",
        "status": "active",
        "opened_days_ago": 22,
    },
    {
        "client_ref": "Halide Labs Inc.",
        "title": "Employee IP assignment template",
        "matter_type": "ip",
        "status": "active",
        "opened_days_ago": 14,
    },
]


def seed() -> None:
    init_db()
    with session_scope() as s:
        # wipe in FK-safe order
        from hermes.db import (
            ApprovalRequest,
            AuditLog,
            Call,
            Email,
            Invoice,
        )

        s.execute(delete(ApprovalRequest))
        s.execute(delete(AuditLog))
        s.execute(delete(Email))
        s.execute(delete(Invoice))
        s.execute(delete(Call))
        s.execute(delete(Matter))
        s.execute(delete(Client))
        s.execute(delete(Firm))
        s.flush()

        firm = Firm(name=FIRM_NAME)
        s.add(firm)
        s.flush()

        clients_by_name: dict[str, Client] = {}
        for c in CLIENTS:
            obj = Client(
                firm_id=firm.id,
                display_name=c["display_name"],
                kind=c["kind"],
                email=c.get("email"),
                phone=c.get("phone"),
                notes=c.get("notes"),
            )
            s.add(obj)
            s.flush()
            clients_by_name[c["display_name"]] = obj

        for m in MATTERS:
            opened = datetime.utcnow() - timedelta(days=m.get("opened_days_ago", 0))
            s.add(
                Matter(
                    client_id=clients_by_name[m["client_ref"]].id,
                    title=m["title"],
                    matter_type=m["matter_type"],
                    status=m.get("status", "intake"),
                    description=m.get("description"),
                    opened_at=opened,
                )
            )

    print(f"Seeded firm '{FIRM_NAME}' with {len(CLIENTS)} clients and {len(MATTERS)} matters.")


if __name__ == "__main__":
    seed()
