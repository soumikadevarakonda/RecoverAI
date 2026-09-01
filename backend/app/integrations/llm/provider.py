from abc import ABC, abstractmethod
from typing import Type, TypeVar
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

class LLMProvider(ABC):
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

