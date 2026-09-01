from sqlalchemy.orm import Session

from app.models.incident import Incident
from app.models.diagnosis import Diagnosis
from app.models.recovery_policy import RecoveryPolicy
from app.domains.recovery.economics import evaluate_recovery_economics
from app.domains.recovery.outcomes import calculate_recovery_performance


def build_decision_evidence(
    db: Session,
    incident: Incident,
    diagnosis: Diagnosis,
    policy: RecoveryPolicy,
    selected_action: str,
    ai_strategist_used: bool,
    reason: str,
) -> dict:
    """
    Assembles structured evidence explaining why a recovery action was selected.
    """
    # Evaluate economics to get candidate details
    candidates = evaluate_recovery_economics(db, incident, diagnosis, policy)
    matched = next((c for c in candidates if c.action == selected_action), None)

    expected_rate = 0.0
    rate_source = "configured"
    expected_net_value = 0
    is_eligible = False

    if matched:
        expected_rate = matched.expected_recovery_rate
        rate_source = matched.rate_source
        expected_net_value = matched.expected_net_recovery_value
        is_eligible = matched.is_eligible
    else:
        # Fallback if not found in candidates (e.g. invalid action, though rare)
        if selected_action == "ops_review":
            expected_rate = 0.05
            rate_source = "configured"
            expected_net_value = 0
            is_eligible = True

    # Retrieve historical sample size
    perf = calculate_recovery_performance(
        db=db,
        merchant_id=incident.merchant_id,
        method=incident.method,
        bank=incident.bank,
        error_code=incident.error_code,
        selected_action=selected_action,
        min_attempts=5,  # Matches the default min_attempts
    )
    sample_size = perf.total_attempts

    # Construct the structured decision evidence dictionary
    evidence = {
        "diagnosis_type": diagnosis.diagnosis_type,
        "diagnosis_confidence": diagnosis.confidence,
        "diagnosis_explanation": diagnosis.explanation,
        "cohort_dimensions": {
            "method": incident.method,
            "bank": incident.bank,
            "error_code": incident.error_code,
            "error_step": incident.error_step,
        },
        "revenue_at_risk": incident.revenue_at_risk,
        "selected_recovery_action": selected_action,
        "expected_recovery_rate": expected_rate,
        "rate_source": rate_source,
        "historical_sample_size": sample_size,
        "expected_net_recovery_value": expected_net_value,
        "is_policy_eligible": is_eligible,
        "ai_strategist_used": ai_strategist_used,
        "concise_decision_reason": reason,
    }

    return evidence

