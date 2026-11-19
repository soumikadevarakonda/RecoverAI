"""
Database-backed tests for the closed-loop additions: cohort grouping and
dedup, finite degradation ratios, attempt expiry, ops_review execution block,
and the dev checkout simulator (settles through webhook verification).

Run only against a dedicated test database (see tests/conftest.py).
"""
from datetime import datetime, timedelta, timezone
import uuid

import pytest
from fastapi.testclient import TestClient

from app.db.session import SessionLocal
from app.domains.cohorts.detector import detect_cohort_degradations
from app.domains.recovery.service import expire_stale_attempts
from app.main import app
from app.models.diagnosis import Diagnosis
from app.models.incident import Incident
from app.models.merchant import Merchant
from app.models.payment import Payment
from app.models.payment_event import PaymentEvent
from app.models.recovery_attempt import RecoveryAttempt
from app.models.recovery_audit_event import RecoveryAuditEvent
from app.models.recovery_campaign import RecoveryCampaign
from app.models.recovery_policy import RecoveryPolicy
from app.models.webhook_event import WebhookEvent


@pytest.fixture
def db_session():
    session = SessionLocal()
    try:
        session.query(RecoveryAuditEvent).delete()
        session.query(RecoveryAttempt).delete()
        session.query(RecoveryCampaign).delete()
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
def merchant(db_session):
    m = Merchant(name="Lifecycle Merchant")
    db_session.add(m)
    db_session.commit()
    db_session.refresh(m)
    return m


def _payment(merchant_id, *, status, at, amount=100_000, error_code=None, error_step=None):
    return Payment(
        merchant_id=merchant_id,
        razorpay_payment_id=f"pay_{uuid.uuid4().hex[:14]}",
        amount=amount,
        currency="INR",
        status=status,
        method="upi",
        bank="HDFC",
        error_code=error_code,
        error_step=error_step,
        created_at=at,
    )


def _incident(merchant_id, status="recovering"):
    now = datetime.now(timezone.utc)
    return Incident(
        merchant_id=merchant_id, method="upi", bank="HDFC",
        error_code="GATEWAY_ERROR", error_step="payment_authorization",
        current_total_count=30, current_failed_count=10, current_failure_rate=0.33,
        current_total_amount=3_000_000, current_failed_amount=1_000_000,
        baseline_total_count=30, baseline_failed_count=1, baseline_failure_rate=0.03,
        baseline_total_amount=3_000_000, baseline_failed_amount=100_000,
        absolute_rate_increase=0.3, relative_degradation=10.0, revenue_at_risk=1_000_000,
        window_start=now - timedelta(minutes=30), window_end=now,
        baseline_start=now - timedelta(hours=2), baseline_end=now - timedelta(hours=1),
        status=status,
    )


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------

def _seed_degradation(db, merchant_id, now, baseline_failed=1):
    window_start, window_end = now - timedelta(minutes=30), now
    base_start = now - timedelta(hours=2)
    payments = []
    for i in range(30):  # baseline
        failed = i < baseline_failed
        payments.append(_payment(
            merchant_id, status="failed" if failed else "captured",
            at=base_start + timedelta(minutes=i),
            error_code="GATEWAY_ERROR" if failed else None,
            error_step="payment_authorization" if failed else None,
        ))
    for i in range(30):  # current: 15 failed
        failed = i < 15
        payments.append(_payment(
            merchant_id, status="failed" if failed else "captured",
            at=window_start + timedelta(minutes=i % 29),
            error_code="GATEWAY_ERROR" if failed else None,
            error_step="payment_authorization" if failed else None,
        ))
    db.add_all(payments)
    db.commit()
    return window_start, window_end, now - timedelta(hours=2), now - timedelta(hours=1)


def test_captured_payments_do_not_need_an_error_code_to_be_in_the_cohort(db_session, merchant):
    now = datetime.now(timezone.utc)
    ws, we, bs, be = _seed_degradation(db_session, merchant.id, now)

    incidents = detect_cohort_degradations(db_session, merchant.id, ws, we, bs, be)

    assert len(incidents) == 1
    inc = incidents[0]
    assert (inc.method, inc.bank) == ("upi", "HDFC")
    assert inc.error_code == "GATEWAY_ERROR"
    assert inc.current_total_count == 30 and inc.current_failed_count == 15
    assert inc.current_failure_rate == 0.5
    assert inc.revenue_at_risk == 15 * 100_000


def test_detection_is_not_duplicated_for_an_active_incident(db_session, merchant):
    now = datetime.now(timezone.utc)
    ws, we, bs, be = _seed_degradation(db_session, merchant.id, now)

    first = detect_cohort_degradations(db_session, merchant.id, ws, we, bs, be)
    second = detect_cohort_degradations(db_session, merchant.id, ws, we, bs, be)

    assert len(first) == 1
    assert second == []


def test_zero_baseline_failures_give_finite_ratio_and_audit_event(db_session, merchant):
    now = datetime.now(timezone.utc)
    ws, we, bs, be = _seed_degradation(db_session, merchant.id, now, baseline_failed=0)

    incidents = detect_cohort_degradations(db_session, merchant.id, ws, we, bs, be)

    assert len(incidents) == 1
    ratio = incidents[0].relative_degradation
    assert ratio == ratio and ratio != float("inf")  # finite, not NaN
    audit = db_session.query(RecoveryAuditEvent).filter_by(incident_id=incidents[0].id).all()
    assert [e.event_type for e in audit] == ["INCIDENT_DETECTED"]


# --------------------------------------------------------------------------
# Expiry and completion
# --------------------------------------------------------------------------

