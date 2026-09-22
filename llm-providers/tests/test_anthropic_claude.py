from __future__ import annotations

from contextlib import contextmanager

import anthropic  # type: ignore
import pytest
from llm_providers import (
    AnthropicClient,
    LLMAuthError,
    LLMProviderError,
    LLMRateLimitError,
    LLMTimeoutError,
)

from conftest import MESSAGES, http_request, http_response, ns


def test_complete_hoists_system_and_always_sends_max_tokens(sdk):
    sdk.messages.create.return_value = ns(
        content=[ns(type="thinking", thinking="..."), ns(type="text", text="po"), ns(type="text", text="ng")]
    )
    client = AnthropicClient(client=sdk)

    assert client.complete(MESSAGES, top_p=0.9) == "pong"

    kwargs = sdk.messages.create.call_args.kwargs
    assert kwargs["model"] == "claude-sonnet-5"
    assert kwargs["system"] == "Be terse."
    assert kwargs["messages"] == [m for m in MESSAGES if m["role"] != "system"]
    assert kwargs["max_tokens"] == 1024 and kwargs["top_p"] == 0.9
    assert "temperature" not in kwargs


def test_explicit_max_tokens_and_no_system(sdk):
    sdk.messages.create.return_value = ns(content=[])
    AnthropicClient(client=sdk, default_max_tokens=7).complete([{"role": "user", "content": "x"}], max_tokens=99)
    kwargs = sdk.messages.create.call_args.kwargs
    assert kwargs["max_tokens"] == 99 and "system" not in kwargs


def test_system_only_conversation_rejected(sdk):
    with pytest.raises(ValueError):
        AnthropicClient(client=sdk).complete([{"role": "system", "content": "x"}])
    sdk.messages.create.assert_not_called()


def test_stream_uses_text_stream_context_manager(sdk):
    @contextmanager
    def fake_stream(**kwargs):
        yield ns(text_stream=iter(["po", "", "ng"]))

    sdk.messages.stream.side_effect = fake_stream
    assert list(AnthropicClient(client=sdk).stream(MESSAGES, temperature=0)) == ["po", "ng"]
    assert sdk.messages.stream.call_args.kwargs["temperature"] == 0


@pytest.mark.parametrize(
    "native, normalised",
    [
        (anthropic.AuthenticationError("bad key", response=http_response(401), body=None), LLMAuthError),
        (anthropic.PermissionDeniedError("forbidden", response=http_response(403), body=None), LLMAuthError),
        (anthropic.RateLimitError("slow down", response=http_response(429), body=None), LLMRateLimitError),
        (anthropic.APITimeoutError(request=http_request()), LLMTimeoutError),
        (anthropic.InternalServerError("boom", response=http_response(529), body=None), LLMProviderError),
        (anthropic.APIConnectionError(request=http_request()), LLMProviderError),
    ],
)
def test_errors_are_normalised(sdk, native, normalised):
    sdk.messages.create.side_effect = native
    sdk.messages.stream.side_effect = native
    client = AnthropicClient(client=sdk)
    with pytest.raises(normalised) as info:
        client.complete(MESSAGES)
    assert info.value.provider == "anthropic"
    assert info.value.__cause__ is native
    with pytest.raises(normalised):
        list(client.stream(MESSAGES))


def test_missing_env_key_fails_closed_without_network():
    with pytest.raises(LLMAuthError) as info:
        AnthropicClient()
    assert "$ANTHROPIC_API_KEY" in str(info.value)


def test_real_sdk_client_built_from_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unit-test-key")
    assert isinstance(AnthropicClient(timeout=5)._client, anthropic.Anthropic)
