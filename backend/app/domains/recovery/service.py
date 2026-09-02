from datetime import datetime, timedelta, timezone
from typing import Any
import uuid
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.incident import Incident
from app.models.diagnosis import Diagnosis
from app.models.payment import Payment
from app.models.recovery_policy import RecoveryPolicy
from app.models.recovery_attempt import RecoveryAttempt
from app.models.recovery_campaign import RecoveryCampaign
from app.integrations.llm.provider import LLMProvider


class NoTargetPaymentsAvailableError(ValueError):
    """Raised when an incident has zero eligible failed payments to target for recovery."""
    pass


def create_recovery_campaign(
    db: Session,
    incident: Incident,
    diagnosis: Diagnosis,
    policy: RecoveryPolicy,
    llm_provider: LLMProvider | None = None,
    target_payments: list[Payment] | None = None,
    adaptive_analyst: Any | None = None,
) -> tuple[RecoveryCampaign, list[RecoveryAttempt]]:
    """
    Creates a RecoveryCampaign for an Incident and spawns individual RecoveryAttempts
    for each target failed payment.
    """
    from app.domains.recovery.economics import evaluate_recovery_economics

    # 1. Resolve target failed payments if not explicitly provided
    if target_payments is None:
        stmt = (
            select(Payment)
            .where(
                Payment.merchant_id == incident.merchant_id,
                Payment.status == "failed",
                Payment.created_at >= incident.window_start,
                Payment.created_at <= incident.window_end,
            )
        )
        if incident.method and incident.method != "UNKNOWN":
            stmt = stmt.where(Payment.method == incident.method)
        if incident.bank and incident.bank != "UNKNOWN":
            stmt = stmt.where(Payment.bank == incident.bank)
        if incident.error_code and incident.error_code != "NONE":
            stmt = stmt.where(Payment.error_code == incident.error_code)
        if incident.error_step and incident.error_step != "NONE":
            stmt = stmt.where(Payment.error_step == incident.error_step)

        active_payment_ids = select(RecoveryAttempt.payment_id).where(
            RecoveryAttempt.payment_id.is_not(None),
            RecoveryAttempt.status.in_(["pending", "approved", "executing", "executed", "recovered"])
        )
        stmt = (
            stmt.where(Payment.id.not_in(active_payment_ids))
            .with_for_update(skip_locked=True)
            .order_by(Payment.created_at.asc())
        )
        target_payments = list(db.scalars(stmt).all())
    else:
        # Validate that all explicit payments belong to the incident merchant
        for p in target_payments:
            if p.merchant_id != incident.merchant_id:
                raise ValueError(f"Target payment {p.id} does not belong to merchant {incident.merchant_id}")

    # Explicit enforcement: never execute or create a campaign on zero actual failed payments
    if not target_payments:
        raise NoTargetPaymentsAvailableError(
            f"NO_TARGET_PAYMENTS_AVAILABLE: Incident {incident.id} has zero eligible failed payments to target."
        )

    # 2. Evaluate recovery candidate economics
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
    guardrail_eval = None
    adaptive_result = None

    from app.domains.recovery.guardrails import (
        evaluate_campaign_guardrails,
        evaluate_execution_guardrails,
        GuardrailViolationError,
    )
    from app.domains.recovery.audit import (
        record_audit_event,
        RecoveryAuditEventType,
    )

    # Record strategy evaluation audit event
    record_audit_event(
        db=db,
        merchant_id=incident.merchant_id,
        incident_id=incident.id,
        event_type=RecoveryAuditEventType.STRATEGY_EVALUATED,
        actor_type="system",
        actor_id="economics_engine",
        reason_code="CANDIDATES_EVALUATED",
        explanation=f"Evaluated {len(candidates)} recovery candidate actions under current policy.",
        evidence={
            "candidates": [
                {
                    "action": c.action,
                    "expected_recovery_rate": c.expected_recovery_rate,
                    "expected_net_recovery_value": c.expected_net_recovery_value,
                    "is_eligible": c.is_eligible,
                }
                for c in candidates
            ]
        },
    )

    # 2a. Priority: Adaptive Recovery Analyst (Phase 1 & 3)
    operational_memory = None
    if adaptive_analyst is not None and eligible_candidates:
        from app.domains.recovery.outcomes import calculate_recovery_performance
        from app.domains.recovery.memory import retrieve_operational_memory
        try:
            operational_memory = retrieve_operational_memory(
                db=db,
                merchant_id=incident.merchant_id,
                incident=incident,
            )
            record_audit_event(
                db=db,
                merchant_id=incident.merchant_id,
                incident_id=incident.id,
                event_type=RecoveryAuditEventType.OPERATIONAL_MEMORY_RETRIEVED,
                actor_type="system",
                actor_id="operational_memory",
                reason_code=operational_memory.evidence_level.value,
                explanation=operational_memory.summary,
                evidence={
                    "evidence_level": operational_memory.evidence_level.value,
                    "historical_sample_size": operational_memory.historical_sample_size,
                    "successful_decisions": operational_memory.signal_breakdown.successful_decisions,
                },
            )

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

            adaptive_result = adaptive_analyst.analyze(
                incident=incident,
                diagnosis=diagnosis,
                policy=policy,
                eligible_candidates=eligible_candidates,
                historical_evidence=historical_evidence,
                operational_memory=operational_memory,
            )

            if adaptive_result.is_accepted and adaptive_result.recommendation and adaptive_result.recommendation.intervene:
                rec = adaptive_result.recommendation
                record_audit_event(
                    db=db,
                    merchant_id=incident.merchant_id,
                    incident_id=incident.id,
                    event_type=RecoveryAuditEventType.ADAPTIVE_ANALYSIS_COMPLETED,
                    actor_type="ai",
                    actor_id=adaptive_result.telemetry.provider,
                    reason_code="ADAPTIVE_RECOMMENDATION_ACCEPTED",
                    explanation=rec.reasoning,
                    evidence={
                        "intervene": rec.intervene,
                        "confidence": rec.confidence,
                        "recommended_action": rec.recommended_action,
                        "recommended_failure_threshold": rec.recommended_failure_threshold,
                        "recommended_failure_rate_threshold": rec.recommended_failure_rate_threshold,
                        "urgency": rec.urgency,
                        "recommended_method": rec.recommended_method,
                        "recommended_bank": rec.recommended_bank,
                        "telemetry": adaptive_result.telemetry.model_dump(),
                    },
                )

                matched = next((c for c in eligible_candidates if c.action == rec.recommended_action), None)
                if matched:
                    ai_action = matched.action
                    ai_incentive = matched.action_cost if ai_action == "incentive" else 0

                    ai_guardrail = evaluate_campaign_guardrails(
                        db=db,
                        incident=incident,
                        policy=policy,
                        proposed_action=ai_action,
                        per_attempt_incentive=ai_incentive,
                        target_payments=target_payments,
                    )

                    if ai_guardrail.decision != "blocked":
                        selected_action = ai_action
                        incentive_amount = ai_incentive
                        ai_strategist_used = True
                        reason = rec.reasoning
                        guardrail_eval = ai_guardrail
                    else:
                        record_audit_event(
                            db=db,
                            merchant_id=incident.merchant_id,
                            incident_id=incident.id,
                            event_type=RecoveryAuditEventType.AI_FALLBACK,
                            actor_type="system",
                            actor_id="guardrail_engine",
                            reason_code=ai_guardrail.reason_code,
                            explanation=f"Adaptive AI action '{ai_action}' was vetoed by guardrails ({ai_guardrail.reason_code}); falling back to deterministic candidate.",
                            evidence={"vetoed_action": ai_action},
                        )
            else:
                rejection_reason = (adaptive_result.rejection_reason or "INTERVENTION_NOT_RECOMMENDED")[:64]
                rejection_expl = adaptive_result.rejection_explanation or f"Adaptive analysis was rejected ({rejection_reason}); using deterministic candidate."
                record_audit_event(
                    db=db,
                    merchant_id=incident.merchant_id,
                    incident_id=incident.id,
                    event_type=RecoveryAuditEventType.ADAPTIVE_ANALYSIS_REJECTED,
                    actor_type="system",
                    actor_id="adaptive_analyst",
                    reason_code=rejection_reason,
                    explanation=rejection_expl,
                    evidence={
                        "fallback_action": adaptive_result.fallback_action,
                        "telemetry": adaptive_result.telemetry.model_dump(),
                    },
                )
                record_audit_event(
                    db=db,
                    merchant_id=incident.merchant_id,
                    incident_id=incident.id,
                    event_type=RecoveryAuditEventType.AI_FALLBACK,
                    actor_type="system",
                    actor_id="adaptive_analyst",
                    reason_code=rejection_reason,
                    explanation="Adaptive analysis fell back to deterministic candidate.",
                    evidence={"fallback_action": adaptive_result.fallback_action},
                )
        except Exception as exc:
            record_audit_event(
                db=db,
                merchant_id=incident.merchant_id,
                incident_id=incident.id,
                event_type=RecoveryAuditEventType.AI_FALLBACK,
                actor_type="system",
                actor_id="adaptive_analyst",
                reason_code="AI_PROVIDER_ERROR",
                explanation="Adaptive analyst invocation failed. Falling back to deterministic candidate.",
                evidence={"error_type": exc.__class__.__name__},
            )

    # 2b. Legacy Recovery Strategist flow
    elif llm_provider is not None and eligible_candidates:
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

            # Record safe AI recommendation event
            record_audit_event(
                db=db,
                merchant_id=incident.merchant_id,
                incident_id=incident.id,
                event_type=RecoveryAuditEventType.AI_RECOMMENDATION,
                actor_type="ai",
                actor_id=llm_provider.__class__.__name__,
                reason_code="AI_RECOMMENDED",
                explanation=rec.concise_reason,
                evidence={
                    "recommended_action": rec.recommended_action,
                    "confidence": rec.confidence,
                    "evidence_level": rec.evidence_level,
                    "sample_size": rec.sample_size,
                    "observed_recovery_rate": rec.observed_recovery_rate,
                },
            )

            matched = next((c for c in eligible_candidates if c.action == rec.recommended_action), None)
            if matched:
                ai_action = matched.action
                ai_incentive = matched.action_cost if ai_action == "incentive" else 0
                
                # Independently evaluate AI recommendation with Guardrail Engine
                ai_guardrail = evaluate_campaign_guardrails(
                    db=db,
                    incident=incident,
                    policy=policy,
                    proposed_action=ai_action,
                    per_attempt_incentive=ai_incentive,
                    target_payments=target_payments,
                )
                
                if ai_guardrail.decision != "blocked":
                    selected_action = ai_action
                    incentive_amount = ai_incentive
                    ai_strategist_used = True
                    reason = rec.concise_reason
                    guardrail_eval = ai_guardrail
                else:
                    record_audit_event(
                        db=db,
                        merchant_id=incident.merchant_id,
                        incident_id=incident.id,
                        event_type=RecoveryAuditEventType.AI_FALLBACK,
                        actor_type="system",
                        actor_id="guardrail_engine",
                        reason_code=ai_guardrail.reason_code,
                        explanation=f"AI recommendation '{ai_action}' was vetoed by guardrails ({ai_guardrail.reason_code}); falling back to deterministic candidate.",
                        evidence={"vetoed_action": ai_action},
                    )
        except Exception as exc:
            record_audit_event(
                db=db,
                merchant_id=incident.merchant_id,
                incident_id=incident.id,
                event_type=RecoveryAuditEventType.AI_FALLBACK,
                actor_type="system",
                actor_id="strategist_engine",
                reason_code="AI_PROVIDER_ERROR",
                explanation="AI strategist provider failed. Falling back to deterministic candidate.",
                evidence={"error_type": exc.__class__.__name__},
            )

    # If AI was not used, failed, or was blocked by guardrails: evaluate deterministic candidates
    if selected_action is None:
        for candidate in eligible_candidates:
            cand_incentive = candidate.action_cost if candidate.action == "incentive" else 0
            cand_guardrail = evaluate_campaign_guardrails(
                db=db,
                incident=incident,
                policy=policy,
                proposed_action=candidate.action,
                per_attempt_incentive=cand_incentive,
                target_payments=target_payments,
            )
            if cand_guardrail.decision != "blocked":
                selected_action = candidate.action
                incentive_amount = cand_incentive
                guardrail_eval = cand_guardrail
                reason = f"Deterministic fallback selected {selected_action} based on highest expected net recovery value"
                break

    # If all candidates blocked by guardrails, fall back to ops_review
    if selected_action is None:
        selected_action = "ops_review"
        incentive_amount = 0
        reason = "Fallback selected ops_review as no candidates were permitted by guardrails"
        guardrail_eval = evaluate_campaign_guardrails(
            db=db,
            incident=incident,
            policy=policy,
            proposed_action="ops_review",
            per_attempt_incentive=0,
            target_payments=target_payments,
        )

    # Record guardrail evaluation events
    record_audit_event(
        db=db,
        merchant_id=incident.merchant_id,
        incident_id=incident.id,
        event_type=RecoveryAuditEventType.GUARDRAIL_EVALUATED,
        actor_type="system",
        actor_id="guardrail_engine",
        reason_code=guardrail_eval.reason_code,
        explanation=guardrail_eval.explanation,
        evidence={
            "decision": guardrail_eval.decision,
            "checks": [c.model_dump() for c in guardrail_eval.checks],
            "evaluated_exposure": guardrail_eval.evaluated_exposure,
        },
    )

    if guardrail_eval.decision == "allowed":
        guardrail_event_type = RecoveryAuditEventType.GUARDRAIL_APPROVED
    elif guardrail_eval.decision == "requires_approval":
        guardrail_event_type = RecoveryAuditEventType.GUARDRAIL_APPROVAL_REQUIRED
    else:
        guardrail_event_type = RecoveryAuditEventType.GUARDRAIL_BLOCKED

    record_audit_event(
        db=db,
        merchant_id=incident.merchant_id,
        incident_id=incident.id,
        event_type=guardrail_event_type,
        actor_type="system",
        actor_id="guardrail_engine",
        reason_code=guardrail_eval.reason_code,
        explanation=guardrail_eval.explanation,
        evidence=guardrail_eval.evaluated_exposure,
    )

    # 3. Determine status from guardrail decision
    campaign_status = "approved" if guardrail_eval.decision == "allowed" else "pending"

    target_count = len(target_payments)
    total_revenue_at_risk = sum(p.amount for p in target_payments)
    per_attempt_incentive = incentive_amount if selected_action == "incentive" else 0
    total_incentive_cost = per_attempt_incentive * target_count

    # 4. Build decision evidence
    from app.domains.recovery.evidence import build_decision_evidence
    evidence = build_decision_evidence(
        db=db,
        incident=incident,
        diagnosis=diagnosis,
        policy=policy,
        selected_action=selected_action,
        ai_strategist_used=ai_strategist_used,
        reason=reason,
        guardrail_result=guardrail_eval,
        adaptive_result=adaptive_result,
        operational_memory=operational_memory,
    )

    # 5. Create RecoveryCampaign
    campaign = RecoveryCampaign(
        campaign_id=f"camp_{uuid.uuid4().hex[:12]}",
        merchant_id=incident.merchant_id,
        incident_id=incident.id,
        selected_action=selected_action,
        status=campaign_status,
        target_payment_count=target_count,
        total_revenue_at_risk=total_revenue_at_risk,
        per_attempt_incentive=per_attempt_incentive,
        total_incentive_cost=total_incentive_cost,
        decision_evidence=evidence,
    )
    db.add(campaign)
    db.flush()

    record_audit_event(
        db=db,
        merchant_id=incident.merchant_id,
        incident_id=incident.id,
        campaign_id=campaign.id,
        event_type=RecoveryAuditEventType.CAMPAIGN_CREATED,
        actor_type="system",
        actor_id="recovery_service",
        new_state=campaign_status,
        reason_code="CAMPAIGN_INITIALIZED",
        explanation=f"Created recovery campaign {campaign.campaign_id} for {target_count} payments.",
        evidence={
            "campaign_id": campaign.campaign_id,
            "selected_action": selected_action,
            "target_payment_count": target_count,
            "total_revenue_at_risk": total_revenue_at_risk,
            "total_incentive_cost": total_incentive_cost,
        },
    )

    if campaign_status == "approved":
        record_audit_event(
            db=db,
            merchant_id=incident.merchant_id,
            incident_id=incident.id,
            campaign_id=campaign.id,
            event_type=RecoveryAuditEventType.CAMPAIGN_APPROVED,
            actor_type="system",
            actor_id="guardrail_engine",
            new_state="approved",
            reason_code="AUTOMATICALLY_APPROVED",
            explanation=f"Campaign {campaign.campaign_id} approved for execution.",
        )

    # 6. Create individual RecoveryAttempts for each target payment
    attempts = []
    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
    for pay in target_payments:
        attempt = RecoveryAttempt(
            recovery_id=f"rec_{uuid.uuid4().hex[:12]}",
            merchant_id=incident.merchant_id,
            incident_id=incident.id,
            campaign_id=campaign.id,
            payment_id=pay.id,
            selected_action=selected_action,
            incentive_amount=per_attempt_incentive,
            status=campaign_status,
            expires_at=expires_at,
            recovered_amount=0,
            decision_evidence=evidence,
        )
        db.add(attempt)
        attempts.append(attempt)

    db.commit()
    db.refresh(campaign)
    for a in attempts:
        db.refresh(a)

    return campaign, attempts


