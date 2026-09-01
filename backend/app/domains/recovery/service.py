from datetime import datetime, timedelta, timezone
import uuid
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.incident import Incident
from app.models.diagnosis import Diagnosis
from app.models.recovery_policy import RecoveryPolicy
from app.models.recovery_attempt import RecoveryAttempt


from app.integrations.llm.provider import LLMProvider

def decide_recovery_action(
    db: Session,
    incident: Incident,
    diagnosis: Diagnosis,
    policy: RecoveryPolicy,
    llm_provider: LLMProvider | None = None,
) -> RecoveryAttempt:
    from app.domains.recovery.economics import evaluate_recovery_economics

    candidates = evaluate_recovery_economics(db, incident, diagnosis, policy)
    eligible_candidates = [c for c in candidates if c.is_eligible]

    priority = {"retry": 4, "grace_period": 3, "incentive": 2, "ops_review": 1}
    eligible_candidates.sort(
        key=lambda x: (x.expected_net_recovery_value, priority[x.action]),
        reverse=True,
    )

    selected_action = None
    incentive_amount = 0
    ai_strategist_used = False
    reason = None

    if llm_provider is not None and eligible_candidates:
        from app.domains.recovery.strategist import RecoveryStrategist
        from app.domains.recovery.outcomes import calculate_recovery_performance

        try:
            historical_evidence = {}
            for c in eligible_candidates:
                perf = calculate_recovery_performance(
                    db=db,
                    merchant_id=incident.merchant_id,
                    method=incident.method,
                    bank=incident.bank,
                    error_code=incident.error_code,
                    selected_action=c.action,
                    min_attempts=1,
                )
                historical_evidence[c.action] = {
                    "evidence_level": perf.evidence_level,
                    "sample_size": perf.total_attempts,
                    "observed_recovery_rate": perf.observed_recovery_rate,
                }

            strategist = RecoveryStrategist(provider=llm_provider)
            rec = strategist.recommend_action(
                incident=incident,
                diagnosis=diagnosis,
                policy=policy,
                eligible_candidates=eligible_candidates,
                historical_evidence=historical_evidence,
            )

            # Find matching eligible candidate
            matched = next((c for c in eligible_candidates if c.action == rec.recommended_action), None)
            if matched:
                selected_action = matched.action
                incentive_amount = matched.action_cost
                ai_strategist_used = True
                reason = rec.concise_reason
        except Exception as e:
            # Fall back to deterministic behavior silently or log it
            pass

    if selected_action is None:
        if eligible_candidates:
            best_candidate = eligible_candidates[0]
            selected_action = best_candidate.action
            incentive_amount = best_candidate.action_cost
            reason = f"Deterministic fallback selected {selected_action} based on highest expected net recovery value"
        else:
            selected_action = "ops_review"
            incentive_amount = 0
            reason = "Fallback selected ops_review as no candidates were eligible"

    # Determine status based on policy rules
    if selected_action == "incentive":
        if incentive_amount > policy.approval_threshold:
            status = "pending"
        else:
            status = "approved"
    elif selected_action == "ops_review":
        incentive_amount = 0
        status = "pending"
    else:
        incentive_amount = 0
        status = "approved"

    # Build decision evidence
    from app.domains.recovery.evidence import build_decision_evidence
    evidence = build_decision_evidence(
        db=db,
        incident=incident,
        diagnosis=diagnosis,
        policy=policy,
        selected_action=selected_action,
        ai_strategist_used=ai_strategist_used,
        reason=reason,
    )

    attempt = RecoveryAttempt(
        recovery_id=f"rec_{uuid.uuid4().hex[:12]}",
        merchant_id=incident.merchant_id,
        incident_id=incident.id,
        payment_id=None,
        selected_action=selected_action,
        incentive_amount=incentive_amount,
        status=status,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        recovered_amount=0,
        decision_evidence=evidence,
    )
    db.add(attempt)
    db.commit()
    db.refresh(attempt)
    return attempt


def execute_recovery_attempt(
    db: Session,
    attempt: RecoveryAttempt,
    client: RazorpayClient = None,
) -> RecoveryAttempt:
    from app.integrations.razorpay import RazorpayClient

    if attempt.status != "approved":
        raise ValueError("Only approved attempts can be executed")

    if client is None:
        client = RazorpayClient()

    original_amount = attempt.payment.amount if attempt.payment else 1000
    charge_amount = max(0, original_amount - attempt.incentive_amount)

    expire_by = int(attempt.expires_at.timestamp()) if attempt.expires_at else None

    link_response = client.create_payment_link(
        amount=charge_amount,
        reference_id=attempt.recovery_id,
        description=f"Payment recovery link for {attempt.recovery_id}",
        expire_by=expire_by,
    )

    attempt.payment_link_id = link_response["id"]
    attempt.short_url = link_response["short_url"]
    attempt.status = "executed"

    db.add(attempt)
    db.commit()
    db.refresh(attempt)
    return attempt


