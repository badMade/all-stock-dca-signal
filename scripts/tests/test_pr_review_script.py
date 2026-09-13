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
