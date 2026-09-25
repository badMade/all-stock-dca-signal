"""Tests for .github/scripts/pr_review.py's operational-failure path.

The script runs in the privileged workflow_run job with GEMINI_API_KEY in its
environment (antigravity-pr-review-post.yml, "Generate PR Review"). A provider
SDK error can echo the request it made, including a query-string API key, so
the skip note the script logs to stderr must never carry the key's value even
though GitHub's log masking would usually catch a registered secret
(2026-09-07 diagnostic S:CI-03).

The script is stdlib-only at import time (the reviewer SDK is imported lazily
inside review_pr), so it is loaded here straight from its path; .github/ is
not a package and is outside the core ruff/coverage gates on purpose.
"""

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / ".github" / "scripts" / "pr_review.py"

# A placeholder shaped like the provider's keys; it is not a real credential.
FAKE_KEY = "AIzaSyFAKE-placeholder-key-for-tests-000"


@pytest.fixture(scope="module")
def pr_review():
    spec = importlib.util.spec_from_file_location("pr_review_under_test", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_skip_note_never_echoes_the_provider_key(pr_review, monkeypatch, capsys):
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)
    exc = RuntimeError(f"POST https://generativelanguage.googleapis.com/v1?key={FAKE_KEY} failed: 503")

    pr_review._emit_skipped_note(exc)

    captured = capsys.readouterr()
    assert FAKE_KEY not in captured.err
    assert FAKE_KEY not in captured.out
    # The redacted remainder must still identify what failed.
    assert "RuntimeError" in captured.err
    assert "503" in captured.err
    assert "[REDACTED]" in captured.err


def test_skip_note_is_unchanged_when_no_key_is_configured(pr_review, monkeypatch, capsys):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    pr_review._emit_skipped_note(OSError("no such file: review_input/diff.patch"))

    captured = capsys.readouterr()
    assert "no such file: review_input/diff.patch" in captured.err
    assert "temporary error in the review tooling" in captured.out


def test_rate_limit_skip_note_is_still_recognised_after_redaction(pr_review, monkeypatch, capsys):
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)

    pr_review._emit_skipped_note(RuntimeError(f"429 RESOURCE_EXHAUSTED for key={FAKE_KEY}"))

    captured = capsys.readouterr()
    assert FAKE_KEY not in captured.err
    assert "rate-limited" in captured.out


# ---------------------------------------------------------------------------
# _generate_review: bounded retry-with-backoff around the provider call.
#
# A 429/quota error is sometimes a per-minute limit that has cleared a few
# seconds later, not just the daily-exhaustion case that no amount of
# in-job backoff can fix. These tests use a fake async agent (no real SDK,
# no real network) and a recording fake `sleep` so the suite stays fast and
# deterministic; they pin the *behavior* (which errors retry, how many
# times, growing delay, non-rate-limit errors bypass the loop) rather than
# timing.
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Minimal stand-in for the SDK's async-iterable chat response."""

    def __init__(self, tokens):
        self._tokens = tokens

    def __aiter__(self):
        return self._aiter()

    async def _aiter(self):
        for token in self._tokens:
            yield token


class _FakeAgent:
    """Minimal stand-in for `google.antigravity.Agent`'s async context manager."""

    def __init__(self, chat_outcome):
        # chat_outcome is either a list of tokens (success) or an exception
        # instance to raise from chat().
        self._chat_outcome = chat_outcome

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def chat(self, prompt):
        if isinstance(self._chat_outcome, BaseException):
            raise self._chat_outcome
        return _FakeResponse(self._chat_outcome)


def _agent_factory_from_outcomes(outcomes):
    """Return an agent_factory() that yields one _FakeAgent per call, in order."""
    outcomes_iter = iter(outcomes)

    def agent_factory():
        return _FakeAgent(next(outcomes_iter))

    return agent_factory


def test_generate_review_retries_rate_limit_errors_then_succeeds(pr_review):
    delays = []

    async def sleep(seconds):
        delays.append(seconds)

    agent_factory = _agent_factory_from_outcomes(
        [
            RuntimeError("429 RESOURCE_EXHAUSTED"),
            RuntimeError("429 RESOURCE_EXHAUSTED"),
            ["Looks", " good."],
        ]
    )

    result = asyncio.run(
        pr_review._generate_review(
            agent_factory,
            "prompt",
            max_attempts=3,
            base_delay_seconds=2.0,
            sleep=sleep,
        )
    )

    assert result == "Looks good."
    # Two retries before the third, successful attempt: backoff doubles.
    assert delays == [2.0, 4.0]


def test_generate_review_does_not_retry_non_rate_limit_errors(pr_review):
    delays = []

    async def sleep(seconds):
        delays.append(seconds)

    boom = ValueError("malformed response")
    agent_factory = _agent_factory_from_outcomes([boom, ["unreached"]])

    with pytest.raises(ValueError, match="malformed response"):
        asyncio.run(
            pr_review._generate_review(
                agent_factory,
                "prompt",
                max_attempts=3,
                base_delay_seconds=2.0,
                sleep=sleep,
            )
        )

    assert delays == []


def test_generate_review_raises_after_exhausting_max_attempts(pr_review):
    delays = []

    async def sleep(seconds):
        delays.append(seconds)

    agent_factory = _agent_factory_from_outcomes(
        [
            RuntimeError("429 quota exceeded"),
            RuntimeError("429 quota exceeded"),
            RuntimeError("429 quota exceeded"),
        ]
    )

    with pytest.raises(RuntimeError, match="quota exceeded"):
        asyncio.run(
            pr_review._generate_review(
                agent_factory,
                "prompt",
                max_attempts=3,
                base_delay_seconds=1.0,
                sleep=sleep,
            )
        )

    # Only 2 backoff sleeps precede the 3rd (final, non-retried) attempt.
    assert delays == [1.0, 2.0]
