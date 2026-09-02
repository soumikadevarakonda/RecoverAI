from datetime import datetime, timedelta, timezone
from uuid import uuid4
import pytest
from sqlalchemy import delete

from app.db.session import SessionLocal
from app.models.merchant import Merchant
from app.models.payment import Payment
from app.models.payment_event import PaymentEvent
from app.models.webhook_event import WebhookEvent
from app.models.incident import Incident
from app.models.diagnosis import Diagnosis
from app.models.recovery_policy import RecoveryPolicy
from app.models.recovery_attempt import RecoveryAttempt
from app.models.recovery_campaign import RecoveryCampaign
from app.models.recovery_audit_event import RecoveryAuditEvent

from app.domains.recovery.audit import get_recovery_audit_trail, RecoveryAuditEventType
from app.domains.recovery.evaluation import (
    LearningSignalClassification,
    AIEconomicTelemetry,
    AdaptiveDecisionEvaluation,
    evaluate_campaign_adaptive_decision,
    evaluate_attempt_adaptive_decision,
)


@pytest.fixture
def db_session():
    session = SessionLocal()
    try:
        session.execute(delete(RecoveryAuditEvent))
        session.execute(delete(RecoveryAttempt))
        session.execute(delete(RecoveryCampaign))
        session.execute(delete(RecoveryPolicy))
        session.execute(delete(Diagnosis))
        session.execute(delete(Incident))
        session.execute(delete(PaymentEvent))
        session.execute(delete(Payment))
        session.execute(delete(WebhookEvent))
        session.execute(delete(Merchant))
        session.commit()
        yield session
        session.execute(delete(RecoveryAuditEvent))
        session.execute(delete(RecoveryAttempt))
        session.execute(delete(RecoveryCampaign))
        session.execute(delete(RecoveryPolicy))
        session.execute(delete(Diagnosis))
        session.execute(delete(Incident))
        session.execute(delete(PaymentEvent))
        session.execute(delete(Payment))
        session.execute(delete(WebhookEvent))
        session.execute(delete(Merchant))
        session.commit()
    finally:
        session.close()


@pytest.fixture
def create_merchant(db_session):
    def _create(name: str = "Evaluation Test Merchant"):
        merchant = Merchant(name=name)
        db_session.add(merchant)
        db_session.commit()
        db_session.refresh(merchant)
        return merchant
    return _create


@pytest.fixture
def create_incident(db_session):
    def _create(merchant_id, revenue_at_risk=30000, failed_count=6):
        now = datetime.now(timezone.utc)
        incident = Incident(
            merchant_id=merchant_id,
            method="upi",
            bank="HDFC",
            error_code="GATEWAY_ERROR",
            error_step="payment_authorization",
            current_total_count=30,
            current_failed_count=failed_count,
            current_failure_rate=failed_count / 30.0,
            current_total_amount=100000,
            current_failed_amount=revenue_at_risk,
            baseline_total_count=30,
            baseline_failed_count=1,
            baseline_failure_rate=0.033,
            baseline_total_amount=100000,
            baseline_failed_amount=1000,
            absolute_rate_increase=0.167,
            relative_degradation=6.0,
            revenue_at_risk=revenue_at_risk,
            window_start=now - timedelta(minutes=30),
            window_end=now,
            baseline_start=now - timedelta(hours=2),
            baseline_end=now - timedelta(hours=1),
            status="detected",
        )
        db_session.add(incident)
        db_session.commit()
        db_session.refresh(incident)
        return incident
    return _create


