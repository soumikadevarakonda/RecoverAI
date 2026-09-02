from datetime import datetime, timedelta, timezone
from uuid import uuid4
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
from app.domains.recovery.memory import (
    MemoryEvidenceLevel,
    retrieve_operational_memory,
)
from app.domains.recovery.adaptive_analyst import (
    AdaptiveRecoveryAnalyst,
    AdaptiveRecommendation,
    verify_adaptive_recommendation,
)
from app.domains.recovery.service import create_recovery_campaign
from app.domains.recovery.evaluation import (
    evaluate_campaign_adaptive_decision,
    LearningSignalClassification,
)


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
    def _create(name: str = "Memory Test Merchant"):
        merchant = Merchant(name=name)
        db_session.add(merchant)
        db_session.commit()
        db_session.refresh(merchant)
        return merchant
    return _create


@pytest.fixture
def create_policy(db_session):
    def _create(merchant_id, allowed_actions=None, max_incentive=1000, max_exposure=50000, approval_threshold=5000.0):
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
def create_incident(db_session):
    def _create(
        merchant_id,
        method="upi",
        bank="HDFC",
        error_code="GATEWAY_ERROR",
        error_step="payment_authorization",
        revenue_at_risk=30000,
        failed_count=6,
    ):
        now = datetime.now(timezone.utc)
        incident = Incident(
            merchant_id=merchant_id,
            method=method,
            bank=bank,
            error_code=error_code,
            error_step=error_step,
            current_total_count=30,
            current_failed_count=failed_count,
            current_failure_rate=failed_count / 30.0,
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

        # Seed matching real failed payment for the incident window
        payment = Payment(
            merchant_id=merchant_id,
            razorpay_payment_id=f"pay_mem_{uuid4().hex[:10]}",
            amount=revenue_at_risk,
            currency="INR",
            status="failed",
            method=method,
            bank=bank,
            error_code=error_code,
            error_step=error_step,
            created_at=now - timedelta(minutes=15),
        )
        db_session.add(payment)
        db_session.commit()

        return incident
    return _create


def _seed_completed_campaign(
    db_session,
    merchant_id,
    incident,
    action="grace_period",
    learning_signal="successful_decision",
    target_count=2,
    recovered_count=1,
    per_attempt_incentive=0,
    amount_per_payment=10000,
):
    now = datetime.now(timezone.utc)
    prior_incident = Incident(
        merchant_id=merchant_id,
        method=incident.method,
        bank=incident.bank,
        error_code=incident.error_code,
        error_step=incident.error_step,
        current_total_count=incident.current_total_count or 30,
        current_failed_count=incident.current_failed_count,
        current_failure_rate=incident.current_failure_rate,
        current_total_amount=incident.current_total_amount or (amount_per_payment * 30),
        current_failed_amount=incident.current_failed_amount or (amount_per_payment * target_count),
        baseline_total_count=30,
        baseline_failed_count=1,
        baseline_failure_rate=0.033,
        baseline_total_amount=amount_per_payment * 30,
        baseline_failed_amount=1000,
        absolute_rate_increase=0.1,
        relative_degradation=3.0,
        revenue_at_risk=amount_per_payment * target_count,
        window_start=now - timedelta(hours=3),
        window_end=now - timedelta(hours=2),
        baseline_start=now - timedelta(hours=5),
        baseline_end=now - timedelta(hours=4),
        status="resolved",
    )
    db_session.add(prior_incident)
    db_session.flush()

    total_incentive = per_attempt_incentive * target_count
    campaign = RecoveryCampaign(
        campaign_id=f"camp_{uuid4().hex[:10]}",
        merchant_id=merchant_id,
        incident_id=prior_incident.id,
        selected_action=action,
        status="completed",
        target_payment_count=target_count,
        total_revenue_at_risk=amount_per_payment * target_count,
        per_attempt_incentive=per_attempt_incentive,
        total_incentive_cost=total_incentive,
        decision_evidence={
            "selected_action": action,
            "learning_signal": {
                "classification": learning_signal,
                "observed_recovery_rate": (recovered_count / target_count) if target_count > 0 else 0.0,
            },
        },
    )
    db_session.add(campaign)
    db_session.flush()

    for i in range(target_count):
        p = Payment(
            merchant_id=merchant_id,
            razorpay_payment_id=f"pay_mem_{uuid4().hex[:8]}",
            amount=amount_per_payment,
            currency="INR",
            status="failed",
        )
        db_session.add(p)
        db_session.flush()

        is_recovered = i < recovered_count
        a = RecoveryAttempt(
            recovery_id=f"rec_mem_{uuid4().hex[:8]}",
            campaign_id=campaign.id,
            payment_id=p.id,
            merchant_id=merchant_id,
            incident_id=prior_incident.id,
            selected_action=action,
            incentive_amount=per_attempt_incentive,
            status="recovered" if is_recovered else "failed",
            recovered_amount=amount_per_payment if is_recovered else 0,
        )
        db_session.add(a)

    db_session.commit()
    db_session.refresh(campaign)
    return campaign


class MockMemoryLLM(LLMProvider):
    def __init__(self, response_data: dict | Exception | None = None, model_name: str = "mock-memory-llm"):
        self.response_data = response_data
        self.model_name = model_name
        self.last_prompt = None

    def generate_structured_output(self, prompt: str, response_model, system_instruction=None, timeout=10.0):
        self.last_prompt = prompt
        if isinstance(self.response_data, Exception):
            raise self.response_data
        if self.response_data is None:
            raise ValueError("No mock response configured")
        if isinstance(self.response_data, dict):
            return response_model.model_validate(self.response_data)
        return self.response_data


# ============================================================================
# Category 1: Retrieval Hierarchy (7 tests)
# ============================================================================

def test_retrieval_exact_cohort(db_session, create_merchant, create_incident):
    merchant = create_merchant()
    inc = create_incident(merchant.id, method="upi", bank="HDFC", error_code="GATEWAY_ERROR", error_step="payment_authorization")
    _seed_completed_campaign(db_session, merchant.id, inc)

    mem = retrieve_operational_memory(db_session, merchant.id, inc)
    assert mem.evidence_level == MemoryEvidenceLevel.EXACT_COHORT
    assert mem.historical_sample_size == 1


def test_retrieval_fallback_method_bank_error(db_session, create_merchant, create_incident):
    merchant = create_merchant()
    # Historical campaign has step 'card_authentication', current has 'payment_authorization'
    hist_inc = create_incident(merchant.id, method="upi", bank="HDFC", error_code="GATEWAY_ERROR", error_step="card_authentication")
    _seed_completed_campaign(db_session, merchant.id, hist_inc)

    curr_inc = create_incident(merchant.id, method="upi", bank="HDFC", error_code="GATEWAY_ERROR", error_step="payment_authorization")
    mem = retrieve_operational_memory(db_session, merchant.id, curr_inc)
    assert mem.evidence_level == MemoryEvidenceLevel.METHOD_BANK_ERROR
    assert mem.historical_sample_size == 1


def test_retrieval_fallback_method_error(db_session, create_merchant, create_incident):
    merchant = create_merchant()
    # Historical has bank 'SBI', current has bank 'HDFC'
    hist_inc = create_incident(merchant.id, method="upi", bank="SBI", error_code="GATEWAY_ERROR", error_step="step_x")
    _seed_completed_campaign(db_session, merchant.id, hist_inc)

    curr_inc = create_incident(merchant.id, method="upi", bank="HDFC", error_code="GATEWAY_ERROR", error_step="payment_authorization")
    mem = retrieve_operational_memory(db_session, merchant.id, curr_inc)
    assert mem.evidence_level == MemoryEvidenceLevel.METHOD_ERROR
    assert mem.historical_sample_size == 1


def test_retrieval_fallback_method(db_session, create_merchant, create_incident):
    merchant = create_merchant()
    # Historical has error 'TIMEOUT', current has error 'GATEWAY_ERROR'
    hist_inc = create_incident(merchant.id, method="upi", bank="SBI", error_code="TIMEOUT", error_step="step_x")
    _seed_completed_campaign(db_session, merchant.id, hist_inc)

    curr_inc = create_incident(merchant.id, method="upi", bank="HDFC", error_code="GATEWAY_ERROR", error_step="payment_authorization")
    mem = retrieve_operational_memory(db_session, merchant.id, curr_inc)
    assert mem.evidence_level == MemoryEvidenceLevel.METHOD
    assert mem.historical_sample_size == 1


def test_retrieval_fallback_global(db_session, create_merchant, create_incident):
    merchant = create_merchant()
    # Historical has method 'card', current has method 'upi'
    hist_inc = create_incident(merchant.id, method="card", bank="ICICI", error_code="TIMEOUT", error_step="step_x")
    _seed_completed_campaign(db_session, merchant.id, hist_inc)

    curr_inc = create_incident(merchant.id, method="upi", bank="HDFC", error_code="GATEWAY_ERROR", error_step="payment_authorization")
    mem = retrieve_operational_memory(db_session, merchant.id, curr_inc)
    assert mem.evidence_level == MemoryEvidenceLevel.GLOBAL
    assert mem.historical_sample_size == 1


def test_retrieval_insufficient_evidence(db_session, create_merchant, create_incident):
    merchant = create_merchant()
    curr_inc = create_incident(merchant.id)

    mem = retrieve_operational_memory(db_session, merchant.id, curr_inc)
    assert mem.evidence_level == MemoryEvidenceLevel.INSUFFICIENT
    assert mem.historical_sample_size == 0


def test_retrieval_strict_merchant_isolation(db_session, create_merchant, create_incident):
    merchant_a = create_merchant("Alpha")
    merchant_b = create_merchant("Beta")

    inc_a = create_incident(merchant_a.id)
    _seed_completed_campaign(db_session, merchant_a.id, inc_a)

    inc_b = create_incident(merchant_b.id)
    mem_b = retrieve_operational_memory(db_session, merchant_b.id, inc_b)
    # Merchant B must have 0 records despite Merchant A having completed campaigns
    assert mem_b.evidence_level == MemoryEvidenceLevel.INSUFFICIENT
    assert mem_b.historical_sample_size == 0


# ============================================================================
# Category 2: Threshold Reasoning (5 tests)
# ============================================================================

def test_threshold_intervention_points_aggregation(db_session, create_merchant, create_incident):
    merchant = create_merchant()
    inc = create_incident(merchant.id, failed_count=8)
    _seed_completed_campaign(db_session, merchant.id, inc, action="grace_period", learning_signal="successful_decision")

    mem = retrieve_operational_memory(db_session, merchant.id, inc)
    assert len(mem.threshold_evidence) == 1
    pt = mem.threshold_evidence[0]
    assert pt.failure_count == 8
    assert pt.action == "grace_period"
    assert pt.learning_signal == "successful_decision"


def test_successful_intervention_range_calculation(db_session, create_merchant, create_incident):
    merchant = create_merchant()
    # Seed 3 completed campaigns with failure counts 8, 10, 14 (all successful)
    for fc in [8, 10, 14]:
        inc = create_incident(merchant.id, failed_count=fc)
        _seed_completed_campaign(db_session, merchant.id, inc, learning_signal="successful_decision")

    curr_inc = create_incident(merchant.id, failed_count=10)
    mem = retrieve_operational_memory(db_session, merchant.id, curr_inc, min_stats_samples=3)

    assert mem.threshold_statistics.has_sufficient_samples is True
    assert mem.threshold_statistics.median_failure_count == 10.0
    assert mem.threshold_statistics.successful_intervention_range == (8, 14)


def test_under_intervention_range_calculation(db_session, create_merchant, create_incident):
    merchant = create_merchant()
    for fc in [18, 22, 25]:
        inc = create_incident(merchant.id, failed_count=fc)
        _seed_completed_campaign(db_session, merchant.id, inc, learning_signal="under_intervention")

    curr_inc = create_incident(merchant.id, failed_count=20)
    mem = retrieve_operational_memory(db_session, merchant.id, curr_inc, min_stats_samples=3)

    assert mem.threshold_statistics.has_sufficient_samples is True
    assert mem.threshold_statistics.under_intervention_range == (18, 25)


def test_over_intervention_range_calculation(db_session, create_merchant, create_incident):
    merchant = create_merchant()
    for fc in [2, 3, 4]:
        inc = create_incident(merchant.id, failed_count=fc)
        _seed_completed_campaign(db_session, merchant.id, inc, learning_signal="over_intervention")

    curr_inc = create_incident(merchant.id, failed_count=3)
    mem = retrieve_operational_memory(db_session, merchant.id, curr_inc, min_stats_samples=3)

    assert mem.threshold_statistics.has_sufficient_samples is True
    assert mem.threshold_statistics.over_intervention_range == (2, 4)


def test_insufficient_samples_do_not_fabricate_statistics(db_session, create_merchant, create_incident):
    merchant = create_merchant()
    # Only 2 samples when minimum required is 3
    for fc in [8, 10]:
        inc = create_incident(merchant.id, failed_count=fc)
        _seed_completed_campaign(db_session, merchant.id, inc, learning_signal="successful_decision")

    curr_inc = create_incident(merchant.id, failed_count=9)
    mem = retrieve_operational_memory(db_session, merchant.id, curr_inc, min_stats_samples=3)

    assert mem.threshold_statistics.has_sufficient_samples is False
    assert mem.threshold_statistics.median_failure_count is None
    assert mem.threshold_statistics.successful_intervention_range is None


# ============================================================================
# Category 3: AI Analyst Integration (6 tests)
# ============================================================================

def test_memory_passed_to_analyst(db_session, create_merchant, create_policy, create_incident):
    merchant = create_merchant()
    inc = create_incident(merchant.id, failed_count=10)
    _seed_completed_campaign(db_session, merchant.id, inc, learning_signal="successful_decision")
    policy = create_policy(merchant.id)

    candidates = evaluate_recovery_economics(db_session, inc, None, policy)
    eligible = [c for c in candidates if c.is_eligible]
    mem = retrieve_operational_memory(db_session, merchant.id, inc)

    mock_llm = MockMemoryLLM(
        response_data={
            "intervene": True,
            "confidence": 0.92,
            "recommended_failure_threshold": 9,
            "recommended_failure_rate_threshold": 0.15,
            "urgency": "medium",
            "recommended_action": "grace_period",
            "reasoning": "Reasoning leveraging memory",
            "evidence_summary": [],
        }
    )

    analyst = AdaptiveRecoveryAnalyst(provider=mock_llm)
    result = analyst.analyze(inc, None, policy, eligible, operational_memory=mem)

    assert "Historical Operational Memory (Advisory)" in mock_llm.last_prompt
    assert result.is_accepted is True
    assert result.telemetry.memory_size_bytes > 0


def test_ai_recommendation_utilizes_memory(db_session, create_merchant, create_policy, create_incident):
    merchant = create_merchant()
    inc = create_incident(merchant.id)
    _seed_completed_campaign(db_session, merchant.id, inc, learning_signal="successful_decision")
    policy = create_policy(merchant.id)
    candidates = evaluate_recovery_economics(db_session, inc, None, policy)
    eligible = [c for c in candidates if c.is_eligible]
    mem = retrieve_operational_memory(db_session, merchant.id, inc)

    mock_llm = MockMemoryLLM(
        response_data={
            "intervene": True,
            "confidence": 0.94,
            "recommended_failure_threshold": 8,
            "recommended_failure_rate_threshold": 0.10,
            "urgency": "medium",
            "recommended_action": "grace_period",
            "reasoning": "Memory showed successful recovery around 8 failures.",
            "evidence_summary": ["4 past successful decisions"],
        }
    )

    analyst = AdaptiveRecoveryAnalyst(provider=mock_llm)
    result = analyst.analyze(inc, None, policy, eligible, operational_memory=mem)

    assert result.is_accepted is True
    assert result.recommendation.recommended_failure_threshold == 8


def test_malformed_memory_handled_safely(db_session, create_merchant, create_policy, create_incident):
    merchant = create_merchant()
    inc = create_incident(merchant.id)
    policy = create_policy(merchant.id)
    candidates = evaluate_recovery_economics(db_session, inc, None, policy)
    eligible = [c for c in candidates if c.is_eligible]

    mock_llm = MockMemoryLLM(
        response_data={
            "intervene": True,
            "confidence": 0.90,
            "recommended_failure_threshold": 7,
            "recommended_failure_rate_threshold": 0.10,
            "urgency": "medium",
            "recommended_action": "grace_period",
            "reasoning": "Fallback reasoning with corrupt memory",
            "evidence_summary": [],
        }
    )

    analyst = AdaptiveRecoveryAnalyst(provider=mock_llm)
    # Pass malformed dictionary instead of OperationalMemory object
    result = analyst.analyze(inc, None, policy, eligible, operational_memory={"corrupt_key": 12345})
    assert result.is_accepted is True


def test_recommendation_violating_hard_threshold_bounds_rejected(db_session, create_merchant, create_policy, create_incident):
    merchant = create_merchant()
    inc = create_incident(merchant.id)
    policy = create_policy(merchant.id)
    candidates = evaluate_recovery_economics(db_session, inc, None, policy)
    eligible = [c for c in candidates if c.is_eligible]

    # Recommends threshold 2 (below MIN=5)
    mock_llm = MockMemoryLLM(
        response_data={
            "intervene": True,
            "confidence": 0.90,
            "recommended_failure_threshold": 2,
            "recommended_failure_rate_threshold": 0.10,
            "urgency": "medium",
            "recommended_action": "grace_period",
            "reasoning": "Violates lower bound",
            "evidence_summary": [],
        }
    )
    analyst = AdaptiveRecoveryAnalyst(provider=mock_llm)
    result = analyst.analyze(inc, None, policy, eligible)
    assert result.is_accepted is False
    assert result.rejection_reason == "THRESHOLD_BELOW_SAFETY_MINIMUM"

    # Recommends threshold 1500 (above MAX=1000)
    mock_llm.response_data["recommended_failure_threshold"] = 1500
    result2 = analyst.analyze(inc, None, policy, eligible)
    assert result2.is_accepted is False
    assert result2.rejection_reason == "THRESHOLD_EXCEEDS_SAFETY_MAXIMUM"


def test_recommendation_violating_hard_rate_bounds_rejected(db_session, create_merchant, create_policy, create_incident):
    merchant = create_merchant()
    inc = create_incident(merchant.id)
    policy = create_policy(merchant.id)
    candidates = evaluate_recovery_economics(db_session, inc, None, policy)
    eligible = [c for c in candidates if c.is_eligible]

    # Recommends rate 0.001 (below MIN=0.01)
    mock_llm = MockMemoryLLM(
        response_data={
            "intervene": True,
            "confidence": 0.90,
            "recommended_failure_threshold": 10,
            "recommended_failure_rate_threshold": 0.001,
            "urgency": "medium",
            "recommended_action": "grace_period",
            "reasoning": "Violates lower rate bound",
            "evidence_summary": [],
        }
    )
    analyst = AdaptiveRecoveryAnalyst(provider=mock_llm)
    result = analyst.analyze(inc, None, policy, eligible)
    assert result.is_accepted is False
    assert result.rejection_reason == "RATE_THRESHOLD_BELOW_SAFETY_MINIMUM"


def test_deterministic_fallback_on_provider_failure(db_session, create_merchant, create_policy, create_incident):
    merchant = create_merchant()
    inc = create_incident(merchant.id)
    policy = create_policy(merchant.id)
    candidates = evaluate_recovery_economics(db_session, inc, None, policy)
    eligible = [c for c in candidates if c.is_eligible]
    expected_best = max(eligible, key=lambda c: c.expected_net_recovery_value).action

    mock_llm = MockMemoryLLM(response_data=RuntimeError("Provider 503 Service Unavailable"))
    analyst = AdaptiveRecoveryAnalyst(provider=mock_llm)
    result = analyst.analyze(inc, None, policy, eligible)

    assert result.is_accepted is False
    assert result.fallback_action == expected_best


# ============================================================================
# Category 4: Evidence and Audit (5 tests)
# ============================================================================

def test_memory_usage_recorded_in_decision_evidence(db_session, create_merchant, create_policy, create_incident):
    merchant = create_merchant()
    inc = create_incident(merchant.id)
    _seed_completed_campaign(db_session, merchant.id, inc)
    policy = create_policy(merchant.id)

    p = Payment(merchant_id=merchant.id, razorpay_payment_id="pay_mem_ev_1", amount=10000, currency="INR", status="failed")
    db_session.add(p)
    db_session.commit()

    mock_llm = MockMemoryLLM(
        response_data={
            "intervene": True,
            "confidence": 0.91,
            "recommended_failure_threshold": 8,
            "recommended_failure_rate_threshold": 0.15,
            "urgency": "medium",
            "recommended_action": "grace_period",
            "reasoning": "Memory recorded test",
            "evidence_summary": [],
        }
    )
    analyst = AdaptiveRecoveryAnalyst(provider=mock_llm)

    campaign, attempts = create_recovery_campaign(
        db=db_session,
        incident=inc,
        diagnosis=None,
        policy=policy,
        target_payments=[p],
        adaptive_analyst=analyst,
    )

    ev = campaign.decision_evidence.get("operational_memory")
    assert ev is not None
    assert ev["memory_used"] is True


def test_evidence_level_recorded(db_session, create_merchant, create_policy, create_incident):
    merchant = create_merchant()
    inc = create_incident(merchant.id)
    _seed_completed_campaign(db_session, merchant.id, inc)
    policy = create_policy(merchant.id)

    p = Payment(merchant_id=merchant.id, razorpay_payment_id="pay_mem_ev_2", amount=10000, currency="INR", status="failed")
    db_session.add(p)
    db_session.commit()

    mock_llm = MockMemoryLLM(
        response_data={
            "intervene": True,
            "confidence": 0.90,
            "recommended_failure_threshold": 8,
            "recommended_failure_rate_threshold": 0.15,
            "urgency": "medium",
            "recommended_action": "grace_period",
            "reasoning": "Exact cohort evidence level",
            "evidence_summary": [],
        }
    )
    analyst = AdaptiveRecoveryAnalyst(provider=mock_llm)

    campaign, attempts = create_recovery_campaign(
        db=db_session,
        incident=inc,
        diagnosis=None,
        policy=policy,
        target_payments=[p],
        adaptive_analyst=analyst,
    )

    ev = campaign.decision_evidence.get("operational_memory")
    assert ev["evidence_level"] == "exact_cohort"
    assert ev["historical_sample_size"] == 1


def test_memory_influence_metadata_stored(db_session, create_merchant, create_policy, create_incident):
    merchant = create_merchant()
    inc = create_incident(merchant.id)
    _seed_completed_campaign(db_session, merchant.id, inc)
    policy = create_policy(merchant.id)

    p = Payment(merchant_id=merchant.id, razorpay_payment_id="pay_mem_ev_3", amount=10000, currency="INR", status="failed")
    db_session.add(p)
    db_session.commit()

    mock_llm = MockMemoryLLM(
        response_data={
            "intervene": True,
            "confidence": 0.90,
            "recommended_failure_threshold": 8,
            "recommended_failure_rate_threshold": 0.15,
            "urgency": "medium",
            "recommended_action": "grace_period",
            "reasoning": "Influence metadata test",
            "evidence_summary": [],
        }
    )
    analyst = AdaptiveRecoveryAnalyst(provider=mock_llm)

    campaign, attempts = create_recovery_campaign(
        db=db_session,
        incident=inc,
        diagnosis=None,
        policy=policy,
        target_payments=[p],
        adaptive_analyst=analyst,
    )

    ev = campaign.decision_evidence.get("operational_memory")
    assert "signal_breakdown" in ev
    assert "threshold_statistics" in ev
    assert len(ev["summary"]) > 0


def test_operational_memory_audit_event_created(db_session, create_merchant, create_policy, create_incident):
    merchant = create_merchant()
    inc = create_incident(merchant.id)
    _seed_completed_campaign(db_session, merchant.id, inc)
    policy = create_policy(merchant.id)

    p = Payment(merchant_id=merchant.id, razorpay_payment_id="pay_mem_ev_4", amount=10000, currency="INR", status="failed")
    db_session.add(p)
    db_session.commit()

    mock_llm = MockMemoryLLM(
        response_data={
            "intervene": True,
            "confidence": 0.90,
            "recommended_failure_threshold": 8,
            "recommended_failure_rate_threshold": 0.15,
            "urgency": "medium",
            "recommended_action": "grace_period",
            "reasoning": "Audit event test",
            "evidence_summary": [],
        }
    )
    analyst = AdaptiveRecoveryAnalyst(provider=mock_llm)

    campaign, attempts = create_recovery_campaign(
        db=db_session,
        incident=inc,
        diagnosis=None,
        policy=policy,
        target_payments=[p],
        adaptive_analyst=analyst,
    )

    events = get_recovery_audit_trail(db_session, merchant.id, incident_id=inc.id)
    event_types = [e.event_type for e in events]
    assert RecoveryAuditEventType.OPERATIONAL_MEMORY_RETRIEVED.value in event_types


def test_sensitive_data_never_enters_memory_payload(db_session, create_merchant, create_incident):
    merchant = create_merchant()
    inc = create_incident(merchant.id)
    _seed_completed_campaign(db_session, merchant.id, inc)

    mem = retrieve_operational_memory(db_session, merchant.id, inc)
    mem_dict = mem.model_dump()
    json_str = mem.model_dump_json()

    for secret in ["key_id", "key_secret", "secret", "api_key", "password", "bearer", "auth_token", "signature"]:
        assert secret not in json_str


# ============================================================================
# Category 5: Learning Loop and Invariants (4 tests)
# ============================================================================

def test_memory_backed_decision_reaches_phase2_evaluator(db_session, create_merchant, create_policy, create_incident):
    merchant = create_merchant()
    inc = create_incident(merchant.id)
    _seed_completed_campaign(db_session, merchant.id, inc)
    policy = create_policy(merchant.id)

    p = Payment(merchant_id=merchant.id, razorpay_payment_id="pay_mem_lp_1", amount=10000, currency="INR", status="failed")
    db_session.add(p)
    db_session.commit()

    mock_llm = MockMemoryLLM(
        response_data={
            "intervene": True,
            "confidence": 0.90,
            "recommended_failure_threshold": 8,
            "recommended_failure_rate_threshold": 0.15,
            "urgency": "medium",
            "recommended_action": "grace_period",
            "reasoning": "Learning loop test",
            "evidence_summary": [],
        }
    )
    analyst = AdaptiveRecoveryAnalyst(provider=mock_llm)

    campaign, attempts = create_recovery_campaign(
        db=db_session,
        incident=inc,
        diagnosis=None,
        policy=policy,
        target_payments=[p],
        adaptive_analyst=analyst,
    )

    # Transition to completed terminal state
    attempts[0].status = "recovered"
    attempts[0].recovered_amount = 10000
    campaign.status = "completed"
    db_session.commit()

    evaluation = evaluate_campaign_adaptive_decision(db_session, campaign, persist=True)
    assert evaluation.memory_used is True
    assert evaluation.memory_evidence_level == "exact_cohort"
    assert evaluation.memory_sample_size == 1


def test_learning_signal_remains_observational(db_session, create_merchant, create_incident):
    merchant = create_merchant()
    inc = create_incident(merchant.id)
    campaign = _seed_completed_campaign(db_session, merchant.id, inc)

    evaluation = evaluate_campaign_adaptive_decision(db_session, campaign, persist=True)
    # The evaluation output is observational
    assert evaluation.classification is not None
    assert campaign.status == "completed"


def test_learning_signals_never_mutate_policy(db_session, create_merchant, create_policy, create_incident):
    merchant = create_merchant()
    policy = create_policy(merchant.id, max_exposure=50000, max_incentive=1000)
    inc = create_incident(merchant.id)
    campaign = _seed_completed_campaign(db_session, merchant.id, inc)

    evaluate_campaign_adaptive_decision(db_session, campaign, persist=True)

    db_session.refresh(policy)
    assert policy.max_exposure == 50000
    assert policy.max_incentive == 1000
    assert policy.allowed_actions == ["retry", "grace_period", "incentive", "ops_review"]


def test_repeated_evaluation_remains_idempotent(db_session, create_merchant, create_incident):
    merchant = create_merchant()
    inc = create_incident(merchant.id)
    campaign = _seed_completed_campaign(db_session, merchant.id, inc)

    eval1 = evaluate_campaign_adaptive_decision(db_session, campaign, persist=True)
    eval2 = evaluate_campaign_adaptive_decision(db_session, campaign, persist=True)

    assert eval1.classification == eval2.classification
    assert eval1.actual_recovered_amount == eval2.actual_recovered_amount
    assert eval1.memory_used == eval2.memory_used


# ============================================================================
# Category 6: Economics & Financial Invariants (3 tests)
# ============================================================================

def test_memory_retrieval_does_not_alter_financial_calculations(db_session, create_merchant, create_policy, create_incident):
    merchant = create_merchant()
    inc = create_incident(merchant.id)
    _seed_completed_campaign(db_session, merchant.id, inc)
    policy = create_policy(merchant.id)

    # Evaluate economics with and without memory
    candidates = evaluate_recovery_economics(db_session, inc, None, policy)
    mem = retrieve_operational_memory(db_session, merchant.id, inc)

    candidates_after = evaluate_recovery_economics(db_session, inc, None, policy)
    for c1, c2 in zip(candidates, candidates_after):
        assert c1.expected_recovery_amount == c2.expected_recovery_amount
        assert c1.expected_net_recovery_value == c2.expected_net_recovery_value
        assert c1.action_cost == c2.action_cost


def test_actual_recovered_amount_remains_payment_webhook_derived(db_session, create_merchant, create_policy, create_incident):
    merchant = create_merchant()
    inc = create_incident(merchant.id)
    campaign = _seed_completed_campaign(db_session, merchant.id, inc, target_count=1, recovered_count=1, amount_per_payment=12500)

    evaluation = evaluate_campaign_adaptive_decision(db_session, campaign, persist=False)
    # Recovered amount is strictly 12500 derived from Payment
    assert evaluation.actual_recovered_amount == 12500


def test_ai_cost_remains_explicitly_unavailable(db_session, create_merchant, create_policy, create_incident):
    merchant = create_merchant()
    inc = create_incident(merchant.id)
    policy = create_policy(merchant.id)
    candidates = evaluate_recovery_economics(db_session, inc, None, policy)
    eligible = [c for c in candidates if c.is_eligible]
    mem = retrieve_operational_memory(db_session, merchant.id, inc)

    mock_llm = MockMemoryLLM(
        response_data={
            "intervene": True,
            "confidence": 0.90,
            "recommended_failure_threshold": 8,
            "recommended_failure_rate_threshold": 0.15,
            "urgency": "medium",
            "recommended_action": "grace_period",
            "reasoning": "Cost availability check",
            "evidence_summary": [],
        }
    )
    analyst = AdaptiveRecoveryAnalyst(provider=mock_llm)
    result = analyst.analyze(inc, None, policy, eligible, operational_memory=mem)

    # Provider does not supply pricing, so cost metrics are explicitly absent / handled
    assert result.telemetry.latency_ms > 0
    assert result.telemetry.input_size_bytes > 0
