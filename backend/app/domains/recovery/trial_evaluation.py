from datetime import datetime, timezone, timedelta
from uuid import UUID
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.incident import Incident
from app.models.diagnosis import Diagnosis
from app.models.recovery_policy import RecoveryPolicy
from app.models.recovery_attempt import RecoveryAttempt
from app.domains.cohorts.detector import detect_cohort_degradations
from app.domains.diagnosis.service import diagnose_incident
from app.domains.recovery.service import decide_recovery_action
from app.domains.recovery.economics import evaluate_recovery_economics
from app.integrations.llm.provider import LLMProvider


TRIAL_REFERENCE_TIME = datetime(2026, 8, 31, 12, 0, 0, tzinfo=timezone.utc)


class TrialEvaluationResult(BaseModel):
    incident_detected: bool = Field(..., description="Whether a degraded cohort incident was detected")
    incident_id: UUID | None = Field(None, description="The ID of the detected incident if any")
    diagnosis_type: str | None = Field(None, description="The root cause anomaly classification if any")
    selected_action: str | None = Field(None, description="The selected recovery action recommended by the strategist or deterministic logic")
    rate_source: str | None = Field(None, description="Source/evidence level of the recovery rate used")
    expected_net_recovery_value: int | None = Field(None, description="Expected net recovery value in minor units")
    requires_approval: bool | None = Field(None, description="Whether the action requires merchant manual approval")
    execution_permitted: bool | None = Field(None, description="Whether auto-execution of the action is permitted")


def evaluate_trial_scenario(
    db: Session,
    merchant_id: UUID,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    baseline_start: datetime | None = None,
    baseline_end: datetime | None = None,
    llm_provider: LLMProvider | None = None,
) -> TrialEvaluationResult:
    """
    Runs a reproducible end-to-end evaluation pipeline for one merchant and time window.
    Composes existing cohort degradation, diagnosis, economics, policy, and strategist services.
    Does NOT execute real payments.
    """
    if window_end is None:
        window_end = TRIAL_REFERENCE_TIME
    if window_start is None:
        window_start = window_end - timedelta(minutes=30)
    if baseline_end is None:
        baseline_end = window_end - timedelta(hours=1)
    if baseline_start is None:
        baseline_start = window_end - timedelta(hours=2)
    # 1. Run cohort degradation detection
    incidents = detect_cohort_degradations(
        db=db,
        merchant_id=merchant_id,
        window_start=window_start,
        window_end=window_end,
        baseline_start=baseline_start,
        baseline_end=baseline_end,
    )

    if not incidents:
        return TrialEvaluationResult(
            incident_detected=False,
            incident_id=None,
            diagnosis_type=None,
            selected_action=None,
            rate_source=None,
            expected_net_recovery_value=None,
            requires_approval=None,
            execution_permitted=None,
        )

    # Use the first detected incident
    incident = incidents[0]

    # 2. Run diagnosis
    diagnosis = diagnose_incident(db, incident)

    # 3. Load merchant RecoveryPolicy
    policy = db.scalar(
        select(RecoveryPolicy).where(RecoveryPolicy.merchant_id == merchant_id)
    )
    if not policy:
        policy = RecoveryPolicy(
            merchant_id=merchant_id,
            allowed_actions=["retry", "grace_period", "incentive", "ops_review"],
            max_incentive=1000,
            max_exposure=10000,
            approval_threshold=5000.0,
        )
        db.add(policy)
        db.commit()
        db.refresh(policy)

    # 4. Make recovery decision (applying economics, strategist, policy rules)
    attempt = decide_recovery_action(
        db=db,
        incident=incident,
        diagnosis=diagnosis,
        policy=policy,
        llm_provider=llm_provider,
    )

    # 5. Evaluate economics to extract rate source & net recovery value for the chosen action
    candidates = evaluate_recovery_economics(db, incident, diagnosis, policy)
    matched = next((c for c in candidates if c.action == attempt.selected_action), None)
    
    rate_source = "configured"
    expected_net_value = 0
    if matched:
        rate_source = matched.rate_source
        expected_net_value = matched.expected_net_recovery_value

    # 6. Apply policy rules to determine approval requirement and auto-execution permission
    requires_approval = (attempt.status == "pending")
    execution_permitted = (attempt.status == "approved" and attempt.selected_action != "ops_review")

    return TrialEvaluationResult(
        incident_detected=True,
        incident_id=incident.id,
        diagnosis_type=diagnosis.diagnosis_type,
        selected_action=attempt.selected_action,
        rate_source=rate_source,
        expected_net_recovery_value=expected_net_value,
        requires_approval=requires_approval,
        execution_permitted=execution_permitted,
    )

