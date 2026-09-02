from datetime import datetime, timezone
from enum import Enum
from typing import Any
import uuid
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.models.recovery_campaign import RecoveryCampaign
from app.models.recovery_attempt import RecoveryAttempt
from app.domains.recovery.audit import record_audit_event, RecoveryAuditEventType


class LearningSignalClassification(str, Enum):
    SUCCESSFUL_DECISION = "successful_decision"
    OVER_INTERVENTION = "over_intervention"
    UNDER_INTERVENTION = "under_intervention"
    RECOVERY_UNDERPERFORMANCE = "recovery_underperformance"
    RECOVERY_OUTPERFORMANCE = "recovery_outperformance"
    GUARDRAIL_VETO = "guardrail_veto"
    AI_FALLBACK = "ai_fallback"


class AIEconomicTelemetry(BaseModel):
    """
    Associates model invocation & payload telemetry with resulting recovery economics.
    Captures latency and payload sizes, explicitly marking cost data as unavailable
    if the LLM provider does not expose pricing.
    """
    provider: str
    model_name: str
    latency_ms: float
    input_size_bytes: int
    output_size_bytes: int
    estimated_cost_usd: float | None = None
    cost_currency: str | None = None
    cost_status: str = Field(default="UNAVAILABLE", description="AVAILABLE or UNAVAILABLE")
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    gross_recovered_amount: int = Field(default=0, description="Gross recovered amount in minor units")
    net_recovery_value: int = Field(default=0, description="Net recovery value (gross - costs) in minor units")
    roi_multiplier: float | None = Field(default=None, description="Net value / cost multiplier if cost is known")


class AdaptiveDecisionEvaluation(BaseModel):
    """
    Evaluates an adaptive decision after terminal execution outcome.
    Observational only: has zero execution authority.
    """
    merchant_id: uuid.UUID
    campaign_id: uuid.UUID | None = None
    recovery_attempt_id: uuid.UUID | None = None
    payment_id: uuid.UUID | None = None
    incident_id: uuid.UUID | None = None
    terminal_status: str
    selected_action: str
    ai_strategist_used: bool = False
    predicted_action: str | None = None
    predicted_recovery_rate: float | None = None
    observed_recovery_rate: float
    rate_delta: float | None = None
    expected_recovered_amount: int = 0
    actual_recovered_amount: int = 0
    expected_net_recovery_value: int = 0
    actual_net_recovery_value: int = 0
    value_delta: int = 0
    recommended_failure_threshold: int | None = None
    observed_failure_count: int = 0
    meaningful_recovery: bool = False
    classification: LearningSignalClassification
    explanation: str
    telemetry: AIEconomicTelemetry | None = None
    memory_used: bool = False
    memory_evidence_level: str | None = None
    memory_sample_size: int | None = None
    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def _extract_adaptive_context(decision_evidence: dict[str, Any] | None) -> dict[str, Any]:
    """Extracts relevant fields from decision evidence safely."""
    evidence = decision_evidence or {}
    ai_strategist_used = bool(evidence.get("ai_strategist_used", False))
    selected_action = evidence.get("selected_action", "unknown")
    guardrail_decision = evidence.get("guardrail_decision")
    guardrail_reason = evidence.get("guardrail_reason_code")

    economics = evidence.get("economics", {})
    action_econ = economics.get("selected_action_economics", {})

    expected_rate = action_econ.get("expected_recovery_rate")
    expected_gross = action_econ.get("expected_recovery_amount", 0)
    expected_net = action_econ.get("expected_net_recovery_value", 0)

    adaptive_info = evidence.get("adaptive_analyst") or {}
    rec = adaptive_info.get("recommendation") or {}
    telemetry_raw = adaptive_info.get("telemetry") or {}

    intervene = rec.get("intervene")
    predicted_action = rec.get("recommended_action") or selected_action
    recommended_threshold = rec.get("recommended_failure_threshold")
    is_accepted = adaptive_info.get("is_accepted", False)
    rejection_reason = adaptive_info.get("rejection_reason")
    fallback_used = telemetry_raw.get("fallback_used", False) or not is_accepted

    mem_info = evidence.get("operational_memory") or {}
    memory_used = bool(mem_info.get("memory_used", False))
    memory_evidence_level = mem_info.get("evidence_level")
    memory_sample_size = mem_info.get("historical_sample_size")

    return {
        "ai_strategist_used": ai_strategist_used,
        "selected_action": selected_action,
        "guardrail_decision": guardrail_decision,
        "guardrail_reason": guardrail_reason,
        "expected_rate": expected_rate,
        "expected_gross": expected_gross,
        "expected_net": expected_net,
        "intervene": intervene,
        "predicted_action": predicted_action,
        "recommended_threshold": recommended_threshold,
        "is_accepted": is_accepted,
        "rejection_reason": rejection_reason,
        "fallback_used": fallback_used,
        "telemetry_raw": telemetry_raw,
        "memory_used": memory_used,
        "memory_evidence_level": memory_evidence_level,
        "memory_sample_size": memory_sample_size,
    }


