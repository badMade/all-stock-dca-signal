"""Tests for the sentiment_score() orchestration and FinbertSentimentAnalyzer.

sentiment_score() itself needs no torch/transformers to test: it accepts an
injectable `analyzer` (any object with a `score_texts` method), so these
tests use a fake analyzer and monkeypatch the fetch functions to avoid any
network call. Only FinbertSentimentAnalyzer's own construction actually
needs the model stack, guarded below with importorskip.
"""

import os
import sys
import types
from unittest import mock

import pytest

import signals

_MODEL_DOWNLOAD_OPT_IN = "TICKER_ADVISOR_RUN_MODEL_DOWNLOAD_TESTS"


class _FakeAnalyzer:
    """Deterministic stand-in for FinbertSentimentAnalyzer -- no model, no torch."""

    def score_texts(self, texts):
        return [
            {"text": text, "positive": 0.8, "negative": 0.1, "neutral": 0.1, "label": "positive", "confidence": 0.8}
            for text in texts
        ]


def test_sentiment_score_combines_news_and_sec_via_injected_analyzer(monkeypatch):
    news = [{"ticker": "ACME", "text": "great news"}]
    sec = [{"ticker": "ACME", "text": "10-K filed"}]
    monkeypatch.setattr(signals, "fetch_news_headlines", lambda ticker, **kwargs: news)
    monkeypatch.setattr(signals, "fetch_sec_filings", lambda ticker, **kwargs: sec)

    result = signals.sentiment_score("ACME", news_api_key="fake-key", analyzer=_FakeAnalyzer())

    assert result is not None
    assert result["n_news_headlines"] == 1
    assert result["n_sec_filings"] == 1
    assert result["news_api_key_used"] is True
    assert result["score"] == pytest.approx(0.7)  # (0.8 - 0.1) * 1.0 weighted avg


def test_sentiment_score_passes_as_of_to_both_fetchers(monkeypatch):
    # as_of anchors both fetches to the SAME instant instead of each one
    # independently recomputing "now" -- a run spanning UTC midnight
    # would otherwise give candidates before and after midnight different
    # windows purely from scoring order (Antigravity review on PR #403:
    # "Anchor sentiment lookbacks to the run timestamp"). fetch_sec_
    # filings takes a date (not a datetime), since EDGA's startdt/enddt
    # are calendar dates.
    from datetime import datetime, timezone

    captured = {}
    run_instant = datetime(2020, 6, 15, 12, 30, tzinfo=timezone.utc)

    def fake_news(ticker, **kwargs):
        captured["news_as_of"] = kwargs.get("as_of")
        return []

    def fake_sec(ticker, **kwargs):
        captured["sec_as_of"] = kwargs.get("as_of")
        return []

    monkeypatch.setattr(signals, "fetch_news_headlines", fake_news)
    monkeypatch.setattr(signals, "fetch_sec_filings", fake_sec)

    signals.sentiment_score("ACME", news_api_key="fake-key", analyzer=_FakeAnalyzer(), as_of=run_instant)

    assert captured["news_as_of"] == run_instant
    assert captured["sec_as_of"] == run_instant.date()


def test_sentiment_score_returns_none_when_no_text_found(monkeypatch):
    monkeypatch.setattr(signals, "fetch_news_headlines", lambda ticker, **kwargs: [])
    monkeypatch.setattr(signals, "fetch_sec_filings", lambda ticker, **kwargs: [])

    result = signals.sentiment_score("ACME", news_api_key=None, analyzer=_FakeAnalyzer())

    assert result is None


def test_sentiment_score_works_without_a_news_api_key(monkeypatch):
    sec = [{"ticker": "ACME", "text": "10-K filed"}]
    monkeypatch.setattr(signals, "fetch_news_headlines", lambda ticker, **kwargs: [])
    monkeypatch.setattr(signals, "fetch_sec_filings", lambda ticker, **kwargs: sec)

    result = signals.sentiment_score("ACME", news_api_key=None, analyzer=_FakeAnalyzer())

    assert result is not None
    assert result["news_api_key_used"] is False
    assert result["n_news_headlines"] == 0
    assert result["n_sec_filings"] == 1


def test_sentiment_score_reports_sec_fetch_failure_instead_of_none(monkeypatch):
    # A genuine SEC fetch failure must not look identical to "there was
    # simply no sentiment data" -- that silent conflation could change an
    # ADD/WATCH/REJECT result with no explanation left in the persisted
    # artifacts (Codex review on PR #403: "Preserve SEC request failures in
    # the report").
    def _fake_fetch_sec_filings(ticker, **kwargs):
        kwargs["errors"].append(f"SEC EDGAR fetch failed for {ticker}: boom")
        return []

    monkeypatch.setattr(signals, "fetch_news_headlines", lambda ticker, **kwargs: [])
    monkeypatch.setattr(signals, "fetch_sec_filings", _fake_fetch_sec_filings)

    result = signals.sentiment_score("ACME", news_api_key=None, analyzer=_FakeAnalyzer())

    assert result is not None
    assert result["score"] is None
    assert any("boom" in message for message in result["errors"])


