from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.db.session import SessionLocal
from app.main import app
from app.domains.recovery.audit import (
    record_audit_event,
    get_recovery_audit_trail,
    RecoveryAuditEventType,
)
from app.domains.recovery.service import (
    create_recovery_campaign,
    execute_recovery_attempt,
    process_recovery_webhook,
)
from app.domains.recovery.strategist import AIRecommendation
from app.integrations.llm.provider import LLMProvider
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


client = TestClient(app)


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
    def _create(name: str = "Audit Test Merchant"):
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
    def _create(merchant_id, revenue_at_risk=30000):
        now = datetime.now(timezone.utc)
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
            window_start=now - timedelta(minutes=30),
            window_end=now,
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
            supporting_evidence={"bank": "HDFC"},
            confidence=0.88,
        )
        db_session.add(diag)
        db_session.commit()
        db_session.refresh(diag)

        # Seed matching real failed payment for the incident window
        import uuid
        payment = Payment(
            merchant_id=merchant_id,
            razorpay_payment_id=f"pay_aud_{uuid.uuid4().hex[:10]}",
            amount=revenue_at_risk,
            currency="INR",
            status="failed",
            method=incident.method,
            bank=incident.bank,
            error_code=incident.error_code,
            error_step=incident.error_step,
            created_at=now - timedelta(minutes=15),
        )
        db_session.add(payment)
        db_session.commit()

        return incident, diag
    return _create


class MockAuditLLM(LLMProvider):
    def __init__(self, recommended_action: str = "grace_period"):
        self.recommended_action = recommended_action

    def generate_structured_output(self, prompt, response_model, system_instruction=None, timeout=10.0):
        return AIRecommendation(
            recommended_action=self.recommended_action,
            concise_reason=f"AI picked {self.recommended_action}",
            evidence_level="exact_cohort",
            sample_size=20,
            observed_recovery_rate=0.85,
            expected_net_recovery_value=15000,
            confidence=0.94,
        )


# ============================================================================
# 1. Immutability Tests
# ============================================================================

def test_audit_event_immutability_prevent_update(db_session, create_merchant):
    merchant = create_merchant()
    event = record_audit_event(
        db=db_session,
        merchant_id=merchant.id,
        event_type=RecoveryAuditEventType.INCIDENT_DETECTED,
        actor_type="system",
        explanation="Initial incident detected",
    )
    db_session.commit()

    # Attempting to mutate an existing audit event instance must raise ValueError
    event.explanation = "Mutated explanation"
    with pytest.raises(ValueError, match="append-only and cannot be modified"):
        db_session.commit()
    db_session.rollback()


def test_audit_event_immutability_prevent_delete(db_session, create_merchant):
    merchant = create_merchant()
    event = record_audit_event(
        db=db_session,
        merchant_id=merchant.id,
        event_type=RecoveryAuditEventType.INCIDENT_DETECTED,
        actor_type="system",
        explanation="Initial incident detected",
    )
    db_session.commit()

    # Attempting to delete an individual audit event instance must raise ValueError
    db_session.delete(event)
    with pytest.raises(ValueError, match="append-only and cannot be deleted"):
        db_session.commit()
    db_session.rollback()


# ============================================================================
# 2. Secret / PII Redaction in Evidence
# ============================================================================

def test_audit_event_redacts_sensitive_keys(db_session, create_merchant):
    merchant = create_merchant()
    event = record_audit_event(
        db=db_session,
        merchant_id=merchant.id,
        event_type=RecoveryAuditEventType.PAYMENT_LINK_CREATED,
        actor_type="system",
        evidence={
            "api_key": "secret_key_12345",
            "key_secret": "topsecret_password",
            "safe_param": "safe_value",
            "nested": {
                "token": "bearer_token_xyz",
                "normal": 42,
            },
        },
    )
    db_session.commit()

    assert event.evidence["api_key"] == "[REDACTED]"
    assert event.evidence["key_secret"] == "[REDACTED]"
    assert event.evidence["safe_param"] == "safe_value"
    assert event.evidence["nested"]["token"] == "[REDACTED]"
    assert event.evidence["nested"]["normal"] == 42


# ============================================================================
# 3. Full Lifecycle Event Flow
# ============================================================================

