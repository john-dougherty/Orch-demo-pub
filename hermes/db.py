"""SQLAlchemy models + session helpers.

Schema is tuned for the demo (one fake firm, a handful of clients/matters) but
structured so the production rebuild can lift it nearly as-is.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    String,
    Text,
    create_engine,
    func,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

from hermes.config import settings


class Base(DeclarativeBase):
    pass


def _now() -> datetime:
    return datetime.utcnow()


class Firm(Base):
    __tablename__ = "firms"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    clients: Mapped[list["Client"]] = relationship(back_populates="firm")


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(primary_key=True)
    firm_id: Mapped[int] = mapped_column(ForeignKey("firms.id"))
    display_name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20))  # "individual" | "business"
    email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_customer_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    firm: Mapped[Firm] = relationship(back_populates="clients")
    matters: Mapped[list["Matter"]] = relationship(back_populates="client")


class Matter(Base):
    __tablename__ = "matters"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    title: Mapped[str] = mapped_column(String(300))
    matter_type: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(30), default="intake")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    client: Mapped[Client] = relationship(back_populates="matters")


class Call(Base):
    __tablename__ = "calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    firm_id: Mapped[int] = mapped_column(ForeignKey("firms.id"))
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id"), nullable=True)
    caller_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    caller_phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    twilio_sid: Mapped[str | None] = mapped_column(String(80), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    matter_type_guess: Mapped[str | None] = mapped_column(String(80), nullable=True)
    urgency: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="in_progress")
    follow_up_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    captured_email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    turns: Mapped[int] = mapped_column(default=0)


class Email(Base):
    __tablename__ = "emails"

    id: Mapped[int] = mapped_column(primary_key=True)
    firm_id: Mapped[int] = mapped_column(ForeignKey("firms.id"))
    direction: Mapped[str] = mapped_column(String(10))  # "inbound" | "outbound"
    to_address: Mapped[str] = mapped_column(String(200))
    from_address: Mapped[str] = mapped_column(String(200))
    subject: Mapped[str] = mapped_column(String(500))
    body: Mapped[str] = mapped_column(Text)
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id"), nullable=True)
    matter_id: Mapped[int | None] = mapped_column(ForeignKey("matters.id"), nullable=True)
    related_call_id: Mapped[int | None] = mapped_column(ForeignKey("calls.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="draft")
    # "draft" | "queued_for_approval" | "sent" | "rejected"
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(primary_key=True)
    firm_id: Mapped[int] = mapped_column(ForeignKey("firms.id"))
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    matter_id: Mapped[int | None] = mapped_column(ForeignKey("matters.id"), nullable=True)
    amount_cents: Mapped[int] = mapped_column()
    description: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="draft")
    # "draft" | "queued_for_approval" | "sent" | "rejected"
    external_invoice_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    external_invoice_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ApprovalRequest(Base):
    __tablename__ = "approval_queue"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64))
    tool_name: Mapped[str] = mapped_column(String(80))
    tool_args: Mapped[dict] = mapped_column(JSON)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    # "pending" | "approved" | "rejected" | "executed"
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(100), nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64))
    event_type: Mapped[str] = mapped_column(String(40))
    tool_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    tool_args: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    tool_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    llm_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_now, server_default=func.current_timestamp()
    )


# --- engine / session ---

def _sqlite_path() -> Path:
    # Ensures data dir exists before SQLAlchemy tries to open the file.
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings.data_dir / "orchestrator.db"


engine = create_engine(
    settings.database_url.replace("./data/orchestrator.db", str(_sqlite_path())),
    echo=False,
    future=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(engine)


@contextmanager
def session_scope() -> Iterator[Session]:
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
