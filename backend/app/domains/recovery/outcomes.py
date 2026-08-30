from uuid import UUID
from pydantic import BaseModel, Field
from sqlalchemy import func, select, case
from sqlalchemy.orm import Session

from app.models.recovery_attempt import RecoveryAttempt
from app.models.incident import Incident


class RecoveryPerformance(BaseModel):
    total_attempts: int = Field(..., description="Total completed attempts")
    recovered_attempts: int = Field(..., description="Total successfully recovered attempts")
    observed_recovery_rate: float = Field(..., description="Observed recovery rate (recovered / total)")
    total_revenue_at_risk: int = Field(..., description="Total revenue at risk in minor units")
    total_recovered_amount: int = Field(..., description="Total actually recovered amount in minor units")


def calculate_recovery_performance(
    db: Session,
    merchant_id: UUID,
    method: str | None = None,
    bank: str | None = None,
    error_code: str | None = None,
    selected_action: str | None = None,
) -> RecoveryPerformance:
    stmt = (
        select(
            func.count(RecoveryAttempt.id).label("total_attempts"),
            func.sum(
                case(
                    (RecoveryAttempt.status == "recovered", 1),
                    else_=0
                )
            ).label("recovered_attempts"),
            func.sum(Incident.revenue_at_risk).label("total_revenue_at_risk"),
            func.sum(RecoveryAttempt.recovered_amount).label("total_recovered_amount"),
        )
        .join(Incident, RecoveryAttempt.incident_id == Incident.id)
        .where(
            RecoveryAttempt.merchant_id == merchant_id,
            RecoveryAttempt.status.in_(["recovered", "failed", "expired"]),
        )
    )

    if method is not None:
        stmt = stmt.where(Incident.method == method)
    if bank is not None:
        stmt = stmt.where(Incident.bank == bank)
    if error_code is not None:
        stmt = stmt.where(Incident.error_code == error_code)
    if selected_action is not None:
        stmt = stmt.where(RecoveryAttempt.selected_action == selected_action)

    result = db.execute(stmt).first()

    total_attempts = 0
    recovered_attempts = 0
    total_revenue_at_risk = 0
    total_recovered_amount = 0

    if result:
        total_attempts = result.total_attempts or 0
        recovered_attempts = result.recovered_attempts or 0
        total_revenue_at_risk = result.total_revenue_at_risk or 0
        total_recovered_amount = result.total_recovered_amount or 0

    observed_recovery_rate = 0.0
    if total_attempts > 0:
        observed_recovery_rate = float(recovered_attempts) / total_attempts

    return RecoveryPerformance(
        total_attempts=total_attempts,
        recovered_attempts=recovered_attempts,
        observed_recovery_rate=observed_recovery_rate,
        total_revenue_at_risk=total_revenue_at_risk,
        total_recovered_amount=total_recovered_amount,
    )

