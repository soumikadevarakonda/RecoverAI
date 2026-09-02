from datetime import datetime, timedelta, timezone
from typing import Any
import pytest
from sqlalchemy import delete

from app.db.session import SessionLocal
from app.integrations.llm.provider import LLMProvider
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

from app.domains.recovery.economics import evaluate_recovery_economics
from app.domains.recovery.audit import get_recovery_audit_trail, RecoveryAuditEventType
from app.domains.recovery.adaptive_analyst import (
    AdaptiveRecoveryAnalyst,
    AdaptiveRecommendation,
    AdaptiveEvidence,
    AdaptiveTelemetry,
    build_adaptive_evidence,
    verify_adaptive_recommendation,
)
from app.domains.recovery.service import create_recovery_campaign


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
    def _create(name: str = "Adaptive Test Merchant"):
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
            current_failed_count=6,
            current_failure_rate=0.20,
            current_total_amount=100000,
            current_failed_amount=revenue_at_risk,
            baseline_total_count=30,
            baseline_failed_count=1,
            baseline_failure_rate=0.033,
            baseline_total_amount=100000,
            baseline_failed_amount=1000,
            absolute_rate_increase=0.167,
            relative_degradation=6.0,
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
        return incident, diag
    return _create


class MockAdaptiveLLM(LLMProvider):
    def __init__(self, response_data: dict | Exception | None = None, model_name: str = "mock-analyst-v1"):
        self.response_data = response_data
        self.model_name = model_name
        self.last_prompt = None

    def generate_structured_output(
        self,
        prompt: str,
        response_model,
        system_instruction: str | None = None,
        timeout: float = 10.0,
    ):
        self.last_prompt = prompt
        if isinstance(self.response_data, Exception):
            raise self.response_data
        if self.response_data is None:
            raise ValueError("No mock response configured")
        if isinstance(self.response_data, dict):
            return response_model.model_validate(self.response_data)
        return self.response_data


# ============================================================================
# 1. Valid Adaptive Recommendation
# ============================================================================

