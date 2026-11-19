from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.payment import Payment
from app.models.payment_event import PaymentEvent
from app.models.webhook_event import WebhookEvent
from app.domains.payments.normalizer import NormalizedPaymentEvent

# Razorpay payment lifecycle order. Webhooks can arrive out of order, so a
# late event must never move a payment back to an earlier state (e.g. a
# delayed payment.failed arriving after payment.captured).
STATUS_PRECEDENCE = {
    "created": 0,
    "failed": 1,
    "authorized": 2,
    "captured": 3,
    "refunded": 4,
}


def _is_status_regression(current: str | None, incoming: str | None) -> bool:
    return STATUS_PRECEDENCE.get(incoming or "", 0) < STATUS_PRECEDENCE.get(current or "", 0)


def process_payment_event(
    db: Session,
    webhook_event: WebhookEvent,
    normalized: NormalizedPaymentEvent,
) -> Payment:

    payment = db.scalar(
        select(Payment).where(
            Payment.razorpay_payment_id
            == normalized.razorpay_payment_id
        )
    )

    if payment is None:
        if webhook_event.merchant_id is None:
            raise ValueError("Cannot record payment: webhook event is not associated with a merchant")
        payment = Payment(
            merchant_id=webhook_event.merchant_id,
            # Use the gateway's event time, not ingestion time, so cohort
            # windows stay correct for delayed or replayed webhooks.
            created_at=normalized.occurred_at,
            razorpay_payment_id=normalized.razorpay_payment_id,
            razorpay_order_id=normalized.razorpay_order_id,
            amount=normalized.amount,
            currency=normalized.currency,
            status=normalized.status,
            method=normalized.method,
            bank=normalized.bank,
            error_code=normalized.error_code,
            error_description=normalized.error_description,
            error_source=normalized.error_source,
            error_step=normalized.error_step,
            error_reason=normalized.error_reason,
        )

        db.add(payment)
        db.flush()

    elif not _is_status_regression(payment.status, normalized.status):
        payment.status = normalized.status
        payment.method = normalized.method or payment.method
        payment.bank = normalized.bank or payment.bank
        payment.error_code = normalized.error_code
        payment.error_description = normalized.error_description
        payment.error_source = normalized.error_source
        payment.error_step = normalized.error_step
        payment.error_reason = normalized.error_reason

    payment_event = db.scalar(
        select(PaymentEvent).where(
            PaymentEvent.webhook_event_id == webhook_event.id
        )
    )

    if payment_event is None:
        payment_event = PaymentEvent(
            payment_id=payment.id,
            webhook_event_id=webhook_event.id,
            event_type=webhook_event.event_type,
            status=normalized.status,
            amount=normalized.amount,
            method=normalized.method,
            bank=normalized.bank,
            error_code=normalized.error_code,
            error_source=normalized.error_source,
            error_step=normalized.error_step,
            error_reason=normalized.error_reason,
            occurred_at=normalized.occurred_at,
        )
        db.add(payment_event)
    else:
        payment_event.payment_id = payment.id
        payment_event.event_type = webhook_event.event_type
        payment_event.status = normalized.status
        payment_event.amount = normalized.amount
        payment_event.method = normalized.method
        payment_event.bank = normalized.bank
        payment_event.error_code = normalized.error_code
        payment_event.error_source = normalized.error_source
        payment_event.error_step = normalized.error_step
        payment_event.error_reason = normalized.error_reason
        payment_event.occurred_at = normalized.occurred_at

    return payment