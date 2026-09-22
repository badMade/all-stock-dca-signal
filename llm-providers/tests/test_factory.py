from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pytest
from llm_providers import (
    AnthropicClient,
    GeminiClient,
    LLMClient,
    NvidiaNimClient,
    available_providers,
    get_client,
)


@pytest.mark.parametrize(
    "name, cls",
    [
        ("nvidia", NvidiaNimClient),
        ("NIM", NvidiaNimClient),
        ("nemotron", NvidiaNimClient),
        ("anthropic", AnthropicClient),
        ("claude", AnthropicClient),
        ("gemini", GeminiClient),
        (" Google ", GeminiClient),
    ],
)
def test_explicit_provider_selection(name, cls):
    client = get_client(name, client=MagicMock())
    assert isinstance(client, cls) and isinstance(client, LLMClient)


def test_default_is_nvidia_when_env_unset():
    assert isinstance(get_client(client=MagicMock()), NvidiaNimClient)


def test_env_var_selects_provider(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "claude")
    assert isinstance(get_client(client=MagicMock()), AnthropicClient)


def test_explicit_argument_beats_env(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "claude")
    assert isinstance(get_client("gemini", client=MagicMock()), GeminiClient)


def test_unknown_provider_fails_at_construction():
    with pytest.raises(ValueError, match="unknown LLM provider 'cohere'"):
        get_client("cohere")


def test_kwargs_forwarded_to_adapter():
    client = get_client("nvidia", client=MagicMock(), model="nvidia/other", enable_thinking=True)
    assert client.model == "nvidia/other" and client.enable_thinking is True


def test_available_providers_are_canonical_and_unique():
    assert available_providers() == ("nvidia", "anthropic", "gemini")


def test_adapters_share_the_interface_signature():
    """Signature parity is what lets a call site swap providers without edits."""
    for method in ("complete", "stream"):
        expected = inspect.signature(getattr(LLMClient, method))
        for cls in (NvidiaNimClient, AnthropicClient, GeminiClient):
            assert inspect.signature(getattr(cls, method)) == expected, f"{cls.__name__}.{method}"
