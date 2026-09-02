from datetime import datetime, timezone, timedelta
import pytest
from sqlalchemy import select, delete
from app.db.session import SessionLocal
from app.domains.recovery.service import decide_recovery_action
from app.models.merchant import Merchant
from app.models.payment import Payment
from app.models.incident import Incident
from app.models.recovery_attempt import RecoveryAttempt
from app.models.recovery_campaign import RecoveryCampaign
from app.models.recovery_policy import RecoveryPolicy
from app.models.diagnosis import Diagnosis
from tests.test_recovery_strategist import MockLLMProvider
from app.domains.recovery.strategist import AIRecommendation


@pytest.fixture
def db_session():
    session = SessionLocal()
    try:
        from app.models.recovery_audit_event import RecoveryAuditEvent
        session.execute(delete(RecoveryAuditEvent))
        session.execute(delete(RecoveryAttempt))
        session.execute(delete(RecoveryCampaign))
        session.execute(delete(RecoveryPolicy))
        session.execute(delete(Diagnosis))
        session.execute(delete(Incident))
        session.execute(delete(Payment))
        session.execute(delete(Merchant))
        session.commit()
        yield session
        session.execute(delete(RecoveryAuditEvent))
        session.execute(delete(RecoveryAttempt))
        session.execute(delete(RecoveryCampaign))
        session.execute(delete(RecoveryPolicy))
        session.execute(delete(Diagnosis))
        session.execute(delete(Incident))
        session.execute(delete(Payment))
        session.execute(delete(Merchant))
        session.commit()
    finally:
        session.close()


@pytest.fixture
def create_merchant(db_session):
    def _create(name: str = "Evidence Merchant"):
        merchant = Merchant(name=name)
        db_session.add(merchant)
        db_session.commit()
        db_session.refresh(merchant)
        return merchant
    return _create


@pytest.fixture
def create_policy(db_session):
    def _create(merchant_id, allowed_actions=None):
        if allowed_actions is None:
            allowed_actions = ["retry", "grace_period", "incentive", "ops_review"]
        policy = RecoveryPolicy(
            merchant_id=merchant_id,
            allowed_actions=allowed_actions,
            max_incentive=1000,
            max_exposure=20000,
            approval_threshold=5000.0,
        )
        db_session.add(policy)
        db_session.commit()
        return policy
    return _create


