from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
import pytest
from sqlalchemy import select, delete

from app.db.session import SessionLocal
from app.domains.recovery.service import (
    create_recovery_campaign,
    execute_recovery_attempt,
    process_recovery_webhook,
)
from app.domains.recovery.batch_measurement import calculate_batch_measurement
from app.integrations.razorpay import RazorpayClient
from app.models.merchant import Merchant
from app.models.payment import Payment
from app.models.payment_event import PaymentEvent
from app.models.webhook_event import WebhookEvent
from app.models.incident import Incident
from app.models.diagnosis import Diagnosis
from app.models.recovery_policy import RecoveryPolicy
from app.models.recovery_attempt import RecoveryAttempt
from app.models.recovery_campaign import RecoveryCampaign
from app.models.recovery_audit_event import RecoveryAuditEvent


@pytest.fixture
def db_session():
    session = SessionLocal()
    try:
        session.execute(delete(RecoveryAuditEvent))
        session.execute(delete(RecoveryAttempt))
        session.execute(delete(RecoveryCampaign))
        session.execute(delete(RecoveryPolicy))
        session.execute(delete(Diagnosis))
        session.execute(delete(Incident))
        session.execute(delete(PaymentEvent))
        session.execute(delete(Payment))
        session.execute(delete(WebhookEvent))
        session.execute(delete(Merchant))
        session.commit()
        yield session
        session.execute(delete(RecoveryAuditEvent))
        session.execute(delete(RecoveryAttempt))
        session.execute(delete(RecoveryCampaign))
        session.execute(delete(RecoveryPolicy))
        session.execute(delete(Diagnosis))
        session.execute(delete(Incident))
        session.execute(delete(PaymentEvent))
        session.execute(delete(Payment))
        session.execute(delete(WebhookEvent))
        session.execute(delete(Merchant))
        session.commit()
    finally:
        session.close()


@pytest.fixture
def create_merchant(db_session):
    def _create(name: str = "Campaign Test Merchant"):
        merchant = Merchant(name=name)
        db_session.add(merchant)
        db_session.commit()
        db_session.refresh(merchant)
        return merchant
    return _create


@pytest.fixture
def create_policy(db_session):
    def _create(
        merchant_id,
        allowed_actions=None,
        max_incentive=1000,
        max_exposure=50000,
        approval_threshold=5000.0,
    ):
        if allowed_actions is None:
            allowed_actions = ["retry", "grace_period", "incentive", "ops_review"]
        policy = RecoveryPolicy(
            merchant_id=merchant_id,
            allowed_actions=allowed_actions,
            max_incentive=max_incentive,
            max_exposure=max_exposure,
            approval_threshold=approval_threshold,
        )
        db_session.add(policy)
        db_session.commit()
        return policy
    return _create


@pytest.fixture
def create_incident_with_diag(db_session):
    def _create(merchant_id, window_start=None, window_end=None, revenue_at_risk=30000):
        now = datetime.now(timezone.utc)
        w_start = window_start or (now - timedelta(minutes=30))
        w_end = window_end or now

        incident = Incident(
            merchant_id=merchant_id,
            method="upi",
            bank="HDFC",
            error_code="GATEWAY_ERROR",
            error_step="payment_authorization",
            current_total_count=30,
            current_failed_count=3,
            current_failure_rate=0.1,
            current_total_amount=100000,
            current_failed_amount=revenue_at_risk,
            baseline_total_count=30,
            baseline_failed_count=1,
            baseline_failure_rate=0.033,
            baseline_total_amount=100000,
            baseline_failed_amount=1000,
            absolute_rate_increase=0.067,
            relative_degradation=3.0,
            revenue_at_risk=revenue_at_risk,
            window_start=w_start,
            window_end=w_end,
            baseline_start=now - timedelta(hours=2),
            baseline_end=now - timedelta(hours=1),
            status="detected",
        )
        db_session.add(incident)
        db_session.commit()
        db_session.refresh(incident)

        diag = Diagnosis(
            incident_id=incident.id,
            diagnosis_type="bank-specific degradation",
            explanation="Localized gateway latency spike at HDFC",
            supporting_evidence={},
            confidence=0.88,
        )
        db_session.add(diag)
        db_session.commit()
        db_session.refresh(diag)
        return incident, diag
    return _create


