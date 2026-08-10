"""Tests for advise_tickers.py's CLI-support helpers -- no heavy deps needed."""

import argparse
import contextlib
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

import advise_tickers
from advise_tickers import (
    PriceHistoryUnavailable,
    _allocate_default_output_dir,
    _json_safe,
    _load_candidates,
    _load_current_universe,
    _load_legacy_symbol_corrections,
    _parse_weights,
    _positive_int,
    _price_returns,
    _sanitize_error_message,
    _split_legacy_symbols,
    _split_valid_symbol_shape,
    _write_atomic,
    score_candidate,
)


def _usable_fake_frame(periods=60):
    """A Close/Adj Close price series with enough rows and genuine
    variance/drawdown to give strategy_health_score() a finite Sharpe or
    Calmar, unlike a tiny flat 2-row fixture.

    signals.MIN_OBSERVATIONS requires >= 30 return observations before
    Sharpe/Calmar are even attempted, and _sharpe()/_calmar()'s own
    zero-variance/zero-drawdown guards return NaN for a flat or
    monotonic series -- main()'s "at least one usable weighted signal"
    check (Antigravity review on PR #403: "Fail when all nonzero-weight
    signals are unavailable" / "Check weighted metric usability, not
    container presence") now requires one of these end-to-end main()
    fixtures to actually clear those guards, not just return a non-None
    dict full of NaN.
    """
    closes = [100.0 + i * 0.3 + (4.0 if i % 2 == 0 else -2.5) for i in range(periods)]
    return pd.DataFrame({"Close": closes, "Adj Close": closes})


def test_module_docstring_lists_all_three_proposed_edit_targets():
    # This module's docstring is used verbatim as the argparse --help
    # description (`ArgumentParser(description=__doc__, ...)` below) --
    # render_proposed_diff() emits a required THIRD edit for
    # config/scheduled_tasks.json alongside config/tickers.json and
    # near_close_dca.ACTIVE_TICKERS, and an operator relying on --help who
    # only saw the two-file version could apply just those two, leaving the
    # scheduled execution universe stale (Codex review on PR #403: "Include
    # the scheduled-task edit in the CLI contract").
    assert advise_tickers.__doc__ is not None
    assert "config/tickers.json" in advise_tickers.__doc__
    assert "near_close_dca.ACTIVE_TICKERS" in advise_tickers.__doc__
    assert "config/scheduled_tasks.json" in advise_tickers.__doc__


def test_repo_root_does_not_shadow_ticker_advisor_directory_in_sys_path():
    # Index 0 is already this module's own directory (ticker-advisor/, set
    # by the interpreter/pytest before any of this module's code runs) --
    # putting REPO_ROOT ahead of it would let a same-named top-level
    # module there (a future repo-root "signals.py"/"pricing.py"/etc.)
    # shadow this subproject's own submodules (Antigravity review on PR
    # #403: "sys.path Precedences in advise_tickers.py").
    repo_root_str = str(advise_tickers.REPO_ROOT)

    assert repo_root_str in sys.path
    assert sys.path.index(repo_root_str) != 0


def test_parse_weights_none_when_not_supplied():
    assert _parse_weights(None) is None
    assert _parse_weights("") is None


def test_parse_weights_valid_keys():
    weights = _parse_weights("health=2,backtest=1,sentiment=0")

    assert weights == {"health": 2.0, "backtest": 1.0, "sentiment": 0.0}


def test_parse_weights_rejects_unknown_key():
    with pytest.raises(ValueError, match="unknown --weights key"):
        _parse_weights("heath=2,backtest=1,sentiment=1")


def test_parse_weights_rejects_malformed_pair():
    with pytest.raises(ValueError, match="malformed --weights entry"):
        _parse_weights("bogus")


def test_parse_weights_rejects_non_numeric_value():
    with pytest.raises(ValueError, match="malformed --weights value"):
        _parse_weights("health=not-a-number")


def test_parse_weights_rejects_nan_value():
    # float("nan") parses without raising and even clears a naive `< 0`
    # check (NaN compares False to everything) -- it would otherwise
    # contaminate weighted_sum/weight_total and silently turn an otherwise
    # usable candidate into WATCH-with-no-composite (Codex review on PR
    # #403: "Reject non-finite custom weights").
    with pytest.raises(ValueError, match="must be finite"):
        _parse_weights("health=nan")


def test_parse_weights_rejects_infinite_value():
    with pytest.raises(ValueError, match="must be finite"):
        _parse_weights("health=inf")


def test_parse_weights_rejects_negative_value():
    # A negative weight would invert that signal's contribution to the
    # composite (rewarding a candidate for scoring badly on it) rather than
    # simply zeroing it out -- --weights health=0 is the correct way to
    # exclude a signal (Antigravity review on PR #403: "Reject negative
    # --weights values").
    with pytest.raises(ValueError, match="must be non-negative"):
        _parse_weights("health=-1")


def test_positive_int_accepts_positive_value():
    assert _positive_int("3") == 3


def test_positive_int_rejects_zero_and_negative():
    # A non-positive --lookback-years silently windows to an empty/nonsense
    # range instead of erroring -- health/backtest quietly disappear while
    # the run still emits normal-looking, sentiment-only classifications
    # (Codex review on PR #403: "Require a positive lookback window").
    with pytest.raises(argparse.ArgumentTypeError):
        _positive_int("0")
    with pytest.raises(argparse.ArgumentTypeError):
        _positive_int("-1")


def test_sanitize_error_message_redacts_urls():
    # A Hugging Face Hub download failure can embed a fully authenticated/
    # signed request URL in its exception text -- persisting str(exc)
    # verbatim would copy that credential into run_metadata and committed
    # analysis/ artifacts (Codex review on PR #403: "Sanitize sentiment
    # initialization errors before persisting them").
    exc = ConnectionError("failed fetching https://huggingface.co/models?token=SECRET123 after 3 tries")

    sanitized = _sanitize_error_message(exc)

    assert "SECRET123" not in sanitized
    assert "https://" not in sanitized
    assert "ConnectionError" in sanitized
    assert "after 3 tries" in sanitized


def test_sanitize_error_message_leaves_url_free_message_untouched():
    exc = RuntimeError("model not found locally")

    sanitized = _sanitize_error_message(exc)

    assert sanitized == "RuntimeError: model not found locally"


def test_load_legacy_symbol_corrections_reads_config(tmp_path):
    (tmp_path / "config").mkdir()
    payload = {"active": ["IRDM"], "excluded": [], "symbol_corrections": {"IRDMI": "IRDM"}}
    (tmp_path / "config" / "tickers.json").write_text(json.dumps(payload), encoding="utf-8")

    corrections = _load_legacy_symbol_corrections(tmp_path)

    assert corrections == {"IRDMI": "IRDM"}


def test_load_legacy_symbol_corrections_missing_file_degrades_to_empty_dict(tmp_path):
    assert _load_legacy_symbol_corrections(tmp_path) == {}


@pytest.mark.parametrize("top_level", [None, [], "a string", 42])
def test_load_legacy_symbol_corrections_non_object_top_level_degrades_to_empty_dict(tmp_path, top_level):
    # Syntactically valid JSON with a non-object top level (null, a bare
    # list, ...) makes payload.get() raise AttributeError, OUTSIDE the
    # except clause, aborting the whole CLI with a traceback before any
    # candidates are scored -- contradicting this function's own
    # documented "degrade to an empty map" contract for a malformed config
    # file (Codex review on PR #403: "Validate the ticker-config root
    # before reading mappings").
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "tickers.json").write_text(json.dumps(top_level), encoding="utf-8")

    assert _load_legacy_symbol_corrections(tmp_path) == {}


