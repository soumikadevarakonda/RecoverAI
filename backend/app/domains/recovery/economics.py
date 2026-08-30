from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.incident import Incident
from app.models.diagnosis import Diagnosis
from app.models.recovery_policy import RecoveryPolicy
from app.models.recovery_attempt import RecoveryAttempt

DEFAULT_RECOVERY_RATES = {
    "retry": 0.30,
    "grace_period": 0.20,
    "incentive": 0.25,
    "ops_review": 0.05,
}


class ActionEconomics(BaseModel):
    action: str
    expected_recovery_amount: int = Field(..., description="Expected recovery amount in minor units")
    action_cost: int = Field(..., description="Action cost to the merchant in minor units")
    expected_net_recovery_value: int = Field(..., description="Expected net recovery value in minor units")
    expected_recovery_rate: float = Field(..., description="Assumed recovery rate")
    is_eligible: bool = Field(..., description="Whether this action is allowed and eligible")
    reason_ineligible: str | None = Field(None, description="Reason for ineligibility if not eligible")


def evaluate_recovery_economics(
    db: Session,
    incident: Incident,
    diagnosis: Diagnosis,
    policy: RecoveryPolicy,
    rates: dict[str, float] = None,
) -> list[ActionEconomics]:
    if rates is None:
        rates = DEFAULT_RECOVERY_RATES

    revenue_at_risk = incident.revenue_at_risk or 0
    allowed_actions = policy.allowed_actions or []
    candidate_actions = ["retry", "grace_period", "incentive", "ops_review"]

    results = []

    # Fetch active exposure for exposure checks
    active_exposure = db.scalar(
        select(func.sum(RecoveryAttempt.incentive_amount)).where(
            RecoveryAttempt.merchant_id == incident.merchant_id,
            RecoveryAttempt.status.in_(["pending", "approved", "executed"]),
        )
    ) or 0

    for action in candidate_actions:
        is_eligible = True
        reason_ineligible = None
        cost = 0

        # Eligibility checks
        if action == "retry":
            if "retry" not in allowed_actions:
                is_eligible = False
                reason_ineligible = "Action not allowed by merchant policy"
            elif diagnosis.diagnosis_type in [
                "bank-specific degradation",
                "payment-method degradation",
            ]:
                is_eligible = False
                reason_ineligible = f"Action inappropriate for diagnosis type: {diagnosis.diagnosis_type}"

        elif action == "grace_period":
            if "grace_period" not in allowed_actions:
                is_eligible = False
                reason_ineligible = "Action not allowed by merchant policy"

        elif action == "incentive":
            proposed_incentive = 500  # Default deterministic incentive
            cost = proposed_incentive

            if "incentive" not in allowed_actions:
                is_eligible = False
                reason_ineligible = "Action not allowed by merchant policy"
            elif diagnosis.diagnosis_type == "insufficient evidence / unknown":
                is_eligible = False
                reason_ineligible = "Incentive not appropriate for unknown/insufficient evidence diagnosis"
            elif proposed_incentive > policy.max_incentive:
                is_eligible = False
                reason_ineligible = f"Proposed incentive {proposed_incentive} exceeds policy max_incentive {policy.max_incentive}"
            elif (active_exposure + proposed_incentive) > policy.max_exposure:
                is_eligible = False
                reason_ineligible = f"Adding proposed incentive {proposed_incentive} would exceed policy max_exposure {policy.max_exposure} (current active: {active_exposure})"

        elif action == "ops_review":
            # Always eligible as fallback
            pass

        # Calculate values
        rate = rates.get(action, 0.0)
        
        if is_eligible:
            expected_recovery_amount = int(revenue_at_risk * rate)
            expected_net_recovery_value = expected_recovery_amount - cost
        else:
            expected_recovery_amount = 0
            expected_net_recovery_value = 0

        results.append(
            ActionEconomics(
                action=action,
                expected_recovery_amount=expected_recovery_amount,
                action_cost=cost,
                expected_net_recovery_value=expected_net_recovery_value,
                expected_recovery_rate=rate,
                is_eligible=is_eligible,
                reason_ineligible=reason_ineligible,
            )
        )

    return results

