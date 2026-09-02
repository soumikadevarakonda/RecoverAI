import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
import random

# Add backend directory to sys.path
sys.path.append(str(Path(__file__).resolve().parents[1]))

from sqlalchemy.orm import Session
from app.db.session import SessionLocal
from app.models.merchant import Merchant
from app.models.payment import Payment
from app.models.incident import Incident
from app.models.diagnosis import Diagnosis
from app.models.recovery_policy import RecoveryPolicy
from app.models.recovery_attempt import RecoveryAttempt
from app.models.recovery_campaign import RecoveryCampaign
from app.domains.recovery.trial_evaluation import TRIAL_REFERENCE_TIME


def seed_trial_scenario(db: Session, merchant_name: str = "Trial Merchant") -> Merchant:
    """
    Deterministically seeds a merchant with a realistic batch of synthetic payment,
    incident, and recovery attempt data.
    """
    from sqlalchemy import select, delete
    from app.models.payment_event import PaymentEvent

    # Identify the trial merchant using the deterministic name and clean up existing records
    existing_merchants = db.scalars(
        select(Merchant).where(Merchant.name == merchant_name)
    ).all()

    for existing_merchant in existing_merchants:
        # Clean up records in correct safe order to avoid FK violation
        from app.models.recovery_audit_event import RecoveryAuditEvent
        db.execute(
            delete(RecoveryAuditEvent).where(RecoveryAuditEvent.merchant_id == existing_merchant.id)
        )
        db.execute(
            delete(RecoveryAttempt).where(RecoveryAttempt.merchant_id == existing_merchant.id)
        )
        db.execute(
            delete(RecoveryCampaign).where(RecoveryCampaign.merchant_id == existing_merchant.id)
        )
        db.execute(
            delete(RecoveryPolicy).where(RecoveryPolicy.merchant_id == existing_merchant.id)
        )
        db.execute(
            delete(Diagnosis).where(
                Diagnosis.incident_id.in_(
                    select(Incident.id).where(Incident.merchant_id == existing_merchant.id)
                )
            )
        )
        db.execute(
            delete(Incident).where(Incident.merchant_id == existing_merchant.id)
        )
        db.execute(
            delete(PaymentEvent).where(
                PaymentEvent.payment_id.in_(
                    select(Payment.id).where(Payment.merchant_id == existing_merchant.id)
                )
            )
        )
        db.execute(
            delete(Payment).where(Payment.merchant_id == existing_merchant.id)
        )
        db.execute(
            delete(Merchant).where(Merchant.id == existing_merchant.id)
        )
        db.commit()

    # Force deterministic randomness
    random.seed(42)

    # 1. Create merchant
    merchant = Merchant(name=merchant_name)
    db.add(merchant)
    db.commit()
    db.refresh(merchant)

    # 2. Create merchant policy
    policy = RecoveryPolicy(
        merchant_id=merchant.id,
        allowed_actions=["retry", "grace_period", "incentive", "ops_review"],
        max_incentive=1000,
        max_exposure=20000,
        approval_threshold=3000.0,
    )
    db.add(policy)
    db.commit()

    now = TRIAL_REFERENCE_TIME

    # 3. Create payment methods and banks list
    methods = ["upi", "card", "netbanking"]
    banks = ["HDFC", "SBI", "ICICI", "Axis"]
    error_codes = ["GATEWAY_ERROR", "BAD_REQUEST_ERROR", "INSUFFICIENT_FUNDS"]

    # --- HISTORICAL NORMAL & FAILED TRAFFIC (e.g. 1 day ago) ---
    # Create 100 historical normal payments
    for i in range(100):
        created_at = now - timedelta(days=1) + timedelta(minutes=i * 10)
        pay = Payment(
            merchant_id=merchant.id,
            razorpay_payment_id=f"pay_hist_ok_{i}_{random.randint(1000, 9999)}",
            status="captured",
            amount=random.choice([50000, 100000, 150000, 200000]),
            currency="INR",
            method=random.choice(methods),
            bank=random.choice(banks),
            created_at=created_at,
        )
        db.add(pay)

    # --- PAST DEGRADED INCIDENTS & ATTEMPTS (e.g. 12 hours ago) ---
    # Create past incident for upi + SBI cohort
    past_inc_time = now - timedelta(hours=12)
    past_inc = Incident(
        merchant_id=merchant.id,
        method="upi",
        bank="SBI",
        error_code="GATEWAY_ERROR",
        error_step="payment_authorization",
        current_total_count=30,
        current_failed_count=10,
        current_failure_rate=0.33,
        current_total_amount=300000,
        current_failed_amount=100000,
        baseline_total_count=30,
        baseline_failed_count=1,
        baseline_failure_rate=0.03,
        baseline_total_amount=300000,
        baseline_failed_amount=10000,
        absolute_rate_increase=0.3,
        relative_degradation=10.0,
        revenue_at_risk=100000,
        window_start=past_inc_time - timedelta(minutes=30),
        window_end=past_inc_time,
        baseline_start=past_inc_time - timedelta(hours=2),
        baseline_end=past_inc_time - timedelta(hours=1),
        status="diagnosed",
        created_at=past_inc_time,
    )
    db.add(past_inc)
    db.commit()
    db.refresh(past_inc)

    past_diag = Diagnosis(
        incident_id=past_inc.id,
        diagnosis_type="bank-specific degradation",
        explanation="SBI UPI gateway failure spike",
        supporting_evidence={},
        confidence=0.95,
        created_at=past_inc_time,
    )
    db.add(past_diag)
    db.commit()

    # Seed historical campaign and attempts for this past incident (historically executed)
    past_campaign = RecoveryCampaign(
        campaign_id=f"camp_hist_{merchant.id.hex[:6]}",
        merchant_id=merchant.id,
        incident_id=past_inc.id,
        selected_action="retry",
        status="completed",
        target_payment_count=17,
        total_revenue_at_risk=170000,
        per_attempt_incentive=0,
        total_incentive_cost=2500,
        created_at=past_inc_time,
    )
    db.add(past_campaign)
    db.flush()

    # We want 5 retry successes and some failures
    for j in range(5):
        attempt = RecoveryAttempt(
            recovery_id=f"rec_hist_retry_{j}_{random.randint(1000, 9999)}",
            merchant_id=merchant.id,
            incident_id=past_inc.id,
            campaign_id=past_campaign.id,
            selected_action="retry",
            status="recovered",
            recovered_amount=10000,
            incentive_amount=0,
            created_at=past_inc_time + timedelta(minutes=j * 5),
        )
        db.add(attempt)

    for j in range(5):
        # 5 grace period attempts (3 recovered, 2 failed)
        attempt = RecoveryAttempt(
            recovery_id=f"rec_hist_gp_{j}_{random.randint(1000, 9999)}",
            merchant_id=merchant.id,
            incident_id=past_inc.id,
            campaign_id=past_campaign.id,
            selected_action="grace_period",
            status="recovered" if j < 3 else "failed",
            recovered_amount=10000 if j < 3 else 0,
            incentive_amount=0,
            created_at=past_inc_time + timedelta(minutes=j * 5 + 30),
        )
        db.add(attempt)

    for j in range(5):
        # 5 incentive attempts (4 recovered, 1 expired)
        attempt = RecoveryAttempt(
            recovery_id=f"rec_hist_inc_{j}_{random.randint(1000, 9999)}",
            merchant_id=merchant.id,
            incident_id=past_inc.id,
            campaign_id=past_campaign.id,
            selected_action="incentive",
            status="recovered" if j < 4 else "expired",
            recovered_amount=10000 if j < 4 else 0,
            incentive_amount=500,
            created_at=past_inc_time + timedelta(minutes=j * 5 + 60),
        )
        db.add(attempt)

    # Incomplete/Active attempts (2 ops_review in pending state)
    for j in range(2):
        attempt = RecoveryAttempt(
            recovery_id=f"rec_hist_ops_{j}_{random.randint(1000, 9999)}",
            merchant_id=merchant.id,
            incident_id=past_inc.id,
            campaign_id=past_campaign.id,
            selected_action="ops_review",
            status="pending",
            recovered_amount=0,
            incentive_amount=0,
            created_at=past_inc_time + timedelta(minutes=j * 5 + 90),
        )
        db.add(attempt)

    # --- CURRENT DEGRADED COHORT TRAFFIC (upi + HDFC) ---
    # Create baseline window for current incident (30 payments, 1 failed, 29 ok)
    # Match error_code and error_step for captured payments to group them in the same cohort
    base_start = now - timedelta(hours=2)
    for i in range(30):
        is_failed = (i == 0)
        pay = Payment(
            merchant_id=merchant.id,
            razorpay_payment_id=f"pay_curr_base_{i}_{random.randint(1000, 9999)}",
            status="failed" if is_failed else "captured",
            amount=10000,
            currency="INR",
            method="upi",
            bank="HDFC",
            error_code="GATEWAY_ERROR",
            error_step="payment_authorization",
            created_at=base_start + timedelta(minutes=i * 2),
        )
        db.add(pay)

    # Create current window for current incident (30 payments, 10 failed, 20 ok)
    # 8 failures concentrated under GATEWAY_ERROR, 2 as noise under other codes
    # 20 captured matching the GATEWAY_ERROR cohort
    curr_start = now - timedelta(minutes=30)
    for i in range(30):
        is_failed = (i < 10)
        if is_failed:
            if i < 8:
                err_code = "GATEWAY_ERROR"
                err_step = "payment_authorization"
            elif i == 8:
                err_code = "BAD_REQUEST_ERROR"
                err_step = "payment_authorization"
            else:
                err_code = "INSUFFICIENT_FUNDS"
                err_step = "payment_authorization"
        else:
            err_code = "GATEWAY_ERROR"
            err_step = "payment_authorization"

        pay = Payment(
            merchant_id=merchant.id,
            razorpay_payment_id=f"pay_curr_window_{i}_{random.randint(1000, 9999)}",
            status="failed" if is_failed else "captured",
            amount=10000,
            currency="INR",
            method="upi",
            bank="HDFC",
            error_code=err_code,
            error_step=err_step,
            created_at=curr_start + timedelta(minutes=i),
        )
        db.add(pay)

    db.commit()
    return merchant


if __name__ == "__main__":
    db = SessionLocal()
    try:
        print("Seeding deterministic trial scenario data...")
        merchant = seed_trial_scenario(db)
        print(f"Successfully seeded Trial Merchant: '{merchant.name}' (ID: {merchant.id})")
    finally:
        db.close()
