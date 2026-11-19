from datetime import datetime, timezone
import json
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.dependencies import get_db
from app.domains.payments.normalizer import normalize_payment_event
from app.domains.payments.service import process_payment_event
from app.integrations.razorpay.webhook import verify_webhook_signature
from app.models.webhook_event import WebhookEvent

logger = logging.getLogger(__name__)


def _as_dict(value) -> dict:
    """Razorpay sends empty objects (e.g. notes) as [] - normalise to dict."""
    return value if isinstance(value, dict) else {}


router = APIRouter(
    prefix="/webhooks",
    tags=["Webhooks"],
)


@router.post("/razorpay")
async def razorpay_webhook(
    request: Request,
    x_razorpay_signature: str | None = Header(default=None),
    x_razorpay_event_id: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    raw_body = await request.body()

    if not settings.razorpay_webhook_secret:
        # An empty secret makes every HMAC trivially forgeable - refuse instead.
        logger.error("RAZORPAY_WEBHOOK_SECRET is not configured; rejecting webhook.")
        raise HTTPException(
            status_code=503,
            detail="Webhook verification is not configured",
        )

    if not x_razorpay_signature:
        raise HTTPException(
            status_code=400,
            detail="Missing webhook signature",
        )

    if not x_razorpay_event_id:
        raise HTTPException(
            status_code=400,
            detail="Missing webhook event ID",
        )

    is_valid = verify_webhook_signature(
        raw_body,
        x_razorpay_signature,
        settings.razorpay_webhook_secret,
    )

    if not is_valid:
        raise HTTPException(
            status_code=401,
            detail="Invalid webhook signature",
        )

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=400,
            detail="Invalid JSON payload",
        )

    event_type = payload.get("event")

    if not event_type:
        raise HTTPException(
            status_code=400,
            detail="Missing event type",
        )

    existing_event = db.scalar(
        select(WebhookEvent)
        .where(WebhookEvent.razorpay_event_id == x_razorpay_event_id)
        .with_for_update()
    )

    if existing_event:
        if existing_event.processing_status == "processed":
            return {
                "status": "duplicate",
                "event_id": x_razorpay_event_id,
            }

        webhook_event = existing_event
        webhook_event.processing_status = "received"
        webhook_event.error_message = None
        db.add(webhook_event)
        db.commit()
        db.refresh(webhook_event)
    else:
        merchant_id: UUID | None = None

        # 1. Authoritative resolution for payment_link.paid via RecoveryAttempt
        if event_type == "payment_link.paid":
            plink_entity = _as_dict(_as_dict(_as_dict(payload.get("payload")).get("payment_link")).get("entity"))
            ref_id = plink_entity.get("reference_id") or _as_dict(plink_entity.get("notes")).get("recovery_id")
            if ref_id:
                from app.models.recovery_attempt import RecoveryAttempt
                attempt = db.scalar(
                    select(RecoveryAttempt).where(RecoveryAttempt.recovery_id == ref_id)
                )
                if attempt:
                    merchant_id = attempt.merchant_id

        # 2. Resolution for payment events via payment notes
        if not merchant_id:
            payment_entity = _as_dict(_as_dict(_as_dict(payload.get("payload")).get("payment")).get("entity"))
            notes = _as_dict(payment_entity.get("notes"))
            if "merchant_id" in notes:
                try:
                    merchant_id = UUID(str(notes["merchant_id"]))
                except (ValueError, TypeError):
                    pass

        # 3. Fallback to configured dev_merchant_id for backwards compatibility in single-tenant dev mode
        if not merchant_id and settings.dev_merchant_id:
            try:
                merchant_id = UUID(settings.dev_merchant_id)
            except (ValueError, AttributeError) as exc:
                raise HTTPException(
                    status_code=500,
                    detail="Invalid DEV_MERCHANT_ID configuration",
                ) from exc

        webhook_event = WebhookEvent(
            razorpay_event_id=x_razorpay_event_id,
            event_type=event_type,
            payload=payload,
            processing_status="received",
            merchant_id=merchant_id,
        )

        db.add(webhook_event)
        try:
            db.commit()
        except IntegrityError:
            # A concurrent delivery of the same event inserted it first.
            db.rollback()
            return {
                "status": "duplicate",
                "event_id": x_razorpay_event_id,
            }
        db.refresh(webhook_event)

    if webhook_event.merchant_id is None and event_type != "payment_link.paid":
        # Cannot attribute this payment to a merchant. Record it and acknowledge
        # (a 5xx would make Razorpay retry an unprocessable event forever).
        webhook_event.processing_status = "ignored"
        webhook_event.error_message = "No merchant could be resolved for this event"
        webhook_event.processed_at = datetime.now(timezone.utc)
        db.commit()
        logger.warning("Ignoring webhook %s: no merchant resolved", x_razorpay_event_id)
        return {
            "status": "ignored",
            "event_id": x_razorpay_event_id,
        }

    try:
        if event_type == "payment_link.paid":
            from app.domains.recovery.service import process_recovery_webhook
            process_recovery_webhook(db=db, payload=payload)
        else:
            normalized = normalize_payment_event(payload)

            process_payment_event(
                db=db,
                webhook_event=webhook_event,
                normalized=normalized,
            )

        webhook_event.processing_status = "processed"
        webhook_event.processed_at = datetime.now(timezone.utc)
        db.commit()

    except Exception as exc:
        db.rollback()

        webhook_event.processing_status = "failed"
        webhook_event.error_message = str(exc)
        webhook_event.processed_at = datetime.now(timezone.utc)
        db.add(webhook_event)
        db.commit()

        logger.exception("Webhook %s processing failed", x_razorpay_event_id)
        raise HTTPException(
            status_code=500,
            detail="Downstream payment processing failed",
        ) from exc

    return {
        "status": "received",
        "event_id": x_razorpay_event_id,
    }