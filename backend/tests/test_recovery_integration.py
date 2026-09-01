from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
import httpx
import pytest
from sqlalchemy import select, delete

from app.db.session import SessionLocal
from app.models.merchant import Merchant
from app.models.payment import Payment
from app.models.incident import Incident
from app.models.recovery_attempt import RecoveryAttempt
from app.models.recovery_policy import RecoveryPolicy
from app.models.diagnosis import Diagnosis

from scripts.seed_trial_data import seed_trial_scenario
from app.domains.recovery.trial_evaluation import TRIAL_REFERENCE_TIME
from app.domains.cohorts.detector import detect_cohort_degradations
from app.domains.diagnosis.service import diagnose_incident
from app.domains.recovery.service import (
    decide_recovery_action,
    execute_recovery_attempt,
    process_recovery_webhook,
)
from app.domains.recovery.batch_measurement import calculate_batch_measurement
from app.integrations.llm.provider import LLMProvider
from app.integrations.razorpay import RazorpayClient


class E2EMockLLMProvider(LLMProvider):
    def __init__(self, response_data: dict | Exception):
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
        return response_model.model_validate(self.response_data)


@pytest.fixture
def db_session():
    session = SessionLocal()
    try:
        session.execute(delete(RecoveryAttempt))
        session.execute(delete(RecoveryPolicy))
        session.execute(delete(Diagnosis))
        session.execute(delete(Incident))
        session.execute(delete(Payment))
        session.execute(delete(Merchant))
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
def create_attempt(db_session):
    def _create(merchant_id, recovery_id="rec_test_123", payment_link_id="plink_expected_123", amount=50000, incentive=500):
        import uuid
        pay = Payment(
            merchant_id=merchant_id,
            razorpay_payment_id=f"pay_{uuid.uuid4().hex[:12]}",
            amount=amount,
            currency="INR",
            status="failed",
        )
        db_session.add(pay)
        db_session.commit()

        now = datetime.now(timezone.utc)
        inc = Incident(
            merchant_id=merchant_id,
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
            window_start=now - timedelta(minutes=30),
            window_end=now,
            baseline_start=now - timedelta(hours=2),
            baseline_end=now - timedelta(hours=1),
            status="detected",
        )
        db_session.add(inc)
        db_session.commit()

        attempt = RecoveryAttempt(
            recovery_id=recovery_id,
            merchant_id=merchant_id,
            incident_id=inc.id,
            payment_id=pay.id,
            selected_action="incentive",
            incentive_amount=incentive,
            status="executed",
            payment_link_id=payment_link_id,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        )
        db_session.add(attempt)
        db_session.commit()
        db_session.refresh(attempt)
        return attempt
    return _create


