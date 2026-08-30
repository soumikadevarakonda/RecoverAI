from uuid import UUID
from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.dependencies import get_db
from app.domains.recovery.service import execute_recovery_attempt
from app.models.incident import Incident
from app.models.payment import Payment
from app.models.recovery_attempt import RecoveryAttempt
from app.models.recovery_policy import RecoveryPolicy
from app.models.diagnosis import Diagnosis
from app.schemas.api import (
    DashboardSummaryResponse,
    IncidentListResponse,
    IncidentDetailResponse,
    RecoveryAttemptDetailResponse,
    DiagnosisSummarySchema,
    RecoveryAttemptSummarySchema,
)

router = APIRouter(
    prefix="/merchant",
    tags=["Merchant APIs"],
)


def get_merchant_id(x_merchant_id: UUID = Header(...)) -> UUID:
    return x_merchant_id


@router.get("/dashboard/summary", response_model=DashboardSummaryResponse)
def get_dashboard_summary(
    merchant_id: UUID = Depends(get_merchant_id),
    db: Session = Depends(get_db),
):
    active_incidents = db.scalar(
        select(func.count(Incident.id)).where(
            Incident.merchant_id == merchant_id,
            Incident.status.in_(["detected", "diagnosed"]),
        )
    ) or 0

    revenue_at_risk = db.scalar(
        select(func.sum(Incident.revenue_at_risk)).where(
            Incident.merchant_id == merchant_id,
            Incident.status.in_(["detected", "diagnosed"]),
        )
    ) or 0

    recovered_revenue = db.scalar(
        select(func.sum(RecoveryAttempt.recovered_amount)).where(
            RecoveryAttempt.merchant_id == merchant_id,
            RecoveryAttempt.status == "recovered",
        )
    ) or 0

    failed_payments = db.scalar(
        select(func.count(Payment.id)).where(
            Payment.merchant_id == merchant_id,
            Payment.status == "failed",
        )
    ) or 0

    recovery_attempts = db.scalar(
        select(func.count(RecoveryAttempt.id)).where(
            RecoveryAttempt.merchant_id == merchant_id,
        )
    ) or 0

    total_risk = db.scalar(
        select(func.sum(Incident.revenue_at_risk)).where(
            Incident.merchant_id == merchant_id,
        )
    ) or 0

    recovery_rate = float(recovered_revenue) / float(total_risk) if total_risk > 0 else 0.0

    return DashboardSummaryResponse(
        revenue_at_risk=revenue_at_risk,
        recovered_revenue=recovered_revenue,
        recovery_rate=recovery_rate,
        active_incidents=active_incidents,
        failed_payments=failed_payments,
        recovery_attempts=recovery_attempts,
    )


@router.get("/incidents", response_model=list[IncidentListResponse])
def list_incidents(
    merchant_id: UUID = Depends(get_merchant_id),
    db: Session = Depends(get_db),
):
    incidents = db.scalars(
        select(Incident).where(Incident.merchant_id == merchant_id)
    ).all()

    response = []
    for inc in incidents:
        diag = db.scalar(select(Diagnosis).where(Diagnosis.incident_id == inc.id))
        diag_schema = None
        if diag:
            diag_schema = DiagnosisSummarySchema(
                diagnosis_type=diag.diagnosis_type,
                confidence=diag.confidence,
                explanation=diag.explanation,
            )

        response.append(
            IncidentListResponse(
                id=inc.id,
                status=inc.status,
                method=inc.method,
                bank=inc.bank,
                error_code=inc.error_code,
                error_step=inc.error_step,
                revenue_at_risk=inc.revenue_at_risk,
                created_at=inc.created_at,
                diagnosis=diag_schema,
            )
        )
    return response


