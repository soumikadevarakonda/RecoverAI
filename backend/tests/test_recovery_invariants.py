from datetime import datetime, timezone, timedelta
import pytest
from sqlalchemy import select, delete
from app.db.session import SessionLocal
from app.domains.recovery.service import process_recovery_webhook
from app.models.merchant import Merchant
from app.models.payment import Payment
from app.models.incident import Incident
from app.models.recovery_attempt import RecoveryAttempt
from app.models.recovery_campaign import RecoveryCampaign
from app.models.recovery_policy import RecoveryPolicy
from app.models.diagnosis import Diagnosis


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
    def _create(name: str = "Merchant"):
        merchant = Merchant(name=name)
        db_session.add(merchant)
        db_session.commit()
        db_session.refresh(merchant)
        return merchant
    return _create


@pytest.fixture
def create_attempt(db_session):
    def _create(merchant_id, recovery_id="rec_test_123", payment_link_id="plink_expected_123", amount=50000, incentive=500):
        # Create payment
        import uuid
        pay = Payment(
            merchant_id=merchant_id,
            razorpay_payment_id=f"pay_{uuid.uuid4().hex[:12]}",
            amount=amount,
            currency="INR",
            status="failed",
        )
        db_session.add(pay)
        db_session.commit()

        # Create incident
        now = datetime.now(timezone.utc)
        inc = Incident(
            merchant_id=merchant_id,
            method="upi",
            bank="HDFC",
            error_code="GATEWAY_ERROR",
            error_step="payment_authorization",
            current_total_count=30,
            current_failed_count=10,
            current_failure_rate=0.33,
            current_total_amount=30000,
            current_failed_amount=10000,
            baseline_total_count=30,
            baseline_failed_count=1,
            baseline_failure_rate=0.03,
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
        db_session.add(inc)
        db_session.commit()

        attempt = RecoveryAttempt(
            recovery_id=recovery_id,
            merchant_id=merchant_id,
            incident_id=inc.id,
            payment_id=pay.id,
            selected_action="incentive",
            incentive_amount=incentive,
            status="executed",
            payment_link_id=payment_link_id,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        )
        db_session.add(attempt)
        db_session.commit()
        db_session.refresh(attempt)
        return attempt
    return _create


def test_invariant_missing_payment_link_id(db_session, create_merchant, create_attempt):
    m = create_merchant()
    attempt = create_attempt(m.id, payment_link_id=None)

    payload = {
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": "plink_xyz",
                    "reference_id": attempt.recovery_id,
                    "status": "paid",
                }
            },
            "payment": {
                "entity": {
                    "id": "pay_xyz",
                    "amount": 49500,
                    "status": "captured",
                }
            }
        }
    }

    with pytest.raises(ValueError, match="does not have a matching payment link stored"):
        process_recovery_webhook(db_session, payload)


def test_invariant_mismatched_payment_link_id(db_session, create_merchant, create_attempt):
    m = create_merchant()
    attempt = create_attempt(m.id, payment_link_id="plink_expected")

    payload = {
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": "plink_mismatched",
                    "reference_id": attempt.recovery_id,
                    "status": "paid",
                }
            },
            "payment": {
                "entity": {
                    "id": "pay_xyz",
                    "amount": 49500,
                    "status": "captured",
                }
            }
        }
    }

    with pytest.raises(ValueError, match="does not match expected attempt link ID"):
        process_recovery_webhook(db_session, payload)


def test_invariant_link_not_paid_status(db_session, create_merchant, create_attempt):
    m = create_merchant()
    attempt = create_attempt(m.id, payment_link_id="plink_expected")

    payload = {
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": "plink_expected",
                    "reference_id": attempt.recovery_id,
                    "status": "created",  # Not paid
                }
            },
            "payment": {
                "entity": {
                    "id": "pay_xyz",
                    "amount": 49500,
                    "status": "captured",
                }
            }
        }
    }

    with pytest.raises(ValueError, match="Payment link is not in a paid state"):
        process_recovery_webhook(db_session, payload)


def test_invariant_payment_not_successful(db_session, create_merchant, create_attempt):
    m = create_merchant()
    attempt = create_attempt(m.id, payment_link_id="plink_expected")

    payload = {
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": "plink_expected",
                    "reference_id": attempt.recovery_id,
                    "status": "paid",
                }
            },
            "payment": {
                "entity": {
                    "id": "pay_xyz",
                    "amount": 49500,
                    "status": "failed",  # Not successful status
                }
            }
        }
    }

    with pytest.raises(ValueError, match="Razorpay payment does not have a successful status"):
        process_recovery_webhook(db_session, payload)


def test_invariant_recovered_amount_exceeds_expected(db_session, create_merchant, create_attempt):
    m = create_merchant()
    # original = 50000, incentive = 500. Expected recovery amount = 49500.
    attempt = create_attempt(m.id, payment_link_id="plink_expected", amount=50000, incentive=500)

    payload = {
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": "plink_expected",
                    "reference_id": attempt.recovery_id,
                    "status": "paid",
                }
            },
            "payment": {
                "entity": {
                    "id": "pay_xyz",
                    "amount": 50000,  # Exceeds 49500
                    "status": "captured",
                }
            }
        }
    }

    with pytest.raises(ValueError, match="exceeds expected recovery amount"):
        process_recovery_webhook(db_session, payload)


def test_invariant_recovered_amount_underpaid_rejected(db_session, create_merchant, create_attempt):
    m = create_merchant()
    # original = 50000, incentive = 500. Expected recovery charge = 49500.
    attempt = create_attempt(m.id, payment_link_id="plink_underpay", amount=50000, incentive=500)

    payload = {
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": "plink_underpay",
                    "reference_id": attempt.recovery_id,
                    "status": "paid",
                }
            },
            "payment": {
                "entity": {
                    "id": "pay_under",
                    "amount": 20000,  # Underpayment: 20000 < 49500
                    "status": "captured",
                }
            }
        }
    }

    with pytest.raises(ValueError, match="is less than expected recovery charge"):
        process_recovery_webhook(db_session, payload)


def test_invariant_idempotency_duplicate_webhook(db_session, create_merchant, create_attempt):
    m = create_merchant()
    attempt = create_attempt(m.id, payment_link_id="plink_expected")

    # Mark as recovered
    attempt.status = "recovered"
    attempt.recovered_amount = 49500
    db_session.commit()

    payload = {
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": "plink_expected",
                    "reference_id": attempt.recovery_id,
                    "status": "paid",
                }
            },
            "payment": {
                "entity": {
                    "id": "pay_new",
                    "amount": 1000,  # Different amount
                    "status": "captured",
                }
            }
        }
    }

    # Should return early silently without changing amount or raising error
    process_recovery_webhook(db_session, payload)
    db_session.commit()

    db_session.refresh(attempt)
    assert attempt.status == "recovered"
    assert attempt.recovered_amount == 49500


def test_invariant_no_backwards_transition(db_session, create_merchant, create_attempt):
    m = create_merchant()
    attempt = create_attempt(m.id, payment_link_id="plink_expected")

    attempt.status = "recovered"
    db_session.commit()

    # Attempting to move backwards should raise a ValueError
    with pytest.raises(ValueError, match="must never transition backwards from 'recovered'"):
        attempt.status = "pending"
