from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass
class NormalizedPaymentEvent:
    razorpay_payment_id: str
    razorpay_order_id: str | None
    amount: int
    currency: str
    status: str
    method: str | None
    bank: str | None
    error_code: str | None
    error_description: str | None
    error_source: str | None
    error_step: str | None
    error_reason: str | None
    occurred_at: datetime


def normalize_payment_event(
    payload: dict[str, Any],
) -> NormalizedPaymentEvent:
    payment = payload["payload"]["payment"]["entity"]

    return NormalizedPaymentEvent(
        razorpay_payment_id=payment["id"],
        razorpay_order_id=payment.get("order_id"),
        amount=payment["amount"],
        currency=payment["currency"],
        status=payment["status"],
        method=payment.get("method"),
        bank=payment.get("bank"),
        error_code=payment.get("error_code"),
        error_description=payment.get("error_description"),
        error_source=payment.get("error_source"),
        error_step=payment.get("error_step"),
        error_reason=payment.get("error_reason"),
        occurred_at=datetime.fromtimestamp(
            payment["created_at"],
            tz=timezone.utc,
        ),
    )
