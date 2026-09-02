from datetime import timezone
from enum import Enum
import statistics
import time
from typing import Any
import uuid
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.incident import Incident
from app.models.recovery_campaign import RecoveryCampaign


class MemoryEvidenceLevel(str, Enum):
    EXACT_COHORT = "exact_cohort"
    METHOD_BANK_ERROR = "method_bank_error"
    METHOD_ERROR = "method_error"
    METHOD = "method"
    GLOBAL = "global"
    INSUFFICIENT = "insufficient"


class HistoricalInterventionPoint(BaseModel):
    campaign_id: str | None = None
    incident_id: str | None = None
    failure_count: int
    failure_rate: float
    revenue_at_risk: int
    action: str
    observed_recovery_rate: float
    actual_net_recovery_value: int
    learning_signal: str


class ThresholdStatistics(BaseModel):
    sample_size: int = 0
    has_sufficient_samples: bool = False
    median_failure_count: float | None = None
    median_failure_rate: float | None = None
    successful_intervention_range: tuple[int, int] | None = None
    under_intervention_range: tuple[int, int] | None = None
    over_intervention_range: tuple[int, int] | None = None


class ActionHistoricalPerformance(BaseModel):
    action: str
    attempts: int = 0
    recovered_attempts: int = 0
    observed_recovery_rate: float = 0.0
    total_revenue_at_risk: int = 0
    recovered_amount: int = 0
    intervention_cost: int = 0
    net_recovery_value: int = 0


class CohortContext(BaseModel):
    method: str
    bank: str
    error_code: str
    error_step: str


class LearningSignalBreakdown(BaseModel):
    total_decisions: int = 0
    successful_decisions: int = 0
    over_interventions: int = 0
    under_interventions: int = 0
    recovery_outperformances: int = 0
    recovery_underperformances: int = 0
    guardrail_vetoes: int = 0
    ai_fallbacks: int = 0


class OperationalMemory(BaseModel):
    """
    Compact, structured retrieval-based operational memory.
    Advisory evidence only: has zero execution authority.
    """
    merchant_id: uuid.UUID
    cohort: CohortContext
    evidence_level: MemoryEvidenceLevel
    historical_sample_size: int
    signal_breakdown: LearningSignalBreakdown
    action_performance: dict[str, ActionHistoricalPerformance]
    threshold_evidence: list[HistoricalInterventionPoint]
    threshold_statistics: ThresholdStatistics
    summary: str
    retrieval_latency_ms: float = 0.0
    memory_size_bytes: int = 0


def _query_campaigns(
    db: Session,
    merchant_id: uuid.UUID,
    method: str | None = None,
    bank: str | None = None,
    error_code: str | None = None,
    error_step: str | None = None,
) -> list[RecoveryCampaign]:
    """Queries completed campaigns joined with incidents for the specific merchant."""
    stmt = (
        select(RecoveryCampaign)
        .join(Incident, RecoveryCampaign.incident_id == Incident.id)
        .where(
            RecoveryCampaign.merchant_id == merchant_id,
            RecoveryCampaign.status == "completed",
        )
        .order_by(RecoveryCampaign.created_at.desc())
    )

    if method is not None:
        stmt = stmt.where(Incident.method == method)
    if bank is not None:
        stmt = stmt.where(Incident.bank == bank)
    if error_code is not None:
        stmt = stmt.where(Incident.error_code == error_code)
    if error_step is not None:
        stmt = stmt.where(Incident.error_step == error_step)

    return list(db.scalars(stmt).all())


def _build_action_performance(campaigns: list[RecoveryCampaign]) -> dict[str, ActionHistoricalPerformance]:
    """Aggregates performance by candidate action."""
    perf: dict[str, dict[str, Any]] = {
        act: {
            "action": act,
            "attempts": 0,
            "recovered": 0,
            "revenue": 0,
            "recovered_amt": 0,
            "cost": 0,
        }
        for act in ["retry", "grace_period", "incentive", "ops_review"]
    }

    for c in campaigns:
        act = c.selected_action
        if act not in perf:
            perf[act] = {
                "action": act,
                "attempts": 0,
                "recovered": 0,
                "revenue": 0,
                "recovered_amt": 0,
                "cost": 0,
            }

        attempts = c.recovery_attempts or []
        target_count = len(attempts)
        rec_attempts = [a for a in attempts if a.status == "recovered"]
        recovered_count = len(rec_attempts)
        recovered_amt = sum(a.recovered_amount for a in rec_attempts)

        perf[act]["attempts"] += target_count
        perf[act]["recovered"] += recovered_count
        perf[act]["revenue"] += c.total_revenue_at_risk or 0
        perf[act]["recovered_amt"] += recovered_amt
        perf[act]["cost"] += c.total_incentive_cost or 0

    result = {}
    for act, data in perf.items():
        total_att = data["attempts"]
        rec_att = data["recovered"]
        rec_amt = data["recovered_amt"]
        cost = data["cost"]
        rate = (rec_att / total_att) if total_att > 0 else 0.0
        net = rec_amt - cost

        result[act] = ActionHistoricalPerformance(
            action=act,
            attempts=total_att,
            recovered_attempts=rec_att,
            observed_recovery_rate=round(rate, 4),
            total_revenue_at_risk=data["revenue"],
            recovered_amount=rec_amt,
            intervention_cost=cost,
            net_recovery_value=net,
        )

    return result