def _classify_learning_signal(
    ctx: dict[str, Any],
    observed_rate: float,
    actual_recovered_amount: int,
    actual_net_recovery_value: int,
    observed_failures: int,
    revenue_at_risk: int,
) -> tuple[LearningSignalClassification, str]:
    """
    Deterministic & explainable classification of the adaptive decision outcome.
    """
    expected_rate = ctx.get("expected_rate")
    predicted_rate = expected_rate if expected_rate is not None else 0.0
    guardrail_decision = ctx.get("guardrail_decision")
    guardrail_reason = ctx.get("guardrail_reason")
    fallback_used = ctx.get("fallback_used", False)
    rejection_reason = ctx.get("rejection_reason")
    intervene = ctx.get("intervene")
    recommended_threshold = ctx.get("recommended_threshold")

    # 1. Guardrail Veto
    if guardrail_decision in ["blocked", "rejected"] or guardrail_reason == "CAMPAIGN_EXPOSURE_EXCEEDED":
        reason = guardrail_reason or "GUARDRAIL_BLOCKED"
        return (
            LearningSignalClassification.GUARDRAIL_VETO,
            f"AI strategy was vetoed by deterministic guardrails ({reason}) to protect merchant exposure limits.",
        )

    # 2. AI Fallback
    if fallback_used or rejection_reason:
        reason = rejection_reason or "AI_FALLBACK_TRIGGERED"
        return (
            LearningSignalClassification.AI_FALLBACK,
            f"Adaptive analysis was rejected or fell back to deterministic strategy ({reason}).",
        )

    # 3. Over-Intervention
    # If net recovery value was negative (incentive costs exceeded recovered revenue)
    if actual_net_recovery_value < 0:
        return (
            LearningSignalClassification.OVER_INTERVENTION,
            f"Intervention cost exceeded recovered revenue (net recovery: {actual_net_recovery_value}); over-intervention occurred.",
        )
    # If AI intervened when observed failures were zero/negligible
    if observed_failures == 0 and revenue_at_risk == 0 and actual_recovered_amount == 0:
        return (
            LearningSignalClassification.OVER_INTERVENTION,
            "Intervention executed on zero observed failures with zero revenue at risk.",
        )

    # 4. Under-Intervention
    # If AI recommended no intervention (intervene=False) despite high recoverable volume
    if intervene is False and observed_failures > 5 and revenue_at_risk > 0:
        return (
            LearningSignalClassification.UNDER_INTERVENTION,
            f"AI recommended against intervention despite {observed_failures} failures and {revenue_at_risk} revenue at risk.",
        )
    # If AI set an excessively high failure threshold that delayed intervention
    if recommended_threshold and recommended_threshold > (observed_failures * 3) and observed_failures > 5:
        return (
            LearningSignalClassification.UNDER_INTERVENTION,
            f"Recommended failure threshold ({recommended_threshold}) was set too high relative to observed failures ({observed_failures}).",
        )

    # 5. Recovery Outperformance
    # Observed rate meaningfully exceeded predicted rate (+0.10 or higher) with positive net value
    if actual_net_recovery_value > 0 and (observed_rate >= (predicted_rate + 0.10)):
        return (
            LearningSignalClassification.RECOVERY_OUTPERFORMANCE,
            f"Observed recovery rate ({observed_rate:.2%}) outperformed predicted rate ({predicted_rate:.2%}) with net value of {actual_net_recovery_value}.",
        )

    # 6. Recovery Underperformance
    # Observed rate fell short of predicted rate (-0.10 or lower)
    if observed_rate <= (predicted_rate - 0.10):
        return (
            LearningSignalClassification.RECOVERY_UNDERPERFORMANCE,
            f"Observed recovery rate ({observed_rate:.2%}) underperformed predicted rate ({predicted_rate:.2%}).",
        )

    # 7. Successful Decision (Default when outcomes aligned within normal tolerance with positive/meaningful recovery)
    return (
        LearningSignalClassification.SUCCESSFUL_DECISION,
        f"Recovery outcome aligned with predicted rate ({predicted_rate:.2%}) and achieved positive net value of {actual_net_recovery_value}.",
    )


