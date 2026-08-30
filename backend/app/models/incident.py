from datetime import datetime, timezone
import uuid

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Incident(Base):
    __tablename__ = "incidents"

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

    # Cohort Dimensions
    method: Mapped[str] = mapped_column(String(50), nullable=False)
    bank: Mapped[str] = mapped_column(String(100), nullable=False)
    error_code: Mapped[str] = mapped_column(String(100), nullable=False)
    error_step: Mapped[str] = mapped_column(String(100), nullable=False)

    # Current Window Metrics
    current_total_count: Mapped[int] = mapped_column(Integer, nullable=False)
    current_failed_count: Mapped[int] = mapped_column(Integer, nullable=False)
    current_failure_rate: Mapped[float] = mapped_column(Float, nullable=False)
    current_total_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    current_failed_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # Baseline Window Metrics
    baseline_total_count: Mapped[int] = mapped_column(Integer, nullable=False)
    baseline_failed_count: Mapped[int] = mapped_column(Integer, nullable=False)
    baseline_failure_rate: Mapped[float] = mapped_column(Float, nullable=False)
    baseline_total_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    baseline_failed_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # Degradation
    absolute_rate_increase: Mapped[float] = mapped_column(Float, nullable=False)
    relative_degradation: Mapped[float] = mapped_column(Float, nullable=False)

    # Financial Impact
    revenue_at_risk: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # Time Windows
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    baseline_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    baseline_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    status: Mapped[str] = mapped_column(String(50), default="detected", nullable=False)

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
