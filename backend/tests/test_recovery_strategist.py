from datetime import datetime, timedelta, timezone
import pytest
from pydantic import ValidationError

from app.domains.recovery.strategist import RecoveryStrategist, AIRecommendation
from app.domains.recovery.economics import ActionEconomics
from app.domains.recovery.service import decide_recovery_action
from app.models.incident import Incident
from app.models.diagnosis import Diagnosis
from app.models.recovery_policy import RecoveryPolicy
from app.integrations.llm.provider import LLMProvider
from app.db.session import SessionLocal
from app.models.recovery_attempt import RecoveryAttempt
from app.models.recovery_campaign import RecoveryCampaign
from app.models.payment import Payment
from app.models.payment_event import PaymentEvent
from app.models.webhook_event import WebhookEvent

@pytest.fixture
def db_session():
    session = SessionLocal()
    try:
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

class MockLLMProvider(LLMProvider):
    def __init__(self, response_data: dict | Exception | None = None):
        self.response_data = response_data
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
            raise ValueError("No mock data configured")
        
        if isinstance(self.response_data, dict):
            return response_model.model_validate(self.response_data)
        
        return self.response_data


from app.models.merchant import Merchant

@pytest.fixture
def test_context(db_session):
    merchant = Merchant(name="Test Merchant")
    db_session.add(merchant)
    db_session.commit()
    db_session.refresh(merchant)

    incident = Incident(
        merchant_id=merchant.id,
        method="upi",
        bank="HDFC",
        error_code="GATEWAY_ERROR",
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
        revenue_at_risk=10000,
        window_start=datetime.now(timezone.utc) - timedelta(minutes=30),
        window_end=datetime.now(timezone.utc),
        baseline_start=datetime.now(timezone.utc) - timedelta(hours=2),
        baseline_end=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    db_session.add(incident)
    db_session.commit()
    db_session.refresh(incident)

    # Seed matching real failed payment
    import uuid
    payment = Payment(
        merchant_id=merchant.id,
        razorpay_payment_id=f"pay_strat_{uuid.uuid4().hex[:10]}",
        amount=10000,
        currency="INR",
        status="failed",
        method=incident.method,
        bank=incident.bank,
        error_code=incident.error_code,
        error_step=incident.error_step,
        created_at=incident.window_start + timedelta(minutes=5),
    )
    db_session.add(payment)
    db_session.commit()

    diagnosis = Diagnosis(
        incident_id=incident.id,
        diagnosis_type="error-code spike",
        explanation="HDFC UPI gateway latency/failures",
        supporting_evidence={},
        confidence=0.9,
    )
    db_session.add(diagnosis)
    db_session.commit()
    db_session.refresh(diagnosis)

    policy = RecoveryPolicy(
        merchant_id=merchant.id,
        allowed_actions=["retry", "grace_period"],
        max_incentive=1000,
        max_exposure=10000,
        approval_threshold=5000.0,
    )
    db_session.add(policy)
    db_session.commit()
    db_session.refresh(policy)

    eligible_candidates = [
        ActionEconomics(
            action="retry",
            expected_recovery_amount=3000,
            action_cost=0,
            expected_net_recovery_value=3000,
            expected_recovery_rate=0.3,
            is_eligible=True,
            rate_source="configured",
        ),
        ActionEconomics(
            action="grace_period",
            expected_recovery_amount=2000,
            action_cost=0,
            expected_net_recovery_value=2000,
            expected_recovery_rate=0.2,
            is_eligible=True,
            rate_source="configured",
        ),
    ]
    historical_evidence = {
        "retry": {"evidence_level": "global", "sample_size": 10, "observed_recovery_rate": 0.3},
        "grace_period": {"evidence_level": "global", "sample_size": 8, "observed_recovery_rate": 0.2},
    }
    return incident, diagnosis, policy, eligible_candidates, historical_evidence



def test_valid_recommendation(test_context):
    incident, diagnosis, policy, candidates, evidence = test_context
    mock_resp = {
        "recommended_action": "retry",
        "concise_reason": "High recovery rate for UPI retry links",
        "evidence_level": "global",
        "sample_size": 10,
        "observed_recovery_rate": 0.3,
        "expected_net_recovery_value": 3000,
        "confidence": 0.85,
    }
    provider = MockLLMProvider(response_data=mock_resp)
    strategist = RecoveryStrategist(provider=provider)

    rec = strategist.recommend_action(incident, diagnosis, policy, candidates, evidence)

    assert rec.recommended_action == "retry"
    assert rec.confidence == 0.85
    assert rec.observed_recovery_rate == 0.3
    assert "High recovery rate" in rec.concise_reason


def test_invalid_action_rejected(test_context):
    incident, diagnosis, policy, candidates, evidence = test_context
    # Returns action "incentive" which is NOT in eligible candidates ["retry", "grace_period"]
    mock_resp = {
        "recommended_action": "incentive",
        "concise_reason": "Offer high coupon",
        "evidence_level": "none",
        "sample_size": 0,
        "observed_recovery_rate": 0.0,
        "expected_net_recovery_value": 0,
        "confidence": 0.9,
    }
    provider = MockLLMProvider(response_data=mock_resp)
    strategist = RecoveryStrategist(provider=provider)

    with pytest.raises(ValueError, match="is not in eligible actions"):
        strategist.recommend_action(incident, diagnosis, policy, candidates, evidence)


def test_malformed_model_response(test_context):
    incident, diagnosis, policy, candidates, evidence = test_context
    # Invalid confidence (1.5 is outside [0.0, 1.0])
    mock_resp = {
        "recommended_action": "retry",
        "concise_reason": "Reason",
        "evidence_level": "global",
        "sample_size": 10,
        "observed_recovery_rate": 0.3,
        "expected_net_recovery_value": 3000,
        "confidence": 1.5,
    }
    provider = MockLLMProvider(response_data=mock_resp)
    strategist = RecoveryStrategist(provider=provider)

    with pytest.raises(ValidationError, match="Confidence must be between 0.0 and 1.0"):
        strategist.recommend_action(incident, diagnosis, policy, candidates, evidence)


def test_provider_failure_with_deterministic_fallback(db_session, test_context):
    incident, diagnosis, policy, candidates, evidence = test_context
    # Provider raises an exception (API timeout/failure)
    provider = MockLLMProvider(response_data=Exception("Gemini API rate limit exceeded"))

    # Pass the MockLLMProvider to decide_recovery_action
    attempt = decide_recovery_action(db_session, incident, diagnosis, policy, llm_provider=provider)

    # Should fall back to the deterministic sorting decision
    # Candidates: retry (3000 net), gp (2000 net). Retry has highest net value.
    assert attempt.selected_action == "retry"
    assert attempt.status == "approved"


def test_eligible_candidate_restriction(db_session, test_context):
    incident, diagnosis, policy, candidates, evidence = test_context
    # Limit allowed actions to grace_period only
    policy.allowed_actions = ["grace_period"]

    # LLM recommends "retry" which is not allowed/eligible anymore
    mock_resp = {
        "recommended_action": "retry",
        "concise_reason": "Retry is normally best",
        "evidence_level": "global",
        "sample_size": 10,
        "observed_recovery_rate": 0.3,
        "expected_net_recovery_value": 3000,
        "confidence": 0.95,
    }
    provider = MockLLMProvider(response_data=mock_resp)

    # decide_recovery_action should reject "retry" because evaluate_recovery_economics marks it ineligible
    # It should fall back to deterministic selection among eligible candidates (grace_period)
    attempt = decide_recovery_action(db_session, incident, diagnosis, policy, llm_provider=provider)

    assert attempt.selected_action == "grace_period"
    assert attempt.status == "approved"


def test_decide_recovery_action_uses_ai_recommendation(db_session, test_context):
    incident, diagnosis, policy, candidates, evidence = test_context
    
    # Even though retry (3000 net) has higher deterministic priority than grace_period (2000 net),
    # the AI Strategist recommends grace_period
    mock_resp = {
        "recommended_action": "grace_period",
        "concise_reason": "UPI gateway is unstable; grace period is safer",
        "evidence_level": "global",
        "sample_size": 8,
        "observed_recovery_rate": 0.2,
        "expected_net_recovery_value": 2000,
        "confidence": 0.9,
    }
    provider = MockLLMProvider(response_data=mock_resp)

    attempt = decide_recovery_action(db_session, incident, diagnosis, policy, llm_provider=provider)

    # Asserts that AI Strategist recommendation is followed
    assert attempt.selected_action == "grace_period"
    assert attempt.status == "approved"