def _build_economic_telemetry(
    telemetry_raw: dict[str, Any] | None,
    gross_recovered: int,
    net_value: int,
) -> AIEconomicTelemetry | None:
    """Builds AIEconomicTelemetry without inventing token prices if unavailable."""
    if not telemetry_raw:
        return None

    provider = telemetry_raw.get("provider", "unknown")
    model_name = telemetry_raw.get("model_name", "unknown")
    latency_ms = float(telemetry_raw.get("latency_ms", 0.0))
    input_bytes = int(telemetry_raw.get("input_size_bytes", 0))
    output_bytes = int(telemetry_raw.get("output_size_bytes", 0))
    input_tokens = telemetry_raw.get("input_tokens")
    output_tokens = telemetry_raw.get("output_tokens")
    total_tokens = telemetry_raw.get("total_tokens")
    cost_status = telemetry_raw.get("cost_status", "UNAVAILABLE")
    estimated_cost_usd = telemetry_raw.get("estimated_cost_usd")

    roi_multiplier = None
    if estimated_cost_usd and estimated_cost_usd > 0 and net_value:
        net_value_usd = (net_value / 100.0) / 83.0
        roi_multiplier = round(net_value_usd / estimated_cost_usd, 2)

    return AIEconomicTelemetry(
        provider=provider,
        model_name=model_name,
        latency_ms=latency_ms,
        input_size_bytes=input_bytes,
        output_size_bytes=output_bytes,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        estimated_cost_usd=estimated_cost_usd,
        cost_currency="USD" if estimated_cost_usd else None,
        cost_status=cost_status,
        gross_recovered_amount=gross_recovered,
        net_recovery_value=net_value,
        roi_multiplier=roi_multiplier,
    )