def _make_evidence(
    expected_rate: float = 0.50,
    expected_gross: int = 15000,
    expected_net: int = 14000,
    ai_used: bool = True,
    selected_action: str = "grace_period",
    intervene: bool = True,
    recommended_threshold: int = 8,
    guardrail_decision: str = "allowed",
    guardrail_reason: str = "EXPOSURE_WITHIN_POLICY",
    fallback_used: bool = False,
    rejection_reason: str | None = None,
) -> dict:
    return {
        "ai_strategist_used": ai_used,
        "selected_action": selected_action,
        "guardrail_decision": guardrail_decision,
        "guardrail_reason_code": guardrail_reason,
        "economics": {
            "selected_action_economics": {
                "expected_recovery_rate": expected_rate,
                "expected_recovery_amount": expected_gross,
                "expected_net_recovery_value": expected_net,
                "action_cost": 500,
            }
        },
        "adaptive_analyst": {
            "is_accepted": not fallback_used and rejection_reason is None,
            "rejection_reason": rejection_reason,
            "recommendation": {
                "intervene": intervene,
                "confidence": 0.90,
                "recommended_failure_threshold": recommended_threshold,
                "recommended_failure_rate_threshold": 0.15,
                "urgency": "medium",
                "recommended_action": selected_action,
                "reasoning": "Standard transient degradation",
                "evidence_summary": [],
            },
            "telemetry": {
                "provider": "MockAdaptiveLLM",
                "model_name": "gemini-1.5-pro",
                "latency_ms": 120.5,
                "input_size_bytes": 1420,
                "output_size_bytes": 280,
                "success": True,
                "fallback_used": fallback_used,
                "error_message": rejection_reason,
            },
        },
    }


# ============================================================================
# 1. Successful Prediction
# ============================================================================

def test_successful_prediction_evaluation(db_session, create_merchant, create_incident):
    merchant = create_merchant()
    incident = create_incident(merchant.id)

    evidence = _make_evidence(expected_rate=0.50, expected_gross=10000, expected_net=10000)

    campaign = RecoveryCampaign(
        campaign_id=f"camp_{uuid4().hex[:10]}",
        merchant_id=merchant.id,
        incident_id=incident.id,
        selected_action="grace_period",
        status="completed",
        target_payment_count=2,
        total_revenue_at_risk=20000,
        per_attempt_incentive=0,
        total_incentive_cost=0,
        decision_evidence=evidence,
    )
    db_session.add(campaign)
    db_session.flush()

    # 1 recovered out of 2 -> 50% observed rate (matches 50% predicted rate)
    p1 = Payment(merchant_id=merchant.id, razorpay_payment_id="pay_succ_1", amount=10000, currency="INR", status="failed")
    p2 = Payment(merchant_id=merchant.id, razorpay_payment_id="pay_succ_2", amount=10000, currency="INR", status="failed")
    db_session.add_all([p1, p2])
    db_session.flush()

    a1 = RecoveryAttempt(
        recovery_id="rec_succ_1",
        campaign_id=campaign.id,
        payment_id=p1.id,
        merchant_id=merchant.id,
        incident_id=incident.id,
        selected_action="grace_period",
        status="recovered",
        recovered_amount=10000,
        decision_evidence=evidence,
    )
    a2 = RecoveryAttempt(
        recovery_id="rec_succ_2",
        campaign_id=campaign.id,
        payment_id=p2.id,
        merchant_id=merchant.id,
        incident_id=incident.id,
        selected_action="grace_period",
        status="failed",
        recovered_amount=0,
        decision_evidence=evidence,
    )
    db_session.add_all([a1, a2])
    db_session.commit()
    db_session.refresh(campaign)

    evaluation = evaluate_campaign_adaptive_decision(db_session, campaign, persist=True)

    assert evaluation.classification == LearningSignalClassification.SUCCESSFUL_DECISION
    assert evaluation.observed_recovery_rate == 0.50
    assert evaluation.actual_recovered_amount == 10000
    assert evaluation.actual_net_recovery_value == 10000
    assert evaluation.meaningful_recovery is True
    assert campaign.decision_evidence.get("learning_signal") is not None


# ============================================================================
# 2. Over-Intervention (Costs Exceed Recovery)
# ============================================================================

