"""Multi-provider LLM chat completion: one interface, one adapter per vendor SDK.

    from llm_providers import get_client
    client = get_client("anthropic")          # or set $LLM_PROVIDER
    text = client.complete([{"role": "user", "content": "hi"}])
    for chunk in client.stream([...]):
        ...

See README.md for the env vars each provider reads.
"""

from .anthropic_claude import AnthropicClient
from .base import (
    ChatMessage,
    LLMAuthError,
    LLMClient,
    LLMError,
    LLMProviderError,
    LLMRateLimitError,
    LLMTimeoutError,
)
from .factory import DEFAULT_PROVIDER, PROVIDER_ENV, available_providers, get_client
from .google_gemini import GeminiClient
from .nvidia_nim import NvidiaNimClient

__all__ = [
    "DEFAULT_PROVIDER",
    "PROVIDER_ENV",
    "AnthropicClient",
    "ChatMessage",
    "GeminiClient",
    "LLMAuthError",
    "LLMClient",
    "LLMError",
    "LLMProviderError",
    "LLMRateLimitError",
    "LLMTimeoutError",
    "NvidiaNimClient",
    "available_providers",
    "get_client",
]
