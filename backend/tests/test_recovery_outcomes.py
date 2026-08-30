from datetime import datetime, timedelta, timezone
import uuid
import pytest
from app.db.session import SessionLocal
from app.domains.recovery.outcomes import calculate_recovery_performance
from app.models.merchant import Merchant
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
def create_incident(db_session):
    def _create(merchant_id, method="upi", bank="HDFC", error_code="BAD_REQUEST", revenue_at_risk=10000):
        now = datetime.now(timezone.utc)
        incident = Incident(
            merchant_id=merchant_id,
            method=method,
            bank=bank,
            error_code=error_code,
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
def create_attempt(db_session):
    def _create(merchant_id, incident_id, status="recovered", selected_action="retry", recovered_amount=0):
        attempt = RecoveryAttempt(
            recovery_id=f"rec_{uuid.uuid4().hex[:12]}",
            merchant_id=merchant_id,
            incident_id=incident_id,
            selected_action=selected_action,
            status=status,
            recovered_amount=recovered_amount,
        )
        db_session.add(attempt)
        db_session.commit()
        db_session.refresh(attempt)
        return attempt
    return _create


def test_all_attempts_recovered(db_session, create_merchant, create_incident, create_attempt):
    m = create_merchant()
    inc = create_incident(m.id, revenue_at_risk=10000)
    create_attempt(m.id, inc.id, status="recovered", recovered_amount=9500)
    create_attempt(m.id, inc.id, status="recovered", recovered_amount=10000)

    perf = calculate_recovery_performance(db_session, m.id)
    assert perf.total_attempts == 2
    assert perf.recovered_attempts == 2
    assert perf.observed_recovery_rate == 1.0
    assert perf.total_revenue_at_risk == 20000
    assert perf.total_recovered_amount == 19500


def test_mixed_recovered_failed_attempts(db_session, create_merchant, create_incident, create_attempt):
    m = create_merchant()
    inc = create_incident(m.id, revenue_at_risk=10000)
    create_attempt(m.id, inc.id, status="recovered", recovered_amount=10000)
    create_attempt(m.id, inc.id, status="failed", recovered_amount=0)

    perf = calculate_recovery_performance(db_session, m.id)
    assert perf.total_attempts == 2
    assert perf.recovered_attempts == 1
    assert perf.observed_recovery_rate == 0.5
    assert perf.total_revenue_at_risk == 20000
    assert perf.total_recovered_amount == 10000


def test_failed_expired_attempts_only(db_session, create_merchant, create_incident, create_attempt):
    m = create_merchant()
    inc = create_incident(m.id, revenue_at_risk=10000)
    create_attempt(m.id, inc.id, status="failed", recovered_amount=0)
    create_attempt(m.id, inc.id, status="expired", recovered_amount=0)

    perf = calculate_recovery_performance(db_session, m.id)
    assert perf.total_attempts == 2
    assert perf.recovered_attempts == 0
    assert perf.observed_recovery_rate == 0.0
    assert perf.total_revenue_at_risk == 20000
    assert perf.total_recovered_amount == 0


def test_incomplete_attempts_excluded(db_session, create_merchant, create_incident, create_attempt):
    m = create_merchant()
    inc = create_incident(m.id, revenue_at_risk=10000)
    
    # Incomplete ones (should be ignored)
    create_attempt(m.id, inc.id, status="pending", recovered_amount=0)
    create_attempt(m.id, inc.id, status="approved", recovered_amount=0)
    create_attempt(m.id, inc.id, status="executed", recovered_amount=0)

    # Completed ones
    create_attempt(m.id, inc.id, status="recovered", recovered_amount=8000)

    perf = calculate_recovery_performance(db_session, m.id)
    assert perf.total_attempts == 1
    assert perf.recovered_attempts == 1
    assert perf.observed_recovery_rate == 1.0
    assert perf.total_revenue_at_risk == 10000
    assert perf.total_recovered_amount == 8000


def test_merchant_isolation(db_session, create_merchant, create_incident, create_attempt):
    m1 = create_merchant("M1")
    m2 = create_merchant("M2")
    inc1 = create_incident(m1.id, revenue_at_risk=10000)
    inc2 = create_incident(m2.id, revenue_at_risk=10000)

    create_attempt(m1.id, inc1.id, status="recovered", recovered_amount=10000)
    create_attempt(m2.id, inc2.id, status="recovered", recovered_amount=5000)

    perf = calculate_recovery_performance(db_session, m1.id)
    assert perf.total_attempts == 1
    assert perf.recovered_attempts == 1
    assert perf.total_recovered_amount == 10000


def test_cohort_isolation(db_session, create_merchant, create_incident, create_attempt):
    m = create_merchant()
    # Distinct cohorts
    inc_card = create_incident(m.id, method="card", bank="HDFC", error_code="EXP_LIMIT", revenue_at_risk=10000)
    inc_upi = create_incident(m.id, method="upi", bank="SBI", error_code="NET_ERR", revenue_at_risk=20000)

    create_attempt(m.id, inc_card.id, status="recovered", recovered_amount=9000, selected_action="retry")
    create_attempt(m.id, inc_upi.id, status="recovered", recovered_amount=19000, selected_action="grace_period")

    # Filter by card only
    perf_card = calculate_recovery_performance(db_session, m.id, method="card")
    assert perf_card.total_attempts == 1
    assert perf_card.total_revenue_at_risk == 10000
    assert perf_card.total_recovered_amount == 9000

    # Filter by action only
    perf_gp = calculate_recovery_performance(db_session, m.id, selected_action="grace_period")
    assert perf_gp.total_attempts == 1
    assert perf_gp.total_revenue_at_risk == 20000
    assert perf_gp.total_recovered_amount == 19000


def test_zero_completed_attempts(db_session, create_merchant):
    m = create_merchant()
    perf = calculate_recovery_performance(db_session, m.id)
    assert perf.total_attempts == 0
    assert perf.recovered_attempts == 0
    assert perf.observed_recovery_rate == 0.0
    assert perf.total_revenue_at_risk == 0
    assert perf.total_recovered_amount == 0