@router.get("/incidents/{incident_id}", response_model=IncidentDetailResponse)
def get_incident_detail(
    incident_id: UUID,
    merchant_id: UUID = Depends(get_merchant_id),
    db: Session = Depends(get_db),
):
    incident = db.scalar(
        select(Incident).where(
            Incident.id == incident_id,
            Incident.merchant_id == merchant_id,
        )
    )
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")

    diag = db.scalar(select(Diagnosis).where(Diagnosis.incident_id == incident.id))
    diag_schema = None
    if diag:
        diag_schema = DiagnosisSummarySchema(
            diagnosis_type=diag.diagnosis_type,
            confidence=diag.confidence,
            explanation=diag.explanation,
        )

    attempts = db.scalars(
        select(RecoveryAttempt).where(RecoveryAttempt.incident_id == incident.id)
    ).all()
    attempts_schema = [
        RecoveryAttemptSummarySchema(
            id=att.id,
            recovery_id=att.recovery_id,
            selected_action=att.selected_action,
            incentive_amount=att.incentive_amount,
            status=att.status,
            recovered_amount=att.recovered_amount,
            payment_link_id=att.payment_link_id,
            short_url=att.short_url,
        )
        for att in attempts
    ]

    return IncidentDetailResponse(
        id=incident.id,
        merchant_id=incident.merchant_id,
        status=incident.status,
        method=incident.method,
        bank=incident.bank,
        error_code=incident.error_code,
        error_step=incident.error_step,
        current_total_count=incident.current_total_count,
        current_failed_count=incident.current_failed_count,
        current_failure_rate=incident.current_failure_rate,
        current_total_amount=incident.current_total_amount,
        current_failed_amount=incident.current_failed_amount,
        baseline_total_count=incident.baseline_total_count,
        baseline_failed_count=incident.baseline_failed_count,
        baseline_failure_rate=incident.baseline_failure_rate,
        baseline_total_amount=incident.baseline_total_amount,
        baseline_failed_amount=incident.baseline_failed_amount,
        absolute_rate_increase=incident.absolute_rate_increase,
        relative_degradation=incident.relative_degradation,
        revenue_at_risk=incident.revenue_at_risk,
        window_start=incident.window_start,
        window_end=incident.window_end,
        baseline_start=incident.baseline_start,
        baseline_end=incident.baseline_end,
        created_at=incident.created_at,
        updated_at=incident.updated_at,
        diagnosis=diag_schema,
        recovery_attempts=attempts_schema,
    )


@router.get("/recoveries", response_model=list[RecoveryAttemptDetailResponse])
def list_recoveries(
    merchant_id: UUID = Depends(get_merchant_id),
    db: Session = Depends(get_db),
):
    attempts = db.scalars(
        select(RecoveryAttempt).where(RecoveryAttempt.merchant_id == merchant_id)
    ).all()

    return [
        RecoveryAttemptDetailResponse(
            id=att.id,
            recovery_id=att.recovery_id,
            merchant_id=att.merchant_id,
            incident_id=att.incident_id,
            payment_id=att.payment_id,
            selected_action=att.selected_action,
            incentive_amount=att.incentive_amount,
            status=att.status,
            created_at=att.created_at,
            expires_at=att.expires_at,
            recovered_amount=att.recovered_amount,
            resulting_payment_id=att.resulting_payment_id,
            payment_link_id=att.payment_link_id,
            short_url=att.short_url,
        )
        for att in attempts
    ]


@router.get("/recoveries/{recovery_id}", response_model=RecoveryAttemptDetailResponse)
def get_recovery_detail(
    recovery_id: str,
    merchant_id: UUID = Depends(get_merchant_id),
    db: Session = Depends(get_db),
):
    attempt = db.scalar(
        select(RecoveryAttempt).where(
            RecoveryAttempt.recovery_id == recovery_id,
            RecoveryAttempt.merchant_id == merchant_id,
        )
    )
    if not attempt:
        try:
            attempt_uuid = UUID(recovery_id)
            attempt = db.scalar(
                select(RecoveryAttempt).where(
                    RecoveryAttempt.id == attempt_uuid,
                    RecoveryAttempt.merchant_id == merchant_id,
                )
            )
        except ValueError:
            pass

    if not attempt:
        raise HTTPException(status_code=404, detail="Recovery attempt not found")

    return RecoveryAttemptDetailResponse(
        id=attempt.id,
        recovery_id=attempt.recovery_id,
        merchant_id=attempt.merchant_id,
        incident_id=attempt.incident_id,
        payment_id=attempt.payment_id,
        selected_action=attempt.selected_action,
        incentive_amount=attempt.incentive_amount,
        status=attempt.status,
        created_at=attempt.created_at,
        expires_at=attempt.expires_at,
        recovered_amount=attempt.recovered_amount,
        resulting_payment_id=attempt.resulting_payment_id,
        payment_link_id=attempt.payment_link_id,
        short_url=attempt.short_url,
    )