def decide_recovery_action(
    db: Session,
    incident: Incident,
    diagnosis: Diagnosis,
    policy: RecoveryPolicy,
    llm_provider: LLMProvider | None = None,
    payment: Payment | None = None,
    adaptive_analyst: Any | None = None,
) -> RecoveryAttempt:
    target_payments = [payment] if payment else None
    campaign, attempts = create_recovery_campaign(
        db=db,
        incident=incident,
        diagnosis=diagnosis,
        policy=policy,
        llm_provider=llm_provider,
        target_payments=target_payments,
        adaptive_analyst=adaptive_analyst,
    )
    return attempts[0] if attempts else None


def execute_recovery_attempt(
    db: Session,
    attempt: RecoveryAttempt,
    client: RazorpayClient = None,
) -> RecoveryAttempt:
    from app.integrations.razorpay import RazorpayClient
    from app.domains.recovery.guardrails import (
        evaluate_execution_guardrails,
        GuardrailViolationError,
    )
    from app.domains.recovery.audit import record_audit_event, RecoveryAuditEventType

    # 0. Idempotent check: if already executed with link, return existing attempt
    if attempt.status == "executed" and attempt.payment_link_id:
        return attempt

    # Defensively evaluate execution guardrails before any gateway interaction
    guardrail_result = evaluate_execution_guardrails(db, attempt)
    if guardrail_result.decision != "allowed":
        record_audit_event(
            db=db,
            merchant_id=attempt.merchant_id,
            incident_id=attempt.incident_id,
            campaign_id=attempt.campaign_id,
            recovery_attempt_id=attempt.id,
            payment_id=attempt.payment_id,
            event_type=RecoveryAuditEventType.GUARDRAIL_BLOCKED,
            actor_type="system",
            actor_id="guardrail_engine",
            reason_code=guardrail_result.reason_code,
            explanation=f"Execution guardrail blocked attempt: {guardrail_result.explanation}",
        )
        raise GuardrailViolationError(guardrail_result.explanation, result=guardrail_result)

    if not attempt.payment:
        raise ValueError(f"RecoveryAttempt {attempt.recovery_id} cannot be executed without an associated Payment")

    if client is None:
        client = RazorpayClient()

    original_amount = attempt.payment.amount
    charge_amount = original_amount - attempt.incentive_amount

    # Commit initial 'executing' transition to persist in-flight execution state before network call
    prev_status = attempt.status
    attempt.status = "executing"
    db.add(attempt)

    record_audit_event(
        db=db,
        merchant_id=attempt.merchant_id,
        incident_id=attempt.incident_id,
        campaign_id=attempt.campaign_id,
        recovery_attempt_id=attempt.id,
        payment_id=attempt.payment_id,
        event_type=RecoveryAuditEventType.RECOVERY_EXECUTION_STARTED,
        actor_type="system",
        actor_id="execution_service",
        previous_state=prev_status,
        new_state="executing",
        reason_code="EXECUTION_DISPATCHED",
        explanation=f"Dispatched recovery attempt {attempt.recovery_id} to Razorpay gateway.",
        evidence={"charge_amount": charge_amount, "action": attempt.selected_action},
    )
    db.commit()

    expire_by = int(attempt.expires_at.timestamp()) if attempt.expires_at else None

    # Check if a payment link already exists for this reference_id (e.g. from a prior crashed attempt)
    link_response = None
    if hasattr(client, "get_payment_link_by_reference_id") and callable(client.get_payment_link_by_reference_id):
        existing_res = client.get_payment_link_by_reference_id(attempt.recovery_id)
        if isinstance(existing_res, dict) and "id" in existing_res:
            link_response = existing_res

    if link_response is None:
        try:
            link_response = client.create_payment_link(
                amount=charge_amount,
                reference_id=attempt.recovery_id,
                description=f"Payment recovery link for {attempt.recovery_id}",
                expire_by=expire_by,
            )
        except Exception as exc:
            # Revert to approved so it can be retried safely
            attempt.status = "approved"
            db.add(attempt)
            record_audit_event(
                db=db,
                merchant_id=attempt.merchant_id,
                incident_id=attempt.incident_id,
                campaign_id=attempt.campaign_id,
                recovery_attempt_id=attempt.id,
                payment_id=attempt.payment_id,
                event_type=RecoveryAuditEventType.PAYMENT_LINK_EXECUTION_FAILED,
                actor_type="system",
                actor_id="razorpay",
                reason_code="GATEWAY_CALL_FAILED",
                explanation=f"Payment link creation failed: {str(exc)}",
            )
            db.commit()
            raise

    attempt.payment_link_id = link_response["id"]
    attempt.short_url = link_response.get("short_url")
    attempt.status = "executed"

    if attempt.campaign and attempt.campaign.status in ["approved", "pending"]:
        attempt.campaign.status = "executing"
        db.add(attempt.campaign)

    record_audit_event(
        db=db,
        merchant_id=attempt.merchant_id,
        incident_id=attempt.incident_id,
        campaign_id=attempt.campaign_id,
        recovery_attempt_id=attempt.id,
        payment_id=attempt.payment_id,
        event_type=RecoveryAuditEventType.PAYMENT_LINK_CREATED,
        actor_type="system",
        actor_id="razorpay",
        previous_state="executing",
        new_state="executed",
        reason_code="PAYMENT_LINK_CREATED",
        explanation=f"Created payment link {link_response['id']} for attempt {attempt.recovery_id}.",
        evidence={
            "payment_link_id": link_response["id"],
            "short_url": link_response.get("short_url"),
            "amount": charge_amount,
        },
    )

    db.add(attempt)
    db.commit()
    db.refresh(attempt)
    return attempt


