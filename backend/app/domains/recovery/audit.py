from enum import Enum
from typing import Any
import uuid
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.recovery_audit_event import RecoveryAuditEvent


class RecoveryAuditEventType(str, Enum):
    INCIDENT_DETECTED = "INCIDENT_DETECTED"
    DIAGNOSIS_CREATED = "DIAGNOSIS_CREATED"
    STRATEGY_EVALUATED = "STRATEGY_EVALUATED"
    AI_RECOMMENDATION = "AI_RECOMMENDATION"
    AI_FALLBACK = "AI_FALLBACK"
    GUARDRAIL_EVALUATED = "GUARDRAIL_EVALUATED"
    GUARDRAIL_BLOCKED = "GUARDRAIL_BLOCKED"
    GUARDRAIL_APPROVAL_REQUIRED = "GUARDRAIL_APPROVAL_REQUIRED"
    GUARDRAIL_APPROVED = "GUARDRAIL_APPROVED"
    CAMPAIGN_CREATED = "CAMPAIGN_CREATED"
    CAMPAIGN_APPROVED = "CAMPAIGN_APPROVED"
    CAMPAIGN_REJECTED = "CAMPAIGN_REJECTED"
    RECOVERY_APPROVED = "RECOVERY_APPROVED"
    RECOVERY_EXECUTION_STARTED = "RECOVERY_EXECUTION_STARTED"
    PAYMENT_LINK_CREATED = "PAYMENT_LINK_CREATED"
    PAYMENT_LINK_EXECUTION_FAILED = "PAYMENT_LINK_EXECUTION_FAILED"
    WEBHOOK_RECEIVED = "WEBHOOK_RECEIVED"
    WEBHOOK_VERIFIED = "WEBHOOK_VERIFIED"
    RECOVERY_RECORDED = "RECOVERY_RECORDED"
    RECOVERY_FAILED = "RECOVERY_FAILED"
    CAMPAIGN_COMPLETED = "CAMPAIGN_COMPLETED"
    ADAPTIVE_ANALYSIS_COMPLETED = "ADAPTIVE_ANALYSIS_COMPLETED"
    ADAPTIVE_ANALYSIS_REJECTED = "ADAPTIVE_ANALYSIS_REJECTED"
    LEARNING_SIGNAL_RECORDED = "LEARNING_SIGNAL_RECORDED"
    OPERATIONAL_MEMORY_RETRIEVED = "OPERATIONAL_MEMORY_RETRIEVED"


SENSITIVE_KEYS = {
    "key_id",
    "key_secret",
    "secret",
    "api_key",
    "authorization",
    "token",
    "password",
    "signature",
    "card_number",
    "cvv",
}


from decimal import Decimal


def _sanitize_evidence(data: Any) -> Any:
    """
    Recursively strips secrets, authorization tokens, and credentials from audit payloads,
    and converts non-JSON-serializable primitives (like Decimal) into JSON-safe types.
    """
    if isinstance(data, dict):
        sanitized = {}
        for k, v in data.items():
            if any(sub in k.lower() for sub in SENSITIVE_KEYS):
                sanitized[k] = "[REDACTED]"
            else:
                sanitized[k] = _sanitize_evidence(v)
        return sanitized
    elif isinstance(data, list):
        return [_sanitize_evidence(item) for item in data]
    elif isinstance(data, tuple):
        return tuple(_sanitize_evidence(item) for item in data)
    elif isinstance(data, Decimal):
        return int(data) if data % 1 == 0 else float(data)
    return data


def record_audit_event(
    db: Session,
    merchant_id: uuid.UUID,
    event_type: RecoveryAuditEventType | str,
    actor_type: str = "system",
    actor_id: str | None = None,
    incident_id: uuid.UUID | None = None,
    campaign_id: uuid.UUID | None = None,
    recovery_attempt_id: uuid.UUID | None = None,
    payment_id: uuid.UUID | None = None,
    previous_state: str | None = None,
    new_state: str | None = None,
    reason_code: str | None = None,
    explanation: str | None = None,
    evidence: dict[str, Any] | None = None,
    flush: bool = True,
) -> RecoveryAuditEvent:
    """
    Appends a new chronological audit event to the ledger.
    Guarantees sanitized evidence and validates merchant assignment.
    """
    if not merchant_id:
        raise ValueError("merchant_id is strictly required for every audit event")

    clean_evidence = _sanitize_evidence(evidence or {})
    event_str = event_type.value if isinstance(event_type, RecoveryAuditEventType) else str(event_type)

    audit_event = RecoveryAuditEvent(
        merchant_id=merchant_id,
        incident_id=incident_id,
        campaign_id=campaign_id,
        recovery_attempt_id=recovery_attempt_id,
        payment_id=payment_id,
        event_type=event_str,
        actor_type=actor_type,
        actor_id=actor_id,
        previous_state=previous_state,
        new_state=new_state,
        reason_code=reason_code,
        explanation=explanation,
        evidence=clean_evidence,
    )
    db.add(audit_event)
    if flush:
        db.flush()
    return audit_event


def get_recovery_audit_trail(
    db: Session,
    merchant_id: uuid.UUID,
    recovery_attempt_id: uuid.UUID | None = None,
    campaign_id: uuid.UUID | None = None,
    payment_id: uuid.UUID | None = None,
    incident_id: uuid.UUID | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[RecoveryAuditEvent]:
    """
    Retrieves chronological audit trail strictly isolated to the specified merchant.
    Allows filtering by attempt, campaign, payment, or incident.
    """
    stmt = (
        select(RecoveryAuditEvent)
        .where(RecoveryAuditEvent.merchant_id == merchant_id)
        .order_by(RecoveryAuditEvent.created_at.asc(), RecoveryAuditEvent.id.asc())
        .limit(limit)
        .offset(offset)
    )

    if recovery_attempt_id is not None:
        stmt = stmt.where(RecoveryAuditEvent.recovery_attempt_id == recovery_attempt_id)
    if campaign_id is not None:
        stmt = stmt.where(RecoveryAuditEvent.campaign_id == campaign_id)
    if payment_id is not None:
        stmt = stmt.where(RecoveryAuditEvent.payment_id == payment_id)
    if incident_id is not None:
        stmt = stmt.where(RecoveryAuditEvent.incident_id == incident_id)

    return list(db.scalars(stmt).all())