@router.post("/recoveries/{recovery_id}/approve", response_model=RecoveryAttemptDetailResponse)
def approve_recovery(
    recovery_id: str,
    merchant_id: UUID = Depends(get_merchant_id),
    db: Session = Depends(get_db),
):
    attempt = db.scalar(
        select(RecoveryAttempt).where(
            RecoveryAttempt.recovery_id == recovery_id,
            RecoveryAttempt.merchant_id == merchant_id,
        ).with_for_update()
    )
    if not attempt:
        try:
            attempt_uuid = UUID(recovery_id)
            attempt = db.scalar(
                select(RecoveryAttempt).where(
                    RecoveryAttempt.id == attempt_uuid,
                    RecoveryAttempt.merchant_id == merchant_id,
                ).with_for_update()
            )
        except ValueError:
            pass

    if not attempt:
        raise HTTPException(status_code=404, detail="Recovery attempt not found")

    # Approve only attempts currently waiting for approval (pending)
    if attempt.status != "pending":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot approve recovery attempt with status: {attempt.status}",
        )

    attempt.status = "approved"
    db.add(attempt)
    db.commit()
    db.refresh(attempt)

    return RecoveryAttemptDetailResponse(
        id=attempt.id,
        recovery_id=attempt.recovery_id,
        merchant_id=attempt.merchant_id,
        incident_id=attempt.incident_id,
        payment_id=attempt.payment_id,
        selected_action=attempt.selected_action,
        incentive_amount=attempt.incentive_amount,
        status=attempt.status,
        created_at=attempt.created_at,
        expires_at=attempt.expires_at,
        recovered_amount=attempt.recovered_amount,
        resulting_payment_id=attempt.resulting_payment_id,
        payment_link_id=attempt.payment_link_id,
        short_url=attempt.short_url,
    )


@router.post("/recoveries/{recovery_id}/execute", response_model=RecoveryAttemptDetailResponse)
def execute_recovery(
    recovery_id: str,
    merchant_id: UUID = Depends(get_merchant_id),
    db: Session = Depends(get_db),
):
    attempt = db.scalar(
        select(RecoveryAttempt).where(
            RecoveryAttempt.recovery_id == recovery_id,
            RecoveryAttempt.merchant_id == merchant_id,
        ).with_for_update()
    )
    if not attempt:
        try:
            attempt_uuid = UUID(recovery_id)
            attempt = db.scalar(
                select(RecoveryAttempt).where(
                    RecoveryAttempt.id == attempt_uuid,
                    RecoveryAttempt.merchant_id == merchant_id,
                ).with_for_update()
            )
        except ValueError:
            pass

    if not attempt:
        raise HTTPException(status_code=404, detail="Recovery attempt not found")

    # Execute only an approved RecoveryAttempt
    if attempt.status != "approved":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot execute recovery attempt with status: {attempt.status}",
        )

    try:
        attempt = execute_recovery_attempt(db, attempt)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Recovery execution failed: {exc}",
        )

    return RecoveryAttemptDetailResponse(
        id=attempt.id,
        recovery_id=attempt.recovery_id,
        merchant_id=attempt.merchant_id,
        incident_id=attempt.incident_id,
        payment_id=attempt.payment_id,
        selected_action=attempt.selected_action,
        incentive_amount=attempt.incentive_amount,
        status=attempt.status,
        created_at=attempt.created_at,
        expires_at=attempt.expires_at,
        recovered_amount=attempt.recovered_amount,
        resulting_payment_id=attempt.resulting_payment_id,
        payment_link_id=attempt.payment_link_id,
        short_url=attempt.short_url,
    )
