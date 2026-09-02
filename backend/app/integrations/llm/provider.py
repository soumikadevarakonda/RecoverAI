from abc import ABC, abstractmethod
from typing import Type, TypeVar
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLMTokenUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class LLMProvider(ABC):
    last_token_usage: LLMTokenUsage | None = None

    def get_last_token_usage(self) -> LLMTokenUsage | None:
        return self.last_token_usage

    @abstractmethod
    def generate_structured_output(
        self,
        prompt: str,
        response_model: Type[T],
        system_instruction: str | None = None,
        timeout: float = 10.0,
    ) -> T:
        """
        Sends a prompt to the LLM and parses the response into the requested Pydantic model.
        Raises exceptions in case of errors (API failures, parsing issues, timeout).
        """
        pass
