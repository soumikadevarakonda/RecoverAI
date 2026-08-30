from datetime import datetime, timedelta, timezone
import pytest
from sqlalchemy import select

from app.db.session import SessionLocal
from app.domains.recovery.economics import evaluate_recovery_economics, DEFAULT_RECOVERY_RATES
from app.domains.recovery.service import decide_recovery_action
from app.models.merchant import Merchant
from app.models.incident import Incident
from app.models.payment import Payment
from app.models.payment_event import PaymentEvent
from app.models.webhook_event import WebhookEvent
from app.models.recovery_policy import RecoveryPolicy
from app.models.recovery_attempt import RecoveryAttempt
from app.models.diagnosis import Diagnosis


@pytest.fixture
def db_session():
    session = SessionLocal()
    try:
        session.query(RecoveryAttempt).delete()
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
def create_merchant(db_session):
    def _create(name: str = "Merchant"):
        merchant = Merchant(name=name)
        db_session.add(merchant)
        db_session.commit()
        db_session.refresh(merchant)
        return merchant
    return _create


@pytest.fixture
def create_incident_and_diagnosis(db_session):
    def _create(merchant_id, diagnosis_type="error-code spike", revenue_at_risk=10000):
        now = datetime.now(timezone.utc)
        incident = Incident(
            merchant_id=merchant_id,
            method="upi",
            bank="HDFC",
            error_code="GATEWAY_ERROR",
            error_step="payment_authorization",
            current_total_count=30,
            current_failed_count=10,
            current_failure_rate=0.3333,
            current_total_amount=30000,
            current_failed_amount=10000,
            baseline_total_count=30,
            baseline_failed_count=1,
            baseline_failure_rate=0.0333,
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

        diagnosis = Diagnosis(
            incident_id=incident.id,
            diagnosis_type=diagnosis_type,
            explanation="Test diagnosis",
            supporting_evidence={},
            confidence=0.8,
        )
        db_session.add(diagnosis)
        db_session.commit()
        db_session.refresh(diagnosis)

        return incident, diagnosis
    return _create


def test_candidate_evaluation(db_session, create_merchant, create_incident_and_diagnosis):
    merchant = create_merchant()
    incident, diagnosis = create_incident_and_diagnosis(merchant.id, "error-code spike", revenue_at_risk=10000)

    # Allow all automated actions
    policy = RecoveryPolicy(
        merchant_id=merchant.id,
        allowed_actions=["retry", "grace_period", "incentive"],
        max_incentive=1000,
        max_exposure=10000,
        approval_threshold=1000.0,
    )
    db_session.add(policy)
    db_session.commit()

    candidates = evaluate_recovery_economics(db_session, incident, diagnosis, policy)

    assert len(candidates) == 4
    candidates_dict = {c.action: c for c in candidates}

    # Verify retry
    c_retry = candidates_dict["retry"]
    assert c_retry.is_eligible is True
    assert c_retry.expected_recovery_rate == DEFAULT_RECOVERY_RATES["retry"]
    assert c_retry.expected_recovery_amount == int(10000 * DEFAULT_RECOVERY_RATES["retry"])
    assert c_retry.action_cost == 0
    assert c_retry.expected_net_recovery_value == c_retry.expected_recovery_amount

    # Verify grace_period
    c_gp = candidates_dict["grace_period"]
    assert c_gp.is_eligible is True
    assert c_gp.expected_recovery_rate == DEFAULT_RECOVERY_RATES["grace_period"]
    assert c_gp.expected_recovery_amount == int(10000 * DEFAULT_RECOVERY_RATES["grace_period"])
    assert c_gp.action_cost == 0
    assert c_gp.expected_net_recovery_value == c_gp.expected_recovery_amount


def test_incentive_cost_calculation(db_session, create_merchant, create_incident_and_diagnosis):
    merchant = create_merchant()
    incident, diagnosis = create_incident_and_diagnosis(merchant.id, "error-code spike", revenue_at_risk=10000)

    policy = RecoveryPolicy(
        merchant_id=merchant.id,
        allowed_actions=["incentive"],
        max_incentive=1000,
        max_exposure=10000,
        approval_threshold=1000.0,
    )
    db_session.add(policy)
    db_session.commit()

    candidates = evaluate_recovery_economics(db_session, incident, diagnosis, policy)
    candidates_dict = {c.action: c for c in candidates}

    c_inc = candidates_dict["incentive"]
    assert c_inc.is_eligible is True
    assert c_inc.action_cost == 500  # Default incentive cost
    assert c_inc.expected_net_recovery_value == c_inc.expected_recovery_amount - 500


def test_best_action_selection(db_session, create_merchant, create_incident_and_diagnosis):
    merchant = create_merchant()
    incident, diagnosis = create_incident_and_diagnosis(merchant.id, "error-code spike", revenue_at_risk=10000)

    policy = RecoveryPolicy(
        merchant_id=merchant.id,
        allowed_actions=["retry", "grace_period", "incentive"],
        max_incentive=1000,
        max_exposure=10000,
        approval_threshold=1000.0,
    )
    db_session.add(policy)
    db_session.commit()

    # With default rates:
    # retry: rate=0.30, cost=0 -> net value=3000
    # gp: rate=0.20, cost=0 -> net value=2000
    # incentive: rate=0.25, cost=500 -> net value=2000
    # Expected: retry has highest net value (3000) and is selected
    attempt = decide_recovery_action(db_session, incident, diagnosis, policy)
    assert attempt.selected_action == "retry"

    # Now let's override with custom rates where incentive has highest net value:
    # Custom rates: gp: 0.10 -> net=1000, retry: 0.10 -> net=1000, incentive: 0.80 -> net = 8000 - 500 = 7500
    custom_rates = {
        "retry": 0.10,
        "grace_period": 0.10,
        "incentive": 0.80,
        "ops_review": 0.05,
    }

    # Monkeypatch/mock the default rates or directly test through evaluation
    candidates = evaluate_recovery_economics(db_session, incident, diagnosis, policy, rates=custom_rates)
    candidates.sort(key=lambda x: x.expected_net_recovery_value if x.is_eligible else -999999, reverse=True)
    assert candidates[0].action == "incentive"


def test_policy_blocked_action(db_session, create_merchant, create_incident_and_diagnosis):
    merchant = create_merchant()
    incident, diagnosis = create_incident_and_diagnosis(merchant.id)

    # policy blocks retry (only gp allowed)
    policy = RecoveryPolicy(
        merchant_id=merchant.id,
        allowed_actions=["grace_period"],
    )
    db_session.add(policy)
    db_session.commit()

    candidates = evaluate_recovery_economics(db_session, incident, diagnosis, policy)
    candidates_dict = {c.action: c for c in candidates}

    assert candidates_dict["retry"].is_eligible is False
    assert candidates_dict["retry"].reason_ineligible == "Action not allowed by merchant policy"
    assert candidates_dict["retry"].expected_net_recovery_value == 0


def test_exposure_limit_blocks_action(db_session, create_merchant, create_incident_and_diagnosis):
    merchant = create_merchant()
    incident, diagnosis = create_incident_and_diagnosis(merchant.id)

    # Policy allows incentive, max_exposure is 400 (proposed incentive is 500)
    policy = RecoveryPolicy(
        merchant_id=merchant.id,
        allowed_actions=["incentive"],
        max_incentive=1000,
        max_exposure=400,
    )
    db_session.add(policy)
    db_session.commit()

    candidates = evaluate_recovery_economics(db_session, incident, diagnosis, policy)
    candidates_dict = {c.action: c for c in candidates}

    assert candidates_dict["incentive"].is_eligible is False
    assert "would exceed policy max_exposure" in candidates_dict["incentive"].reason_ineligible


def test_zero_revenue_at_risk(db_session, create_merchant, create_incident_and_diagnosis):
    merchant = create_merchant()
    incident, diagnosis = create_incident_and_diagnosis(merchant.id, revenue_at_risk=0)

    policy = RecoveryPolicy(
        merchant_id=merchant.id,
        allowed_actions=["retry", "grace_period", "incentive"],
        max_incentive=1000,
        max_exposure=10000,
        approval_threshold=1000.0,
    )
    db_session.add(policy)
    db_session.commit()

    candidates = evaluate_recovery_economics(db_session, incident, diagnosis, policy)
    candidates_dict = {c.action: c for c in candidates}

    # For zero revenue at risk, expected recovery is 0 for all
    for action, c in candidates_dict.items():
        assert c.expected_recovery_amount == 0
        if action == "incentive":
            assert c.expected_net_recovery_value == -500  # expected = 0, cost = 500
        else:
            assert c.expected_net_recovery_value == 0
