from datetime import datetime, timedelta, timezone
import logging
from typing import List
from uuid import UUID
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.core.config import settings
from app.core.security import TokenSecretNotConfiguredError, verify_merchant_token
from app.db.dependencies import get_db
from app.domains.recovery.guardrails import GuardrailViolationError
from app.domains.recovery.service import (
    execute_recovery_attempt,
    expire_stale_attempts,
    orchestrate_recovery,
)
from app.models.merchant import Merchant
from app.models.incident import Incident
from app.models.payment import Payment
from app.models.recovery_attempt import RecoveryAttempt
from app.models.recovery_policy import RecoveryPolicy
from app.models.diagnosis import Diagnosis
from app.models.recovery_campaign import RecoveryCampaign
from app.domains.recovery.audit import (
    record_audit_event,
    get_recovery_audit_trail,
    RecoveryAuditEventType,
)
from app.schemas.api import (
    DashboardSummaryResponse,
    IncidentListResponse,
    IncidentDetailResponse,
    RecoveryAttemptDetailResponse,
    RecoveryAuditEventResponse,
    DiagnosisSummarySchema,
    RecoveryAttemptSummarySchema,
    DetectionRunResponse,
    DetectedIncidentSummary,
)

logger = logging.getLogger(__name__)

# Incident states that still represent unrecovered revenue.
ACTIVE_INCIDENT_STATUSES = ("detected", "diagnosed", "recovering")

router = APIRouter(
    prefix="/merchant",
    tags=["Merchant APIs"],
)


DEFAULT_TRIAL_MERCHANT_ID = UUID("b614b90f-49fd-4d6a-8689-52d4d2878b03")


def _ensure_trial_data(db: Session, merchant_id: UUID) -> None:
    """
    Ensures trial scenario data exists for the default trial merchant in development mode.
    """
    if not settings.is_production and merchant_id == DEFAULT_TRIAL_MERCHANT_ID:
        inc_count = db.scalar(select(func.count(Incident.id)).where(Incident.merchant_id == merchant_id))
        # The seeder deletes the merchant's existing rows before re-seeding, so it
        # must never run once real payments have been ingested for this merchant
        # (DEV_MERCHANT_ID defaults to the trial merchant in the README setup).
        payment_count = db.scalar(select(func.count(Payment.id)).where(Payment.merchant_id == merchant_id))
        if not inc_count and not payment_count:
            try:
                from scripts.seed_trial_data import seed_trial_scenario
                from app.domains.recovery.trial_evaluation import evaluate_trial_scenario
                seed_trial_scenario(db, merchant_id=merchant_id)
                evaluate_trial_scenario(db, merchant_id=merchant_id)
            except Exception:
                # Leave the session usable for the request that triggered seeding.
                db.rollback()
                logger.exception("Automatic trial data seeding failed for merchant %s", merchant_id)


def get_merchant_id(
    x_merchant_id: UUID | None = Header(None),
    authorization: str | None = Header(None),
    db: Session = Depends(get_db),
) -> UUID:
    """
    Resolves merchant identity with explicit production security boundaries:
    - In production mode, rejects unverified x-merchant-id headers and requires a valid Bearer token.
    - In development/demo mode, permits the x-merchant-id header for automated testing and local staging.
    """
    if settings.auth_mode == "production" or settings.environment == "production":
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Production authentication requires a valid Bearer token.",
            )
        token = authorization.split(" ", 1)[1].strip()
        try:
            merchant_uuid = verify_merchant_token(token)
        except TokenSecretNotConfiguredError:
            logger.error("AUTH_TOKEN_SECRET is not configured; rejecting production request.")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Server authentication is not configured.",
            )
        merchant = (
            db.scalar(select(Merchant).where(Merchant.id == merchant_uuid))
            if merchant_uuid is not None
            else None
        )

        if not merchant:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authorization token.",
            )
        return merchant.id

    if x_merchant_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing required x-merchant-id header in development mode.",
        )
    return x_merchant_id


