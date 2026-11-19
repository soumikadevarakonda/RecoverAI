from datetime import datetime
import uuid
from sqlalchemy import case, func, literal_column, select
from sqlalchemy.orm import Session

from app.domains.cohorts.schemas import CohortMetrics
from app.models.payment import Payment


def _coalesced(column, default: str, label: str):
    # Inline the default as a SQL literal. As a bind parameter it is sent once
    # per occurrence by drivers with positional parameters (e.g. pg8000), and
    # PostgreSQL then cannot prove the SELECT and GROUP BY expressions equal.
    return func.coalesce(column, literal_column(f"'{default}'")).label(label)


def _dimension_exprs():
    return (
        _coalesced(Payment.method, "UNKNOWN", "method"),
        _coalesced(Payment.bank, "UNKNOWN", "bank"),
    )


def _dominant_failure_signatures(
    db: Session,
    merchant_id: uuid.UUID,
    window_start: datetime,
    window_end: datetime,
) -> dict[tuple[str, str], tuple[str, str, int, int]]:
    """
    For each (method, bank) returns the (error_code, error_step, failed_count,
    failed_amount) signature shared by the most failed payments. Ties are broken
    by failed amount, then alphabetically, so results are deterministic.
    """
    method_expr, bank_expr = _dimension_exprs()
    error_code_expr = _coalesced(Payment.error_code, "NONE", "error_code")
    error_step_expr = _coalesced(Payment.error_step, "NONE", "error_step")

    stmt = (
        select(
            method_expr,
            bank_expr,
            error_code_expr,
            error_step_expr,
            func.count(Payment.id).label("cnt"),
            func.coalesce(func.sum(Payment.amount), 0).label("amt"),
        )
        .where(
            Payment.merchant_id == merchant_id,
            Payment.status == "failed",
            Payment.created_at >= window_start,
            Payment.created_at <= window_end,
        )
        .group_by(method_expr, bank_expr, error_code_expr, error_step_expr)
    )

    grouped: dict[tuple[str, str], list[tuple[str, str, int, int]]] = {}
    for row in db.execute(stmt).all():
        grouped.setdefault((str(row.method), str(row.bank)), []).append(
            (str(row.error_code), str(row.error_step), int(row.cnt), int(row.amt))
        )

    # Sort alphabetically first; max() keeps the first of equal elements.
    return {
        key: max(sorted(cands), key=lambda c: (c[2], c[3]))
        for key, cands in grouped.items()
    }


def get_cohort_metrics(
    db: Session,
    merchant_id: uuid.UUID,
    window_start: datetime,
    window_end: datetime,
) -> list[CohortMetrics]:
    method_expr, bank_expr = _dimension_exprs()
    failed_amount_expr = func.coalesce(
        func.sum(case((Payment.status == "failed", Payment.amount), else_=0)),
        0,
    )

    stmt = (
        select(
            method_expr,
            bank_expr,
            func.count(Payment.id).label("total_count"),
            func.count(case((Payment.status == "failed", Payment.id))).label("failed_count"),
            func.coalesce(func.sum(Payment.amount), 0).label("total_amount"),
            failed_amount_expr.label("failed_amount"),
        )
        .where(
            Payment.merchant_id == merchant_id,
            Payment.created_at >= window_start,
            Payment.created_at <= window_end,
        )
        .group_by(method_expr, bank_expr)
        .order_by(failed_amount_expr.desc(), func.count(Payment.id).desc())
    )

    results = db.execute(stmt).all()
    signatures = _dominant_failure_signatures(db, merchant_id, window_start, window_end)

    cohorts: list[CohortMetrics] = []
    for row in results:
        total_count = int(row.total_count)
        failed_count = int(row.failed_count)
        failure_rate = (
            round(failed_count / total_count, 4) if total_count > 0 else 0.0
        )
        method, bank = str(row.method), str(row.bank)
        error_code, error_step, sig_count, sig_amount = signatures.get(
            (method, bank), ("NONE", "NONE", 0, 0)
        )
        cohorts.append(
            CohortMetrics(
                method=method,
                bank=bank,
                error_code=error_code,
                error_step=error_step,
                total_count=total_count,
                failed_count=failed_count,
                failure_rate=failure_rate,
                total_amount=int(row.total_amount),
                failed_amount=int(row.failed_amount),
                signature_failed_count=sig_count,
                signature_failed_amount=sig_amount,
            )
        )

    return cohorts