def test_over_intervention_evaluation(db_session, create_merchant, create_incident):
    merchant = create_merchant()
    incident = create_incident(merchant.id)

    evidence = _make_evidence(expected_rate=0.80, selected_action="incentive")

    # Incentive cost 4000, but only 2000 recovered -> net recovery = -2000 (loss!)
    campaign = RecoveryCampaign(
        campaign_id=f"camp_{uuid4().hex[:10]}",
        merchant_id=merchant.id,
        incident_id=incident.id,
        selected_action="incentive",
        status="completed",
        target_payment_count=4,
        total_revenue_at_risk=20000,
        per_attempt_incentive=1000,
        total_incentive_cost=4000,
        decision_evidence=evidence,
    )
    db_session.add(campaign)
    db_session.flush()

    p = Payment(merchant_id=merchant.id, razorpay_payment_id="pay_over_1", amount=5000, currency="INR", status="failed")
    db_session.add(p)
    db_session.flush()

    a = RecoveryAttempt(
        recovery_id="rec_over_1",
        campaign_id=campaign.id,
        payment_id=p.id,
        merchant_id=merchant.id,
        incident_id=incident.id,
        selected_action="incentive",
        status="recovered",
        incentive_amount=1000,
        recovered_amount=2000,
        decision_evidence=evidence,
    )
    db_session.add(a)
    db_session.commit()
    db_session.refresh(campaign)

    evaluation = evaluate_campaign_adaptive_decision(db_session, campaign, persist=True)

    assert evaluation.classification == LearningSignalClassification.OVER_INTERVENTION
    assert evaluation.actual_net_recovery_value < 0


# ============================================================================
# 3. Under-Intervention (High Failure Volume Ignored)
# ============================================================================

def test_under_intervention_evaluation(db_session, create_merchant, create_incident):
    merchant = create_merchant()
    # 20 failures and 50000 at risk, but AI recommended intervene=False
    incident = create_incident(merchant.id, revenue_at_risk=50000, failed_count=20)

    evidence = _make_evidence(intervene=False, selected_action="ops_review")

    campaign = RecoveryCampaign(
        campaign_id=f"camp_{uuid4().hex[:10]}",
        merchant_id=merchant.id,
        incident_id=incident.id,
        selected_action="ops_review",
        status="completed",
        target_payment_count=0,
        total_revenue_at_risk=50000,
        per_attempt_incentive=0,
        total_incentive_cost=0,
        decision_evidence=evidence,
    )
    db_session.add(campaign)
    db_session.commit()
    db_session.refresh(campaign)

    evaluation = evaluate_campaign_adaptive_decision(db_session, campaign, persist=True)

    assert evaluation.classification == LearningSignalClassification.UNDER_INTERVENTION


# ============================================================================
# 4. Recovery Outperforming Expectation
# ============================================================================

def test_recovery_outperformance_evaluation(db_session, create_merchant, create_incident):
    merchant = create_merchant()
    incident = create_incident(merchant.id)

    # Predicted rate 20%, but actual observed rate is 100% (+80% outperformance!)
    evidence = _make_evidence(expected_rate=0.20, expected_gross=2000, expected_net=2000)

    campaign = RecoveryCampaign(
        campaign_id=f"camp_{uuid4().hex[:10]}",
        merchant_id=merchant.id,
        incident_id=incident.id,
        selected_action="grace_period",
        status="completed",
        target_payment_count=1,
        total_revenue_at_risk=10000,
        per_attempt_incentive=0,
        total_incentive_cost=0,
        decision_evidence=evidence,
    )
    db_session.add(campaign)
    db_session.flush()

    p = Payment(merchant_id=merchant.id, razorpay_payment_id="pay_outp_1", amount=10000, currency="INR", status="failed")
    db_session.add(p)
    db_session.flush()

    a = RecoveryAttempt(
        recovery_id="rec_outp_1",
        campaign_id=campaign.id,
        payment_id=p.id,
        merchant_id=merchant.id,
        incident_id=incident.id,
        selected_action="grace_period",
        status="recovered",
        recovered_amount=10000,
        decision_evidence=evidence,
    )
    db_session.add(a)
    db_session.commit()
    db_session.refresh(campaign)

    evaluation = evaluate_campaign_adaptive_decision(db_session, campaign, persist=True)

    assert evaluation.classification == LearningSignalClassification.RECOVERY_OUTPERFORMANCE
    assert evaluation.rate_delta >= 0.10


# ============================================================================
# 5. Recovery Underperforming Expectation
# ============================================================================