def _calculate_threshold_statistics(
    points: list[HistoricalInterventionPoint],
    min_stats_samples: int = 3,
) -> ThresholdStatistics:
    """Calculates statistics only when sample size is sufficient."""
    sample_size = len(points)
    if sample_size < min_stats_samples:
        return ThresholdStatistics(
            sample_size=sample_size,
            has_sufficient_samples=False,
        )

    failure_counts = [p.failure_count for p in points]
    failure_rates = [p.failure_rate for p in points]

    med_count = statistics.median(failure_counts)
    med_rate = statistics.median(failure_rates)

    successful_pts = [
        p.failure_count
        for p in points
        if p.learning_signal in ["successful_decision", "recovery_outperformance"]
    ]
    succ_range = (min(successful_pts), max(successful_pts)) if successful_pts else None

    under_pts = [p.failure_count for p in points if p.learning_signal == "under_intervention"]
    under_range = (min(under_pts), max(under_pts)) if under_pts else None

    over_pts = [p.failure_count for p in points if p.learning_signal == "over_intervention"]
    over_range = (min(over_pts), max(over_pts)) if over_pts else None

    return ThresholdStatistics(
        sample_size=sample_size,
        has_sufficient_samples=True,
        median_failure_count=round(float(med_count), 2),
        median_failure_rate=round(float(med_rate), 4),
        successful_intervention_range=succ_range,
        under_intervention_range=under_range,
        over_intervention_range=over_range,
    )


def _build_summary(
    level: MemoryEvidenceLevel,
    sample_size: int,
    signal_breakdown: LearningSignalBreakdown,
    stats: ThresholdStatistics,
    action_perf: dict[str, ActionHistoricalPerformance],
) -> str:
    """Produces a concise advisory summary for the analyst."""
    if level == MemoryEvidenceLevel.INSUFFICIENT or sample_size == 0:
        return (
            f"Evidence Level: {level.value} (Sample: {sample_size} decisions). "
            "Insufficient historical data for statistical threshold estimation. Exercise elevated caution."
        )

    parts = [
        f"Evidence Level: {level.value} ({sample_size} historical decisions).",
        f"Signals: {signal_breakdown.successful_decisions} successful, {signal_breakdown.recovery_outperformances} outperforming, "
        f"{signal_breakdown.recovery_underperformances} underperforming, {signal_breakdown.over_interventions} over-interventions, "
        f"{signal_breakdown.under_interventions} under-interventions, {signal_breakdown.guardrail_vetoes} guardrail vetoes.",
    ]

    if stats.has_sufficient_samples:
        parts.append(
            f"Threshold stats: median failure count {stats.median_failure_count}, median failure rate {stats.median_failure_rate:.1%}."
        )
        if stats.successful_intervention_range:
            parts.append(
                f"Successful interventions occurred between {stats.successful_intervention_range[0]} and {stats.successful_intervention_range[1]} failures."
            )
        if stats.over_intervention_range:
            parts.append(
                f"Over-interventions occurred at {stats.over_intervention_range} failures."
            )

    # Best performing action
    viable_actions = [a for a in action_perf.values() if a.attempts > 0]
    if viable_actions:
        best_act = max(viable_actions, key=lambda a: a.net_recovery_value)
        parts.append(
            f"Top historical action: '{best_act.action}' ({best_act.observed_recovery_rate:.1%} recovery rate, "
            f"net recovery value {best_act.net_recovery_value})."
        )

    return " ".join(parts)


