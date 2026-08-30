from datetime import datetime, timedelta, timezone
import uuid
import pytest
from sqlalchemy import select

from app.db.session import SessionLocal
from app.domains.cohorts.service import get_cohort_metrics
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


def test_empty_cohort_metrics(db_session, create_merchant):
    merchant = create_merchant("Empty Merchant")
    now = datetime.now(timezone.utc)
    metrics = get_cohort_metrics(
        db=db_session,
        merchant_id=merchant.id,
        window_start=now - timedelta(hours=1),
        window_end=now,
    )
    assert metrics == []


def test_single_cohort_success_and_failure(db_session, create_merchant):
    merchant = create_merchant("Single Cohort Merchant")
    now = datetime.now(timezone.utc)
    t1 = now - timedelta(minutes=30)
    t2 = now - timedelta(minutes=20)
    t3 = now - timedelta(minutes=10)

    # 2 failed, 1 captured
    p1 = Payment(
        merchant_id=merchant.id,
        razorpay_payment_id=f"pay_sc_1_{uuid.uuid4().hex[:8]}",
        amount=50000,
        currency="INR",
        status="failed",
        method="upi",
        bank="HDFC",
        error_code="BAD_REQUEST_ERROR",
        error_step="payment_authorization",
        created_at=t1,
    )
    p2 = Payment(
        merchant_id=merchant.id,
        razorpay_payment_id=f"pay_sc_2_{uuid.uuid4().hex[:8]}",
        amount=30000,
        currency="INR",
        status="failed",
        method="upi",
        bank="HDFC",
        error_code="BAD_REQUEST_ERROR",
        error_step="payment_authorization",
        created_at=t2,
    )
    p3 = Payment(
        merchant_id=merchant.id,
        razorpay_payment_id=f"pay_sc_3_{uuid.uuid4().hex[:8]}",
        amount=20000,
        currency="INR",
        status="captured",
        method="upi",
        bank="HDFC",
        error_code="BAD_REQUEST_ERROR",
        error_step="payment_authorization",
        created_at=t3,
    )
    db_session.add_all([p1, p2, p3])
    db_session.commit()

    metrics = get_cohort_metrics(
        db=db_session,
        merchant_id=merchant.id,
        window_start=now - timedelta(hours=1),
        window_end=now,
    )

    assert len(metrics) == 1
    cohort = metrics[0]
    assert cohort.method == "upi"
    assert cohort.bank == "HDFC"
    assert cohort.error_code == "BAD_REQUEST_ERROR"
    assert cohort.error_step == "payment_authorization"
    assert cohort.total_count == 3
    assert cohort.failed_count == 2
    assert cohort.failure_rate == 0.6667
    assert cohort.total_amount == 100000
    assert cohort.failed_amount == 80000


def test_multiple_cohorts_aggregation(db_session, create_merchant):
    merchant = create_merchant("Multi Cohort Merchant")
    now = datetime.now(timezone.utc)

    # Cohort 1: card + HDFC + GATEWAY_ERROR
    c1_p1 = Payment(
        merchant_id=merchant.id,
        razorpay_payment_id=f"pay_mc_1_{uuid.uuid4().hex[:8]}",
        amount=150000,
        currency="INR",
        status="failed",
        method="card",
        bank="HDFC",
        error_code="GATEWAY_ERROR",
        error_step="payment_authentication",
        created_at=now - timedelta(minutes=25),
    )

    # Cohort 2: upi + SBIN + BAD_REQUEST_ERROR
    c2_p1 = Payment(
        merchant_id=merchant.id,
        razorpay_payment_id=f"pay_mc_2_{uuid.uuid4().hex[:8]}",
        amount=70000,
        currency="INR",
        status="failed",
        method="upi",
        bank="SBIN",
        error_code="BAD_REQUEST_ERROR",
        error_step="payment_authorization",
        created_at=now - timedelta(minutes=20),
    )
    c2_p2 = Payment(
        merchant_id=merchant.id,
        razorpay_payment_id=f"pay_mc_3_{uuid.uuid4().hex[:8]}",
        amount=30000,
        currency="INR",
        status="captured",
        method="upi",
        bank="SBIN",
        error_code="BAD_REQUEST_ERROR",
        error_step="payment_authorization",
        created_at=now - timedelta(minutes=15),
    )

    db_session.add_all([c1_p1, c2_p1, c2_p2])
    db_session.commit()

    metrics = get_cohort_metrics(
        db=db_session,
        merchant_id=merchant.id,
        window_start=now - timedelta(hours=1),
        window_end=now,
    )

    assert len(metrics) == 2
    # Ordered by failed_amount desc -> c1 (150000) then c2 (70000)
    assert metrics[0].method == "card"
    assert metrics[0].bank == "HDFC"
    assert metrics[0].failed_amount == 150000
    assert metrics[0].failure_rate == 1.0

    assert metrics[1].method == "upi"
    assert metrics[1].bank == "SBIN"
    assert metrics[1].failed_amount == 70000
    assert metrics[1].total_amount == 100000
    assert metrics[1].failure_rate == 0.5