def test_sentiment_score_deduplicates_repeated_articles(monkeypatch):
    # A syndicated/duplicated story must not cast multiple confidence-
    # weighted votes in compute_ticker_sentiment() -- only the analyzer call
    # count (one per DISTINCT text) reveals whether dedup actually happened,
    # since a naive average of [x, x] equals x either way (Codex review on
    # PR #403: "Deduplicate repeated news articles before sentiment
    # scoring").
    news = [
        {"ticker": "ACME", "text": "Great earnings beat"},
        {"ticker": "ACME", "text": "  great earnings beat  "},  # same story, re-cased/re-spaced
    ]
    monkeypatch.setattr(signals, "fetch_news_headlines", lambda ticker, **kwargs: news)
    monkeypatch.setattr(signals, "fetch_sec_filings", lambda ticker, **kwargs: [])

    scored_texts = []

    class _CountingAnalyzer:
        def score_texts(self, texts):
            scored_texts.extend(texts)
            return [
                {"text": t, "positive": 0.8, "negative": 0.1, "neutral": 0.1, "label": "positive", "confidence": 0.8}
                for t in texts
            ]

    result = signals.sentiment_score("ACME", news_api_key="fake-key", analyzer=_CountingAnalyzer())

    assert result is not None
    assert len(scored_texts) == 1  # the duplicate never reached the analyzer


def test_finbert_sentiment_analyzer_real_model():
    pytest.importorskip("torch")
    pytest.importorskip("transformers")
    if not os.environ.get(_MODEL_DOWNLOAD_OPT_IN):
        # Even with torch/transformers installed, constructing the real
        # analyzer downloads FinBERT weights from the Hugging Face Hub on
        # first use -- that makes the default `python -m pytest tests/`
        # fail in an offline, proxied, rate-limited, or temporarily
        # unavailable environment even though the code under test is fine.
        # Gate this one network-dependent test behind an explicit opt-in
        # rather than the whole default suite on external service
        # availability (Codex review on PR #403: "Keep the model download
        # out of the default test suite").
        pytest.skip(f"set {_MODEL_DOWNLOAD_OPT_IN}=1 to run (downloads FinBERT from the Hugging Face Hub)")

    analyzer = signals.FinbertSentimentAnalyzer()
    scored = analyzer.score_texts(["Company reports record profits and raises guidance."])

    assert len(scored) == 1
    entry = scored[0]
    assert entry["label"] in {"positive", "negative", "neutral"}
    assert 0.0 <= entry["confidence"] <= 1.0
    assert entry["positive"] + entry["negative"] + entry["neutral"] == pytest.approx(1.0, abs=1e-3)


def test_finbert_sentiment_analyzer_passes_same_revision_to_both_downloads():
    # Two separate from_pretrained() calls against the mutable default
    # branch ("main") could each resolve a DIFFERENT commit if the
    # upstream repository changes in between -- resolving the revision
    # ONCE and reusing it for both the tokenizer and model downloads is
    # what actually guarantees they're pinned to the same commit (Codex
    # review on PR #403: "Pin and record the FinBERT revision"). No real
    # torch/transformers install needed -- a fake stand-in module is
    # enough to verify the wiring.
    captured = {}

    class _FakeConfig:
        id2label = {0: "negative", 1: "neutral", 2: "positive"}

    class _FakeModel:
        config = _FakeConfig()

        def eval(self):
            return self

    def fake_tokenizer_from_pretrained(model_name, revision=None):
        captured["tokenizer_revision"] = revision
        return object()

    def fake_model_from_pretrained(model_name, revision=None):
        captured["model_revision"] = revision
        return _FakeModel()

    fake_transformers = types.SimpleNamespace(
        AutoTokenizer=types.SimpleNamespace(from_pretrained=fake_tokenizer_from_pretrained),
        AutoModelForSequenceClassification=types.SimpleNamespace(from_pretrained=fake_model_from_pretrained),
    )

    with mock.patch.dict(sys.modules, {"transformers": fake_transformers}):
        analyzer = signals.FinbertSentimentAnalyzer(revision="abc123")

    assert analyzer.revision == "abc123"
    assert captured["tokenizer_revision"] == "abc123"
    assert captured["model_revision"] == "abc123"