def retrieve_operational_memory(
    db: Session,
    merchant_id: uuid.UUID,
    incident: Incident,
    min_samples: int = 1,
    min_stats_samples: int = 3,
) -> OperationalMemory:
    """
    Progressively retrieves operational memory across 6 deterministic hierarchy levels.
    Strictly merchant-isolated: never leaks cross-merchant data.
    """
    start_time = time.perf_counter()

    cohort = CohortContext(
        method=incident.method,
        bank=incident.bank,
        error_code=incident.error_code,
        error_step=incident.error_step,
    )

    # 1. Level 1: exact_cohort (method + bank + error_code + error_step)
    campaigns = _query_campaigns(
        db,
        merchant_id,
        method=incident.method,
        bank=incident.bank,
        error_code=incident.error_code,
        error_step=incident.error_step,
    )
    evidence_level = MemoryEvidenceLevel.EXACT_COHORT

    # 2. Level 2: method_bank_error (method + bank + error_code)
    if len(campaigns) < min_samples:
        campaigns = _query_campaigns(
            db,
            merchant_id,
            method=incident.method,
            bank=incident.bank,
            error_code=incident.error_code,
        )
        evidence_level = MemoryEvidenceLevel.METHOD_BANK_ERROR

    # 3. Level 3: method_error (method + error_code)
    if len(campaigns) < min_samples:
        campaigns = _query_campaigns(
            db,
            merchant_id,
            method=incident.method,
            error_code=incident.error_code,
        )
        evidence_level = MemoryEvidenceLevel.METHOD_ERROR

    # 4. Level 4: method (method)
    if len(campaigns) < min_samples:
        campaigns = _query_campaigns(
            db,
            merchant_id,
            method=incident.method,
        )
        evidence_level = MemoryEvidenceLevel.METHOD

    # 5. Level 5: global (merchant-wide)
    if len(campaigns) < min_samples:
        campaigns = _query_campaigns(
            db,
            merchant_id,
        )
        evidence_level = MemoryEvidenceLevel.GLOBAL

    # 6. Level 6: insufficient
    if len(campaigns) < min_samples:
        evidence_level = MemoryEvidenceLevel.INSUFFICIENT
        campaigns = []

    # Aggregate threshold intervention points & signal breakdown
    breakdown = LearningSignalBreakdown()
    points: list[HistoricalInterventionPoint] = []

    for c in campaigns:
        ev = c.decision_evidence or {}
        ls = ev.get("learning_signal") or {}
        signal_val = ls.get("classification") or "unknown"

        breakdown.total_decisions += 1
        if signal_val == "successful_decision":
            breakdown.successful_decisions += 1
        elif signal_val == "over_intervention":
            breakdown.over_interventions += 1
        elif signal_val == "under_intervention":
            breakdown.under_interventions += 1
        elif signal_val == "recovery_outperformance":
            breakdown.recovery_outperformances += 1
        elif signal_val == "recovery_underperformance":
            breakdown.recovery_underperformances += 1
        elif signal_val == "guardrail_veto":
            breakdown.guardrail_vetoes += 1
        elif signal_val == "ai_fallback":
            breakdown.ai_fallbacks += 1

        attempts = c.recovery_attempts or []
        target_cnt = len(attempts)
        recovered_cnt = sum(1 for a in attempts if a.status == "recovered")
        recovered_amt = sum(a.recovered_amount for a in attempts if a.status == "recovered")
        rate = (recovered_cnt / target_cnt) if target_cnt > 0 else 0.0
        net_val = recovered_amt - (c.total_incentive_cost or 0)

        f_count = c.incident.current_failed_count if c.incident else target_cnt
        f_rate = c.incident.current_failure_rate if c.incident else 0.0

        pt = HistoricalInterventionPoint(
            campaign_id=c.campaign_id,
            incident_id=str(c.incident_id),
            failure_count=f_count,
            failure_rate=f_rate,
            revenue_at_risk=c.total_revenue_at_risk or 0,
            action=c.selected_action,
            observed_recovery_rate=round(rate, 4),
            actual_net_recovery_value=net_val,
            learning_signal=signal_val,
        )
        points.append(pt)

    action_performance = _build_action_performance(campaigns)
    threshold_stats = _calculate_threshold_statistics(points, min_stats_samples=min_stats_samples)
    summary = _build_summary(
        level=evidence_level,
        sample_size=len(campaigns),
        signal_breakdown=breakdown,
        stats=threshold_stats,
        action_perf=action_performance,
    )

    elapsed_ms = (time.perf_counter() - start_time) * 1000.0

    memory = OperationalMemory(
        merchant_id=merchant_id,
        cohort=cohort,
        evidence_level=evidence_level,
        historical_sample_size=len(campaigns),
        signal_breakdown=breakdown,
        action_performance=action_performance,
        threshold_evidence=points,
        threshold_statistics=threshold_stats,
        summary=summary,
        retrieval_latency_ms=round(elapsed_ms, 2),
    )

    memory.memory_size_bytes = len(memory.model_dump_json().encode("utf-8"))
    return memory