def test_recovery_underperformance_evaluation(db_session, create_merchant, create_incident):
    merchant = create_merchant()
    incident = create_incident(merchant.id)

    # Predicted rate 80%, but actual observed is 0% (-80% underperformance)
    evidence = _make_evidence(expected_rate=0.80, expected_gross=8000, expected_net=8000)

    campaign = RecoveryCampaign(
        campaign_id=f"camp_{uuid4().hex[:10]}",
        merchant_id=merchant.id,
        incident_id=incident.id,
        selected_action="grace_period",
        status="completed",
        target_payment_count=1,
        total_revenue_at_risk=10000,
        per_attempt_incentive=0,
        total_incentive_cost=0,
        decision_evidence=evidence,
    )
    db_session.add(campaign)
    db_session.flush()

    p = Payment(merchant_id=merchant.id, razorpay_payment_id="pay_underp_1", amount=10000, currency="INR", status="failed")
    db_session.add(p)
    db_session.flush()

    a = RecoveryAttempt(
        recovery_id="rec_underp_1",
        campaign_id=campaign.id,
        payment_id=p.id,
        merchant_id=merchant.id,
        incident_id=incident.id,
        selected_action="grace_period",
        status="failed",
        recovered_amount=0,
        decision_evidence=evidence,
    )
    db_session.add(a)
    db_session.commit()
    db_session.refresh(campaign)

    evaluation = evaluate_campaign_adaptive_decision(db_session, campaign, persist=True)

    assert evaluation.classification == LearningSignalClassification.RECOVERY_UNDERPERFORMANCE
    assert evaluation.rate_delta <= -0.10


# ============================================================================
# 6. Incomplete / Non-Terminal Attempts (Enforce Invariant)
# ============================================================================

def test_non_terminal_attempt_raises_error(db_session, create_merchant, create_incident):
    merchant = create_merchant()
    incident = create_incident(merchant.id)

    campaign = RecoveryCampaign(
        campaign_id=f"camp_{uuid4().hex[:10]}",
        merchant_id=merchant.id,
        incident_id=incident.id,
        selected_action="retry",
        status="executing",  # Non-terminal!
    )
    db_session.add(campaign)
    db_session.commit()

    with pytest.raises(ValueError, match="Cannot evaluate non-terminal campaign"):
        evaluate_campaign_adaptive_decision(db_session, campaign)

    a = RecoveryAttempt(
        recovery_id="rec_nonterm_1",
        campaign_id=campaign.id,
        merchant_id=merchant.id,
        incident_id=incident.id,
        selected_action="retry",
        status="pending",  # Non-terminal!
    )
    db_session.add(a)
    db_session.commit()

    with pytest.raises(ValueError, match="Cannot evaluate non-terminal attempt"):
        evaluate_attempt_adaptive_decision(db_session, a)


# ============================================================================
# 7. Zero Revenue-At-Risk
# ============================================================================

def test_zero_revenue_at_risk_handles_gracefully(db_session, create_merchant, create_incident):
    merchant = create_merchant()
    incident = create_incident(merchant.id, revenue_at_risk=0)

    evidence = _make_evidence(expected_rate=0.0, expected_gross=0, expected_net=0)

    campaign = RecoveryCampaign(
        campaign_id=f"camp_{uuid4().hex[:10]}",
        merchant_id=merchant.id,
        incident_id=incident.id,
        selected_action="grace_period",
        status="completed",
        target_payment_count=0,
        total_revenue_at_risk=0,
        per_attempt_incentive=0,
        total_incentive_cost=0,
        decision_evidence=evidence,
    )
    db_session.add(campaign)
    db_session.commit()

    evaluation = evaluate_campaign_adaptive_decision(db_session, campaign, persist=False)

    assert evaluation.observed_recovery_rate == 0.0
    assert evaluation.actual_recovered_amount == 0
    assert evaluation.expected_recovered_amount == 0


# ============================================================================
# 8. Zero AI Cost Information (Explicitly Unavailable)
# ============================================================================

def test_zero_ai_cost_information_explicit(db_session, create_merchant, create_incident):
    merchant = create_merchant()
    incident = create_incident(merchant.id)

    evidence = _make_evidence()
    campaign = RecoveryCampaign(
        campaign_id=f"camp_{uuid4().hex[:10]}",
        merchant_id=merchant.id,
        incident_id=incident.id,
        selected_action="grace_period",
        status="completed",
        decision_evidence=evidence,
    )
    db_session.add(campaign)
    db_session.commit()

    evaluation = evaluate_campaign_adaptive_decision(db_session, campaign, persist=False)

    assert evaluation.telemetry is not None
    assert evaluation.telemetry.cost_status == "UNAVAILABLE"
    assert evaluation.telemetry.estimated_cost_usd is None
    assert evaluation.telemetry.cost_currency is None
    assert evaluation.telemetry.input_size_bytes > 0