@pytest.mark.parametrize("top_level", [None, [], "a string", 42])
def test_load_current_universe_non_object_top_level_degrades_to_empty_sets(tmp_path, top_level):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "tickers.json").write_text(json.dumps(top_level), encoding="utf-8")

    active, excluded = _load_current_universe(tmp_path)

    assert active == frozenset()
    assert excluded == frozenset()


def test_split_legacy_symbols_excludes_corrected_candidates():
    # "IRDMI" satisfies the ticker schema and is neither active nor
    # excluded, so without this filter it would still participate in
    # cross-candidate normalization and could be proposed as an ADD right
    # alongside the canonical "IRDM" it's a legacy alias for (Codex review
    # on PR #403: "Reject corrected legacy symbols before scoring").
    kept, legacy = _split_legacy_symbols(["AAPL", "IRDMI", "GOOD"], {"IRDMI": "IRDM"})

    assert kept == ["AAPL", "GOOD"]
    assert legacy == ["IRDMI"]


def test_split_valid_symbol_shape_separates_invalid_candidates():
    # A schema-incompatible candidate like "BRK-B" must be excluded BEFORE
    # scoring, not only from the final proposed diff -- otherwise it still
    # participates in combine_scores()'s cross-candidate z-score
    # normalization and can shift every other candidate's composite (Codex
    # review on PR #403: "Validate symbols before cross-candidate
    # normalization").
    valid, invalid = _split_valid_symbol_shape(["AAPL", "BRK-B", "GOOD"])

    assert valid == ["AAPL", "GOOD"]
    assert invalid == ["BRK-B"]


def test_split_valid_symbol_shape_all_valid():
    valid, invalid = _split_valid_symbol_shape(["AAPL", "MSFT"])

    assert valid == ["AAPL", "MSFT"]
    assert invalid == []


def test_split_ineligible_universe_symbols_excludes_active_and_excluded():
    # An already-active or explicitly-excluded candidate must be dropped
    # BEFORE scoring, not only from render_proposed_diff()'s later output
    # filter -- otherwise it still participates in combine_scores()'s
    # cross-candidate z-score normalization and can shift every other
    # candidate's composite even though its own proposed edit was always
    # going to be discarded downstream (Codex review on PR #403: "Exclude
    # ineligible symbols before normalization").
    from advise_tickers import _split_ineligible_universe_symbols

    eligible, dropped = _split_ineligible_universe_symbols(
        ["AAPL", "BADCO", "GOOD"], active_tickers=frozenset({"AAPL"}), excluded_tickers=frozenset({"BADCO"})
    )

    assert eligible == ["GOOD"]
    assert dropped == ["AAPL", "BADCO"]


def test_split_ineligible_universe_symbols_none_ineligible():
    from advise_tickers import _split_ineligible_universe_symbols

    eligible, dropped = _split_ineligible_universe_symbols(
        ["AAPL", "MSFT"], active_tickers=frozenset(), excluded_tickers=frozenset()
    )

    assert eligible == ["AAPL", "MSFT"]
    assert dropped == []


def test_allocate_default_output_dir_avoids_same_second_collision(tmp_path):
    # Two default-path invocations starting within the same second (e.g.
    # parallel agent/automation runs) previously collided on a bare
    # %H%M%S-formatted directory, silently writing the same three filenames
    # and risking overwritten/interleaved reports and proposed diffs.
    # Exclusive creation with a retry guarantees uniqueness even when both
    # calls share the exact same `now` (Codex review on PR #403: "Allocate
    # a unique directory for concurrent runs").
    now = datetime(2026, 8, 10, 15, 30, 0, 123456)

    first = _allocate_default_output_dir(tmp_path, now)
    second = _allocate_default_output_dir(tmp_path, now)

    assert first is not None
    assert second is not None
    assert first != second
    assert first.is_dir()
    assert second.is_dir()


def test_allocate_default_output_dir_returns_none_when_exhausted(tmp_path):
    now = datetime(2026, 8, 10, 15, 30, 0, 123456)

    result = _allocate_default_output_dir(tmp_path, now, max_attempts=0)

    assert result is None


def test_write_atomic_writes_full_content_and_leaves_no_temp_file(tmp_path):
    # write_text() writes in place -- a process interrupted mid-write
    # leaves a truncated file behind. os.replace() (what _write_atomic()
    # uses) only ever swaps in a COMPLETE file, so an interrupted run
    # leaves either the old complete generation or the new one, never a
    # truncated one (Codex review on PR #403: "Publish the report files
    # without leaving mixed generations").
    target = tmp_path / "scores.json"

    _write_atomic(target, "hello world")

    assert target.read_text(encoding="utf-8") == "hello world"
    assert list(tmp_path.iterdir()) == [target]  # no leftover .tmp-* file


def test_write_atomic_replaces_existing_file_content(tmp_path):
    target = tmp_path / "scores.json"
    target.write_text("stale content", encoding="utf-8")

    _write_atomic(target, "fresh content")

    assert target.read_text(encoding="utf-8") == "fresh content"


def test_write_atomic_uses_unique_temp_names_across_calls(tmp_path, monkeypatch):
    # The PID alone isn't a unique temp-file token if this function is
    # ever called concurrently from multiple threads within the same
    # process (main() itself is a plain sequential loop today, but a
    # future caller might not be) -- two threads writing the same
    # destination filename would collide on the identical temp path
    # (Antigravity review on PR #403: "Race Condition Risk in
    # _write_atomic for Same-Process Threads").
    captured_tmp_names = []
    original_write_text = Path.write_text

    def capturing_write_text(self, *args, **kwargs):
        captured_tmp_names.append(self.name)
        return original_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", capturing_write_text)

    target = tmp_path / "scores.json"
    _write_atomic(target, "first")
    _write_atomic(target, "second")

    assert len(captured_tmp_names) == 2
    assert captured_tmp_names[0] != captured_tmp_names[1]


def test_load_candidates_dedupes_and_uppercases():
    result = _load_candidates(["coin", "COIN", "pltr"], None)

    assert result == ["COIN", "PLTR"]


def test_load_candidates_strips_whitespace_from_cli_values():
    # --candidates-file lines are already stripped, but a --candidates CLI
    # value with incidental surrounding whitespace (e.g. from a wrapper
    # script that didn't trim) previously failed TICKER_SYMBOL_PATTERN and
    # got silently dropped as "invalid-shaped" even though the ticker
    # itself was fine (Antigravity review on PR #403: "Candidate Input
    # Whitespace Normalization").
    result = _load_candidates([" coin ", "COIN", " pltr"], None)

    assert result == ["COIN", "PLTR"]


def test_load_candidates_missing_file_raises_value_error(tmp_path):
    missing = tmp_path / "does-not-exist.txt"

    with pytest.raises(ValueError, match="not found"):
        _load_candidates([], missing)


def test_load_candidates_file_skips_comments_and_blank_lines(tmp_path):
    path = tmp_path / "candidates.txt"
    path.write_text("COIN\n# a comment\n\nPLTR\n", encoding="utf-8")

    result = _load_candidates([], path)

    assert result == ["COIN", "PLTR"]


def test_load_candidates_file_is_a_directory_raises_value_error(tmp_path):
    # IsADirectoryError is an OSError subclass, not FileNotFoundError -- the
    # earlier handler only caught the latter, so this bypassed main()'s
    # user-facing "error: ..." exit path and terminated the CLI with a raw
    # traceback instead (Codex review on PR #403: "Convert all
    # candidate-file read failures into CLI errors").
    directory = tmp_path / "a-directory"
    directory.mkdir()

    with pytest.raises(ValueError, match="could not be read"):
        _load_candidates([], directory)


def test_load_candidates_file_invalid_utf8_raises_value_error(tmp_path):
    # UnicodeDecodeError is a ValueError subclass, not an OSError one --
    # neither the FileNotFoundError handler nor a bare OSError catch would
    # have caught it.
    path = tmp_path / "candidates.txt"
    path.write_bytes(b"\xff\xfe\x00COIN")

    with pytest.raises(ValueError, match="could not be read"):
        _load_candidates([], path)


