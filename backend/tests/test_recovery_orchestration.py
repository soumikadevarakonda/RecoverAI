from datetime import datetime, timedelta, timezone
import pytest
from unittest.mock import MagicMock
from sqlalchemy import select

from app.db.session import SessionLocal
from app.domains.recovery.service import orchestrate_recovery
from app.integrations.razorpay import RazorpayClient
from app.models.merchant import Merchant
from app.models.incident import Incident
from app.models.payment import Payment
from app.models.payment_event import PaymentEvent
from app.models.webhook_event import WebhookEvent
from app.models.recovery_policy import RecoveryPolicy
from app.models.recovery_attempt import RecoveryAttempt
from app.models.diagnosis import Diagnosis


@pytest.fixture
def db_session():
    session = SessionLocal()
    try:
        session.query(RecoveryAttempt).delete()
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


import uuid

def make_payment(db_session, merchant_id, method, bank, status, error_code=None, error_step=None):
    payment = Payment(
        merchant_id=merchant_id,
        razorpay_payment_id=f"pay_{uuid.uuid4().hex[:12]}",
        amount=10000,
        currency="INR",
        status=status,
        method=method,
        bank=bank,
        error_code=error_code,
        error_step=error_step,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=15),
    )
    db_session.add(payment)
    return payment


def test_incident_diagnosis_recovery_decision_pipeline(db_session, create_merchant, create_incident):
    merchant = create_merchant()
    incident = create_incident(merchant.id)

    # Allow grace_period in policy
    policy = RecoveryPolicy(
        merchant_id=merchant.id,
        allowed_actions=["grace_period"],
    )
    db_session.add(policy)
    db_session.commit()

    mock_client = MagicMock(spec=RazorpayClient)
    mock_client.create_payment_link.return_value = {
        "id": "plink_pipeline_123",
        "short_url": "https://rzp.io/i/orch_pipe",
        "status": "created",
    }

    attempt = orchestrate_recovery(db_session, incident, client=mock_client)

    # Pipeline Assertions
    assert attempt is not None
    assert attempt.merchant_id == merchant.id
    assert attempt.incident_id == incident.id
    assert attempt.selected_action == "grace_period"
    assert attempt.status == "executed"  # executed automatically as it was approved

    # Check that diagnosis row was created too
    diag = db_session.scalar(
        select(Diagnosis).where(Diagnosis.incident_id == incident.id)
    )
    assert diag is not None
    assert diag.confidence == 0.0  # insufficient evidence because no payment records were seeded in window


def test_automatically_executable_recovery(db_session, create_merchant, create_incident):
    merchant = create_merchant()
    incident = create_incident(merchant.id)

    policy = RecoveryPolicy(
        merchant_id=merchant.id,
        allowed_actions=["grace_period"],
    )
    db_session.add(policy)
    db_session.commit()

    mock_client = MagicMock(spec=RazorpayClient)
    mock_client.create_payment_link.return_value = {
        "id": "plink_orchestrated_123",
        "short_url": "https://rzp.io/i/orch_123",
        "status": "created",
    }

    attempt = orchestrate_recovery(db_session, incident, client=mock_client)

    assert attempt.status == "executed"
    assert attempt.payment_link_id == "plink_orchestrated_123"
    mock_client.create_payment_link.assert_called_once()


def test_approval_required_recovery_stops_before_execution(db_session, create_merchant, create_incident):
    merchant = create_merchant()
    incident = create_incident(merchant.id)

    # Seed 10 failed payments, 8 of which share GATEWAY_ERROR.
    # Distributed across banks and steps.
    for _ in range(2):
        make_payment(db_session, merchant.id, "upi", "HDFC", "failed", "GATEWAY_ERROR", "payment_authorization")
    for _ in range(2):
        make_payment(db_session, merchant.id, "upi", "ICIC", "failed", "GATEWAY_ERROR", "payment_authorization")
    for _ in range(2):
        make_payment(db_session, merchant.id, "upi", "HDFC", "failed", "GATEWAY_ERROR", "payment_authentication")
    for _ in range(2):
        make_payment(db_session, merchant.id, "upi", "ICIC", "failed", "GATEWAY_ERROR", "payment_authentication")
    make_payment(db_session, merchant.id, "upi", "HDFC", "failed", "BAD_REQUEST_ERROR", "refund_processing")
    make_payment(db_session, merchant.id, "upi", "ICIC", "failed", "BAD_REQUEST_ERROR", "setup_intent")

    # Plus some captured ones
    for _ in range(5):
        make_payment(db_session, merchant.id, "upi", "HDFC", "captured")
    for _ in range(5):
        make_payment(db_session, merchant.id, "upi", "ICIC", "captured")

    db_session.commit()

    # approval_threshold (400.0) is below default proposed incentive (500)
    policy = RecoveryPolicy(
        merchant_id=merchant.id,
        allowed_actions=["incentive"],
        max_incentive=1000,
        max_exposure=10000,
        approval_threshold=400.0,
    )
    db_session.add(policy)
    db_session.commit()

    mock_client = MagicMock(spec=RazorpayClient)

    attempt = orchestrate_recovery(db_session, incident, client=mock_client)

    # stops at pending approval, client is not called
    assert attempt.selected_action == "incentive"
    assert attempt.status == "pending"
    assert attempt.payment_link_id is None
    mock_client.create_payment_link.assert_not_called()


def test_ops_review_stops_before_execution(db_session, create_merchant, create_incident):
    merchant = create_merchant()
    incident = create_incident(merchant.id)

    # Empty actions list forces ops_review
    policy = RecoveryPolicy(
        merchant_id=merchant.id,
        allowed_actions=[],
    )
    db_session.add(policy)
    db_session.commit()

    mock_client = MagicMock(spec=RazorpayClient)

    attempt = orchestrate_recovery(db_session, incident, client=mock_client)

    assert attempt.selected_action == "ops_review"
    assert attempt.status == "pending"
    mock_client.create_payment_link.assert_not_called()


def test_repeated_orchestration_is_idempotent(db_session, create_merchant, create_incident):
    merchant = create_merchant()
    incident = create_incident(merchant.id)

    policy = RecoveryPolicy(
        merchant_id=merchant.id,
        allowed_actions=["grace_period"],
    )
    db_session.add(policy)
    db_session.commit()

    mock_client = MagicMock(spec=RazorpayClient)
    mock_client.create_payment_link.return_value = {
        "id": "plink_idem_1",
        "short_url": "https://rzp.io/i/idem1",
        "status": "created",
    }

    attempt1 = orchestrate_recovery(db_session, incident, client=mock_client)
    attempt2 = orchestrate_recovery(db_session, incident, client=mock_client)

    # Should be the identical attempt row
    assert attempt1.id == attempt2.id
    assert attempt2.payment_link_id == "plink_idem_1"

    # Client was only called once
    mock_client.create_payment_link.assert_called_once()

    # Verify database has only 1 RecoveryAttempt total
    all_attempts = db_session.scalars(select(RecoveryAttempt)).all()
    assert len(all_attempts) == 1
