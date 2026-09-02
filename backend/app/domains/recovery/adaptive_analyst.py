import json
import logging
import time
from typing import Any
from pydantic import BaseModel, Field, field_validator

from app.integrations.llm.provider import LLMProvider
from app.models.incident import Incident
from app.models.diagnosis import Diagnosis
from app.models.recovery_policy import RecoveryPolicy
from app.domains.recovery.economics import ActionEconomics

logger = logging.getLogger(__name__)

# Sensitive keys to explicitly strip from any evidence payload
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


# Hard bounds protecting the AI threshold adaptations
MIN_AI_INTERVENTION_FAILURE_THRESHOLD = 5
MAX_AI_INTERVENTION_FAILURE_THRESHOLD = 1000
MIN_AI_INTERVENTION_RATE_THRESHOLD = 0.01
MAX_AI_INTERVENTION_RATE_THRESHOLD = 1.0


class AdaptiveRecommendation(BaseModel):
    """
    Strict response model for the Adaptive Recovery Analyst.
    Enforces bounded outputs, valid probabilities, and validated urgency.
    """
    intervene: bool = Field(..., description="Whether intervention is warranted based on evidence")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Confidence score between 0.0 and 1.0")
    recommended_failure_threshold: int = Field(..., gt=0, description="Proposed failure count threshold")
    recommended_failure_rate_threshold: float = Field(..., ge=0.0, le=1.0, description="Proposed failure rate threshold [0.0, 1.0]")
    urgency: str = Field(..., description="Urgency level: low, medium, high, critical")
    recommended_method: str | None = Field(None, description="Recommended payment method scope")
    recommended_bank: str | None = Field(None, description="Recommended bank scope")
    recommended_action: str | None = Field(None, description="Recommended recovery strategy action")
    reasoning: str = Field(..., min_length=1, description="Concise analytical reasoning")
    evidence_summary: list[str] = Field(default_factory=list, description="Evidence points supporting recommendation")

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError("Confidence must be between 0.0 and 1.0")
        return v

    @field_validator("recommended_failure_threshold")
    @classmethod
    def validate_threshold(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("Recommended failure threshold must be a positive integer")
        return v

    @field_validator("recommended_failure_rate_threshold")
    @classmethod
    def validate_rate_threshold(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError("Recommended failure rate threshold must be between 0.0 and 1.0")
        return v

    @field_validator("urgency")
    @classmethod
    def validate_urgency(cls, v: str) -> str:
        cleaned = v.strip().lower()
        if cleaned not in {"low", "medium", "high", "critical"}:
            raise ValueError(f"Urgency '{v}' must be one of: low, medium, high, critical")
        return cleaned

    @field_validator("recommended_action")
    @classmethod
    def validate_action_when_intervening(cls, v: str | None, info) -> str | None:
        data = info.data
        if data.get("intervene") is True and not v:
            raise ValueError("A recommended action must be specified when intervention is warranted")
        return v


class AdaptiveTelemetry(BaseModel):
    """
    Lightweight telemetry captured for each invocation of the Adaptive Recovery Analyst.
    """
    provider: str
    model_name: str
    latency_ms: float
    success: bool
    fallback_used: bool
    input_size_bytes: int
    output_size_bytes: int
    error_message: str | None = None
    memory_retrieval_latency_ms: float = 0.0
    memory_size_bytes: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cost_status: str = "UNAVAILABLE"
    estimated_cost_usd: float | None = None


class AdaptiveEvidence(BaseModel):
    """
    Sanitized, structured evidence provided to the Adaptive Recovery Analyst.
    Guaranteed to contain zero secrets, credentials, or PII.
    """
    baseline_transaction_count: int
    baseline_failed_count: int
    baseline_failure_rate: float
    current_transaction_count: int
    current_failed_count: int
    current_failure_rate: float
    absolute_degradation: float
    relative_degradation: float
    revenue_at_risk: int
    method: str
    bank: str
    error_code: str
    error_step: str
    historical_recovery_performance: dict[str, Any]
    available_recovery_actions: list[str]
    expected_economics: list[dict[str, Any]]
    merchant_policy_boundaries: dict[str, Any]
    trend_information: dict[str, Any]
    operational_memory: dict[str, Any] | None = None


class AdaptiveAnalysisResult(BaseModel):
    """
    Unified result produced by the Adaptive Recovery Analyst and verification layer.
    """
    recommendation: AdaptiveRecommendation | None = None
    is_accepted: bool
    rejection_reason: str | None = None
    rejection_explanation: str | None = None
    fallback_action: str | None = None
    telemetry: AdaptiveTelemetry
    evidence: dict[str, Any]


def _sanitize_dict(data: Any) -> Any:
    """Recursively redacts any sensitive keys from evidence dictionaries."""
    if isinstance(data, dict):
        clean = {}
        for k, v in data.items():
            if any(s in k.lower() for s in SENSITIVE_KEYS):
                continue
            clean[k] = _sanitize_dict(v)
        return clean
    elif isinstance(data, list):
        return [_sanitize_dict(item) for item in data]
    return data


def build_adaptive_evidence(
    incident: Incident,
    diagnosis: Diagnosis | None,
    policy: RecoveryPolicy,
    eligible_candidates: list[ActionEconomics],
    historical_evidence: dict[str, dict[str, Any]] | None = None,
    operational_memory: Any | None = None,
) -> AdaptiveEvidence:
    """
    Constructs a strictly sanitized AdaptiveEvidence object for the analyst.
    """
    hist = historical_evidence or {}
    economics_summary = [
        {
            "action": c.action,
            "expected_recovery_rate": c.expected_recovery_rate,
            "expected_net_recovery_value": c.expected_net_recovery_value,
            "action_cost": c.action_cost,
            "is_eligible": c.is_eligible,
        }
        for c in eligible_candidates
    ]

    policy_boundaries = {
        "allowed_actions": policy.allowed_actions,
        "max_incentive": policy.max_incentive,
        "max_exposure": policy.max_exposure,
        "approval_threshold": policy.approval_threshold,
    }

    trend_info = {
        "window_duration_minutes": (
            (incident.window_end - incident.window_start).total_seconds() / 60.0
            if incident.window_start and incident.window_end
            else 30.0
        ),
        "diagnosis_type": diagnosis.diagnosis_type if diagnosis else "unknown",
        "diagnosis_confidence": diagnosis.confidence if diagnosis else 0.0,
    }

    mem_dict = None
    if operational_memory:
        if hasattr(operational_memory, "model_dump"):
            mem_dict = _sanitize_dict(operational_memory.model_dump(mode="json"))
        elif isinstance(operational_memory, dict):
            mem_dict = _sanitize_dict(operational_memory)

    evidence = AdaptiveEvidence(
        baseline_transaction_count=incident.baseline_total_count or 0,
        baseline_failed_count=incident.baseline_failed_count or 0,
        baseline_failure_rate=incident.baseline_failure_rate or 0.0,
        current_transaction_count=incident.current_total_count or 0,
        current_failed_count=incident.current_failed_count or 0,
        current_failure_rate=incident.current_failure_rate or 0.0,
        absolute_degradation=incident.absolute_rate_increase or 0.0,
        relative_degradation=incident.relative_degradation or 1.0,
        revenue_at_risk=incident.revenue_at_risk or 0,
        method=incident.method,
        bank=incident.bank,
        error_code=incident.error_code,
        error_step=incident.error_step,
        historical_recovery_performance=_sanitize_dict(hist),
        available_recovery_actions=[c.action for c in eligible_candidates],
        expected_economics=_sanitize_dict(economics_summary),
        merchant_policy_boundaries=_sanitize_dict(policy_boundaries),
        trend_information=_sanitize_dict(trend_info),
        operational_memory=mem_dict,
    )

    return evidence


def verify_adaptive_recommendation(
    rec: AdaptiveRecommendation,
    incident: Incident,
    policy: RecoveryPolicy,
    eligible_candidates: list[ActionEconomics],
    min_failure_threshold: int = MIN_AI_INTERVENTION_FAILURE_THRESHOLD,
    max_failure_threshold: int = MAX_AI_INTERVENTION_FAILURE_THRESHOLD,
    min_rate_threshold: float = MIN_AI_INTERVENTION_RATE_THRESHOLD,
    max_rate_threshold: float = MAX_AI_INTERVENTION_RATE_THRESHOLD,
) -> tuple[bool, str | None, str | None]:
    """
    Deterministic Verification Layer.
    Guarantees the AI recommendation adheres strictly to:
    1. Action eligibility and policy boundaries.
    2. Cohort alignment with the active incident.
    3. Minimum and maximum safety bounds on adaptive thresholds.
    Returns: (is_valid, reason_code, explanation)
    """
    eligible_actions = {c.action for c in eligible_candidates}

    # 1. Action verification
    if rec.intervene:
        if not rec.recommended_action:
            return False, "MISSING_RECOMMENDED_ACTION", "A recommended action must be specified when intervention is warranted."
        if rec.recommended_action not in eligible_actions:
            return (
                False,
                "ACTION_NOT_ELIGIBLE",
                f"Action '{rec.recommended_action}' is not in eligible candidates: {list(eligible_actions)}",
            )
        if rec.recommended_action not in policy.allowed_actions:
            return (
                False,
                "ACTION_DISALLOWED_BY_POLICY",
                f"Action '{rec.recommended_action}' is not in allowed policy actions: {policy.allowed_actions}",
            )

    # 2. Cohort scope verification
    if rec.recommended_method:
        rec_m = rec.recommended_method.strip().lower()
        if rec_m not in {incident.method.lower(), "all", "*"}:
            return (
                False,
                "COHORT_METHOD_MISMATCH",
                f"Recommended method '{rec.recommended_method}' does not match incident '{incident.method}'",
            )

    if rec.recommended_bank:
        rec_b = rec.recommended_bank.strip().lower()
        if rec_b not in {incident.bank.lower(), "all", "*"}:
            return (
                False,
                "COHORT_BANK_MISMATCH",
                f"Recommended bank '{rec.recommended_bank}' does not match incident '{incident.bank}'",
            )

    # 3. Hard safety bounds on adaptive thresholds
    if rec.recommended_failure_threshold < min_failure_threshold:
        return (
            False,
            "THRESHOLD_BELOW_SAFETY_MINIMUM",
            f"Proposed threshold {rec.recommended_failure_threshold} is below safety floor of {min_failure_threshold}",
        )

    if rec.recommended_failure_threshold > max_failure_threshold:
        return (
            False,
            "THRESHOLD_EXCEEDS_SAFETY_MAXIMUM",
            f"Proposed threshold {rec.recommended_failure_threshold} exceeds safety ceiling of {max_failure_threshold}",
        )

    if rec.recommended_failure_rate_threshold < min_rate_threshold:
        return (
            False,
            "RATE_THRESHOLD_BELOW_SAFETY_MINIMUM",
            f"Proposed rate threshold {rec.recommended_failure_rate_threshold} is below safety floor of {min_rate_threshold}",
        )

    if rec.recommended_failure_rate_threshold > max_rate_threshold:
        return (
            False,
            "RATE_THRESHOLD_EXCEEDS_SAFETY_MAXIMUM",
            f"Proposed rate threshold {rec.recommended_failure_rate_threshold} exceeds safety ceiling of {max_rate_threshold}",
        )

    return True, None, None


class AdaptiveRecoveryAnalyst:
    """
    Isolated Adaptive Recovery Analyst.
    Responsible for evidence-based intervention reasoning, threshold recommendation,
    and cohort scope estimation.
    Passes all outputs through the deterministic verification layer.
    """
    def __init__(
        self,
        provider: LLMProvider,
        min_failure_threshold: int = MIN_AI_INTERVENTION_FAILURE_THRESHOLD,
        max_failure_threshold: int = MAX_AI_INTERVENTION_FAILURE_THRESHOLD,
        min_rate_threshold: float = MIN_AI_INTERVENTION_RATE_THRESHOLD,
        max_rate_threshold: float = MAX_AI_INTERVENTION_RATE_THRESHOLD,
    ):
        self.provider = provider
        self.min_failure_threshold = min_failure_threshold
        self.max_failure_threshold = max_failure_threshold
        self.min_rate_threshold = min_rate_threshold
        self.max_rate_threshold = max_rate_threshold

    def analyze(
        self,
        incident: Incident,
        diagnosis: Diagnosis | None,
        policy: RecoveryPolicy,
        eligible_candidates: list[ActionEconomics],
        historical_evidence: dict[str, dict[str, Any]] | None = None,
        operational_memory: Any | None = None,
        timeout: float = 10.0,
    ) -> AdaptiveAnalysisResult:
        """
        Executes adaptive analysis with latency telemetry and deterministic fallback.
        """
        # Determine best deterministic fallback action (highest expected net recovery value)
        fallback_action = (
            max(eligible_candidates, key=lambda c: c.expected_net_recovery_value).action
            if eligible_candidates
            else None
        )

        mem_latency = getattr(operational_memory, "retrieval_latency_ms", 0.0) if operational_memory else 0.0
        mem_size = getattr(operational_memory, "memory_size_bytes", 0) if operational_memory else 0

        evidence = build_adaptive_evidence(
            incident=incident,
            diagnosis=diagnosis,
            policy=policy,
            eligible_candidates=eligible_candidates,
            historical_evidence=historical_evidence,
            operational_memory=operational_memory,
        )
        evidence_dict = evidence.model_dump()

        provider_name = self.provider.__class__.__name__
        model_name = getattr(self.provider, "model_name", "unknown")

        system_instruction = (
            "You are an Adaptive Recovery Analyst for RecoverAI, an intelligent payment recovery platform.\n"
            "Analyze the provided sanitized incident metrics, historical operational memory, and economic candidates.\n"
            "Advisory guidelines:\n"
            "- Historical operational memory is advisory evidence and must not be blindly copied.\n"
            "- Exact-cohort evidence is stronger than broad/global evidence; insufficient evidence warrants caution.\n"
            "- Historical outcomes may inform intervention thresholds and strategy selection.\n"
            "- You must NOT override deterministic safety bounds or invent historical facts.\n"
            "Recommend whether intervention is warranted, the urgency, adaptive thresholds, cohort scope, and recovery action.\n"
            "You must strictly adhere to the requested schema. Choose only eligible actions provided in the evidence."
        )

        memory_section = ""
        if operational_memory:
            mem_summary = getattr(operational_memory, "summary", "")
            memory_section = f"\n\nHistorical Operational Memory (Advisory):\nSummary: {mem_summary}\nDetails:\n{json.dumps(evidence_dict.get('operational_memory'), indent=2, default=str)}"

        prompt = (
            f"Incident and Economics Evidence:\n{json.dumps(evidence_dict, indent=2, default=str)}"
            f"{memory_section}\n\n"
            f"Eligible Actions: {[c.action for c in eligible_candidates]}\n"
            f"Safety Constraints: Failure threshold must be between {self.min_failure_threshold} and {self.max_failure_threshold}. "
            f"Rate threshold must be between {self.min_rate_threshold} and {self.max_rate_threshold}.\n"
            "Provide your structured analysis."
        )

        input_size_bytes = len(prompt.encode("utf-8"))
        start_time = time.perf_counter()

        try:
            rec = self.provider.generate_structured_output(
                prompt=prompt,
                response_model=AdaptiveRecommendation,
                system_instruction=system_instruction,
                timeout=timeout,
            )
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            output_size_bytes = len(rec.model_dump_json().encode("utf-8"))

            # Run deterministic verification layer
            is_valid, reason_code, explanation = verify_adaptive_recommendation(
                rec=rec,
                incident=incident,
                policy=policy,
                eligible_candidates=eligible_candidates,
                min_failure_threshold=self.min_failure_threshold,
                max_failure_threshold=self.max_failure_threshold,
                min_rate_threshold=self.min_rate_threshold,
                max_rate_threshold=self.max_rate_threshold,
            )

            # Extract token usage and honest cost metadata
            token_usage = getattr(self.provider, "last_token_usage", None)
            if callable(getattr(self.provider, "get_last_token_usage", None)):
                token_usage = self.provider.get_last_token_usage()

            input_tokens = token_usage.input_tokens if token_usage else None
            output_tokens = token_usage.output_tokens if token_usage else None
            total_tokens = token_usage.total_tokens if token_usage else None

            pricing = getattr(self.provider, "pricing_per_million", None)
            cost_status = "UNAVAILABLE"
            estimated_cost_usd = None
            if pricing and isinstance(pricing, dict) and input_tokens is not None and output_tokens is not None:
                cost_status = "AVAILABLE"
                estimated_cost_usd = round(
                    (input_tokens * pricing.get("input", 0.0) + output_tokens * pricing.get("output", 0.0)) / 1_000_000,
                    6,
                )

            if not is_valid:
                telemetry = AdaptiveTelemetry(
                    provider=provider_name,
                    model_name=model_name,
                    latency_ms=round(elapsed_ms, 2),
                    success=True,
                    fallback_used=True,
                    input_size_bytes=input_size_bytes,
                    output_size_bytes=output_size_bytes,
                    error_message=explanation,
                    memory_retrieval_latency_ms=mem_latency,
                    memory_size_bytes=mem_size,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=total_tokens,
                    cost_status=cost_status,
                    estimated_cost_usd=estimated_cost_usd,
                )
                return AdaptiveAnalysisResult(
                    recommendation=rec,
                    is_accepted=False,
                    rejection_reason=reason_code,
                    rejection_explanation=explanation,
                    fallback_action=fallback_action,
                    telemetry=telemetry,
                    evidence=evidence_dict,
                )

            # Validated successfully
            telemetry = AdaptiveTelemetry(
                provider=provider_name,
                model_name=model_name,
                latency_ms=round(elapsed_ms, 2),
                success=True,
                fallback_used=False,
                input_size_bytes=input_size_bytes,
                output_size_bytes=output_size_bytes,
                memory_retrieval_latency_ms=mem_latency,
                memory_size_bytes=mem_size,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                cost_status=cost_status,
                estimated_cost_usd=estimated_cost_usd,
            )
            return AdaptiveAnalysisResult(
                recommendation=rec,
                is_accepted=True,
                rejection_reason=None,
                fallback_action=None,
                telemetry=telemetry,
                evidence=evidence_dict,
            )

        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            telemetry = AdaptiveTelemetry(
                provider=provider_name,
                model_name=model_name,
                latency_ms=round(elapsed_ms, 2),
                success=False,
                fallback_used=True,
                input_size_bytes=input_size_bytes,
                output_size_bytes=0,
                error_message=f"{exc.__class__.__name__}: {str(exc)}",
                memory_retrieval_latency_ms=mem_latency,
                memory_size_bytes=mem_size,
            )
            return AdaptiveAnalysisResult(
                recommendation=None,
                is_accepted=False,
                rejection_reason=exc.__class__.__name__,
                rejection_explanation=str(exc),
                fallback_action=fallback_action,
                telemetry=telemetry,
                evidence=evidence_dict,
            )