def test_multiple_failed_payments_produce_independent_recovery_attempts(
    db_session, create_merchant, create_policy, create_incident_with_diag
):
    merchant = create_merchant()
    now = datetime.now(timezone.utc)
    w_start = now - timedelta(minutes=30)
    w_end = now
    incident, diag = create_incident_with_diag(merchant.id, window_start=w_start, window_end=w_end)
    policy = create_policy(merchant.id, allowed_actions=["grace_period"])

    # Seed 3 distinct failed payments for this incident with different amounts
    amounts = [7500, 12500, 20000]
    payments = []
    for idx, amt in enumerate(amounts):
        p = Payment(
            merchant_id=merchant.id,
            razorpay_payment_id=f"pay_camp_{idx}_{merchant.id.hex[:4]}",
            amount=amt,
            currency="INR",
            status="failed",
            method="upi",
            bank="HDFC",
            error_code="GATEWAY_ERROR",
            error_step="payment_authorization",
            created_at=w_start + timedelta(minutes=5 * (idx + 1)),
        )
        db_session.add(p)
        payments.append(p)
    db_session.commit()

    # Create campaign
    campaign, attempts = create_recovery_campaign(
        db=db_session,
        incident=incident,
        diagnosis=diag,
        policy=policy,
    )

    assert campaign is not None
    assert campaign.target_payment_count == 3
    assert campaign.total_revenue_at_risk == sum(amounts)
    assert campaign.selected_action == "grace_period"
    assert campaign.status == "approved"
    assert len(attempts) == 3

    # Verify each attempt is linked to the campaign and to a distinct payment
    linked_payment_ids = {a.payment_id for a in attempts}
    assert linked_payment_ids == {p.id for p in payments}

    for attempt in attempts:
        assert attempt.campaign_id == campaign.id
        assert attempt.incident_id == incident.id
        assert attempt.merchant_id == merchant.id
        assert attempt.selected_action == "grace_period"
        assert attempt.payment is not None
        assert attempt.payment.amount in amounts


def test_payment_amount_derived_during_execution(
    db_session, create_merchant, create_policy, create_incident_with_diag
):
    merchant = create_merchant()
    now = datetime.now(timezone.utc)
    w_start = now - timedelta(minutes=30)
    w_end = now
    incident, diag = create_incident_with_diag(merchant.id, window_start=w_start, window_end=w_end)
    policy = create_policy(merchant.id, allowed_actions=["incentive"], approval_threshold=10000.0)

    # Failed payment of 15000 paise (Rs 150)
    p = Payment(
        merchant_id=merchant.id,
        razorpay_payment_id="pay_exec_test_15000",
        amount=15000,
        currency="INR",
        status="failed",
        method="upi",
        bank="HDFC",
        error_code="GATEWAY_ERROR",
        error_step="payment_authorization",
        created_at=w_start + timedelta(minutes=10),
    )
    db_session.add(p)
    db_session.commit()

    campaign, attempts = create_recovery_campaign(
        db=db_session,
        incident=incident,
        diagnosis=diag,
        policy=policy,
    )
    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt.incentive_amount == 500  # Rs 5 incentive

    mock_client = MagicMock(spec=RazorpayClient)
    mock_client.create_payment_link.return_value = {
        "id": "plink_exec_14500",
        "short_url": "https://rzp.io/i/exec14500",
        "status": "created",
    }

    executed_attempt = execute_recovery_attempt(db_session, attempt, client=mock_client)

    # Verification: charge_amount must be payment.amount (15000) - incentive (500) = 14500 paise!
    mock_client.create_payment_link.assert_called_once_with(
        amount=14500,
        reference_id=attempt.recovery_id,
        description=f"Payment recovery link for {attempt.recovery_id}",
        expire_by=int(attempt.expires_at.timestamp()),
    )
    assert executed_attempt.status == "executed"
    assert executed_attempt.payment_link_id == "plink_exec_14500"
    assert campaign.status == "executing"


def test_missing_payment_fails_execution_safely(
    db_session, create_merchant, create_incident_with_diag
):
    merchant = create_merchant()
    incident, diag = create_incident_with_diag(merchant.id)

    # Attempt with NO payment_id
    orphan_attempt = RecoveryAttempt(
        recovery_id="rec_orphan_attempt",
        merchant_id=merchant.id,
        incident_id=incident.id,
        payment_id=None,
        selected_action="retry",
        status="approved",
    )
    db_session.add(orphan_attempt)
    db_session.commit()

    # Executing an orphan attempt MUST raise ValueError rather than defaulting to 1000
    with pytest.raises(ValueError, match="cannot be executed without an associated Payment"):
        execute_recovery_attempt(db_session, orphan_attempt)