@pytest.fixture
def create_incident_and_diagnosis(db_session):
    def _create(merchant_id, revenue_at_risk=10000):
        now = datetime.now(timezone.utc)
        incident = Incident(
            merchant_id=merchant_id,
            method="upi",
            bank="HDFC",
            error_code="GATEWAY_ERROR",
            error_step="payment_authorization",
            current_total_count=30,
            current_failed_count=10,
            current_failure_rate=0.33,
            current_total_amount=30000,
            current_failed_amount=revenue_at_risk,
            baseline_total_count=30,
            baseline_failed_count=1,
            baseline_failure_rate=0.03,
            baseline_total_amount=30000,
            baseline_failed_amount=1000,
            absolute_rate_increase=0.3,
            relative_degradation=10.0,
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

        diagnosis = Diagnosis(
            incident_id=incident.id,
            diagnosis_type="bank-specific degradation",
            explanation="HDFC UPI gateway latency spike",
            supporting_evidence={},
            confidence=0.85,
        )
        db_session.add(diagnosis)
        db_session.commit()
        db_session.refresh(diagnosis)

        # Seed matching real failed payment for the incident window
        import uuid
        payment = Payment(
            merchant_id=merchant_id,
            razorpay_payment_id=f"pay_evi_{uuid.uuid4().hex[:10]}",
            amount=revenue_at_risk,
            currency="INR",
            status="failed",
            method=incident.method,
            bank=incident.bank,
            error_code=incident.error_code,
            error_step=incident.error_step,
            created_at=now - timedelta(minutes=15),
        )
        db_session.add(payment)
        db_session.commit()

        return incident, diagnosis
    return _create


def test_evidence_created_normal_decision(db_session, create_merchant, create_policy, create_incident_and_diagnosis):
    m = create_merchant()
    create_policy(m.id)
    incident, diagnosis = create_incident_and_diagnosis(m.id)

    # Trigger deterministic decision (no LLM provider)
    attempt = decide_recovery_action(db_session, incident, diagnosis, db_session.scalar(select(RecoveryPolicy).where(RecoveryPolicy.merchant_id == m.id)))

    assert attempt.decision_evidence is not None
    evidence = attempt.decision_evidence

    assert evidence["diagnosis_type"] == "bank-specific degradation"
    assert evidence["diagnosis_confidence"] == 0.85
    assert evidence["diagnosis_explanation"] == "HDFC UPI gateway latency spike"
    assert evidence["cohort_dimensions"]["method"] == "upi"
    assert evidence["cohort_dimensions"]["bank"] == "HDFC"
    assert evidence["cohort_dimensions"]["error_code"] == "GATEWAY_ERROR"
    assert evidence["revenue_at_risk"] == 10000
    assert evidence["selected_recovery_action"] == attempt.selected_action
    assert "expected_recovery_rate" in evidence
    assert "expected_net_recovery_value" in evidence
    assert evidence["is_policy_eligible"] is True
    assert evidence["ai_strategist_used"] is False
    assert "concise_decision_reason" in evidence


def test_historical_evidence_included(db_session, create_merchant, create_policy, create_incident_and_diagnosis):
    m = create_merchant()
    create_policy(m.id)
    incident, diagnosis = create_incident_and_diagnosis(m.id)

    # Seed 6 completed historical attempts for "grace_period" (all recovered)
    for i in range(6):
        att = RecoveryAttempt(
            recovery_id=f"rec_hist_{i}",
            merchant_id=m.id,
            incident_id=incident.id,
            selected_action="grace_period",
            status="recovered",
            recovered_amount=10000,
            incentive_amount=0,
        )
        db_session.add(att)
    db_session.commit()

    attempt = decide_recovery_action(db_session, incident, diagnosis, db_session.scalar(select(RecoveryPolicy).where(RecoveryPolicy.merchant_id == m.id)))

    # In "bank-specific degradation", retry is ineligible, so grace_period is the best choice
    assert attempt.selected_action == "grace_period"
    evidence = attempt.decision_evidence
    assert evidence["rate_source"] == "exact_cohort"  # Since sample size is 6 >= 5, exact cohort was used
    assert evidence["historical_sample_size"] == 6


def test_configured_rate_fallback_represented(db_session, create_merchant, create_policy, create_incident_and_diagnosis):
    m = create_merchant()
    create_policy(m.id)
    incident, diagnosis = create_incident_and_diagnosis(m.id)

    attempt = decide_recovery_action(db_session, incident, diagnosis, db_session.scalar(select(RecoveryPolicy).where(RecoveryPolicy.merchant_id == m.id)))

    evidence = attempt.decision_evidence
    assert evidence["rate_source"] == "configured"
    assert evidence["historical_sample_size"] == 0


def test_ai_recommendation_represented(db_session, create_merchant, create_policy, create_incident_and_diagnosis):
    m = create_merchant()
    create_policy(m.id)
    incident, diagnosis = create_incident_and_diagnosis(m.id)

    # Mock LLM provider recommending incentive
    rec = {
        "recommended_action": "incentive",
        "concise_reason": "AI strategist detects optimal cohort matches for discount incentives",
        "evidence_level": "global",
        "sample_size": 10,
        "observed_recovery_rate": 0.25,
        "expected_net_recovery_value": 2000,
        "confidence": 0.90,
    }
    provider = MockLLMProvider(response_data=rec)

    attempt = decide_recovery_action(
        db_session, incident, diagnosis,
        db_session.scalar(select(RecoveryPolicy).where(RecoveryPolicy.merchant_id == m.id)),
        llm_provider=provider
    )

    assert attempt.selected_action == "incentive"
    evidence = attempt.decision_evidence
    assert evidence["ai_strategist_used"] is True
    assert evidence["concise_decision_reason"] == "AI strategist detects optimal cohort matches for discount incentives"


def test_deterministic_fallback_represented(db_session, create_merchant, create_policy, create_incident_and_diagnosis):
    m = create_merchant()
    create_policy(m.id)
    incident, diagnosis = create_incident_and_diagnosis(m.id)

    attempt = decide_recovery_action(
        db_session, incident, diagnosis,
        db_session.scalar(select(RecoveryPolicy).where(RecoveryPolicy.merchant_id == m.id)),
        llm_provider=None
    )

    evidence = attempt.decision_evidence
    assert evidence["ai_strategist_used"] is False
    assert "Deterministic fallback selected" in evidence["concise_decision_reason"]


def test_policy_ineligible_action_cannot_be_approved(db_session, create_merchant, create_policy, create_incident_and_diagnosis):
    m = create_merchant()
    # Policy only allows grace_period (retry is blocked by policy)
    create_policy(m.id, allowed_actions=["grace_period"])
    incident, diagnosis = create_incident_and_diagnosis(m.id)

    # Let's change diagnosis to normal / unknown so that retry would normally be eligible from diagnosis standpoint
    diagnosis.diagnosis_type = "insufficient evidence / unknown"
    db_session.commit()

    attempt = decide_recovery_action(
        db_session, incident, diagnosis,
        db_session.scalar(select(RecoveryPolicy).where(RecoveryPolicy.merchant_id == m.id))
    )

    # Decider must select grace_period, because retry is blocked by policy
    assert attempt.selected_action == "grace_period"
    evidence = attempt.decision_evidence
    assert evidence["selected_recovery_action"] == "grace_period"
    assert evidence["is_policy_eligible"] is True
