from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.payment import Payment
from app.models.payment_event import PaymentEvent
from app.models.webhook_event import WebhookEvent
from app.domains.payments.normalizer import NormalizedPaymentEvent


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
        payment = Payment(
            merchant_id=webhook_event.merchant_id,
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

    else:
        payment.status = normalized.status
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