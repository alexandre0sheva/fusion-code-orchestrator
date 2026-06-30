"""Base provider interface and shared types."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field, model_validator


class Message(BaseModel):
    """A single chat message."""

    role: str
    content: str


class ModelRequest(BaseModel):
    """Request to generate a completion from a model."""

    model_id: str
    messages: list[Message] = Field(default_factory=list)
    system_prompt: str = ""
    user_prompt: str = ""
    temperature: float = 0.2
    max_tokens: int = 4096
    json_mode: bool = False
    timeout: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _ensure_prompt(self) -> ModelRequest:
        if not self.messages and not self.user_prompt:
            msg = "ModelRequest requires messages or user_prompt"
            raise ValueError(msg)
        return self

    def resolved_messages(self) -> list[Message]:
        """Return explicit messages or derive them from system/user prompts."""
        if self.messages:
            return list(self.messages)
        messages: list[Message] = []
        if self.system_prompt:
            messages.append(Message(role="system", content=self.system_prompt))
        messages.append(Message(role="user", content=self.user_prompt))
        return messages

    def effective_timeout(self, default: float) -> float:
        return self.timeout if self.timeout is not None else default


class ModelResponse(BaseModel):
    """Standardized response from a model provider."""

    provider: str
    model: str
    text: str = ""
    parsed_json: dict[str, Any] | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_estimate_usd: float | None = None
    latency_ms: float = 0.0
    finish_reason: str | None = None
    raw_response: dict[str, Any] | None = None
    error: str | None = None

    @property
    def content(self) -> str:
        """Backward-compatible alias for ``text``."""
        return self.text

    @property
    def model_id(self) -> str:
        """Backward-compatible alias for ``model``."""
        return self.model

    @property
    def raw(self) -> dict[str, Any]:
        """Backward-compatible alias for ``raw_response``."""
        return self.raw_response or {}

    @property
    def ok(self) -> bool:
        return self.error is None


# Backward-compatible aliases used across orchestration code.
CompletionRequest = ModelRequest
CompletionResponse = ModelResponse


class ProviderError(Exception):
    """Raised when a provider call fails irrecoverably."""


class ModelProvider(ABC):
    """Async interface for calling language model providers."""

    name: str

    @abstractmethod
    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Generate a completion for the given request."""

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if the provider is configured and reachable."""

    async def safe_complete(self, request: ModelRequest) -> ModelResponse:
        """Call complete and return structured errors instead of raising."""
        try:
            return await self.complete(request)
        except ProviderError as exc:
            return ModelResponse(
                provider=self.name,
                model=request.model_id,
                error=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 — provider boundary
            return ModelResponse(
                provider=self.name,
                model=request.model_id,
                error=f"Unexpected provider error: {exc}",
            )
