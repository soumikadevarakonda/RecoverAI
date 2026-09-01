from datetime import datetime, timedelta, timezone
import pytest
from app.db.session import SessionLocal
from app.domains.recovery.batch_measurement import calculate_batch_measurement
from app.models.merchant import Merchant
from app.models.payment import Payment
from app.models.incident import Incident
from app.models.recovery_attempt import RecoveryAttempt
from app.models.recovery_policy import RecoveryPolicy
from app.models.diagnosis import Diagnosis


@pytest.fixture
def db_session():
    session = SessionLocal()
    try:
        session.query(RecoveryAttempt).delete()
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
def create_payment(db_session):
    def _create(merchant_id, status="failed", amount=10000, created_at=None):
        if created_at is None:
            created_at = datetime.now(timezone.utc)
        import uuid
        pay = Payment(
            merchant_id=merchant_id,
            razorpay_payment_id=f"pay_{uuid.uuid4().hex[:12]}",
            status=status,
            amount=amount,
            currency="INR",
            created_at=created_at,
        )
        db_session.add(pay)
        db_session.commit()
        db_session.refresh(pay)
        return pay
    return _create


@pytest.fixture
def create_incident(db_session):
    def _create(merchant_id, revenue_at_risk=10000, created_at=None):
        if created_at is None:
            created_at = datetime.now(timezone.utc)
        inc = Incident(
            merchant_id=merchant_id,
            method="upi",
            bank="HDFC",
            error_code="BAD_REQUEST",
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
            revenue_at_risk=revenue_at_risk,
            window_start=created_at - timedelta(minutes=30),
            window_end=created_at,
            baseline_start=created_at - timedelta(hours=2),
            baseline_end=created_at - timedelta(hours=1),
            status="detected",
            created_at=created_at,
        )
        db_session.add(inc)
        db_session.commit()
        db_session.refresh(inc)
        return inc
    return _create


@pytest.fixture
def create_attempt(db_session):
    def _create(merchant_id, incident_id, status="recovered", selected_action="retry", recovered_amount=0, incentive_amount=0, created_at=None):
        if created_at is None:
            created_at = datetime.now(timezone.utc)
        import uuid
        attempt = RecoveryAttempt(
            recovery_id=f"rec_{uuid.uuid4().hex[:12]}",
            merchant_id=merchant_id,
            incident_id=incident_id,
            selected_action=selected_action,
            status=status,
            incentive_amount=incentive_amount,
            recovered_amount=recovered_amount,
            created_at=created_at,
        )
        db_session.add(attempt)
        db_session.commit()
        db_session.refresh(attempt)
        return attempt
    return _create


def test_empty_batch(db_session, create_merchant):
    m = create_merchant()
    start = datetime.now(timezone.utc) - timedelta(hours=1)
    end = datetime.now(timezone.utc) + timedelta(hours=1)

    res = calculate_batch_measurement(db_session, m.id, start, end)

    assert res.total_transactions_analyzed == 0
    assert res.failed_transactions == 0
    assert res.revenue_at_risk == 0
    assert res.eligible_recovery_attempts == 0
    assert res.completed_recovery_attempts == 0
    assert res.recovered_attempts == 0
    assert res.actual_recovered_amount == 0
    assert res.gross_recovery_rate == 0.0
    assert res.intervention_cost == 0
    assert res.net_recovered_amount == 0
    assert "retry" in res.action_breakdowns


def test_mixed_recovered_failed_attempts(db_session, create_merchant, create_payment, create_incident, create_attempt):
    m = create_merchant()
    start = datetime.now(timezone.utc) - timedelta(minutes=10)
    end = datetime.now(timezone.utc) + timedelta(minutes=10)

    # Analyzed payments
    create_payment(m.id, status="captured", amount=15000, created_at=datetime.now(timezone.utc))
    create_payment(m.id, status="failed", amount=10000, created_at=datetime.now(timezone.utc))
    create_payment(m.id, status="failed", amount=20000, created_at=datetime.now(timezone.utc))

    # Incident with revenue at risk
    inc = create_incident(m.id, revenue_at_risk=30000, created_at=datetime.now(timezone.utc))

    # Recovery attempts (1 recovered, 1 failed)
    create_attempt(m.id, inc.id, status="recovered", selected_action="retry", recovered_amount=10000, incentive_amount=0)
    create_attempt(m.id, inc.id, status="failed", selected_action="retry", recovered_amount=0, incentive_amount=0)

    res = calculate_batch_measurement(db_session, m.id, start, end)

    assert res.total_transactions_analyzed == 3
    assert res.failed_transactions == 2
    assert res.revenue_at_risk == 30000
    assert res.eligible_recovery_attempts == 2
    assert res.completed_recovery_attempts == 2
    assert res.recovered_attempts == 1
    assert res.actual_recovered_amount == 10000
    # gross_rate = 10000 / 30000 = 0.3333333333333333
    assert abs(res.gross_recovery_rate - 0.3333) < 0.001


