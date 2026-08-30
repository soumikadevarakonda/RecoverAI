from datetime import datetime, timedelta, timezone
import uuid
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.incident import Incident
from app.models.diagnosis import Diagnosis
from app.models.recovery_policy import RecoveryPolicy
from app.models.recovery_attempt import RecoveryAttempt


def decide_recovery_action(
    db: Session,
    incident: Incident,
    diagnosis: Diagnosis,
    policy: RecoveryPolicy,
) -> RecoveryAttempt:
    selected_action = "ops_review"
    incentive_amount = 0
    status = "pending"

    allowed_actions = policy.allowed_actions or []

    # Priority 1: retry
    if "retry" in allowed_actions and diagnosis.diagnosis_type not in [
        "bank-specific degradation",
        "payment-method degradation",
    ]:
        selected_action = "retry"
        status = "approved"

    # Priority 2: grace_period
    elif "grace_period" in allowed_actions:
        selected_action = "grace_period"
        status = "approved"

    # Priority 3: incentive
    elif "incentive" in allowed_actions and diagnosis.diagnosis_type != "insufficient evidence / unknown":
        proposed_incentive = 500  # Default deterministic incentive in minor units (paise)

        # Check policy max incentive
        within_max_incentive = proposed_incentive <= policy.max_incentive

        # Check exposure limit
        active_exposure = db.scalar(
            select(func.sum(RecoveryAttempt.incentive_amount)).where(
                RecoveryAttempt.merchant_id == incident.merchant_id,
                RecoveryAttempt.status.in_(["pending", "approved", "executed"]),
            )
        ) or 0
        within_exposure_limit = (active_exposure + proposed_incentive) <= policy.max_exposure

        if within_max_incentive and within_exposure_limit:
            selected_action = "incentive"
            incentive_amount = proposed_incentive
            
            # Check approval threshold (monetary limit in float format)
            if proposed_incentive > policy.approval_threshold:
                status = "pending"
            else:
                status = "approved"

    # Fallback to ops_review
    if selected_action == "ops_review":
        incentive_amount = 0
        status = "pending"

    attempt = RecoveryAttempt(
        recovery_id=f"rec_{uuid.uuid4().hex[:12]}",
        merchant_id=incident.merchant_id,
        incident_id=incident.id,
        payment_id=None,
        selected_action=selected_action,
        incentive_amount=incentive_amount,
        status=status,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        recovered_amount=0,
    )
    db.add(attempt)
    db.commit()
    db.refresh(attempt)
    return attempt


def execute_recovery_attempt(
    db: Session,
    attempt: RecoveryAttempt,
    client: RazorpayClient = None,
) -> RecoveryAttempt:
    from app.integrations.razorpay import RazorpayClient

    if attempt.status != "approved":
        raise ValueError("Only approved attempts can be executed")

    if client is None:
        client = RazorpayClient()

    original_amount = attempt.payment.amount if attempt.payment else 1000
    charge_amount = max(0, original_amount - attempt.incentive_amount)

    expire_by = int(attempt.expires_at.timestamp()) if attempt.expires_at else None

    link_response = client.create_payment_link(
        amount=charge_amount,
        reference_id=attempt.recovery_id,
        description=f"Payment recovery link for {attempt.recovery_id}",
        expire_by=expire_by,
    )

    attempt.payment_link_id = link_response["id"]
    attempt.short_url = link_response["short_url"]
    attempt.status = "executed"

    db.add(attempt)
    db.commit()
    db.refresh(attempt)
    return attempt


def process_recovery_webhook(db: Session, payload: dict) -> None:
    plink_entity = payload.get("payload", {}).get("payment_link", {}).get("entity", {})
    payment_entity = payload.get("payload", {}).get("payment", {}).get("entity", {})

    recovery_id = plink_entity.get("reference_id") or plink_entity.get("notes", {}).get("recovery_id")
    if not recovery_id:
        raise ValueError("Missing recovery_id or reference_id in payment link webhook")

    attempt = db.scalar(
        select(RecoveryAttempt)
        .where(RecoveryAttempt.recovery_id == recovery_id)
        .with_for_update()
    )
    if not attempt:
        raise ValueError(f"No RecoveryAttempt found for recovery_id: {recovery_id}")

    payment_link_id = plink_entity.get("id")
    if attempt.payment_link_id != payment_link_id:
        raise ValueError(
            f"Payment link ID {payment_link_id} does not match expected attempt link ID {attempt.payment_link_id}"
        )

    plink_status = plink_entity.get("status")
    payment_status = payment_entity.get("status")
    if plink_status != "paid" or payment_status not in ["authorized", "captured"]:
        raise ValueError("Payment link is not paid or associated payment is not captured/authorized")

    if attempt.status == "recovered":
        return

    attempt.status = "recovered"
    attempt.resulting_payment_id = payment_entity.get("id")
    attempt.recovered_amount = payment_entity.get("amount") or plink_entity.get("amount_paid") or 0

    db.add(attempt)
    db.flush()


def orchestrate_recovery(
    db: Session,
    incident: Incident,
    client: RazorpayClient = None,
) -> RecoveryAttempt:
    from app.domains.diagnosis.service import diagnose_incident
    from app.models.recovery_policy import RecoveryPolicy
    from app.integrations.razorpay import RazorpayClient

    # 1. Idempotency check: do not execute recovery if the incident has already been handled
    existing_attempt = db.scalar(
        select(RecoveryAttempt).where(RecoveryAttempt.incident_id == incident.id)
    )
    if existing_attempt:
        return existing_attempt

    # 2. Run Diagnosis
    diagnosis = diagnose_incident(db, incident)

    # 3. Load merchant RecoveryPolicy
    policy = db.scalar(
        select(RecoveryPolicy).where(RecoveryPolicy.merchant_id == incident.merchant_id)
    )
    if not policy:
        policy = RecoveryPolicy(
            merchant_id=incident.merchant_id,
            allowed_actions=[],
            max_incentive=0,
            max_exposure=0,
            approval_threshold=0.0,
        )

    # 4. Make recovery decision / create RecoveryAttempt
    attempt = decide_recovery_action(db, incident, diagnosis, policy)

    # 5. If approved for automatic execution, execute it
    if attempt.status == "approved" and attempt.selected_action != "ops_review":
        attempt = execute_recovery_attempt(db, attempt, client)

    return attempt
