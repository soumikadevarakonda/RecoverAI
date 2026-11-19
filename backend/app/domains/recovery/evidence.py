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
    guardrail_result: Any | None = None,
    adaptive_result: Any | None = None,
    operational_memory: Any | None = None,
) -> dict:
    """
    Assembles structured evidence explaining why a recovery action was selected,
    including candidate economics, historical outcomes, and guardrail verification.
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
        "diagnosis_type": diagnosis.diagnosis_type if diagnosis else "unknown",
        "diagnosis_confidence": diagnosis.confidence if diagnosis else 0.0,
        "diagnosis_explanation": diagnosis.explanation if diagnosis else "No diagnosis provided.",
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

    if guardrail_result is not None:
        evidence.update({
            "guardrail_decision": guardrail_result.decision,
            "guardrail_reason_code": guardrail_result.reason_code,
            "guardrail_explanation": guardrail_result.explanation,
            "guardrail_audit_event": guardrail_result.audit_event_type,
            "guardrail_checks": [c.model_dump() for c in guardrail_result.checks],
            "guardrail_evaluated_exposure": guardrail_result.evaluated_exposure,
            "policy_values": guardrail_result.policy_values,
        })
    else:
        evidence.update({
            "guardrail_decision": "allowed" if is_eligible else "blocked",
            "guardrail_reason_code": "WITHIN_POLICY" if is_eligible else "ACTION_DISALLOWED",
            "guardrail_explanation": "Default guardrail policy check passed." if is_eligible else "Action disallowed by policy.",
            "guardrail_audit_event": "GUARDRAIL_APPROVED" if is_eligible else "GUARDRAIL_BLOCKED",
            "guardrail_checks": [],
            "guardrail_evaluated_exposure": {},
            "policy_values": {
                "allowed_actions": list(policy.allowed_actions),
                "max_incentive": policy.max_incentive,
                "max_exposure": policy.max_exposure,
                "approval_threshold": policy.approval_threshold,
            },
        })

    if adaptive_result is not None:
        evidence["adaptive_analyst"] = {
            "is_accepted": adaptive_result.is_accepted,
            "rejection_reason": adaptive_result.rejection_reason,
            "fallback_action": adaptive_result.fallback_action,
            "recommendation": adaptive_result.recommendation.model_dump() if adaptive_result.recommendation else None,
            "telemetry": adaptive_result.telemetry.model_dump() if getattr(adaptive_result, "telemetry", None) else None,
        }

    if operational_memory is not None:
        sample_size = getattr(operational_memory, "historical_sample_size", 0)
        ev_level = getattr(operational_memory, "evidence_level", "insufficient")
        if hasattr(ev_level, "value"):
            ev_level = ev_level.value

        evidence["operational_memory"] = {
            "memory_used": sample_size > 0,
            "evidence_level": str(ev_level),
            "historical_sample_size": sample_size,
            "signal_breakdown": operational_memory.signal_breakdown.model_dump() if hasattr(getattr(operational_memory, "signal_breakdown", None), "model_dump") else {},
            "threshold_statistics": operational_memory.threshold_statistics.model_dump() if hasattr(getattr(operational_memory, "threshold_statistics", None), "model_dump") else {},
            "summary": getattr(operational_memory, "summary", ""),
        }

    return evidence