def test_load_current_universe_reads_active_and_excluded(tmp_path):
    (tmp_path / "config").mkdir()
    payload = {"active": ["AAPL", "MSFT"], "excluded": ["BANNED"]}
    (tmp_path / "config" / "tickers.json").write_text(json.dumps(payload), encoding="utf-8")

    active, excluded = _load_current_universe(tmp_path)

    assert active == frozenset({"AAPL", "MSFT"})
    assert excluded == frozenset({"BANNED"})


def test_load_current_universe_uppercases_entries(tmp_path):
    # Candidates are normalized to uppercase (_load_candidates()'s
    # .strip().upper()) -- a lowercase/mixed-case entry in
    # config/tickers.json would otherwise silently fail to match an
    # uppercased candidate, letting an already-active or explicitly-
    # excluded symbol through _split_ineligible_universe_symbols()
    # undetected (Antigravity review on PR #403: "Case Normalization in
    # _load_current_universe").
    (tmp_path / "config").mkdir()
    payload = {"active": ["aapl", "Msft"], "excluded": ["banned"]}
    (tmp_path / "config" / "tickers.json").write_text(json.dumps(payload), encoding="utf-8")

    active, excluded = _load_current_universe(tmp_path)

    assert active == frozenset({"AAPL", "MSFT"})
    assert excluded == frozenset({"BANNED"})


def test_load_current_universe_missing_file_degrades_to_empty_sets(tmp_path):
    active, excluded = _load_current_universe(tmp_path)

    assert active == frozenset()
    assert excluded == frozenset()


@pytest.mark.parametrize("malformed_active", [None, {}, "AAPL", 5])
def test_load_current_universe_warns_on_malformed_active_value(tmp_path, capsys, malformed_active):
    # A schema-invalid but PRESENT value ("active": null or "active": {})
    # previously fell through the isinstance(active, list) check silently
    # -- with no warning at all, unlike every other malformed-config case
    # this function degrades through. An actually-active candidate could
    # then be scored and proposed as a duplicate addition, while the
    # report gave no indication the universe-filtering check had failed
    # (Antigravity review on PR #403: "Warn on malformed active and
    # excluded collections").
    (tmp_path / "config").mkdir()
    payload = {"active": malformed_active, "excluded": []}
    (tmp_path / "config" / "tickers.json").write_text(json.dumps(payload), encoding="utf-8")

    active, excluded = _load_current_universe(tmp_path)

    assert active == frozenset()
    assert excluded == frozenset()
    assert "warning: could not read" in capsys.readouterr().err


def test_load_current_universe_missing_active_key_warns(tmp_path, capsys):
    # "active" is schema-REQUIRED (schemas/tickers.schema.json's own
    # "required": ["active"]) -- its total absence is exactly as malformed
    # as a present-but-wrong-type value. An already-active candidate could
    # otherwise be scored and proposed as a duplicate addition with the
    # report giving no indication the universe-filtering check had failed
    # (Antigravity review on PR #403: "Treat a missing active universe as
    # malformed").
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "tickers.json").write_text(json.dumps({}), encoding="utf-8")

    active, excluded = _load_current_universe(tmp_path)

    assert active == frozenset()
    assert excluded == frozenset()
    assert "warning: could not read" in capsys.readouterr().err


def test_load_current_universe_absent_excluded_key_degrades_silently(tmp_path, capsys):
    # Unlike "active", "excluded" is NOT schema-required -- its absence
    # (with "active" present and valid) is the benign, ordinary "no
    # exclusions configured" case, not a malformed-config signal.
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "tickers.json").write_text(json.dumps({"active": ["AAPL"]}), encoding="utf-8")

    active, excluded = _load_current_universe(tmp_path)

    assert active == frozenset({"AAPL"})
    assert excluded == frozenset()
    assert capsys.readouterr().err == ""


def test_json_safe_replaces_nan_and_inf_with_none():
    payload = {"a": float("nan"), "b": float("inf"), "c": [1.0, float("-inf"), {"d": float("nan")}]}

    safe = _json_safe(payload)

    assert safe == {"a": None, "b": None, "c": [1.0, None, {"d": None}]}
    json.dumps(safe)  # must not contain bare NaN/Infinity tokens


def test_json_safe_leaves_finite_values_untouched():
    payload = {"score": 0.5, "count": 3, "label": "ADD", "nested": {"x": 1.0}}

    assert _json_safe(payload) == payload


def test_json_safe_output_round_trips_through_strict_json_parse():
    payload = {"sharpe": float("nan"), "sortino": 1.5}

    text = json.dumps(_json_safe(payload))
    reparsed = json.loads(text)  # strict parse -- would fail on a bare NaN token

    assert reparsed == {"sharpe": None, "sortino": 1.5}
    assert not math.isnan(reparsed["sortino"])


def test_price_returns_excludes_gap_period_instead_of_bridging_it():
    # A NaN close must not be bridged into a single fabricated return
    # spanning the gap -- Sharpe/CAGR/Calmar assume every return row is one
    # trading day apart, so compressing a multi-session move into one row
    # inflates/suppresses those metrics and misassigns the move to a single
    # date for benchmark alignment (Codex review on PR #403: "Avoid
    # collapsing missing-price gaps into one-day returns"; supersedes the
    # earlier "Drop missing prices before calculating returns" fix, whose
    # dropna()-before-pct_change() approach was itself the bridging bug).
    # Both the gap row and the row immediately after it (its return would
    # need the NaN as a baseline) are excluded entirely, not compressed.
    frame = pd.DataFrame({"Adj Close": [100.0, float("nan"), 110.0, 111.0]})

    returns = _price_returns(frame)

    assert len(returns) == 1  # only the 110 -> 111 return survives
    assert returns.iloc[0] == pytest.approx(111.0 / 110.0 - 1.0)


def test_price_returns_excludes_infinite_values():
    # dropna() alone leaves +/-inf in place (e.g. a zero-baseline price
    # producing a division-by-zero pct_change) -- a single infinite return
    # poisons statistics.mean()/pstdev() for the WHOLE series once it
    # reaches strategy_health_score(), corrupting every risk-adjusted
    # metric for the candidate instead of just excluding the one bad
    # observation (Antigravity review on PR #403: "Unfiltered Non-Finite
    # Returns (inf / -inf) in _price_returns").
    frame = pd.DataFrame({"Adj Close": [0.0, 100.0, 110.0, 111.0]})

    returns = _price_returns(frame)

    assert all(math.isfinite(value) for value in returns)
    assert len(returns) == 2  # the inf-producing 0 -> 100 transition is excluded


class _FakeAnalyzer:
    def score_texts(self, texts):
        return [
            {"text": text, "positive": 0.7, "negative": 0.1, "neutral": 0.2, "label": "positive", "confidence": 0.7}
            for text in texts
        ]


def test_score_candidate_still_attempts_sentiment_after_price_history_failure(monkeypatch):
    # Sentiment doesn't need price data -- a price-history failure must not
    # skip it too (Codex review on PR #403: "Continue to sentiment after
    # price-history failures").
    def _raise_unavailable(ticker, **kwargs):
        raise PriceHistoryUnavailable(f"no history for {ticker}")

    monkeypatch.setattr(advise_tickers, "fetch_price_history", _raise_unavailable)
    monkeypatch.setattr(
        advise_tickers, "sentiment_score", lambda ticker, **kwargs: {"score": 0.5, "n_news_headlines": 1}
    )

    raw = score_candidate(
        "NEWCO",
        benchmark_returns=None,
        lookback_years=3,
        skip_sentiment=False,
        news_api_key=None,
        analyzer=_FakeAnalyzer(),
        history_dir=None,
    )

    assert raw["health"] is None
    assert raw["backtest"] is None
    assert raw["sentiment"] == {"score": 0.5, "n_news_headlines": 1}
    assert any("no history for NEWCO" in error for error in raw["errors"])