def test_incomplete_attempts_excluded(db_session, create_merchant, create_payment, create_incident, create_attempt):
    m = create_merchant()
    start = datetime.now(timezone.utc) - timedelta(minutes=10)
    end = datetime.now(timezone.utc) + timedelta(minutes=10)

    inc = create_incident(m.id, revenue_at_risk=10000)

    # Active/incomplete attempts (should be excluded from completed, but counted in eligible and cost)
    create_attempt(m.id, inc.id, status="pending", selected_action="incentive", incentive_amount=200, recovered_amount=0)
    create_attempt(m.id, inc.id, status="approved", selected_action="incentive", incentive_amount=200, recovered_amount=0)
    create_attempt(m.id, inc.id, status="executed", selected_action="incentive", incentive_amount=200, recovered_amount=0)

    # Completed attempt
    create_attempt(m.id, inc.id, status="recovered", selected_action="incentive", incentive_amount=200, recovered_amount=8000)

    res = calculate_batch_measurement(db_session, m.id, start, end)

    assert res.eligible_recovery_attempts == 4
    assert res.completed_recovery_attempts == 1
    assert res.recovered_attempts == 1
    assert res.actual_recovered_amount == 8000
    assert res.intervention_cost == 800  # 4 * 200
    assert res.net_recovered_amount == 7200  # 8000 - 800


def test_merchant_isolation(db_session, create_merchant, create_payment, create_incident, create_attempt):
    m1 = create_merchant("Merchant 1")
    m2 = create_merchant("Merchant 2")
    start = datetime.now(timezone.utc) - timedelta(minutes=10)
    end = datetime.now(timezone.utc) + timedelta(minutes=10)

    # Data for Merchant 1
    create_payment(m1.id, status="failed", amount=10000)
    inc1 = create_incident(m1.id, revenue_at_risk=10000)
    create_attempt(m1.id, inc1.id, status="recovered", selected_action="retry", recovered_amount=10000, incentive_amount=0)

    # Data for Merchant 2 (should be ignored by m1 query)
    create_payment(m2.id, status="failed", amount=20000)
    inc2 = create_incident(m2.id, revenue_at_risk=20000)
    create_attempt(m2.id, inc2.id, status="recovered", selected_action="retry", recovered_amount=20000, incentive_amount=0)

    res1 = calculate_batch_measurement(db_session, m1.id, start, end)
    assert res1.failed_transactions == 1
    assert res1.revenue_at_risk == 10000
    assert res1.actual_recovered_amount == 10000


def test_action_level_breakdown(db_session, create_merchant, create_incident, create_attempt):
    m = create_merchant()
    start = datetime.now(timezone.utc) - timedelta(minutes=10)
    end = datetime.now(timezone.utc) + timedelta(minutes=10)

    inc = create_incident(m.id, revenue_at_risk=20000)

    # Retry attempts: 2 eligible, 1 recovered, 0 cost
    create_attempt(m.id, inc.id, status="recovered", selected_action="retry", recovered_amount=5000, incentive_amount=0)
    create_attempt(m.id, inc.id, status="failed", selected_action="retry", recovered_amount=0, incentive_amount=0)

    # Incentive attempts: 1 eligible, 1 recovered, 500 cost, 8000 recovered
    create_attempt(m.id, inc.id, status="recovered", selected_action="incentive", recovered_amount=8000, incentive_amount=500)

    res = calculate_batch_measurement(db_session, m.id, start, end)

    breakdowns = res.action_breakdowns
    assert "retry" in breakdowns
    assert breakdowns["retry"].eligible_recovery_attempts == 2
    assert breakdowns["retry"].completed_recovery_attempts == 2
    assert breakdowns["retry"].recovered_attempts == 1
    assert breakdowns["retry"].actual_recovered_amount == 5000
    assert breakdowns["retry"].intervention_cost == 0
    assert breakdowns["retry"].net_recovered_amount == 5000
    assert breakdowns["retry"].gross_recovery_rate == 0.5  # 1 / 2

    assert "incentive" in breakdowns
    assert breakdowns["incentive"].eligible_recovery_attempts == 1
    assert breakdowns["incentive"].completed_recovery_attempts == 1
    assert breakdowns["incentive"].recovered_attempts == 1
    assert breakdowns["incentive"].actual_recovered_amount == 8000
    assert breakdowns["incentive"].intervention_cost == 500
    assert breakdowns["incentive"].net_recovered_amount == 7500
    assert breakdowns["incentive"].gross_recovery_rate == 1.0  # 1 / 1