def test_full_lifecycle_emits_ordered_events(db_session, create_merchant, create_policy, create_incident_with_diag):
    merchant = create_merchant()
    incident, diag = create_incident_with_diag(merchant.id)
    policy = create_policy(merchant.id, allowed_actions=["retry", "grace_period"])

    p = Payment(
        merchant_id=merchant.id,
        razorpay_payment_id="pay_lifecycle_test",
        amount=10000,
        currency="INR",
        status="failed",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(p)
    db_session.commit()

    # 1. Campaign creation with AI recommendation
    mock_llm = MockAuditLLM(recommended_action="grace_period")
    campaign, attempts = create_recovery_campaign(
        db=db_session,
        incident=incident,
        diagnosis=diag,
        policy=policy,
        llm_provider=mock_llm,
        target_payments=[p],
    )
    attempt = attempts[0]

    # Verify campaign events
    campaign_events = get_recovery_audit_trail(db_session, merchant.id, campaign_id=campaign.id)
    event_types = [e.event_type for e in campaign_events]

    assert RecoveryAuditEventType.CAMPAIGN_CREATED.value in event_types
    assert RecoveryAuditEventType.CAMPAIGN_APPROVED.value in event_types

    # 2. Execution of recovery attempt
    mock_client = MagicMock(spec=RazorpayClient)
    mock_client.create_payment_link.return_value = {
        "id": "plink_lifecycle_123",
        "short_url": "https://rzp.io/i/testlink",
        "status": "created",
    }

    execute_recovery_attempt(db_session, attempt, client=mock_client)

    # 3. Webhook arrival and verification
    webhook_payload = {
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": "plink_lifecycle_123",
                    "reference_id": attempt.recovery_id,
                    "status": "paid",
                    "amount_paid": 10000,
                }
            },
            "payment": {
                "entity": {
                    "id": "pay_recovered_final",
                    "status": "captured",
                    "amount": 10000,
                }
            },
        },
    }

    process_recovery_webhook(db_session, webhook_payload)
    db_session.commit()

    # Query full timeline for this attempt
    attempt_events = get_recovery_audit_trail(db_session, merchant.id, recovery_attempt_id=attempt.id)
    attempt_event_types = [e.event_type for e in attempt_events]

    expected_sequence = [
        RecoveryAuditEventType.RECOVERY_EXECUTION_STARTED.value,
        RecoveryAuditEventType.PAYMENT_LINK_CREATED.value,
        RecoveryAuditEventType.WEBHOOK_RECEIVED.value,
        RecoveryAuditEventType.WEBHOOK_VERIFIED.value,
        RecoveryAuditEventType.RECOVERY_RECORDED.value,
    ]

    for expected in expected_sequence:
        assert expected in attempt_event_types, f"Missing {expected} in {attempt_event_types}"

    # Verify campaign reached CAMPAIGN_COMPLETED
    all_campaign_events = get_recovery_audit_trail(db_session, merchant.id, campaign_id=campaign.id)
    all_types = [e.event_type for e in all_campaign_events]
    assert RecoveryAuditEventType.CAMPAIGN_COMPLETED.value in all_types


# ============================================================================
# 4. AI Recommendation & Fallback Recording
# ============================================================================

def test_audit_records_ai_recommendation_and_veto_fallback(
    db_session, create_merchant, create_policy, create_incident_with_diag
):
    merchant = create_merchant()
    incident, diag = create_incident_with_diag(merchant.id)
    # Allow incentive, but set max_exposure = 1000
    policy = create_policy(
        merchant.id,
        allowed_actions=["retry", "grace_period", "incentive"],
        max_incentive=500,
        max_exposure=1000,
    )

    # 4 payments * 500 = 2000 > max_exposure of 1000!
    payments = [
        Payment(
            merchant_id=merchant.id,
            razorpay_payment_id=f"pay_ai_audit_{i}",
            amount=10000,
            currency="INR",
            status="failed",
            created_at=datetime.now(timezone.utc),
        )
        for i in range(4)
    ]
    db_session.add_all(payments)
    db_session.commit()

    # AI recommends action 'incentive'
    mock_llm = MockAuditLLM(recommended_action="incentive")
    campaign, attempts = create_recovery_campaign(
        db=db_session,
        incident=incident,
        diagnosis=diag,
        policy=policy,
        llm_provider=mock_llm,
        target_payments=payments,
    )

    incident_events = get_recovery_audit_trail(db_session, merchant.id, incident_id=incident.id)
    event_types = [e.event_type for e in incident_events]

    assert RecoveryAuditEventType.STRATEGY_EVALUATED.value in event_types
    assert RecoveryAuditEventType.AI_RECOMMENDATION.value in event_types
    assert RecoveryAuditEventType.AI_FALLBACK.value in event_types

    fallback_event = next(e for e in incident_events if e.event_type == RecoveryAuditEventType.AI_FALLBACK.value)
    assert fallback_event.reason_code == "CAMPAIGN_EXPOSURE_EXCEEDED"


# ============================================================================
# 5. Execution Gateway Failure Recording
# ============================================================================

