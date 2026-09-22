"""Provider-agnostic chat-completion interface shared by every adapter.

This module is deliberately small: it holds only what is needed to unify three
vendor SDKs behind one call signature (messages in, text or text-chunks out)
and one exception family. Anything provider-specific -- message re-shaping,
streaming event handling, native error classes -- lives in the adapter that
owns it, so a vendor SDK change never ripples through the others.

No SDK is imported here. The base is pure standard library so a caller that
only ever uses one provider does not have to install the other two.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

__all__ = [
    "VALID_ROLES",
    "ChatMessage",
    "LLMAuthError",
    "LLMClient",
    "LLMError",
    "LLMProviderError",
    "LLMRateLimitError",
    "LLMTimeoutError",
    "MessageLike",
    "coerce_messages",
    "drop_none",
    "read_api_key",
    "split_system",
]

# The three roles every backend understands. Tool/function roles are out of
# scope on purpose -- the brief is plain chat completion, and each SDK spells
# tool messages differently enough that unifying them would be exactly the
# "custom abstraction layer beyond what's needed" the brief rules out.
VALID_ROLES: frozenset[str] = frozenset({"system", "user", "assistant"})


@dataclass(frozen=True)
class ChatMessage:
    """One chat turn. ``role`` is one of ``VALID_ROLES``; ``content`` is plain text."""

    role: str
    content: str


# Callers may pass either ChatMessage instances or ``{"role": ..., "content": ...}``
# dicts (the shape every vendor's own docs use), so migrating existing call
# sites does not require constructing dataclasses first.
MessageLike = ChatMessage | Mapping[str, Any]


class LLMError(Exception):
    """Base of the normalised exception family. ``provider`` names the adapter that raised it."""

    def __init__(self, message: str, *, provider: str) -> None:
        super().__init__(message)
        self.provider = provider


class LLMAuthError(LLMError):
    """Missing, malformed, or rejected credentials (HTTP 401/403 or no env var set)."""


class LLMRateLimitError(LLMError):
    """The provider throttled the request (HTTP 429 / quota exhausted)."""


class LLMTimeoutError(LLMError):
    """The request exceeded the configured timeout before a response completed."""


class LLMProviderError(LLMError):
    """Any other provider-side failure (5xx, malformed response, connection refused)."""


def read_api_key(*env_vars: str, provider: str) -> str:
    """Return the first non-empty value among ``env_vars``, or raise ``LLMAuthError``.

    Shared by every adapter so the "no hard-coded keys" rule has exactly one
    enforcement point. The error message names the variables, never the value:
    nothing here should ever end up logging a secret.
    """
    for name in env_vars:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    joined = " or ".join(f"${name}" for name in env_vars)
    raise LLMAuthError(f"no API key found; set {joined}", provider=provider)


def coerce_messages(messages: Sequence[MessageLike]) -> list[ChatMessage]:
    """Validate and normalise caller input into ``ChatMessage`` objects.

    Fails fast on unknown roles and non-string content so a typo surfaces as a
    ``ValueError`` at the call site instead of as an opaque 400 from a vendor.
    """
    if not messages:
        raise ValueError("messages must contain at least one turn")
    out: list[ChatMessage] = []
    for index, item in enumerate(messages):
        if isinstance(item, ChatMessage):
            role, content = item.role, item.content
        elif isinstance(item, Mapping):
            role, content = item.get("role"), item.get("content")
        else:
            raise ValueError(f"messages[{index}] must be a ChatMessage or a mapping, got {type(item).__name__}")
        if role not in VALID_ROLES:
            raise ValueError(f"messages[{index}].role must be one of {sorted(VALID_ROLES)}, got {role!r}")
        if not isinstance(content, str):
            raise ValueError(f"messages[{index}].content must be a string, got {type(content).__name__}")
        out.append(ChatMessage(role=role, content=content))
    return out


def split_system(messages: Sequence[ChatMessage]) -> tuple[str | None, list[ChatMessage]]:
    """Separate system turns from the conversation.

    Anthropic and Gemini both take the system prompt as a top-level parameter
    rather than as a message; OpenAI-compatible APIs take it inline. Multiple
    system turns are joined with a blank line so callers that build a system
    prompt from several fragments behave the same on every backend.
    """
    system_parts = [m.content for m in messages if m.role == "system"]
    rest = [m for m in messages if m.role != "system"]
    return ("\n\n".join(system_parts) if system_parts else None), rest


def drop_none(**kwargs: Any) -> dict[str, Any]:
    """Return ``kwargs`` without ``None`` values.

    Every adapter uses this to pass only the sampling knobs the caller actually
    set, so each vendor's own defaults apply when a knob is omitted and the
    request payload is identical to what a hand-written SDK call would send.
    """
    return {key: value for key, value in kwargs.items() if value is not None}


class LLMClient(ABC):
    """The one interface callers program against.

    Concrete adapters set ``provider`` and expose ``model``; the two abstract
    methods are the whole contract. ``stream`` yields text deltas only -- no
    role markers, no reasoning/thinking tokens, no usage metadata -- so a
    caller can ``"".join(client.stream(...))`` and get exactly what
    ``client.complete(...)`` would have returned.
    """

    provider: ClassVar[str]
    model: str

    @abstractmethod
    def complete(
        self,
        messages: Sequence[MessageLike],
        *,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Send ``messages`` and return the completed assistant text."""

    @abstractmethod
    def stream(
        self,
        messages: Sequence[MessageLike],
        *,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
    ) -> Iterator[str]:
        """Send ``messages`` and yield assistant text as it arrives."""