@router.get("/dashboard/summary", response_model=DashboardSummaryResponse)
def get_dashboard_summary(
    merchant_id: UUID = Depends(get_merchant_id),
    db: Session = Depends(get_db),
):
    _ensure_trial_data(db, merchant_id)
    expire_stale_attempts(db, merchant_id)

    active_incidents = db.scalar(
        select(func.count(Incident.id)).where(
            Incident.merchant_id == merchant_id,
            Incident.status.in_(ACTIVE_INCIDENT_STATUSES),
        )
    ) or 0

    active_incident_risk = db.scalar(
        select(func.sum(Incident.revenue_at_risk)).where(
            Incident.merchant_id == merchant_id,
            Incident.status.in_(ACTIVE_INCIDENT_STATUSES),
        )
    ) or 0

    # Revenue already verified as recovered on still-active incidents is no
    # longer at risk.
    recovered_on_active = db.scalar(
        select(func.sum(RecoveryAttempt.recovered_amount))
        .join(Incident, RecoveryAttempt.incident_id == Incident.id)
        .where(
            RecoveryAttempt.merchant_id == merchant_id,
            RecoveryAttempt.status == "recovered",
            Incident.status.in_(ACTIVE_INCIDENT_STATUSES),
        )
    ) or 0

    revenue_at_risk = max(0, int(active_incident_risk) - int(recovered_on_active))

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

    # Clamped: recovered amounts can legitimately lag or exceed an incident's
    # initial estimate, but a rate above 100% is meaningless to display.
    recovery_rate = min(1.0, float(recovered_revenue) / float(total_risk)) if total_risk > 0 else 0.0

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
    _ensure_trial_data(db, merchant_id)
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
            campaign_id=att.campaign_id,
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



def _to_recovery_detail_response(attempt: RecoveryAttempt) -> RecoveryAttemptDetailResponse:
    payment_amount = attempt.payment.amount if attempt.payment is not None else None
    charge_amount = (
        max(0, payment_amount - (attempt.incentive_amount or 0))
        if payment_amount is not None
        else None
    )
    return RecoveryAttemptDetailResponse(
        payment_amount=payment_amount,
        charge_amount=charge_amount,
        id=attempt.id,
        recovery_id=attempt.recovery_id,
        merchant_id=attempt.merchant_id,
        incident_id=attempt.incident_id,
        campaign_id=attempt.campaign_id,
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
        decision_evidence=attempt.decision_evidence,
    )


@router.get("/recoveries", response_model=list[RecoveryAttemptDetailResponse])
def list_recoveries(
    merchant_id: UUID = Depends(get_merchant_id),
    db: Session = Depends(get_db),
):
    _ensure_trial_data(db, merchant_id)
    expire_stale_attempts(db, merchant_id)
    attempts = db.scalars(
        select(RecoveryAttempt)
        .where(RecoveryAttempt.merchant_id == merchant_id)
        .order_by(RecoveryAttempt.created_at.asc(), RecoveryAttempt.recovery_id.asc())
    ).all()

    return [_to_recovery_detail_response(att) for att in attempts]


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

    return _to_recovery_detail_response(attempt)


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

    record_audit_event(
        db=db,
        merchant_id=merchant_id,
        incident_id=attempt.incident_id,
        campaign_id=attempt.campaign_id,
        recovery_attempt_id=attempt.id,
        payment_id=attempt.payment_id,
        event_type=RecoveryAuditEventType.RECOVERY_APPROVED,
        actor_type="operator",
        actor_id=str(merchant_id),
        previous_state="pending",
        new_state="approved",
        reason_code="OPERATOR_APPROVED",
        explanation=f"Operator approved recovery attempt {attempt.recovery_id}.",
    )

    if attempt.campaign and attempt.campaign.status == "pending":
        attempt.campaign.status = "approved"
        db.add(attempt.campaign)
        record_audit_event(
            db=db,
            merchant_id=merchant_id,
            incident_id=attempt.incident_id,
            campaign_id=attempt.campaign.id,
            event_type=RecoveryAuditEventType.CAMPAIGN_APPROVED,
            actor_type="operator",
            actor_id=str(merchant_id),
            previous_state="pending",
            new_state="approved",
            reason_code="OPERATOR_APPROVED",
            explanation=f"Operator approved recovery campaign {attempt.campaign.campaign_id}.",
        )

    db.commit()
    db.refresh(attempt)

    return _to_recovery_detail_response(attempt)


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

    # 'ops_review' means a human investigates the cohort; it is never executed
    # by sending the customer a payment link.
    if attempt.selected_action == "ops_review":
        raise HTTPException(
            status_code=400,
            detail="ops_review attempts are resolved by operator review and cannot be executed as payment links.",
        )

    # Execute approved attempts, or resume one interrupted mid-execution.
    if attempt.status not in ("approved", "executing"):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot execute recovery attempt with status: {attempt.status}",
        )

    try:
        attempt = execute_recovery_attempt(db, attempt)
    except GuardrailViolationError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "Execution blocked by guardrails.",
                "reason_code": exc.result.reason_code,
                "explanation": exc.result.explanation,
            },
        )
    except Exception:
        db.rollback()
        logger.exception("Recovery execution failed for %s", recovery_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Recovery execution failed while contacting the payment gateway. The attempt can be retried.",
        )

    return _to_recovery_detail_response(attempt)


