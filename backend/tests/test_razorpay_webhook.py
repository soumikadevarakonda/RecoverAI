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


def test_webhook_duplicate_event_handling(client):
    event_id = f"evt_dup_{uuid.uuid4().hex[:12]}"
    payment_id = f"pay_dup_{uuid.uuid4().hex[:12]}"
    payload = {
        "entity": "event",
        "event": "payment.failed",
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "amount": 10000,
                    "currency": "INR",
                    "status": "failed",
                    "created_at": 1700000000,
                }
            }
        },
    }

    body, sig = make_signed_payload(payload)
    headers = {
        "x-razorpay-signature": sig,
        "x-razorpay-event-id": event_id,
        "content-type": "application/json",
    }

    # First delivery
    resp1 = client.post("/api/v1/webhooks/razorpay", content=body, headers=headers)
    assert resp1.status_code == 200
    assert resp1.json()["status"] == "received"

    # Duplicate delivery
    resp2 = client.post("/api/v1/webhooks/razorpay", content=body, headers=headers)
    assert resp2.status_code == 200
    assert resp2.json()["status"] == "duplicate"
    assert resp2.json()["event_id"] == event_id


def test_webhook_downstream_failure_preserves_raw_event(client, db_session):
    event_id = f"evt_fail_{uuid.uuid4().hex[:12]}"
    # Malformed inner payment structure missing entity dict
    payload = {
        "entity": "event",
        "event": "payment.failed",
        "payload": {
            "payment": {}
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

    # Must return 500 and not swallow exception
    assert response.status_code == 500
    assert "Downstream payment processing failed" in response.json()["detail"]

    # Raw WebhookEvent must remain persisted with failed status and error message
    webhook_event = db_session.scalar(
        select(WebhookEvent).where(WebhookEvent.razorpay_event_id == event_id)
    )
    assert webhook_event is not None
    assert webhook_event.processing_status == "failed"
    assert webhook_event.error_message is not None
    assert "'entity'" in webhook_event.error_message
    assert webhook_event.processed_at is not None
    assert webhook_event.processed_at.tzinfo is not None


def test_failed_event_successful_retry(client, db_session):
    event_id = f"evt_retry_succ_{uuid.uuid4().hex[:12]}"
    payment_id = f"pay_retry_succ_{uuid.uuid4().hex[:12]}"

    # 1. First attempt fails due to malformed payload
    failed_payload = {
        "entity": "event",
        "event": "payment.failed",
        "payload": {"payment": {}},
    }
    body1, sig1 = make_signed_payload(failed_payload)
    resp1 = client.post(
        "/api/v1/webhooks/razorpay",
        content=body1,
        headers={
            "x-razorpay-signature": sig1,
            "x-razorpay-event-id": event_id,
            "content-type": "application/json",
        },
    )
    assert resp1.status_code == 500

    # Verify event is recorded as failed in DB
    ev = db_session.scalar(
        select(WebhookEvent).where(WebhookEvent.razorpay_event_id == event_id)
    )
    assert ev is not None
    assert ev.processing_status == "failed"
    assert ev.error_message is not None

    # 2. Razorpay retries same event_id with valid payload
    valid_payload = {
        "entity": "event",
        "event": "payment.failed",
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "order_id": "order_retry_1",
                    "amount": 75000,
                    "currency": "INR",
                    "status": "failed",
                    "created_at": 1700000000,
                }
            }
        },
    }
    body2, sig2 = make_signed_payload(valid_payload)
    resp2 = client.post(
        "/api/v1/webhooks/razorpay",
        content=body2,
        headers={
            "x-razorpay-signature": sig2,
            "x-razorpay-event-id": event_id,
            "content-type": "application/json",
        },
    )
    assert resp2.status_code == 200
    assert resp2.json()["status"] == "received"

    # Refresh and verify WebhookEvent transitioned to processed and cleared error
    db_session.refresh(ev)
    assert ev.processing_status == "processed"
    assert ev.error_message is None

    # Verify payment exists
    payment = db_session.scalar(
        select(Payment).where(Payment.razorpay_payment_id == payment_id)
    )
    assert payment is not None
    assert payment.amount == 75000

    # Verify exactly 1 PaymentEvent exists for this webhook event (no duplicates)
    payment_events = db_session.scalars(
        select(PaymentEvent).where(PaymentEvent.webhook_event_id == ev.id)
    ).all()
    assert len(payment_events) == 1


def test_failed_event_failed_retry(client, db_session):
    event_id = f"evt_retry_fail_{uuid.uuid4().hex[:12]}"

    failed_payload = {
        "entity": "event",
        "event": "payment.failed",
        "payload": {"payment": {}},
    }
    body, sig = make_signed_payload(failed_payload)
    headers = {
        "x-razorpay-signature": sig,
        "x-razorpay-event-id": event_id,
        "content-type": "application/json",
    }

    # Attempt 1
    resp1 = client.post("/api/v1/webhooks/razorpay", content=body, headers=headers)
    assert resp1.status_code == 500

    # Attempt 2 (Retry still fails)
    resp2 = client.post("/api/v1/webhooks/razorpay", content=body, headers=headers)
    assert resp2.status_code == 500

    # WebhookEvent remains failed with error recorded
    ev = db_session.scalar(
        select(WebhookEvent).where(WebhookEvent.razorpay_event_id == event_id)
    )
    assert ev is not None
    assert ev.processing_status == "failed"
    assert ev.error_message is not None


def test_processed_event_no_reprocessing(client, db_session):
    event_id = f"evt_noreprocess_{uuid.uuid4().hex[:12]}"
    payment_id = f"pay_noreprocess_{uuid.uuid4().hex[:12]}"

    payload = {
        "entity": "event",
        "event": "payment.failed",
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "amount": 33000,
                    "currency": "INR",
                    "status": "failed",
                    "created_at": 1700000000,
                }
            }
        },
    }
    body, sig = make_signed_payload(payload)
    headers = {
        "x-razorpay-signature": sig,
        "x-razorpay-event-id": event_id,
        "content-type": "application/json",
    }

    # Initial delivery
    resp1 = client.post("/api/v1/webhooks/razorpay", content=body, headers=headers)
    assert resp1.status_code == 200
    assert resp1.json()["status"] == "received"

    # Redelivery of processed event
    resp2 = client.post("/api/v1/webhooks/razorpay", content=body, headers=headers)
    assert resp2.status_code == 200
    assert resp2.json()["status"] == "duplicate"
    assert resp2.json()["event_id"] == event_id

    # Verify only 1 PaymentEvent exists in DB
    ev = db_session.scalar(
        select(WebhookEvent).where(WebhookEvent.razorpay_event_id == event_id)
    )
    payment_events = db_session.scalars(
        select(PaymentEvent).where(PaymentEvent.webhook_event_id == ev.id)
    ).all()
    assert len(payment_events) == 1