# ============================================================================
# 9. Guardrail Veto Classification
# ============================================================================

def test_guardrail_veto_classification(db_session, create_merchant, create_incident):
    merchant = create_merchant()
    incident = create_incident(merchant.id)

    evidence = _make_evidence(
        guardrail_decision="blocked",
        guardrail_reason="CAMPAIGN_EXPOSURE_EXCEEDED",
    )

    campaign = RecoveryCampaign(
        campaign_id=f"camp_{uuid4().hex[:10]}",
        merchant_id=merchant.id,
        incident_id=incident.id,
        selected_action="retry",
        status="completed",
        decision_evidence=evidence,
    )
    db_session.add(campaign)
    db_session.commit()

    evaluation = evaluate_campaign_adaptive_decision(db_session, campaign, persist=False)

    assert evaluation.classification == LearningSignalClassification.GUARDRAIL_VETO
    assert "guardrails" in evaluation.explanation


# ============================================================================
# 10. AI Fallback Classification
# ============================================================================

def test_ai_fallback_classification(db_session, create_merchant, create_incident):
    merchant = create_merchant()
    incident = create_incident(merchant.id)

    evidence = _make_evidence(
        fallback_used=True,
        rejection_reason="THRESHOLD_BELOW_SAFETY_MINIMUM",
    )

    campaign = RecoveryCampaign(
        campaign_id=f"camp_{uuid4().hex[:10]}",
        merchant_id=merchant.id,
        incident_id=incident.id,
        selected_action="retry",
        status="completed",
        decision_evidence=evidence,
    )
    db_session.add(campaign)
    db_session.commit()

    evaluation = evaluate_campaign_adaptive_decision(db_session, campaign, persist=False)

    assert evaluation.classification == LearningSignalClassification.AI_FALLBACK
    assert "THRESHOLD_BELOW_SAFETY_MINIMUM" in evaluation.explanation


# ============================================================================
# 11. Merchant Isolation
# ============================================================================

def test_merchant_isolation(db_session, create_merchant, create_incident):
    merchant_a = create_merchant("Merchant Alpha")
    merchant_b = create_merchant("Merchant Beta")

    incident_a = create_incident(merchant_a.id)
    campaign_a = RecoveryCampaign(
        campaign_id=f"camp_{uuid4().hex[:10]}",
        merchant_id=merchant_a.id,
        incident_id=incident_a.id,
        selected_action="grace_period",
        status="completed",
        decision_evidence=_make_evidence(),
    )
    db_session.add(campaign_a)
    db_session.commit()

    evaluate_campaign_adaptive_decision(db_session, campaign_a, persist=True)

    # Merchant B audit trail must have zero events from Merchant A
    events_b = get_recovery_audit_trail(db_session, merchant_id=merchant_b.id)
    assert len(events_b) == 0

    # Merchant A has the learning signal event
    events_a = get_recovery_audit_trail(db_session, merchant_id=merchant_a.id)
    assert any(e.event_type == RecoveryAuditEventType.LEARNING_SIGNAL_RECORDED.value for e in events_a)


# ============================================================================
# 12. Campaign-Level Aggregation
# ============================================================================

