from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
import pytest
from sqlalchemy import select, delete

from app.db.session import SessionLocal
from app.domains.recovery.guardrails import (
    evaluate_campaign_guardrails,
    evaluate_execution_guardrails,
    GuardrailResult,
    GuardrailViolationError,
)
from app.domains.recovery.service import (
    create_recovery_campaign,
    execute_recovery_attempt,
)
from app.integrations.razorpay import RazorpayClient
from app.integrations.llm.provider import LLMProvider
from app.domains.recovery.strategist import AIRecommendation
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
    def _create(name: str = "Guardrail Merchant"):
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
            supporting_evidence={},
            confidence=0.88,
        )
        db_session.add(diag)
        db_session.commit()
        db_session.refresh(diag)
        return incident, diag
    return _create


class MockGuardrailLLM(LLMProvider):
    def __init__(self, recommended_action: str):
        self.recommended_action = recommended_action

    def generate_structured_output(self, prompt, response_model, system_instruction=None, timeout=10.0):
        return AIRecommendation(
            recommended_action=self.recommended_action,
            concise_reason=f"AI picked {self.recommended_action}",
            evidence_level="exact_cohort",
            sample_size=15,
            observed_recovery_rate=0.8,
            expected_net_recovery_value=12000,
            confidence=0.92,
        )


# ============================================================================
# 1. Action Safety Checks
# ============================================================================

