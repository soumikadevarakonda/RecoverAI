from datetime import datetime, timedelta, timezone
import uuid
import pytest
from sqlalchemy import select

from app.db.session import SessionLocal
from app.domains.cohorts.detector import detect_cohort_degradations
from app.models.incident import Incident
from app.models.merchant import Merchant
from app.models.payment import Payment


@pytest.fixture
def db_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def create_merchant(db_session):
    def _create(name: str = "Test Merchant"):
        merchant = Merchant(name=name)
        db_session.add(merchant)
        db_session.commit()
        db_session.refresh(merchant)
        return merchant
    return _create


def make_payments(merchant_id, method, bank, error_code, error_step, total, failed, timestamp, amount_per_payment=1000):
    payments = []
    for i in range(total):
        is_failed = i < failed
        payments.append(
            Payment(
                merchant_id=merchant_id,
                razorpay_payment_id=f"pay_{uuid.uuid4().hex[:12]}",
                amount=amount_per_payment,
                currency="INR",
                status="failed" if is_failed else "captured",
                method=method,
                bank=bank,
                error_code=error_code,
                error_step=error_step,
                created_at=timestamp,
            )
        )
    return payments


def test_normal_cohort_not_flagged(db_session, create_merchant):
    merchant = create_merchant("Normal Cohort Merchant")
    now = datetime.now(timezone.utc)
    baseline_window = (now - timedelta(hours=2), now - timedelta(hours=1))
    current_window = (now - timedelta(hours=1), now)

    # Baseline: 100 txns, 2 failed (2% failure rate)
    base_payments = make_payments(
        merchant.id, "upi", "HDFC", "BAD_REQUEST_ERROR", "payment_authorization",
        total=100, failed=2, timestamp=now - timedelta(minutes=90), amount_per_payment=5000
    )
    # Current: 100 txns, 2 failed (2% failure rate - normal)
    curr_payments = make_payments(
        merchant.id, "upi", "HDFC", "BAD_REQUEST_ERROR", "payment_authorization",
        total=100, failed=2, timestamp=now - timedelta(minutes=30), amount_per_payment=5000
    )
    db_session.add_all(base_payments + curr_payments)
    db_session.commit()

    incidents = detect_cohort_degradations(
        db=db_session,
        merchant_id=merchant.id,
        window_start=current_window[0],
        window_end=current_window[1],
        baseline_start=baseline_window[0],
        baseline_end=baseline_window[1],
    )

    assert incidents == []


def test_degraded_cohort_flagged(db_session, create_merchant):
    merchant = create_merchant("Degraded Cohort Merchant")
    now = datetime.now(timezone.utc)
    baseline_window = (now - timedelta(hours=2), now - timedelta(hours=1))
    current_window = (now - timedelta(hours=1), now)

    # Baseline: 100 txns, 5 failed (5% failure rate)
    base_payments = make_payments(
        merchant.id, "upi", "HDFC", "GATEWAY_ERROR", "payment_authorization",
        total=100, failed=5, timestamp=now - timedelta(minutes=90), amount_per_payment=10000
    )
    # Current: 50 txns, 15 failed (30% failure rate -> +25% abs increase, 6x relative multiplier)
    curr_payments = make_payments(
        merchant.id, "upi", "HDFC", "GATEWAY_ERROR", "payment_authorization",
        total=50, failed=15, timestamp=now - timedelta(minutes=30), amount_per_payment=10000
    )
    db_session.add_all(base_payments + curr_payments)
    db_session.commit()

    incidents = detect_cohort_degradations(
        db=db_session,
        merchant_id=merchant.id,
        window_start=current_window[0],
        window_end=current_window[1],
        baseline_start=baseline_window[0],
        baseline_end=baseline_window[1],
    )

    assert len(incidents) == 1
    incident = incidents[0]
    assert incident.merchant_id == merchant.id
    assert incident.method == "upi"
    assert incident.bank == "HDFC"
    assert incident.error_code == "GATEWAY_ERROR"
    assert incident.error_step == "payment_authorization"

    # Current metrics
    assert incident.current_total_count == 50
    assert incident.current_failed_count == 15
    assert incident.current_failure_rate == 0.3
    assert incident.current_total_amount == 500000
    assert incident.current_failed_amount == 150000

    # Baseline metrics
    assert incident.baseline_total_count == 100
    assert incident.baseline_failed_count == 5
    assert incident.baseline_failure_rate == 0.05
    assert incident.baseline_total_amount == 1000000
    assert incident.baseline_failed_amount == 50000

    # Degradation calculations
    assert incident.absolute_rate_increase == 0.25
    assert incident.relative_degradation == 6.0
    assert incident.revenue_at_risk == 150000

    # Verify persisted in database
    db_incident = db_session.scalar(
        select(Incident).where(Incident.id == incident.id)
    )
    assert db_incident is not None
    assert db_incident.status == "detected"


