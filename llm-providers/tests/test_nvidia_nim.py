from __future__ import annotations

import openai  # type: ignore
import pytest
from llm_providers import (
    LLMAuthError,
    LLMProviderError,
    LLMRateLimitError,
    LLMTimeoutError,
    NvidiaNimClient,
)

from conftest import MESSAGES, http_request, http_response, ns


def _chunk(text):
    return ns(choices=[ns(delta=ns(content=text))])


def test_complete_passes_messages_inline_and_only_set_knobs(sdk):
    sdk.chat.completions.create.return_value = ns(choices=[ns(message=ns(content="pong"))])
    client = NvidiaNimClient(client=sdk)

    assert client.complete(MESSAGES, temperature=0.2, max_tokens=64) == "pong"

    kwargs = sdk.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == "nvidia/nemotron-3-ultra-550b-a55b"
    assert kwargs["messages"] == MESSAGES  # system stays inline for OpenAI-compatible APIs
    assert kwargs["temperature"] == 0.2 and kwargs["max_tokens"] == 64
    assert "top_p" not in kwargs and "extra_body" not in kwargs and "stream" not in kwargs


def test_enable_thinking_maps_to_chat_template_kwargs(sdk):
    sdk.chat.completions.create.return_value = ns(choices=[ns(message=ns(content=""))])
    NvidiaNimClient(client=sdk, enable_thinking=False).complete(MESSAGES)
    assert sdk.chat.completions.create.call_args.kwargs["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": False}
    }


def test_complete_null_content_becomes_empty_string(sdk):
    sdk.chat.completions.create.return_value = ns(choices=[ns(message=ns(content=None))])
    assert NvidiaNimClient(client=sdk).complete(MESSAGES) == ""


def test_complete_no_choices_is_provider_error(sdk):
    sdk.chat.completions.create.return_value = ns(choices=[])
    with pytest.raises(LLMProviderError):
        NvidiaNimClient(client=sdk).complete(MESSAGES)


def test_stream_yields_only_text_deltas(sdk):
    sdk.chat.completions.create.return_value = iter(
        [
            ns(choices=[ns(delta=ns(reasoning_content="thinking..."))]),  # no .content attr
            _chunk("po"),
            _chunk(None),
            _chunk(""),
            _chunk("ng"),
            ns(choices=[]),  # usage trailer
        ]
    )
    assert list(NvidiaNimClient(client=sdk).stream(MESSAGES)) == ["po", "ng"]
    assert sdk.chat.completions.create.call_args.kwargs["stream"] is True


@pytest.mark.parametrize(
    "native, normalised",
    [
        (openai.AuthenticationError("bad key", response=http_response(401), body=None), LLMAuthError),
        (openai.PermissionDeniedError("forbidden", response=http_response(403), body=None), LLMAuthError),
        (openai.RateLimitError("slow down", response=http_response(429), body=None), LLMRateLimitError),
        (openai.APITimeoutError(request=http_request()), LLMTimeoutError),
        (openai.InternalServerError("boom", response=http_response(500), body=None), LLMProviderError),
        (openai.APIConnectionError(request=http_request()), LLMProviderError),
    ],
)
def test_errors_are_normalised(sdk, native, normalised):
    sdk.chat.completions.create.side_effect = native
    client = NvidiaNimClient(client=sdk)
    with pytest.raises(normalised) as info:
        client.complete(MESSAGES)
    assert info.value.provider == "nvidia"
    assert info.value.__cause__ is native
    with pytest.raises(normalised):
        list(client.stream(MESSAGES))


def test_unrelated_exceptions_propagate_untouched(sdk):
    sdk.chat.completions.create.side_effect = KeyError("not an sdk error")
    with pytest.raises(KeyError):
        NvidiaNimClient(client=sdk).complete(MESSAGES)


def test_missing_env_key_fails_closed_without_network():
    with pytest.raises(LLMAuthError) as info:
        NvidiaNimClient()
    assert "$NVIDIA_API_KEY" in str(info.value)


def test_real_sdk_client_built_from_env(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "unit-test-key")
    client = NvidiaNimClient(timeout=5)
    assert isinstance(client._client, openai.OpenAI)
    assert client._client is not None
    assert str(client._client.base_url).rstrip("/") == "https://integrate.api.nvidia.com/v1"
