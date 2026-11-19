from datetime import datetime, timezone
import uuid

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class RecoveryPolicy(Base):
    __tablename__ = "recovery_policies"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("merchants.id"),
        nullable=False,
        index=True,
    )

    max_incentive: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
    )

    max_exposure: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
    )

    allowed_actions: Mapped[list] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
    )

    approval_threshold: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=0.5,
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

    merchant = relationship("Merchant", backref="recovery_policies")

    __table_args__ = (
        CheckConstraint("max_incentive >= 0", name="chk_max_incentive_non_negative"),
        CheckConstraint("max_exposure >= 0", name="chk_max_exposure_non_negative"),
    )
