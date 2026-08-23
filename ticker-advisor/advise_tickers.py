#!/usr/bin/env python3
"""Score candidate tickers for the DCA universe: strategy health, backtest, sentiment.

Reads a supplied candidate list (never discovers candidates on its own -- see
README.md), scores each one against three independent signals, and writes a
ranked report plus a *proposed* diff for config/tickers.json /
near_close_dca.ACTIVE_TICKERS / config/scheduled_tasks.json's
canonical_first_buy_and_topup_rule.active_universe.active list -- skipping
that third file leaves the scheduled execution universe stale even after the
other two are updated (Codex review on PR #403: "Include the scheduled-task
edit in the CLI contract"). It never edits any of the three files itself: a
human or an external agent session with Robinhood MCP access still has to
verify broker tradability and apply the edit by hand, exactly as documented
in docs/strategy.md's "TICKER UNIVERSE UPDATE" changelog.

Usage:
    python advise_tickers.py --candidates COIN PLTR SHOP
    python advise_tickers.py --candidates-file candidates.txt --skip-sentiment
    python advise_tickers.py --candidates COIN --weights health=2,backtest=1,sentiment=1
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
# Owned HERE, not inherited as a side effect of importing `pricing` first --
# relying on another module's sys.path mutation made `import dca_paths`
# below fragile: an isort/`ruff --select I` pass (or any tool reordering
# these imports alphabetically) would place it before `from pricing import
# ...` and break with ModuleNotFoundError, since dca_paths.py lives at
# REPO_ROOT, not on the default import path (Antigravity review on PR
# #403: "Fragile Import Order Dependency in advise_tickers.py"). Inserted
# at index 1, not 0 -- index 0 is already this script's own directory
# (ticker-advisor/, set by the interpreter before any of this module's
# code runs), and putting REPO_ROOT ahead of it would let a same-named
# top-level module there (a future repo-root "signals.py"/"pricing.py"/
# etc.) shadow this subproject's own submodules (Antigravity review on PR
# #403: "sys.path Precedences in advise_tickers.py").
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(1, str(REPO_ROOT))

import dca_paths  # noqa: E402
from dca_ticker_paths import configured_ticker_history_dir  # noqa: E402
from pricing import PriceHistoryUnavailable, fetch_price_history  # noqa: E402
from report import render_markdown, render_proposed_diff  # noqa: E402
from scoring import (  # noqa: E402
    _BACKTEST_STRATEGIES,
    _HEALTH_COMPONENTS,
    DEFAULT_WEIGHTS,
    TICKER_SYMBOL_PATTERN,
    _extract_backtest_component,
    _extract_health_component,
    _extract_sentiment_component,
    combine_scores,
)
from scripts.file_lock import cross_process_lock  # noqa: E402
from signals import FinbertSentimentAnalyzer, backtest_score, sentiment_score, strategy_health_score  # noqa: E402

DEFAULT_BENCHMARK = "SPY"
_VALID_WEIGHT_KEYS = frozenset({"health", "backtest", "sentiment"})
_URL_PATTERN = re.compile(r"https?://\S+")


def _sanitize_error_message(exc: Exception) -> str:
    """Redact URLs from an exception message before it's persisted to disk.

    A Hugging Face Hub / FinBERT model-download failure can embed a fully
    authenticated or signed request URL (e.g. a presigned S3 URL carrying a
    query-string token) in the exception text. Passing str(exc) through
    unmodified would copy that straight into run_metadata, every
    candidate's rationale, and repository-local analysis/ artifacts that
    may get committed -- persist the exception class and a URL-redacted
    message instead (Codex review on PR #403: "Sanitize sentiment
    initialization errors before persisting them").
    """
    return f"{type(exc).__name__}: {_URL_PATTERN.sub('<redacted-url>', str(exc))}"


def _parse_weights(raw: str | None) -> dict[str, float] | None:
    if not raw:
        return None
    weights: dict[str, float] = {}
    for pair in raw.split(","):
        key, _, value = pair.partition("=")
        key = key.strip()
        if not key or not value:
            raise ValueError(f"malformed --weights entry {pair!r}; expected key=value")
        # A typo like "heath=2" would otherwise be silently ignored --
        # combine_scores() only reads the three known keys, so the run would
        # succeed but quietly use the DEFAULT_WEIGHTS health value instead
        # of the one the operator thought they set (Codex review on PR
        # #403: "Reject unknown weight names instead of silently ignoring
        # them").
        if key not in _VALID_WEIGHT_KEYS:
            raise ValueError(
                f"unknown --weights key {key!r}; expected one of {sorted(_VALID_WEIGHT_KEYS)}"
            )
        try:
            parsed_value = float(value)
        except ValueError as exc:
            raise ValueError(f"malformed --weights value in {pair!r}: {exc}") from exc
        if not math.isfinite(parsed_value):
            # float("nan")/float("inf") both parse without raising, and NaN
            # in particular passes a naive `< 0` check too (NaN compares
            # False to everything). A NaN weight contaminates
            # weighted_sum/weight_total and silently turns an otherwise
            # usable candidate into WATCH-with-no-composite; an infinite one
            # can produce a NaN composite classified as REJECT -- either way
            # a malformed parameter should be a CLI error, not a
            # normal-looking but invalid report (Codex review on PR #403:
            # "Reject non-finite custom weights").
            raise ValueError(f"weight for {key!r} must be finite, got {parsed_value}")
        if parsed_value < 0:
            # A negative weight inverts that signal's contribution to the
            # composite -- a good Sharpe would count AGAINST a candidate --
            # which has no sensible use case as a weight (as opposed to,
            # say, deliberately zeroing a signal out) and most likely means
            # a typo (Antigravity review on PR #403: "Enforce Non-Negative
            # Weights").
            raise ValueError(f"weight for {key!r} must be non-negative, got {parsed_value}")
        weights[key] = parsed_value
    return weights


def _positive_int(raw: str) -> int:
    """argparse type= for --lookback-years: reject zero/negative windows.

    A non-positive value still parses fine as an int, and pricing.py's
    _windowed()/yfinance's period=f"{n}y" don't reject it either -- a fresh
    archive gets windowed down to its own final row or an empty future
    window, and yfinance receives a nonsensical "0y"/negative period.
    Health and backtest quietly disappear (too few observations) while a
    multi-candidate run still emits normal-looking, sentiment-only
    classifications, with nothing telling the operator why (Codex review on
    PR #403: "Require a positive lookback window").
    """
    value = int(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError(f"--lookback-years must be positive, got {value}")
    return value


def _load_candidates(candidates: list[str], candidates_file: Path | None) -> list[str]:
    all_candidates = list(candidates)
    if candidates_file is not None:
        try:
            text = candidates_file.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ValueError(f"--candidates-file not found: {candidates_file}") from exc
        except (OSError, UnicodeDecodeError) as exc:
            # A directory, a permissions error, or invalid UTF-8 all raise
            # something OTHER than FileNotFoundError -- IsADirectoryError/
            # PermissionError are OSError subclasses, and UnicodeDecodeError
            # is its own type (a ValueError subclass, not an OSError one).
            # Uncaught, any of these bypasses main()'s user-facing "error:
            # ..." exit path and terminates the CLI with a raw traceback
            # instead (Codex review on PR #403: "Convert all candidate-file
            # read failures into CLI errors").
            raise ValueError(f"--candidates-file could not be read: {candidates_file} ({exc})") from exc
        all_candidates.extend(
            line.strip().upper() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")
        )
    seen: set[str] = set()
    ordered: list[str] = []
    for ticker in all_candidates:
        # .strip() here too, not just on the --candidates-file lines above
        # -- a --candidates CLI value with incidental surrounding
        # whitespace (e.g. from a wrapper script that didn't trim) would
        # otherwise fail TICKER_SYMBOL_PATTERN and get silently dropped as
        # "invalid-shaped" even though the ticker itself was fine
        # (Antigravity review on PR #403: "Candidate Input Whitespace
        # Normalization").
        upper = ticker.strip().upper()
        if upper not in seen:
            seen.add(upper)
            ordered.append(upper)
    return ordered


def _split_valid_symbol_shape(candidates: list[str]) -> tuple[list[str], list[str]]:
    """Split candidates into (valid-shaped, invalid-shaped) against config/tickers.json's schema.

    Filtering happens here, BEFORE scoring, not only in report.py's later
    diff-emission filter: an invalid-shaped candidate like "BRK-B" would
    otherwise still participate in scoring.combine_scores()'s cross-candidate
    z-score normalization, changing every OTHER candidate's composite (and
    potentially its ADD/WATCH/REJECT classification) even though its own
    proposed edit was already being discarded downstream (Codex review on
    PR #403: "Validate symbols before cross-candidate normalization").
    """
    valid = [t for t in candidates if TICKER_SYMBOL_PATTERN.match(t)]
    invalid = [t for t in candidates if not TICKER_SYMBOL_PATTERN.match(t)]
    return valid, invalid


def _load_legacy_symbol_corrections(repo_root: Path) -> dict[str, str]:
    """Read config/tickers.json's legacy-symbol correction map, read-only.

    A candidate matching one of these keys (e.g. "IRDMI", corrected in that
    config to the already-active "IRDM") satisfies the ticker schema and is
    neither in `active` nor `excluded`, so without this check it would
    still participate in combine_scores()'s cross-candidate normalization
    and could be proposed as an ADD right alongside the canonical symbol
    it's a legacy alias for (Codex review on PR #403: "Reject corrected
    legacy symbols before scoring"). Degrades to an empty map on a
    missing/malformed config file, same as _load_current_universe.
    """
    path = repo_root / "config" / "tickers.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        # Syntactically valid JSON with a non-object top level (null, a
        # bare list, ...) makes .get() raise AttributeError, OUTSIDE this
        # except clause, aborting the whole CLI with a traceback before any
        # candidates are scored -- contradicting this function's own
        # documented "degrade to an empty map" contract for a malformed
        # config file (Codex review on PR #403: "Validate the ticker-
        # config root before reading mappings").
        if not isinstance(payload, dict):
            return {}
        corrections = payload.get("symbol_corrections")
        if not isinstance(corrections, dict):
            return {}
        return {key.upper(): value for key, value in corrections.items()}
    except (OSError, json.JSONDecodeError):
        return {}


def _split_legacy_symbols(candidates: list[str], corrections: dict[str, str]) -> tuple[list[str], list[str]]:
    kept = [t for t in candidates if t not in corrections]
    legacy = [t for t in candidates if t in corrections]
    return kept, legacy


def _load_current_universe(repo_root: Path) -> tuple[frozenset[str], frozenset[str]]:
    """Read config/tickers.json's current active/excluded sets, read-only.

    render_proposed_diff() needs these to avoid proposing a duplicate
    addition or an already-excluded symbol -- see its docstring. A missing
    or malformed config file degrades to "no filtering" (empty sets) with a
    warning rather than aborting the whole advisory run: this tool's own
    output is still useful, just without that one safety check, and the
    human/agent applying the diff by hand is the actual backstop either way.
    """
    path = repo_root / "config" / "tickers.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        # Same non-object-top-level risk as _load_legacy_symbol_corrections
        # above (Codex review on PR #403: "Validate the ticker-config root
        # before reading mappings") -- raise here so the except clause
        # below degrades it the same way as a missing/malformed file.
        if not isinstance(payload, dict):
            raise TypeError(f"expected a JSON object at the top level, got {type(payload).__name__}")
        # A schema-invalid but present value ("active": null or "active":
        # {}) previously fell through the isinstance(active, list) check
        # below silently -- with no warning at all, unlike every other
        # malformed-config case this function degrades through. An
        # actually-active candidate could then be scored and proposed as
        # a duplicate addition, while the report gave no indication the
        # universe-filtering check had failed (Antigravity review on PR
        # #403: "Warn on malformed active and excluded collections").
        for key in ("active", "excluded", "whole_share"):
            if key in payload and not isinstance(payload[key], list):
                raise TypeError(f'config/tickers.json\'s "{key}" must be a list, got {type(payload[key]).__name__}')
        # "active" is schema-REQUIRED (schemas/tickers.schema.json's own
        # "required": ["active"]) -- its total absence is exactly as
        # malformed as a present-but-wrong-type value, not the benign
        # "no active concept" case an absent OPTIONAL field (like
        # "excluded", not schema-required) would be. Without this, a
        # schema-invalid config missing "active" entirely produced no
        # warning at all, and an already-active candidate could be scored
        # and proposed as a duplicate addition with the report giving no
        # indication the universe-filtering check had failed (Antigravity
        # review on PR #403: "Treat a missing active universe as
        # malformed").
        if "active" not in payload:
            raise TypeError('config/tickers.json is missing its required "active" key')
        active = payload.get("active")
        excluded = payload.get("excluded")
        whole_share = payload.get("whole_share")
        # Uppercased (and non-string entries dropped) to match how
        # candidates are normalized (_load_candidates()'s
        # .strip().upper()) -- a lowercase/mixed-case entry in
        # config/tickers.json would otherwise silently fail to match an
        # uppercased candidate, letting an already-active or explicitly-
        # excluded symbol through _split_ineligible_universe_symbols()
        # undetected (Antigravity review on PR #403: "Case Normalization
        # in _load_current_universe").
        active_set = (
            frozenset(t.upper() for t in active if isinstance(t, str)) if isinstance(active, list) else frozenset()
        )
        # config/tickers.json's third category, `whole_share` (tickers not
        # tradable for fractional buys, accumulated toward a whole share
        # instead -- see docs/whole_share_accumulation.md), is disjoint
        # from `active` by the schema's own invariant. It is folded into
        # the returned "active" set here (not surfaced as its own bucket)
        # because from this advisor's perspective a whole-share ticker is
        # just as ineligible for a fresh ADD proposal as anything already
        # in `active` -- without this, a whole-share symbol (e.g. BYDDY)
        # would be scored and proposed as a "new" addition, skewing every
        # other candidate's z-score normalization and duplicating a symbol
        # already in the live universe (Codex review: "Exclude whole-share
        # symbols from ticker-advisor candidates").
        whole_share_set = (
            frozenset(t.upper() for t in whole_share if isinstance(t, str))
            if isinstance(whole_share, list)
            else frozenset()
        )
        return (
            active_set | whole_share_set,
            frozenset(t.upper() for t in excluded if isinstance(t, str))
            if isinstance(excluded, list)
            else frozenset(),
        )
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        print(
            f"warning: could not read {path} to filter already-active/excluded tickers ({exc}); "
            "proposed_tickers_diff.json will not check for duplicates against the live universe",
            file=sys.stderr,
        )
        return frozenset(), frozenset()


def _split_ineligible_universe_symbols(
    candidates: list[str], active_tickers: frozenset[str], excluded_tickers: frozenset[str]
) -> tuple[list[str], list[str]]:
    """Split candidates into (eligible, already-active-or-excluded) against the live universe.

    Filtering happens here, BEFORE scoring -- render_proposed_diff()'s own
    active/excluded filter only drops these from the proposed diff
    afterward, by which point an already-active or explicitly-excluded
    candidate has already participated in combine_scores()'s cross-
    candidate z-score normalization, changing every OTHER candidate's
    composite (and potentially its ADD/WATCH/REJECT classification) even
    though its own proposed edit was always going to be discarded
    downstream -- the same class of skew already guarded against for
    invalid-shaped and legacy symbols above (Codex review on PR #403:
    "Exclude ineligible symbols before normalization").
    """
    ineligible = active_tickers | excluded_tickers
    eligible = [t for t in candidates if t not in ineligible]
    dropped = [t for t in candidates if t in ineligible]
    return eligible, dropped


def _write_atomic(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """Write `content` to `path` atomically via a same-directory temp file + rename.

    write_text() writes in place -- a process interrupted mid-write leaves
    a truncated file behind, and a run whose LATER artifact fails to
    generate after an EARLIER one already landed leaves a new scores.json
    next to a stale or missing scores.md/proposed diff. No consumer of
    these three files can tell a partial run's output from a coherent
    recommendation (Codex review on PR #403: "Publish the report files
    without leaving mixed generations"). os.replace() is atomic on POSIX
    and Windows when source and destination share a filesystem, which a
    same-directory temp file guarantees. The PID alone isn't a unique
    temp-file token if this function is ever called concurrently from
    multiple threads within the same process (main() itself is a plain
    sequential loop today, but a future caller might not be) -- two
    threads writing the same destination filename would collide on the
    identical temp path (Antigravity review on PR #403: "Race Condition
    Risk in _write_atomic for Same-Process Threads").
    """
    tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    tmp_path.write_text(content, encoding=encoding)
    os.replace(tmp_path, path)


def _allocate_default_output_dir(output_root: Path, now: datetime, *, max_attempts: int = 1000) -> Path | None:
    """Exclusively create and return a fresh dated output directory for the default (no --output-dir) path.

    Two default-path invocations starting within the same second (e.g.
    parallel agent/automation runs) previously collided on this directory's
    coarse %H%M%S timestamp, silently writing the same three filenames and
    risking overwritten/interleaved reports and proposed diffs. Microsecond
    resolution shrinks the collision window, but exclusive creation with a
    retry is what actually guarantees uniqueness under true concurrency --
    clock granularity and containerized/virtualized clocks can still
    produce the same timestamp twice (Codex review on PR #403: "Allocate a
    unique directory for concurrent runs"). Returns None if every attempt
    up to ``max_attempts`` collides.
    """
    day_dir = output_root / f"{now.date().isoformat()}-ticker-advisory"
    for attempt in range(max_attempts):
        suffix = f"-{attempt}" if attempt else ""
        candidate_dir = day_dir / f"{now.strftime('%H%M%S%f')}{suffix}"
        try:
            candidate_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            continue
        return candidate_dir
    return None


def _detect_benchmark_calendar_gaps(benchmark_calendar: pd.DatetimeIndex) -> list[pd.Timestamp]:
    """Flag likely trading sessions within the benchmark's own range that it has no row for.

    ``benchmark_calendar`` is the ONLY session calendar this module has --
    score_candidate() reindexes every candidate onto it (restricted to that
    candidate's own observed range) to turn an entirely-missing session row
    into an ordinary NaN the existing gap-exclusion logic already handles.
    But that only works if the benchmark's own calendar is itself complete:
    when the benchmark history omits a trading-session row, that date is
    silently absent from ``benchmark_calendar`` too, so a candidate missing
    the SAME row is reindexed onto an equally incomplete calendar and comes
    out looking gap-free -- both series bridge the same multi-session move
    undetected (Codex review on PR #429: "Validate the benchmark calendar
    independently").

    This cross-checks ``benchmark_calendar`` against ``pandas_market_
    calendars``' real NYSE trading-session calendar instead of trusting it
    as ground truth. An earlier version of this function approximated the
    exchange calendar with federal holidays + weekends, but that's a
    materially different calendar -- Good Friday is an NYSE holiday but
    not a federal one, while Columbus Day/Veterans Day are federal
    holidays the market trades straight through. Flagging Good Friday as
    a "gap" and having the caller union it into every candidate's
    reindex calendar would inject a SYNTHETIC missing session into every
    candidate's history on a real non-trading day, degrading health/
    backtest scoring across the whole run instead of only around a
    genuine data-quality issue (Codex review on PR #429: "Use an exchange
    calendar before adding gap dates"). The real NYSE calendar has no such
    false positives, so callers can safely union its result directly.
    """
    if benchmark_calendar.empty:
        return []
    import pandas_market_calendars as mcal  # type: ignore

    nyse_sessions = mcal.get_calendar("NYSE").schedule(
        start_date=benchmark_calendar.min(), end_date=benchmark_calendar.max()
    ).index
    return list(pd.DatetimeIndex(nyse_sessions).difference(benchmark_calendar))


def _json_safe(value: object) -> object:
    """Recursively replace non-finite floats (NaN/inf) with None.

    json.dumps happily emits bare `NaN`/`Infinity` tokens for those values
    by default -- valid Python, but outside the JSON standard, so a strict
    consumer's JSON.parse can't read scores.json. `null` is the correct
    "not available" representation instead (Codex review on PR #403:
    "Serialize unavailable metrics as valid JSON values").
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _price_returns(frame: pd.DataFrame) -> pd.Series:
    close_column = "Adj Close" if "Adj Close" in frame.columns else "Close"
    # pct_change(fill_method=None) on the RAW indexed series, not dropna()
    # first: dropping the gap row before calling pct_change() closes the
    # date gap and bridges the previous valid price directly to the next
    # one, recording an entire multi-session move as a SINGLE row's return
    # -- Sharpe/CAGR/Calmar all assume every row is exactly one trading day
    # apart (CAGR's exponent is TRADING_DAYS/len(returns)), so compressing a
    # multi-day move into one row inflates or suppresses those metrics and
    # misassigns the bridged return to a single date for benchmark
    # alignment. Calling pct_change on the original series instead (with
    # fill_method=None, not the deprecated pad default that fabricates a
    # 0%-return gap day) drops both the gap row AND the row immediately
    # after it -- its return would otherwise need the NaN as a baseline --
    # excluding the elapsed period entirely rather than compressing it into
    # a fabricated row (Codex review on PR #403: "Avoid collapsing missing-
    # price gaps into one-day returns").
    returns = frame[close_column].pct_change(fill_method=None)
    # dropna() alone leaves +/-inf in place (e.g. a zero-baseline price
    # producing a division-by-zero pct_change) -- a single infinite return
    # poisons statistics.mean()/pstdev() for the WHOLE series once it
    # reaches strategy_health_score(), not just that one day, corrupting
    # every risk-adjusted metric for the candidate instead of just
    # excluding the one bad observation. np.isfinite() is False for NaN
    # too, so this replaces dropna() entirely rather than needing both
    # (Antigravity review on PR #403: "Unfiltered Non-Finite Returns
    # (inf / -inf) in _price_returns").
    return cast(pd.Series, returns[np.isfinite(returns)])


def _signal_has_usable_metric(raw: dict[str, object], signal: str) -> bool:
    """Whether `raw[signal]` has at least one component combine_scores() would actually use.

    Mirrors combine_scores()'s own had_raw computation exactly (same
    extraction helpers, same components) rather than a looser "the
    container isn't None" check -- a health/backtest dict can be present
    but hold nothing but NaN (e.g. Sharpe/Calmar under
    strategy_health_score()'s zero-variance guard), which combine_scores()
    itself would treat as unusable (Antigravity review on PR #403: "Check
    weighted metric usability, not container presence").
    """
    if signal == "health":
        return any(_extract_health_component(raw.get("health"), c) is not None for c in _HEALTH_COMPONENTS)
    if signal == "backtest":
        return any(_extract_backtest_component(raw.get("backtest"), s) is not None for s in _BACKTEST_STRATEGIES)
    if signal == "sentiment":
        return _extract_sentiment_component(raw.get("sentiment")) is not None
    return False


def _signal_has_normalizable_metric(raw_scores: dict[str, dict[str, object]], signal: str) -> bool:
    """Whether `signal` has a component that will actually survive combine_scores()'s z-scoring.

    scoring._zscore() excludes a component for EVERY candidate in the batch
    when fewer than 2 of them have a usable value for it -- unlike
    _signal_has_usable_metric() (which only asks "does ANY candidate have
    ANY usable value"), this checks the same >= 2 cross-candidate count
    _zscore() itself applies, so a batch where only one candidate ever
    produced a usable value for this signal correctly reports "not
    normalizable" even though a raw value technically exists somewhere
    (Codex review on PR #403: "Require weighted metrics to be
    normalizable").
    """
    tickers = list(raw_scores)
    if signal == "health":
        return any(
            sum(1 for t in tickers if _extract_health_component(raw_scores[t].get("health"), c) is not None) >= 2
            for c in _HEALTH_COMPONENTS
        )
    if signal == "backtest":
        return any(
            sum(1 for t in tickers if _extract_backtest_component(raw_scores[t].get("backtest"), s) is not None) >= 2
            for s in _BACKTEST_STRATEGIES
        )
    if signal == "sentiment":
        return (
            sum(1 for t in tickers if _extract_sentiment_component(raw_scores[t].get("sentiment")) is not None) >= 2
        )
    return False


def score_candidate(
    ticker: str,
    *,
    benchmark_returns: pd.Series | None,
    lookback_years: int,
    skip_sentiment: bool,
    news_api_key: str | None,
    analyzer: FinbertSentimentAnalyzer | Any | None,
    history_dir: Path | None,
    sentiment_unavailable_reason: str | None = None,
    benchmark_calendar: pd.DatetimeIndex | None = None,
    run_as_of: datetime | None = None,
) -> dict[str, Any]:
    """Every signal is best-effort: one signal's failure doesn't drop the candidate,
    it just leaves that signal None with the error recorded in ``errors``."""
    raw: dict[str, Any] = {"health": None, "backtest": None, "sentiment": None, "errors": []}
    errors: list[str] = raw["errors"]

    # Price history feeds health/backtest only -- sentiment is independent
    # of it (news/SEC text, not price data), so a price-history failure must
    # not skip sentiment too. An early `return raw` here used to drop a
    # newly-listed or temporarily-unavailable ticker's sentiment signal even
    # though nothing about fetching it depends on price data being present
    # (Codex review on PR #403: "Continue to sentiment after price-history
    # failures").
    try:
        # run_as_of anchors this candidate's future/current-date/staleness
        # cutoffs to the SAME run-level instant already used for the
        # benchmark fetch and the sentiment lookback below -- otherwise a
        # run spanning UTC midnight could give the benchmark and
        # candidates (or candidates on opposite sides of midnight)
        # different effective cutoffs (Antigravity review on PR #403:
        # "Anchor price cutoffs to the run timestamp").
        frame = fetch_price_history(
            ticker,
            lookback_years=lookback_years,
            history_dir=history_dir,
            now=pd.Timestamp(run_as_of) if run_as_of is not None else None,
        )
    except PriceHistoryUnavailable as exc:
        errors.append(str(exc))
        frame = None

    if frame is not None:
        # fetch_price_history() attaches this when it fell back to a stale/
        # incomplete local archive after a live refresh also failed --
        # without surfacing it here, a reviewer reading scores.json/
        # scores.md/the proposed diff can't tell this candidate's
        # health/backtest signals (and any resulting ADD recommendation)
        # were derived from that gap rather than current data (Codex review
        # on PR #403: "Persist stale-archive fallback warnings in result
        # artifacts").
        fallback_reason = frame.attrs.get("stale_archive_fallback_reason")
        if fallback_reason:
            errors.append(f"price history fallback: {fallback_reason}")

        if benchmark_calendar is not None and not frame.empty:
            # A source that omits a trading-session ROW entirely (rather
            # than retaining it with a null price) is invisible to the
            # NaN-based gap detection below and to _price_returns()'s own
            # pct_change() -- there's no missing LABEL to notice, so a
            # multi-session move silently bridges into what looks like a
            # single day's return, the same distortion the null-price gap
            # fix already closed for the case where the row exists but is
            # blank. A fixed weekday/business-day range would also flag
            # genuine market holidays as false "gaps" (this codebase has
            # no holiday calendar of its own); the benchmark's OWN index,
            # already fetched from the same price-history source, only
            # ever contains days the market actually traded, so
            # reindexing onto the union of the candidate's own dates and
            # the benchmark's sessions (restricted to the candidate's own
            # observed range) turns a genuinely-missing session back into
            # an ordinary NaN row the EXISTING gap-exclusion logic already
            # handles correctly, instead of needing a second detection
            # mechanism (Antigravity review on PR #403: "Detect missing
            # index sessions as price-history gaps").
            if not pd.api.types.is_datetime64_any_dtype(frame.index):
                # A monkeypatched/alternate data source can hand back a
                # non-datetime index (e.g. a plain RangeIndex) -- keep
                # scoring alive and record that missing-session detection
                # was skipped for this candidate instead of aborting the
                # entire batch. Checking the index dtype directly rather
                # than wrapping pd.Timestamp(...) in a try/except matters:
                # pd.Timestamp(0) on an integer RangeIndex value SUCCEEDS by
                # interpreting it as nanoseconds since the Unix epoch
                # instead of raising, so the try/except never caught this
                # case -- the coercion silently produced a bogus 1970
                # timestamp range, found no benchmark sessions in it, and
                # continued gap-sensitive scoring with missing-session
                # detection disabled and no warning recorded at all (Codex
                # review, PR #538: "Reject numeric indexes before timestamp
                # coercion").
                errors.append(
                    "candidate history index is not datetime-like -- missing-session detection skipped "
                    "for this candidate's health/backtest signals"
                )
            else:
                frame_index_start = pd.Timestamp(frame.index.min())
                frame_index_end = pd.Timestamp(frame.index.max())
                expected_sessions = benchmark_calendar[
                    (benchmark_calendar >= frame_index_start) & (benchmark_calendar <= frame_index_end)
                ]
                frame = frame.reindex(frame.index.union(expected_sessions))
        elif benchmark_calendar is None and not frame.empty:
            # The benchmark's own fetched index is the ONLY calendar this
            # module has for missing-session detection (see the comment
            # above -- there's no standalone holiday calendar of its own).
            # When the benchmark fetch itself failed, main()'s stderr
            # warning says only "beta/alpha will be NaN," but missing-
            # session detection is silently disabled for THIS candidate's
            # health/backtest signals too -- a source that omits a trading
            # day entirely (rather than nulling it) would bridge undetected
            # into a fabricated multi-session bar for both, not just
            # beta/alpha, with nothing recording that the gap check never
            # ran (Codex review on PR #403: "Disable gap-sensitive scoring
            # without a session calendar").
            errors.append(
                "no benchmark calendar available (benchmark fetch failed) -- missing-session detection "
                "skipped for this candidate's health/backtest signals, not just beta/alpha; a source that "
                "omits a trading day entirely, rather than nulling it, would go undetected"
            )

        raw_close = frame["Adj Close" if "Adj Close" in frame.columns else "Close"]
        returns = _price_returns(frame)

        # dropna() on the raw price series compacts observations on
        # opposite sides of any INTERNAL gap into ADJACENT bars --
        # _portfolio_metrics() pins vectorbt's Portfolio.from_signals to
        # freq="1D", so a multi-session gap in the middle of the series
        # silently becomes a single daily bar, distorting both backtest
        # strategies' Sharpe ratios and the resulting ranking. A gap
        # before the first or after the last valid price doesn't compact
        # anything, so only an INTERNAL gap disqualifies the series
        # (Codex review on PR #403: "Preserve missing sessions in
        # backtest inputs").
        first_valid = raw_close.first_valid_index()
        last_valid = raw_close.last_valid_index()
        has_internal_gap = (
            first_valid is not None and last_valid is not None and raw_close.loc[first_valid:last_valid].isna().any()
        )

        # _price_returns()'s pct_change-before-dropna ordering excludes
        # BOTH the gap row and the return immediately after it, rather
        # than bridging them into one fabricated multi-session return --
        # but strategy_health_score() still computes over whatever
        # observations remain, silently missing the ENTIRE price movement
        # across the gap. _cagr()'s TRADING_DAYS/n annualization exponent
        # then uses the REDUCED post-exclusion observation count `n`,
        # even though the real elapsed calendar time was longer, over-
        # annualizing CAGR (and so Calmar and max_drawdown) exactly the
        # same way an uncorrected calendar-compressed backtest would have
        # (Antigravity review on PR #403: "Skip health scoring when price
        # sessions are missing"). Guarded by the SAME has_internal_gap
        # backtest_score() already skips on, rather than a second
        # detection mechanism.
        if has_internal_gap:
            errors.append(
                "health signal skipped -- Close/Adj Close has internal gaps that would understate "
                "the elapsed period behind CAGR/Calmar's annualization"
            )
        else:
            try:
                raw["health"] = strategy_health_score(returns, benchmark_returns)
            except Exception as exc:  # noqa: BLE001 -- one bad signal must not abort the batch
                errors.append(f"health signal failed: {exc}")

        if has_internal_gap:
            errors.append(
                "backtest signal skipped -- Close/Adj Close has internal gaps that dropna() would "
                "silently compress into contiguous daily bars"
            )
        else:
            try:
                raw["backtest"] = backtest_score(raw_close.dropna())
            except Exception as exc:  # noqa: BLE001
                errors.append(f"backtest signal failed: {exc}")

    if not skip_sentiment and analyzer is not None:
        try:
            # run_as_of anchors both the SEC and NewsAPI trailing lookback
            # windows to the SAME instant across every candidate in this
            # run, instead of each fetch independently recomputing "now"
            # -- a run spanning UTC midnight (plausible with a cold
            # multi-GB FinBERT download or a large candidate batch) would
            # otherwise give candidates before and after midnight
            # different windows purely from scoring order, even though
            # run_metadata.generated_at records a single earlier timestamp
            # for the whole run (Antigravity review on PR #403: "Anchor
            # sentiment lookbacks to the run timestamp").
            raw["sentiment"] = sentiment_score(
                ticker, news_api_key=news_api_key, analyzer=analyzer, as_of=run_as_of
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"sentiment signal failed: {exc}")
        else:
            # A SEC EDGAR fetch failure (a 403, a timeout, malformed JSON)
            # inside sentiment_score() degrades to a score-less dict with
            # its own "errors" list rather than raising -- combine_scores()
            # only reads raw["errors"] at THIS top level when building a
            # candidate's rationale, so without lifting it here a genuine
            # SEC request failure would look identical in scores.md to "SEC
            # simply had nothing to report" (Codex review on PR #403:
            # "Preserve SEC request failures in the report").
            sentiment_result = raw["sentiment"]
            if isinstance(sentiment_result, dict):
                sentiment_errors = sentiment_result.get("errors")
                if isinstance(sentiment_errors, list):
                    errors.extend(sentiment_errors)
    elif not skip_sentiment and analyzer is None and sentiment_unavailable_reason:
        # Distinguishes "sentiment ran and genuinely found nothing" (also
        # raw["sentiment"] is None, but no error) from "sentiment never ran
        # because FinbertSentimentAnalyzer() failed to construct" -- both
        # previously looked identical in scores.json/scores.md, so a reader
        # couldn't tell a degraded two-signal composite from a normal run
        # (Codex review on PR #403: "Persist sentiment initialization
        # failures in the report").
        errors.append(f"sentiment signal skipped: {sentiment_unavailable_reason}")

    return raw


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--candidates", nargs="*", default=[], help="Ticker symbols to score.")
    parser.add_argument(
        "--candidates-file", type=Path, default=None, help="One symbol per line; # comments allowed."
    )
    parser.add_argument("--benchmark", default=DEFAULT_BENCHMARK, help="Benchmark for beta/alpha (default: SPY).")
    parser.add_argument(
        "--lookback-years", type=_positive_int, default=3, help="Matches generate_per_ticker_sizing.py's default."
    )
    parser.add_argument("--weights", type=str, default=None, help="e.g. 'health=2,backtest=1,sentiment=1'")
    parser.add_argument("--news-api-key-env", default="NEWS_API_KEY", help="Env var holding a NewsAPI.org key.")
    parser.add_argument(
        "--skip-sentiment", action="store_true", help="Skip FinBERT entirely (no torch/transformers needed)."
    )
    parser.add_argument(
        "--history-dir", type=Path, default=None, help="Override the local ticker-history archive dir."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Default: analysis/<today>-ticker-advisory/<HHMMSS>/ under the repo root "
        "(or $TICKER_ADVISOR_OUTPUT_ROOT if set).",
    )
    args = parser.parse_args(argv)
    # Normalized the same way candidates already are (_load_candidates()'s
    # .strip().upper()) -- pricing.py's archive lookup builds its path as
    # a literal f"{ticker}.csv" with no case-folding of its own, so on a
    # case-sensitive filesystem an unnormalized "--benchmark spy" looks
    # for spy.csv instead of the archived SPY.csv, needlessly falling
    # through to a live yfinance request (and losing beta/alpha for every
    # candidate if that also fails/is offline) even though the archive
    # was right there (Antigravity review on PR #403: "Normalize
    # benchmark symbols before archive lookup"). Mutating args.benchmark
    # itself, not just a local variable, so run_metadata's persisted
    # "benchmark" value matches what was actually fetched.
    args.benchmark = args.benchmark.strip().upper()

    try:
        candidates = _load_candidates(args.candidates, args.candidates_file)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not candidates:
        print("no candidates supplied -- pass --candidates or --candidates-file", file=sys.stderr)
        return 2
    # Captured before any filtering overwrites `candidates` -- run_metadata
    # persists this AS THE ORIGINAL request, alongside the separate
    # exclusion buckets below. Reusing the (by-then-filtered) `candidates`
    # variable for "candidates_requested" silently dropped invalid/legacy/
    # ineligible symbols from a saved run's recorded input and couldn't
    # reconstruct the request's original ordering or duplicates (Codex
    # review on PR #403: "Preserve the original requested candidate
    # list").
    candidates_requested = list(candidates)

    candidates, invalid_shaped = _split_valid_symbol_shape(candidates)
    if invalid_shaped:
        print(
            f"warning: excluding candidates that don't match config/tickers.json's symbol schema "
            f"(^[A-Z.]{{1,6}}$), so they can't skew other candidates' z-scores: {invalid_shaped}",
            file=sys.stderr,
        )
    if not candidates:
        print("no candidates left after excluding invalid-shaped symbols", file=sys.stderr)
        return 2

    legacy_corrections = _load_legacy_symbol_corrections(REPO_ROOT)
    candidates, legacy_symbols = _split_legacy_symbols(candidates, legacy_corrections)
    if legacy_symbols:
        print(
            "warning: excluding legacy symbols config/tickers.json's symbol_corrections already maps to a "
            f"canonical (likely already-active) symbol, so they can't be double-counted or proposed "
            f"redundantly: {[f'{t}->{legacy_corrections[t]}' for t in legacy_symbols]}",
            file=sys.stderr,
        )
    if not candidates:
        print("no candidates left after excluding legacy/corrected symbols", file=sys.stderr)
        return 2

    active_tickers, excluded_tickers = _load_current_universe(REPO_ROOT)
    candidates, ineligible_universe_symbols = _split_ineligible_universe_symbols(
        candidates, active_tickers, excluded_tickers
    )
    if ineligible_universe_symbols:
        print(
            "warning: excluding candidates already in config/tickers.json's active or excluded "
            f"sets, so they can't skew other candidates' z-scores: {ineligible_universe_symbols}",
            file=sys.stderr,
        )
    if not candidates:
        print("no candidates left after excluding already-active/excluded symbols", file=sys.stderr)
        return 2

    try:
        weights = _parse_weights(args.weights)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    effective_weights = {**DEFAULT_WEIGHTS, **(weights or {})}
    if all(value == 0 for value in effective_weights.values()):
        # Each individual weight already passed validation (0 is a valid,
        # useful way to disable one signal) -- but --weights health=0,
        # backtest=0,sentiment=0 zeroes ALL of them, so combine_scores()
        # accumulates weight_total=0.0, gives every candidate a null
        # composite, and reports "no usable signals at all" even though
        # every signal actually computed fine. Reject only this all-zero
        # combination, not any individual zero (Codex review on PR #403:
        # "Reject an all-zero effective weight set").
        print(
            f"error: --weights leaves every signal at 0 ({effective_weights}); nothing left to score with",
            file=sys.stderr,
        )
        return 2
    if args.skip_sentiment and effective_weights["health"] == 0 and effective_weights["backtest"] == 0:
        # --skip-sentiment --weights health=0,backtest=0,sentiment=1 passes
        # the all-zero check above (sentiment is nonzero) but sentiment
        # itself is never computed, so combine_scores() still accumulates
        # weight_total=0.0 for every candidate -- a null composite and a
        # WATCH classification for all of them, silently, with a normal-
        # looking scores.json/scores.md (Codex review on PR #403: "Reject
        # weights that target a skipped signal").
        print(
            f"error: --skip-sentiment disables the only signal --weights leaves nonzero "
            f"({effective_weights}); nothing left to score with",
            file=sys.stderr,
        )
        return 2

    # UTC (not naive local time): render_markdown() independently stamps its
    # own "Generated:" line with datetime.now(timezone.utc) whenever it
    # isn't passed one explicitly -- on a non-UTC host a naive local `now`
    # here would make scores.json's run_metadata.generated_at and
    # scores.md's "Generated:" line disagree about when the run occurred,
    # and a naive timestamp can't be unambiguously interpreted later
    # anyway. Passed explicitly to render_markdown() below so both
    # artifacts stamp the exact same instant (Codex review on PR #403:
    # "Persist one timezone-aware generation timestamp").
    now = datetime.now(timezone.utc)
    # A bare date-only default directory collides across multiple same-day
    # invocations (e.g. evaluating separate batches, or retrying with
    # different --weights) -- a later run's writes would silently replace
    # an earlier run's scores/report/diff with no indication anything was
    # lost (Codex review on PR #403: "Avoid overwriting earlier advisory
    # runs"). Adding a time-of-day component keeps the per-day grouping
    # convention analysis/ already uses elsewhere while making same-day
    # reruns land in distinct directories.
    # TICKER_ADVISOR_OUTPUT_ROOT lets a deployment redirect where advisory
    # output lands (e.g. a read-only checkout where REPO_ROOT/analysis isn't
    # writable) without changing the documented analysis/<date>-ticker-
    # advisory/<time>/ layout for the common case. Falling back to
    # $DCA_CRON_TRACKING_ROOT (nested under its own "ticker_advisor_analysis"
    # subdirectory, not the shared root itself) when that -- but not
    # TICKER_ADVISOR_OUTPUT_ROOT -- is explicitly set means a deployment
    # that already redirected the CORE artifact tree away from the same
    # read-only checkout doesn't also have to separately discover and
    # configure this subproject's own variable (Codex review on PR #403:
    # "Route advisory artifacts through the shared root"). The nested
    # subdirectory name keeps this subproject's versioned, git-tracked
    # analysis/<date>-ticker-advisory/ reports structurally distinct from
    # the core pipeline's gitignored per-stream run-artifact trees, per
    # docs/ticker_advisory.md's documented layout and an earlier round of
    # this same review (Codex review on PR #403: "Route default artifacts
    # through the shared root helpers") -- both concerns hold at once by
    # giving ticker-advisor its own named subtree under whichever root is
    # in effect, rather than writing directly into cron_tracking_root().
    # .get(...) truthiness, not "in os.environ" presence -- an EMPTY string
    # (a deployment that exports the variable but leaves it blank, e.g. a
    # templated CI config substituting an unset value) still satisfies "in
    # os.environ" and Path("") resolves to the invocation's current
    # directory, silently writing a default run's dated artifact directory
    # under whatever directory happened to launch the command -- potentially
    # a read-only checkout -- instead of following the documented analysis/
    # shared-root selection below (Antigravity review on PR #403: "Treat
    # blank output-root overrides as unset"). Matches the DCA_CRON_TRACKING_ROOT
    # branch immediately below, which already used .get() truthiness.
    output_root_is_implicit_default = False
    if os.environ.get("TICKER_ADVISOR_OUTPUT_ROOT"):
        output_root = Path(os.environ["TICKER_ADVISOR_OUTPUT_ROOT"])
    elif os.environ.get("DCA_CRON_TRACKING_ROOT"):
        output_root = Path(dca_paths.stream_root("ticker_advisor_analysis"))
    else:
        output_root = REPO_ROOT / "analysis"
        output_root_is_implicit_default = True
    if args.output_dir is not None:
        output_dir = args.output_dir
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # --output-dir pointing at an existing FILE, or a directory the
            # process lacks permission to create, previously escaped this
            # CLI's normal "error: ..." exit path and terminated with a raw
            # traceback and exit code 1 instead (Codex review on PR #403:
            # "Convert output-directory creation failures into CLI
            # errors").
            print(f"error: could not create --output-dir {output_dir}: {exc}", file=sys.stderr)
            return 2
    else:
        try:
            allocated_dir = _allocate_default_output_dir(output_root, now)
        except OSError as exc:
            # The fully-implicit default (no TICKER_ADVISOR_OUTPUT_ROOT or
            # DCA_CRON_TRACKING_ROOT override) writes into the checkout's own
            # analysis/ tree so its reports are git-tracked, per
            # docs/ticker_advisory.md's documented layout -- but that
            # assumes the checkout itself is writable. On a read-only or
            # installed deployment that assumption fails and the command
            # previously aborted here even though a perfectly good writable
            # artifact root (dca_paths' shared, configurable tree, the same
            # one $DCA_CRON_TRACKING_ROOT already routes through above) was
            # available the whole time (Codex review on PR #429: "Route
            # default output through the shared artifact root"). Falling
            # back automatically here, rather than requiring the caller to
            # pass --output-dir/TICKER_ADVISOR_OUTPUT_ROOT by hand, is safe
            # ONLY because this is the implicit default; an explicit
            # override that fails to create is a real configuration error
            # the caller needs to see, not something to silently paper over.
            if not output_root_is_implicit_default:
                print(f"error: could not create a default output directory under {output_root}: {exc}", file=sys.stderr)
                return 2
            fallback_root = Path(dca_paths.stream_root("ticker_advisor_analysis"))
            print(
                f"warning: could not create the default output directory under {output_root} ({exc}); "
                f"falling back to {fallback_root}",
                file=sys.stderr,
            )
            try:
                allocated_dir = _allocate_default_output_dir(fallback_root, now)
            except OSError as fallback_exc:
                print(
                    f"error: could not create a default output directory under {fallback_root}: {fallback_exc}",
                    file=sys.stderr,
                )
                return 2
        if allocated_dir is None:
            print("error: could not allocate a unique output directory after 1000 attempts", file=sys.stderr)
            return 2
        output_dir = allocated_dir

    benchmark_returns: pd.Series | None = None
    benchmark_calendar: pd.DatetimeIndex | None = None
    benchmark_fallback_reason: str | None = None
    benchmark_calendar_gap_warning: str | None = None
    try:
        # now=now anchors the benchmark's future/current-date/staleness
        # cutoffs to the SAME single run instant every candidate below
        # also uses (Antigravity review on PR #403: "Anchor price cutoffs
        # to the run timestamp").
        benchmark_frame = fetch_price_history(
            args.benchmark, lookback_years=args.lookback_years, history_dir=args.history_dir, now=pd.Timestamp(now)
        )
        # fetch_price_history() attaches this via frame.attrs when it fell
        # back to a stale/incomplete local archive after a live refresh
        # also failed -- score_candidate() already lifts the same attr
        # into a candidate's own persisted `errors`, but this conversion
        # here discarded it for the BENCHMARK, so beta/alpha computed
        # against deficient benchmark history looked identical to a normal
        # run in scores.json once stderr was gone (Codex review on PR
        # #403: "Persist benchmark fallback warnings").
        benchmark_fallback_reason = benchmark_frame.attrs.get("stale_archive_fallback_reason")
        benchmark_returns = _price_returns(benchmark_frame)
        # The benchmark's own index, not a fixed weekday/business-day
        # range -- see score_candidate()'s own comment on why (Antigravity
        # review on PR #403: "Detect missing index sessions as
        # price-history gaps").
        benchmark_calendar = pd.DatetimeIndex(benchmark_frame.index)
        benchmark_calendar_gaps = _detect_benchmark_calendar_gaps(benchmark_calendar)
        if benchmark_calendar_gaps:
            gap_dates = [day.date().isoformat() for day in benchmark_calendar_gaps]
            shown = ", ".join(gap_dates[:10])
            suffix = "" if len(gap_dates) <= 10 else f" (+{len(gap_dates) - 10} more)"
            benchmark_calendar_gap_warning = (
                f"benchmark calendar was missing {len(gap_dates)} likely trading session(s) "
                f"({shown}{suffix}); added to the calendar used for every candidate's missing-session "
                "detection below, since a candidate missing the SAME session would otherwise have been "
                "reindexed onto an equally incomplete calendar and come out looking gap-free"
            )
            print(f"warning: {benchmark_calendar_gap_warning}", file=sys.stderr)
            # Warning alone leaves the exact loophole this check exists to
            # close: score_candidate() reindexes every candidate onto
            # `benchmark_calendar` restricted to that candidate's own
            # observed range, so a session missing from BOTH the benchmark
            # and a candidate stays invisible to that reindex unless the
            # gap date is actually IN the calendar. Unioning the detected
            # gaps in turns them into ordinary expected sessions for that
            # reindex -- a candidate that has a real price row for one of
            # these dates keeps it unaffected; a candidate that's ALSO
            # missing it now gets the same NaN-gap detection every other
            # missing session already receives (Codex review on PR #429:
            # "Stop scoring when the benchmark calendar has a gap"). No
            # benchmark PRICE data exists for these dates -- benchmark_
            # returns is intentionally left untouched -- only calendar
            # membership is being corrected here.
            benchmark_calendar = pd.DatetimeIndex(benchmark_calendar.union(pd.DatetimeIndex(benchmark_calendar_gaps)))
    except PriceHistoryUnavailable as exc:
        # Not just beta/alpha -- score_candidate()'s missing-session
        # detection also depends entirely on this same benchmark_calendar
        # (there's no other calendar source in this module); losing the
        # benchmark silently disables that check for every candidate's
        # health/backtest signals too (Codex review on PR #403: "Disable
        # gap-sensitive scoring without a session calendar"). Each affected
        # candidate's own `errors` list also records this -- see
        # score_candidate()'s benchmark_calendar is None branch -- so it's
        # visible in scores.json/scores.md too, not just here on stderr.
        print(
            f"warning: benchmark unavailable ({exc}); beta/alpha will be NaN and missing-session "
            "detection will be disabled for every candidate's health/backtest signals too",
            file=sys.stderr,
        )

    news_api_key = os.environ.get(args.news_api_key_env)
    analyzer: FinbertSentimentAnalyzer | None = None
    sentiment_unavailable_reason: str | None = None
    if not args.skip_sentiment:
        try:
            analyzer = FinbertSentimentAnalyzer()
        except Exception as exc:  # noqa: BLE001 -- missing torch/transformers must not abort the whole run
            sentiment_unavailable_reason = _sanitize_error_message(exc)
            print(
                f"warning: sentiment analyzer unavailable ({sentiment_unavailable_reason}); "
                "sentiment signal skipped for this run",
                file=sys.stderr,
            )
            if effective_weights["health"] == 0 and effective_weights["backtest"] == 0:
                # Sentiment was the only signal --weights left nonzero, and
                # it just failed to initialize -- the same no-weighted-
                # signal state the explicit --skip-sentiment guard above
                # already rejects, just discovered at runtime (a missing
                # torch/transformers install, a model download failure)
                # instead of from the CLI flags alone. Without this,
                # combine_scores() still accumulates weight_total=0.0 for
                # every candidate, and the command exits successfully
                # after writing a normal-looking all-WATCH scores.json/
                # scores.md (Codex review on PR #403: "Stop when the only
                # weighted signal fails to initialize").
                print(
                    f"error: sentiment analyzer unavailable ({sentiment_unavailable_reason}) and it's the only "
                    f"signal --weights leaves nonzero ({effective_weights}); nothing left to score with",
                    file=sys.stderr,
                )
                return 2

    raw_scores: dict[str, dict[str, object]] = {}
    for ticker in candidates:
        print(f"scoring {ticker}...", file=sys.stderr)
        raw_scores[ticker] = score_candidate(
            ticker,
            benchmark_returns=benchmark_returns,
            lookback_years=args.lookback_years,
            skip_sentiment=args.skip_sentiment,
            news_api_key=news_api_key,
            analyzer=analyzer,
            history_dir=args.history_dir,
            sentiment_unavailable_reason=sentiment_unavailable_reason,
            benchmark_calendar=benchmark_calendar,
            run_as_of=now,
        )

    # The --skip-sentiment / analyzer-init-failure guards above only catch
    # ONE way every weighted signal can end up unavailable -- a sentiment
    # analyzer that fails to CONSTRUCT. A health- or backtest-only run
    # where every candidate's signal fails at RUNTIME instead (vectorbt
    # missing, or every backtest_score()/strategy_health_score() call
    # raising for some systemic reason -- score_candidate()'s per-signal
    # exception guards catch each one individually, so nothing upstream
    # notices the aggregate) previously still exited 0 and published a
    # normal-looking, all-WATCH report with zero actual weighted evidence
    # behind it (Antigravity review on PR #403: "Fail when all
    # nonzero-weight signals are unavailable"). Checked against the RAW
    # per-signal values, not the composite -- a single-candidate run's
    # composite is expected to be None (combine_scores() can't z-score
    # without peers), which is a different, already-known limitation, not
    # "no evidence at all." Uses _signal_has_usable_metric(), the SAME
    # extraction helpers combine_scores() itself uses, not just a "the
    # container is non-None" check -- a health/backtest dict with every
    # numeric component NaN (e.g. a flat/constant-return history hitting
    # strategy_health_score()'s own zero-variance guard) would otherwise
    # count as "usable" here even though combine_scores() then excludes
    # every one of its components, reproducing the exact zero-evidence
    # report this check exists to catch (Antigravity review on PR #403:
    # "Check weighted metric usability, not container presence").
    #
    # For 2+ candidates, "some raw value exists somewhere" is still not
    # enough -- scoring._zscore() excludes a component for EVERY candidate
    # when fewer than 2 of them have a usable value for it. A health-only
    # two-candidate run where one candidate's price history fails entirely
    # leaves exactly one usable value per health component, so every health
    # sub-score ends up None for both candidates: the SAME zero-evidence,
    # all-WATCH/null-composite outcome this guard exists to catch, just
    # reached through a per-candidate data failure instead of a systemic
    # one (Codex review on PR #403: "Require weighted metrics to be
    # normalizable"). _signal_has_normalizable_metric() checks the actual
    # cross-candidate count _zscore() will apply, not just per-candidate
    # presence. This is deliberately skipped for a single-candidate run --
    # there, no component could EVER reach a count of 2 no matter what the
    # data looks like, so the check would always fail and turn the
    # intentionally-supported "single candidate scores WATCH with
    # composite: null" case (see README.md) into a hard error.
    nonzero_weight_signals = [signal for signal, weight in effective_weights.items() if weight > 0]
    if len(candidates) == 1:
        any_usable_weighted_signal = any(
            _signal_has_usable_metric(raw_scores[ticker], signal)
            for ticker in raw_scores
            for signal in nonzero_weight_signals
        )
    else:
        any_usable_weighted_signal = any(
            _signal_has_normalizable_metric(raw_scores, signal) for signal in nonzero_weight_signals
        )
    if not any_usable_weighted_signal:
        print(
            f"error: every nonzero-weight signal ({sorted(nonzero_weight_signals)}) was unavailable for "
            "every candidate; nothing usable to score with",
            file=sys.stderr,
        )
        return 2

    scores = combine_scores(raw_scores, weights=weights)

    # Persisted alongside the results, not just implied by the command that
    # produced them -- a saved ADD recommendation is otherwise indistinguishable
    # from one produced with different weights/benchmark/lookback once shell
    # history is gone, and can't be reproduced reliably (Codex review on PR
    # #403: "Persist scoring parameters with the advisory results").
    run_metadata = {
        "generated_at": now.isoformat(),
        "effective_weights": effective_weights,
        "benchmark": args.benchmark,
        "benchmark_fallback_reason": benchmark_fallback_reason,
        "benchmark_calendar_gap_warning": benchmark_calendar_gap_warning,
        "lookback_years": args.lookback_years,
        # --history-dir determines BOTH candidate and benchmark price
        # inputs and can materially change every health/backtest score --
        # without recording it, a saved run looks identical to one scored
        # against the default archive and can't be reproduced or audited
        # once the command line that produced it is gone (Codex review on
        # PR #403: "Record the price-history override in run metadata").
        # None means the default configured_ticker_history_dir() was used.
        # .resolve(), not the raw (possibly relative) CLI spelling -- a
        # relative --history-dir persisted verbatim can't identify its
        # price source once the run is inspected from a different working
        # directory or the invoking shell context is gone (Codex review on
        # PR #403: "Persist an absolute history-directory override").
        "history_dir_override": str(args.history_dir.resolve()) if args.history_dir is not None else None,
        # history_dir_override alone is None whenever --history-dir isn't
        # passed on the CLI, even when $DCA_TICKER_HISTORY_DIR redirects
        # fetch_price_history() to a DIFFERENT archive than the repo's
        # plain default -- indistinguishable in the saved metadata from a
        # run that genuinely used the ordinary default. Persisting the
        # actual EFFECTIVE directory (CLI override if given, else
        # whatever configured_ticker_history_dir() resolves to -- the
        # same function fetch_price_history() itself calls when no
        # explicit history_dir is passed) identifies which archive
        # supplied every candidate and benchmark price regardless of
        # which of the two selected it (Antigravity review on PR #403:
        # "Record the effective environment-selected history directory").
        "history_dir_effective": str(
            (args.history_dir if args.history_dir is not None else configured_ticker_history_dir()).resolve()
        ),
        "skip_sentiment": args.skip_sentiment,
        "sentiment_unavailable_reason": sentiment_unavailable_reason,
        # The same candidates and persisted run parameters could otherwise
        # produce different sentiment scores across runs -- FinbertSentiment
        # Analyzer resolves this once and reuses it for both its tokenizer
        # and model downloads; recording it here is the only artifact that
        # identifies which revision actually produced this run's scores
        # (Codex review on PR #403: "Pin and record the FinBERT revision").
        "finbert_revision": analyzer.revision if analyzer is not None else None,
        "candidates_requested": candidates_requested,
        "candidates_scored": candidates,
        "candidates_excluded_invalid_shape": invalid_shaped,
        "candidates_excluded_legacy_symbol": legacy_symbols,
        "candidates_excluded_already_active_or_excluded": ineligible_universe_symbols,
    }

    scores_payload = {
        "run_metadata": run_metadata,
        "candidates": {
            ticker: {
                "raw": score.raw,
                "sub_scores": score.sub_scores,
                "composite": score.composite,
                "classification": score.classification,
                "rationale": score.rationale,
            }
            for ticker, score in scores.items()
        },
    }

    scores_path = output_dir / "scores.json"
    markdown_path = output_dir / "scores.md"
    diff_path = output_dir / "proposed_tickers_diff.json"

    # as_of=now.date(), not render_proposed_diff()'s own date.today()
    # default: without it, a run near UTC midnight on a non-UTC host could
    # give the proposed diff's "generated_date" a different calendar date
    # than run_metadata.generated_at and the output directory it's written
    # into both already use (Codex review on PR #403: "Pass the run date
    # into the proposed diff").
    diff = render_proposed_diff(
        scores, as_of=now.date(), active_tickers=active_tickers, excluded_tickers=excluded_tickers
    )
    # render_proposed_diff()'s own skipped_* filters only see `scores`,
    # which -- across earlier rounds of this same review -- already
    # excludes invalid-shape, legacy-symbol, and already-active-or-
    # excluded candidates (all filtered upstream, BEFORE scoring). Its
    # skipped_* fields were therefore structurally always empty for this
    # call path even when candidates genuinely were filtered out; the
    # information only survived in run_metadata's separate
    # candidates_excluded_* keys below. Merging the pre-filtering buckets
    # in here keeps a consumer reading only proposed_tickers_diff.json's
    # own skipped_* fields (not run_metadata) from seeing an incomplete
    # picture (Antigravity review on PR #403: "Logic Disconnect:
    # render_proposed_diff Output Fields Are Always Empty"). Legacy
    # symbols have no matching bucket here (they're neither invalid-shape
    # nor active/excluded) and stay recorded only in run_metadata.
    diff["skipped_invalid_symbol_shape"] = list(diff["skipped_invalid_symbol_shape"]) + invalid_shaped
    diff["skipped_already_active"] = list(diff["skipped_already_active"]) + [
        t for t in ineligible_universe_symbols if t in active_tickers
    ]
    diff["skipped_explicitly_excluded"] = list(diff["skipped_explicitly_excluded"]) + [
        t for t in ineligible_universe_symbols if t in excluded_tickers
    ]
    diff["run_metadata"] = run_metadata

    # Every artifact's content is rendered BEFORE any of them is written --
    # a run whose diff/markdown rendering fails after scores.json already
    # landed previously left a new scores.json next to a stale/missing
    # sibling. Each write is then atomic (_write_atomic()), so a process
    # interrupted mid-run leaves either the old complete generation or the
    # new one, never a truncated file (Codex review on PR #403: "Publish
    # the report files without leaving mixed generations").
    scores_content = json.dumps(_json_safe(scores_payload), indent=2, default=str) + "\n"
    markdown_content = render_markdown(scores, generated_at=now, run_metadata=run_metadata)
    diff_content = json.dumps(_json_safe(diff), indent=2) + "\n"

    # The marker-removal + three-write + marker-write sequence below is
    # each individually atomic, but not exclusive ACROSS processes -- two
    # invocations sharing the same explicit --output-dir (the default
    # no---output-dir path already gets a directory allocated exclusively
    # per run via _allocate_default_output_dir(), so this only matters
    # for a deliberately-shared explicit path) could otherwise interleave
    # their writes, e.g. one process writes scores.json, the other writes
    # the remaining two files and recreates the marker -- leaving
    # .generation_complete beside a mix of two different runs' artifacts,
    # which no single-process fix can prevent (Antigravity review on PR
    # #403: "Serialize writers sharing an explicit output directory").
    # cross_process_lock() is the same POSIX-flock-with-portable-fallback
    # primitive scripts/run_ledger.py and scheduler_lease.py already use
    # for this exact class of problem -- a concurrent second writer waits
    # here rather than interleaving, and fails closed (TimeoutError) if
    # the first writer hangs rather than risking a double writer.
    with cross_process_lock(output_dir / ".lock"):
        # Removed BEFORE any of the three writes below, not just written
        # LAST on success -- an --output-dir reused from a COMPLETED
        # prior run already has this marker on disk from that earlier
        # generation. A crash between this run's three writes (each
        # individually atomic, but not committed as a SET) would
        # otherwise leave the stale marker from the OLD generation still
        # present, making a consumer trust a directory that's actually a
        # mix of the old markdown.md/diff.json and the new scores.json
        # (Antigravity review on PR #403: "Invalidate the old marker
        # before replacing artifacts"). missing_ok=True covers the
        # equally common case of a fresh --output-dir with no prior
        # marker at all.
        completion_marker_path = output_dir / ".generation_complete"
        completion_marker_path.unlink(missing_ok=True)

        _write_atomic(scores_path, scores_content)
        _write_atomic(markdown_path, markdown_content)
        _write_atomic(diff_path, diff_content)
        # A crash between the three writes above (each individually
        # atomic, but not committed as a SET) can still leave the
        # directory holding a mix of an old and a new generation when
        # --output-dir is reused across runs. This marker -- written
        # LAST, also atomically, only once all three siblings are
        # confirmed on disk -- lets a careful consumer treat "directory
        # lacks this file" as "do not trust this artifact set; it may be
        # an interrupted publish," covering the remaining exposure the
        # per-file atomic writes alone don't (Codex review on PR #403:
        # "Commit the artifact set as one generation").
        _write_atomic(completion_marker_path, now.isoformat() + "\n")

    print(f"wrote {scores_path}")
    print(f"wrote {markdown_path}")
    print(f"wrote {diff_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