def test_end_to_end_recovery_pipeline(db_session):
    # 1. Seed trial payment data deterministically
    merchant = seed_trial_scenario(db_session, "E2E Merchant")
    policy = db_session.scalar(
        select(RecoveryPolicy).where(RecoveryPolicy.merchant_id == merchant.id)
    )

    # Align payment dimensions for HDFC UPI cohort to allow fractional degradation detection
    # and isolate bank-specific degradation (preventing error-code spike or step degradation from dominating)
    now = TRIAL_REFERENCE_TIME
    window_start = now - timedelta(minutes=30)
    window_end = now + timedelta(minutes=5)
    baseline_start = now - timedelta(hours=2)
    baseline_end = now - timedelta(hours=1)

    curr_payments = db_session.scalars(
        select(Payment)
        .where(
            Payment.merchant_id == merchant.id,
            Payment.created_at >= window_start,
            Payment.created_at <= window_end,
        )
        .order_by(Payment.created_at.asc())
    ).all()

    for idx, pay in enumerate(curr_payments):
        if pay.status == "failed":
            if idx < 4:
                pay.error_code = "GATEWAY_ERROR"
                pay.error_step = "payment_authorization"
            else:
                pay.error_code = "INSUFFICIENT_FUNDS"
                pay.error_step = "payment_authentication"
        else:
            pay.error_code = "GATEWAY_ERROR"
            pay.error_step = "payment_authorization"

    base_payments = db_session.scalars(
        select(Payment)
        .where(
            Payment.merchant_id == merchant.id,
            Payment.created_at >= baseline_start,
            Payment.created_at <= baseline_end,
        )
        .order_by(Payment.created_at.asc())
    ).all()

    for idx, pay in enumerate(base_payments):
        if pay.status == "failed":
            pay.error_code = "GATEWAY_ERROR"
            pay.error_step = "payment_authorization"
        else:
            pay.error_code = "GATEWAY_ERROR"
            pay.error_step = "payment_authorization"

    db_session.commit()

    now = TRIAL_REFERENCE_TIME
    window_start = now - timedelta(minutes=30)
    window_end = now + timedelta(minutes=5)
    baseline_start = now - timedelta(hours=2)
    baseline_end = now - timedelta(hours=1)

    # 2. Run cohort degradation detection
    incidents = detect_cohort_degradations(
        db=db_session,
        merchant_id=merchant.id,
        window_start=window_start,
        window_end=window_end,
        baseline_start=baseline_start,
        baseline_end=baseline_end,
    )
    assert len(incidents) > 0, "No degraded cohort detected"
    incident = incidents[0]
    assert incident.method == "upi"
    assert incident.bank == "HDFC"

    # 3. Produce Incident Diagnosis
    diagnosis = diagnose_incident(db_session, incident)
    assert diagnosis is not None
    assert diagnosis.diagnosis_type == "bank-specific degradation"

    # 4. Invoke AI Recovery Strategist (via Mocked LLM Provider)
    # The strategist should recommend grace_period since retry is disabled for bank-specific degradation
    mock_llm_response = {
        "recommended_action": "grace_period",
        "concise_reason": "Strategist recommends grace_period based on high historical recovery rates",
        "evidence_level": "exact_cohort",
        "sample_size": 5,
        "observed_recovery_rate": 0.6,
        "expected_net_recovery_value": 6000,
        "confidence": 0.95,
    }
    llm_provider = E2EMockLLMProvider(response_data=mock_llm_response)

    # 5. Run decide_recovery_action
    attempt = decide_recovery_action(
        db=db_session,
        incident=incident,
        diagnosis=diagnosis,
        policy=policy,
        llm_provider=llm_provider,
    )
    attempt.created_at = TRIAL_REFERENCE_TIME + timedelta(minutes=5)
    db_session.commit()
    assert attempt is not None
    assert attempt.selected_action == "grace_period"
    assert attempt.decision_evidence["ai_strategist_used"] is True
    assert attempt.decision_evidence["concise_decision_reason"] == "Strategist recommends grace_period based on high historical recovery rates"

    # 6. Execute recovery attempt (Mocked Razorpay Payment Link creation)
    mock_razorpay = MagicMock(spec=RazorpayClient)
    mock_razorpay.create_payment_link.return_value = {
        "id": "plink_e2e_789",
        "short_url": "https://rzp.io/i/e2e_789",
        "status": "created",
    }

    # Associate attempt with a payment so expected_recovery_amount calculation works correctly
    payment = Payment(
        merchant_id=merchant.id,
        razorpay_payment_id="pay_e2e_failed_999",
        amount=10000,
        currency="INR",
        status="failed",
    )
    db_session.add(payment)
    db_session.commit()
    attempt.payment_id = payment.id
    db_session.commit()

    executed_attempt = execute_recovery_attempt(
        db=db_session,
        attempt=attempt,
        client=mock_razorpay,
    )
    assert executed_attempt.status == "executed"
    assert executed_attempt.payment_link_id == "plink_e2e_789"
    mock_razorpay.create_payment_link.assert_called_once()

    # 7. Simulate payment_link.paid webhook event
    payload = {
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": "plink_e2e_789",
                    "reference_id": attempt.recovery_id,
                    "status": "paid",
                    "amount_paid": 10000,
                }
            },
            "payment": {
                "entity": {
                    "id": "pay_e2e_recovered_999",
                    "amount": 10000,
                    "status": "captured",
                }
            }
        }
    }

    process_recovery_webhook(db_session, payload)
    db_session.commit()

    # 8. Verify recovery status and amount
    db_session.refresh(attempt)
    assert attempt.status == "recovered"
    assert attempt.resulting_payment_id == "pay_e2e_recovered_999"
    assert attempt.recovered_amount == 10000

    # 9. Verify batch measurement reports the recovery
    batch_start = now - timedelta(hours=3)
    batch_end = now + timedelta(hours=1)
    metrics = calculate_batch_measurement(
        db=db_session,
        merchant_id=merchant.id,
        start_time=batch_start,
        end_time=batch_end,
    )

    assert metrics.recovered_attempts >= 1
    assert metrics.actual_recovered_amount >= 10000


def test_failure_path_razorpay_execution_failure(db_session, create_merchant, create_attempt):
    m = create_merchant()
    attempt = create_attempt(m.id, payment_link_id=None)
    attempt.status = "approved"
    db_session.commit()

    mock_client = MagicMock(spec=RazorpayClient)
    mock_client.create_payment_link.side_effect = httpx.HTTPStatusError(
        "API error",
        request=httpx.Request("POST", "http://test"),
        response=httpx.Response(500, text="Internal Server Error"),
    )

    with pytest.raises(httpx.HTTPStatusError):
        execute_recovery_attempt(db_session, attempt, client=mock_client)

    db_session.refresh(attempt)
    assert attempt.status == "approved"
    assert attempt.payment_link_id is None


def test_failure_path_mismatched_payment_link_webhook(db_session, create_merchant, create_attempt):
    m = create_merchant()
    attempt = create_attempt(m.id, payment_link_id="plink_expected")

    payload = {
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": "plink_wrong",  # Wrong ID
                    "reference_id": attempt.recovery_id,
                    "status": "paid",
                }
            },
            "payment": {
                "entity": {
                    "id": "pay_xyz",
                    "amount": 49500,
                    "status": "captured",
                }
            }
        }
    }

    with pytest.raises(ValueError, match="does not match expected attempt link ID"):
        process_recovery_webhook(db_session, payload)


