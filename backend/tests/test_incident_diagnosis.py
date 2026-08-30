from datetime import datetime, timedelta, timezone
import uuid
import pytest
from sqlalchemy import select

from app.db.session import SessionLocal
from app.domains.diagnosis.service import diagnose_incident
from app.models.diagnosis import Diagnosis
from app.models.incident import Incident
from app.models.merchant import Merchant
from app.models.payment import Payment
from app.models.payment_event import PaymentEvent
from app.models.webhook_event import WebhookEvent


from app.models.recovery_attempt import RecoveryAttempt
from app.models.recovery_policy import RecoveryPolicy


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
    def _create(name: str = "Diagnosis Test Merchant"):
        merchant = Merchant(name=name)
        db_session.add(merchant)
        db_session.commit()
        db_session.refresh(merchant)
        return merchant
    return _create


@pytest.fixture
def create_incident(db_session):
    def _create(merchant_id, method="upi", bank="HDFC", error_code="GATEWAY_ERROR", error_step="payment_authorization"):
        now = datetime.now(timezone.utc)
        incident = Incident(
            merchant_id=merchant_id,
            method=method,
            bank=bank,
            error_code=error_code,
            error_step=error_step,
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


def make_payment(merchant_id, method, bank, status, error_code=None, error_step=None, timestamp=None):
    if timestamp is None:
        timestamp = datetime.now(timezone.utc) - timedelta(minutes=15)
    return Payment(
        merchant_id=merchant_id,
        razorpay_payment_id=f"pay_{uuid.uuid4().hex[:12]}",
        amount=1000,
        currency="INR",
        status=status,
        method=method,
        bank=bank,
        error_code=error_code,
        error_step=error_step,
        created_at=timestamp,
    )


def test_clear_bank_specific_degradation(db_session, create_merchant, create_incident):
    merchant = create_merchant()
    incident = create_incident(merchant.id, bank="HDFC", method="upi")

    # Bank HDFC UPI has high failure rate (e.g. 8 failed, 2 success)
    p_hdfc = [
        make_payment(merchant.id, "upi", "HDFC", "failed", "GATEWAY_ERROR", "payment_authorization")
        for _ in range(8)
    ] + [
        make_payment(merchant.id, "upi", "HDFC", "captured")
        for _ in range(2)
    ]
    # Bank ICIC UPI has low failure rate (e.g. 0 failed, 10 success)
    p_icic = [
        make_payment(merchant.id, "upi", "ICIC", "captured")
        for _ in range(10)
    ]

    db_session.add_all(p_hdfc + p_icic)
    db_session.commit()

    diagnosis = diagnose_incident(db_session, incident)

    assert diagnosis.diagnosis_type == "bank-specific degradation"
    assert diagnosis.confidence >= 0.5
    assert "HDFC" in diagnosis.explanation
    assert diagnosis.supporting_evidence["bank"] == "HDFC"


def test_clear_method_level_degradation(db_session, create_merchant, create_incident):
    merchant = create_merchant()
    incident = create_incident(merchant.id, bank="HDFC", method="upi")

    # Failure rate is high across all banks on UPI
    p_upi_hdfc = [
        make_payment(merchant.id, "upi", "HDFC", "failed", "GATEWAY_ERROR", "payment_authorization")
        for _ in range(5)
    ]
    p_upi_icic = [
        make_payment(merchant.id, "upi", "ICIC", "failed", "GATEWAY_ERROR", "payment_authorization")
        for _ in range(5)
    ]
    # Meanwhile card method is perfectly healthy
    p_card = [
        make_payment(merchant.id, "card", "HDFC", "captured")
        for _ in range(10)
    ]

    db_session.add_all(p_upi_hdfc + p_upi_icic + p_card)
    db_session.commit()

    diagnosis = diagnose_incident(db_session, incident)

    assert diagnosis.diagnosis_type == "payment-method degradation"
    assert diagnosis.confidence >= 0.5
    assert "upi" in diagnosis.explanation


def test_error_code_concentration(db_session, create_merchant, create_incident):
    merchant = create_merchant()
    incident = create_incident(merchant.id, error_code="GATEWAY_ERROR")

    # 10 failed payments, 8 of which share GATEWAY_ERROR.
    # Failures are distributed across banks and steps to prevent bank/step dominance.
    p_failed = [
        make_payment(merchant.id, "upi", "HDFC", "failed", "GATEWAY_ERROR", "payment_authorization")
        for _ in range(2)
    ] + [
        make_payment(merchant.id, "upi", "ICIC", "failed", "GATEWAY_ERROR", "payment_authorization")
        for _ in range(2)
    ] + [
        make_payment(merchant.id, "upi", "HDFC", "failed", "GATEWAY_ERROR", "payment_authentication")
        for _ in range(2)
    ] + [
        make_payment(merchant.id, "upi", "ICIC", "failed", "GATEWAY_ERROR", "payment_authentication")
        for _ in range(2)
    ] + [
        make_payment(merchant.id, "upi", "HDFC", "failed", "BAD_REQUEST_ERROR", "refund_processing")
        for _ in range(1)
    ] + [
        make_payment(merchant.id, "upi", "ICIC", "failed", "BAD_REQUEST_ERROR", "setup_intent")
        for _ in range(1)
    ]
    # Plus some captured ones
    p_captured = [
        make_payment(merchant.id, "upi", "HDFC", "captured")
        for _ in range(5)
    ] + [
        make_payment(merchant.id, "upi", "ICIC", "captured")
        for _ in range(5)
    ]

    db_session.add_all(p_failed + p_captured)
    db_session.commit()

    diagnosis = diagnose_incident(db_session, incident)

    assert diagnosis.diagnosis_type == "error-code spike"
    assert diagnosis.confidence >= 0.5


def test_authorization_step_concentration(db_session, create_merchant, create_incident):
    merchant = create_merchant()
    incident = create_incident(merchant.id, error_step="payment_authorization")

    p_failed = [
        make_payment(merchant.id, "upi", "HDFC", "failed", f"ERR_H_{i}", "payment_authorization")
        for i in range(4)
    ] + [
        make_payment(merchant.id, "upi", "ICIC", "failed", f"ERR_I_{i}", "payment_authorization")
        for i in range(4)
    ]
    p_captured = [
        make_payment(merchant.id, "upi", "HDFC", "captured")
        for _ in range(5)
    ] + [
        make_payment(merchant.id, "upi", "ICIC", "captured")
        for _ in range(5)
    ]

    db_session.add_all(p_failed + p_captured)
    db_session.commit()

    diagnosis = diagnose_incident(db_session, incident)

    assert diagnosis.diagnosis_type == "authorization-step degradation"
    assert diagnosis.confidence >= 0.5
    assert "payment_authorization" in diagnosis.explanation


def test_insufficient_evidence(db_session, create_merchant, create_incident):
    merchant = create_merchant()
    incident = create_incident(merchant.id)

    # Very few transactions in window -> not enough to make a high confidence diagnosis
    p_few = [
        make_payment(merchant.id, "upi", "HDFC", "failed", "GATEWAY_ERROR", "payment_authorization")
        for _ in range(2)
    ]
    db_session.add_all(p_few)
    db_session.commit()

    diagnosis = diagnose_incident(db_session, incident)

    assert diagnosis.diagnosis_type == "insufficient evidence / unknown"
    assert diagnosis.confidence == 0.0
