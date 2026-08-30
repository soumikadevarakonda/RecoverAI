from datetime import datetime, timedelta, timezone
import pytest
from unittest.mock import MagicMock
import httpx
from sqlalchemy import select

from app.db.session import SessionLocal
from app.domains.recovery.service import execute_recovery_attempt, process_recovery_webhook
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


@pytest.fixture
def create_payment(db_session):
    def _create(merchant_id):
        payment = Payment(
            merchant_id=merchant_id,
            razorpay_payment_id="pay_failed_123",
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


def test_successful_payment_link_creation(db_session, create_merchant, create_incident, create_payment):
    merchant = create_merchant()
    incident = create_incident(merchant.id)
    payment = create_payment(merchant.id)

    attempt = RecoveryAttempt(
        recovery_id="rec_exec_success",
        merchant_id=merchant.id,
        incident_id=incident.id,
        payment_id=payment.id,
        selected_action="incentive",
        incentive_amount=500,
        status="approved",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    db_session.add(attempt)
    db_session.commit()

    # Mock client response
    mock_client = MagicMock(spec=RazorpayClient)
    mock_client.create_payment_link.return_value = {
        "id": "plink_success_123",
        "short_url": "https://rzp.io/i/success_123",
        "status": "created",
    }

    executed_attempt = execute_recovery_attempt(db_session, attempt, client=mock_client)

    # Check state updates
    assert executed_attempt.status == "executed"
    assert executed_attempt.payment_link_id == "plink_success_123"
    assert executed_attempt.short_url == "https://rzp.io/i/success_123"

    # Check client call arguments (50000 original - 500 incentive = 49500)
    mock_client.create_payment_link.assert_called_once()
    args, kwargs = mock_client.create_payment_link.call_args
    assert kwargs["amount"] == 49500
    assert kwargs["reference_id"] == "rec_exec_success"


def test_payment_link_creation_api_failure(db_session, create_merchant, create_incident, create_payment):
    merchant = create_merchant()
    incident = create_incident(merchant.id)
    payment = create_payment(merchant.id)

    attempt = RecoveryAttempt(
        recovery_id="rec_exec_fail",
        merchant_id=merchant.id,
        incident_id=incident.id,
        payment_id=payment.id,
        selected_action="incentive",
        incentive_amount=500,
        status="approved",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    db_session.add(attempt)
    db_session.commit()

    # Mock API raises error
    mock_client = MagicMock(spec=RazorpayClient)
    mock_client.create_payment_link.side_effect = httpx.HTTPStatusError(
        "API Error",
        request=httpx.Request("POST", "http://test"),
        response=httpx.Response(500, text="Internal Error")
    )

    with pytest.raises(httpx.HTTPStatusError):
        execute_recovery_attempt(db_session, attempt, client=mock_client)

    # Ensure attempt status was not changed from approved
    db_session.refresh(attempt)
    assert attempt.status == "approved"
    assert attempt.payment_link_id is None


def test_recovery_webhook_matched_and_successful_update(db_session, create_merchant, create_incident, create_payment):
    merchant = create_merchant()
    incident = create_incident(merchant.id)
    payment = create_payment(merchant.id)

    attempt = RecoveryAttempt(
        recovery_id="rec_webhook_match",
        merchant_id=merchant.id,
        incident_id=incident.id,
        payment_id=payment.id,
        selected_action="incentive",
        incentive_amount=500,
        status="executed",
        payment_link_id="plink_match_123",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    db_session.add(attempt)
    db_session.commit()

    # Webhook payload for payment_link.paid
    payload = {
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": "plink_match_123",
                    "amount": 49500,
                    "amount_paid": 49500,
                    "currency": "INR",
                    "reference_id": "rec_webhook_match",
                    "status": "paid",
                }
            },
            "payment": {
                "entity": {
                    "id": "pay_recovered_789",
                    "amount": 49500,
                    "status": "captured",
                }
            }
        }
    }

    process_recovery_webhook(db_session, payload)
    db_session.commit()

    # Verify attempt was updated successfully
    db_session.refresh(attempt)
    assert attempt.status == "recovered"
    assert attempt.resulting_payment_id == "pay_recovered_789"
    assert attempt.recovered_amount == 49500


def test_unmatched_recovery_id_raises_value_error(db_session):
    payload = {
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": "plink_unmatched",
                    "reference_id": "nonexistent_recovery_id",
                    "status": "paid",
                }
            },
            "payment": {
                "entity": {
                    "id": "pay_unmatched",
                    "amount": 1000,
                    "status": "captured",
                }
            }
        }
    }

    with pytest.raises(ValueError, match="No RecoveryAttempt found"):
        process_recovery_webhook(db_session, payload)


def test_duplicate_recovery_webhook(db_session, create_merchant, create_incident):
    merchant = create_merchant()
    incident = create_incident(merchant.id)

    attempt = RecoveryAttempt(
        recovery_id="rec_dup_webhook",
        merchant_id=merchant.id,
        incident_id=incident.id,
        selected_action="grace_period",
        status="recovered",  # Already recovered
        payment_link_id="plink_dup",
        resulting_payment_id="pay_old",
        recovered_amount=1000,
    )
    db_session.add(attempt)
    db_session.commit()

    payload = {
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": "plink_dup",
                    "reference_id": "rec_dup_webhook",
                    "status": "paid",
                }
            },
            "payment": {
                "entity": {
                    "id": "pay_new",
                    "amount": 1000,
                    "status": "captured",
                }
            }
        }
    }

    # Should run and return early without mutating the record or raising errors
    process_recovery_webhook(db_session, payload)
    db_session.commit()

    db_session.refresh(attempt)
    assert attempt.status == "recovered"
    assert attempt.resulting_payment_id == "pay_old"
    assert attempt.recovered_amount == 1000


def test_payment_link_mismatch_rejected(db_session, create_merchant, create_incident):
    merchant = create_merchant()
    incident = create_incident(merchant.id)

    attempt = RecoveryAttempt(
        recovery_id="rec_mismatch",
        merchant_id=merchant.id,
        incident_id=incident.id,
        selected_action="retry",
        status="executed",
        payment_link_id="plink_expected_abc",
    )
    db_session.add(attempt)
    db_session.commit()

    payload = {
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": "plink_mismatched_xyz",  # Mismatched ID
                    "reference_id": "rec_mismatch",
                    "status": "paid",
                }
            },
            "payment": {
                "entity": {
                    "id": "pay_mismatch",
                    "amount": 1000,
                    "status": "captured",
                }
            }
        }
    }

    with pytest.raises(ValueError, match="does not match expected attempt link ID"):
        process_recovery_webhook(db_session, payload)
