"""Seed demo customers into the QuickBooks Online sandbox.

Creates three records:
  - Marshall Goodwin        — also exists in our local SQLite (demo of
                               a repeat client where both worlds already know
                               them; tests the dedup guard)
  - Sierra Construction LLC — same as above (business variant)
  - Samantha Reyes          — does NOT exist in our local SQLite; this is
                               the "QBO match wins" demo moment —
                               find_qbo_customer_by_contact returns a hit
                               even though lookup_client misses

Idempotent: QBOClient.find_or_create_customer reuses existing records when
DisplayName matches.
"""

from __future__ import annotations

from hermes.qbo import qbo


SEED = [
    {
        "display_name": "Marshall Goodwin",
        "email": "marshall.goodwin@example.com",
        "phone": "+1-415-555-0142",
    },
    {
        "display_name": "Sierra Construction LLC",
        "email": "ap@sierraconstruction.example",
        "phone": "+1-415-555-0118",
    },
    {
        "display_name": "Samantha Reyes",
        "email": "samantha.reyes@example.com",
        "phone": "+1-415-555-0199",
    },
]


def seed() -> None:
    if not qbo.configured:
        print("QBO not configured; aborting")
        return
    for c in SEED:
        try:
            row = qbo.find_or_create_customer(
                c["display_name"], email=c["email"], phone=c["phone"]
            )
            print(f"  {c['display_name']:30}  → QBO id {row['Id']}")
        except Exception as e:
            print(f"  {c['display_name']:30}  FAILED: {e}")


if __name__ == "__main__":
    seed()
