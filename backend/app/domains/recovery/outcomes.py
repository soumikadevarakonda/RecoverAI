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
    evidence_level: str = Field(..., description="Evidence level used for calculation")


def _calculate_metrics(
    db: Session,
    merchant_id: UUID,
    method: str | None = None,
    bank: str | None = None,
    error_code: str | None = None,
    selected_action: str | None = None,
):
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

    return total_attempts, recovered_attempts, total_revenue_at_risk, total_recovered_amount


def calculate_recovery_performance(
    db: Session,
    merchant_id: UUID,
    method: str | None = None,
    bank: str | None = None,
    error_code: str | None = None,
    selected_action: str | None = None,
    min_attempts: int = 1,
) -> RecoveryPerformance:
    # 1. Level 1: exact method + bank + error_code
    if method and bank and error_code:
        total, recovered, revenue, amount = _calculate_metrics(
            db, merchant_id, method, bank, error_code, selected_action
        )
        if total >= min_attempts:
            rate = float(recovered) / total if total > 0 else 0.0
            return RecoveryPerformance(
                total_attempts=total,
                recovered_attempts=recovered,
                observed_recovery_rate=rate,
                total_revenue_at_risk=revenue,
                total_recovered_amount=amount,
                evidence_level="exact_cohort",
            )

    # 2. Level 2: method + error_code
    if method and error_code:
        total, recovered, revenue, amount = _calculate_metrics(
            db, merchant_id, method=method, error_code=error_code, selected_action=selected_action
        )
        if total >= min_attempts:
            rate = float(recovered) / total if total > 0 else 0.0
            return RecoveryPerformance(
                total_attempts=total,
                recovered_attempts=recovered,
                observed_recovery_rate=rate,
                total_revenue_at_risk=revenue,
                total_recovered_amount=amount,
                evidence_level="method_error",
            )

    # 3. Level 3: method
    if method:
        total, recovered, revenue, amount = _calculate_metrics(
            db, merchant_id, method=method, selected_action=selected_action
        )
        if total >= min_attempts:
            rate = float(recovered) / total if total > 0 else 0.0
            return RecoveryPerformance(
                total_attempts=total,
                recovered_attempts=recovered,
                observed_recovery_rate=rate,
                total_revenue_at_risk=revenue,
                total_recovered_amount=amount,
                evidence_level="method",
            )

    # 4. Level 4: global action history
    total, recovered, revenue, amount = _calculate_metrics(
        db, merchant_id, selected_action=selected_action
    )
    if total >= min_attempts:
        rate = float(recovered) / total if total > 0 else 0.0
        return RecoveryPerformance(
            total_attempts=total,
            recovered_attempts=recovered,
            observed_recovery_rate=rate,
            total_revenue_at_risk=revenue,
            total_recovered_amount=amount,
            evidence_level="global",
        )

    # 5. Level 5: no historical evidence
    return RecoveryPerformance(
        total_attempts=0,
        recovered_attempts=0,
        observed_recovery_rate=0.0,
        total_revenue_at_risk=0,
        total_recovered_amount=0,
        evidence_level="none",
    )