def test_finbert_sentiment_analyzer_raises_when_revision_resolution_fails(monkeypatch):
    # A resolution failure must propagate, not silently fall back to the
    # unresolved "main" -- a silent fallback would let construction
    # succeed and persist finbert_revision: "main" indistinguishable from
    # a genuinely-pinned run, defeating the auditability this exists for
    # (Codex review on PR #429: "Reject unresolved mutable FinBERT
    # revisions"). advise_tickers.py's caller already degrades a
    # construction failure to skipping sentiment for the run, so raising
    # here is safe -- it does not crash the whole advisory run.
    monkeypatch.delenv("FINBERT_MODEL_REVISION", raising=False)

    class _FakeConfig:
        id2label = {0: "negative", 1: "neutral", 2: "positive"}

    class _FakeModel:
        config = _FakeConfig()

        def eval(self):
            return self

    fake_transformers = types.SimpleNamespace(
        AutoTokenizer=types.SimpleNamespace(from_pretrained=lambda model_name, revision=None: object()),
        AutoModelForSequenceClassification=types.SimpleNamespace(
            from_pretrained=lambda model_name, revision=None: _FakeModel()
        ),
    )

    def failing_resolver(model_name, revision):
        raise ConnectionError("no network egress")

    with mock.patch.dict(sys.modules, {"transformers": fake_transformers}):
        with pytest.raises(ConnectionError):
            signals.FinbertSentimentAnalyzer(revision_resolver=failing_resolver)


def test_finbert_sentiment_analyzer_raises_when_resolver_returns_unusable_value(monkeypatch):
    monkeypatch.delenv("FINBERT_MODEL_REVISION", raising=False)

    fake_transformers = types.SimpleNamespace(
        AutoTokenizer=types.SimpleNamespace(from_pretrained=lambda model_name, revision=None: object()),
        AutoModelForSequenceClassification=types.SimpleNamespace(
            from_pretrained=lambda model_name, revision=None: object()
        ),
    )

    with mock.patch.dict(sys.modules, {"transformers": fake_transformers}):
        with pytest.raises(ValueError):
            signals.FinbertSentimentAnalyzer(revision_resolver=lambda model_name, revision: None)


def test_finbert_sentiment_analyzer_resolves_main_to_a_commit_sha():
    # The mutable "main" branch previously stayed "main" in both
    # self.revision and every from_pretrained() call -- so identical
    # parameters could load a different upstream commit run over run while
    # scores.json kept recording finbert_revision: "main", defeating the
    # auditability that metadata exists for (Codex review on PR #429:
    # "Resolve an immutable FinBERT revision"). A resolved SHA must be
    # threaded through to both downloads AND persisted on the instance.
    captured = {}

    class _FakeConfig:
        id2label = {0: "negative", 1: "neutral", 2: "positive"}

    class _FakeModel:
        config = _FakeConfig()

        def eval(self):
            return self

    def fake_tokenizer_from_pretrained(model_name, revision=None):
        captured["tokenizer_revision"] = revision
        return object()

    def fake_model_from_pretrained(model_name, revision=None):
        captured["model_revision"] = revision
        return _FakeModel()

    fake_transformers = types.SimpleNamespace(
        AutoTokenizer=types.SimpleNamespace(from_pretrained=fake_tokenizer_from_pretrained),
        AutoModelForSequenceClassification=types.SimpleNamespace(from_pretrained=fake_model_from_pretrained),
    )

    def fake_resolver(model_name, revision):
        assert revision == "main"
        return "deadbeef" * 5

    with mock.patch.dict(sys.modules, {"transformers": fake_transformers}):
        analyzer = signals.FinbertSentimentAnalyzer(revision_resolver=fake_resolver)

    assert analyzer.revision == "deadbeef" * 5
    assert captured["tokenizer_revision"] == "deadbeef" * 5
    assert captured["model_revision"] == "deadbeef" * 5


def test_finbert_sentiment_analyzer_keeps_explicit_pin_unresolved():
    # An explicit revision (a tag, branch, or SHA the caller/deployment
    # chose) is already a pin -- resolving it further would silently
    # override that choice, so the resolver must not be consulted at all.
    captured = {}

    class _FakeConfig:
        id2label = {0: "negative", 1: "neutral", 2: "positive"}

    class _FakeModel:
        config = _FakeConfig()

        def eval(self):
            return self

    def fake_tokenizer_from_pretrained(model_name, revision=None):
        captured["revision"] = revision
        return object()

    def fake_model_from_pretrained(model_name, revision=None):
        return _FakeModel()

    fake_transformers = types.SimpleNamespace(
        AutoTokenizer=types.SimpleNamespace(from_pretrained=fake_tokenizer_from_pretrained),
        AutoModelForSequenceClassification=types.SimpleNamespace(from_pretrained=fake_model_from_pretrained),
    )

    def fail_resolver(model_name, revision):
        raise AssertionError("resolver must not run for an explicit pin")

    with mock.patch.dict(sys.modules, {"transformers": fake_transformers}):
        analyzer = signals.FinbertSentimentAnalyzer(revision="v1.0", revision_resolver=fail_resolver)

    assert analyzer.revision == "v1.0"
    assert captured["revision"] == "v1.0"