def test_failure_path_duplicate_recovery_webhook(db_session, create_merchant, create_attempt):
    m = create_merchant()
    attempt = create_attempt(m.id, payment_link_id="plink_expected")
    attempt.status = "recovered"
    attempt.recovered_amount = 49500
    db_session.commit()

    payload = {
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": "plink_expected",
                    "reference_id": attempt.recovery_id,
                    "status": "paid",
                }
            },
            "payment": {
                "entity": {
                    "id": "pay_xyz",
                    "amount": 1000,  # Different amount
                    "status": "captured",
                }
            }
        }
    }

    # Should exit early silently without changing the recovered amount or erroring
    process_recovery_webhook(db_session, payload)
    db_session.commit()

    db_session.refresh(attempt)
    assert attempt.status == "recovered"
    assert attempt.recovered_amount == 49500


def test_failure_path_strategist_failure_with_fallback(db_session, create_merchant):
    m = create_merchant()
    # Create policy
    policy = RecoveryPolicy(
        merchant_id=m.id,
        allowed_actions=["retry", "grace_period", "incentive", "ops_review"],
        max_incentive=1000,
        max_exposure=20000,
        approval_threshold=3000.0,
    )
    db_session.add(policy)

    # Incident UPI + SBI
    now = datetime.now(timezone.utc)
    incident = Incident(
        merchant_id=m.id,
        method="upi",
        bank="SBI",
        error_code="GATEWAY_ERROR",
        error_step="payment_authorization",
        current_total_count=30,
        current_failed_count=10,
        current_failure_rate=0.33,
        current_total_amount=300000,
        current_failed_amount=100000,
        baseline_total_count=30,
        baseline_failed_count=1,
        baseline_failure_rate=0.03,
        baseline_total_amount=300000,
        baseline_failed_amount=10000,
        absolute_rate_increase=0.3,
        relative_degradation=10.0,
        revenue_at_risk=100000,
        window_start=now - timedelta(minutes=30),
        window_end=now,
        baseline_start=now - timedelta(hours=2),
        baseline_end=now - timedelta(hours=1),
        status="detected",
    )
    db_session.add(incident)
    db_session.commit()

    diagnosis = Diagnosis(
        incident_id=incident.id,
        diagnosis_type="bank-specific degradation",
        explanation="SBI UPI gateway latency spike",
        supporting_evidence={},
        confidence=0.85,
    )
    db_session.add(diagnosis)
    db_session.commit()

    bad_provider = E2EMockLLMProvider(response_data=Exception("LLM Timeout"))

    # Should fall back to deterministic choice (grace_period)
    attempt = decide_recovery_action(
        db=db_session,
        incident=incident,
        diagnosis=diagnosis,
        policy=policy,
        llm_provider=bad_provider,
    )

    assert attempt is not None
    assert attempt.selected_action == "incentive"
    assert attempt.decision_evidence["ai_strategist_used"] is False
    assert "Deterministic fallback" in attempt.decision_evidence["concise_decision_reason"]


def test_failure_path_policy_blocked_recovery(db_session, create_merchant):
    m = create_merchant()
    # Policy only allows ops_review
    policy = RecoveryPolicy(
        merchant_id=m.id,
        allowed_actions=["ops_review"],
        max_incentive=1000,
        max_exposure=20000,
        approval_threshold=3000.0,
    )
    db_session.add(policy)

    now = datetime.now(timezone.utc)
    incident = Incident(
        merchant_id=m.id,
        method="upi",
        bank="SBI",
        error_code="GATEWAY_ERROR",
        error_step="payment_authorization",
        current_total_count=30,
        current_failed_count=10,
        current_failure_rate=0.33,
        current_total_amount=300000,
        current_failed_amount=100000,
        baseline_total_count=30,
        baseline_failed_count=1,
        baseline_failure_rate=0.03,
        baseline_total_amount=300000,
        baseline_failed_amount=10000,
        absolute_rate_increase=0.3,
        relative_degradation=10.0,
        revenue_at_risk=100000,
        window_start=now - timedelta(minutes=30),
        window_end=now,
        baseline_start=now - timedelta(hours=2),
        baseline_end=now - timedelta(hours=1),
        status="detected",
    )
    db_session.add(incident)
    db_session.commit()

    diagnosis = Diagnosis(
        incident_id=incident.id,
        diagnosis_type="bank-specific degradation",
        explanation="SBI UPI gateway latency spike",
        supporting_evidence={},
        confidence=0.85,
    )
    db_session.add(diagnosis)
    db_session.commit()

    attempt = decide_recovery_action(
        db=db_session,
        incident=incident,
        diagnosis=diagnosis,
        policy=policy,
        llm_provider=None,
    )

    # Since all automated actions are blocked by policy, it falls back to ops_review
    assert attempt is not None
    assert attempt.selected_action == "ops_review"
    assert attempt.status == "pending"
