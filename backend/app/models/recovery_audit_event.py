import uuid
from datetime import datetime, timezone
from typing import Any
from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    JSON,
    event,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class RecoveryAuditEvent(Base):
    """
    Append-only immutable audit event ledger documenting the complete lifecycle,
    evidence, decisions, and outcomes for payment recoveries.
    """
    __tablename__ = "recovery_audit_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("merchants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    incident_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("incidents.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("recovery_campaigns.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    recovery_attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("recovery_attempts.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("payments.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    event_type: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
    )

    actor_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="system",
    )  # system, ai, operator, webhook

    actor_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
    )

    previous_state: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
    )

    new_state: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
    )

    reason_code: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )

    explanation: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    evidence: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )

    # Relationships for convenient navigation
    merchant = relationship("Merchant")
    incident = relationship("Incident")
    campaign = relationship("RecoveryCampaign")
    recovery_attempt = relationship("RecoveryAttempt")
    payment = relationship("Payment")

    __table_args__ = (
        Index("ix_recovery_audit_events_merchant_created", "merchant_id", "created_at"),
        Index("ix_recovery_audit_events_campaign_created", "campaign_id", "created_at"),
        Index("ix_recovery_audit_events_attempt_created", "recovery_attempt_id", "created_at"),
        Index("ix_recovery_audit_events_payment_created", "payment_id", "created_at"),
    )


@event.listens_for(RecoveryAuditEvent, "before_update")
def _prevent_audit_update(mapper, connection, target):
    raise ValueError("RecoveryAuditEvent records are append-only and cannot be modified.")


@event.listens_for(RecoveryAuditEvent, "before_delete")
def _prevent_audit_delete(mapper, connection, target):
    raise ValueError("RecoveryAuditEvent records are append-only and cannot be deleted individually.")