def process_recovery_webhook(db: Session, payload: dict) -> None:
    from app.domains.recovery.audit import record_audit_event, RecoveryAuditEventType

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

    # Record webhook received event
    record_audit_event(
        db=db,
        merchant_id=attempt.merchant_id,
        incident_id=attempt.incident_id,
        campaign_id=attempt.campaign_id,
        recovery_attempt_id=attempt.id,
        payment_id=attempt.payment_id,
        event_type=RecoveryAuditEventType.WEBHOOK_RECEIVED,
        actor_type="webhook",
        actor_id="razorpay",
        reason_code="WEBHOOK_RECEIVED",
        explanation=f"Received payment link webhook for {attempt.recovery_id}.",
        evidence={
            "event": payload.get("event"),
            "payment_link_id": plink_entity.get("id"),
        },
    )

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

    if not attempt.payment:
        raise ValueError(f"RecoveryAttempt {attempt.recovery_id} does not have an associated Payment")

    original_amount = attempt.payment.amount
    expected_recovery_amount = max(0, original_amount - attempt.incentive_amount)
    if recovered_amount > expected_recovery_amount:
        raise ValueError(
            f"Recovered amount {recovered_amount} exceeds expected recovery amount {expected_recovery_amount}"
        )
    if recovered_amount < expected_recovery_amount:
        raise ValueError(
            f"Recovered amount {recovered_amount} is less than expected recovery charge {expected_recovery_amount} (accept_partial=False)"
        )

    # Record verified webhook event
    record_audit_event(
        db=db,
        merchant_id=attempt.merchant_id,
        incident_id=attempt.incident_id,
        campaign_id=attempt.campaign_id,
        recovery_attempt_id=attempt.id,
        payment_id=attempt.payment_id,
        event_type=RecoveryAuditEventType.WEBHOOK_VERIFIED,
        actor_type="system",
        actor_id="webhook_verifier",
        reason_code="SIGNATURE_AND_PAYLOAD_VERIFIED",
        explanation="Payment link status and financial payment invariants verified.",
        evidence={
            "payment_link_status": plink_status,
            "payment_status": payment_status,
            "amount_paid": recovered_amount,
        },
    )

    attempt.status = "recovered"
    attempt.resulting_payment_id = payment_entity.get("id")
    attempt.recovered_amount = recovered_amount

    # Record successful recovery event
    record_audit_event(
        db=db,
        merchant_id=attempt.merchant_id,
        incident_id=attempt.incident_id,
        campaign_id=attempt.campaign_id,
        recovery_attempt_id=attempt.id,
        payment_id=attempt.payment_id,
        event_type=RecoveryAuditEventType.RECOVERY_RECORDED,
        actor_type="system",
        actor_id="recovery_service",
        previous_state="executed",
        new_state="recovered",
        reason_code="PAYMENT_RECOVERED",
        explanation=f"Recorded verified recovery of {recovered_amount} for attempt {attempt.recovery_id}.",
        evidence={
            "resulting_payment_id": attempt.resulting_payment_id,
            "recovered_amount": recovered_amount,
        },
    )

    if attempt.campaign:
        all_attempts = attempt.campaign.recovery_attempts
        if all(a.status in ["recovered", "failed", "expired"] for a in all_attempts):
            attempt.campaign.status = "completed"
            db.add(attempt.campaign)

            recovered_count = sum(1 for a in all_attempts if a.status == "recovered")
            failed_count = sum(1 for a in all_attempts if a.status in ["failed", "expired"])
            gross_recovered = sum(a.recovered_amount for a in all_attempts if a.status == "recovered")

            record_audit_event(
                db=db,
                merchant_id=attempt.campaign.merchant_id,
                incident_id=attempt.campaign.incident_id,
                campaign_id=attempt.campaign.id,
                event_type=RecoveryAuditEventType.CAMPAIGN_COMPLETED,
                actor_type="system",
                actor_id="recovery_service",
                previous_state="executing",
                new_state="completed",
                reason_code="ALL_ATTEMPTS_SETTLED",
                explanation=f"Campaign {attempt.campaign.campaign_id} completed.",
                evidence={
                    "target_count": len(all_attempts),
                    "recovered_count": recovered_count,
                    "failed_count": failed_count,
                    "gross_recovered_amount": gross_recovered,
                    "total_incentive_cost": attempt.campaign.total_incentive_cost,
                },
            )

            # Phase 2: Observational Adaptive Decision Evaluation
            from app.domains.recovery.evaluation import evaluate_campaign_adaptive_decision
            try:
                evaluate_campaign_adaptive_decision(db=db, campaign=attempt.campaign, persist=True)
            except Exception:
                # Observational learning signals must never disrupt webhook settlement
                pass

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
    from app.domains.recovery.audit import record_audit_event, RecoveryAuditEventType

    # Row-level lock on incident to serialize concurrent orchestration attempts
    locked_incident = db.scalar(
        select(Incident).where(Incident.id == incident.id).with_for_update()
    )
    if locked_incident:
        incident = locked_incident

    # 1. Idempotency check: do not execute recovery if a campaign already exists for this incident
    existing_campaign = db.scalar(
        select(RecoveryCampaign).where(RecoveryCampaign.incident_id == incident.id)
    )
    if existing_campaign:
        existing_attempt = db.scalar(
            select(RecoveryAttempt).where(RecoveryAttempt.campaign_id == existing_campaign.id)
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

    # 4. Create RecoveryCampaign and individual RecoveryAttempts with concurrency race protection
    try:
        campaign, attempts = create_recovery_campaign(
            db=db,
            incident=incident,
            diagnosis=diagnosis,
            policy=policy,
        )
    except IntegrityError:
        db.rollback()
        existing_campaign = db.scalar(
            select(RecoveryCampaign).where(RecoveryCampaign.incident_id == incident.id)
        )
        if existing_campaign:
            existing_attempt = db.scalar(
                select(RecoveryAttempt).where(RecoveryAttempt.campaign_id == existing_campaign.id)
            )
            if existing_attempt:
                return existing_attempt
        raise
    except NoTargetPaymentsAvailableError as exc:
        record_audit_event(
            db=db,
            merchant_id=incident.merchant_id,
            incident_id=incident.id,
            event_type=RecoveryAuditEventType.GUARDRAIL_BLOCKED,
            actor_type="system",
            reason_code="NO_TARGET_PAYMENTS_AVAILABLE",
            explanation=str(exc),
        )
        return None

    # 5. If approved for automatic execution, execute all attempts
    if campaign.status == "approved" and campaign.selected_action != "ops_review":
        for att in attempts:
            execute_recovery_attempt(db, att, client)

    return attempts[0] if attempts else None