def test_campaign_approval_threshold_gate(
    db_session, create_merchant, create_policy, create_incident_with_diag
):
    merchant = create_merchant()
    now = datetime.now(timezone.utc)
    w_start = now - timedelta(minutes=30)
    w_end = now
    incident, diag = create_incident_with_diag(merchant.id, window_start=w_start, window_end=w_end)

    # Seed 5 failed payments
    for i in range(5):
        p = Payment(
            merchant_id=merchant.id,
            razorpay_payment_id=f"pay_thresh_{i}",
            amount=10000,
            currency="INR",
            status="failed",
            method="upi",
            bank="HDFC",
            error_code="GATEWAY_ERROR",
            error_step="payment_authorization",
            created_at=w_start + timedelta(minutes=i + 1),
        )
        db_session.add(p)
    db_session.commit()

    # Total incentive cost for 5 payments @ 500 = 2500 paise.
    # Set approval_threshold = 2000.0 (< 2500), so approval MUST be required (status: pending)
    policy = create_policy(
        merchant.id,
        allowed_actions=["incentive"],
        approval_threshold=2000.0,
    )

    campaign, attempts = create_recovery_campaign(
        db=db_session,
        incident=incident,
        diagnosis=diag,
        policy=policy,
    )

    assert campaign.status == "pending"
    assert campaign.total_incentive_cost == 2500
    for a in attempts:
        assert a.status == "pending"


def test_webhook_recovery_mapped_to_specific_payment_and_campaign_completion(
    db_session, create_merchant, create_policy, create_incident_with_diag
):
    merchant = create_merchant()
    now = datetime.now(timezone.utc)
    w_start = now - timedelta(minutes=30)
    w_end = now
    incident, diag = create_incident_with_diag(merchant.id, window_start=w_start, window_end=w_end)
    policy = create_policy(merchant.id, allowed_actions=["grace_period"])

    p1 = Payment(
        merchant_id=merchant.id,
        razorpay_payment_id="pay_hook_p1",
        amount=10000,
        currency="INR",
        status="failed",
        method="upi",
        bank="HDFC",
        error_code="GATEWAY_ERROR",
        error_step="payment_authorization",
        created_at=w_start + timedelta(minutes=5),
    )
    p2 = Payment(
        merchant_id=merchant.id,
        razorpay_payment_id="pay_hook_p2",
        amount=20000,
        currency="INR",
        status="failed",
        method="upi",
        bank="HDFC",
        error_code="GATEWAY_ERROR",
        error_step="payment_authorization",
        created_at=w_start + timedelta(minutes=10),
    )
    db_session.add_all([p1, p2])
    db_session.commit()

    campaign, attempts = create_recovery_campaign(
        db=db_session,
        incident=incident,
        diagnosis=diag,
        policy=policy,
    )

    # Mock execute both attempts
    mock_client = MagicMock(spec=RazorpayClient)
    mock_client.create_payment_link.side_effect = [
        {"id": "plink_p1_100", "short_url": "https://rzp.io/p1", "status": "created"},
        {"id": "plink_p2_200", "short_url": "https://rzp.io/p2", "status": "created"},
    ]
    a1 = execute_recovery_attempt(db_session, attempts[0], client=mock_client)
    a2 = execute_recovery_attempt(db_session, attempts[1], client=mock_client)

    assert campaign.status == "executing"
    assert a1.status == "executed"
    assert a2.status == "executed"

    # Simulate webhook payment for a1 ONLY
    webhook_payload_a1 = {
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": a1.payment_link_id,
                    "reference_id": a1.recovery_id,
                    "status": "paid",
                    "amount_paid": 10000,
                }
            },
            "payment": {
                "entity": {
                    "id": "pay_rzp_hook_success_1",
                    "status": "captured",
                    "amount": 10000,
                }
            },
        },
    }

    process_recovery_webhook(db_session, webhook_payload_a1)
    db_session.commit()
    db_session.refresh(a1)
    db_session.refresh(a2)
    db_session.refresh(campaign)

    # a1 is recovered, but a2 is still executing, so campaign is still executing
    assert a1.status == "recovered"
    assert a1.recovered_amount == 10000
    assert a1.resulting_payment_id == "pay_rzp_hook_success_1"
    assert a2.status == "executed"
    assert campaign.status == "executing"

    # Simulate webhook payment for a2
    webhook_payload_a2 = {
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": a2.payment_link_id,
                    "reference_id": a2.recovery_id,
                    "status": "paid",
                    "amount_paid": 20000,
                }
            },
            "payment": {
                "entity": {
                    "id": "pay_rzp_hook_success_2",
                    "status": "captured",
                    "amount": 20000,
                }
            },
        },
    }

    process_recovery_webhook(db_session, webhook_payload_a2)
    db_session.commit()
    db_session.refresh(a2)
    db_session.refresh(campaign)

    # Both settled -> campaign transitions to completed!
    assert a2.status == "recovered"
    assert a2.recovered_amount == 20000
    assert campaign.status == "completed"


