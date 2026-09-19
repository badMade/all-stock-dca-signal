from __future__ import annotations

import pytest
from llm_providers.base import (
    ChatMessage,
    LLMAuthError,
    coerce_messages,
    drop_none,
    read_api_key,
    split_system,
)


def test_coerce_accepts_dicts_and_dataclasses():
    out = coerce_messages([{"role": "user", "content": "a"}, ChatMessage("assistant", "b")])
    assert out == [ChatMessage("user", "a"), ChatMessage("assistant", "b")]


@pytest.mark.parametrize(
    "bad",
    [
        [],
        [{"role": "tool", "content": "x"}],
        [{"role": "user", "content": 5}],
        ["not a message"],
    ],
)
def test_coerce_rejects_bad_input(bad):
    with pytest.raises(ValueError):
        coerce_messages(bad)


def test_split_system_joins_multiple_system_turns_and_preserves_order():
    msgs = coerce_messages(
        [
            {"role": "system", "content": "A"},
            {"role": "user", "content": "u1"},
            {"role": "system", "content": "B"},
            {"role": "assistant", "content": "a1"},
        ]
    )
    system, rest = split_system(msgs)
    assert system == "A\n\nB"
    assert [m.role for m in rest] == ["user", "assistant"]


def test_split_system_none_when_absent():
    assert split_system([ChatMessage("user", "x")]) == (None, [ChatMessage("user", "x")])


def test_drop_none():
    assert drop_none(a=1, b=None, c=0) == {"a": 1, "c": 0}


def test_read_api_key_prefers_first_set_and_never_leaks_value(monkeypatch):
    monkeypatch.setenv("SECOND", "s3cr3t-value")
    assert read_api_key("FIRST", "SECOND", provider="p") == "s3cr3t-value"
    with pytest.raises(LLMAuthError) as info:
        read_api_key("FIRST", provider="p")
    assert "$FIRST" in str(info.value)
    assert "s3cr3t" not in str(info.value)
    assert info.value.provider == "p"