def test_insufficient_volume_not_flagged(db_session, create_merchant):
    merchant = create_merchant("Low Volume Merchant")
    now = datetime.now(timezone.utc)
    baseline_window = (now - timedelta(hours=2), now - timedelta(hours=1))
    current_window = (now - timedelta(hours=1), now)

    # Baseline: 50 txns, 1 failed (2% failure rate)
    base_payments = make_payments(
        merchant.id, "card", "SBIN", "GATEWAY_ERROR", "payment_authorization",
        total=50, failed=1, timestamp=now - timedelta(minutes=90), amount_per_payment=2000
    )
    # Current: only 15 txns (< 20 required threshold), even with 100% failure rate
    curr_payments = make_payments(
        merchant.id, "card", "SBIN", "GATEWAY_ERROR", "payment_authorization",
        total=15, failed=15, timestamp=now - timedelta(minutes=30), amount_per_payment=2000
    )
    db_session.add_all(base_payments + curr_payments)
    db_session.commit()

    incidents = detect_cohort_degradations(
        db=db_session,
        merchant_id=merchant.id,
        window_start=current_window[0],
        window_end=current_window[1],
        baseline_start=baseline_window[0],
        baseline_end=baseline_window[1],
    )

    assert incidents == []


def test_below_threshold_degradation_not_flagged(db_session, create_merchant):
    merchant = create_merchant("Below Threshold Merchant")
    now = datetime.now(timezone.utc)
    baseline_window = (now - timedelta(hours=2), now - timedelta(hours=1))
    current_window = (now - timedelta(hours=1), now)

    # Case 1: Fails absolute increase threshold (< 0.05), but meets relative (2x)
    # Baseline: 100 txns, 1 failed (1% failure rate)
    # Current: 100 txns, 3 failed (3% failure rate -> increase is only 0.02 < 0.05)
    p_base1 = make_payments(
        merchant.id, "upi", "ICIC", "BAD_REQUEST_ERROR", "payment_authorization",
        total=100, failed=1, timestamp=now - timedelta(minutes=90)
    )
    p_curr1 = make_payments(
        merchant.id, "upi", "ICIC", "BAD_REQUEST_ERROR", "payment_authorization",
        total=100, failed=3, timestamp=now - timedelta(minutes=30)
    )

    # Case 2: Fails relative multiplier threshold (< 2x), but meets absolute (>= 0.05)
    # Baseline: 100 txns, 20 failed (20% failure rate)
    # Current: 100 txns, 26 failed (26% failure rate -> increase is 0.06 >= 0.05, but 26/20 = 1.3x < 2.0x)
    p_base2 = make_payments(
        merchant.id, "card", "KKBK", "BAD_REQUEST_ERROR", "payment_authorization",
        total=100, failed=20, timestamp=now - timedelta(minutes=90)
    )
    p_curr2 = make_payments(
        merchant.id, "card", "KKBK", "BAD_REQUEST_ERROR", "payment_authorization",
        total=100, failed=26, timestamp=now - timedelta(minutes=30)
    )

    db_session.add_all(p_base1 + p_curr1 + p_base2 + p_curr2)
    db_session.commit()

    incidents = detect_cohort_degradations(
        db=db_session,
        merchant_id=merchant.id,
        window_start=current_window[0],
        window_end=current_window[1],
        baseline_start=baseline_window[0],
        baseline_end=baseline_window[1],
    )

    assert incidents == []


def test_multiple_cohorts_detection(db_session, create_merchant):
    merchant = create_merchant("Multiple Cohorts Merchant")
    now = datetime.now(timezone.utc)
    baseline_window = (now - timedelta(hours=2), now - timedelta(hours=1))
    current_window = (now - timedelta(hours=1), now)

    # Cohort 1: Degraded (upi + HDFC) -> 5% baseline -> 25% current
    c1_base = make_payments(
        merchant.id, "upi", "HDFC", "GATEWAY_ERROR", "payment_authorization",
        total=100, failed=5, timestamp=now - timedelta(minutes=90), amount_per_payment=5000
    )
    c1_curr = make_payments(
        merchant.id, "upi", "HDFC", "GATEWAY_ERROR", "payment_authorization",
        total=40, failed=10, timestamp=now - timedelta(minutes=30), amount_per_payment=5000
    )

    # Cohort 2: Normal (card + ICIC) -> 3% baseline -> 3% current
    c2_base = make_payments(
        merchant.id, "card", "ICIC", "BAD_REQUEST_ERROR", "payment_authorization",
        total=100, failed=3, timestamp=now - timedelta(minutes=90), amount_per_payment=10000
    )
    c2_curr = make_payments(
        merchant.id, "card", "ICIC", "BAD_REQUEST_ERROR", "payment_authorization",
        total=100, failed=3, timestamp=now - timedelta(minutes=30), amount_per_payment=10000
    )

    # Cohort 3: Degraded (netbanking + SBIN) -> 2% baseline -> 20% current
    c3_base = make_payments(
        merchant.id, "netbanking", "SBIN", "GATEWAY_ERROR", "payment_authorization",
        total=50, failed=1, timestamp=now - timedelta(minutes=90), amount_per_payment=8000
    )
    c3_curr = make_payments(
        merchant.id, "netbanking", "SBIN", "GATEWAY_ERROR", "payment_authorization",
        total=30, failed=6, timestamp=now - timedelta(minutes=30), amount_per_payment=8000
    )

    db_session.add_all(c1_base + c1_curr + c2_base + c2_curr + c3_base + c3_curr)
    db_session.commit()

    incidents = detect_cohort_degradations(
        db=db_session,
        merchant_id=merchant.id,
        window_start=current_window[0],
        window_end=current_window[1],
        baseline_start=baseline_window[0],
        baseline_end=baseline_window[1],
    )

    assert len(incidents) == 2
    methods = {inc.method for inc in incidents}
    assert methods == {"upi", "netbanking"}
