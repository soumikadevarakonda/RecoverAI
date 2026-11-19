"""
Database-free tests for signed merchant tokens, webhook secret enforcement,
CORS, dev-endpoint gating and webhook ordering rules.
"""
import uuid

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.security import (
    TokenSecretNotConfiguredError,
    issue_merchant_token,
    verify_merchant_token,
)
from app.domains.payments.service import _is_status_regression
from app.main import app


SECRET = "unit-test-secret"


def test_token_roundtrip():
    merchant_id = uuid.uuid4()
    token = issue_merchant_token(merchant_id, secret=SECRET)
    assert verify_merchant_token(token, secret=SECRET) == merchant_id


def test_bare_uuid_is_not_a_credential():
    assert verify_merchant_token(str(uuid.uuid4()), secret=SECRET) is None


def test_forged_and_wrong_secret_tokens_rejected():
    merchant_id = uuid.uuid4()
    token = issue_merchant_token(merchant_id, secret=SECRET)
    assert verify_merchant_token(f"{merchant_id}.{'0' * 64}", secret=SECRET) is None
    assert verify_merchant_token(token, secret="another-secret") is None
    # A token for merchant A cannot be replayed as merchant B
    other = uuid.uuid4()
    signature = token.split(".", 1)[1]
    assert verify_merchant_token(f"{other}.{signature}", secret=SECRET) is None


@pytest.mark.parametrize("garbage", ["", ".", "abc.def", "not-a-uuid.deadbeef", "a" * 500])
def test_garbage_tokens_rejected(garbage):
    assert verify_merchant_token(garbage, secret=SECRET) is None


def test_missing_secret_raises_instead_of_accepting():
    with pytest.raises(TokenSecretNotConfiguredError):
        issue_merchant_token(uuid.uuid4(), secret="")
    with pytest.raises(TokenSecretNotConfiguredError):
        verify_merchant_token("x.y", secret="")


def test_webhook_rejected_when_secret_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "razorpay_webhook_secret", "")
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/api/v1/webhooks/razorpay",
        content=b"{}",
        headers={"x-razorpay-signature": "anything", "x-razorpay-event-id": "evt_1"},
    )
    assert resp.status_code == 503


def test_cors_allows_configured_origin_only():
    client = TestClient(app)
    ok = client.options(
        "/api/v1/merchant/dashboard/summary",
        headers={
            "Origin": "http://127.0.0.1:5173",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "x-merchant-id",
        },
    )
    assert ok.status_code == 200
    assert ok.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"

    evil = client.options(
        "/api/v1/merchant/dashboard/summary",
        headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "GET"},
    )
    assert "access-control-allow-origin" not in evil.headers


def test_dev_endpoint_disabled_in_production(monkeypatch):
    from app.api.routes.dev import require_development_mode

    monkeypatch.setattr(settings, "environment", "production")
    with pytest.raises(HTTPException) as exc:
        require_development_mode()
    assert exc.value.status_code == 404

    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "auth_mode", "production")
    with pytest.raises(HTTPException):
        require_development_mode()


def test_production_auth_without_credentials_is_401(monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", "production")
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get(
        "/api/v1/merchant/dashboard/summary",
        headers={"X-Merchant-ID": str(uuid.uuid4())},
    )
    assert resp.status_code == 401


def test_late_events_never_regress_payment_status():
    assert _is_status_regression("captured", "failed") is True
    assert _is_status_regression("authorized", "failed") is True
    assert _is_status_regression("failed", "captured") is False
    assert _is_status_regression("captured", "captured") is False
    assert _is_status_regression(None, "failed") is False
