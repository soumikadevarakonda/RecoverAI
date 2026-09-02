from datetime import datetime, timedelta, timezone
import pytest
from app.db.session import SessionLocal
from app.domains.recovery.trial_evaluation import evaluate_trial_scenario
from app.models.merchant import Merchant
from app.models.payment import Payment
from app.models.incident import Incident
from app.models.recovery_attempt import RecoveryAttempt
from app.models.recovery_campaign import RecoveryCampaign
from app.models.recovery_policy import RecoveryPolicy
from app.models.diagnosis import Diagnosis
from tests.test_recovery_strategist import MockLLMProvider


@pytest.fixture
def db_session():
    session = SessionLocal()
    try:
        session.query(RecoveryAttempt).delete()
        session.query(RecoveryCampaign).delete()
        session.query(RecoveryPolicy).delete()
        session.query(Diagnosis).delete()
        session.query(Incident).delete()
        session.query(Payment).delete()
        session.query(Merchant).delete()
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
def seed_payments(db_session):
    def _seed(merchant_id, start_time, count, failed_count, method="upi", bank="HDFC", amount=2000):
        import uuid
        for i in range(count):
            is_failed = (i < failed_count)
            # Use 4 of GATEWAY_ERROR / payment_authorization and the rest of INSUFFICIENT_FUNDS / payment_authentication
            # to keep error-code and authorization-step concentrations under 50%
            if is_failed:
                error_code = "GATEWAY_ERROR" if i < 4 else "INSUFFICIENT_FUNDS"
                error_step = "payment_authorization" if i < 4 else "payment_authentication"
            else:
                error_code = "GATEWAY_ERROR"
                error_step = "payment_authorization"
            pay = Payment(
                merchant_id=merchant_id,
                razorpay_payment_id=f"pay_{uuid.uuid4().hex[:12]}",
                status="failed" if is_failed else "captured",
                amount=amount,
                currency="INR",
                method=method,
                bank=bank,
                error_code=error_code,
                error_step=error_step,
                created_at=start_time + timedelta(minutes=i),
            )
            db_session.add(pay)
        db_session.commit()
    return _seed


def test_successful_degraded_cohort_evaluation(db_session, create_merchant, create_policy, seed_payments):
    m = create_merchant()
    create_policy(m.id)

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(minutes=30)
    window_end = now
    baseline_start = now - timedelta(hours=2)
    baseline_end = now - timedelta(hours=1)

    # Seed degraded current window: 30 payments, 10 failed -> 33.3% failure
    seed_payments(m.id, window_start, 30, 10)
    # Seed healthy baseline: 30 payments, 1 failed -> 3.3% failure
    seed_payments(m.id, baseline_start, 30, 1)

    # Run trial evaluation
    res = evaluate_trial_scenario(
        db_session, m.id, window_start, window_end,
        baseline_start=baseline_start, baseline_end=baseline_end
    )

    assert res.incident_detected is True
    assert res.incident_id is not None
    assert res.diagnosis_type == "bank-specific degradation"
    # Degradation bank-specific block retry. Next best candidate by priority is grace_period.
    assert res.selected_action == "grace_period"
    assert res.requires_approval is False
    assert res.execution_permitted is True


def test_no_incident_detected(db_session, create_merchant, create_policy, seed_payments):
    m = create_merchant()
    create_policy(m.id)

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(minutes=30)
    window_end = now

    # Healthy payments only
    seed_payments(m.id, window_start, 30, 0)

    res = evaluate_trial_scenario(db_session, m.id, window_start, window_end)

    assert res.incident_detected is False
    assert res.selected_action is None


def test_policy_blocked_action(db_session, create_merchant, create_policy, seed_payments):
    m = create_merchant()
    # Only allow incentive
    create_policy(m.id, allowed_actions=["incentive"])

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(minutes=30)
    window_end = now
    baseline_start = now - timedelta(hours=2)
    baseline_end = now - timedelta(hours=1)

    # Seed degraded UPI HDFC (triggers bank-specific degradation)
    seed_payments(m.id, window_start, 30, 10)
    seed_payments(m.id, baseline_start, 30, 1)

    res = evaluate_trial_scenario(
        db_session, m.id, window_start, window_end,
        baseline_start=baseline_start, baseline_end=baseline_end
    )

    assert res.incident_detected is True
    # retry and grace_period are blocked by policy, so it should fall back to incentive (which is policy-allowed)
    assert res.selected_action == "incentive"
    assert res.requires_approval is False  # incentive cost is 500, which is < approval_threshold (5000.0)
    assert res.execution_permitted is True


