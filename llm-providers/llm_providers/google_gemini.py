"""Google Gemini adapter over the official ``google-genai`` SDK.

Gemini's differences: roles are ``user``/``model`` (not ``assistant``), each
turn is a ``Content`` with ``parts``, the system prompt and sampling knobs all
live in a ``GenerateContentConfig``, and errors are one ``APIError`` class
distinguished by HTTP ``code``. The SDK accepts plain dicts everywhere it
accepts typed objects, and we use dicts so the request payload is directly
assertable in tests without importing ``google.genai.types``.
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

DEFAULT_MODEL = "gemini-2.5-flash"
# google-genai itself honours both names; we check the Gemini-specific one first.
API_KEY_ENVS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
_ROLE_MAP = {"user": "user", "assistant": "model"}


class GeminiClient(LLMClient):
    """``generate_content`` chat completions via ``google.genai.Client``."""

    provider = "gemini"

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        timeout: float | None = 60.0,
        client: Any | None = None,
    ) -> None:
        """Build the adapter; ``client`` injects a ``google.genai.Client``-compatible object for tests."""
        self.model = model
        if client is None:
            from google import genai

            http_options = {"timeout": int(timeout * 1000)} if timeout is not None else None  # SDK wants ms
            api_key = read_api_key(*API_KEY_ENVS, provider=self.provider)
            client = genai.Client(api_key=api_key, http_options=http_options)
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
            raise ValueError("Gemini requires at least one non-system message")
        config = drop_none(
            system_instruction=system,
            temperature=temperature,
            top_p=top_p,
            max_output_tokens=max_tokens,
        )
        kwargs: dict[str, Any] = {
            "model": self.model,
            "contents": [{"role": _ROLE_MAP[m.role], "parts": [{"text": m.content}]} for m in turns],
        }
        if config:
            kwargs["config"] = config
        return kwargs

    def _translate(self, exc: BaseException) -> LLMError | None:
        from google.genai import errors

        if isinstance(exc, errors.APIError):
            code = getattr(exc, "code", None)
            if code in (401, 403):
                return LLMAuthError(str(exc), provider=self.provider)
            if code == 429:
                return LLMRateLimitError(str(exc), provider=self.provider)
            if code == 408 or code == 504:
                return LLMTimeoutError(str(exc), provider=self.provider)
            return LLMProviderError(str(exc), provider=self.provider)
        # google-genai has no timeout class of its own; the underlying httpx
        # transport raises ``httpx.TimeoutException`` subclasses. Match on the
        # class name so we do not import httpx (a transitive dep, not ours).
        if isinstance(exc, TimeoutError) or "Timeout" in type(exc).__name__:
            return LLMTimeoutError(str(exc), provider=self.provider)
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
            response = self._client.models.generate_content(**kwargs)
        except Exception as exc:
            translated = self._translate(exc)
            if translated is None:
                raise
            raise translated from exc
        # ``.text`` is None on safety blocks / empty candidates; normalise to "".
        return response.text or ""

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
            for chunk in self._client.models.generate_content_stream(**kwargs):
                text = chunk.text
                if text:
                    yield text
        except Exception as exc:
            translated = self._translate(exc)
            if translated is None:
                raise
            raise translated from exc
