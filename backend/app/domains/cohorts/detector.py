from datetime import datetime
import uuid
from sqlalchemy.orm import Session

from app.domains.cohorts.schemas import CohortKey, CohortMetrics
from app.domains.cohorts.service import get_cohort_metrics
from app.models.incident import Incident

MIN_TRANSACTION_COUNT = 20
MIN_ABSOLUTE_INCREASE = 0.05
MIN_RELATIVE_MULTIPLIER = 2.0


def detect_cohort_degradations(
    db: Session,
    merchant_id: uuid.UUID,
    window_start: datetime,
    window_end: datetime,
    baseline_start: datetime | None = None,
    baseline_end: datetime | None = None,
) -> list[Incident]:
    if baseline_start is None or baseline_end is None:
        duration = window_end - window_start
        baseline_end = window_start
        baseline_start = window_start - duration

    current_cohorts = get_cohort_metrics(db, merchant_id, window_start, window_end)
    baseline_cohorts = get_cohort_metrics(db, merchant_id, baseline_start, baseline_end)

    baseline_map: dict[CohortKey, CohortMetrics] = {
        CohortKey(
            method=c.method,
            bank=c.bank,
            error_code=c.error_code,
            error_step=c.error_step,
        ): c
        for c in baseline_cohorts
    }

    incidents: list[Incident] = []

    for current in current_cohorts:
        # Rule 1: current transaction count is at least 20
        if current.total_count < MIN_TRANSACTION_COUNT:
            continue

        key = CohortKey(
            method=current.method,
            bank=current.bank,
            error_code=current.error_code,
            error_step=current.error_step,
        )

        baseline = baseline_map.get(key)
        baseline_failure_rate = baseline.failure_rate if baseline else 0.0
        baseline_total_count = baseline.total_count if baseline else 0
        baseline_failed_count = baseline.failed_count if baseline else 0
        baseline_total_amount = baseline.total_amount if baseline else 0
        baseline_failed_amount = baseline.failed_amount if baseline else 0

        current_failure_rate = current.failure_rate
        absolute_rate_increase = round(current_failure_rate - baseline_failure_rate, 4)

        if baseline_failure_rate > 0:
            relative_degradation = round(current_failure_rate / baseline_failure_rate, 4)
        else:
            relative_degradation = float("inf") if current_failure_rate > 0 else 1.0

        # Rule 2: current failure rate is at least 5 percentage points higher than baseline
        if absolute_rate_increase < MIN_ABSOLUTE_INCREASE:
            continue

        # Rule 3: current failure rate is at least 2x the baseline
        if current_failure_rate < MIN_RELATIVE_MULTIPLIER * baseline_failure_rate:
            continue

        # Revenue at risk: failed amount in current window
        revenue_at_risk = current.failed_amount

        incident = Incident(
            merchant_id=merchant_id,
            method=current.method,
            bank=current.bank,
            error_code=current.error_code,
            error_step=current.error_step,
            current_total_count=current.total_count,
            current_failed_count=current.failed_count,
            current_failure_rate=current_failure_rate,
            current_total_amount=current.total_amount,
            current_failed_amount=current.failed_amount,
            baseline_total_count=baseline_total_count,
            baseline_failed_count=baseline_failed_count,
            baseline_failure_rate=baseline_failure_rate,
            baseline_total_amount=baseline_total_amount,
            baseline_failed_amount=baseline_failed_amount,
            absolute_rate_increase=absolute_rate_increase,
            relative_degradation=relative_degradation,
            revenue_at_risk=revenue_at_risk,
            window_start=window_start,
            window_end=window_end,
            baseline_start=baseline_start,
            baseline_end=baseline_end,
            status="detected",
        )
        db.add(incident)
        incidents.append(incident)

    if incidents:
        db.commit()
        for inc in incidents:
            db.refresh(inc)

    return incidents
