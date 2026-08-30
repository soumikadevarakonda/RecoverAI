from datetime import datetime, timedelta, timezone
import uuid
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.incident import Incident
from app.models.diagnosis import Diagnosis
from app.models.recovery_policy import RecoveryPolicy
from app.models.recovery_attempt import RecoveryAttempt


def decide_recovery_action(
    db: Session,
    incident: Incident,
    diagnosis: Diagnosis,
    policy: RecoveryPolicy,
) -> RecoveryAttempt:
    selected_action = "ops_review"
    incentive_amount = 0
    status = "pending"

    allowed_actions = policy.allowed_actions or []

    # Priority 1: retry
    if "retry" in allowed_actions and diagnosis.diagnosis_type not in [
        "bank-specific degradation",
        "payment-method degradation",
    ]:
        selected_action = "retry"
        status = "approved"

    # Priority 2: grace_period
    elif "grace_period" in allowed_actions:
        selected_action = "grace_period"
        status = "approved"

    # Priority 3: incentive
    elif "incentive" in allowed_actions and diagnosis.diagnosis_type != "insufficient evidence / unknown":
        proposed_incentive = 500  # Default deterministic incentive in minor units (paise)

        # Check policy max incentive
        within_max_incentive = proposed_incentive <= policy.max_incentive

        # Check exposure limit
        active_exposure = db.scalar(
            select(func.sum(RecoveryAttempt.incentive_amount)).where(
                RecoveryAttempt.merchant_id == incident.merchant_id,
                RecoveryAttempt.status.in_(["pending", "approved", "executed"]),
            )
        ) or 0
        within_exposure_limit = (active_exposure + proposed_incentive) <= policy.max_exposure

        if within_max_incentive and within_exposure_limit:
            selected_action = "incentive"
            incentive_amount = proposed_incentive
            
            # Check approval threshold (monetary limit in float format)
            if proposed_incentive > policy.approval_threshold:
                status = "pending"
            else:
                status = "approved"

    # Fallback to ops_review
    if selected_action == "ops_review":
        incentive_amount = 0
        status = "pending"

    attempt = RecoveryAttempt(
        recovery_id=f"rec_{uuid.uuid4().hex[:12]}",
        merchant_id=incident.merchant_id,
        incident_id=incident.id,
        payment_id=None,
        selected_action=selected_action,
        incentive_amount=incentive_amount,
        status=status,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        recovered_amount=0,
    )
    db.add(attempt)
    db.commit()
    db.refresh(attempt)
    return attempt
