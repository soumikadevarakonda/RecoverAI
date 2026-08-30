from datetime import datetime, timezone
import hashlib
import hmac
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import settings
from app.db.session import SessionLocal
from app.domains.payments.normalizer import normalize_payment_event
from app.integrations.razorpay.webhook import verify_webhook_signature
from app.main import app
from app.models.merchant import Merchant
from app.models.payment import Payment
from app.models.payment_event import PaymentEvent
from app.models.webhook_event import WebhookEvent


@pytest.fixture
def db_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(autouse=True)
def ensure_dev_merchant(db_session):
    # Ensure settings has a valid webhook secret and merchant for tests
    if not settings.razorpay_webhook_secret:
        settings.razorpay_webhook_secret = "test-webhook-secret"

    if not settings.dev_merchant_id:
        merchant = db_session.scalar(select(Merchant).limit(1))
        if not merchant:
            merchant = Merchant(name="Test Merchant")
            db_session.add(merchant)
            db_session.commit()
            db_session.refresh(merchant)
        settings.dev_merchant_id = str(merchant.id)


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


def make_signed_payload(payload_dict: dict, secret: str = None) -> tuple[bytes, str]:
    if secret is None:
        secret = settings.razorpay_webhook_secret
    body = json.dumps(payload_dict, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(
        secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()
    return body, signature


def test_valid_signature():
    secret = "test-secret"
    body = b'{"event":"payment.failed"}'

    signature = hmac.new(
        secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()

    assert verify_webhook_signature(
        body,
        signature,
        secret,
    )


def test_invalid_signature():
    secret = "test-secret"
    body = b'{"event":"payment.failed"}'

    assert not verify_webhook_signature(
        body,
        "definitely-invalid",
        secret,
    )


def test_tampered_payload():
    secret = "test-secret"

    original_body = b'{"event":"payment.failed","amount":100}'

    signature = hmac.new(
        secret.encode(),
        original_body,
        hashlib.sha256,
    ).hexdigest()

    tampered_body = b'{"event":"payment.failed","amount":999999}'

    assert not verify_webhook_signature(
        tampered_body,
        signature,
        secret,
    )


def test_normalizer_timezone_awareness():
    timestamp = 1700000000
    payload = {
        "event": "payment.failed",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_test_norm_123",
                    "amount": 250000,
                    "currency": "INR",
                    "status": "failed",
                    "created_at": timestamp,
                }
            }
        },
    }

    normalized = normalize_payment_event(payload)
    assert normalized.occurred_at.tzinfo == timezone.utc
    assert normalized.occurred_at == datetime.fromtimestamp(timestamp, tz=timezone.utc)
    assert normalized.amount == 250000


def test_webhook_invalid_signature_rejected(client):
    body = b'{"event":"payment.failed"}'
    response = client.post(
        "/api/v1/webhooks/razorpay",
        content=body,
        headers={
            "x-razorpay-signature": "invalid_signature",
            "x-razorpay-event-id": f"evt_{uuid.uuid4().hex[:12]}",
            "content-type": "application/json",
        },
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid webhook signature"


def test_webhook_successful_persistence(client, db_session):
    event_id = f"evt_success_{uuid.uuid4().hex[:12]}"
    payment_id = f"pay_succ_{uuid.uuid4().hex[:12]}"
    payload = {
        "entity": "event",
        "event": "payment.failed",
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "order_id": "order_success_123",
                    "amount": 49900,
                    "currency": "INR",
                    "status": "failed",
                    "method": "card",
                    "bank": "HDFC",
                    "error_code": "BAD_REQUEST_ERROR",
                    "error_description": "Card expired",
                    "error_source": "bank",
                    "error_step": "payment_authorization",
                    "error_reason": "payment_failed",
                    "created_at": 1700000000,
                }
            }
        },
    }

    body, sig = make_signed_payload(payload)
    response = client.post(
        "/api/v1/webhooks/razorpay",
        content=body,
        headers={
            "x-razorpay-signature": sig,
            "x-razorpay-event-id": event_id,
            "content-type": "application/json",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "received",
        "event_id": event_id,
    }

    # Verify WebhookEvent in DB
    webhook_event = db_session.scalar(
        select(WebhookEvent).where(WebhookEvent.razorpay_event_id == event_id)
    )
    assert webhook_event is not None
    assert webhook_event.processing_status == "processed"
    assert webhook_event.processed_at is not None
    assert webhook_event.processed_at.tzinfo is not None
    assert webhook_event.received_at.tzinfo is not None
    assert webhook_event.error_message is None

    # Verify Payment in DB
    payment = db_session.scalar(
        select(Payment).where(Payment.razorpay_payment_id == payment_id)
    )
    assert payment is not None
    assert payment.amount == 49900
    assert payment.status == "failed"
    assert payment.error_code == "BAD_REQUEST_ERROR"
    assert payment.created_at.tzinfo is not None

    # Verify PaymentEvent in DB
    payment_event = db_session.scalar(
        select(PaymentEvent).where(PaymentEvent.payment_id == payment.id)
    )
    assert payment_event is not None
    assert payment_event.webhook_event_id == webhook_event.id
    assert payment_event.occurred_at.tzinfo is not None


