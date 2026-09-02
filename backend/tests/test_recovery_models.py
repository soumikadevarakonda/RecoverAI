from datetime import datetime, timedelta, timezone
import uuid
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.session import SessionLocal
from app.models.diagnosis import Diagnosis
from app.models.incident import Incident
from app.models.merchant import Merchant
from app.models.payment import Payment
from app.models.payment_event import PaymentEvent
from app.models.recovery_attempt import RecoveryAttempt
from app.models.recovery_campaign import RecoveryCampaign
from app.models.recovery_policy import RecoveryPolicy
from app.models.webhook_event import WebhookEvent


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
def create_incident(db_session):
    def _create(merchant_id):
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
        return incident
    return _create


@pytest.fixture
def create_payment(db_session):
    def _create(merchant_id):
        payment = Payment(
            merchant_id=merchant_id,
            razorpay_payment_id=f"pay_{uuid.uuid4().hex[:12]}",
            amount=50000,
            currency="INR",
            status="failed",
            method="upi",
            bank="HDFC",
            error_code="GATEWAY_ERROR",
            error_step="payment_authorization",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(payment)
        db_session.commit()
        db_session.refresh(payment)
        return payment
    return _create


def test_policy_creation(db_session, create_merchant):
    merchant = create_merchant()

    policy = RecoveryPolicy(
        merchant_id=merchant.id,
        max_incentive=5000,
        max_exposure=100000,
        allowed_actions=["retry", "incentive"],
        approval_threshold=0.6,
    )
    db_session.add(policy)
    db_session.commit()
    db_session.refresh(policy)

    assert policy.id is not None
    assert policy.max_incentive == 5000
    assert policy.max_exposure == 100000
    assert policy.allowed_actions == ["retry", "incentive"]
    assert policy.approval_threshold == 0.6


def test_attempt_creation(db_session, create_merchant, create_incident, create_payment):
    merchant = create_merchant()
    incident = create_incident(merchant.id)
    payment = create_payment(merchant.id)

    attempt = RecoveryAttempt(
        recovery_id="rec_attempt_123",
        merchant_id=merchant.id,
        incident_id=incident.id,
        payment_id=payment.id,
        selected_action="retry",
        incentive_amount=1000,
        status="pending",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        recovered_amount=0,
    )
    db_session.add(attempt)
    db_session.commit()
    db_session.refresh(attempt)

    assert attempt.id is not None
    assert attempt.recovery_id == "rec_attempt_123"
    assert attempt.status == "pending"
    assert attempt.incentive_amount == 1000
    assert attempt.recovered_amount == 0


def test_unique_recovery_id(db_session, create_merchant, create_incident):
    merchant = create_merchant()
    incident = create_incident(merchant.id)

    att1 = RecoveryAttempt(
        recovery_id="rec_dup",
        merchant_id=merchant.id,
        incident_id=incident.id,
        selected_action="grace_period",
    )
    att2 = RecoveryAttempt(
        recovery_id="rec_dup",
        merchant_id=merchant.id,
        incident_id=incident.id,
        selected_action="incentive",
    )
    db_session.add(att1)
    db_session.commit()

    db_session.add(att2)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_merchant_isolation(db_session, create_merchant, create_incident):
    merchant_a = create_merchant("Merchant A")
    merchant_b = create_merchant("Merchant B")
    incident_a = create_incident(merchant_a.id)

    att_a = RecoveryAttempt(
        recovery_id="rec_a",
        merchant_id=merchant_a.id,
        incident_id=incident_a.id,
        selected_action="retry",
    )
    db_session.add(att_a)
    db_session.commit()

    # Querying isolation
    retrieved = db_session.scalars(
        select(RecoveryAttempt).where(RecoveryAttempt.merchant_id == merchant_a.id)
    ).all()
    assert len(retrieved) == 1
    assert retrieved[0].recovery_id == "rec_a"

    retrieved_b = db_session.scalars(
        select(RecoveryAttempt).where(RecoveryAttempt.merchant_id == merchant_b.id)
    ).all()
    assert len(retrieved_b) == 0


def test_invalid_negative_monetary_values(db_session, create_merchant, create_incident):
    merchant = create_merchant()
    incident = create_incident(merchant.id)

    # RecoveryPolicy check constraint
    p_invalid = RecoveryPolicy(
        merchant_id=merchant.id,
        max_incentive=-1,
        max_exposure=100,
    )
    db_session.add(p_invalid)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    p_invalid_exp = RecoveryPolicy(
        merchant_id=merchant.id,
        max_incentive=10,
        max_exposure=-10,
    )
    db_session.add(p_invalid_exp)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # RecoveryAttempt check constraint
    att_invalid_inc = RecoveryAttempt(
        recovery_id="rec_neg_inc",
        merchant_id=merchant.id,
        incident_id=incident.id,
        selected_action="incentive",
        incentive_amount=-5,
    )
    db_session.add(att_invalid_inc)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    att_invalid_rec = RecoveryAttempt(
        recovery_id="rec_neg_rec",
        merchant_id=merchant.id,
        incident_id=incident.id,
        selected_action="incentive",
        recovered_amount=-100,
    )
    db_session.add(att_invalid_rec)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_relationships(db_session, create_merchant, create_incident, create_payment):
    merchant = create_merchant()
    incident = create_incident(merchant.id)
    payment = create_payment(merchant.id)

    policy = RecoveryPolicy(
        merchant_id=merchant.id,
        max_incentive=1000,
        max_exposure=50000,
    )
    attempt = RecoveryAttempt(
        recovery_id="rec_rel",
        merchant_id=merchant.id,
        incident_id=incident.id,
        payment_id=payment.id,
        selected_action="ops_review",
    )
    db_session.add_all([policy, attempt])
    db_session.commit()
    db_session.refresh(policy)
    db_session.refresh(attempt)

    # Relationships checks
    assert policy.merchant.id == merchant.id
    assert attempt.merchant.id == merchant.id
    assert attempt.incident.id == incident.id
    assert attempt.payment.id == payment.id

    # Backref checks
    assert policy in merchant.recovery_policies
    assert attempt in merchant.recovery_attempts
    assert attempt in incident.recovery_attempts
    assert attempt in payment.recovery_attempts
