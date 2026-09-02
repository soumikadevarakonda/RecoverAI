from datetime import datetime, timedelta, timezone
import pytest
from sqlalchemy import select

from app.db.session import SessionLocal
from app.domains.recovery.service import decide_recovery_action
from app.models.diagnosis import Diagnosis
from app.models.incident import Incident
from app.models.merchant import Merchant
from app.models.payment import Payment
from app.models.payment_event import PaymentEvent
from app.models.webhook_event import WebhookEvent
from app.models.recovery_policy import RecoveryPolicy
from app.models.recovery_attempt import RecoveryAttempt
from app.models.recovery_campaign import RecoveryCampaign


@pytest.fixture
def db_session():
    session = SessionLocal()
    try:
        session.query(RecoveryAttempt).delete()
        session.query(RecoveryCampaign).delete()
        session.query(RecoveryPolicy).delete()
        session.query(Diagnosis).delete()
        session.query(Incident).delete()
        session.query(PaymentEvent).delete()
        session.query(Payment).delete()
        session.query(WebhookEvent).delete()
        session.commit()
        yield session
    finally:
        session.close()


@pytest.fixture
def create_merchant(db_session):
    def _create(name: str = "Merchant"):
        merchant = Merchant(name=name)
        db_session.add(merchant)
        db_session.commit()
        db_session.refresh(merchant)
        return merchant
    return _create


