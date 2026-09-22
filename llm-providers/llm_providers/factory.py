"""Config-driven adapter selection.

``get_client("anthropic")`` (or ``get_client()`` with ``$LLM_PROVIDER`` set)
is the only thing a call site needs to change -- or set in its environment --
to move between backends. Any extra keyword arguments are forwarded to the
chosen adapter's constructor, which is how tests inject mocked SDK clients
through the same path production uses.
"""

from __future__ import annotations

import os
from typing import Any

from .anthropic_claude import AnthropicClient
from .base import LLMClient
from .google_gemini import GeminiClient
from .nvidia_nim import NvidiaNimClient

PROVIDER_ENV = "LLM_PROVIDER"
# NVIDIA is the default because it is the deployment the brief was written
# around; override per-process with $LLM_PROVIDER rather than editing code.
DEFAULT_PROVIDER = "nvidia"

# Canonical names plus the aliases people actually type. Keep this the single
# place a new provider gets registered.
_REGISTRY: dict[str, type[LLMClient]] = {
    "nvidia": NvidiaNimClient,
    "nim": NvidiaNimClient,
    "nemotron": NvidiaNimClient,
    "anthropic": AnthropicClient,
    "claude": AnthropicClient,
    "gemini": GeminiClient,
    "google": GeminiClient,
}


def available_providers() -> tuple[str, ...]:
    """Canonical provider names, in registration order, for help text and validation."""
    seen: dict[str, None] = {}
    for cls in _REGISTRY.values():
        seen.setdefault(cls.provider, None)
    return tuple(seen)


def get_client(provider: str | None = None, **kwargs: Any) -> LLMClient:
    """Return an adapter for ``provider`` (default: ``$LLM_PROVIDER``, then ``"nvidia"``).

    Raises ``ValueError`` for an unknown name so a misconfiguration fails at
    startup, not on the first request.
    """
    name = (provider or os.environ.get(PROVIDER_ENV) or DEFAULT_PROVIDER).strip().lower()
    try:
        cls = _REGISTRY[name]
    except KeyError:
        raise ValueError(f"unknown LLM provider {name!r}; expected one of {', '.join(available_providers())}") from None
    return cls(**kwargs)