def test_score_candidate_surfaces_stale_archive_fallback_reason(monkeypatch):
    # fetch_price_history() attaches this via frame.attrs when it fell back
    # to a stale/incomplete local archive after a live refresh also failed.
    # A stderr-only warning is invisible to anyone who only reads
    # scores.json/scores.md/the proposed diff later -- score_candidate()
    # must lift it into the candidate's own persisted `errors` list (Codex
    # review on PR #403: "Persist stale-archive fallback warnings in result
    # artifacts").
    index = pd.bdate_range(end=pd.Timestamp.now().normalize(), periods=5)
    frame = pd.DataFrame(
        {"Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0, "Adj Close": 1.0, "Volume": 1}, index=index
    )
    frame.attrs["stale_archive_fallback_reason"] = "archive.csv is 60 days stale; live refresh failed (timeout)"
    monkeypatch.setattr(advise_tickers, "fetch_price_history", lambda ticker, **kwargs: frame)

    raw = score_candidate(
        "STALECO",
        benchmark_returns=None,
        lookback_years=3,
        skip_sentiment=True,
        news_api_key=None,
        analyzer=None,
        history_dir=None,
    )

    assert any("price history fallback" in error and "stale" in error for error in raw["errors"])


def test_score_candidate_skips_backtest_on_internal_price_gap(monkeypatch):
    # dropna() on the raw price series compacts observations on opposite
    # sides of an INTERNAL gap into ADJACENT bars -- _portfolio_metrics()
    # pins vectorbt to freq="1D", so a multi-session gap in the middle of
    # the series would silently become a single daily bar, distorting both
    # backtest strategies' Sharpe ratios (Codex review on PR #403:
    # "Preserve missing sessions in backtest inputs").
    index = pd.bdate_range(end=pd.Timestamp.now().normalize(), periods=10)
    closes = [100.0] * 10
    closes[5] = float("nan")  # an internal gap, not a leading/trailing one
    frame = pd.DataFrame(
        {"Open": closes, "High": closes, "Low": closes, "Close": closes, "Adj Close": closes, "Volume": 1},
        index=index,
    )
    monkeypatch.setattr(advise_tickers, "fetch_price_history", lambda ticker, **kwargs: frame)

    raw = score_candidate(
        "GAPPY",
        benchmark_returns=None,
        lookback_years=3,
        skip_sentiment=True,
        news_api_key=None,
        analyzer=None,
        history_dir=None,
    )

    assert raw["backtest"] is None
    assert any("backtest signal skipped" in error and "gap" in error for error in raw["errors"])


def test_score_candidate_skips_health_on_internal_price_gap(monkeypatch):
    # _price_returns() correctly excludes both the gap row and the
    # following return rather than bridging them, but strategy_health_
    # score() still computed over whatever observations remained --
    # missing the ENTIRE price movement across the gap and then
    # over-annualizing CAGR/Calmar via _cagr()'s TRADING_DAYS/n exponent
    # using the REDUCED post-exclusion observation count, the same
    # distortion an uncorrected calendar-compressed backtest would have
    # had (Antigravity review on PR #403: "Skip health scoring when price
    # sessions are missing").
    index = pd.bdate_range(end=pd.Timestamp.now().normalize(), periods=10)
    closes = [100.0] * 10
    closes[5] = float("nan")  # an internal gap, not a leading/trailing one
    frame = pd.DataFrame(
        {"Open": closes, "High": closes, "Low": closes, "Close": closes, "Adj Close": closes, "Volume": 1},
        index=index,
    )
    monkeypatch.setattr(advise_tickers, "fetch_price_history", lambda ticker, **kwargs: frame)

    raw = score_candidate(
        "GAPPYHEALTH",
        benchmark_returns=None,
        lookback_years=3,
        skip_sentiment=True,
        news_api_key=None,
        analyzer=None,
        history_dir=None,
    )

    assert raw["health"] is None
    assert any("health signal skipped" in error and "gap" in error for error in raw["errors"])


def test_score_candidate_still_backtests_with_only_leading_or_trailing_gap(monkeypatch):
    # A gap before the first or after the last valid price doesn't compact
    # anything once dropna() runs -- only an INTERNAL gap disqualifies the
    # series, so this must NOT be skipped.
    index = pd.bdate_range(end=pd.Timestamp.now().normalize(), periods=10)
    closes = [100.0 + i for i in range(10)]
    closes[0] = float("nan")  # leading gap only
    frame = pd.DataFrame(
        {"Open": closes, "High": closes, "Low": closes, "Close": closes, "Adj Close": closes, "Volume": 1},
        index=index,
    )
    monkeypatch.setattr(advise_tickers, "fetch_price_history", lambda ticker, **kwargs: frame)
    monkeypatch.setattr(advise_tickers, "backtest_score", lambda close: {"called_with_len": len(close)})

    raw = score_candidate(
        "LEADGAP",
        benchmark_returns=None,
        lookback_years=3,
        skip_sentiment=True,
        news_api_key=None,
        analyzer=None,
        history_dir=None,
    )

    assert raw["backtest"] == {"called_with_len": 9}
    assert not any("backtest signal skipped" in error for error in raw["errors"])
    # A leading-only gap must not trip the health-side skip either
    # (Antigravity review on PR #403: "Skip health scoring when price
    # sessions are missing") -- health is still ATTEMPTED (returns an
    # all-NaN dict rather than None, since 9 observations is below
    # MIN_OBSERVATIONS, but that's a different, unrelated reason).
    assert raw["health"] is not None
    assert not any("health signal skipped" in error for error in raw["errors"])


def test_score_candidate_detects_missing_session_via_benchmark_calendar(monkeypatch):
    # A source that omits a trading-session ROW entirely (rather than
    # retaining it with a null price) is invisible to the NaN-based gap
    # detection above on its own -- there's no missing LABEL to notice.
    # The benchmark's own index (only ever containing days the market
    # actually traded, no holiday-calendar guesswork needed) is used to
    # detect this and reindex the candidate's frame so the EXISTING
    # gap-exclusion logic can catch it (Antigravity review on PR #403:
    # "Detect missing index sessions as price-history gaps").
    full_calendar = pd.bdate_range(end=pd.Timestamp.now().normalize(), periods=5)
    candidate_index = full_calendar.delete(2)  # a middle session's ROW is entirely absent
    closes = [100.0] * 4
    frame = pd.DataFrame(
        {"Open": closes, "High": closes, "Low": closes, "Close": closes, "Adj Close": closes, "Volume": 1},
        index=candidate_index,
    )
    monkeypatch.setattr(advise_tickers, "fetch_price_history", lambda ticker, **kwargs: frame)

    raw = score_candidate(
        "MISSSESSION",
        benchmark_returns=None,
        lookback_years=3,
        skip_sentiment=True,
        news_api_key=None,
        analyzer=None,
        history_dir=None,
        benchmark_calendar=full_calendar,
    )

    assert raw["backtest"] is None
    assert any("backtest signal skipped" in error and "gap" in error for error in raw["errors"])


def test_score_candidate_without_benchmark_calendar_skips_missing_session_check(monkeypatch):
    # Without a benchmark_calendar (e.g. the benchmark itself was
    # unavailable), missing-session detection degrades gracefully rather
    # than raising -- same best-effort stance as every other signal here.
    # It must also be DISCLOSED in this candidate's own errors, not just
    # silently skipped -- main()'s stderr warning on benchmark failure used
    # to claim only beta/alpha were affected, when missing-session
    # detection for health/backtest was ALSO silently disabled for every
    # candidate with nothing recording that the gap check never ran (Codex
    # review on PR #403: "Disable gap-sensitive scoring without a session
    # calendar").
    full_calendar = pd.bdate_range(end=pd.Timestamp.now().normalize(), periods=5)
    candidate_index = full_calendar.delete(2)
    closes = [100.0 + i for i in range(4)]
    frame = pd.DataFrame(
        {"Open": closes, "High": closes, "Low": closes, "Close": closes, "Adj Close": closes, "Volume": 1},
        index=candidate_index,
    )
    monkeypatch.setattr(advise_tickers, "fetch_price_history", lambda ticker, **kwargs: frame)
    monkeypatch.setattr(advise_tickers, "backtest_score", lambda close: {"called_with_len": len(close)})

    raw = score_candidate(
        "NOCALENDAR",
        benchmark_returns=None,
        lookback_years=3,
        skip_sentiment=True,
        news_api_key=None,
        analyzer=None,
        history_dir=None,
        benchmark_calendar=None,
    )

    assert raw["backtest"] == {"called_with_len": 4}
    assert any("no benchmark calendar available" in error and "health/backtest" in error for error in raw["errors"])


def test_score_candidate_skips_sentiment_when_skip_sentiment_true(monkeypatch):
    def _raise_unavailable(ticker, **kwargs):
        raise PriceHistoryUnavailable(f"no history for {ticker}")

    monkeypatch.setattr(advise_tickers, "fetch_price_history", _raise_unavailable)

    raw = score_candidate(
        "NEWCO",
        benchmark_returns=None,
        lookback_years=3,
        skip_sentiment=True,
        news_api_key=None,
        analyzer=None,
        history_dir=None,
    )

    assert raw["health"] is None
    assert raw["backtest"] is None
    assert raw["sentiment"] is None


def test_score_candidate_records_sentiment_init_failure_in_errors(monkeypatch):
    # analyzer=None with a reason (FinbertSentimentAnalyzer() failed to
    # construct in main()) must be distinguishable in scores.json/scores.md
    # from analyzer=None because the user passed --skip-sentiment, or from
    # sentiment running and genuinely finding nothing -- both of the latter
    # leave raw["sentiment"] as None with no error either (Codex review on
    # PR #403: "Persist sentiment initialization failures in the report").
    fake_frame = pd.DataFrame({"Close": [1.0, 2.0]})
    monkeypatch.setattr(advise_tickers, "fetch_price_history", lambda ticker, **kwargs: fake_frame)

    raw = score_candidate(
        "NEWCO",
        benchmark_returns=None,
        lookback_years=3,
        skip_sentiment=False,
        news_api_key=None,
        analyzer=None,
        history_dir=None,
        sentiment_unavailable_reason="No module named 'torch'",
    )

    assert raw["sentiment"] is None
    assert any("sentiment signal skipped" in error and "torch" in error for error in raw["errors"])


def test_score_candidate_lifts_sentiment_fetch_errors_into_top_level_errors(monkeypatch):
    # sentiment_score() records a SEC-fetch failure inside its OWN
    # "errors" key, not raw["errors"] -- combine_scores() only reads
    # raw["errors"] at the top level when building a candidate's rationale,
    # so without lifting it here a real SEC outage would look identical in
    # scores.md to "SEC simply had nothing to report" (Codex review on PR
    # #403: "Preserve SEC request failures in the report").
    fake_frame = pd.DataFrame({"Close": [1.0, 2.0]})
    monkeypatch.setattr(advise_tickers, "fetch_price_history", lambda ticker, **kwargs: fake_frame)
    monkeypatch.setattr(
        advise_tickers,
        "sentiment_score",
        lambda ticker, **kwargs: {
            "score": None,
            "n_news_headlines": 0,
            "n_sec_filings": 0,
            "news_api_key_used": False,
            "errors": ["SEC EDGAR fetch failed for NEWCO: boom"],
        },
    )

    raw = score_candidate(
        "NEWCO",
        benchmark_returns=None,
        lookback_years=3,
        skip_sentiment=False,
        news_api_key=None,
        analyzer=_FakeAnalyzer(),
        history_dir=None,
    )

    assert any("SEC EDGAR fetch failed" in error for error in raw["errors"])


def test_score_candidate_no_error_when_sentiment_explicitly_skipped(monkeypatch):
    # --skip-sentiment (skip_sentiment=True, no reason given) must NOT
    # produce a spurious "sentiment skipped" error -- that's expected,
    # user-requested behavior, not a degradation to flag.
    fake_frame = pd.DataFrame({"Close": [1.0, 2.0]})
    monkeypatch.setattr(advise_tickers, "fetch_price_history", lambda ticker, **kwargs: fake_frame)

    raw = score_candidate(
        "NEWCO",
        benchmark_returns=None,
        lookback_years=3,
        skip_sentiment=True,
        news_api_key=None,
        analyzer=None,
        history_dir=None,
        sentiment_unavailable_reason=None,
    )

    assert not any("sentiment" in error for error in raw["errors"])


def test_main_rejects_all_zero_effective_weights(capsys):
    # Each individual weight already passes _parse_weights (0 is a valid,
    # useful way to disable one signal) -- but zeroing ALL of them makes
    # combine_scores() accumulate weight_total=0.0 and report "no usable
    # signals at all" for every candidate even though every signal actually
    # computed fine. This must fail fast, before any network/model work,
    # not produce a normal-looking but empty report (Codex review on PR
    # #403: "Reject an all-zero effective weight set").
    # ZZZZZZ (not a real symbol) rather than AAPL: AAPL is in the live
    # config/tickers.json active set, and the already-active/excluded
    # filter now runs before this check, which would otherwise reject the
    # candidate for the wrong reason.
    exit_code = advise_tickers.main(
        ["--candidates", "ZZZZZZ", "--weights", "health=0,backtest=0,sentiment=0"]
    )

    assert exit_code == 2
    stderr = capsys.readouterr().err
    assert "weights" in stderr.lower()


def test_main_rejects_weights_that_only_target_a_skipped_signal(capsys):
    # --skip-sentiment --weights health=0,backtest=0,sentiment=1 passes the
    # all-zero check (sentiment is nonzero) but sentiment itself is never
    # computed when skipped, so combine_scores() still accumulates
    # weight_total=0.0 for every candidate -- a null composite and WATCH
    # classification, silently, with a normal-looking scores.json/scores.md
    # (Codex review on PR #403: "Reject weights that target a skipped
    # signal").
    exit_code = advise_tickers.main(
        ["--candidates", "ZZZZZZ", "--skip-sentiment", "--weights", "health=0,backtest=0,sentiment=1"]
    )

    assert exit_code == 2
    stderr = capsys.readouterr().err.lower()
    assert "skip-sentiment" in stderr and "weights" in stderr


def test_main_stops_when_only_weighted_signal_fails_to_initialize(capsys, monkeypatch):
    # Sentiment was the only signal --weights left nonzero, and it just
    # failed to initialize (e.g. missing torch/transformers, a model
    # download failure) -- the same no-weighted-signal state the explicit
    # --skip-sentiment guard already rejects, just discovered at runtime
    # instead of from the CLI flags alone. Without this, combine_scores()
    # still accumulates weight_total=0.0 for every candidate and the
    # command exits successfully after writing a normal-looking all-WATCH
    # scores.json/scores.md (Codex review on PR #403: "Stop when the only
    # weighted signal fails to initialize").
    def _raise_price_unavailable(ticker, **kwargs):
        raise PriceHistoryUnavailable(f"no history for {ticker}")

    def _raise_analyzer_error(*args, **kwargs):
        raise RuntimeError("torch not installed")

    monkeypatch.setattr(advise_tickers, "fetch_price_history", _raise_price_unavailable)
    monkeypatch.setattr(advise_tickers, "FinbertSentimentAnalyzer", _raise_analyzer_error)

    exit_code = advise_tickers.main(["--candidates", "ZZZZZZ", "--weights", "health=0,backtest=0,sentiment=1"])

    assert exit_code == 2
    stderr = capsys.readouterr().err.lower()
    assert "sentiment analyzer unavailable" in stderr
    assert "weights" in stderr


def test_main_fails_when_every_nonzero_weight_signal_is_unavailable_at_runtime(tmp_path, capsys, monkeypatch):
    # The --skip-sentiment / analyzer-init-failure guards above only catch
    # ONE way every weighted signal can end up unavailable -- a sentiment
    # analyzer that fails to CONSTRUCT. A health- or backtest-only run
    # where every candidate's signal fails at RUNTIME instead (every
    # fetch_price_history() call raising, here) previously still exited 0
    # and published a normal-looking, all-WATCH report with zero actual
    # weighted evidence behind it (Antigravity review on PR #403: "Fail
    # when all nonzero-weight signals are unavailable").
    def _raise_price_unavailable(ticker, **kwargs):
        raise PriceHistoryUnavailable(f"no history for {ticker}")

    monkeypatch.setattr(advise_tickers, "fetch_price_history", _raise_price_unavailable)

    exit_code = advise_tickers.main(
        ["--candidates", "ZZZZZZ", "YYYYYY", "--skip-sentiment", "--output-dir", str(tmp_path / "out")]
    )

    assert exit_code == 2
    stderr = capsys.readouterr().err.lower()
    assert "unavailable for every candidate" in stderr


def test_main_fails_when_weighted_signal_container_present_but_all_metrics_nan(tmp_path, monkeypatch):
    # A health/backtest dict can be present but hold nothing but NaN (a
    # flat/constant-return history hits strategy_health_score()'s own
    # zero-variance guard) -- checking only "the container isn't None"
    # wrongly counted this as usable evidence even though combine_scores()
    # would then exclude every one of its components, reproducing the
    # exact zero-evidence report the aggregate guard above exists to
    # catch (Antigravity review on PR #403: "Check weighted metric
    # usability, not container presence").
    flat_closes = [100.0] * 40
    flat_frame = pd.DataFrame({"Close": flat_closes, "Adj Close": flat_closes})
    monkeypatch.setattr(advise_tickers, "fetch_price_history", lambda ticker, **kwargs: flat_frame)

    exit_code = advise_tickers.main(
        [
            "--candidates",
            "ZZZZZZ",
            "--weights",
            "health=1,backtest=0,sentiment=0",
            "--skip-sentiment",
            "--output-dir",
            str(tmp_path / "out"),
        ]
    )

    assert exit_code == 2


def test_main_fails_when_weighted_signal_not_normalizable_across_batch(tmp_path, monkeypatch):
    # _signal_has_usable_metric() alone only asks "does ANY candidate have
    # ANY usable raw value for this signal" -- but scoring._zscore()
    # excludes a component for EVERY candidate once fewer than 2 of them
    # have a usable value for it. A health-only two-candidate run where one
    # candidate's price history fails entirely leaves exactly one usable
    # value per health component batch-wide, so health could never actually
    # be normalized for either candidate -- previously this still exited 0
    # with a null-composite, all-WATCH report (Codex review on PR #403:
    # "Require weighted metrics to be normalizable").
    def _fetch(ticker, **kwargs):
        if ticker == "GOODCO":
            return _usable_fake_frame()
        raise PriceHistoryUnavailable(f"no history for {ticker}")

    monkeypatch.setattr(advise_tickers, "fetch_price_history", _fetch)

    exit_code = advise_tickers.main(
        [
            "--candidates",
            "GOODCO",
            "BADCO",
            "--weights",
            "health=1,backtest=0,sentiment=0",
            "--skip-sentiment",
            "--output-dir",
            str(tmp_path / "out"),
        ]
    )

    assert exit_code == 2


def test_main_records_finbert_revision_in_run_metadata(tmp_path, monkeypatch):
    # FinbertSentimentAnalyzer resolves its revision once and reuses it for
    # both its tokenizer and model downloads; recording it here is the
    # only artifact that identifies which revision actually produced this
    # run's scores (Codex review on PR #403: "Pin and record the FinBERT
    # revision").
    captured = {}

    def fake_render_markdown(scores, **kwargs):
        captured.update(kwargs.get("run_metadata") or {})
        return "# fake report\n"

    class _FakeAnalyzer:
        revision = "abc123"

    fake_frame = _usable_fake_frame()
    monkeypatch.setattr(advise_tickers, "fetch_price_history", lambda ticker, **kwargs: fake_frame)
    monkeypatch.setattr(advise_tickers, "render_markdown", fake_render_markdown)
    monkeypatch.setattr(advise_tickers, "FinbertSentimentAnalyzer", lambda: _FakeAnalyzer())
    monkeypatch.setattr(
        advise_tickers,
        "sentiment_score",
        lambda ticker, **kwargs: {"score": 0.1, "n_news_headlines": 0, "n_sec_filings": 0},
    )

    exit_code = advise_tickers.main(["--candidates", "ZZZZZZ", "--output-dir", str(tmp_path / "out")])

    assert exit_code == 0
    assert captured.get("finbert_revision") == "abc123"


def test_main_persists_benchmark_fallback_reason_in_run_metadata(tmp_path, monkeypatch):
    # fetch_price_history() attaches this via frame.attrs when it fell back
    # to a stale/incomplete local archive after a live refresh also failed.
    # score_candidate() already lifts the same attr into a CANDIDATE's own
    # persisted errors, but the benchmark's copy was discarded entirely --
    # beta/alpha computed against deficient benchmark history looked
    # identical to a normal run in scores.json once stderr was gone (Codex
    # review on PR #403: "Persist benchmark fallback warnings").
    captured = {}

    def fake_render_markdown(scores, **kwargs):
        captured.update(kwargs.get("run_metadata") or {})
        return "# fake report\n"

    def fake_fetch_price_history(ticker, **kwargs):
        frame = _usable_fake_frame()
        if ticker == "SPY":
            frame.attrs["stale_archive_fallback_reason"] = "SPY.csv is 60 days stale; live refresh failed (timeout)"
        return frame

    monkeypatch.setattr(advise_tickers, "fetch_price_history", fake_fetch_price_history)
    monkeypatch.setattr(advise_tickers, "render_markdown", fake_render_markdown)

    exit_code = advise_tickers.main(
        ["--candidates", "ZZZZZZ", "--skip-sentiment", "--output-dir", str(tmp_path / "out")]
    )

    assert exit_code == 0
    assert captured.get("benchmark_fallback_reason") is not None
    assert "stale" in captured["benchmark_fallback_reason"]


def test_main_normalizes_benchmark_symbol_before_price_lookup(tmp_path, monkeypatch):
    # pricing.py's archive lookup builds its path as a literal
    # f"{ticker}.csv" with no case-folding of its own -- on a case-
    # sensitive filesystem an unnormalized "--benchmark spy" looks for
    # spy.csv instead of the archived SPY.csv, unlike candidate symbols
    # which ARE stripped and uppercased (Antigravity review on PR #403:
    # "Normalize benchmark symbols before archive lookup").
    fetched_tickers = []

    def fake_fetch_price_history(ticker, **kwargs):
        fetched_tickers.append(ticker)
        return _usable_fake_frame()

    monkeypatch.setattr(advise_tickers, "fetch_price_history", fake_fetch_price_history)

    exit_code = advise_tickers.main(
        ["--candidates", "ZZZZZZ", "--benchmark", " spy ", "--skip-sentiment", "--output-dir", str(tmp_path / "out")]
    )

    assert exit_code == 0
    assert "SPY" in fetched_tickers  # normalized before the lookup, not the literal " spy "


def test_main_default_output_root_falls_back_to_cron_tracking_root(tmp_path, monkeypatch):
    # A deployment that already redirected the CORE artifact tree away
    # from a read-only checkout via $DCA_CRON_TRACKING_ROOT shouldn't also
    # have to separately discover and configure TICKER_ADVISOR_OUTPUT_ROOT
    # (Codex review on PR #403: "Route advisory artifacts through the
    # shared root"). Nested under "ticker_advisor_analysis" so this
    # subproject's versioned reports stay structurally distinct from the
    # core pipeline's gitignored per-stream trees.
    monkeypatch.delenv("TICKER_ADVISOR_OUTPUT_ROOT", raising=False)
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron-root"))

    fake_frame = _usable_fake_frame()
    monkeypatch.setattr(advise_tickers, "fetch_price_history", lambda ticker, **kwargs: fake_frame)

    exit_code = advise_tickers.main(["--candidates", "ZZZZZZ", "--skip-sentiment"])

    assert exit_code == 0
    assert (tmp_path / "cron-root" / "ticker_advisor_analysis").is_dir()


def test_main_treats_blank_output_root_override_as_unset(tmp_path, monkeypatch):
    # A deployment that exports TICKER_ADVISOR_OUTPUT_ROOT but leaves it
    # blank (e.g. a templated CI config substituting an unset value)
    # still satisfied "in os.environ" -- Path("") resolves to the
    # invocation's current directory, silently writing a default run's
    # dated artifact directory under whatever directory happened to
    # launch the command instead of falling through to the next root in
    # the documented selection order (Antigravity review on PR #403:
    # "Treat blank output-root overrides as unset"). DCA_CRON_TRACKING_ROOT
    # is set here (rather than left unset) so the blank override, if
    # wrongly honored, would diverge from it -- and so this test never
    # touches the real repo's own analysis/ directory regardless.
    monkeypatch.setenv("TICKER_ADVISOR_OUTPUT_ROOT", "")
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron-root"))
    monkeypatch.chdir(tmp_path)

    fake_frame = _usable_fake_frame()
    monkeypatch.setattr(advise_tickers, "fetch_price_history", lambda ticker, **kwargs: fake_frame)

    exit_code = advise_tickers.main(["--candidates", "ZZZZZZ", "--skip-sentiment"])

    assert exit_code == 0
    assert not (tmp_path / "analysis").exists()  # not written under blank-string-as-cwd
    assert (tmp_path / "cron-root" / "ticker_advisor_analysis").is_dir()


def test_main_writes_completion_marker_after_all_three_artifacts(tmp_path, monkeypatch):
    # A crash between the three per-file atomic writes can still leave the
    # directory holding a mix of an old and a new generation when
    # --output-dir is reused across runs. The marker, written LAST, lets a
    # careful consumer treat its absence as "do not trust this artifact
    # set" (Codex review on PR #403: "Commit the artifact set as one
    # generation").
    fake_frame = _usable_fake_frame()
    monkeypatch.setattr(advise_tickers, "fetch_price_history", lambda ticker, **kwargs: fake_frame)

    out_dir = tmp_path / "out"
    exit_code = advise_tickers.main(["--candidates", "ZZZZZZ", "--skip-sentiment", "--output-dir", str(out_dir)])

    assert exit_code == 0
    assert (out_dir / ".generation_complete").is_file()
    assert (out_dir / "scores.json").is_file()
    assert (out_dir / "scores.md").is_file()
    assert (out_dir / "proposed_tickers_diff.json").is_file()


def test_main_invalidates_stale_completion_marker_before_reusing_output_dir(tmp_path, monkeypatch):
    # An --output-dir reused from a COMPLETED prior run already has a
    # marker on disk from that earlier generation. A crash between this
    # run's three writes (each individually atomic, but not committed as
    # a SET) would otherwise leave the STALE marker from the old
    # generation still present, making a consumer trust a directory
    # that's actually a mix of the old scores.md/proposed diff and the
    # new scores.json (Antigravity review on PR #403: "Invalidate the old
    # marker before replacing artifacts").
    fake_frame = _usable_fake_frame()
    monkeypatch.setattr(advise_tickers, "fetch_price_history", lambda ticker, **kwargs: fake_frame)

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    marker = out_dir / ".generation_complete"
    marker.write_text("stale-prior-run\n", encoding="utf-8")

    original_write_atomic = advise_tickers._write_atomic

    def failing_write_atomic(path, content, **kwargs):
        if path.name == "scores.md":
            raise RuntimeError("simulated crash mid-publish")
        return original_write_atomic(path, content, **kwargs)

    monkeypatch.setattr(advise_tickers, "_write_atomic", failing_write_atomic)

    with pytest.raises(RuntimeError):
        advise_tickers.main(["--candidates", "ZZZZZZ", "--skip-sentiment", "--output-dir", str(out_dir)])

    assert not marker.exists()  # invalidated before the crash, not left behind as falsely trustworthy
    assert (out_dir / "scores.json").is_file()  # the write before the simulated crash still landed
    assert not (out_dir / "scores.md").is_file()


def test_main_acquires_lock_around_artifact_publish(tmp_path, monkeypatch):
    # Two invocations sharing the same explicit --output-dir could
    # otherwise interleave their writes -- one process writes
    # scores.json, the other writes the remaining two files and
    # recreates the marker, leaving .generation_complete beside a mix of
    # two different runs' artifacts, which no single-process fix can
    # prevent (Antigravity review on PR #403: "Serialize writers sharing
    # an explicit output directory"). cross_process_lock() itself is
    # already covered by scripts/tests/test_file_lock.py; this only
    # checks main() wires it around the publish sequence with the right
    # lock path.
    fake_frame = _usable_fake_frame()
    monkeypatch.setattr(advise_tickers, "fetch_price_history", lambda ticker, **kwargs: fake_frame)

    lock_calls = []
    state = {"locked": False}
    writes_while_locked = []

    @contextlib.contextmanager
    def fake_lock(path, **kwargs):
        lock_calls.append(path)
        state["locked"] = True
        try:
            yield
        finally:
            state["locked"] = False

    monkeypatch.setattr(advise_tickers, "cross_process_lock", fake_lock)

    original_write_atomic = advise_tickers._write_atomic

    def tracking_write_atomic(path, content, **kwargs):
        writes_while_locked.append(state["locked"])
        return original_write_atomic(path, content, **kwargs)

    monkeypatch.setattr(advise_tickers, "_write_atomic", tracking_write_atomic)

    out_dir = tmp_path / "out"
    exit_code = advise_tickers.main(["--candidates", "ZZZZZZ", "--skip-sentiment", "--output-dir", str(out_dir)])

    assert exit_code == 0
    assert lock_calls == [out_dir / ".lock"]
    assert writes_while_locked == [True, True, True, True]  # scores, markdown, diff, marker


def test_main_merges_pre_filtered_exclusions_into_proposed_diff(tmp_path, monkeypatch):
    # render_proposed_diff()'s own skipped_* filters only see `scores`,
    # which already excludes invalid-shape and already-active-or-excluded
    # candidates (filtered upstream, before scoring) -- its skipped_*
    # fields were therefore structurally always empty for this call path
    # even when candidates genuinely were filtered out (Antigravity review
    # on PR #403: "Logic Disconnect: render_proposed_diff Output Fields
    # Are Always Empty").
    fake_frame = _usable_fake_frame()
    monkeypatch.setattr(advise_tickers, "fetch_price_history", lambda ticker, **kwargs: fake_frame)

    out_dir = tmp_path / "out"
    # AAPL is in the live config/tickers.json active set (already-active,
    # filtered before scoring); BRK-B fails TICKER_SYMBOL_PATTERN
    # (invalid-shape, also filtered before scoring).
    exit_code = advise_tickers.main(
        ["--candidates", "ZZZZZZ", "AAPL", "BRK-B", "--skip-sentiment", "--output-dir", str(out_dir)]
    )

    assert exit_code == 0
    diff = json.loads((out_dir / "proposed_tickers_diff.json").read_text(encoding="utf-8"))
    assert "AAPL" in diff["skipped_already_active"]
    assert "BRK-B" in diff["skipped_invalid_symbol_shape"]


def test_main_passes_run_date_to_proposed_diff(tmp_path, monkeypatch):
    # render_proposed_diff()'s own date.today() default is LOCAL, while
    # `now` (used for run_metadata.generated_at, the output directory, and
    # scores.md's "Generated:" line) is UTC -- on a non-UTC host near UTC
    # midnight those two could otherwise land on different calendar dates
    # unless the same captured `now` is passed through explicitly (Codex
    # review on PR #403: "Pass the run date into the proposed diff").
    captured = {}

    def fake_render_proposed_diff(scores, **kwargs):
        captured.update(kwargs)
        return {
            "additions": [],
            "skipped_already_active": [],
            "skipped_explicitly_excluded": [],
            "skipped_invalid_symbol_shape": [],
            "generated_date": kwargs.get("as_of").isoformat() if kwargs.get("as_of") else None,
        }

    fake_frame = _usable_fake_frame()
    monkeypatch.setattr(advise_tickers, "fetch_price_history", lambda ticker, **kwargs: fake_frame)
    monkeypatch.setattr(advise_tickers, "render_proposed_diff", fake_render_proposed_diff)

    exit_code = advise_tickers.main(
        ["--candidates", "ZZZZZZ", "--skip-sentiment", "--output-dir", str(tmp_path / "out")]
    )

    assert exit_code == 0
    assert captured.get("as_of") is not None
    assert captured["as_of"] == datetime.now(timezone.utc).date()


def test_main_reports_clean_error_when_output_dir_cannot_be_created(tmp_path, capsys):
    # --output-dir pointing at an existing FILE (or a directory the process
    # lacks permission to create) previously escaped this CLI's normal
    # "error: ..." exit path and terminated with a raw traceback and exit
    # code 1 instead (Codex review on PR #403: "Convert output-directory
    # creation failures into CLI errors").
    blocking_file = tmp_path / "not-a-directory"
    blocking_file.write_text("occupied", encoding="utf-8")

    exit_code = advise_tickers.main(["--candidates", "ZZZZZZ", "--output-dir", str(blocking_file)])

    assert exit_code == 2
    stderr = capsys.readouterr().err.lower()
    assert "output-dir" in stderr


def test_main_records_history_dir_override_in_run_metadata(tmp_path, monkeypatch):
    # --history-dir determines BOTH candidate and benchmark price inputs
    # and can materially change every health/backtest score, but was
    # absent from the persisted run parameters -- a saved run looked
    # identical to one using the default archive and couldn't be
    # reproduced or audited once the command line was gone (Codex review
    # on PR #403: "Record the price-history override in run metadata").
    captured = {}

    def fake_render_markdown(scores, **kwargs):
        captured.update(kwargs.get("run_metadata") or {})
        return "# fake report\n"

    fake_frame = _usable_fake_frame()
    monkeypatch.setattr(advise_tickers, "fetch_price_history", lambda ticker, **kwargs: fake_frame)
    monkeypatch.setattr(advise_tickers, "render_markdown", fake_render_markdown)

    history_dir = tmp_path / "custom-history"
    history_dir.mkdir()

    exit_code = advise_tickers.main(
        [
            "--candidates",
            "ZZZZZZ",
            "--skip-sentiment",
            "--output-dir",
            str(tmp_path / "out"),
            "--history-dir",
            str(history_dir),
        ]
    )

    assert exit_code == 0
    assert captured.get("history_dir_override") == str(history_dir)
    assert captured.get("history_dir_effective") == str(history_dir.resolve())


def test_main_records_effective_history_dir_from_environment_override(tmp_path, monkeypatch):
    # history_dir_override alone is None whenever --history-dir isn't
    # passed on the CLI, even when $DCA_TICKER_HISTORY_DIR redirects
    # fetch_price_history() to a DIFFERENT archive than the repo's plain
    # default -- indistinguishable in the saved metadata from a run that
    # genuinely used the ordinary default (Antigravity review on PR #403:
    # "Record the effective environment-selected history directory").
    captured = {}

    def fake_render_markdown(scores, **kwargs):
        captured.update(kwargs.get("run_metadata") or {})
        return "# fake report\n"

    fake_frame = _usable_fake_frame()
    monkeypatch.setattr(advise_tickers, "fetch_price_history", lambda ticker, **kwargs: fake_frame)
    monkeypatch.setattr(advise_tickers, "render_markdown", fake_render_markdown)

    env_history_dir = tmp_path / "env-history"
    env_history_dir.mkdir()
    monkeypatch.setenv("DCA_TICKER_HISTORY_DIR", str(env_history_dir))

    exit_code = advise_tickers.main(
        ["--candidates", "ZZZZZZ", "--skip-sentiment", "--output-dir", str(tmp_path / "out")]
    )

    assert exit_code == 0
    assert captured.get("history_dir_override") is None  # not passed on the CLI
    assert captured.get("history_dir_effective") == str(env_history_dir.resolve())


def test_main_preserves_original_requested_candidate_list(tmp_path, monkeypatch):
    # `candidates` gets overwritten by each filtering step (invalid-shape,
    # legacy-symbol, already-active/excluded) before run_metadata is built
    # -- persisting THAT variable as "candidates_requested" silently
    # dropped filtered-out symbols from a saved run's recorded input, and
    # the separate exclusion buckets alone can't reconstruct the original
    # request's ordering or duplicates (Codex review on PR #403: "Preserve
    # the original requested candidate list").
    captured = {}

    def fake_render_markdown(scores, **kwargs):
        captured.update(kwargs.get("run_metadata") or {})
        return "# fake report\n"

    fake_frame = _usable_fake_frame()
    monkeypatch.setattr(advise_tickers, "fetch_price_history", lambda ticker, **kwargs: fake_frame)
    monkeypatch.setattr(advise_tickers, "render_markdown", fake_render_markdown)

    exit_code = advise_tickers.main(
        ["--candidates", "ZZZZZZ", "BAD-SYMBOL", "--skip-sentiment", "--output-dir", str(tmp_path / "out")]
    )

    assert exit_code == 0
    assert captured.get("candidates_requested") == ["ZZZZZZ", "BAD-SYMBOL"]
    assert captured.get("candidates_scored") == ["ZZZZZZ"]


def test_main_resolves_relative_history_dir_override_to_absolute(tmp_path, monkeypatch):
    # A relative --history-dir persisted verbatim can't identify its price
    # source once the run is inspected from a different working directory
    # or the invoking shell context is gone (Codex review on PR #403:
    # "Persist an absolute history-directory override").
    captured = {}

    def fake_render_markdown(scores, **kwargs):
        captured.update(kwargs.get("run_metadata") or {})
        return "# fake report\n"

    fake_frame = _usable_fake_frame()
    monkeypatch.setattr(advise_tickers, "fetch_price_history", lambda ticker, **kwargs: fake_frame)
    monkeypatch.setattr(advise_tickers, "render_markdown", fake_render_markdown)

    (tmp_path / "relative-history").mkdir()
    monkeypatch.chdir(tmp_path)

    exit_code = advise_tickers.main(
        [
            "--candidates",
            "ZZZZZZ",
            "--skip-sentiment",
            "--output-dir",
            str(tmp_path / "out"),
            "--history-dir",
            "relative-history",
        ]
    )

    assert exit_code == 0
    recorded = captured.get("history_dir_override")
    assert recorded is not None
    assert recorded == str((tmp_path / "relative-history").resolve())
    assert recorded != "relative-history"
