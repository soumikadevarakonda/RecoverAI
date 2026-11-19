from datetime import datetime, timedelta, timezone
import uuid
import pytest
from unittest.mock import MagicMock
from fastapi.testclient import TestClient

from app.db.session import SessionLocal
from app.main import app
from app.integrations.razorpay import RazorpayClient
from app.models.merchant import Merchant
from app.models.incident import Incident
from app.models.payment import Payment
from app.models.payment_event import PaymentEvent
from app.models.webhook_event import WebhookEvent
from app.models.recovery_policy import RecoveryPolicy
from app.models.recovery_attempt import RecoveryAttempt
from app.models.recovery_campaign import RecoveryCampaign
from app.models.diagnosis import Diagnosis


@pytest.fixture
def db_session():
    session = SessionLocal()
    try:
        from app.models.recovery_audit_event import RecoveryAuditEvent
        session.query(RecoveryAuditEvent).delete()
        session.query(RecoveryAttempt).delete()
        session.query(RecoveryCampaign).delete()
        session.query(RecoveryPolicy).delete()
        session.query(Diagnosis).delete()
        session.query(Incident).delete()
        session.query(PaymentEvent).delete()
        session.query(Payment).delete()
        session.query(WebhookEvent).delete()
        session.commit()
        yield session
    finally:
        session.close()


@pytest.fixture
def create_merchant(db_session):
    def _create(name: str = "Merchant"):
        merchant = Merchant(name=name)
        db_session.add(merchant)
        db_session.commit()
        db_session.refresh(merchant)
        return merchant
    return _create


@pytest.fixture
def create_incident_with_diag(db_session):
    def _create(merchant_id, status="detected", revenue_at_risk=10000):
        now = datetime.now(timezone.utc)
        incident = Incident(
            merchant_id=merchant_id,
            method="upi",
            bank="HDFC",
            error_code="GATEWAY_ERROR",
            error_step="payment_authorization",
            current_total_count=30,
            current_failed_count=10,
            current_failure_rate=0.3333,
            current_total_amount=30000,
            current_failed_amount=10000,
            baseline_total_count=30,
            baseline_failed_count=1,
            baseline_failure_rate=0.0333,
            baseline_total_amount=30000,
            baseline_failed_amount=1000,
            absolute_rate_increase=0.3,
            relative_degradation=10.0,
            revenue_at_risk=revenue_at_risk,
            window_start=now - timedelta(minutes=30),
            window_end=now,
            baseline_start=now - timedelta(hours=2),
            baseline_end=now - timedelta(hours=1),
            status=status,
        )
        db_session.add(incident)
        db_session.commit()
        db_session.refresh(incident)

        diagnosis = Diagnosis(
            incident_id=incident.id,
            diagnosis_type="error-code spike",
            explanation="Spike in gateway error code",
            supporting_evidence={},
            confidence=0.8,
        )
        db_session.add(diagnosis)
        db_session.commit()

        return incident
    return _create