def evaluate_campaign_adaptive_decision(
    db: Session,
    campaign: RecoveryCampaign,
    persist: bool = True,
) -> AdaptiveDecisionEvaluation:
    """
    Evaluates an adaptive recovery campaign decision after terminal completion.
    Observational only: does not modify policies, caps, or execute actions.
    """
    if campaign.status != "completed":
        raise ValueError(
            f"Cannot evaluate non-terminal campaign {campaign.campaign_id}. Status is '{campaign.status}', expected 'completed'."
        )

    all_attempts = campaign.recovery_attempts or []
    target_count = len(all_attempts)

    recovered_attempts = [a for a in all_attempts if a.status == "recovered"]
    recovered_count = len(recovered_attempts)
    observed_rate = (recovered_count / target_count) if target_count > 0 else 0.0

    actual_recovered_amount = sum(a.recovered_amount for a in recovered_attempts)
    total_incentive_cost = campaign.total_incentive_cost or 0
    actual_net_recovery_value = actual_recovered_amount - total_incentive_cost

    ctx = _extract_adaptive_context(campaign.decision_evidence)
    predicted_rate = ctx.get("expected_rate")
    rate_delta = (observed_rate - predicted_rate) if predicted_rate is not None else None

    expected_gross = ctx.get("expected_gross", 0)
    expected_net = ctx.get("expected_net", 0)
    value_delta = actual_net_recovery_value - expected_net

    observed_failures = campaign.incident.current_failed_count if campaign.incident else target_count
    revenue_at_risk = campaign.total_revenue_at_risk or 0

    classification, explanation = _classify_learning_signal(
        ctx=ctx,
        observed_rate=observed_rate,
        actual_recovered_amount=actual_recovered_amount,
        actual_net_recovery_value=actual_net_recovery_value,
        observed_failures=observed_failures,
        revenue_at_risk=revenue_at_risk,
    )

    meaningful_recovery = actual_recovered_amount > 0 and actual_net_recovery_value > 0
    telemetry = _build_economic_telemetry(
        telemetry_raw=ctx.get("telemetry_raw"),
        gross_recovered=actual_recovered_amount,
        net_value=actual_net_recovery_value,
    )

    evaluation = AdaptiveDecisionEvaluation(
        merchant_id=campaign.merchant_id,
        campaign_id=campaign.id,
        incident_id=campaign.incident_id,
        terminal_status=campaign.status,
        selected_action=campaign.selected_action,
        ai_strategist_used=ctx["ai_strategist_used"],
        predicted_action=ctx["predicted_action"],
        predicted_recovery_rate=predicted_rate,
        observed_recovery_rate=round(observed_rate, 4),
        rate_delta=round(rate_delta, 4) if rate_delta is not None else None,
        expected_recovered_amount=expected_gross,
        actual_recovered_amount=actual_recovered_amount,
        expected_net_recovery_value=expected_net,
        actual_net_recovery_value=actual_net_recovery_value,
        value_delta=value_delta,
        recommended_failure_threshold=ctx["recommended_threshold"],
        observed_failure_count=observed_failures,
        meaningful_recovery=meaningful_recovery,
        classification=classification,
        explanation=explanation,
        telemetry=telemetry,
        memory_used=ctx["memory_used"],
        memory_evidence_level=ctx["memory_evidence_level"],
        memory_sample_size=ctx["memory_sample_size"],
    )

    if persist:
        # 1. Update campaign decision_evidence with learning signal (zero migration!)
        evidence = dict(campaign.decision_evidence or {})
        evidence["learning_signal"] = evaluation.model_dump(mode="json")
        campaign.decision_evidence = evidence
        db.add(campaign)

        # 2. Append to recovery_audit_events
        record_audit_event(
            db=db,
            merchant_id=campaign.merchant_id,
            incident_id=campaign.incident_id,
            campaign_id=campaign.id,
            event_type=RecoveryAuditEventType.LEARNING_SIGNAL_RECORDED,
            actor_type="system",
            actor_id="adaptive_evaluator",
            reason_code=classification.value,
            explanation=explanation,
            evidence=evaluation.model_dump(mode="json"),
        )
        db.commit()
        db.refresh(campaign)

    return evaluation


