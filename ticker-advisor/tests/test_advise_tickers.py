"""Tests for advise_tickers.py's CLI-support helpers -- no heavy deps needed."""

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import advise_tickers
import pandas as pd
import pytest
from advise_tickers import (
    PriceHistoryUnavailable,
    _allocate_default_output_dir,
    _detect_benchmark_calendar_gaps,
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

    The index is a DatetimeIndex of business days ending today (matching
    what fetch_price_history() actually returns) so that score_candidate()'s
    benchmark-calendar reindex step -- which compares benchmark_calendar
    against frame.index.min()/max() -- receives pd.Timestamps rather than
    bare integers (Codex fix PR #549: "TypeError: Invalid comparison between
    dtype=datetime64[ns] and int").
    """
    closes = [100.0 + i * 0.3 + (4.0 if i % 2 == 0 else -2.5) for i in range(periods)]
    index = pd.bdate_range(end=pd.Timestamp.now().normalize(), periods=periods)
    return pd.DataFrame({"Close": closes, "Adj Close": closes}, index=index)


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


def test_detect_benchmark_calendar_gaps_finds_missing_business_day():
    # A business day (not a weekend, not a US federal holiday) that's
    # simply absent from the benchmark's own fetched index means the
    # SOURCE itself omitted a real trading session -- previously invisible
    # because benchmark_calendar was the only calendar this module had and
    # was trusted unconditionally (Codex review on PR #429: "Validate the
    # benchmark calendar independently"). 2026-08-11 (Tuesday) is dropped
    # from an otherwise-contiguous business-day week.
    calendar = pd.DatetimeIndex(
        ["2026-08-10", "2026-08-12", "2026-08-13", "2026-08-14"]  # Mon, Wed, Thu, Fri -- Tue missing
    )

    gaps = _detect_benchmark_calendar_gaps(calendar)

    assert pd.Timestamp("2026-08-11") in gaps


def test_detect_benchmark_calendar_gaps_ignores_weekends_and_holidays():
    # A calendar that's genuinely complete (skips only weekends and the
    # federal holiday in range) must not be flagged -- only PR #403's
    # concern (a fixed calendar producing false positives on real
    # holidays) would make this check worse than not having it at all.
    calendar = pd.date_range("2026-07-01", "2026-07-10", freq="B")  # includes July 4 (Sat) already excluded

    gaps = _detect_benchmark_calendar_gaps(calendar)

    assert gaps == []


def test_detect_benchmark_calendar_gaps_handles_empty_calendar():
    assert _detect_benchmark_calendar_gaps(pd.DatetimeIndex([])) == []


def test_detect_benchmark_calendar_gaps_does_not_flag_good_friday():
    # Good Friday is an NYSE holiday but NOT a US federal one -- an earlier
    # version of this function approximated the exchange calendar with
    # federal holidays + weekends and would have flagged a benchmark
    # calendar that correctly excludes Good Friday as "missing" a session.
    # Unioning that false positive into every candidate's reindex calendar
    # would inject a synthetic gap on a real non-trading day and degrade
    # health/backtest scoring across the whole run (Codex review on PR
    # #429: "Use an exchange calendar before adding gap dates"). 2026's
    # Good Friday is April 3.
    calendar = pd.DatetimeIndex(["2026-04-01", "2026-04-02", "2026-04-06", "2026-04-07"])  # Good Friday (04-03) out

    gaps = _detect_benchmark_calendar_gaps(calendar)

    assert pd.Timestamp("2026-04-03") not in gaps


def test_detect_benchmark_calendar_gaps_flags_missing_session_on_a_columbus_day_week():
    # Columbus Day is a US federal holiday but the NYSE trades through it --
    # the real exchange calendar must still flag a genuinely missing
    # session in the same week rather than mistaking Columbus Day itself
    # for the gap.
    calendar = pd.DatetimeIndex(
        ["2026-10-12", "2026-10-14", "2026-10-15", "2026-10-16"]  # Mon(Columbus Day), Wed, Thu, Fri -- Tue missing
    )

    gaps = _detect_benchmark_calendar_gaps(calendar)

    assert pd.Timestamp("2026-10-13") in gaps
    assert pd.Timestamp("2026-10-12") not in gaps


def test_score_candidate_rejects_numeric_index_before_timestamp_coercion(monkeypatch):
    # Codex review, PR #538 discussion_r3837689396 ("Reject numeric indexes
    # before timestamp coercion"): pd.Timestamp(0) on an integer RangeIndex
    # value SUCCEEDS by interpreting it as nanoseconds since the Unix epoch,
    # instead of raising -- so a bare try/except around the coercion never
    # caught a non-datetime index. Missing-session detection then silently
    # ran against a bogus 1970 timestamp range, found no benchmark sessions
    # in it, and continued gap-sensitive scoring with the check disabled and
    # no warning recorded at all.
    frame = _usable_fake_frame().reset_index(drop=True)  # plain RangeIndex, not datetime
    monkeypatch.setattr(advise_tickers, "fetch_price_history", lambda ticker, **kwargs: frame)

    benchmark_calendar = pd.bdate_range("2024-01-01", periods=300)
    result = score_candidate(
        "ZZZZZZ",
        benchmark_returns=None,
        lookback_years=2,
        skip_sentiment=True,
        news_api_key=None,
        analyzer=None,
        history_dir=None,
        benchmark_calendar=benchmark_calendar,
    )

    assert any("not datetime-like" in err for err in result["errors"])


def test_score_candidate_accepts_object_dtype_datetime_index(monkeypatch):
    # Codex review, PR #538 ("Recognize object-backed datetime indexes"):
    # is_datetime64_any_dtype() alone rejects an Index(dtype=object) holding
    # real datetime values -- exactly what an alternate/monkeypatched data
    # source can hand back -- even though pd.Timestamp(...) converts every
    # one of those values correctly. Missing-session detection must still
    # run for this candidate rather than being silently skipped.
    frame = _usable_fake_frame()
    frame.index = pd.Index([ts.to_pydatetime() for ts in frame.index], dtype=object)
    monkeypatch.setattr(advise_tickers, "fetch_price_history", lambda ticker, **kwargs: frame)

    benchmark_calendar = pd.bdate_range(end=pd.Timestamp.now().normalize(), periods=300)
    result = score_candidate(
        "ZZZZZZ",
        benchmark_returns=None,
        lookback_years=2,
        skip_sentiment=True,
        news_api_key=None,
        analyzer=None,
        history_dir=None,
        benchmark_calendar=benchmark_calendar,
    )

    assert not any("not datetime-like" in err for err in result["errors"])


def test_score_candidate_detects_gap_with_object_dtype_date_index(monkeypatch):
    # Codex review, PR #538 ("Normalize accepted object indexes before
    # unioning sessions"): an object-dtype index of bare datetime.date values
    # -- exactly what _index_is_datetime_like() accepts -- was not equal to
    # the benchmark's pd.Timestamp labels under a plain Index.union(), so a
    # genuinely dropped session went undetected (or produced duplicate
    # date/Timestamp rows) instead of surfacing as an internal gap.
    frame = _usable_fake_frame()
    dropped_day = frame.index[30]
    frame = frame.drop(index=dropped_day)
    frame.index = pd.Index([ts.date() for ts in frame.index], dtype=object)
    monkeypatch.setattr(advise_tickers, "fetch_price_history", lambda ticker, **kwargs: frame)

    benchmark_calendar = pd.bdate_range(end=pd.Timestamp.now().normalize(), periods=300)
    result = score_candidate(
        "ZZZZZZ",
        benchmark_returns=None,
        lookback_years=2,
        skip_sentiment=True,
        news_api_key=None,
        analyzer=None,
        history_dir=None,
        benchmark_calendar=benchmark_calendar,
    )

    assert any("internal gaps" in err for err in result["errors"])


def test_score_candidate_accepts_timezone_aware_object_dtype_index(monkeypatch):
    # Codex review, PR #538 ("Normalize timezone awareness before comparing
    # calendars"): _index_is_datetime_like() accepts an object-dtype index of
    # timezone-AWARE datetime values, but pd.DatetimeIndex(...) preserves that
    # awareness, and comparing it against the always-timezone-NAIVE
    # benchmark_calendar raised TypeError -- outside the per-signal error
    # guards, aborting the whole candidate batch instead of degrading
    # gracefully.
    frame = _usable_fake_frame()
    frame.index = pd.Index(
        [ts.tz_localize("America/New_York").to_pydatetime() for ts in frame.index], dtype=object
    )
    monkeypatch.setattr(advise_tickers, "fetch_price_history", lambda ticker, **kwargs: frame)

    benchmark_calendar = pd.bdate_range(end=pd.Timestamp.now().normalize(), periods=300)
    assert benchmark_calendar.tz is None
    result = score_candidate(
        "ZZZZZZ",
        benchmark_returns=None,
        lookback_years=2,
        skip_sentiment=True,
        news_api_key=None,
        analyzer=None,
        history_dir=None,
        benchmark_calendar=benchmark_calendar,
    )

    assert not any("not datetime-like" in err for err in result["errors"])
    assert result["health"] is not None or result["errors"]


def test_score_candidate_normalizes_non_midnight_object_dtype_index_to_session_dates(monkeypatch):
    # Codex review, PR #538 ("Normalize accepted indexes to session dates"):
    # an object-dtype index with a non-midnight time-of-day (e.g. one
    # market-close timestamp per day) must be normalized to midnight before
    # unioning with benchmark_calendar's midnight session labels -- otherwise
    # every candidate date is a DISTINCT label from its matching benchmark
    # date, inserting a spurious NaN row for every session and falsely
    # reporting an internal gap for a candidate with NO missing sessions.
    frame = _usable_fake_frame()
    frame.index = pd.Index(
        [(ts + pd.Timedelta(hours=16)).to_pydatetime() for ts in frame.index], dtype=object
    )
    monkeypatch.setattr(advise_tickers, "fetch_price_history", lambda ticker, **kwargs: frame)

    benchmark_calendar = pd.bdate_range(end=pd.Timestamp.now().normalize(), periods=300)
    result = score_candidate(
        "ZZZZZZ",
        benchmark_returns=None,
        lookback_years=2,
        skip_sentiment=True,
        news_api_key=None,
        analyzer=None,
        history_dir=None,
        benchmark_calendar=benchmark_calendar,
    )

    assert not any("internal gaps" in err for err in result["errors"])


def test_score_candidate_normalizes_non_midnight_benchmark_calendar(monkeypatch):
    # Codex review, PR #538 ("Normalize the benchmark calendar before session
    # union"): when the CONFIGURED price-history source timestamps every
    # daily bar at market close, main() preserves those non-midnight values
    # in benchmark_calendar itself -- normalizing only the candidate index
    # (the sibling fix above) still leaves each candidate date a DISTINCT
    # label from its matching benchmark date, falsely reporting an internal
    # gap for a candidate with no missing sessions. The candidate's own
    # index here is an ordinary midnight DatetimeIndex; only the benchmark
    # calendar is shifted, to isolate this half of the normalization.
    frame = _usable_fake_frame()
    monkeypatch.setattr(advise_tickers, "fetch_price_history", lambda ticker, **kwargs: frame)

    benchmark_calendar = pd.bdate_range(
        end=pd.Timestamp.now().normalize(), periods=300
    ) + pd.Timedelta(hours=16)
    result = score_candidate(
        "ZZZZZZ",
        benchmark_returns=None,
        lookback_years=2,
        skip_sentiment=True,
        news_api_key=None,
        analyzer=None,
        history_dir=None,
        benchmark_calendar=benchmark_calendar,
    )

    assert not any("internal gaps" in err for err in result["errors"])


def test_index_is_datetime_like_rejects_numeric_object_dtype_index():
    # An object-dtype index is only accepted when EVERY value is itself
    # date-like -- a numeric value smuggled in via an object-dtype index
    # (e.g. mixed int/str labels) must still be rejected, not just a
    # genuine numeric-dtype RangeIndex.
    assert advise_tickers._index_is_datetime_like(pd.Index([0, 1, 2], dtype=object)) is False
    assert advise_tickers._index_is_datetime_like(pd.RangeIndex(3)) is False
    assert (
        advise_tickers._index_is_datetime_like(pd.bdate_range("2024-01-01", periods=3))
        is True
    )


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


def test_load_current_universe_folds_whole_share_into_active(tmp_path):
    # config/tickers.json's third category, `whole_share` (tickers not
    # tradable for fractional buys, accumulated toward a whole share
    # instead -- see docs/whole_share_accumulation.md), is disjoint from
    # `active` and `excluded` by the schema's own invariant. Without
    # folding it into the returned "active" set here, a whole-share
    # symbol like BYDDY would be neither active nor excluded from this
    # advisor's perspective and could be scored and proposed as a
    # duplicate ADD, skewing every other candidate's z-score
    # normalization (Codex review: "Exclude whole-share symbols from
    # ticker-advisor candidates").
    (tmp_path / "config").mkdir()
    payload = {"active": ["AAPL", "MSFT"], "excluded": ["BANNED"], "whole_share": ["BYDDY"]}
    (tmp_path / "config" / "tickers.json").write_text(json.dumps(payload), encoding="utf-8")

    active, excluded = _load_current_universe(tmp_path)

    assert active == frozenset({"AAPL", "MSFT", "BYDDY"})
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
        analyzer=cast(Any, _FakeAnalyzer()),
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


def _ohlc(index):
    return pd.DataFrame(
        {"Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0, "Adj Close": 1.0, "Volume": 1}, index=index
    )


def test_score_candidate_survives_a_numeric_price_history_index(monkeypatch):
    # Codex review ("Validate price-history indexes before calendar
    # comparisons"): comparing a DatetimeIndex benchmark calendar against a
    # NUMERIC candidate index raises TypeError. score_candidate() is called in
    # a bare loop with no per-candidate except, so one malformed source aborted
    # the ENTIRE advisory batch instead of merely losing that candidate's
    # gap-sensitive scores.
    frame = _ohlc(pd.RangeIndex(start=0, stop=5))
    monkeypatch.setattr(advise_tickers, "fetch_price_history", lambda ticker, **kwargs: frame)
    calendar = pd.bdate_range(end=pd.Timestamp("2026-08-14"), periods=10)

    raw = score_candidate(
        "NUMIDX",
        benchmark_returns=None,
        lookback_years=3,
        skip_sentiment=True,
        news_api_key=None,
        analyzer=None,
        history_dir=None,
        benchmark_calendar=calendar,
    )

    assert any("not datetime-like" in error for error in raw["errors"]), (
        "the candidate must degrade with a recorded reason, not raise"
    )


def test_score_candidate_normalizes_a_tz_aware_price_history_index(monkeypatch):
    # An object/tz-aware index compares and unions element-wise against
    # DatetimeIndex labels, so the same session carried as 2026-08-03 00:00-04:00
    # and 2026-08-03 00:00 becomes TWO labels -- the union invents sessions the
    # NaN-gap logic then reads as missing data. The benchmark calendar is
    # tz-naive (fetched from the same price-history source as every other
    # candidate), so a tz-aware candidate's own tz is DROPPED, not converted
    # through it -- see the positive-offset variant below for why conversion
    # would be wrong.
    tz_index = pd.date_range("2026-08-03", periods=5, freq="B", tz="America/New_York")
    frame = _ohlc(tz_index)
    monkeypatch.setattr(advise_tickers, "fetch_price_history", lambda ticker, **kwargs: frame)
    calendar = pd.bdate_range(start="2026-08-03", periods=5)

    raw = score_candidate(
        "TZIDX",
        benchmark_returns=None,
        lookback_years=3,
        skip_sentiment=True,
        news_api_key=None,
        analyzer=None,
        history_dir=None,
        benchmark_calendar=calendar,
    )

    assert not any("not datetime-like" in error for error in raw["errors"])
    # Five real sessions, matching the calendar exactly -- no invented labels
    # and therefore no fabricated missing-session gaps.
    assert not any("missing" in error.lower() and "session" in error.lower() for error in raw["errors"])


def test_score_candidate_preserves_positive_offset_session_dates(monkeypatch):
    # Codex review discussion_r3867858365 ("Preserve local session dates
    # before dropping timezones"). These labels are trading DATES, not
    # instants: a daily history indexed in a POSITIVE-offset exchange
    # timezone carries its sessions as local midnight, and converting them
    # through UTC before dropping the tz moves every one onto the PREVIOUS
    # calendar day -- the benchmark union then invents labels that never
    # traded and reads the candidate's real sessions as missing, distorting
    # the gap-sensitive health and backtest scores this normalization exists
    # to protect. The negative-offset case above happens to survive either
    # way (its UTC hour never crosses midnight back a day); only a
    # positive-offset zone actually exercises the bug.
    tz_index = pd.date_range("2026-08-03", periods=5, freq="B", tz="Australia/Sydney")
    frame = _ohlc(tz_index)
    monkeypatch.setattr(advise_tickers, "fetch_price_history", lambda ticker, **kwargs: frame)
    calendar = pd.bdate_range(start="2026-08-03", periods=5)

    raw = score_candidate(
        "TZPOSOFFSET",
        benchmark_returns=None,
        lookback_years=3,
        skip_sentiment=True,
        news_api_key=None,
        analyzer=None,
        history_dir=None,
        benchmark_calendar=calendar,
    )

    assert not any("not datetime-like" in error for error in raw["errors"])
    # Five real sessions, matching the calendar exactly -- no invented labels
    # and therefore no fabricated missing-session gaps.
    assert not any("missing" in error.lower() and "session" in error.lower() for error in raw["errors"])


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


def test_score_candidate_deduplicates_candidate_dates_before_reindex(monkeypatch):
    # Codex review, PR #654 ("Deduplicate candidate dates before calendar
    # reindexing"): an alternate source can hand back multiple rows for the
    # same trading session (e.g. two intraday timestamps that both
    # normalize() to the same midnight session-date label). Combined with a
    # genuinely missing middle session (forcing the reindex-onto-benchmark-
    # sessions path this test also exercises), frame.set_axis(normalized_
    # index).reindex(...) raised "ValueError: cannot reindex on an axis
    # with duplicate labels" -- outside every per-signal error-recording
    # guard, aborting the whole candidate instead of degrading gracefully.
    full_calendar = pd.bdate_range(end=pd.Timestamp.now().normalize(), periods=5)
    candidate_index = full_calendar.delete(2)  # a middle session's ROW is entirely absent
    candidate_index = candidate_index.insert(0, candidate_index[0])  # duplicate the first date
    closes = [100.0] * 5
    frame = pd.DataFrame(
        {"Open": closes, "High": closes, "Low": closes, "Close": closes, "Adj Close": closes, "Volume": 1},
        index=candidate_index,
    )
    monkeypatch.setattr(advise_tickers, "fetch_price_history", lambda ticker, **kwargs: frame)

    # Must not raise -- this is the regression. The reindex() call this
    # exercises sits OUTSIDE score_candidate()'s own try/except (that only
    # wraps fetch_price_history), so a ValueError here previously
    # propagated straight out of score_candidate() and aborted the whole
    # advisory batch rather than being recorded in raw["errors"].
    raw = score_candidate(
        "DUPSESSION",
        benchmark_returns=None,
        lookback_years=3,
        skip_sentiment=True,
        news_api_key=None,
        analyzer=None,
        history_dir=None,
        benchmark_calendar=full_calendar,
    )

    assert raw is not None


def test_score_candidate_keeps_last_row_for_duplicate_trading_session(monkeypatch):
    # Codex review, PR #654 ("Keep the final row for duplicate trading
    # sessions"): an alternate source supplies TWO observations for one
    # session at different prices (e.g. a 09:30 open print and a 16:00
    # close print) that both normalize() to the same session-date label.
    # Keeping the FIRST occurrence (the prior fix's default) retains the
    # stale opening print as that session's daily close instead of the
    # real closing price, distorting health/backtest returns -- pricing.py's
    # own canonical normalization deliberately uses keep="last" for the
    # exact same hazard.
    full_calendar = pd.bdate_range(end=pd.Timestamp.now().normalize(), periods=5)
    candidate_index = full_calendar.insert(0, full_calendar[0])  # duplicate the first session
    # First (stale, kept by keep="first") row at $100, second (real close,
    # kept by keep="last") row at $999 for that same duplicated session;
    # every other session at $100 for contrast.
    closes = [100.0, 999.0] + [100.0] * 4
    frame = pd.DataFrame(
        {"Open": closes, "High": closes, "Low": closes, "Close": closes, "Adj Close": closes, "Volume": 1},
        index=candidate_index,
    )
    monkeypatch.setattr(advise_tickers, "fetch_price_history", lambda ticker, **kwargs: frame)

    captured_close = {}

    def capturing_backtest_score(close):
        captured_close["close"] = close
        return {}

    monkeypatch.setattr(advise_tickers, "backtest_score", capturing_backtest_score)

    score_candidate(
        "DUPPRICE",
        benchmark_returns=None,
        lookback_years=3,
        skip_sentiment=True,
        news_api_key=None,
        analyzer=None,
        history_dir=None,
        benchmark_calendar=full_calendar,
    )

    close = captured_close["close"]
    duplicated_session = full_calendar[0].normalize()
    assert not close.index.duplicated().any()
    assert close.loc[duplicated_session] == 999.0


def test_index_is_datetime_like_rejects_multiindex_without_raising():
    # Codex review, PR #1300 discussion_r3920155086 ("Handle MultiIndex
    # before calling pd.isna"): an alternate price-history source can return
    # a MultiIndex, e.g. (session, ticker). pd.isna(index) raises
    # NotImplementedError for a MultiIndex rather than returning a mask,
    # which previously escaped this function (and its callers' per-signal
    # error-recording guards), aborting the entire benchmark setup or
    # candidate scoring instead of taking the documented degraded
    # no-calendar path.
    multi = pd.MultiIndex.from_tuples(
        [(pd.Timestamp("2026-07-20"), "AAPL"), (pd.Timestamp("2026-07-21"), "AAPL")],
        names=["session", "ticker"],
    )
    assert advise_tickers._index_is_datetime_like(multi) is False


def test_index_is_datetime_like_rejects_all_nat_period_index():
    # Codex review, PR #654 follow-up ("Reject all-NaT PeriodIndexes before
    # normalization"): a nonempty daily PeriodIndex holding only NaT is the
    # same "genuinely typed but carries no calendar information" case the
    # datetime64 branch above already rejects -- without this, it reached
    # _normalize_datetime_frame() and produced an all-NaT DatetimeIndex,
    # whose min()/max() then raised inside
    # _detect_benchmark_calendar_gaps() (NaTType has no `normalize`).
    nat_only_period = pd.PeriodIndex([pd.NaT, pd.NaT], freq="D")
    assert not advise_tickers._index_is_datetime_like(nat_only_period)

    # A single valid period among NaTs is still usable and still accepted.
    mixed_period = pd.PeriodIndex([pd.NaT, pd.Period("2026-08-03", freq="D")])
    assert advise_tickers._index_is_datetime_like(mixed_period)


def test_main_cancels_queued_scoring_on_interrupt(tmp_path, monkeypatch):
    # executor.map() submits every candidate up front; an operator's Ctrl-C
    # while iterating results must not let the whole remaining batch run to
    # completion before returning control (Codex review: "Cancel queued
    # scoring work when interrupted"). Simulates the interrupt via the first
    # score_candidate() call and verifies the executor is torn down with
    # cancel_futures=True rather than the bare with-block's default
    # shutdown(wait=True), and that the interrupt is propagated to the
    # caller rather than hard-terminating the interpreter -- main() is a
    # reusable function, not just a CLI entry point, so a caller must be
    # able to run its own cleanup (Codex follow-up review: "Let callers
    # perform cleanup after an interrupt").
    import concurrent.futures

    def _raise_price_unavailable(ticker, **kwargs):
        raise PriceHistoryUnavailable(f"no history for {ticker}")

    def _raise_keyboard_interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(advise_tickers, "fetch_price_history", _raise_price_unavailable)
    monkeypatch.setattr(advise_tickers, "score_candidate", _raise_keyboard_interrupt)

    shutdown_calls = []
    original_shutdown = concurrent.futures.ThreadPoolExecutor.shutdown

    def _tracking_shutdown(self, *args, **kwargs):
        shutdown_calls.append((args, kwargs))
        return original_shutdown(self, *args, **kwargs)

    monkeypatch.setattr(concurrent.futures.ThreadPoolExecutor, "shutdown", _tracking_shutdown)

    with pytest.raises(KeyboardInterrupt):
        advise_tickers.main(
            ["--candidates", "ZZZZZZ", "YYYYYY", "--skip-sentiment", "--output-dir", str(tmp_path / "out")]
        )

    assert len(shutdown_calls) == 1
    _args, kwargs = shutdown_calls[0]
    assert kwargs.get("wait") is False
    assert kwargs.get("cancel_futures") is True


def test_main_bounds_concurrent_candidate_scoring(tmp_path, monkeypatch):
    # The unbounded default ThreadPoolExecutor worker count (min(32,
    # os.cpu_count() + 4)) would let that many candidates issue NewsAPI
    # requests at once, some of which then hit throttling and silently lose
    # news evidence while their peers keep it (Codex review: "Bound
    # parallel NewsAPI requests"). Matches the bound this repo's other
    # concurrent sentiment collector uses for the same reason
    # (sentiment_cron.py's MAX_CONCURRENT_TICKERS).
    import concurrent.futures

    def _raise_price_unavailable(ticker, **kwargs):
        raise PriceHistoryUnavailable(f"no history for {ticker}")

    monkeypatch.setattr(advise_tickers, "fetch_price_history", _raise_price_unavailable)

    captured_max_workers = []
    original_init = concurrent.futures.ThreadPoolExecutor.__init__

    def _tracking_init(self, *args, max_workers=None, **kwargs):
        captured_max_workers.append(max_workers)
        return original_init(self, *args, max_workers=max_workers, **kwargs)

    monkeypatch.setattr(concurrent.futures.ThreadPoolExecutor, "__init__", _tracking_init)

    exit_code = advise_tickers.main(
        ["--candidates", "ZZZZZZ", "YYYYYY", "--skip-sentiment", "--output-dir", str(tmp_path / "out")]
    )

    assert exit_code == 2  # both candidates fail to price -- unrelated to this assertion
    assert captured_max_workers == [advise_tickers.MAX_CONCURRENT_CANDIDATES]