def test_null_dimension_normalization(db_session, create_merchant):
    merchant = create_merchant("Null Dimension Merchant")
    now = datetime.now(timezone.utc)

    # Payment with null dimensions
    p = Payment(
        merchant_id=merchant.id,
        razorpay_payment_id=f"pay_null_{uuid.uuid4().hex[:8]}",
        amount=45000,
        currency="INR",
        status="failed",
        method=None,
        bank=None,
        error_code=None,
        error_step=None,
        created_at=now - timedelta(minutes=10),
    )
    db_session.add(p)
    db_session.commit()

    metrics = get_cohort_metrics(
        db=db_session,
        merchant_id=merchant.id,
        window_start=now - timedelta(hours=1),
        window_end=now,
    )

    assert len(metrics) == 1
    assert metrics[0].method == "UNKNOWN"
    assert metrics[0].bank == "UNKNOWN"
    assert metrics[0].error_code == "NONE"
    assert metrics[0].error_step == "NONE"
    assert metrics[0].total_amount == 45000
    assert metrics[0].failed_amount == 45000


def test_merchant_isolation(db_session, create_merchant):
    merchant_a = create_merchant("Merchant A")
    merchant_b = create_merchant("Merchant B")
    now = datetime.now(timezone.utc)

    # Payment for Merchant A
    pa = Payment(
        merchant_id=merchant_a.id,
        razorpay_payment_id=f"pay_a_{uuid.uuid4().hex[:8]}",
        amount=10000,
        currency="INR",
        status="failed",
        method="upi",
        bank="ICIC",
        error_code="GATEWAY_ERROR",
        error_step="payment_authorization",
        created_at=now - timedelta(minutes=10),
    )
    # Payment for Merchant B
    pb = Payment(
        merchant_id=merchant_b.id,
        razorpay_payment_id=f"pay_b_{uuid.uuid4().hex[:8]}",
        amount=99999,
        currency="INR",
        status="failed",
        method="upi",
        bank="ICIC",
        error_code="GATEWAY_ERROR",
        error_step="payment_authorization",
        created_at=now - timedelta(minutes=10),
    )
    db_session.add_all([pa, pb])
    db_session.commit()

    metrics_a = get_cohort_metrics(
        db=db_session,
        merchant_id=merchant_a.id,
        window_start=now - timedelta(hours=1),
        window_end=now,
    )
    assert len(metrics_a) == 1
    assert metrics_a[0].total_amount == 10000
    assert metrics_a[0].failed_amount == 10000


def test_time_window_boundaries(db_session, create_merchant):
    merchant = create_merchant("Time Window Merchant")
    now = datetime.now(timezone.utc)
    w_start = now - timedelta(hours=1)
    w_end = now

    # 1. Before window
    p_before = Payment(
        merchant_id=merchant.id,
        razorpay_payment_id=f"pay_before_{uuid.uuid4().hex[:8]}",
        amount=10000,
        currency="INR",
        status="failed",
        method="upi",
        bank="HDFC",
        error_code="GATEWAY_ERROR",
        error_step="payment_authorization",
        created_at=w_start - timedelta(minutes=5),
    )
    # 2. Inside window
    p_inside = Payment(
        merchant_id=merchant.id,
        razorpay_payment_id=f"pay_inside_{uuid.uuid4().hex[:8]}",
        amount=25000,
        currency="INR",
        status="failed",
        method="upi",
        bank="HDFC",
        error_code="GATEWAY_ERROR",
        error_step="payment_authorization",
        created_at=w_start + timedelta(minutes=20),
    )
    # 3. After window
    p_after = Payment(
        merchant_id=merchant.id,
        razorpay_payment_id=f"pay_after_{uuid.uuid4().hex[:8]}",
        amount=50000,
        currency="INR",
        status="failed",
        method="upi",
        bank="HDFC",
        error_code="GATEWAY_ERROR",
        error_step="payment_authorization",
        created_at=w_end + timedelta(minutes=5),
    )
    db_session.add_all([p_before, p_inside, p_after])
    db_session.commit()

    metrics = get_cohort_metrics(
        db=db_session,
        merchant_id=merchant.id,
        window_start=w_start,
        window_end=w_end,
    )

    assert len(metrics) == 1
    assert metrics[0].total_amount == 25000
    assert metrics[0].total_count == 1