def test_valid_adaptive_recommendation(db_session, create_merchant, create_policy, create_incident_with_diag):
    merchant = create_merchant()
    incident, diag = create_incident_with_diag(merchant.id)
    policy = create_policy(merchant.id, allowed_actions=["retry", "grace_period"])

    p = Payment(
        merchant_id=merchant.id,
        razorpay_payment_id="pay_adaptive_valid_1",
        amount=10000,
        currency="INR",
        status="failed",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(p)
    db_session.commit()

    candidates = evaluate_recovery_economics(db_session, incident, diag, policy)
    eligible = [c for c in candidates if c.is_eligible]

    mock_llm = MockAdaptiveLLM(
        response_data={
            "intervene": True,
            "confidence": 0.92,
            "recommended_failure_threshold": 8,
            "recommended_failure_rate_threshold": 0.15,
            "urgency": "high",
            "recommended_method": "upi",
            "recommended_bank": "HDFC",
            "recommended_action": "grace_period",
            "reasoning": "Elevated transient timeouts on HDFC UPI resolve quickly under grace period.",
            "evidence_summary": ["6 failures in 30 transactions", "Relative degradation is 6.0x"],
        }
    )

    analyst = AdaptiveRecoveryAnalyst(provider=mock_llm, min_failure_threshold=5)
    result = analyst.analyze(
        incident=incident,
        diagnosis=diag,
        policy=policy,
        eligible_candidates=eligible,
    )

    assert result.is_accepted is True
    assert result.rejection_reason is None
    assert result.recommendation.recommended_action == "grace_period"
    assert result.telemetry.success is True
    assert result.telemetry.fallback_used is False
    assert result.telemetry.latency_ms > 0
    assert result.telemetry.input_size_bytes > 0


# ============================================================================
# 2. Invalid Confidence (Schema Validation)
# ============================================================================

def test_invalid_confidence_triggers_fallback(db_session, create_merchant, create_policy, create_incident_with_diag):
    merchant = create_merchant()
    incident, diag = create_incident_with_diag(merchant.id)
    policy = create_policy(merchant.id)

    candidates = evaluate_recovery_economics(db_session, incident, diag, policy)
    eligible = [c for c in candidates if c.is_eligible]

    # Confidence > 1.0 must fail validation
    mock_llm = MockAdaptiveLLM(
        response_data={
            "intervene": True,
            "confidence": 1.5,
            "recommended_failure_threshold": 10,
            "recommended_failure_rate_threshold": 0.10,
            "urgency": "high",
            "recommended_action": "retry",
            "reasoning": "Invalid confidence test",
            "evidence_summary": [],
        }
    )

    analyst = AdaptiveRecoveryAnalyst(provider=mock_llm)
    result = analyst.analyze(incident, diag, policy, eligible)

    assert result.is_accepted is False
    assert result.recommendation is None
    assert result.telemetry.success is False
    assert result.telemetry.fallback_used is True
    assert result.fallback_action is not None


# ============================================================================
# 3. Invalid Failure Threshold (Schema Validation)
# ============================================================================

def test_invalid_threshold_triggers_fallback(db_session, create_merchant, create_policy, create_incident_with_diag):
    merchant = create_merchant()
    incident, diag = create_incident_with_diag(merchant.id)
    policy = create_policy(merchant.id)

    candidates = evaluate_recovery_economics(db_session, incident, diag, policy)
    eligible = [c for c in candidates if c.is_eligible]

    # Non-positive threshold must fail validation
    mock_llm = MockAdaptiveLLM(
        response_data={
            "intervene": True,
            "confidence": 0.85,
            "recommended_failure_threshold": -5,
            "recommended_failure_rate_threshold": 0.10,
            "urgency": "medium",
            "recommended_action": "retry",
            "reasoning": "Invalid threshold test",
            "evidence_summary": [],
        }
    )

    analyst = AdaptiveRecoveryAnalyst(provider=mock_llm)
    result = analyst.analyze(incident, diag, policy, eligible)

    assert result.is_accepted is False
    assert result.telemetry.success is False
    assert result.telemetry.fallback_used is True


# ============================================================================
# 4. AI Recommending Threshold Below Deterministic Minimum Safety Bounds
# ============================================================================

def test_threshold_below_safety_floor_rejected(db_session, create_merchant, create_policy, create_incident_with_diag):
    merchant = create_merchant()
    incident, diag = create_incident_with_diag(merchant.id)
    policy = create_policy(merchant.id)

    candidates = evaluate_recovery_economics(db_session, incident, diag, policy)
    eligible = [c for c in candidates if c.is_eligible]

    # AI proposes threshold = 2, which is below safety floor of 5
    mock_llm = MockAdaptiveLLM(
        response_data={
            "intervene": True,
            "confidence": 0.90,
            "recommended_failure_threshold": 2,
            "recommended_failure_rate_threshold": 0.10,
            "urgency": "medium",
            "recommended_action": "grace_period",
            "reasoning": "Aggressive low threshold recommendation",
            "evidence_summary": [],
        }
    )

    analyst = AdaptiveRecoveryAnalyst(provider=mock_llm, min_failure_threshold=5)
    result = analyst.analyze(incident, diag, policy, eligible)

    assert result.is_accepted is False
    assert "THRESHOLD_BELOW_SAFETY_MINIMUM" in result.rejection_reason
    assert result.telemetry.fallback_used is True
    assert result.fallback_action is not None


# ============================================================================
# 5. AI Recommending Unavailable Action
# ============================================================================

def test_unavailable_action_rejected(db_session, create_merchant, create_policy, create_incident_with_diag):
    merchant = create_merchant()
    incident, diag = create_incident_with_diag(merchant.id)
    # Policy only allows 'retry' and 'grace_period'
    policy = create_policy(merchant.id, allowed_actions=["retry", "grace_period"])

    candidates = evaluate_recovery_economics(db_session, incident, diag, policy)
    eligible = [c for c in candidates if c.is_eligible]

    # AI recommends 'incentive' (disallowed by policy and not in eligible)
    mock_llm = MockAdaptiveLLM(
        response_data={
            "intervene": True,
            "confidence": 0.95,
            "recommended_failure_threshold": 10,
            "recommended_failure_rate_threshold": 0.10,
            "urgency": "high",
            "recommended_action": "incentive",
            "reasoning": "Offering coupon will recover customers.",
            "evidence_summary": [],
        }
    )

    analyst = AdaptiveRecoveryAnalyst(provider=mock_llm)
    result = analyst.analyze(incident, diag, policy, eligible)

    assert result.is_accepted is False
    assert "ACTION_NOT_ELIGIBLE" in result.rejection_reason
    assert result.telemetry.fallback_used is True


# ============================================================================
# 6. AI Recommending Mismatched Cohort Scope
# ============================================================================

def test_mismatched_cohort_scope_rejected(db_session, create_merchant, create_policy, create_incident_with_diag):
    merchant = create_merchant()
    incident, diag = create_incident_with_diag(merchant.id)
    policy = create_policy(merchant.id)

    candidates = evaluate_recovery_economics(db_session, incident, diag, policy)
    eligible = [c for c in candidates if c.is_eligible]

    # Active incident is HDFC UPI, AI recommends ICICI
    mock_llm = MockAdaptiveLLM(
        response_data={
            "intervene": True,
            "confidence": 0.88,
            "recommended_failure_threshold": 10,
            "recommended_failure_rate_threshold": 0.10,
            "urgency": "medium",
            "recommended_method": "upi",
            "recommended_bank": "ICICI",
            "recommended_action": "grace_period",
            "reasoning": "Mismatched bank scope",
            "evidence_summary": [],
        }
    )

    analyst = AdaptiveRecoveryAnalyst(provider=mock_llm)
    result = analyst.analyze(incident, diag, policy, eligible)

    assert result.is_accepted is False
    assert "COHORT_BANK_MISMATCH" in result.rejection_reason
    assert result.telemetry.fallback_used is True


# ============================================================================
# 7. Provider Timeout Handling
# ============================================================================

def test_provider_timeout_falls_back(db_session, create_merchant, create_policy, create_incident_with_diag):
    merchant = create_merchant()
    incident, diag = create_incident_with_diag(merchant.id)
    policy = create_policy(merchant.id)

    candidates = evaluate_recovery_economics(db_session, incident, diag, policy)
    eligible = [c for c in candidates if c.is_eligible]

    mock_llm = MockAdaptiveLLM(response_data=TimeoutError("LLM inference timed out after 10.0s"))

    analyst = AdaptiveRecoveryAnalyst(provider=mock_llm)
    result = analyst.analyze(incident, diag, policy, eligible)

    assert result.is_accepted is False
    assert "TimeoutError" in result.rejection_reason
    assert result.telemetry.success is False
    assert result.telemetry.fallback_used is True
    assert result.fallback_action is not None


# ============================================================================
# 8. Provider Malformed Response Handling
# ============================================================================

def test_provider_malformed_response_falls_back(db_session, create_merchant, create_policy, create_incident_with_diag):
    merchant = create_merchant()
    incident, diag = create_incident_with_diag(merchant.id)
    policy = create_policy(merchant.id)

    candidates = evaluate_recovery_economics(db_session, incident, diag, policy)
    eligible = [c for c in candidates if c.is_eligible]

    # Missing required field 'urgency' and 'reasoning'
    mock_llm = MockAdaptiveLLM(
        response_data={
            "intervene": True,
            "confidence": 0.90,
            "recommended_failure_threshold": 10,
        }
    )

    analyst = AdaptiveRecoveryAnalyst(provider=mock_llm)
    result = analyst.analyze(incident, diag, policy, eligible)

    assert result.is_accepted is False
    assert result.telemetry.success is False
    assert result.telemetry.fallback_used is True


# ============================================================================
# 9. Deterministic Fallback Selection
# ============================================================================

def test_deterministic_fallback_selection(db_session, create_merchant, create_policy, create_incident_with_diag):
    merchant = create_merchant()
    incident, diag = create_incident_with_diag(merchant.id)
    policy = create_policy(merchant.id)

    candidates = evaluate_recovery_economics(db_session, incident, diag, policy)
    eligible = [c for c in candidates if c.is_eligible]
    expected_best = max(eligible, key=lambda c: c.expected_net_recovery_value).action

    mock_llm = MockAdaptiveLLM(response_data=RuntimeError("Provider offline"))

    analyst = AdaptiveRecoveryAnalyst(provider=mock_llm)
    result = analyst.analyze(incident, diag, policy, eligible)

    assert result.fallback_action == expected_best


# ============================================================================
# 10. Policy Guardrail Override of AI Recommendation
# ============================================================================

def test_policy_guardrail_overrides_ai_recommendation(db_session, create_merchant, create_policy, create_incident_with_diag):
    merchant = create_merchant()
    incident, diag = create_incident_with_diag(merchant.id)
    # Policy allows 'incentive', but max_exposure is capped at 1000
    policy = create_policy(
        merchant.id,
        allowed_actions=["retry", "grace_period", "incentive"],
        max_incentive=500,
        max_exposure=1000,
    )

    # 4 payments * 500 = 2000 total incentive > 1000 max_exposure!
    payments = [
        Payment(
            merchant_id=merchant.id,
            razorpay_payment_id=f"pay_guardrail_veto_{i}",
            amount=10000,
            currency="INR",
            status="failed",
            created_at=datetime.now(timezone.utc),
        )
        for i in range(4)
    ]
    db_session.add_all(payments)
    db_session.commit()

    # AI analyst recommends 'incentive' (valid at analyst layer)
    mock_llm = MockAdaptiveLLM(
        response_data={
            "intervene": True,
            "confidence": 0.94,
            "recommended_failure_threshold": 10,
            "recommended_failure_rate_threshold": 0.15,
            "urgency": "high",
            "recommended_method": "upi",
            "recommended_bank": "HDFC",
            "recommended_action": "incentive",
            "reasoning": "High-value cohort warrants incentive recovery.",
            "evidence_summary": [],
        }
    )
    analyst = AdaptiveRecoveryAnalyst(provider=mock_llm)

    campaign, attempts = create_recovery_campaign(
        db=db_session,
        incident=incident,
        diagnosis=diag,
        policy=policy,
        target_payments=payments,
        adaptive_analyst=analyst,
    )

    # Guardrails must have blocked the incentive action and fallen back to retry/grace_period
    assert campaign.selected_action != "incentive"

    events = get_recovery_audit_trail(db_session, merchant.id, incident_id=incident.id)
    event_types = [e.event_type for e in events]

    assert RecoveryAuditEventType.ADAPTIVE_ANALYSIS_COMPLETED.value in event_types
    assert RecoveryAuditEventType.AI_FALLBACK.value in event_types
    fallback_event = next(e for e in events if e.event_type == RecoveryAuditEventType.AI_FALLBACK.value)
    assert fallback_event.reason_code == "CAMPAIGN_EXPOSURE_EXCEEDED"


# ============================================================================
# 11. Sensitive Evidence Sanitization
# ============================================================================

def test_sensitive_evidence_sanitization(db_session, create_merchant, create_policy, create_incident_with_diag):
    merchant = create_merchant()
    incident, diag = create_incident_with_diag(merchant.id)
    policy = create_policy(merchant.id)

    candidates = evaluate_recovery_economics(db_session, incident, diag, policy)
    eligible = [c for c in candidates if c.is_eligible]

    historical = {
        "retry": {
            "evidence_level": "exact_cohort",
            "sample_size": 15,
            "observed_recovery_rate": 0.80,
            "secret_api_key": "sk_test_sensitive_12345",
            "authorization": "Bearer token_xyz",
        }
    }

    evidence = build_adaptive_evidence(
        incident=incident,
        diagnosis=diag,
        policy=policy,
        eligible_candidates=eligible,
        historical_evidence=historical,
    )
    evidence_dict = evidence.model_dump()

    hist_data = evidence_dict["historical_recovery_performance"]["retry"]
    assert "secret_api_key" not in hist_data
    assert "authorization" not in hist_data
    assert hist_data["sample_size"] == 15


# ============================================================================
# 12. Audit Event Creation
# ============================================================================

def test_adaptive_audit_events_created(db_session, create_merchant, create_policy, create_incident_with_diag):
    merchant = create_merchant()
    incident, diag = create_incident_with_diag(merchant.id)
    policy = create_policy(merchant.id)

    p = Payment(
        merchant_id=merchant.id,
        razorpay_payment_id="pay_audit_adaptive_1",
        amount=10000,
        currency="INR",
        status="failed",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(p)
    db_session.commit()

    mock_llm = MockAdaptiveLLM(
        response_data={
            "intervene": True,
            "confidence": 0.91,
            "recommended_failure_threshold": 12,
            "recommended_failure_rate_threshold": 0.20,
            "urgency": "medium",
            "recommended_action": "grace_period",
            "reasoning": "Transient issue resolves on grace period",
            "evidence_summary": [],
        }
    )
    analyst = AdaptiveRecoveryAnalyst(provider=mock_llm)

    campaign, attempts = create_recovery_campaign(
        db=db_session,
        incident=incident,
        diagnosis=diag,
        policy=policy,
        target_payments=[p],
        adaptive_analyst=analyst,
    )

    events = get_recovery_audit_trail(db_session, merchant.id, incident_id=incident.id)
    event_types = [e.event_type for e in events]

    assert RecoveryAuditEventType.ADAPTIVE_ANALYSIS_COMPLETED.value in event_types
    audit_item = next(e for e in events if e.event_type == RecoveryAuditEventType.ADAPTIVE_ANALYSIS_COMPLETED.value)
    assert audit_item.evidence["confidence"] == 0.91
    assert audit_item.evidence["recommended_failure_threshold"] == 12


# ============================================================================
# 13. AI Cost & Latency Telemetry Capture
# ============================================================================

def test_telemetry_capture(db_session, create_merchant, create_policy, create_incident_with_diag):
    merchant = create_merchant()
    incident, diag = create_incident_with_diag(merchant.id)
    policy = create_policy(merchant.id)

    candidates = evaluate_recovery_economics(db_session, incident, diag, policy)
    eligible = [c for c in candidates if c.is_eligible]

    mock_llm = MockAdaptiveLLM(
        response_data={
            "intervene": True,
            "confidence": 0.89,
            "recommended_failure_threshold": 6,
            "recommended_failure_rate_threshold": 0.05,
            "urgency": "low",
            "recommended_action": "grace_period",
            "reasoning": "Low-urgency grace period suffices.",
            "evidence_summary": [],
        },
        model_name="gemini-1.5-pro",
    )

    analyst = AdaptiveRecoveryAnalyst(provider=mock_llm)
    result = analyst.analyze(incident, diag, policy, eligible)

    telem = result.telemetry
    assert telem.provider == "MockAdaptiveLLM"
    assert telem.model_name == "gemini-1.5-pro"
    assert telem.latency_ms > 0
    assert telem.input_size_bytes > 0
    assert telem.output_size_bytes > 0
    assert telem.success is True
    assert telem.fallback_used is False
    assert telem.cost_status == "UNAVAILABLE"
    assert telem.estimated_cost_usd is None


def test_token_usage_tracking_and_cost_calculation(db_session, create_merchant, create_policy, create_incident_with_diag):
    """
    Verifies that when token counts and pricing are configured on the provider,
    input/output/total tokens are captured and estimated_cost_usd is honestly calculated.
    """
    from app.integrations.llm.provider import LLMTokenUsage

    merchant = create_merchant()
    incident, diag = create_incident_with_diag(merchant.id)
    policy = create_policy(merchant.id)

    candidates = evaluate_recovery_economics(db_session, incident, diag, policy)
    eligible = [c for c in candidates if c.is_eligible]

    mock_llm = MockAdaptiveLLM(
        response_data={
            "intervene": True,
            "confidence": 0.95,
            "recommended_failure_threshold": 10,
            "recommended_failure_rate_threshold": 0.1,
            "urgency": "medium",
            "recommended_action": "grace_period",
            "reasoning": "Sufficient volume for grace period",
            "evidence_summary": [],
        },
        model_name="gemini-1.5-pro",
    )
    # Configure token usage & pricing on provider
    mock_llm.last_token_usage = LLMTokenUsage(input_tokens=1500, output_tokens=250, total_tokens=1750)
    mock_llm.pricing_per_million = {"input": 3.50, "output": 10.50}

    analyst = AdaptiveRecoveryAnalyst(provider=mock_llm)
    result = analyst.analyze(incident, diag, policy, eligible)

    telem = result.telemetry
    assert telem.input_tokens == 1500
    assert telem.output_tokens == 250
    assert telem.total_tokens == 1750
    assert telem.cost_status == "AVAILABLE"
    # Cost = (1500 * 3.50 + 250 * 10.50) / 1,000,000 = (5250 + 2625) / 1,000,000 = 0.007875 USD
    assert telem.estimated_cost_usd == pytest.approx(0.007875, rel=1e-5)


def test_missing_pricing_keeps_cost_unavailable(db_session, create_merchant, create_policy, create_incident_with_diag):
    """
    Verifies that when token counts are present but pricing is absent,
    cost is never fabricated and remains explicitly UNAVAILABLE.
    """
    from app.integrations.llm.provider import LLMTokenUsage

    merchant = create_merchant()
    incident, diag = create_incident_with_diag(merchant.id)
    policy = create_policy(merchant.id)

    candidates = evaluate_recovery_economics(db_session, incident, diag, policy)
    eligible = [c for c in candidates if c.is_eligible]

    mock_llm = MockAdaptiveLLM(
        response_data={
            "intervene": False,
            "confidence": 0.88,
            "recommended_failure_threshold": 5,
            "recommended_failure_rate_threshold": 0.05,
            "urgency": "low",
            "reasoning": "Low failure volume",
            "evidence_summary": [],
        },
        model_name="gemini-1.5-flash",
    )
    # Token usage present, but NO pricing_per_million
    mock_llm.last_token_usage = LLMTokenUsage(input_tokens=800, output_tokens=100, total_tokens=900)
    mock_llm.pricing_per_million = None

    analyst = AdaptiveRecoveryAnalyst(provider=mock_llm)
    result = analyst.analyze(incident, diag, policy, eligible)

    telem = result.telemetry
    assert telem.input_tokens == 800
    assert telem.output_tokens == 100
    assert telem.total_tokens == 900
    assert telem.cost_status == "UNAVAILABLE"
    assert telem.estimated_cost_usd is None