def test_dashboard_summary(db_session, create_merchant, create_incident_with_diag):
    merchant = create_merchant()
    incident = create_incident_with_diag(merchant.id, status="detected", revenue_at_risk=10000)

    # Add a failed payment
    payment = Payment(
        merchant_id=merchant.id,
        razorpay_payment_id="pay_fail_1",
        amount=10000,
        currency="INR",
        status="failed",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(payment)

    # Add a recovered attempt
    attempt = RecoveryAttempt(
        recovery_id="rec_sum_1",
        merchant_id=merchant.id,
        incident_id=incident.id,
        selected_action="incentive",
        status="recovered",
        recovered_amount=8000,
    )
    db_session.add(attempt)
    db_session.commit()

    client = TestClient(app)
    response = client.get(
        "/api/v1/merchant/dashboard/summary",
        headers={"X-Merchant-ID": str(merchant.id)},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["revenue_at_risk"] == 10000
    assert data["recovered_revenue"] == 8000
    assert data["active_incidents"] == 1
    assert data["failed_payments"] == 1
    assert data["recovery_attempts"] == 1
    assert data["recovery_rate"] == 8000.0 / 10000.0


def test_incident_list_and_detail(db_session, create_merchant, create_incident_with_diag):
    merchant = create_merchant()
    incident = create_incident_with_diag(merchant.id, status="detected", revenue_at_risk=15000)

    client = TestClient(app)

    # List endpoint
    response = client.get(
        "/api/v1/merchant/incidents",
        headers={"X-Merchant-ID": str(merchant.id)},
    )
    assert response.status_code == 200
    inc_list = response.json()
    assert len(inc_list) == 1
    assert inc_list[0]["id"] == str(incident.id)
    assert inc_list[0]["revenue_at_risk"] == 15000
    assert inc_list[0]["diagnosis"]["diagnosis_type"] == "error-code spike"

    # Detail endpoint
    response_detail = client.get(
        f"/api/v1/merchant/incidents/{incident.id}",
        headers={"X-Merchant-ID": str(merchant.id)},
    )
    assert response_detail.status_code == 200
    inc_detail = response_detail.json()
    assert inc_detail["id"] == str(incident.id)
    assert inc_detail["diagnosis"]["diagnosis_type"] == "error-code spike"
    assert len(inc_detail["recovery_attempts"]) == 0


def test_recovery_list_and_detail(db_session, create_merchant, create_incident_with_diag):
    merchant = create_merchant()
    incident = create_incident_with_diag(merchant.id)

    attempt = RecoveryAttempt(
        recovery_id="rec_test_detail",
        merchant_id=merchant.id,
        incident_id=incident.id,
        selected_action="retry",
        status="pending",
    )
    db_session.add(attempt)
    db_session.commit()

    client = TestClient(app)

    # List recovery
    response = client.get(
        "/api/v1/merchant/recoveries",
        headers={"X-Merchant-ID": str(merchant.id)},
    )
    assert response.status_code == 200
    rec_list = response.json()
    assert len(rec_list) == 1
    assert rec_list[0]["recovery_id"] == "rec_test_detail"

    # Detail recovery by string recovery_id
    response_detail = client.get(
        "/api/v1/merchant/recoveries/rec_test_detail",
        headers={"X-Merchant-ID": str(merchant.id)},
    )
    assert response_detail.status_code == 200
    assert response_detail.json()["recovery_id"] == "rec_test_detail"

    # Detail recovery by UUID id
    response_detail_uuid = client.get(
        f"/api/v1/merchant/recoveries/{attempt.id}",
        headers={"X-Merchant-ID": str(merchant.id)},
    )
    assert response_detail_uuid.status_code == 200
    assert response_detail_uuid.json()["id"] == str(attempt.id)


def test_merchant_isolation(db_session, create_merchant, create_incident_with_diag):
    merchant_a = create_merchant("Merchant A")
    merchant_b = create_merchant("Merchant B")

    incident_a = create_incident_with_diag(merchant_a.id)

    attempt_a = RecoveryAttempt(
        recovery_id="rec_a_private",
        merchant_id=merchant_a.id,
        incident_id=incident_a.id,
        selected_action="grace_period",
        status="pending",
    )
    db_session.add(attempt_a)
    db_session.commit()

    client = TestClient(app)

    # Merchant B should not be able to list Merchant A's incident
    response = client.get(
        "/api/v1/merchant/incidents",
        headers={"X-Merchant-ID": str(merchant_b.id)},
    )
    assert response.status_code == 200
    assert len(response.json()) == 0

    # Merchant B should get 404 on Merchant A's specific incident
    response_detail = client.get(
        f"/api/v1/merchant/incidents/{incident_a.id}",
        headers={"X-Merchant-ID": str(merchant_b.id)},
    )
    assert response_detail.status_code == 404

    # Merchant B should get 404 on Merchant A's specific recovery attempt
    response_rec = client.get(
        "/api/v1/merchant/recoveries/rec_a_private",
        headers={"X-Merchant-ID": str(merchant_b.id)},
    )
    assert response_rec.status_code == 404


def test_approval_state_transition(db_session, create_merchant, create_incident_with_diag):
    merchant = create_merchant()
    incident = create_incident_with_diag(merchant.id)

    attempt = RecoveryAttempt(
        recovery_id="rec_to_approve",
        merchant_id=merchant.id,
        incident_id=incident.id,
        selected_action="incentive",
        status="pending",
    )
    db_session.add(attempt)
    db_session.commit()

    client = TestClient(app)

    # Successful approval transition
    response = client.post(
        "/api/v1/merchant/recoveries/rec_to_approve/approve",
        headers={"X-Merchant-ID": str(merchant.id)},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "approved"

    # Re-approving now should fail with 400 Bad Request
    response_again = client.post(
        "/api/v1/merchant/recoveries/rec_to_approve/approve",
        headers={"X-Merchant-ID": str(merchant.id)},
    )
    assert response_again.status_code == 400


def test_execution_of_approved_recovery(db_session, create_merchant, create_incident_with_diag, monkeypatch):
    merchant = create_merchant()
    incident = create_incident_with_diag(merchant.id)

    pay = Payment(
        merchant_id=merchant.id,
        razorpay_payment_id="pay_to_exec_123",
        amount=10000,
        currency="INR",
        status="failed",
        method="upi",
        bank="HDFC",
    )
    db_session.add(pay)
    db_session.commit()
    db_session.refresh(pay)

    attempt = RecoveryAttempt(
        recovery_id="rec_to_execute",
        merchant_id=merchant.id,
        incident_id=incident.id,
        payment_id=pay.id,
        selected_action="grace_period",
        status="approved",
    )
    db_session.add(attempt)
    db_session.commit()

    # Mock client call
    mock_client_instance = MagicMock(spec=RazorpayClient)
    mock_client_instance.create_payment_link.return_value = {
        "id": "plink_exec_api_123",
        "short_url": "https://rzp.io/i/exec123",
        "status": "created",
    }
    monkeypatch.setattr(
        "app.integrations.razorpay.RazorpayClient",
        lambda *args, **kwargs: mock_client_instance,
    )

    client = TestClient(app)

    response = client.post(
        "/api/v1/merchant/recoveries/rec_to_execute/execute",
        headers={"X-Merchant-ID": str(merchant.id)},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "executed"
    assert data["payment_link_id"] == "plink_exec_api_123"
    assert data["short_url"] == "https://rzp.io/i/exec123"
    mock_client_instance.create_payment_link.assert_called_once()


def test_invalid_recovery_id(db_session, create_merchant):
    merchant = create_merchant()
    client = TestClient(app)

    # 404 for nonexistent id
    response = client.get(
        "/api/v1/merchant/recoveries/nonexistent_id",
        headers={"X-Merchant-ID": str(merchant.id)},
    )
    assert response.status_code == 404


def test_invalid_state_transitions(db_session, create_merchant, create_incident_with_diag):
    merchant = create_merchant()
    incident = create_incident_with_diag(merchant.id)

    attempt = RecoveryAttempt(
        recovery_id="rec_invalid_transition",
        merchant_id=merchant.id,
        incident_id=incident.id,
        selected_action="retry",
        status="pending", # not approved
    )
    db_session.add(attempt)
    db_session.commit()

    client = TestClient(app)

    # Executing a pending (unapproved) attempt must fail with 400
    response = client.post(
        "/api/v1/merchant/recoveries/rec_invalid_transition/execute",
        headers={"X-Merchant-ID": str(merchant.id)},
    )
    assert response.status_code == 400


def test_production_auth_mode_requires_bearer_token(db_session, create_merchant, monkeypatch):
    from app.core.config import settings
    merchant = create_merchant()
    client = TestClient(app)

    monkeypatch.setattr(settings, "auth_mode", "production")

    # In production auth mode, unsigned X-Merchant-ID must be rejected with 401 Unauthorized
    resp = client.get(
        "/api/v1/merchant/dashboard/summary",
        headers={"X-Merchant-ID": str(merchant.id)},
    )
    assert resp.status_code == 401
    assert "Bearer token" in resp.json()["detail"]

    # In production auth mode, valid Bearer token for existing merchant must succeed
    resp_authed = client.get(
        "/api/v1/merchant/dashboard/summary",
        headers={"Authorization": f"Bearer {merchant.id}"},
    )
    assert resp_authed.status_code == 200
