from datetime import datetime
import uuid
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.domains.cohorts.schemas import CohortMetrics
from app.models.payment import Payment


def get_cohort_metrics(
    db: Session,
    merchant_id: uuid.UUID,
    window_start: datetime,
    window_end: datetime,
) -> list[CohortMetrics]:
    method_expr = func.coalesce(Payment.method, "UNKNOWN").label("method")
    bank_expr = func.coalesce(Payment.bank, "UNKNOWN").label("bank")
    error_code_expr = func.coalesce(Payment.error_code, "NONE").label("error_code")
    error_step_expr = func.coalesce(Payment.error_step, "NONE").label("error_step")

    stmt = (
        select(
            method_expr,
            bank_expr,
            error_code_expr,
            error_step_expr,
            func.count(Payment.id).label("total_count"),
            func.count(case((Payment.status == "failed", Payment.id))).label("failed_count"),
            func.coalesce(func.sum(Payment.amount), 0).label("total_amount"),
            func.coalesce(
                func.sum(case((Payment.status == "failed", Payment.amount), else_=0)),
                0,
            ).label("failed_amount"),
        )
        .where(
            Payment.merchant_id == merchant_id,
            Payment.created_at >= window_start,
            Payment.created_at <= window_end,
        )
        .group_by(
            method_expr,
            bank_expr,
            error_code_expr,
            error_step_expr,
        )
        .order_by(
            func.coalesce(
                func.sum(case((Payment.status == "failed", Payment.amount), else_=0)),
                0,
            ).desc(),
            func.count(Payment.id).desc(),
        )
    )

    results = db.execute(stmt).all()

    cohorts: list[CohortMetrics] = []
    for row in results:
        total_count = int(row.total_count)
        failed_count = int(row.failed_count)
        failure_rate = (
            round(failed_count / total_count, 4) if total_count > 0 else 0.0
        )
        cohorts.append(
            CohortMetrics(
                method=str(row.method),
                bank=str(row.bank),
                error_code=str(row.error_code),
                error_step=str(row.error_step),
                total_count=total_count,
                failed_count=failed_count,
                failure_rate=failure_rate,
                total_amount=int(row.total_amount),
                failed_amount=int(row.failed_amount),
            )
        )

    return cohorts
