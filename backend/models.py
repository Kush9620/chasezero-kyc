from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class KycCase(Base):
    __tablename__ = "kyc_cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    case_code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    client_name: Mapped[str] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(String(64), default="Missing_Docs")
    days_open: Mapped[int] = mapped_column(Integer, default=14)
    last_contact_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    days_since_contact: Mapped[float] = mapped_column(Float, default=0.0)
    sla_breached: Mapped[bool] = mapped_column(Boolean, default=False)
    sla_delay_days: Mapped[float] = mapped_column(Float, default=0.0)
    chase_round_count: Mapped[int] = mapped_column(Integer, default=1)

    lei: Mapped[str] = mapped_column(String(64))
    entity_type: Mapped[str] = mapped_column(String(128))
    jurisdiction: Mapped[str] = mapped_column(String(128))
    desk: Mapped[str] = mapped_column(String(128))
    region: Mapped[str] = mapped_column(String(128))
    address: Mapped[str] = mapped_column(String(255))

    contact_name: Mapped[str] = mapped_column(String(128))
    contact_role: Mapped[str] = mapped_column(String(128))
    contact_email: Mapped[str] = mapped_column(String(255))
    assigned_rm: Mapped[str] = mapped_column(String(128), default="Sarah Jenkins")
    facility_value: Mapped[str] = mapped_column(String(64))
    relationship_notes: Mapped[str] = mapped_column(Text, default="")
    target_deadline_label: Mapped[str] = mapped_column(String(64), default="November 1st")

    current_draft_subject: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    current_draft_body: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    critic_passed: Mapped[bool] = mapped_column(Boolean, default=False)
    critic_score: Mapped[int] = mapped_column(Integer, default=0)
    critic_feedback: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    critic_checks_json: Mapped[str] = mapped_column(Text, default="[]")
    workflow_iterations: Mapped[int] = mapped_column(Integer, default=0)
    audit_hash: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    documents: Mapped[List["KycDocument"]] = relationship(
        "KycDocument",
        back_populates="kyc_case",
        cascade="all, delete-orphan",
        order_by="KycDocument.id",
    )
    actions: Mapped[List["AgentAction"]] = relationship(
        "AgentAction",
        back_populates="kyc_case",
        cascade="all, delete-orphan",
        order_by="desc(AgentAction.id)",
    )


class KycDocument(Base):
    __tablename__ = "kyc_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    case_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("kyc_cases.id", ondelete="CASCADE"), index=True
    )
    document_name: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(64), default="Missing")
    description: Mapped[str] = mapped_column(Text, default="")
    requirement_meta: Mapped[str] = mapped_column(String(255), default="")
    assigned_to_or_verifier: Mapped[str] = mapped_column(String(255), default="")
    doc_code: Mapped[str] = mapped_column(String(64), default="")

    kyc_case: Mapped["KycCase"] = relationship("KycCase", back_populates="documents")


class AgentAction(Base):
    __tablename__ = "agent_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    case_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("kyc_cases.id", ondelete="CASCADE"), index=True
    )
    action_type: Mapped[str] = mapped_column(String(64), index=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    actor: Mapped[str] = mapped_column(String(128))
    details: Mapped[str] = mapped_column(Text)

    kyc_case: Mapped["KycCase"] = relationship("KycCase", back_populates="actions")
