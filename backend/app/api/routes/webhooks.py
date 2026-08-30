from datetime import datetime, timezone
import json
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.dependencies import get_db
from app.domains.payments.normalizer import normalize_payment_event
from app.domains.payments.service import process_payment_event
from app.integrations.razorpay.webhook import verify_webhook_signature
from app.models.webhook_event import WebhookEvent

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

    merchant_id: UUID | None = None
    if settings.dev_merchant_id:
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
    db.commit()
    db.refresh(webhook_event)

    try:
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

        raise HTTPException(
            status_code=500,
            detail=f"Downstream payment processing failed: {exc}",
        ) from exc

    return {
        "status": "received",
        "event_id": x_razorpay_event_id,
    }