@pytest.fixture
def create_incident_and_diagnosis(db_session):
    def _create(merchant_id, diagnosis_type="error-code spike"):
        now = datetime.now(timezone.utc)
        incident = Incident(
            merchant_id=merchant_id,
            method="upi",
            bank="HDFC",
            error_code="GATEWAY_ERROR",
            error_step="payment_authorization",
            current_total_count=30,
            current_failed_count=10,
            current_failure_rate=0.3333,
            current_total_amount=30000,
            current_failed_amount=10000,
            baseline_total_count=30,
            baseline_failed_count=1,
            baseline_failure_rate=0.0333,
            baseline_total_amount=30000,
            baseline_failed_amount=1000,
            absolute_rate_increase=0.3,
            relative_degradation=10.0,
            revenue_at_risk=10000,
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
            diagnosis_type=diagnosis_type,
            explanation="Test diagnosis",
            supporting_evidence={},
            confidence=0.8,
        )
        db_session.add(diagnosis)
        # Seed matching real failed payment for the incident window
        import uuid
        payment = Payment(
            merchant_id=merchant_id,
            razorpay_payment_id=f"pay_dec_{uuid.uuid4().hex[:10]}",
            amount=10000,
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


def test_retry_selected_when_allowed(db_session, create_merchant, create_incident_and_diagnosis):
    merchant = create_merchant()
    incident, diagnosis = create_incident_and_diagnosis(merchant.id, "error-code spike")

    policy = RecoveryPolicy(
        merchant_id=merchant.id,
        allowed_actions=["retry", "grace_period", "incentive"],
        max_incentive=1000,
        max_exposure=10000,
        approval_threshold=1000.0,
    )
    db_session.add(policy)
    db_session.commit()

    attempt = decide_recovery_action(db_session, incident, diagnosis, policy)

    assert attempt.selected_action == "retry"
    assert attempt.status == "approved"
    assert attempt.incentive_amount == 0


def test_grace_period_selected_when_retry_unavailable(db_session, create_merchant, create_incident_and_diagnosis):
    merchant = create_merchant()
    # Retry is inappropriate for bank-specific degradation
    incident, diagnosis = create_incident_and_diagnosis(merchant.id, "bank-specific degradation")

    policy = RecoveryPolicy(
        merchant_id=merchant.id,
        allowed_actions=["retry", "grace_period", "incentive"],
        max_incentive=1000,
        max_exposure=10000,
        approval_threshold=1000.0,
    )
    db_session.add(policy)
    db_session.commit()

    attempt = decide_recovery_action(db_session, incident, diagnosis, policy)

    # Retry is skipped because bank-specific degradation is not appropriate for retry.
    # Fallback to grace_period.
    assert attempt.selected_action == "grace_period"
    assert attempt.status == "approved"
    assert attempt.incentive_amount == 0


def test_incentive_selected_when_cheaper_actions_unavailable(db_session, create_merchant, create_incident_and_diagnosis):
    merchant = create_merchant()
    # Diagnosis is bank-specific degradation (retry ineligible), and grace_period is not in allowed actions
    incident, diagnosis = create_incident_and_diagnosis(merchant.id, "bank-specific degradation")

    policy = RecoveryPolicy(
        merchant_id=merchant.id,
        allowed_actions=["incentive"],
        max_incentive=1000,
        max_exposure=10000,
        approval_threshold=1000.0,
    )
    db_session.add(policy)
    db_session.commit()

    attempt = decide_recovery_action(db_session, incident, diagnosis, policy)

    assert attempt.selected_action == "incentive"
    assert attempt.status == "approved"
    assert attempt.incentive_amount == 500


def test_policy_blocks_incentive_above_max_incentive(db_session, create_merchant, create_incident_and_diagnosis):
    merchant = create_merchant()
    incident, diagnosis = create_incident_and_diagnosis(merchant.id, "bank-specific degradation")

    # max_incentive (400) is below default proposed incentive (500)
    policy = RecoveryPolicy(
        merchant_id=merchant.id,
        allowed_actions=["incentive"],
        max_incentive=400,
        max_exposure=10000,
        approval_threshold=1000.0,
    )
    db_session.add(policy)
    db_session.commit()

    attempt = decide_recovery_action(db_session, incident, diagnosis, policy)

    # Incentive blocked because 500 > 400. Falls back to ops_review.
    assert attempt.selected_action == "ops_review"
    assert attempt.status == "pending"


def test_exposure_limit_prevents_incentive(db_session, create_merchant, create_incident_and_diagnosis):
    merchant = create_merchant()
    incident, diagnosis = create_incident_and_diagnosis(merchant.id, "bank-specific degradation")

    # max_exposure (400) is below default proposed incentive (500)
    policy = RecoveryPolicy(
        merchant_id=merchant.id,
        allowed_actions=["incentive"],
        max_incentive=1000,
        max_exposure=400,
        approval_threshold=1000.0,
    )
    db_session.add(policy)
    db_session.commit()

    attempt = decide_recovery_action(db_session, incident, diagnosis, policy)

    # Incentive blocked due to exposure limit. Falls back to ops_review.
    assert attempt.selected_action == "ops_review"
    assert attempt.status == "pending"


def test_ops_review_when_no_automated_action_permitted(db_session, create_merchant, create_incident_and_diagnosis):
    merchant = create_merchant()
    incident, diagnosis = create_incident_and_diagnosis(merchant.id, "error-code spike")

    # Empty allowed actions
    policy = RecoveryPolicy(
        merchant_id=merchant.id,
        allowed_actions=[],
    )
    db_session.add(policy)
    db_session.commit()

    attempt = decide_recovery_action(db_session, incident, diagnosis, policy)

    assert attempt.selected_action == "ops_review"
    assert attempt.status == "pending"


def test_approval_requirement(db_session, create_merchant, create_incident_and_diagnosis):
    merchant = create_merchant()
    incident, diagnosis = create_incident_and_diagnosis(merchant.id, "bank-specific degradation")

    # approval_threshold (400.0) is below proposed incentive (500)
    policy = RecoveryPolicy(
        merchant_id=merchant.id,
        allowed_actions=["incentive"],
        max_incentive=1000,
        max_exposure=10000,
        approval_threshold=400.0,
    )
    db_session.add(policy)
    db_session.commit()

    attempt = decide_recovery_action(db_session, incident, diagnosis, policy)

    assert attempt.selected_action == "incentive"
    assert attempt.status == "pending"  # requires approval
    assert attempt.incentive_amount == 500
