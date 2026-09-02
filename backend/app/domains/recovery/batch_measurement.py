from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.models.payment import Payment
from app.models.incident import Incident
from app.models.recovery_attempt import RecoveryAttempt


class ActionMeasurement(BaseModel):
    action: str = Field(..., description="The recovery action")
    eligible_recovery_attempts: int = Field(..., description="Eligible attempts for this action")
    completed_recovery_attempts: int = Field(..., description="Completed attempts for this action")
    recovered_attempts: int = Field(..., description="Successfully recovered attempts for this action")
    actual_recovered_amount: int = Field(..., description="Actual recovered amount in minor units")
    intervention_cost: int = Field(..., description="Total intervention cost in minor units")
    net_recovered_amount: int = Field(..., description="Net recovered amount in minor units")
    gross_recovery_rate: float = Field(..., description="Gross recovery rate (recovered attempts / eligible attempts or amount / risk)")


class BatchMeasurement(BaseModel):
    total_transactions_analyzed: int = Field(..., description="Total transactions analyzed")
    failed_transactions: int = Field(..., description="Total failed transactions")
    revenue_at_risk: int = Field(..., description="Total revenue at risk in minor units")
    eligible_recovery_attempts: int = Field(..., description="Total eligible recovery attempts")
    completed_recovery_attempts: int = Field(..., description="Total completed recovery attempts")
    recovered_attempts: int = Field(..., description="Total successfully recovered attempts")
    actual_recovered_amount: int = Field(..., description="Actual recovered amount in minor units")
    gross_recovery_rate: float = Field(..., description="Gross recovery rate (recovered / risk)")
    intervention_cost: int = Field(..., description="Total intervention cost in minor units")
    net_recovered_amount: int = Field(..., description="Net recovered amount in minor units")
    action_breakdowns: dict[str, ActionMeasurement] = Field(..., description="Breakdown of measurements by recovery action")


def calculate_batch_measurement(
    db: Session,
    merchant_id: UUID,
    start_time: datetime,
    end_time: datetime,
) -> BatchMeasurement:
    """
    Calculates measured recovery results for a defined batch/time window and merchant.
    Strictly isolated to the merchant and the defined time window [start_time, end_time].
    """
    # 1. Total transactions analyzed (all Payments in time range)
    total_tx = db.scalar(
        select(func.count(Payment.id)).where(
            Payment.merchant_id == merchant_id,
            Payment.created_at >= start_time,
            Payment.created_at <= end_time,
        )
    ) or 0

    # 2. Failed transactions
    failed_tx = db.scalar(
        select(func.count(Payment.id)).where(
            Payment.merchant_id == merchant_id,
            Payment.status == "failed",
            Payment.created_at >= start_time,
            Payment.created_at <= end_time,
        )
    ) or 0

    # 3. Revenue at risk (sum of revenue_at_risk from Incidents in time range)
    raw_revenue_risk = db.scalar(
        select(func.sum(Incident.revenue_at_risk)).where(
            Incident.merchant_id == merchant_id,
            Incident.created_at >= start_time,
            Incident.created_at <= end_time,
        )
    )
    revenue_risk = int(raw_revenue_risk) if raw_revenue_risk is not None else 0

    # 4. Fetch all RecoveryAttempts within the time range
    attempts = db.scalars(
        select(RecoveryAttempt).where(
            RecoveryAttempt.merchant_id == merchant_id,
            RecoveryAttempt.created_at >= start_time,
            RecoveryAttempt.created_at <= end_time,
        )
    ).all()

    eligible_attempts = len(attempts)
    completed_attempts = sum(1 for a in attempts if a.status in ["recovered", "failed", "expired"])
    recovered_attempts = sum(1 for a in attempts if a.status == "recovered")
    actual_recovered = sum(a.recovered_amount for a in attempts if a.status == "recovered")
    intervention_cost = sum(a.incentive_amount for a in attempts)
    net_recovered = actual_recovered - intervention_cost

    gross_rate = float(actual_recovered) / float(revenue_risk) if revenue_risk > 0 else 0.0

    # Calculate breakdowns by action
    actions = ["retry", "grace_period", "incentive", "ops_review"]
    # Also add any other action found in attempts
    for a in attempts:
        if a.selected_action not in actions:
            actions.append(a.selected_action)

    action_breakdowns = {}
    for action in actions:
        action_attempts = [a for a in attempts if a.selected_action == action]
        act_eligible = len(action_attempts)
        act_completed = sum(1 for a in action_attempts if a.status in ["recovered", "failed", "expired"])
        act_recovered = sum(1 for a in action_attempts if a.status == "recovered")
        act_recovered_amount = sum(a.recovered_amount for a in action_attempts if a.status == "recovered")
        act_cost = sum(a.incentive_amount for a in action_attempts)
        act_net = act_recovered_amount - act_cost

        # For action-level breakdown, rate is recovered_attempts / eligible_attempts if eligible > 0 else 0.0
        act_rate = float(act_recovered) / float(act_eligible) if act_eligible > 0 else 0.0

        # Only include actions in breakdown if they have at least 1 attempt or are one of the defaults
        if act_eligible > 0 or action in ["retry", "grace_period", "incentive", "ops_review"]:
            action_breakdowns[action] = ActionMeasurement(
                action=action,
                eligible_recovery_attempts=act_eligible,
                completed_recovery_attempts=act_completed,
                recovered_attempts=act_recovered,
                actual_recovered_amount=act_recovered_amount,
                intervention_cost=act_cost,
                net_recovered_amount=act_net,
                gross_recovery_rate=act_rate,
            )

    return BatchMeasurement(
        total_transactions_analyzed=total_tx,
        failed_transactions=failed_tx,
        revenue_at_risk=revenue_risk,
        eligible_recovery_attempts=eligible_attempts,
        completed_recovery_attempts=completed_attempts,
        recovered_attempts=recovered_attempts,
        actual_recovered_amount=actual_recovered,
        gross_recovery_rate=gross_rate,
        intervention_cost=intervention_cost,
        net_recovered_amount=net_recovered,
        action_breakdowns=action_breakdowns,
    )

