"""Anthropic Claude adapter over the official ``anthropic`` SDK.

Differences from the OpenAI shape that this adapter absorbs: the system
prompt is a top-level parameter, ``max_tokens`` is mandatory, responses are a
list of typed content blocks, and streaming is a context manager exposing a
``text_stream`` iterator.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any

from .base import (
    LLMAuthError,
    LLMClient,
    LLMError,
    LLMProviderError,
    LLMRateLimitError,
    LLMTimeoutError,
    MessageLike,
    coerce_messages,
    drop_none,
    read_api_key,
    split_system,
)

DEFAULT_MODEL = "claude-sonnet-5"
# Anthropic rejects requests without max_tokens, so unlike the other two
# adapters we must always send one. 1024 mirrors the SDK's own quickstart.
DEFAULT_MAX_TOKENS = 1024
API_KEY_ENV = "ANTHROPIC_API_KEY"


class AnthropicClient(LLMClient):
    """Messages API chat completions via ``anthropic.Anthropic``."""

    provider = "anthropic"

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        timeout: float | None = 60.0,
        default_max_tokens: int = DEFAULT_MAX_TOKENS,
        client: Any | None = None,
    ) -> None:
        """Build the adapter; ``client`` injects an ``anthropic.Anthropic``-compatible object for tests."""
        self.model = model
        self.default_max_tokens = default_max_tokens
        if client is None:
            from anthropic import Anthropic

            client = Anthropic(api_key=read_api_key(API_KEY_ENV, provider=self.provider), timeout=timeout)
        self._client = client

    def _request_kwargs(
        self,
        messages: Sequence[MessageLike],
        temperature: float | None,
        top_p: float | None,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        system, turns = split_system(coerce_messages(messages))
        if not turns:
            raise ValueError("Anthropic requires at least one non-system message")
        return {
            "model": self.model,
            "max_tokens": max_tokens if max_tokens is not None else self.default_max_tokens,
            "messages": [{"role": m.role, "content": m.content} for m in turns],
            **drop_none(system=system, temperature=temperature, top_p=top_p),
        }

    def _translate(self, exc: BaseException) -> LLMError | None:
        import anthropic

        if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
            return LLMAuthError(str(exc), provider=self.provider)
        if isinstance(exc, anthropic.RateLimitError):
            return LLMRateLimitError(str(exc), provider=self.provider)
        if isinstance(exc, anthropic.APITimeoutError):
            return LLMTimeoutError(str(exc), provider=self.provider)
        if isinstance(exc, anthropic.APIError):
            return LLMProviderError(str(exc), provider=self.provider)
        return None

    def complete(
        self,
        messages: Sequence[MessageLike],
        *,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        kwargs = self._request_kwargs(messages, temperature, top_p, max_tokens)
        try:
            response = self._client.messages.create(**kwargs)
        except Exception as exc:
            translated = self._translate(exc)
            if translated is None:
                raise
            raise translated from exc
        # Only text blocks count; thinking/tool_use blocks are dropped so the
        # return value matches what ``stream`` would have yielded.
        return "".join(block.text for block in response.content if getattr(block, "type", None) == "text")

    def stream(
        self,
        messages: Sequence[MessageLike],
        *,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
    ) -> Iterator[str]:
        kwargs = self._request_kwargs(messages, temperature, top_p, max_tokens)
        try:
            # ``messages.stream`` is a context manager whose ``text_stream``
            # already filters to text deltas -- the SDK does the normalising.
            with self._client.messages.stream(**kwargs) as stream:
                for text in stream.text_stream:
                    if text:
                        yield text
        except Exception as exc:
            translated = self._translate(exc)
            if translated is None:
                raise
            raise translated from exc
