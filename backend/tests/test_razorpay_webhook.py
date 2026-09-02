from datetime import datetime, timedelta, timezone
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

    from uuid import UUID
    merchant_exists = False
    if settings.dev_merchant_id:
        try:
            m_uuid = UUID(settings.dev_merchant_id)
            merchant_exists = db_session.get(Merchant, m_uuid) is not None
        except ValueError:
            pass

    if not settings.dev_merchant_id or not merchant_exists:
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


def test_webhook_merchant_attribution_from_recovery_attempt(client, db_session):
    """
    Verifies that WebhookEvent.merchant_id is authoritatively resolved from the
    RecoveryAttempt for payment_link.paid events, instead of falling back to DEV_MERCHANT_ID.
    """
    from app.models.recovery_attempt import RecoveryAttempt
    from app.models.payment import Payment
    from app.models.incident import Incident

    # Create distinct Merchant B
    merchant_b = Merchant(name="Tenant Merchant B")
    db_session.add(merchant_b)
    db_session.commit()
    db_session.refresh(merchant_b)

    # Make sure DEV_MERCHANT_ID is NOT merchant_b
    assert str(merchant_b.id) != settings.dev_merchant_id

    now = datetime.now(timezone.utc)
    inc_b = Incident(
        merchant_id=merchant_b.id,
        method="upi",
        bank="HDFC",
        error_code="GATEWAY_ERROR",
        error_step="payment_authorization",
        current_total_count=10,
        current_failed_count=1,
        current_failure_rate=0.1,
        current_total_amount=15000,
        current_failed_amount=15000,
        baseline_total_count=10,
        baseline_failed_count=0,
        baseline_failure_rate=0.0,
        baseline_total_amount=15000,
        baseline_failed_amount=0,
        absolute_rate_increase=0.1,
        relative_degradation=1.0,
        revenue_at_risk=15000,
        window_start=now - timedelta(minutes=30),
        window_end=now,
        baseline_start=now - timedelta(hours=2),
        baseline_end=now - timedelta(hours=1),
        status="detected",
    )
    db_session.add(inc_b)
    db_session.flush()

    pay_id_failed = f"pay_tb_{uuid.uuid4().hex[:10]}"
    pay_b = Payment(
        merchant_id=merchant_b.id,
        razorpay_payment_id=pay_id_failed,
        amount=15000,
        currency="INR",
        status="failed",
    )
    db_session.add(pay_b)
    db_session.commit()

    rec_id = f"rec_attrib_{uuid.uuid4().hex[:8]}"
    plink_id = f"plink_attrib_{uuid.uuid4().hex[:8]}"
    attempt = RecoveryAttempt(
        recovery_id=rec_id,
        merchant_id=merchant_b.id,
        incident_id=inc_b.id,
        payment_id=pay_b.id,
        payment_link_id=plink_id,
        selected_action="retry",
        incentive_amount=0,
        status="executed",
    )
    db_session.add(attempt)
    db_session.commit()

    event_id = f"evt_attrib_{uuid.uuid4().hex[:12]}"
    pay_id_captured = f"pay_hook_{uuid.uuid4().hex[:10]}"
    payload = {
        "entity": "event",
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": plink_id,
                    "reference_id": rec_id,
                    "status": "paid",
                    "amount_paid": 15000,
                }
            },
            "payment": {
                "entity": {
                    "id": pay_id_captured,
                    "amount": 15000,
                    "status": "captured",
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

    resp = client.post("/api/v1/webhooks/razorpay", content=body, headers=headers)
    assert resp.status_code == 200

    # Verify WebhookEvent in database was attributed to Merchant B!
    webhook_ev = db_session.scalar(
        select(WebhookEvent).where(WebhookEvent.razorpay_event_id == event_id)
    )
    assert webhook_ev is not None
    assert webhook_ev.merchant_id == merchant_b.id