def test_guardrail_allowed_action_passes(db_session, create_merchant, create_policy, create_incident_with_diag):
    merchant = create_merchant()
    incident, diag = create_incident_with_diag(merchant.id)
    policy = create_policy(merchant.id, allowed_actions=["retry", "grace_period"])

    p = Payment(
        merchant_id=merchant.id,
        razorpay_payment_id="pay_guard_action_1",
        amount=10000,
        currency="INR",
        status="failed",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(p)
    db_session.commit()

    result = evaluate_campaign_guardrails(
        db=db_session,
        incident=incident,
        policy=policy,
        proposed_action="retry",
        per_attempt_incentive=0,
        target_payments=[p],
    )
    assert result.decision == "allowed"
    assert result.reason_code == "WITHIN_POLICY"
    assert result.audit_event_type == "GUARDRAIL_APPROVED"


def test_guardrail_disallowed_action_blocked(db_session, create_merchant, create_policy, create_incident_with_diag):
    merchant = create_merchant()
    incident, diag = create_incident_with_diag(merchant.id)
    policy = create_policy(merchant.id, allowed_actions=["retry"])  # only retry allowed

    p = Payment(
        merchant_id=merchant.id,
        razorpay_payment_id="pay_guard_action_2",
        amount=10000,
        currency="INR",
        status="failed",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(p)
    db_session.commit()

    result = evaluate_campaign_guardrails(
        db=db_session,
        incident=incident,
        policy=policy,
        proposed_action="incentive",
        per_attempt_incentive=500,
        target_payments=[p],
    )
    assert result.decision == "blocked"
    assert result.reason_code == "ACTION_DISALLOWED"
    assert result.audit_event_type == "GUARDRAIL_BLOCKED"


# ============================================================================
# 2. Incentive Safety Checks
# ============================================================================

def test_guardrail_incentive_within_cap_passes(db_session, create_merchant, create_policy, create_incident_with_diag):
    merchant = create_merchant()
    incident, diag = create_incident_with_diag(merchant.id)
    policy = create_policy(merchant.id, allowed_actions=["incentive"], max_incentive=1000)

    p = Payment(
        merchant_id=merchant.id,
        razorpay_payment_id="pay_guard_inc_1",
        amount=10000,
        currency="INR",
        status="failed",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(p)
    db_session.commit()

    result = evaluate_campaign_guardrails(
        db=db_session,
        incident=incident,
        policy=policy,
        proposed_action="incentive",
        per_attempt_incentive=500,
        target_payments=[p],
    )
    assert result.decision == "allowed"
    assert result.reason_code == "WITHIN_POLICY"


def test_guardrail_incentive_exceeding_cap_blocked(db_session, create_merchant, create_policy, create_incident_with_diag):
    merchant = create_merchant()
    incident, diag = create_incident_with_diag(merchant.id)
    policy = create_policy(merchant.id, allowed_actions=["incentive"], max_incentive=300)

    p = Payment(
        merchant_id=merchant.id,
        razorpay_payment_id="pay_guard_inc_2",
        amount=10000,
        currency="INR",
        status="failed",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(p)
    db_session.commit()

    result = evaluate_campaign_guardrails(
        db=db_session,
        incident=incident,
        policy=policy,
        proposed_action="incentive",
        per_attempt_incentive=500,  # 500 > 300
        target_payments=[p],
    )
    assert result.decision == "blocked"
    assert result.reason_code == "INCENTIVE_EXCEEDS_CAP"


def test_guardrail_incentive_exceeding_payment_amount_blocked(db_session, create_merchant, create_policy, create_incident_with_diag):
    merchant = create_merchant()
    incident, diag = create_incident_with_diag(merchant.id)
    policy = create_policy(merchant.id, allowed_actions=["incentive"], max_incentive=5000)

    p = Payment(
        merchant_id=merchant.id,
        razorpay_payment_id="pay_guard_inc_3",
        amount=200,  # payment is only 200 paise
        currency="INR",
        status="failed",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(p)
    db_session.commit()

    result = evaluate_campaign_guardrails(
        db=db_session,
        incident=incident,
        policy=policy,
        proposed_action="incentive",
        per_attempt_incentive=500,  # 500 >= 200 -> invalid!
        target_payments=[p],
    )
    assert result.decision == "blocked"
    assert result.reason_code == "FINANCIAL_SANITY_VIOLATION"


# ============================================================================
# 3. Campaign-Level Aggregate Exposure
# ============================================================================

def test_guardrail_campaign_aggregate_exposure_enforced(db_session, create_merchant, create_policy, create_incident_with_diag):
    merchant = create_merchant()
    incident, diag = create_incident_with_diag(merchant.id)
    # max_exposure is 2000 paise (Rs 20)
    # per_attempt max_incentive is 500 paise
    policy = create_policy(
        merchant.id,
        allowed_actions=["incentive"],
        max_incentive=500,
        max_exposure=2000,
    )

    # 10 failed payments
    payments = []
    for i in range(10):
        p = Payment(
            merchant_id=merchant.id,
            razorpay_payment_id=f"pay_guard_exp_{i}",
            amount=5000,
            currency="INR",
            status="failed",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(p)
        payments.append(p)
    db_session.commit()

    # 10 payments * 300 incentive = 3000 total incentive cost.
    # Even though 300 <= max_incentive (500), 3000 > max_exposure (2000)!
    result = evaluate_campaign_guardrails(
        db=db_session,
        incident=incident,
        policy=policy,
        proposed_action="incentive",
        per_attempt_incentive=300,
        target_payments=payments,
    )
    assert result.decision == "blocked"
    assert result.reason_code == "CAMPAIGN_EXPOSURE_EXCEEDED"
    assert "exceeds policy max_exposure" in result.explanation


# ============================================================================
# 4. Approval Threshold
# ============================================================================

def test_guardrail_approval_threshold_requires_approval(db_session, create_merchant, create_policy, create_incident_with_diag):
    merchant = create_merchant()
    incident, diag = create_incident_with_diag(merchant.id)
    policy = create_policy(
        merchant.id,
        allowed_actions=["incentive"],
        max_incentive=1000,
        max_exposure=50000,
        approval_threshold=1500.0,
    )

    # 5 payments * 500 = 2500 paise. 2500 > approval_threshold (1500)
    payments = [
        Payment(
            merchant_id=merchant.id,
            razorpay_payment_id=f"pay_guard_app_{i}",
            amount=10000,
            currency="INR",
            status="failed",
            created_at=datetime.now(timezone.utc),
        )
        for i in range(5)
    ]
    db_session.add_all(payments)
    db_session.commit()

    result = evaluate_campaign_guardrails(
        db=db_session,
        incident=incident,
        policy=policy,
        proposed_action="incentive",
        per_attempt_incentive=500,
        target_payments=payments,
    )
    assert result.decision == "requires_approval"
    assert result.reason_code == "EXPOSURE_REQUIRES_APPROVAL"
    assert result.audit_event_type == "GUARDRAIL_APPROVAL_REQUIRED"


def test_guardrail_ops_review_always_requires_approval(db_session, create_merchant, create_policy, create_incident_with_diag):
    merchant = create_merchant()
    incident, diag = create_incident_with_diag(merchant.id)
    policy = create_policy(merchant.id, allowed_actions=["ops_review"])

    p = Payment(
        merchant_id=merchant.id,
        razorpay_payment_id="pay_guard_ops_1",
        amount=10000,
        currency="INR",
        status="failed",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(p)
    db_session.commit()

    result = evaluate_campaign_guardrails(
        db=db_session,
        incident=incident,
        policy=policy,
        proposed_action="ops_review",
        per_attempt_incentive=0,
        target_payments=[p],
    )
    assert result.decision == "requires_approval"
    assert result.reason_code == "OPS_REVIEW_REQUIRES_APPROVAL"


# ============================================================================
# 5. Payment Validity & Financial Sanity
# ============================================================================

def test_guardrail_non_failed_payment_blocked(db_session, create_merchant, create_policy, create_incident_with_diag):
    merchant = create_merchant()
    incident, diag = create_incident_with_diag(merchant.id)
    policy = create_policy(merchant.id)

    # Payment that has status 'captured' (not failed)
    p = Payment(
        merchant_id=merchant.id,
        razorpay_payment_id="pay_guard_captured",
        amount=10000,
        currency="INR",
        status="captured",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(p)
    db_session.commit()

    result = evaluate_campaign_guardrails(
        db=db_session,
        incident=incident,
        policy=policy,
        proposed_action="retry",
        per_attempt_incentive=0,
        target_payments=[p],
    )
    assert result.decision == "blocked"
    assert result.reason_code == "INVALID_PAYMENTS"


def test_guardrail_negative_incentive_blocked(db_session, create_merchant, create_policy, create_incident_with_diag):
    merchant = create_merchant()
    incident, diag = create_incident_with_diag(merchant.id)
    policy = create_policy(merchant.id)

    p = Payment(
        merchant_id=merchant.id,
        razorpay_payment_id="pay_guard_negative",
        amount=10000,
        currency="INR",
        status="failed",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(p)
    db_session.commit()

    result = evaluate_campaign_guardrails(
        db=db_session,
        incident=incident,
        policy=policy,
        proposed_action="incentive",
        per_attempt_incentive=-100,
        target_payments=[p],
    )
    assert result.decision == "blocked"
    assert result.reason_code == "FINANCIAL_SANITY_VIOLATION"


# ============================================================================
# 6. Merchant Isolation
# ============================================================================

def test_guardrail_cross_merchant_payment_rejected(db_session, create_merchant, create_policy, create_incident_with_diag):
    m1 = create_merchant("Merchant 1")
    m2 = create_merchant("Merchant 2")

    inc1, diag1 = create_incident_with_diag(m1.id)
    policy1 = create_policy(m1.id)

    # Payment belonging to Merchant 2 passed into Merchant 1 campaign
    p_m2 = Payment(
        merchant_id=m2.id,
        razorpay_payment_id="pay_m2_cross",
        amount=10000,
        currency="INR",
        status="failed",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(p_m2)
    db_session.commit()

    result = evaluate_campaign_guardrails(
        db=db_session,
        incident=inc1,
        policy=policy1,
        proposed_action="retry",
        per_attempt_incentive=0,
        target_payments=[p_m2],
    )
    assert result.decision == "blocked"
    assert result.reason_code == "MERCHANT_ISOLATION_VIOLATION"


# ============================================================================
# 7. Execution Guardrails & Lifecycle Validity
# ============================================================================

def test_execution_guardrail_unapproved_attempt_blocked(db_session, create_merchant, create_incident_with_diag):
    merchant = create_merchant()
    incident, diag = create_incident_with_diag(merchant.id)

    p = Payment(
        merchant_id=merchant.id,
        razorpay_payment_id="pay_exec_guard_1",
        amount=10000,
        currency="INR",
        status="failed",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(p)
    db_session.commit()

    # Attempt in 'pending' status
    attempt = RecoveryAttempt(
        recovery_id="rec_exec_pending",
        merchant_id=merchant.id,
        incident_id=incident.id,
        payment_id=p.id,
        selected_action="retry",
        status="pending",
    )
    db_session.add(attempt)
    db_session.commit()

    result = evaluate_execution_guardrails(db_session, attempt)
    assert result.decision == "requires_approval"
    assert result.reason_code == "REQUIRES_APPROVAL"

    # execute_recovery_attempt must raise GuardrailViolationError
    with pytest.raises(GuardrailViolationError, match="pending operator approval"):
        execute_recovery_attempt(db_session, attempt)


def test_execution_guardrail_already_recovered_blocked(db_session, create_merchant, create_incident_with_diag):
    merchant = create_merchant()
    incident, diag = create_incident_with_diag(merchant.id)

    p = Payment(
        merchant_id=merchant.id,
        razorpay_payment_id="pay_exec_guard_recovered",
        amount=10000,
        currency="INR",
        status="failed",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(p)
    db_session.commit()

    attempt = RecoveryAttempt(
        recovery_id="rec_exec_recovered",
        merchant_id=merchant.id,
        incident_id=incident.id,
        payment_id=p.id,
        selected_action="retry",
        status="recovered",
        payment_link_id="plink_old",
    )
    db_session.add(attempt)
    db_session.commit()

    result = evaluate_execution_guardrails(db_session, attempt)
    assert result.decision == "blocked"
    assert result.reason_code == "ALREADY_RECOVERED"

    with pytest.raises(GuardrailViolationError, match="already recovered"):
        execute_recovery_attempt(db_session, attempt)


def test_execution_guardrail_completed_campaign_blocked(db_session, create_merchant, create_incident_with_diag):
    merchant = create_merchant()
    incident, diag = create_incident_with_diag(merchant.id)

    campaign = RecoveryCampaign(
        campaign_id="camp_completed_guard",
        merchant_id=merchant.id,
        incident_id=incident.id,
        selected_action="retry",
        status="completed",  # campaign is completed
    )
    db_session.add(campaign)
    db_session.commit()

    p = Payment(
        merchant_id=merchant.id,
        razorpay_payment_id="pay_exec_completed_camp",
        amount=10000,
        currency="INR",
        status="failed",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(p)
    db_session.commit()

    attempt = RecoveryAttempt(
        recovery_id="rec_exec_camp_done",
        merchant_id=merchant.id,
        incident_id=incident.id,
        campaign_id=campaign.id,
        payment_id=p.id,
        selected_action="retry",
        status="approved",
    )
    db_session.add(attempt)
    db_session.commit()

    result = evaluate_execution_guardrails(db_session, attempt)
    assert result.decision == "blocked"
    assert result.reason_code == "CAMPAIGN_COMPLETED"

    with pytest.raises(GuardrailViolationError, match="Parent campaign camp_completed_guard is completed"):
        execute_recovery_attempt(db_session, attempt)


def test_execution_guardrail_missing_payment_blocked(db_session, create_merchant, create_incident_with_diag):
    merchant = create_merchant()
    incident, diag = create_incident_with_diag(merchant.id)

    attempt = RecoveryAttempt(
        recovery_id="rec_exec_orphan",
        merchant_id=merchant.id,
        incident_id=incident.id,
        payment_id=None,  # missing payment
        selected_action="retry",
        status="approved",
    )
    db_session.add(attempt)
    db_session.commit()

    result = evaluate_execution_guardrails(db_session, attempt)
    assert result.decision == "blocked"
    assert result.reason_code == "MISSING_PAYMENT"

    with pytest.raises(GuardrailViolationError, match="cannot be executed without an associated Payment"):
        execute_recovery_attempt(db_session, attempt)


# ============================================================================
# 8. AI Recommendation Veto and Guardrail Integration
# ============================================================================

def test_guardrail_accepts_valid_ai_recommendation(db_session, create_merchant, create_policy, create_incident_with_diag):
    merchant = create_merchant()
    incident, diag = create_incident_with_diag(merchant.id)
    policy = create_policy(merchant.id, allowed_actions=["retry", "grace_period"])

    p = Payment(
        merchant_id=merchant.id,
        razorpay_payment_id="pay_ai_valid_1",
        amount=10000,
        currency="INR",
        status="failed",
        method="upi",
        bank="HDFC",
        error_code="GATEWAY_ERROR",
        error_step="payment_authorization",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(p)
    db_session.commit()

    mock_llm = MockGuardrailLLM(recommended_action="grace_period")
    campaign, attempts = create_recovery_campaign(
        db=db_session,
        incident=incident,
        diagnosis=diag,
        policy=policy,
        llm_provider=mock_llm,
        target_payments=[p],
    )

    assert campaign.selected_action == "grace_period"
    assert campaign.status == "approved"
    assert campaign.decision_evidence["ai_strategist_used"] is True
    assert campaign.decision_evidence["guardrail_decision"] == "allowed"
    assert campaign.decision_evidence["guardrail_audit_event"] == "GUARDRAIL_APPROVED"


def test_guardrail_vetoes_unsafe_ai_recommendation_and_falls_back(
    db_session, create_merchant, create_policy, create_incident_with_diag
):
    merchant = create_merchant()
    incident, diag = create_incident_with_diag(merchant.id)
    # Policy has max_exposure = 1000 paise
    policy = create_policy(
        merchant.id,
        allowed_actions=["retry", "grace_period", "incentive"],
        max_incentive=500,
        max_exposure=1000,
    )

    # 4 failed payments
    payments = [
        Payment(
            merchant_id=merchant.id,
            razorpay_payment_id=f"pay_ai_unsafe_{i}",
            amount=5000,
            currency="INR",
            status="failed",
            method="upi",
            bank="HDFC",
            error_code="GATEWAY_ERROR",
            error_step="payment_authorization",
            created_at=datetime.now(timezone.utc),
        )
        for i in range(4)
    ]
    db_session.add_all(payments)
    db_session.commit()

    # AI recommends 'incentive' (4 payments * 500 = 2000 > max_exposure of 1000)
    mock_llm = MockGuardrailLLM(recommended_action="incentive")
    campaign, attempts = create_recovery_campaign(
        db=db_session,
        incident=incident,
        diagnosis=diag,
        policy=policy,
        llm_provider=mock_llm,
        target_payments=payments,
    )

    # Guardrail must VETO the unsafe AI recommendation!
    # It must safely fall back to a non-incentive candidate (retry or grace_period)
    assert campaign.selected_action != "incentive"
    assert campaign.selected_action in ["retry", "grace_period"]
    assert campaign.status == "approved"
    assert campaign.decision_evidence["ai_strategist_used"] is False
    assert campaign.decision_evidence["guardrail_decision"] == "allowed"


# ============================================================================
# 9. Execution Gate Prevents Gateway Calls
# ============================================================================

def test_execution_guardrail_prevents_gateway_call(db_session, create_merchant, create_incident_with_diag):
    merchant = create_merchant()
    incident, diag = create_incident_with_diag(merchant.id)

    # Attempt with non-positive amount
    p = Payment(
        merchant_id=merchant.id,
        razorpay_payment_id="pay_exec_zero_amount",
        amount=0,  # 0 amount!
        currency="INR",
        status="failed",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(p)
    db_session.commit()

    attempt = RecoveryAttempt(
        recovery_id="rec_exec_zero",
        merchant_id=merchant.id,
        incident_id=incident.id,
        payment_id=p.id,
        selected_action="retry",
        status="approved",
    )
    db_session.add(attempt)
    db_session.commit()

    mock_client = MagicMock(spec=RazorpayClient)

    with pytest.raises(GuardrailViolationError, match="Payment amount.*is not positive"):
        execute_recovery_attempt(db_session, attempt, client=mock_client)

    # Razorpay create_payment_link must NEVER be called
    mock_client.create_payment_link.assert_not_called()


# ============================================================================
# 10. PostgreSQL Decimal Serialization Regression Test
# ============================================================================

def test_decimal_active_exposure_serialization_in_guardrails_and_audit(db_session, create_merchant, create_incident_with_diag):
    merchant = create_merchant()
    incident, diag = create_incident_with_diag(merchant.id)

    policy = RecoveryPolicy(
        merchant_id=merchant.id,
        allowed_actions=["incentive", "grace_period"],
        max_incentive=1000,
        max_exposure=50000,
        approval_threshold=100000.0,
    )
    db_session.add(policy)

    # Seed an existing failed payment and an existing active recovery attempt with non-zero incentive
    p_existing = Payment(
        merchant_id=merchant.id,
        razorpay_payment_id="pay_exist_exposure",
        amount=15000,
        currency="INR",
        status="failed",
        created_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    db_session.add(p_existing)
    db_session.commit()

    prior_attempt = RecoveryAttempt(
        recovery_id="rec_prior_exposure",
        merchant_id=merchant.id,
        incident_id=incident.id,
        payment_id=p_existing.id,
        selected_action="incentive",
        incentive_amount=5000,  # Non-zero active exposure
        status="executed",
    )
    db_session.add(prior_attempt)
    db_session.commit()

    # Create target payment for new campaign
    p_new = Payment(
        merchant_id=merchant.id,
        razorpay_payment_id="pay_target_new",
        amount=20000,
        currency="INR",
        status="failed",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
    )
    db_session.add(p_new)
    db_session.commit()

    # Evaluate campaign guardrails
    result = evaluate_campaign_guardrails(
        db=db_session,
        incident=incident,
        policy=policy,
        proposed_action="incentive",
        per_attempt_incentive=500,
        target_payments=[p_new],
    )

    assert result.decision == "allowed"
    exposure_check = next(c for c in result.checks if c.check_name == "campaign_exposure")
    assert exposure_check.passed is True
    # Verify active_exposure is an int, not a Decimal
    assert isinstance(exposure_check.details["active_exposure"], int)
    assert exposure_check.details["active_exposure"] == 5000

    # Create a campaign and ensure audit logging succeeds without Decimal serialization error
    campaign, attempts = create_recovery_campaign(
        db=db_session,
        incident=incident,
        diagnosis=diag,
        policy=policy,
        target_payments=[p_new],
    )
    assert campaign is not None
    assert len(attempts) == 1

    # Verify audit event evidence is valid JSON
    audit_events = db_session.scalars(
        select(RecoveryAuditEvent).where(RecoveryAuditEvent.campaign_id == campaign.id)
    ).all()
    assert len(audit_events) > 0
    for evt in audit_events:
        assert isinstance(evt.evidence, dict)

