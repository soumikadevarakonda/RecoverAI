import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    JSON,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class RecoveryCampaign(Base):
    __tablename__ = "recovery_campaigns"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    campaign_id: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        unique=True,
        index=True,
    )

    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("merchants.id"),
        nullable=False,
        index=True,
    )

    incident_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("incidents.id"),
        nullable=False,
        index=True,
    )

    selected_action: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="pending",
    )  # pending, approved, executing, completed, cancelled

    target_payment_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )

    total_revenue_at_risk: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
    )

    per_attempt_incentive: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
    )

    total_incentive_cost: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
    )

    decision_evidence: Mapped[dict | None] = mapped_column(
        JSON,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    merchant = relationship("Merchant", backref="recovery_campaigns")
    incident = relationship("Incident", backref="recovery_campaigns")

    __table_args__ = (
        CheckConstraint("target_payment_count >= 0", name="chk_campaign_target_count_non_negative"),
        CheckConstraint("total_revenue_at_risk >= 0", name="chk_campaign_revenue_risk_non_negative"),
        CheckConstraint("per_attempt_incentive >= 0", name="chk_campaign_per_attempt_incentive_non_negative"),
        CheckConstraint("total_incentive_cost >= 0", name="chk_campaign_total_incentive_non_negative"),
        UniqueConstraint("incident_id", name="uq_recovery_campaigns_incident_id"),
    )

