"""Shared fixtures: fake SDK objects and env isolation. No adapter test touches the network."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

MESSAGES = [
    {"role": "system", "content": "Be terse."},
    {"role": "user", "content": "Hello"},
    {"role": "assistant", "content": "Hi."},
    {"role": "user", "content": "Ping?"},
]


@pytest.fixture(autouse=True)
def _scrub_provider_env(monkeypatch):
    """Guarantee tests never see a real key or provider selection from the developer's shell."""
    for name in ("NVIDIA_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "LLM_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    assert "LLM_PROVIDER" not in os.environ


def http_response(status: int) -> httpx.Response:
    """A minimal httpx response for constructing openai/anthropic status errors."""
    return httpx.Response(status, request=httpx.Request("POST", "https://unit.test/v1"))


def http_request() -> httpx.Request:
    return httpx.Request("POST", "https://unit.test/v1")


def ns(**kwargs):
    """Attribute-access stand-in for SDK response models."""
    return SimpleNamespace(**kwargs)


@pytest.fixture
def sdk():
    """A permissive MagicMock standing in for any vendor client object."""
    return MagicMock()
