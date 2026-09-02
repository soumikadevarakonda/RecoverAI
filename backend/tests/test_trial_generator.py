from datetime import datetime, timezone, timedelta
import pytest
from sqlalchemy import select, func, delete
from app.db.session import SessionLocal
from scripts.seed_trial_data import seed_trial_scenario
from app.models.merchant import Merchant
from app.models.payment import Payment
from app.models.incident import Incident
from app.models.recovery_attempt import RecoveryAttempt
from app.models.recovery_campaign import RecoveryCampaign
from app.models.recovery_policy import RecoveryPolicy
from app.models.diagnosis import Diagnosis


@pytest.fixture
def clean_db():
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


def test_deterministic_generation(clean_db):
    merchant = seed_trial_scenario(clean_db, merchant_name="Deterministic Trial Merchant")

    # Verify merchant creation
    assert merchant.name == "Deterministic Trial Merchant"
    assert merchant.id is not None

    # Verify recovery policy is seeded
    policy = clean_db.scalar(
        select(RecoveryPolicy).where(RecoveryPolicy.merchant_id == merchant.id)
    )
    assert policy is not None
    assert policy.allowed_actions == ["retry", "grace_period", "incentive", "ops_review"]

    # Verify payments count
    payments_count = clean_db.scalar(
        select(func.count(Payment.id)).where(Payment.merchant_id == merchant.id)
    )
    assert payments_count == 160

    # Verify failed payment count in baseline vs window
    failed_count = clean_db.scalar(
        select(func.count(Payment.id)).where(
            Payment.merchant_id == merchant.id,
            Payment.status == "failed"
        )
    )
    assert failed_count == 11

    # Verify past incident is seeded
    incidents_count = clean_db.scalar(
        select(func.count(Incident.id)).where(Incident.merchant_id == merchant.id)
    )
    assert incidents_count == 1

    # Verify past diagnosis is seeded
    diagnoses_count = clean_db.scalar(select(func.count(Diagnosis.id)))
    assert diagnoses_count == 1

    # Verify recovery attempts count
    attempts_count = clean_db.scalar(
        select(func.count(RecoveryAttempt.id)).where(RecoveryAttempt.merchant_id == merchant.id)
    )
    assert attempts_count == 17

    # Verify status breakdown
    recovered_count = clean_db.scalar(
        select(func.count(RecoveryAttempt.id)).where(
            RecoveryAttempt.merchant_id == merchant.id,
            RecoveryAttempt.status == "recovered"
        )
    )
    assert recovered_count == 12

    failed_attempts_count = clean_db.scalar(
        select(func.count(RecoveryAttempt.id)).where(
            RecoveryAttempt.merchant_id == merchant.id,
            RecoveryAttempt.status == "failed"
        )
    )
    assert failed_attempts_count == 2

    pending_attempts_count = clean_db.scalar(
        select(func.count(RecoveryAttempt.id)).where(
            RecoveryAttempt.merchant_id == merchant.id,
            RecoveryAttempt.status == "pending"
        )
    )
    assert pending_attempts_count == 2


def test_generation_is_strictly_reproducible(clean_db):
    # Running the seed twice on clean sessions should yield identical parameters
    merchant = seed_trial_scenario(clean_db, merchant_name="Trial Merchant 1")
    attempts1 = clean_db.scalars(
        select(RecoveryAttempt)
        .where(RecoveryAttempt.merchant_id == merchant.id)
        .order_by(RecoveryAttempt.recovery_id)
    ).all()

    # Clear and run again
    clean_db.execute(delete(RecoveryAttempt))
    clean_db.execute(delete(RecoveryCampaign))
    clean_db.execute(delete(RecoveryPolicy))
    clean_db.execute(delete(Diagnosis))
    clean_db.execute(delete(Incident))
    clean_db.execute(delete(Payment))
    clean_db.execute(delete(Merchant))
    clean_db.commit()

    merchant2 = seed_trial_scenario(clean_db, merchant_name="Trial Merchant 2")
    attempts2 = clean_db.scalars(
        select(RecoveryAttempt)
        .where(RecoveryAttempt.merchant_id == merchant2.id)
        .order_by(RecoveryAttempt.recovery_id)
    ).all()

    assert len(attempts1) == len(attempts2)
    # Check that status and selected actions match exactly for each index
    for a1, a2 in zip(attempts1, attempts2):
        assert a1.selected_action == a2.selected_action
        assert a1.status == a2.status
        assert a1.recovered_amount == a2.recovered_amount
        assert a1.incentive_amount == a2.incentive_amount


