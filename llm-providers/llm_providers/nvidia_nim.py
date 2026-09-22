"""NVIDIA NIM adapter: Nemotron-3 Ultra through the OpenAI-compatible endpoint.

Wraps the ``openai`` SDK pointed at ``https://integrate.api.nvidia.com/v1``.
The OpenAI wire format already matches our ``ChatMessage`` shape (system turns
stay inline), so this adapter is mostly error translation plus the
Nemotron-specific ``chat_template_kwargs`` passthrough.
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
)

DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_MODEL = "nvidia/nemotron-3-ultra-550b-a55b"
API_KEY_ENV = "NVIDIA_API_KEY"


class NvidiaNimClient(LLMClient):
    """Chat completions against NVIDIA-hosted Nemotron via the ``openai`` SDK."""

    provider = "nvidia"

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float | None = 60.0,
        enable_thinking: bool | None = None,
        client: Any | None = None,
    ) -> None:
        """Build the adapter.

        ``client`` lets tests (or a caller with a pre-configured SDK client)
        inject an ``openai.OpenAI``-compatible object; when omitted, one is
        built from ``$NVIDIA_API_KEY``. ``enable_thinking`` maps to Nemotron's
        ``chat_template_kwargs.enable_thinking`` (see the NIM model reference);
        ``None`` leaves the server default untouched.
        """
        self.model = model
        self.enable_thinking = enable_thinking
        if client is None:
            # Imported lazily so the package imports cleanly when only another
            # provider's SDK is installed.
            from openai import OpenAI  # type: ignore

            api_key = read_api_key(API_KEY_ENV, provider=self.provider)
            client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
        self._client: Any = client

    # -- request shaping ---------------------------------------------------

    def _request_kwargs(
        self,
        messages: Sequence[MessageLike],
        temperature: float | None,
        top_p: float | None,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in coerce_messages(messages)],
            **drop_none(temperature=temperature, top_p=top_p, max_tokens=max_tokens),
        }
        if self.enable_thinking is not None:
            # Nemotron-3 reads this from the chat template, not a top-level
            # field, hence extra_body rather than a named SDK argument.
            kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": self.enable_thinking}}
        return kwargs

    # -- error translation -------------------------------------------------

    def _translate(self, exc: BaseException) -> LLMError | None:
        """Map ``openai`` exceptions onto the shared family; ``None`` means "not ours, re-raise"."""
        import openai  # type: ignore

        if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
            return LLMAuthError(str(exc), provider=self.provider)
        if isinstance(exc, openai.RateLimitError):
            return LLMRateLimitError(str(exc), provider=self.provider)
        if isinstance(exc, openai.APITimeoutError):
            return LLMTimeoutError(str(exc), provider=self.provider)
        if isinstance(exc, openai.APIError):
            return LLMProviderError(str(exc), provider=self.provider)
        return None

    # -- public interface --------------------------------------------------

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
            response = self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            translated = self._translate(exc)
            if translated is None:
                raise
            raise translated from exc
        if not response.choices:
            raise LLMProviderError("response contained no choices", provider=self.provider)
        # ``content`` is None when the model emits only reasoning/tool output;
        # normalise to the empty string so the return type is always ``str``.
        return response.choices[0].message.content or ""

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
            for chunk in self._client.chat.completions.create(stream=True, **kwargs):
                # Usage-only trailer chunks and keep-alives carry no choices;
                # reasoning deltas arrive under ``reasoning_content`` which we
                # deliberately do not surface (the contract is answer text).
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                text = getattr(delta, "content", None)
                if text:
                    yield text
        except Exception as exc:
            translated = self._translate(exc)
            if translated is None:
                raise
            raise translated from exc
