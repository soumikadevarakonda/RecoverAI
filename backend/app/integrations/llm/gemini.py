import os
import httpx
from typing import Type, TypeVar
from pydantic import BaseModel

from app.integrations.llm.provider import LLMProvider, LLMTokenUsage

T = TypeVar("T", bound=BaseModel)

class GeminiLLMProvider(LLMProvider):
    def __init__(
        self,
        api_key: str | None = None,
        model_name: str = "gemini-1.5-pro",
    ):
        self.api_key = api_key or os.getenv("GEMINI_API_KEY", "")
        self.model_name = model_name
        self.last_token_usage = None

    def generate_structured_output(
        self,
        prompt: str,
        response_model: Type[T],
        system_instruction: str | None = None,
        timeout: float = 10.0,
    ) -> T:
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY is not set")

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model_name}:generateContent?key={self.api_key}"

        # Build contents
        contents = {
            "contents": [
                {
                    "parts": [
                        {"text": prompt}
                    ]
                }
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
            }
        }

        # Add system instruction if provided
        if system_instruction:
            contents["systemInstruction"] = {
                "parts": [
                    {"text": system_instruction}
                ]
            }

        # Add response schema for strict JSON constraint
        schema = response_model.model_json_schema()
        # Clean schema definitions if present to avoid Gemini API validation issues
        if "$defs" in schema:
            del schema["$defs"]
        contents["generationConfig"]["responseSchema"] = schema

        with httpx.Client() as client:
            response = client.post(url, json=contents, timeout=timeout)
            response.raise_for_status()
            
            resp_data = response.json()
            candidates = resp_data.get("candidates", [])
            if not candidates:
                raise ValueError("No candidates returned from Gemini API")

            usage = resp_data.get("usageMetadata", {})
            if usage:
                self.last_token_usage = LLMTokenUsage(
                    input_tokens=usage.get("promptTokenCount", 0),
                    output_tokens=usage.get("candidatesTokenCount", 0),
                    total_tokens=usage.get("totalTokenCount", 0),
                )
            else:
                self.last_token_usage = None
            
            text = candidates[0]["content"]["parts"][0]["text"]
            return response_model.model_validate_json(text)

