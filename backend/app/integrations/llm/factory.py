"""
Builds the configured LLM provider for the recovery strategist.

When GEMINI_API_KEY is unset the strategist is disabled and recovery decisions
come from the deterministic economics engine alone. Either way the AI is
advisory only: every recommendation is re-checked by the guardrail engine.
"""
from app.core.config import settings
from app.integrations.llm.provider import LLMProvider


def get_default_llm_provider() -> LLMProvider | None:
    if not settings.gemini_api_key:
        return None
    from app.integrations.llm.gemini import GeminiLLMProvider

    return GeminiLLMProvider(
        api_key=settings.gemini_api_key,
        model_name=settings.gemini_model,
    )