def test_strategist_failure_falling_back_safely(db_session, create_merchant, create_policy, seed_payments):
    m = create_merchant()
    create_policy(m.id)

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(minutes=30)
    window_end = now
    baseline_start = now - timedelta(hours=2)
    baseline_end = now - timedelta(hours=1)

    seed_payments(m.id, window_start, 30, 10)
    seed_payments(m.id, baseline_start, 30, 1)

    # Supplying a provider that fails
    bad_provider = MockLLMProvider(response_data=Exception("Gemini API connection error"))

    res = evaluate_trial_scenario(
        db_session, m.id, window_start, window_end,
        baseline_start=baseline_start, baseline_end=baseline_end,
        llm_provider=bad_provider
    )

    # The evaluation must succeed by falling back to deterministic action (grace_period)
    assert res.incident_detected is True
    assert res.selected_action == "grace_period"
    assert res.requires_approval is False


def test_merchant_isolation(db_session, create_merchant, create_policy, seed_payments):
    m1 = create_merchant("Merchant 1")
    m2 = create_merchant("Merchant 2")
    create_policy(m1.id)
    create_policy(m2.id)

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(minutes=30)
    window_end = now
    baseline_start = now - timedelta(hours=2)
    baseline_end = now - timedelta(hours=1)

    # Seed degraded current window for Merchant 2 ONLY
    seed_payments(m2.id, window_start, 30, 10)
    seed_payments(m2.id, baseline_start, 30, 1)

    # Verify that query for Merchant 1 detects nothing (isolation)
    res1 = evaluate_trial_scenario(
        db_session, m1.id, window_start, window_end,
        baseline_start=baseline_start, baseline_end=baseline_end
    )
    assert res1.incident_detected is False

    # Verify that query for Merchant 2 detects the incident
    res2 = evaluate_trial_scenario(
        db_session, m2.id, window_start, window_end,
        baseline_start=baseline_start, baseline_end=baseline_end
    )
    assert res2.incident_detected is True


def test_evaluation_deterministic_defaults(db_session):
    from scripts.seed_trial_data import seed_trial_scenario
    # Seed using trial scenario seeder (which now uses TRIAL_REFERENCE_TIME)
    merchant = seed_trial_scenario(db_session, "Evaluation Defaults Merchant")
    
    # We must align the payments in this test to force bank-specific degradation
    # so that evaluate_trial_scenario returns the expected incident
    from app.domains.recovery.trial_evaluation import TRIAL_REFERENCE_TIME
    from app.models.payment import Payment
    from sqlalchemy import select, update
    
    # Align payment dimensions for HDFC UPI cohort to allow fractional degradation detection
    # and isolate bank-specific degradation (preventing error-code spike or step degradation from dominating)
    window_start = TRIAL_REFERENCE_TIME - timedelta(minutes=30)
    window_end = TRIAL_REFERENCE_TIME + timedelta(minutes=5)
    baseline_start = TRIAL_REFERENCE_TIME - timedelta(hours=2)
    baseline_end = TRIAL_REFERENCE_TIME - timedelta(hours=1)

    curr_payments = db_session.scalars(
        select(Payment)
        .where(
            Payment.merchant_id == merchant.id,
            Payment.created_at >= window_start,
            Payment.created_at <= window_end,
        )
        .order_by(Payment.created_at.asc())
    ).all()

    for idx, pay in enumerate(curr_payments):
        if pay.status == "failed":
            if idx < 4:
                pay.error_code = "GATEWAY_ERROR"
                pay.error_step = "payment_authorization"
            else:
                pay.error_code = "INSUFFICIENT_FUNDS"
                pay.error_step = "payment_authentication"
        else:
            pay.error_code = "GATEWAY_ERROR"
            pay.error_step = "payment_authorization"

    base_payments = db_session.scalars(
        select(Payment)
        .where(
            Payment.merchant_id == merchant.id,
            Payment.created_at >= baseline_start,
            Payment.created_at <= baseline_end,
        )
        .order_by(Payment.created_at.asc())
    ).all()

    for idx, pay in enumerate(base_payments):
        if pay.status == "failed":
            pay.error_code = "GATEWAY_ERROR"
            pay.error_step = "payment_authorization"
        else:
            pay.error_code = "GATEWAY_ERROR"
            pay.error_step = "payment_authorization"

    db_session.commit()

    # Run evaluate_trial_scenario with default (None) boundaries
    res = evaluate_trial_scenario(db_session, merchant.id)

    assert res.incident_detected is True
    assert res.diagnosis_type == "bank-specific degradation"
    assert res.selected_action == "incentive"
