from pydantic import BaseModel, Field
from uuid import UUID
from datetime import datetime
from typing import List, Optional

class DashboardSummaryResponse(BaseModel):
    revenue_at_risk: int
    recovered_revenue: int
    recovery_rate: float
    active_incidents: int
    failed_payments: int
    recovery_attempts: int


class DiagnosisSummarySchema(BaseModel):
    diagnosis_type: str
    confidence: float
    explanation: str


class IncidentListResponse(BaseModel):
    id: UUID
    status: str
    method: Optional[str] = None
    bank: Optional[str] = None
    error_code: Optional[str] = None
    error_step: Optional[str] = None
    revenue_at_risk: int
    created_at: datetime
    diagnosis: Optional[DiagnosisSummarySchema] = None


class RecoveryAttemptSummarySchema(BaseModel):
    id: UUID
    recovery_id: str
    campaign_id: Optional[UUID] = None
    selected_action: str
    incentive_amount: int
    status: str
    recovered_amount: int
    payment_link_id: Optional[str] = None
    short_url: Optional[str] = None


class IncidentDetailResponse(BaseModel):
    id: UUID
    merchant_id: UUID
    status: str
    method: Optional[str] = None
    bank: Optional[str] = None
    error_code: Optional[str] = None
    error_step: Optional[str] = None
    current_total_count: int
    current_failed_count: int
    current_failure_rate: float
    current_total_amount: int
    current_failed_amount: int
    baseline_total_count: int
    baseline_failed_count: int
    baseline_failure_rate: float
    baseline_total_amount: int
    baseline_failed_amount: int
    absolute_rate_increase: float
    relative_degradation: float
    revenue_at_risk: int
    window_start: datetime
    window_end: datetime
    baseline_start: datetime
    baseline_end: datetime
    created_at: datetime
    updated_at: datetime
    diagnosis: Optional[DiagnosisSummarySchema] = None
    recovery_attempts: List[RecoveryAttemptSummarySchema] = []


class RecoveryAttemptDetailResponse(BaseModel):
    id: UUID
    recovery_id: str
    merchant_id: UUID
    incident_id: UUID
    campaign_id: Optional[UUID] = None
    payment_id: Optional[UUID] = None
    selected_action: str
    incentive_amount: int
    status: str
    created_at: datetime
    expires_at: Optional[datetime] = None
    recovered_amount: int
    resulting_payment_id: Optional[str] = None
    payment_link_id: Optional[str] = None
    short_url: Optional[str] = None
    decision_evidence: Optional[dict] = None


class RecoveryAuditEventResponse(BaseModel):
    id: UUID
    event_type: str
    actor_type: str
    actor_id: Optional[str] = None
    previous_state: Optional[str] = None
    new_state: Optional[str] = None
    reason_code: Optional[str] = None
    explanation: Optional[str] = None
    evidence: dict = {}
    created_at: datetime
    recovery_attempt_id: Optional[UUID] = None
    campaign_id: Optional[UUID] = None
    incident_id: Optional[UUID] = None
    payment_id: Optional[UUID] = None