def evaluate_attempt_adaptive_decision(
    db: Session,
    attempt: RecoveryAttempt,
    persist: bool = True,
) -> AdaptiveDecisionEvaluation:
    """
    Evaluates an individual adaptive recovery attempt after terminal completion.
    Observational only: does not modify policies, caps, or execute actions.
    """
    if attempt.status not in ["recovered", "failed", "expired"]:
        raise ValueError(
            f"Cannot evaluate non-terminal attempt {attempt.recovery_id}. Status is '{attempt.status}', expected 'recovered', 'failed', or 'expired'."
        )

    is_recovered = attempt.status == "recovered"
    observed_rate = 1.0 if is_recovered else 0.0
    actual_recovered_amount = attempt.recovered_amount if is_recovered else 0
    incentive_cost = attempt.incentive_amount if is_recovered else 0
    actual_net_recovery_value = actual_recovered_amount - incentive_cost

    ctx = _extract_adaptive_context(attempt.decision_evidence)
    predicted_rate = ctx.get("expected_rate")
    rate_delta = (observed_rate - predicted_rate) if predicted_rate is not None else None

    # Expected values for single attempt
    original_amount = attempt.payment.amount if attempt.payment else 0
    expected_gross = int(round((predicted_rate or 0.0) * original_amount)) if original_amount > 0 else ctx.get("expected_gross", 0)
    expected_net = expected_gross - (attempt.incentive_amount if attempt.selected_action == "incentive" else 0)
    value_delta = actual_net_recovery_value - expected_net

    observed_failures = 1
    revenue_at_risk = original_amount

    classification, explanation = _classify_learning_signal(
        ctx=ctx,
        observed_rate=observed_rate,
        actual_recovered_amount=actual_recovered_amount,
        actual_net_recovery_value=actual_net_recovery_value,
        observed_failures=observed_failures,
        revenue_at_risk=revenue_at_risk,
    )

    meaningful_recovery = actual_recovered_amount > 0 and actual_net_recovery_value > 0
    telemetry = _build_economic_telemetry(
        telemetry_raw=ctx.get("telemetry_raw"),
        gross_recovered=actual_recovered_amount,
        net_value=actual_net_recovery_value,
    )

    evaluation = AdaptiveDecisionEvaluation(
        merchant_id=attempt.merchant_id,
        campaign_id=attempt.campaign_id,
        recovery_attempt_id=attempt.id,
        payment_id=attempt.payment_id,
        incident_id=attempt.incident_id,
        terminal_status=attempt.status,
        selected_action=attempt.selected_action,
        ai_strategist_used=ctx["ai_strategist_used"],
        predicted_action=ctx["predicted_action"],
        predicted_recovery_rate=predicted_rate,
        observed_recovery_rate=round(observed_rate, 4),
        rate_delta=round(rate_delta, 4) if rate_delta is not None else None,
        expected_recovered_amount=expected_gross,
        actual_recovered_amount=actual_recovered_amount,
        expected_net_recovery_value=expected_net,
        actual_net_recovery_value=actual_net_recovery_value,
        value_delta=value_delta,
        recommended_failure_threshold=ctx["recommended_threshold"],
        observed_failure_count=observed_failures,
        meaningful_recovery=meaningful_recovery,
        classification=classification,
        explanation=explanation,
        telemetry=telemetry,
        memory_used=ctx["memory_used"],
        memory_evidence_level=ctx["memory_evidence_level"],
        memory_sample_size=ctx["memory_sample_size"],
    )

    if persist:
        # 1. Update attempt decision_evidence with learning signal (zero migration!)
        evidence = dict(attempt.decision_evidence or {})
        evidence["learning_signal"] = evaluation.model_dump(mode="json")
        attempt.decision_evidence = evidence
        db.add(attempt)

        # 2. Append to recovery_audit_events
        record_audit_event(
            db=db,
            merchant_id=attempt.merchant_id,
            incident_id=attempt.incident_id,
            campaign_id=attempt.campaign_id,
            recovery_attempt_id=attempt.id,
            payment_id=attempt.payment_id,
            event_type=RecoveryAuditEventType.LEARNING_SIGNAL_RECORDED,
            actor_type="system",
            actor_id="adaptive_evaluator",
            reason_code=classification.value,
            explanation=explanation,
            evidence=evaluation.model_dump(mode="json"),
        )
        db.commit()
        db.refresh(attempt)

    return evaluation