def process_recovery_webhook(db: Session, payload: dict) -> None:
    plink_entity = payload.get("payload", {}).get("payment_link", {}).get("entity", {})
    payment_entity = payload.get("payload", {}).get("payment", {}).get("entity", {})

    recovery_id = plink_entity.get("reference_id") or plink_entity.get("notes", {}).get("recovery_id")
    if not recovery_id:
        raise ValueError("Missing recovery_id or reference_id in payment link webhook")

    attempt = db.scalar(
        select(RecoveryAttempt)
        .where(RecoveryAttempt.recovery_id == recovery_id)
        .with_for_update()
    )
    if not attempt:
        raise ValueError(f"No RecoveryAttempt found for recovery_id: {recovery_id}")

    # Invariant 7: Idempotency check for duplicate deliveries
    if attempt.status == "recovered":
        return

    # Invariant 1: Stored attempt must have a matching payment link ID
    if not attempt.payment_link_id:
        raise ValueError("RecoveryAttempt does not have a matching payment link stored")

    # Invariant 2 & 9: Incoming event payment link ID must match the stored payment link ID
    payment_link_id = plink_entity.get("id")
    if not payment_link_id:
        raise ValueError("Event payload is missing payment link ID")
    if attempt.payment_link_id != payment_link_id:
        raise ValueError(
            f"Payment link ID {payment_link_id} does not match expected attempt link ID {attempt.payment_link_id}"
        )

    # Invariant 4: Payment link status must be 'paid'
    plink_status = plink_entity.get("status")
    if plink_status != "paid":
        raise ValueError("Payment link is not in a paid state")

    # Invariant 5: Resulting payment status must be successful ('captured' or 'authorized')
    payment_status = payment_entity.get("status")
    if payment_status not in ["captured", "authorized"]:
        raise ValueError("Razorpay payment does not have a successful status")

    # Invariant 6: Recovered amount must come from verified payment data and must not exceed expected recovery amount
    recovered_amount = payment_entity.get("amount") or plink_entity.get("amount_paid") or 0
    if recovered_amount <= 0:
        raise ValueError("Recovered amount must be greater than zero")

    original_amount = attempt.payment.amount if attempt.payment else 1000
    expected_recovery_amount = max(0, original_amount - attempt.incentive_amount)
    if recovered_amount > expected_recovery_amount:
        raise ValueError(
            f"Recovered amount {recovered_amount} exceeds expected recovery amount {expected_recovery_amount}"
        )

    attempt.status = "recovered"
    attempt.resulting_payment_id = payment_entity.get("id")
    attempt.recovered_amount = recovered_amount

    db.add(attempt)
    db.flush()


def orchestrate_recovery(
    db: Session,
    incident: Incident,
    client: RazorpayClient = None,
) -> RecoveryAttempt:
    from app.domains.diagnosis.service import diagnose_incident
    from app.models.recovery_policy import RecoveryPolicy
    from app.integrations.razorpay import RazorpayClient

    # 1. Idempotency check: do not execute recovery if the incident has already been handled
    existing_attempt = db.scalar(
        select(RecoveryAttempt).where(RecoveryAttempt.incident_id == incident.id)
    )
    if existing_attempt:
        return existing_attempt

    # 2. Run Diagnosis
    diagnosis = diagnose_incident(db, incident)

    # 3. Load merchant RecoveryPolicy
    policy = db.scalar(
        select(RecoveryPolicy).where(RecoveryPolicy.merchant_id == incident.merchant_id)
    )
    if not policy:
        policy = RecoveryPolicy(
            merchant_id=incident.merchant_id,
            allowed_actions=[],
            max_incentive=0,
            max_exposure=0,
            approval_threshold=0.0,
        )

    # 4. Make recovery decision / create RecoveryAttempt
    attempt = decide_recovery_action(db, incident, diagnosis, policy)

    # 5. If approved for automatic execution, execute it
    if attempt.status == "approved" and attempt.selected_action != "ops_review":
        attempt = execute_recovery_attempt(db, attempt, client)

    return attempt