def test_merchant_isolation_in_campaigns(
    db_session, create_merchant, create_policy, create_incident_with_diag
):
    m1 = create_merchant("Merchant 1")
    m2 = create_merchant("Merchant 2")

    now = datetime.now(timezone.utc)
    w_start = now - timedelta(minutes=30)
    w_end = now

    inc1, diag1 = create_incident_with_diag(m1.id, window_start=w_start, window_end=w_end)
    inc2, diag2 = create_incident_with_diag(m2.id, window_start=w_start, window_end=w_end)

    p1 = create_policy(m1.id)
    p2 = create_policy(m2.id)

    # M1 failed payment
    pay1 = Payment(
        merchant_id=m1.id,
        razorpay_payment_id="pay_m1_isolated",
        amount=5000,
        currency="INR",
        status="failed",
        method="upi",
        bank="HDFC",
        error_code="GATEWAY_ERROR",
        error_step="payment_authorization",
        created_at=w_start + timedelta(minutes=5),
    )
    # M2 failed payment
    pay2 = Payment(
        merchant_id=m2.id,
        razorpay_payment_id="pay_m2_isolated",
        amount=8000,
        currency="INR",
        status="failed",
        method="upi",
        bank="HDFC",
        error_code="GATEWAY_ERROR",
        error_step="payment_authorization",
        created_at=w_start + timedelta(minutes=5),
    )
    db_session.add_all([pay1, pay2])
    db_session.commit()

    camp1, atts1 = create_recovery_campaign(db_session, inc1, diag1, p1)
    camp2, atts2 = create_recovery_campaign(db_session, inc2, diag2, p2)

    # Verify complete isolation
    assert camp1.merchant_id == m1.id
    assert len(atts1) == 1
    assert atts1[0].payment_id == pay1.id
    assert atts1[0].payment.amount == 5000

    assert camp2.merchant_id == m2.id
    assert len(atts2) == 1
    assert atts2[0].payment_id == pay2.id
    assert atts2[0].payment.amount == 8000


def test_batch_measurement_payment_level_attribution(
    db_session, create_merchant, create_policy, create_incident_with_diag
):
    merchant = create_merchant()
    now = datetime.now(timezone.utc)
    w_start = now - timedelta(minutes=30)
    w_end = now
    incident, diag = create_incident_with_diag(merchant.id, window_start=w_start, window_end=w_end)
    policy = create_policy(merchant.id, allowed_actions=["incentive"])

    # 2 failed payments
    p1 = Payment(
        merchant_id=merchant.id,
        razorpay_payment_id="pay_batch_1",
        amount=10000,
        currency="INR",
        status="failed",
        method="upi",
        bank="HDFC",
        error_code="GATEWAY_ERROR",
        error_step="payment_authorization",
        created_at=w_start + timedelta(minutes=5),
    )
    p2 = Payment(
        merchant_id=merchant.id,
        razorpay_payment_id="pay_batch_2",
        amount=20000,
        currency="INR",
        status="failed",
        method="upi",
        bank="HDFC",
        error_code="GATEWAY_ERROR",
        error_step="payment_authorization",
        created_at=w_start + timedelta(minutes=10),
    )
    db_session.add_all([p1, p2])
    db_session.commit()

    camp, attempts = create_recovery_campaign(db_session, incident, diag, policy)
    assert len(attempts) == 2

    # Execute and simulate 1 recovered, 1 failed
    mock_client = MagicMock(spec=RazorpayClient)
    mock_client.create_payment_link.side_effect = [
        {
            "id": "plink_batch_test_1",
            "short_url": "https://rzp.io/b1",
            "status": "created",
        },
        {
            "id": "plink_batch_test_2",
            "short_url": "https://rzp.io/b2",
            "status": "created",
        },
    ]
    execute_recovery_attempt(db_session, attempts[0], client=mock_client)
    execute_recovery_attempt(db_session, attempts[1], client=mock_client)

    # Recover attempt 0 (10000 - 500 = 9500 recovered)
    attempts[0].status = "recovered"
    attempts[0].recovered_amount = 9500
    # Fail attempt 1
    attempts[1].status = "failed"
    attempts[1].recovered_amount = 0
    db_session.commit()

    # Calculate batch measurement
    measurement = calculate_batch_measurement(
        db=db_session,
        merchant_id=merchant.id,
        start_time=w_start - timedelta(hours=1),
        end_time=w_end + timedelta(hours=1),
    )

    assert measurement.eligible_recovery_attempts == 2
    assert measurement.completed_recovery_attempts == 2
    assert measurement.recovered_attempts == 1
    assert measurement.actual_recovered_amount == 9500
    assert measurement.intervention_cost == 1000  # 500 * 2
    assert measurement.net_recovered_amount == 8500  # 9500 - 1000
