"""Base provider interface and shared types."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class Message(BaseModel):
    """A single chat message."""

    role: str
    content: str


class ImagePart(BaseModel):
    """An image attached to the last user message (needs a vision-capable model)."""

    media_type: Literal["image/png", "image/jpeg", "image/webp", "image/gif"]
    data: bytes


class ModelRequest(BaseModel):
    """Request to generate a completion from a model."""

    model_id: str
    messages: list[Message] = Field(default_factory=list)
    system_prompt: str = ""
    user_prompt: str = ""
    temperature: float | None = None
    max_tokens: int = 4096
    json_mode: bool = False
    # JSON Schema the answer must follow. Mapped to each provider's native structured output
    # when the catalog says the model supports it, otherwise spelled out in the system prompt.
    response_schema: dict[str, Any] | None = None
    response_schema_name: str = "response"
    response_schema_strict: bool = True
    # Overrides the catalog's default_reasoning_effort (none/minimal/low/medium/high/xhigh/max).
    reasoning_effort: str | None = None
    # Legacy extended-thinking budget; only for models without adaptive thinking.
    thinking_budget_tokens: int | None = None
    # Cache everything before the last message (system prompt + shared task prefix).
    cache_prefix: bool = False
    # Stream over SSE to measure time-to-first-token and decode speed.
    stream: bool = False
    images: list[ImagePart] = Field(default_factory=list)
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

    def system_text(self) -> str:
        """System prompt: the explicit field, else any system-role messages."""
        if self.system_prompt:
            return self.system_prompt
        return "\n\n".join(m.content for m in self.messages if m.role == "system")

    def chat_messages(self) -> list[Message]:
        """Conversation turns without system messages."""
        if self.messages:
            return [m for m in self.messages if m.role != "system"]
        return [Message(role="user", content=self.user_prompt)]

    def effective_timeout(self, default: float) -> float:
        return self.timeout if self.timeout is not None else default


class ModelResponse(BaseModel):
    """Standardized response from a model provider."""

    provider: str
    model: str
    model_alias: str | None = None
    text: str = ""
    parsed_json: dict[str, Any] | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    cost_estimate_usd: float | None = None
    actual_cost_usd: float | None = None
    latency_ms: float = 0.0
    # Set only for streamed calls: ms until the first non-empty text delta.
    ttft_ms: float | None = None
    # output_tokens / (latency - ttft) for streamed calls; None otherwise.
    decode_tokens_per_s: float | None = None
    # output_tokens / latency; needs provider-reported output tokens.
    total_tokens_per_s: float | None = None
    retries: int = 0
    finish_reason: str | None = None
    raw_response: dict[str, Any] | None = None
    error: str | None = None
    error_type: str | None = None

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

    @property
    def total_tokens(self) -> int | None:
        if self.input_tokens is None and self.output_tokens is None:
            return None
        return (self.input_tokens or 0) + (self.output_tokens or 0)


# Backward-compatible aliases used across orchestration code.
CompletionRequest = ModelRequest
CompletionResponse = ModelResponse


class ProviderError(Exception):
    """Raised when a provider call fails irrecoverably.

    ``error_type`` is the stable label surfaced on ``ModelResponse``; ``retries`` counts the
    attempts made after the first one before giving up.
    """

    error_type = "ProviderError"

    def __init__(self, message: str, *, retries: int = 0) -> None:
        super().__init__(message)
        self.retries = retries


class RateLimitError(ProviderError):
    error_type = "RateLimit"


class AuthError(ProviderError):
    error_type = "Auth"


class ProviderTimeoutError(ProviderError):
    error_type = "Timeout"


class BadRequestError(ProviderError):
    error_type = "BadRequest"


class ServerError(ProviderError):
    error_type = "Server"


def speed_metrics(
    output_tokens: int | None, latency_ms: float, ttft_ms: float | None
) -> tuple[float | None, float | None]:
    """Return ``(decode_tokens_per_s, total_tokens_per_s)`` from provider-reported tokens."""
    if not output_tokens or output_tokens <= 0 or latency_ms <= 0:
        return None, None
    total = output_tokens / (latency_ms / 1000.0)
    decode: float | None = None
    if ttft_ms is not None and latency_ms > ttft_ms:
        decode = output_tokens / ((latency_ms - ttft_ms) / 1000.0)
    return decode, total


def schema_instruction(schema: Mapping[str, Any]) -> str:
    """Prompt text for models without native structured output."""
    return (
        "Respond with a single JSON object that validates against this JSON Schema. "
        "Output only the JSON.\n" + json.dumps(schema, indent=2, sort_keys=True)
    )


def with_schema_instruction(system_prompt: str, schema: Mapping[str, Any]) -> str:
    instruction = schema_instruction(schema)
    return f"{system_prompt}\n\n{instruction}" if system_prompt else instruction


def inline_schema_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Resolve local ``$ref`` pointers (Pydantic emits ``$defs``) so providers see one tree."""
    defs: dict[str, Any] = {**schema.get("definitions", {}), **schema.get("$defs", {})}

    def walk(node: Any, seen: frozenset[str]) -> Any:
        if isinstance(node, list):
            return [walk(item, seen) for item in node]
        if not isinstance(node, dict):
            return node
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.rsplit("/", 1)[-1] in defs:
            name = ref.rsplit("/", 1)[-1]
            if name in seen:  # recursive type: keep the pointer, let the provider decide
                return node
            merged = {**defs[name], **{k: v for k, v in node.items() if k != "$ref"}}
            return walk(merged, seen | {name})
        return {k: walk(v, seen) for k, v in node.items() if k not in {"$defs", "definitions"}}

    result: dict[str, Any] = walk(schema, frozenset())
    return result


def close_objects(schema: dict[str, Any]) -> dict[str, Any]:
    """Set ``additionalProperties: false`` on every object node (required by Anthropic)."""

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(item) for item in node]
        if not isinstance(node, dict):
            return node
        out = {k: walk(v) for k, v in node.items()}
        if out.get("type") == "object" and "additionalProperties" not in out:
            out["additionalProperties"] = False
        return out

    result: dict[str, Any] = walk(schema)
    return result


class ModelProvider(ABC):
    """Async interface for calling language model providers."""

    name: str

    async def aclose(self) -> None:  # noqa: B027 — optional hook, no-op by default
        """Release network resources. Safe to call more than once."""

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
                error_type=exc.error_type,
                retries=exc.retries,
            )
        except Exception as exc:  # noqa: BLE001 — provider boundary
            return ModelResponse(
                provider=self.name,
                model=request.model_id,
                error=f"Unexpected provider error: {exc}",
                error_type=exc.__class__.__name__,
            )


async def close_providers(providers: Mapping[str, ModelProvider]) -> None:
    """Close every provider; one failing close never prevents the others."""
    for provider in providers.values():
        try:
            await provider.aclose()
        except Exception:  # noqa: BLE001 — shutdown must not raise
            continue
