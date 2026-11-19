"""
Development-only endpoints.

The built-in checkout simulator (frontend/pay.html) is used when Razorpay
test keys are not configured. Instead of faking a recovery in browser
storage, it calls this endpoint, which settles the attempt through the same
verification path a real `payment_link.paid` webhook uses: the amount comes
from the stored payment (never from the browser), the link id must match, and
the attempt must be in the 'executed' state. Disabled in production.
"""
from datetime import datetime, timezone
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.routes.merchant import get_merchant_id
from app.core.config import settings
from app.db.dependencies import get_db
from app.domains.recovery.service import process_recovery_webhook
from app.models.recovery_attempt import RecoveryAttempt
from app.models.webhook_event import WebhookEvent
from app.schemas.api import SimulatedPaymentResponse

SIMULATOR_URL_PREFIX = "/pay.html"

router = APIRouter(prefix="/dev", tags=["Development"])


def require_development_mode() -> None:
    if settings.is_production:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")


@router.post(
    "/payment-links/{recovery_id}/simulate-paid",
    response_model=SimulatedPaymentResponse,
    dependencies=[Depends(require_development_mode)],
)
def simulate_payment_link_paid(
    recovery_id: str,
    merchant_id: uuid.UUID = Depends(get_merchant_id),
    db: Session = Depends(get_db),
):
    attempt = db.scalar(
        select(RecoveryAttempt).where(
            RecoveryAttempt.recovery_id == recovery_id,
            RecoveryAttempt.merchant_id == merchant_id,
        )
    )
    if not attempt:
        raise HTTPException(status_code=404, detail="Recovery attempt not found")

    if attempt.status == "recovered":
        return SimulatedPaymentResponse(
            recovery_id=attempt.recovery_id,
            status=attempt.status,
            recovered_amount=attempt.recovered_amount,
            payment_link_id=attempt.payment_link_id,
            resulting_payment_id=attempt.resulting_payment_id,
        )

    if attempt.status != "executed" or not attempt.payment_link_id:
        raise HTTPException(
            status_code=409,
            detail=f"Only executed attempts with a payment link can be paid (status: {attempt.status}).",
        )

    if not (attempt.short_url or "").startswith(SIMULATOR_URL_PREFIX):
        raise HTTPException(
            status_code=409,
            detail="This attempt has a real Razorpay payment link; pay it through Razorpay test mode instead.",
        )

    if attempt.payment is None:
        raise HTTPException(status_code=409, detail="Recovery attempt has no associated payment")

    charge_amount = attempt.payment.amount - (attempt.incentive_amount or 0)
    now_ts = int(datetime.now(timezone.utc).timestamp())
    simulated_payment_id = f"pay_sim_{uuid.uuid4().hex[:14]}"

    payload = {
        "entity": "event",
        "event": "payment_link.paid",
        "created_at": now_ts,
        "payload": {
            "payment_link": {
                "entity": {
                    "id": attempt.payment_link_id,
                    "reference_id": attempt.recovery_id,
                    "status": "paid",
                    "amount": charge_amount,
                    "amount_paid": charge_amount,
                    "currency": "INR",
                    "notes": {"recovery_id": attempt.recovery_id},
                }
            },
            "payment": {
                "entity": {
                    "id": simulated_payment_id,
                    "amount": charge_amount,
                    "currency": "INR",
                    "status": "captured",
                    "method": "upi",
                    "created_at": now_ts,
                }
            },
        },
    }

    webhook_event = WebhookEvent(
        razorpay_event_id=f"evt_sim_{uuid.uuid4().hex[:16]}",
        event_type="payment_link.paid",
        payload=payload,
        processing_status="received",
        merchant_id=attempt.merchant_id,
    )
    db.add(webhook_event)
    db.flush()

    try:
        process_recovery_webhook(db=db, payload=payload)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=f"Simulated settlement rejected: {exc}")

    webhook_event.processing_status = "processed"
    webhook_event.processed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(attempt)

    return SimulatedPaymentResponse(
        recovery_id=attempt.recovery_id,
        status=attempt.status,
        recovered_amount=attempt.recovered_amount,
        payment_link_id=attempt.payment_link_id,
        resulting_payment_id=attempt.resulting_payment_id,
    )