@router.get("/recoveries/{recovery_id}/audit", response_model=List[RecoveryAuditEventResponse])
def get_recovery_audit(
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

    return get_recovery_audit_trail(
        db=db,
        merchant_id=merchant_id,
        recovery_attempt_id=attempt.id,
    )


@router.get("/campaigns/{campaign_id}/audit", response_model=List[RecoveryAuditEventResponse])
def get_campaign_audit(
    campaign_id: str,
    merchant_id: UUID = Depends(get_merchant_id),
    db: Session = Depends(get_db),
):
    campaign = db.scalar(
        select(RecoveryCampaign).where(
            RecoveryCampaign.campaign_id == campaign_id,
            RecoveryCampaign.merchant_id == merchant_id,
        )
    )
    if not campaign:
        try:
            campaign_uuid = UUID(campaign_id)
            campaign = db.scalar(
                select(RecoveryCampaign).where(
                    RecoveryCampaign.id == campaign_uuid,
                    RecoveryCampaign.merchant_id == merchant_id,
                )
            )
        except ValueError:
            pass

    if not campaign:
        raise HTTPException(status_code=404, detail="Recovery campaign not found")

    return get_recovery_audit_trail(
        db=db,
        merchant_id=merchant_id,
        campaign_id=campaign.id,
    )


@router.post("/detection/run", response_model=DetectionRunResponse)
def run_detection_cycle(
    window_minutes: int = Query(30, ge=5, le=24 * 60),
    baseline_minutes: int = Query(60, ge=5, le=7 * 24 * 60),
    window_end: datetime | None = None,
    merchant_id: UUID = Depends(get_merchant_id),
    db: Session = Depends(get_db),
):
    """
    Runs one closed-loop cycle over live payment telemetry:
    cohort detection -> diagnosis -> economics/AI/guardrails -> campaign.
    Campaigns the guardrails approve for automatic execution are dispatched.

    Call this on a schedule (cron / worker) or from the control plane.
    """
    from app.domains.cohorts.detector import detect_cohort_degradations

    end = window_end or datetime.now(timezone.utc)
    start = end - timedelta(minutes=window_minutes)
    baseline_end = start
    baseline_start = baseline_end - timedelta(minutes=baseline_minutes)

    expire_stale_attempts(db, merchant_id, now=end)

    incidents = detect_cohort_degradations(
        db=db,
        merchant_id=merchant_id,
        window_start=start,
        window_end=end,
        baseline_start=baseline_start,
        baseline_end=baseline_end,
    )

    summaries: list[DetectedIncidentSummary] = []
    for incident in incidents:
        attempt = None
        error = None
        try:
            attempt = orchestrate_recovery(db, incident)
        except Exception as exc:
            db.rollback()
            logger.exception("Orchestration failed for incident %s", incident.id)
            error = exc.__class__.__name__
        db.refresh(incident)
        campaign = db.scalar(
            select(RecoveryCampaign).where(RecoveryCampaign.incident_id == incident.id)
        )
        summaries.append(
            DetectedIncidentSummary(
                incident_id=incident.id,
                method=incident.method,
                bank=incident.bank,
                error_code=incident.error_code,
                error_step=incident.error_step,
                status=incident.status,
                revenue_at_risk=incident.revenue_at_risk,
                campaign_id=campaign.campaign_id if campaign else None,
                campaign_status=campaign.status if campaign else None,
                selected_action=attempt.selected_action if attempt else None,
                error=error,
            )
        )

    return DetectionRunResponse(
        window_start=start,
        window_end=end,
        baseline_start=baseline_start,
        baseline_end=baseline_end,
        incidents_detected=len(incidents),
        incidents=summaries,
    )