def test_campaign_level_aggregation(db_session, create_merchant, create_incident):
    merchant = create_merchant()
    incident = create_incident(merchant.id)

    evidence = _make_evidence(expected_rate=0.66)
    campaign = RecoveryCampaign(
        campaign_id=f"camp_{uuid4().hex[:10]}",
        merchant_id=merchant.id,
        incident_id=incident.id,
        selected_action="grace_period",
        status="completed",
        target_payment_count=3,
        total_revenue_at_risk=30000,
        per_attempt_incentive=0,
        total_incentive_cost=0,
        decision_evidence=evidence,
    )
    db_session.add(campaign)
    db_session.flush()

    payments = [
        Payment(merchant_id=merchant.id, razorpay_payment_id=f"pay_agg_{i}", amount=10000, currency="INR", status="failed")
        for i in range(3)
    ]
    db_session.add_all(payments)
    db_session.flush()

    # 2 out of 3 recovered (66.67%)
    attempts = [
        RecoveryAttempt(
            recovery_id=f"rec_agg_{i}",
            campaign_id=campaign.id,
            payment_id=payments[i].id,
            merchant_id=merchant.id,
            incident_id=incident.id,
            selected_action="grace_period",
            status="recovered" if i < 2 else "failed",
            recovered_amount=10000 if i < 2 else 0,
            decision_evidence=evidence,
        )
        for i in range(3)
    ]
    db_session.add_all(attempts)
    db_session.commit()
    db_session.refresh(campaign)

    evaluation = evaluate_campaign_adaptive_decision(db_session, campaign, persist=False)

    assert evaluation.observed_recovery_rate == pytest.approx(0.6667, abs=1e-3)
    assert evaluation.actual_recovered_amount == 20000
    assert evaluation.actual_net_recovery_value == 20000


# ============================================================================
# 13. Payment-Level Attribution
# ============================================================================

def test_payment_level_attribution(db_session, create_merchant, create_incident):
    merchant = create_merchant()
    incident = create_incident(merchant.id)

    p = Payment(merchant_id=merchant.id, razorpay_payment_id="pay_attr_1", amount=15000, currency="INR", status="failed")
    db_session.add(p)
    db_session.flush()

    evidence = _make_evidence(expected_rate=0.50, selected_action="incentive")
    attempt = RecoveryAttempt(
        recovery_id="rec_attr_1",
        merchant_id=merchant.id,
        incident_id=incident.id,
        payment_id=p.id,
        selected_action="incentive",
        incentive_amount=1000,
        status="recovered",
        recovered_amount=14000,
        decision_evidence=evidence,
    )
    db_session.add(attempt)
    db_session.commit()
    db_session.refresh(attempt)

    evaluation = evaluate_attempt_adaptive_decision(db_session, attempt, persist=True)

    assert evaluation.payment_id == p.id
    assert evaluation.recovery_attempt_id == attempt.id
    assert evaluation.actual_recovered_amount == 14000
    assert evaluation.actual_net_recovery_value == 13000  # 14000 - 1000
    assert evaluation.observed_recovery_rate == 1.0


# ============================================================================
# 14. Idempotent Evaluation
# ============================================================================

def test_idempotent_evaluation(db_session, create_merchant, create_incident):
    merchant = create_merchant()
    incident = create_incident(merchant.id)

    campaign = RecoveryCampaign(
        campaign_id=f"camp_{uuid4().hex[:10]}",
        merchant_id=merchant.id,
        incident_id=incident.id,
        selected_action="grace_period",
        status="completed",
        decision_evidence=_make_evidence(),
    )
    db_session.add(campaign)
    db_session.commit()

    eval1 = evaluate_campaign_adaptive_decision(db_session, campaign, persist=True)
    eval2 = evaluate_campaign_adaptive_decision(db_session, campaign, persist=True)

    assert eval1.classification == eval2.classification
    assert eval1.actual_recovered_amount == eval2.actual_recovered_amount
    assert eval1.observed_recovery_rate == eval2.observed_recovery_rate


# ============================================================================
# 15. Malformed / Missing Adaptive Evidence Handling
# ============================================================================

def test_malformed_or_missing_evidence_handles_gracefully(db_session, create_merchant, create_incident):
    merchant = create_merchant()
    incident = create_incident(merchant.id)

    # Empty or null decision_evidence
    campaign = RecoveryCampaign(
        campaign_id=f"camp_{uuid4().hex[:10]}",
        merchant_id=merchant.id,
        incident_id=incident.id,
        selected_action="grace_period",
        status="completed",
        decision_evidence=None,
    )
    db_session.add(campaign)
    db_session.commit()

    evaluation = evaluate_campaign_adaptive_decision(db_session, campaign, persist=False)

    assert evaluation is not None
    assert evaluation.predicted_recovery_rate is None
    assert evaluation.actual_recovered_amount == 0
    assert evaluation.telemetry is None

