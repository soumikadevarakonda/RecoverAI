from datetime import datetime, timezone
import uuid

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    JSON,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.db.base import Base


class RecoveryAttempt(Base):
    __tablename__ = "recovery_attempts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    recovery_id: Mapped[str] = mapped_column(
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

    payment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("payments.id"),
        nullable=True,
        index=True,
    )

    selected_action: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )

    incentive_amount: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
    )

    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="pending",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    recovered_amount: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
    )

    resulting_payment_id: Mapped[str] = mapped_column(
        String(100),
        nullable=True,
    )

    payment_link_id: Mapped[str] = mapped_column(
        String(100),
        nullable=True,
        unique=True,
        index=True,
    )

    short_url: Mapped[str] = mapped_column(
        String(500),
        nullable=True,
    )

    decision_evidence: Mapped[dict] = mapped_column(
        JSON,
        nullable=True,
    )

    merchant = relationship("Merchant", backref="recovery_attempts")
    incident = relationship("Incident", backref="recovery_attempts")
    payment = relationship("Payment", backref="recovery_attempts")

    __table_args__ = (
        CheckConstraint("incentive_amount >= 0", name="chk_incentive_amount_non_negative"),
        CheckConstraint("recovered_amount >= 0", name="chk_recovered_amount_non_negative"),
    )

    @validates("status")
    def validate_status(self, key, value):
        current_status = getattr(self, "status", None)
        if current_status == "recovered" and value != "recovered":
            raise ValueError("A recovery attempt must never transition backwards from 'recovered' to another state")
        return value
