from __future__ import annotations

import pytest
from google import genai  # type: ignore
from google.genai import errors  # type: ignore
from llm_providers import (
    GeminiClient,
    LLMAuthError,
    LLMProviderError,
    LLMRateLimitError,
    LLMTimeoutError,
)

from conftest import MESSAGES, ns


def test_complete_maps_roles_and_config(sdk):
    sdk.models.generate_content.return_value = ns(text="pong")
    client = GeminiClient(client=sdk)

    assert client.complete(MESSAGES, temperature=0.1, top_p=0.8, max_tokens=32) == "pong"

    kwargs = sdk.models.generate_content.call_args.kwargs
    assert kwargs["model"] == "gemini-2.5-flash"
    assert kwargs["contents"] == [
        {"role": "user", "parts": [{"text": "Hello"}]},
        {"role": "model", "parts": [{"text": "Hi."}]},
        {"role": "user", "parts": [{"text": "Ping?"}]},
    ]
    assert kwargs["config"] == {
        "system_instruction": "Be terse.",
        "temperature": 0.1,
        "top_p": 0.8,
        "max_output_tokens": 32,
    }


def test_no_config_key_when_nothing_set(sdk):
    sdk.models.generate_content.return_value = ns(text=None)  # safety-blocked / empty
    assert GeminiClient(client=sdk).complete([{"role": "user", "content": "x"}]) == ""
    assert "config" not in sdk.models.generate_content.call_args.kwargs


def test_system_only_conversation_rejected(sdk):
    with pytest.raises(ValueError):
        GeminiClient(client=sdk).complete([{"role": "system", "content": "x"}])


def test_stream_skips_empty_chunks(sdk):
    sdk.models.generate_content_stream.return_value = iter([ns(text="po"), ns(text=None), ns(text=""), ns(text="ng")])
    assert list(GeminiClient(client=sdk).stream(MESSAGES)) == ["po", "ng"]


class _FakeReadTimeout(Exception):
    """Mimics httpx.ReadTimeout by class name only, so the test does not depend on httpx internals."""


@pytest.mark.parametrize(
    "native, normalised",
    [
        (errors.APIError(401, {"error": {"message": "bad key", "status": "UNAUTHENTICATED"}}), LLMAuthError),
        (errors.ClientError(403, {"error": {"message": "forbidden"}}), LLMAuthError),
        (errors.ClientError(429, {"error": {"message": "quota"}}), LLMRateLimitError),
        (errors.ServerError(504, {"error": {"message": "deadline"}}), LLMTimeoutError),
        (errors.ServerError(500, {"error": {"message": "boom"}}), LLMProviderError),
        (TimeoutError("socket"), LLMTimeoutError),
        (_FakeReadTimeout("read"), LLMTimeoutError),
    ],
)
def test_errors_are_normalised(sdk, native, normalised):
    sdk.models.generate_content.side_effect = native
    sdk.models.generate_content_stream.side_effect = native
    client = GeminiClient(client=sdk)
    with pytest.raises(normalised) as info:
        client.complete(MESSAGES)
    assert info.value.provider == "gemini"
    assert info.value.__cause__ is native
    with pytest.raises(normalised):
        list(client.stream(MESSAGES))


def test_missing_env_key_fails_closed_without_network():
    with pytest.raises(LLMAuthError) as info:
        GeminiClient()
    assert "$GEMINI_API_KEY" in str(info.value) and "$GOOGLE_API_KEY" in str(info.value)


def test_real_sdk_client_built_from_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "unit-test-key")  # the fallback name works too
    assert isinstance(GeminiClient(timeout=5)._client, genai.Client)