def test_stale_attempts_expire_and_complete_the_campaign(db_session, merchant):
    incident = _incident(merchant.id)
    db_session.add(incident)
    db_session.flush()
    campaign = RecoveryCampaign(
        campaign_id=f"camp_{uuid.uuid4().hex[:10]}", merchant_id=merchant.id,
        incident_id=incident.id, selected_action="retry", status="executing",
        target_payment_count=2, total_revenue_at_risk=200_000,
    )
    db_session.add(campaign)
    db_session.flush()
    now = datetime.now(timezone.utc)
    paid = RecoveryAttempt(
        recovery_id=f"rec_{uuid.uuid4().hex[:10]}", merchant_id=merchant.id, incident_id=incident.id,
        campaign_id=campaign.id, selected_action="retry", status="recovered",
        recovered_amount=100_000, expires_at=now - timedelta(hours=1),
    )
    unpaid = RecoveryAttempt(
        recovery_id=f"rec_{uuid.uuid4().hex[:10]}", merchant_id=merchant.id, incident_id=incident.id,
        campaign_id=campaign.id, selected_action="retry", status="executed",
        expires_at=now - timedelta(minutes=5),
    )
    live = RecoveryAttempt(
        recovery_id=f"rec_{uuid.uuid4().hex[:10]}", merchant_id=merchant.id, incident_id=incident.id,
        campaign_id=campaign.id, selected_action="retry", status="executed",
        expires_at=now + timedelta(hours=1),
    )
    db_session.add_all([paid, unpaid, live])
    db_session.commit()

    assert expire_stale_attempts(db_session, merchant.id) == 1
    db_session.refresh(unpaid), db_session.refresh(paid), db_session.refresh(live)
    assert unpaid.status == "expired"
    assert paid.status == "recovered"      # terminal states are never touched
    assert live.status == "executed"       # not expired yet -> campaign stays open
    db_session.refresh(campaign)
    assert campaign.status == "executing"

    # Once the last open attempt expires the campaign completes and the incident resolves.
    live.expires_at = now - timedelta(seconds=1)
    db_session.commit()
    assert expire_stale_attempts(db_session, merchant.id) == 1
    db_session.refresh(campaign), db_session.refresh(incident)
    assert campaign.status == "completed"
    assert incident.status == "resolved"


# --------------------------------------------------------------------------
# API: ops_review block, dev simulator
# --------------------------------------------------------------------------

def _attempt_with_payment(db, merchant_id, *, action="retry", status="approved",
                          short_url=None, link_id=None, incentive=0):
    incident = _incident(merchant_id)
    db.add(incident)
    payment = _payment(merchant_id, status="failed", at=datetime.now(timezone.utc), amount=150_000)
    db.add(payment)
    db.flush()
    attempt = RecoveryAttempt(
        recovery_id=f"rec_{uuid.uuid4().hex[:10]}", merchant_id=merchant_id, incident_id=incident.id,
        payment_id=payment.id, selected_action=action, status=status, incentive_amount=incentive,
        payment_link_id=link_id, short_url=short_url,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    db.add(attempt)
    db.commit()
    db.refresh(attempt)
    return attempt


def test_ops_review_cannot_be_executed_as_a_payment_link(db_session, merchant):
    attempt = _attempt_with_payment(db_session, merchant.id, action="ops_review", status="approved")
    client = TestClient(app)
    resp = client.post(
        f"/api/v1/merchant/recoveries/{attempt.recovery_id}/execute",
        headers={"X-Merchant-ID": str(merchant.id)},
    )
    assert resp.status_code == 400
    db_session.refresh(attempt)
    assert attempt.status == "approved" and attempt.payment_link_id is None


def test_dev_simulator_settles_through_backend_verification(db_session, merchant):
    attempt = _attempt_with_payment(
        db_session, merchant.id, status="executed", incentive=10_000,
        link_id="plink_sim1", short_url="/pay.html?id=plink_sim1&ref=x",
    )
    attempt_id = attempt.recovery_id
    client = TestClient(app)
    headers = {"X-Merchant-ID": str(merchant.id)}

    resp = client.post(f"/api/v1/dev/payment-links/{attempt_id}/simulate-paid", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # The amount is derived server-side from the stored payment minus the incentive
    assert body["status"] == "recovered" and body["recovered_amount"] == 140_000

    again = client.post(f"/api/v1/dev/payment-links/{attempt_id}/simulate-paid", headers=headers)
    assert again.status_code == 200 and again.json()["recovered_amount"] == 140_000

    events = [e.event_type for e in db_session.query(RecoveryAuditEvent)
              .filter_by(recovery_attempt_id=attempt.id).all()]
    assert "WEBHOOK_VERIFIED" in events and "RECOVERY_RECORDED" in events


def test_dev_simulator_refuses_real_razorpay_links_and_other_merchants(db_session, merchant):
    real = _attempt_with_payment(
        db_session, merchant.id, status="executed",
        link_id="plink_real", short_url="https://rzp.io/i/abc123",
    )
    client = TestClient(app)
    own = client.post(
        f"/api/v1/dev/payment-links/{real.recovery_id}/simulate-paid",
        headers={"X-Merchant-ID": str(merchant.id)},
    )
    assert own.status_code == 409

    other = client.post(
        f"/api/v1/dev/payment-links/{real.recovery_id}/simulate-paid",
        headers={"X-Merchant-ID": str(uuid.uuid4())},
    )
    assert other.status_code == 404


def test_attempt_detail_exposes_server_side_amounts_in_paise(db_session, merchant):
    attempt = _attempt_with_payment(db_session, merchant.id, status="pending", incentive=20_000)
    client = TestClient(app)
    resp = client.get(
        f"/api/v1/merchant/recoveries/{attempt.recovery_id}",
        headers={"X-Merchant-ID": str(merchant.id)},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["payment_amount"] == 150_000
    assert data["charge_amount"] == 130_000
