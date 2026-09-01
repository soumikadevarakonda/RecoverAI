import logging
from pydantic import BaseModel, Field, field_validator
from typing import Any

from app.models.incident import Incident
from app.models.diagnosis import Diagnosis
from app.models.recovery_policy import RecoveryPolicy
from app.domains.recovery.economics import ActionEconomics
from app.integrations.llm.provider import LLMProvider

logger = logging.getLogger(__name__)

class AIRecommendation(BaseModel):
    recommended_action: str = Field(..., description="The action recommended for recovery")
    concise_reason: str = Field(..., description="Brief reasoning behind the recommendation")
    evidence_level: str = Field(..., description="Evidence level of the historical data used")
    sample_size: int = Field(..., description="Number of historical attempts at this evidence level")
    observed_recovery_rate: float = Field(..., description="Observed recovery rate of the recommended action")
    expected_net_recovery_value: int = Field(..., description="Expected net recovery value in minor units")
    confidence: float = Field(..., description="Strategist confidence in this recommendation, between 0.0 and 1.0")

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError("Confidence must be between 0.0 and 1.0")
        return v


class RecoveryStrategist:
    def __init__(self, provider: LLMProvider):
        self.provider = provider

    def recommend_action(
        self,
        incident: Incident,
        diagnosis: Diagnosis,
        policy: RecoveryPolicy,
        eligible_candidates: list[ActionEconomics],
        historical_evidence: dict[str, dict[str, Any]],
        timeout: float = 10.0,
    ) -> AIRecommendation:
        """
        Generates a recommendation using the LLM provider based on current context and evidence.
        Raises exception if the recommendation is invalid or fails validation.
        """
        if not eligible_candidates:
            raise ValueError("No eligible candidates provided")

        eligible_actions = {c.action for c in eligible_candidates}

        system_instruction = (
            "You are an AI Recovery Strategist for RecoverAI, an intelligent payment recovery platform.\n"
            "Your task is to analyze the current incident, diagnosis, policy, and candidate recovery actions with their historical performance.\n"
            "Then, recommend the best candidate action to execute.\n"
            "You must choose one of the eligible candidate actions provided. Do not recommend an ineligible or unavailable action."
        )

        prompt = f"""
        Current Incident Context:
        - Method: {incident.method}
        - Bank: {incident.bank}
        - Error Code: {incident.error_code}
        - Revenue At Risk: {incident.revenue_at_risk} minor units

        Diagnosis Context:
        - Anomaly Type: {diagnosis.diagnosis_type}
        - Explanation: {diagnosis.explanation}
        - Confidence: {diagnosis.confidence}

        Merchant Policy Limits:
        - Allowed Actions: {policy.allowed_actions}
        - Max Incentive Cap: {policy.max_incentive} minor units

        Eligible Candidate Actions and Historical Evidence:
        """
        for c in eligible_candidates:
            evidence = historical_evidence.get(c.action, {})
            prompt += f"""
            - Action: '{c.action}'
              - Expected Recovery Rate: {c.expected_recovery_rate}
              - Expected Net Recovery Value: {c.expected_net_recovery_value} minor units
              - Rate Source / Evidence Level: {evidence.get('evidence_level', 'none')}
              - Sample Size: {evidence.get('sample_size', 0)}
              - Observed Recovery Rate: {evidence.get('observed_recovery_rate', 0.0)}
            """

        prompt += f"\nRecommend the single best action from the eligible candidates: {list(eligible_actions)}. Provide the structured JSON output adhering to the requested schema."

        # Generate structured output using the provider
        rec = self.provider.generate_structured_output(
            prompt=prompt,
            response_model=AIRecommendation,
            system_instruction=system_instruction,
            timeout=timeout,
        )

        # Validate that the recommended action is indeed in the eligible candidate actions
        if rec.recommended_action not in eligible_actions:
            raise ValueError(
                f"Recommended action '{rec.recommended_action}' is not in eligible actions: {eligible_actions}"
            )

        return rec