def test_audit_records_gateway_failure(db_session, create_merchant, create_policy, create_incident_with_diag):
    merchant = create_merchant()
    incident, diag = create_incident_with_diag(merchant.id)
    policy = create_policy(merchant.id)

    p = Payment(
        merchant_id=merchant.id,
        razorpay_payment_id="pay_fail_audit_1",
        amount=10000,
        currency="INR",
        status="failed",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(p)
    db_session.commit()

    campaign, attempts = create_recovery_campaign(
        db=db_session,
        incident=incident,
        diagnosis=diag,
        policy=policy,
        target_payments=[p],
    )
    attempt = attempts[0]

    mock_client = MagicMock(spec=RazorpayClient)
    mock_client.create_payment_link.side_effect = RuntimeError("Razorpay upstream gateway timeout")

    with pytest.raises(RuntimeError, match="upstream gateway timeout"):
        execute_recovery_attempt(db_session, attempt, client=mock_client)

    events = get_recovery_audit_trail(db_session, merchant.id, recovery_attempt_id=attempt.id)
    event_types = [e.event_type for e in events]

    assert RecoveryAuditEventType.RECOVERY_EXECUTION_STARTED.value in event_types
    assert RecoveryAuditEventType.PAYMENT_LINK_EXECUTION_FAILED.value in event_types

    failed_event = next(e for e in events if e.event_type == RecoveryAuditEventType.PAYMENT_LINK_EXECUTION_FAILED.value)
    assert "upstream gateway timeout" in failed_event.explanation


# ============================================================================
# 6. Merchant Isolation in Query Service & API
# ============================================================================

def test_audit_merchant_isolation_service(db_session, create_merchant):
    m1 = create_merchant("Merchant One")
    m2 = create_merchant("Merchant Two")

    e1 = record_audit_event(
        db=db_session,
        merchant_id=m1.id,
        event_type=RecoveryAuditEventType.INCIDENT_DETECTED,
        actor_type="system",
        explanation="Event for Merchant 1",
    )
    e2 = record_audit_event(
        db=db_session,
        merchant_id=m2.id,
        event_type=RecoveryAuditEventType.INCIDENT_DETECTED,
        actor_type="system",
        explanation="Event for Merchant 2",
    )
    db_session.commit()

    trail_m1 = get_recovery_audit_trail(db_session, m1.id)
    trail_m2 = get_recovery_audit_trail(db_session, m2.id)

    assert len(trail_m1) == 1
    assert trail_m1[0].id == e1.id
    assert trail_m1[0].explanation == "Event for Merchant 1"

    assert len(trail_m2) == 1
    assert trail_m2[0].id == e2.id
    assert trail_m2[0].explanation == "Event for Merchant 2"


def test_audit_api_endpoint_and_isolation(db_session, create_merchant, create_policy, create_incident_with_diag):
    m1 = create_merchant("API Merchant 1")
    m2 = create_merchant("API Merchant 2")

    inc1, diag1 = create_incident_with_diag(m1.id)
    policy1 = create_policy(m1.id)

    p = Payment(
        merchant_id=m1.id,
        razorpay_payment_id="pay_api_audit_test",
        amount=10000,
        currency="INR",
        status="failed",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(p)
    db_session.commit()

    campaign, attempts = create_recovery_campaign(
        db=db_session,
        incident=inc1,
        diagnosis=diag1,
        policy=policy1,
        target_payments=[p],
    )
    attempt = attempts[0]

    # 1. Merchant 1 queries their own recovery audit
    resp = client.get(
        f"/api/v1/merchant/recoveries/{attempt.recovery_id}/audit",
        headers={"x-merchant-id": str(m1.id)},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)

    # 2. Merchant 2 attempts to query Merchant 1's recovery audit -> 404 Not Found
    cross_resp = client.get(
        f"/api/v1/merchant/recoveries/{attempt.recovery_id}/audit",
        headers={"x-merchant-id": str(m2.id)},
    )
    assert cross_resp.status_code == 404
    assert cross_resp.json()["detail"] == "Recovery attempt not found"

    # 3. Merchant 1 queries campaign audit
    camp_resp = client.get(
        f"/api/v1/merchant/campaigns/{campaign.campaign_id}/audit",
        headers={"x-merchant-id": str(m1.id)},
    )
    assert camp_resp.status_code == 200
    camp_data = camp_resp.json()
    assert len(camp_data) >= 1
    assert camp_data[0]["event_type"] in [
        RecoveryAuditEventType.CAMPAIGN_CREATED.value,
        RecoveryAuditEventType.CAMPAIGN_APPROVED.value,
    ]

    # 4. Merchant 2 attempts to query Merchant 1's campaign audit -> 404
    cross_camp = client.get(
        f"/api/v1/merchant/campaigns/{campaign.campaign_id}/audit",
        headers={"x-merchant-id": str(m2.id)},
    )
    assert cross_camp.status_code == 404


# ============================================================================
# 7. Operator Manual Approval Audit Recording
# ============================================================================

def test_audit_operator_approval_emits_events(db_session, create_merchant, create_policy, create_incident_with_diag):
    merchant = create_merchant()
    incident, diag = create_incident_with_diag(merchant.id)
    # Policy allows only ops_review, forcing 'pending' state
    policy = create_policy(merchant.id, allowed_actions=["ops_review"])

    p = Payment(
        merchant_id=merchant.id,
        razorpay_payment_id="pay_ops_approve_test",
        amount=10000,
        currency="INR",
        status="failed",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(p)
    db_session.commit()

    campaign, attempts = create_recovery_campaign(
        db=db_session,
        incident=incident,
        diagnosis=diag,
        policy=policy,
        target_payments=[p],
    )
    attempt = attempts[0]
    assert attempt.status == "pending"

    # Operator approves via API
    resp = client.post(
        f"/api/v1/merchant/recoveries/{attempt.recovery_id}/approve",
        headers={"x-merchant-id": str(merchant.id)},
    )
    assert resp.status_code == 200

    events = get_recovery_audit_trail(db_session, merchant.id, recovery_attempt_id=attempt.id)
    event_types = [e.event_type for e in events]

    assert RecoveryAuditEventType.RECOVERY_APPROVED.value in event_types

    camp_events = get_recovery_audit_trail(db_session, merchant.id, campaign_id=campaign.id)
    camp_types = [e.event_type for e in camp_events]
    assert RecoveryAuditEventType.CAMPAIGN_APPROVED.value in camp_types


# ============================================================================
# 8. Webhook Rejection Does Not Falsely Record Recovery
# ============================================================================

def test_audit_invalid_webhook_does_not_record_recovery(db_session, create_merchant, create_policy, create_incident_with_diag):
    merchant = create_merchant()
    incident, diag = create_incident_with_diag(merchant.id)
    policy = create_policy(merchant.id)

    p = Payment(
        merchant_id=merchant.id,
        razorpay_payment_id="pay_invalid_hook_test",
        amount=10000,
        currency="INR",
        status="failed",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(p)
    db_session.commit()

    campaign, attempts = create_recovery_campaign(
        db=db_session,
        incident=incident,
        diagnosis=diag,
        policy=policy,
        target_payments=[p],
    )
    attempt = attempts[0]
    attempt.status = "executed"
    attempt.payment_link_id = "plink_expected_val"
    db_session.add(attempt)
    db_session.commit()

    # Webhook with mismatching payment link ID
    bad_payload = {
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": "plink_wrong_id",
                    "reference_id": attempt.recovery_id,
                    "status": "paid",
                }
            },
            "payment": {"entity": {"id": "pay_xyz", "status": "captured", "amount": 10000}},
        },
    }

    with pytest.raises(ValueError, match="does not match expected attempt link ID"):
        process_recovery_webhook(db_session, bad_payload)

    events = get_recovery_audit_trail(db_session, merchant.id, recovery_attempt_id=attempt.id)
    event_types = [e.event_type for e in events]

    assert RecoveryAuditEventType.WEBHOOK_RECEIVED.value in event_types
    # Must NOT record recovery or verification
    assert RecoveryAuditEventType.WEBHOOK_VERIFIED.value not in event_types
    assert RecoveryAuditEventType.RECOVERY_RECORDED.value not in event_types


# ============================================================================
# 9. Correlation Filtering
# ============================================================================

def test_audit_correlation_filtering(db_session, create_merchant, create_policy, create_incident_with_diag):
    merchant = create_merchant()
    incident, diag = create_incident_with_diag(merchant.id)
    policy = create_policy(merchant.id)

    p1 = Payment(
        merchant_id=merchant.id,
        razorpay_payment_id="pay_corr_1",
        amount=10000,
        currency="INR",
        status="failed",
        created_at=datetime.now(timezone.utc),
    )
    p2 = Payment(
        merchant_id=merchant.id,
        razorpay_payment_id="pay_corr_2",
        amount=20000,
        currency="INR",
        status="failed",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add_all([p1, p2])
    db_session.commit()

    campaign, attempts = create_recovery_campaign(
        db=db_session,
        incident=incident,
        diagnosis=diag,
        policy=policy,
        target_payments=[p1, p2],
    )
    att1, att2 = attempts[0], attempts[1]

    # Filter by attempt 1
    events_att1 = get_recovery_audit_trail(db_session, merchant.id, recovery_attempt_id=att1.id)
    for e in events_att1:
        assert e.recovery_attempt_id == att1.id

    # Filter by campaign
    events_camp = get_recovery_audit_trail(db_session, merchant.id, campaign_id=campaign.id)
    assert len(events_camp) >= 2
    for e in events_camp:
        assert e.campaign_id == campaign.id