def test_seeder_idempotency_and_unrelated_merchant_preservation(clean_db):
    # 1. Seed unrelated merchant data first
    unrelated_merchant = Merchant(name="Unrelated Merchant")
    clean_db.add(unrelated_merchant)
    clean_db.commit()
    clean_db.refresh(unrelated_merchant)

    unrelated_policy = RecoveryPolicy(
        merchant_id=unrelated_merchant.id,
        allowed_actions=["retry"],
        max_incentive=100,
        max_exposure=1000,
        approval_threshold=500.0,
    )
    clean_db.add(unrelated_policy)

    unrelated_payment = Payment(
        merchant_id=unrelated_merchant.id,
        razorpay_payment_id="pay_unrelated_123",
        amount=5000,
        currency="INR",
        status="captured",
    )
    clean_db.add(unrelated_payment)
    clean_db.commit()

    # 2. First seed of trial merchant
    merchant1 = seed_trial_scenario(clean_db, merchant_name="Idempotent Trial Merchant")
    assert merchant1 is not None

    # Count records for Idempotent Trial Merchant after 1st seed
    payments_count_1 = clean_db.scalar(
        select(func.count(Payment.id)).where(Payment.merchant_id == merchant1.id)
    )
    attempts_count_1 = clean_db.scalar(
        select(func.count(RecoveryAttempt.id)).where(RecoveryAttempt.merchant_id == merchant1.id)
    )

    # 3. Second seed of the SAME trial merchant name (without cleaning DB manually)
    merchant2 = seed_trial_scenario(clean_db, merchant_name="Idempotent Trial Merchant")
    assert merchant2 is not None

    # Count records for Idempotent Trial Merchant after 2nd seed
    payments_count_2 = clean_db.scalar(
        select(func.count(Payment.id)).where(Payment.merchant_id == merchant2.id)
    )
    attempts_count_2 = clean_db.scalar(
        select(func.count(RecoveryAttempt.id)).where(RecoveryAttempt.merchant_id == merchant2.id)
    )

    # Verify counts are exactly identical (idempotency, no duplicates created)
    assert payments_count_1 == payments_count_2
    assert attempts_count_1 == attempts_count_2

    # Verify that the database does not have duplicate merchant records for the trial merchant name
    trial_merchants = clean_db.scalars(
        select(Merchant).where(Merchant.name == "Idempotent Trial Merchant")
    ).all()
    assert len(trial_merchants) == 1

    # 4. Verify unrelated merchant data remains untouched
    db_unrelated = clean_db.scalar(
        select(Merchant).where(Merchant.id == unrelated_merchant.id)
    )
    assert db_unrelated is not None
    assert db_unrelated.name == "Unrelated Merchant"

    db_unrelated_policy = clean_db.scalar(
        select(RecoveryPolicy).where(RecoveryPolicy.merchant_id == unrelated_merchant.id)
    )
    assert db_unrelated_policy is not None
    assert db_unrelated_policy.allowed_actions == ["retry"]

    db_unrelated_payment = clean_db.scalar(
        select(Payment).where(Payment.merchant_id == unrelated_merchant.id)
    )
    assert db_unrelated_payment is not None
    assert db_unrelated_payment.razorpay_payment_id == "pay_unrelated_123"

