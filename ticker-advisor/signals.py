"""Three independent, pure(ish) scoring functions for a candidate ticker.

Each function is import-light at module scope (only ``pandas``/``numpy``,
already required by every part of this tool) and defers its heavier,
signal-specific dependency (``scipy``, ``vectorbt``,
``transformers``/``torch``) to the call inside the function that actually
needs it. That way ``import signals`` never fails just because one of the
three signals' dependency isn't installed -- only calling that specific
function does, with a clear ImportError rather than a mysterious collection
failure. Tests mirror this with ``pytest.importorskip`` per signal.

The math here is a fresh, tested reimplementation of the same approach used
by ``trading-strategies/strategy_monitor.py`` (risk-adjusted return metrics),
``trading-strategies/backtest.py`` (MA-crossover / RSI-reversion vectorbt
backtests), and ``trading-strategies/sentiment_monitor.py`` (FinBERT news/SEC
sentiment) -- not a cross-import, since that tree's module names aren't
importable Python identifiers and CLAUDE.md scopes it out as unmaintained
research tooling, not a base to build production tooling on.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime
from typing import Any, Callable

import numpy as np
import pandas as pd

TRADING_DAYS = 252
DEFAULT_RISK_FREE_RATE = 0.05
_ZERO_VARIANCE_TOLERANCE = 1e-12

_URL_PATTERN = re.compile(r"https?://\S+")


def _sanitize_fetch_error(exc: Exception) -> str:
    """Redact URLs from a fetch exception before it's persisted to disk.

    ``requests.raise_for_status()`` embeds the full request URL -- including
    NewsAPI's ``apiKey`` query parameter -- in its exception text. That
    message flows into candidate rationale and gets written to
    ``scores.json``/``scores.md``; persisting it unmodified would
    materialize the API key in repository-local artifacts that may get
    committed (Codex review on PR #403: "Redact the NewsAPI key from
    persisted request errors"), the same class of leak already guarded
    against for FinBERT initialization failures in advise_tickers.py.
    """
    return _URL_PATTERN.sub("<redacted-url>", str(exc))
# CAGR/Calmar annualize by raising a cumulative return to the power of
# TRADING_DAYS/n -- at n=5 that exponent is ~50, so a routine few-day move
# explodes into a distorting outlier (e.g. a 10% cumulative gain over 5 days
# implies a ~12,000% "CAGR"). 30 sessions (~6 weeks) keeps that exponent
# bounded to something a real annualization can survive (review finding:
# Antigravity code review on PR #403).
MIN_OBSERVATIONS = 30

_HEALTH_METRIC_KEYS = ("sharpe", "sortino", "max_drawdown", "cagr", "beta", "alpha", "calmar")


# ---------------------------------------------------------------------------
# Signal A: strategy-health metrics (risk-adjusted return profile)
# ---------------------------------------------------------------------------


def _sharpe(excess: pd.Series) -> float:
    std = excess.std(ddof=1)
    # An exact `not std` check is unreliable here for the same reason
    # _beta_alpha's benchmark-variance check is: pandas' std of a
    # bit-identical constant series can come back as a tiny nonzero float
    # (~1e-19) rather than a hard 0.0, from floating-point summation, not
    # genuine variance. Dividing by that near-zero epsilon produced an
    # absurd ~1e17 "Sharpe" for a flat/constant returns series instead of
    # the intended NaN (Copilot review on PR #403, surfaced via a test
    # using a constant return series that should have been flagged
    # zero-variance, not silently accepted as a huge positive Sharpe).
    if pd.isna(std) or std < _ZERO_VARIANCE_TOLERANCE:
        return float("nan")
    return (excess.mean() / std) * math.sqrt(TRADING_DAYS)


def _sortino(excess: pd.Series) -> float:
    downside = excess.clip(upper=0.0)
    downside_std = math.sqrt((downside**2).mean())
    # A tolerance threshold rather than exact `not downside_std`, matching
    # _sharpe/_beta_alpha's guards (Antigravity review on PR #403). Unlike
    # pandas' .std(), clip-to-a-literal-then-square-then-mean doesn't
    # exhibit the same floating-point epsilon-instead-of-exact-zero
    # behavior in practice (verified empirically), but the threshold costs
    # nothing and keeps all three variance guards consistent.
    if pd.isna(downside_std) or downside_std < _ZERO_VARIANCE_TOLERANCE:
        return float("nan")
    return (excess.mean() / downside_std) * math.sqrt(TRADING_DAYS)


def _max_drawdown(returns: pd.Series) -> float:
    cumulative = (1.0 + returns).cumprod()
    # Anchor the running peak at the starting equity (1.0, before any
    # returns are applied). Without this, when the very first return is a
    # loss, cumulative's first value is already below 1.0 and cummax() takes
    # THAT reduced value as its own peak -- so the first day's loss never
    # shows up in the drawdown at all (a one-period -10% series reported as
    # 0% drawdown). Every series where cumulative hasn't yet recovered above
    # 1.0 understates its drawdown the same way (Codex review on PR #403:
    # "Include the initial capital in drawdown calculation").
    running_max = cumulative.cummax().clip(lower=1.0)
    drawdown = cumulative / running_max - 1.0
    return float(drawdown.min())


def _cagr(returns: pd.Series) -> float:
    cumulative = (1.0 + returns).cumprod()
    total = cumulative.iloc[-1]
    n = len(returns)
    if total <= 0 or n == 0:
        return float("nan")
    return float(total ** (TRADING_DAYS / n) - 1.0)


def _calmar(cagr: float, max_drawdown: float) -> float:
    if not max_drawdown or pd.isna(max_drawdown) or pd.isna(cagr):
        return float("nan")
    return cagr / abs(max_drawdown)


def _beta_alpha(returns: pd.Series, benchmark: pd.Series, rf_daily: float) -> tuple[float, float]:
    from scipy import stats

    aligned = pd.concat([returns, benchmark], axis=1, join="inner").dropna()
    if len(aligned) < MIN_OBSERVATIONS:
        return float("nan"), float("nan")
    ret_col = aligned.iloc[:, 0]
    bm_col = aligned.iloc[:, 1]
    # A flat/near-constant benchmark window (zero variance) has no slope to
    # fit. Checking variance explicitly up front -- rather than relying on
    # scipy to raise -- is deliberate: across scipy versions linregress's
    # behavior on a degenerate input isn't consistent (some raise ValueError,
    # others return a slope of 0.0 with a precision-loss warning instead), so
    # a version-dependent try/except would make this function's behavior
    # depend on whatever scipy happens to be installed (Codex review on PR
    # #403, P1: "Detect zero-variance benchmarks before calling linregress").
    # An exact `== 0` check is not reliable here: pandas' std of a genuinely
    # constant series (e.g. the same float literal repeated 300 times) can
    # still come back as a tiny nonzero float (~1e-19) from floating-point
    # summation, not a hard zero -- confirmed empirically, not a hypothetical.
    if bm_col.std(ddof=0) < _ZERO_VARIANCE_TOLERANCE:
        return float("nan"), float("nan")
    regression = stats.linregress(bm_col, ret_col)
    slope = float(regression.slope)
    alpha_annualized = (ret_col.mean() - rf_daily - slope * (bm_col.mean() - rf_daily)) * TRADING_DAYS
    return slope, alpha_annualized


def strategy_health_score(
    returns: pd.Series,
    benchmark_returns: pd.Series | None = None,
    *,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
) -> dict[str, float]:
    """Risk-adjusted return metrics for a single daily-returns series.

    Returns an all-NaN dict (never raises) when there are fewer than
    ``MIN_OBSERVATIONS`` non-null observations -- a thin candidate history
    should read as "not enough data," not crash the whole batch. ``beta``
    and ``alpha`` stay NaN unless ``benchmark_returns`` is supplied.
    """
    metrics: dict[str, float] = dict.fromkeys(_HEALTH_METRIC_KEYS, float("nan"))
    clean_returns = returns.dropna()
    if len(clean_returns) < MIN_OBSERVATIONS:
        return metrics

    rf_daily = risk_free_rate / TRADING_DAYS
    excess = clean_returns - rf_daily
    metrics["sharpe"] = _sharpe(excess)
    metrics["sortino"] = _sortino(excess)
    metrics["max_drawdown"] = _max_drawdown(clean_returns)
    metrics["cagr"] = _cagr(clean_returns)
    metrics["calmar"] = _calmar(metrics["cagr"], metrics["max_drawdown"])
    if benchmark_returns is not None:
        metrics["beta"], metrics["alpha"] = _beta_alpha(clean_returns, benchmark_returns.dropna(), rf_daily)
    return metrics


# ---------------------------------------------------------------------------
# Signal B: backtested strategy performance (MA-crossover + RSI-reversion)
# ---------------------------------------------------------------------------


def generate_ma_crossover_signals(
    close: pd.Series, fast_window: int = 50, slow_window: int = 200
) -> tuple[pd.Series, pd.Series]:
    """Golden/death-cross entries and exits, boolean series aligned to ``close``."""
    fast = close.rolling(fast_window).mean()
    slow = close.rolling(slow_window).mean()
    entries = (fast > slow) & (fast.shift(1) <= slow.shift(1))
    exits = (fast < slow) & (fast.shift(1) >= slow.shift(1))
    return entries.fillna(False), exits.fillna(False)


def _wilder_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Standard Wilder RSI, hand-rolled to avoid a pandas_ta dependency.

    (pandas_ta's current PyPI releases require Python >=3.12, an
    unnecessary version constraint to take on for a ~10-line, well-known
    formula that trading-strategies/backtest.py otherwise pulls the whole
    library in for.)
    """
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)

    # Wilder's own recurrence seeds its FIRST smoothed average with the
    # plain mean of the initial `period` gains/losses, then applies the
    # 1/period exponential update from there. ewm(adjust=False) instead
    # starts its hidden recurrence from the very first delta and merely
    # suppresses output until min_periods is reached -- for a series with
    # mixed early gains/losses that produces RSI values several points off
    # from the standard formula near the start of the series, which can
    # shift 30/70 threshold crossings and change the RSI backtest Sharpe fed
    # into candidate rankings (Codex review on PR #403: "Seed Wilder
    # averages with the initial period mean").
    avg_gain = np.full(len(close), np.nan)
    avg_loss = np.full(len(close), np.nan)
    if len(close) > period:
        gain_values = gain.to_numpy()
        loss_values = loss.to_numpy()
        avg_gain[period] = np.nanmean(gain_values[1 : period + 1])
        avg_loss[period] = np.nanmean(loss_values[1 : period + 1])
        for i in range(period + 1, len(close)):
            avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gain_values[i]) / period
            avg_loss[i] = (avg_loss[i - 1] * (period - 1) + loss_values[i]) / period
    avg_gain = pd.Series(avg_gain, index=close.index)
    avg_loss = pd.Series(avg_loss, index=close.index)

    relative_strength = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + relative_strength))
    rsi = rsi.where(avg_loss != 0, 100.0)
    # A fully flat window (no gains AND no losses) has no relative strength
    # at all -- 100.0 above is only correct for "gains with zero losses"
    # (strictly rising), not "nothing moved." Treat a flat window as neutral
    # rather than falsely overbought, or generate_rsi_reversion_signals would
    # fire spurious exit signals on a static price (Antigravity code review
    # on PR #403).
    return rsi.where((avg_gain != 0) | (avg_loss != 0), 50.0)


def generate_rsi_reversion_signals(
    close: pd.Series, rsi_period: int = 14, rsi_low: float = 30.0, rsi_high: float = 70.0
) -> tuple[pd.Series, pd.Series]:
    """Recovery-entry / overbought-exit signals from a Wilder RSI, boolean series.

    Entry fires on the bounce OUT of oversold (RSI crosses back above
    rsi_low), not on the dip INTO oversold -- entering on the dip buys
    before any evidence of a reversal, which changes the holding period and
    the resulting Sharpe this signal feeds into candidate rankings. Matches
    trading-strategies/backtest.py's generate_rsi_reversion_signals, the
    reference this function's docstring (module-level) says it reimplements
    (Codex review on PR #403: "Enter RSI positions on the recovery cross").
    """
    rsi = _wilder_rsi(close, rsi_period)
    entries = (rsi >= rsi_low) & (rsi.shift(1) < rsi_low)
    exits = (rsi >= rsi_high) & (rsi.shift(1) < rsi_high)
    return entries.fillna(False), exits.fillna(False)


def _portfolio_metrics(
    close: pd.Series,
    entries: pd.Series,
    exits: pd.Series,
    *,
    commission: float,
    slippage: float,
    init_cash: float,
) -> dict[str, float]:
    import importlib

    vbt = importlib.import_module("vectorbt")

    # entries/exits are computed from day T's close (a crossover or RSI
    # threshold that's only knowable once that close prints) -- pairing them
    # with THAT SAME day's close in Portfolio.from_signals fills the trade at
    # a price that had already closed by the time the signal existed, which
    # isn't realizable and inflates both strategies' Sharpe ratios that feed
    # candidate rankings. Shifting by one bar means a signal detected on day
    # T executes at day T+1's close instead -- the next fill actually
    # achievable after the signal is known (Codex review on PR #403:
    # "Execute close-derived signals on the following bar").
    # On pandas >=3.0, shift() on a boolean series produces object dtype and
    # fillna(False) no longer downcasts it back -- vectorbt's Numba-jitted
    # from_signals() then chokes on object arrays with an opaque
    # TypingError, which score_candidate()'s broad except swallows into a
    # silently-missing backtest signal for every candidate on a fresh
    # install (Codex review on PR #403: "Cast shifted signals back to
    # boolean"). Explicit astype(bool) keeps the dtype correct regardless of
    # pandas' downcast behavior.
    entries = entries.shift(1).fillna(False).astype(bool)
    exits = exits.shift(1).fillna(False).astype(bool)

    portfolio = vbt.Portfolio.from_signals(
        close, entries, exits, fees=commission, slippage=slippage, init_cash=init_cash, freq="1D"
    )

    def _safe(getter: Callable[[], Any]) -> float:
        # vectorbt's convenience metrics raise/warn on a portfolio with zero
        # trades rather than returning NaN outright, and the exact set of
        # available stat keys/methods has shifted across releases -- degrade
        # any single metric to NaN instead of failing the whole candidate.
        try:
            value = getter()
            return float(value) if value is not None else float("nan")
        except Exception:
            return float("nan")

    total_trades = _safe(lambda: portfolio.trades.count())
    # bool(float("nan")) is True in Python -- a plain `if total_trades`
    # would treat a NaN'd trade count (the _safe fallback when .count()
    # itself failed) as "yes, call win_rate() too," instead of skipping it
    # the same way the failed count was skipped (Antigravity review on PR
    # #403: "Inaccurate Truthiness Check for total_trades").
    has_trades = not math.isnan(total_trades) and total_trades > 0
    return {
        "sharpe": _safe(portfolio.sharpe_ratio),
        "sortino": _safe(portfolio.sortino_ratio),
        "max_drawdown": _safe(portfolio.max_drawdown),
        "calmar": _safe(portfolio.calmar_ratio),
        "total_return": _safe(portfolio.total_return),
        "win_rate": _safe(lambda: portfolio.trades.win_rate()) if has_trades else float("nan"),
        "total_trades": total_trades,
    }


def backtest_score(
    close: pd.Series,
    *,
    fast_window: int = 50,
    slow_window: int = 200,
    rsi_period: int = 14,
    rsi_low: float = 30.0,
    rsi_high: float = 70.0,
    commission: float = 0.001,
    slippage: float = 0.0005,
    init_cash: float = 10_000.0,
) -> dict[str, dict[str, float]]:
    """Backtested MA-crossover and RSI-reversion performance for ``close``.

    Returns ``{"ma_crossover": {...}, "rsi_reversion": {...}}``; each inner
    dict is whatever ``_portfolio_metrics`` produced (all-NaN on failure, so
    one candidate's data quirk never aborts the batch).
    """
    ma_entries, ma_exits = generate_ma_crossover_signals(close, fast_window, slow_window)
    rsi_entries, rsi_exits = generate_rsi_reversion_signals(close, rsi_period, rsi_low, rsi_high)
    return {
        "ma_crossover": _portfolio_metrics(
            close, ma_entries, ma_exits, commission=commission, slippage=slippage, init_cash=init_cash
        ),
        "rsi_reversion": _portfolio_metrics(
            close, rsi_entries, rsi_exits, commission=commission, slippage=slippage, init_cash=init_cash
        ),
    }


# ---------------------------------------------------------------------------
# Signal C: news / SEC-filing sentiment (FinBERT)
# ---------------------------------------------------------------------------

SEC_EDGAR_FULL_TEXT_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
NEWS_API_URL = "https://newsapi.org/v2/everything"

# SEC EDGAR requires a contact-identifying User-Agent (its documented format
# is "Sample Company Name AdminContact@domain.com") and can 403 requests that
# don't provide one -- see https://www.sec.gov/os/webmaster-faq#developers.
# Overridable via env var rather than hardcoding a real address in source;
# operators relying on the SEC signal in production should set their own
# contact (Codex review on PR #403).
_DEFAULT_SEC_EDGAR_USER_AGENT = (
    "all-stock-dca ticker-advisor (set SEC_EDGAR_USER_AGENT to "
    "'Your Org contact@your-domain.example' for reliable access)"
)

_PARENTHESIZED_PATTERN = re.compile(r"\(([^)]*)\)")

# SEC's fair-access policy asks for no more than ~10 requests/second
# (https://www.sec.gov/developer). advise_tickers.main() scores candidates
# in a plain sequential loop -- no concurrency to bound -- but a large
# batch scored from local archives (fast, no network wait per candidate)
# can still issue EDGAR requests faster than that ceiling, causing later
# candidates in the batch to 403 and lose their SEC sentiment purely based
# on input order. The repo's existing async SEC collector
# (trading-strategies/monday-sentiment/sentiment_cron.py's SecRateLimiter)
# enforces the same ~8 req/s headroom for the same reason; this is its
# synchronous equivalent, shared across every fetch_sec_filings() call in
# the process rather than relying on incidental per-candidate processing
# latency (Codex review on PR #403: "Throttle SEC requests across the
# candidate batch").
SEC_MAX_REQUESTS_PER_SECOND = 8.0

# EFTS's search-index hits are search-relevance metadata, not filing
# content -- a normal (non-highlighted) query gets no "_text" field at all,
# so requiring one to be present discarded nearly every otherwise-valid
# issuer hit and silently starved the SEC-only sentiment fallback (Codex
# review on PR #429: "Fetch SEC filing content before requiring _text").
# When "_text" is absent, fetch the actual filing document instead and
# extract a bounded, tag-stripped snippet from it -- capped well short of
# a full 10-K (often hundreds of KB) since only a representative excerpt is
# needed for FinBERT sentiment, not the complete filing.
_HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
_WHITESPACE_RUN_PATTERN = re.compile(r"\s+")
_MAX_FILING_SNIPPET_CHARS = 4000
# Modern filings are inline-XBRL: a <head> full of CSS/metadata and an
# <ix:header> block of hidden tagged facts routinely precede the visible
# narrative, and a naive strip-tags-then-truncate can consume the entire
# _MAX_FILING_SNIPPET_CHARS budget on that boilerplate before reaching any
# actual prose -- FinBERT would then score filing metadata, not filing
# CONTENT, the same "score CONTENT, not metadata" line this module already
# draws elsewhere (Codex review on PR #429: "Extract visible filing prose
# before truncating"). DOTALL so ".*?" spans the (frequently multi-line)
# block content; non-greedy so a document with more than one such block
# doesn't collapse everything between the first open tag and the last close
# tag of the same name into one match.
_HTML_HIDDEN_BLOCK_PATTERN = re.compile(
    r"<(script|style|head|ix:header)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL
)
_HTML_BODY_PATTERN = re.compile(r"<body\b[^>]*>(.*)</body\s*>", re.IGNORECASE | re.DOTALL)


class _SecRateLimiter:
    """Enforce a minimum spacing between successive SEC EDGAR requests."""

    def __init__(self, max_per_second: float) -> None:
        self._min_interval = 1.0 / max_per_second
        self._next_allowed_at = 0.0

    def acquire(self) -> None:
        import time

        now = time.monotonic()
        wait = self._next_allowed_at - now
        if wait > 0:
            time.sleep(wait)
            now = time.monotonic()
        self._next_allowed_at = max(now, self._next_allowed_at) + self._min_interval


_sec_rate_limiter = _SecRateLimiter(SEC_MAX_REQUESTS_PER_SECOND)


def _parenthesized_tickers(display_name: str) -> set[str]:
    """Extract the ticker symbol(s) from an EDGAR display_names entry.

    EFTS formats these as "COMPANY NAME (TICKER)" for a single-class
    issuer, but a multi-class one is "COMPANY NAME (TICKER1, TICKER2)" --
    a bare substring check for "(TICKER1)" never matches inside "(TICKER1,
    TICKER2)", incorrectly discarding that issuer's legitimate filings for
    every share class (Codex review on PR #403: "Parse ticker lists in SEC
    display names").
    """
    match = _PARENTHESIZED_PATTERN.search(display_name)
    if not match:
        return set()
    return {symbol.strip().upper() for symbol in match.group(1).split(",") if symbol.strip()}


def _sec_filing_document_url(hit: dict, source: dict) -> str | None:
    """Build the URL of the actual filing document an EFTS hit refers to.

    EFTS's own "_id" field for a hit is formatted "{accession-no}:{primary-
    document-filename}" (e.g. "0001193125-26-012345:aapl-20260101.htm"),
    and "_source.ciks" is a list of zero-padded CIK strings -- both
    verifiable only against EFTS's documented schema, since this dev
    environment has no network egress to sec.gov to confirm against a live
    response (same caveat as this module's other SEC-schema assumptions).
    Returns None -- letting the caller fall back to excluding the hit,
    exactly as before this fetch existed -- for any hit that doesn't have
    both pieces in the expected shape, rather than guessing.
    """
    hit_id = hit.get("_id")
    if not isinstance(hit_id, str) or ":" not in hit_id:
        return None
    accession_no, _, filename = hit_id.partition(":")
    accession_no = accession_no.strip()
    filename = filename.strip()
    if not accession_no or not filename:
        return None
    ciks = source.get("ciks")
    if not isinstance(ciks, list) or not ciks:
        return None
    cik = ciks[0]
    if not isinstance(cik, str) or not cik.strip().lstrip("0"):
        return None
    try:
        cik_number = int(cik)
    except ValueError:
        return None
    accession_no_nodash = accession_no.replace("-", "")
    if not accession_no_nodash.isdigit():
        return None
    return f"https://www.sec.gov/Archives/edgar/data/{cik_number}/{accession_no_nodash}/{filename}"


def _extract_filing_snippet(raw_document: object) -> str | None:
    """Reduce a fetched filing document to a bounded, sentiment-scorable text snippet.

    Filing documents are HTML (occasionally plain text) and can run to
    hundreds of KB for a 10-K -- far more than FinBERT's tokenizer needs or
    a sentiment excerpt should require. Strips markup, collapses
    whitespace, and caps the result well short of the full document.
    Returns None for a document that reduces to nothing usable (a blank
    document, or one that was entirely markup with no text content).
    """
    if not isinstance(raw_document, str):
        return None
    # Drop <script>/<style>/<head>/<ix:header> blocks (content included, not
    # just the tags) before anything else -- otherwise their content would
    # still be present as plain text after generic tag-stripping below, and
    # could consume the truncation budget before any visible prose is
    # reached (see the module-level comment on _HTML_HIDDEN_BLOCK_PATTERN).
    without_hidden_blocks = _HTML_HIDDEN_BLOCK_PATTERN.sub(" ", raw_document)
    # Prefer the <body> content specifically when present -- narrows to the
    # visible document even when boilerplate outside <body> (a <head> this
    # particular document didn't close properly, XML/DOCTYPE preamble)
    # wasn't already caught above. Falls back to the whole (already-
    # cleaned) document for a plain-text filing or a fragment with no
    # <body> tag at all.
    body_match = _HTML_BODY_PATTERN.search(without_hidden_blocks)
    visible_html = body_match.group(1) if body_match else without_hidden_blocks
    without_tags = _HTML_TAG_PATTERN.sub(" ", visible_html)
    collapsed = _WHITESPACE_RUN_PATTERN.sub(" ", without_tags).strip()
    if not collapsed:
        return None
    return collapsed[:_MAX_FILING_SNIPPET_CHARS]


def fetch_sec_filings(
    ticker: str,
    *,
    limit: int = 3,
    days: int | None = None,
    timeout: float = 10.0,
    fetcher: Callable[[], Any] | None = None,
    document_fetcher: Callable[[str], str] | None = None,
    errors: list[str] | None = None,
    as_of: date | None = None,
) -> list[dict[str, str]]:
    """Recent SEC EDGAR full-text-search hits for ``ticker``, as ``{"text": ...}`` items.

    No API key required. ``fetcher`` is an injectable ``() -> dict`` (the raw
    EDGAR JSON payload) for tests -- production code never has to mock
    ``requests`` directly, matching this repo's injected-callable convention
    (see ``dca_live_adapters.py``). A network failure degrades to an empty
    list rather than raising, since one candidate's SEC lookup failing
    should not abort the whole advisory run.

    ``days`` scopes results to the trailing N days via EFTS's ``startdt``/
    ``enddt`` params. Without it, EDGAR's relevance ranking can surface
    arbitrarily old filings, which would then be scored as "recent" sentiment
    alongside NewsAPI's genuinely day-bounded headlines (Codex review on PR
    #403: "Apply the sentiment lookback to SEC filing searches").

    ``as_of`` anchors the trailing window to a caller-supplied date instead
    of recomputing ``datetime.now(timezone.utc).date()`` fresh on every call
    -- a multi-candidate run spanning UTC midnight (plausible with a cold
    multi-GB FinBERT download or a large batch) would otherwise give
    candidates before and after midnight different windows depending purely
    on scoring order, even though the persisted run_metadata.generated_at
    stays fixed at the run's single earlier timestamp (Antigravity review on
    PR #403: "Anchor sentiment lookbacks to the run timestamp"). Defaults to
    ``None`` (falls back to "now", the previous behavior) so direct callers
    that don't care about this still get the current date.

    ``errors``, if given, gets a message appended when the fetch ITSELF
    fails (a 403, a timeout, malformed JSON) -- distinct from the request
    succeeding and simply finding nothing, which stays a silent ``[]`` as
    before. Without this, a genuine SEC outage was indistinguishable from
    "no filings" in sentiment_score()'s result, silently degrading the
    composite with no explanation in scores.md (Codex review on PR #403:
    "Preserve SEC request failures in the report").

    ``document_fetcher`` is an injectable ``(url) -> raw document text``
    for tests, used to retrieve the actual filing when a hit has no
    "_text" snippet in the search-index response itself -- the normal case
    for a non-highlighted EFTS query. Without it, every such hit was
    discarded outright even though it was a genuine issuer match, silently
    starving the SEC-only sentiment fallback for users without a NewsAPI
    key (Codex review on PR #429: "Fetch SEC filing content before
    requiring _text"). A per-hit document fetch failure (403, timeout,
    unparseable response) excludes just that hit -- matching this
    function's existing "degrade, don't raise" contract -- rather than
    aborting the whole candidate's SEC signal.
    """
    if fetcher is None:
        def fetcher() -> dict:
            import os
            from datetime import datetime, timedelta, timezone

            import requests

            headers = {"User-Agent": os.environ.get("SEC_EDGAR_USER_AGENT", _DEFAULT_SEC_EDGAR_USER_AGENT)}
            params = {"q": ticker, "forms": "10-K,10-Q,8-K"}
            if days is not None:
                today = as_of if as_of is not None else datetime.now(timezone.utc).date()
                # EFTS ignores startdt/enddt unless dateRange=custom is also
                # set -- both existing SEC collectors in this repo
                # (trading-strategies/sentiment_monitor.py,
                # trading-strategies/monday-sentiment/sentiment_cron.py) set
                # it alongside the date fields; without it EDGAR still
                # returns relevance-ranked results across all time (Codex
                # review on PR #403: "Activate the SEC custom date range").
                params["dateRange"] = "custom"
                params["startdt"] = (today - timedelta(days=days)).isoformat()
                params["enddt"] = today.isoformat()
            _sec_rate_limiter.acquire()
            response = requests.get(SEC_EDGAR_FULL_TEXT_SEARCH_URL, params=params, headers=headers, timeout=timeout)
            response.raise_for_status()
            return response.json()

    if document_fetcher is None:
        def document_fetcher(url: str) -> str:
            import os

            import requests

            headers = {"User-Agent": os.environ.get("SEC_EDGAR_USER_AGENT", _DEFAULT_SEC_EDGAR_USER_AGENT)}
            _sec_rate_limiter.acquire()
            response = requests.get(url, headers=headers, timeout=timeout)
            response.raise_for_status()
            return response.text

    try:
        payload = fetcher()
    except Exception as exc:  # noqa: BLE001 -- one candidate's SEC lookup failing must not abort the batch
        if errors is not None:
            errors.append(f"SEC EDGAR fetch failed for {ticker}: {_sanitize_fetch_error(exc)}")
        return []

    if not isinstance(payload, dict):
        # A response that parses as valid JSON but isn't a mapping at the
        # top level (null, a bare list, ...) would otherwise raise
        # AttributeError on `.get("hits")` below, OUTSIDE the fetch
        # exception handler above -- and since sentiment_score() fetches
        # NewsAPI first and calls this function second, that uncaught error
        # would discard otherwise-available NEWS sentiment too, the same
        # class of failure already guarded against on the NewsAPI path
        # (Codex review on PR #403: "Validate the top-level SEC payload").
        if errors is not None:
            errors.append(f"SEC EDGAR fetch failed for {ticker}: unexpected response type {type(payload).__name__}")
        return []

    # `payload.get("hits", {})` only supplies the default when the key is
    # MISSING -- an explicit `{"hits": null}` response makes it return None,
    # and `.get("hits", [])` on None raises AttributeError, escaping this
    # function's own "degrades to [] rather than raising" contract
    # (Antigravity review on PR #403).
    # `limit` must be applied AFTER issuer filtering, not before: slicing to
    # the first N raw hits first (as an earlier version of this function
    # did) can discard exactly the hits that would fail the issuer check
    # while leaving a genuinely relevant filing further down the raw
    # (relevance-ranked, not issuer-ranked) result list unexamined --
    # incorrectly starving the sentiment signal of real data for a
    # common-word ticker (Codex review on PR #403: "Filter issuer hits
    # before enforcing the filing limit").
    # The top-level-payload check above only guards `payload` itself -- the
    # nested "hits" containers can independently be a malformed shape
    # (`{"hits": "error"}`, `{"hits": {"hits": [null]}}`, a hit whose
    # "_source" is null, a non-list "display_names"), any of which raises
    # AttributeError/TypeError below, OUTSIDE the fetch exception handler
    # above, and discards otherwise-valid NEWS sentiment too since
    # sentiment_score() fetches SEC after NewsAPI (Codex review on PR #403:
    # "Validate nested SEC hit structures before dereferencing").
    hits_container = payload.get("hits")
    hits = (hits_container.get("hits") if isinstance(hits_container, dict) else None) or []
    if not isinstance(hits, list):
        hits = []
    filings = []
    for hit in hits:
        if len(filings) >= limit:
            break
        if not isinstance(hit, dict):
            continue
        source = hit.get("_source")
        if not isinstance(source, dict):
            source = {}
        display_names = source.get("display_names") or []
        if not isinstance(display_names, list):
            display_names = []
        display_names = [name for name in display_names if isinstance(name, str)]
        # EFTS's `q` is a bare full-text search -- a common-word ticker can
        # match filings from an unrelated registrant that merely mentions
        # the term. Real EDGAR display_names are formatted "COMPANY NAME
        # (TICKER)", so require the ticker to actually appear there before
        # accepting a hit; when display_names is absent entirely there's no
        # basis to validate OR reject, so it's still accepted (Codex review
        # on PR #403: "Scope SEC searches to the candidate issuer"). A
        # multi-class issuer's parenthesized field is a comma-separated
        # list ("Alphabet Inc. (GOOG, GOOGL)"), not a single ticker -- a
        # bare substring check for "(GOOG)" never matches inside "(GOOG,
        # GOOGL)", incorrectly discarding that issuer's legitimate filings
        # for BOTH share classes (Codex review on PR #403: "Parse ticker
        # lists in SEC display names").
        display_name_tickers: set[str] = set()
        for name in display_names:
            display_name_tickers.update(_parenthesized_tickers(name))
        # config/tickers.json uses the dotted share-class spelling
        # (BRK.B); EDGAR's parenthetical uses the hyphenated form (BRK-B)
        # -- the same provider-spelling mismatch _load_from_yfinance()
        # already translates at its own boundary. Without normalizing
        # here too, a dotted candidate's legitimate filing under the
        # hyphenated spelling fails this exact-membership check, silently
        # discarding its SEC signal (and, without a NewsAPI key, the whole
        # sentiment signal) (Codex review on PR #403: "Normalize
        # share-class symbols before SEC issuer matching").
        normalized_display_tickers = {symbol.replace("-", ".") for symbol in display_name_tickers}
        # Gating on `normalized_display_tickers` (whether any ticker could
        # be PARSED out) rather than on `display_names` (whether EFTS
        # PROVIDED any names at all) let a hit through unvalidated whenever
        # none of the provided names happened to contain a parenthesized
        # ticker -- indistinguishable from the genuinely-no-basis-to-
        # validate case this fallback exists for. For a common-word ticker
        # like "COIN" or "SHOP", an unrelated issuer that merely mentions
        # the query and supplies a tickerless display name would feed its
        # text into FinBERT and alter the composite (Codex review on PR
        # #403: "Reject SEC hits whose provided names contain no
        # ticker").
        #
        # An entirely ABSENT `display_names` field used to be treated as
        # "no basis to validate, so accept" -- but this is a bare
        # full-text search (`q=ticker`), not an issuer-scoped one, so an
        # unrelated filer whose hit simply omits display_names can match
        # on a common-word symbol ("COIN", "SHOP", "CAT") and feed
        # unrelated text into FinBERT under the candidate's name. There is
        # no longer a case where silence is treated as acceptance --
        # every hit needs a PARSEABLE, matching ticker to be counted,
        # whether display_names was malformed, absent, or simply lacked
        # one (Antigravity review on PR #403: "Reject SEC hits without
        # issuer identity").
        if ticker.upper().replace("-", ".") not in normalized_display_tickers:
            continue
        # EFTS's actual field name for the form type, and whether a filing
        # snippet is exposed at all under "_text" vs. only in "_source", is
        # not verifiable from this dev environment (no network egress to
        # sec.gov). Accept either naming so real responses degrade to
        # company-name-only text rather than silently going empty if the
        # schema differs from what's assumed here; verify against a live
        # query before depending on filing-content nuance in production
        # (Codex review on PR #403: "Use filing text for the SEC sentiment
        # signal").
        # A truthy but non-string "form_type"/"_text" (a nested object, a
        # number -- EFTS's real schema for these leaf fields isn't
        # verifiable from this dev environment) would otherwise either
        # store a non-string `text` that _dedupe_items() later calls
        # .strip() on (AttributeError) or get passed to " ".join() below
        # (TypeError) -- either way escaping this function's own "degrade,
        # don't raise" contract and, since sentiment_score() has already
        # fetched NewsAPI by this point, discarding otherwise-valid news
        # sentiment too (Codex review on PR #403: "Validate SEC text
        # fields before combining sentiment").
        raw_form_type = source.get("form_type") or source.get("file_type")
        form_type = raw_form_type if isinstance(raw_form_type, str) else ""
        raw_snippet = hit.get("_text") or source.get("_text")
        snippet = raw_snippet if isinstance(raw_snippet, str) else None
        # A normal (non-highlighted) EFTS query returns NO "_text" field at
        # all -- it's only populated when the request explicitly asks for
        # highlighting, which this module's search request does not.
        # Requiring "_text" therefore discarded nearly every otherwise-
        # valid issuer hit, and with no NewsAPI key configured that meant
        # NO sentiment signal at all rather than a degraded one (Codex
        # review on PR #429: "Fetch SEC filing content before requiring
        # _text"). Fetch the actual filing document instead of giving up --
        # still real filing CONTENT, not fabricated metadata like the
        # previous "COMPANY NAME (TICKER) 10-K" fallback this module
        # already rejected once (Antigravity review on PR #403: "Do not
        # score filing metadata as filing sentiment"). A fetch failure (no
        # URL derivable from the hit, a 403/timeout, an empty document)
        # excludes just this hit, matching the prior behavior exactly.
        if snippet is None:
            document_url = _sec_filing_document_url(hit, source)
            if document_url is not None:
                try:
                    raw_document = document_fetcher(document_url)
                except Exception as exc:  # noqa: BLE001 -- one hit's document fetch must not abort the batch
                    if errors is not None:
                        errors.append(
                            f"SEC filing document fetch failed for {ticker}: {_sanitize_fetch_error(exc)}"
                        )
                    raw_document = None
                if raw_document is not None:
                    snippet = _extract_filing_snippet(raw_document)
        if snippet:
            filings.append({"ticker": ticker, "text": snippet, "form": form_type})
    return filings


def fetch_news_headlines(
    ticker: str,
    *,
    news_api_key: str | None,
    days: int = 7,
    page_size: int = 20,
    timeout: float = 10.0,
    fetcher: Callable[[], Any] | None = None,
    errors: list[str] | None = None,
    as_of: datetime | None = None,
) -> list[dict[str, str]]:
    """Recent NewsAPI headlines for ``ticker``, as ``{"text": ...}`` items.

    Silently returns ``[]`` when ``news_api_key`` is falsy -- sentiment
    scoring still runs on SEC filings alone rather than failing outright,
    matching ``sentiment_monitor.py``'s NewsCollector behavior.

    ``as_of`` anchors the trailing window to a caller-supplied instant
    instead of recomputing ``datetime.now(timezone.utc)`` fresh on every
    call -- see ``fetch_sec_filings``'s matching parameter for why
    (Antigravity review on PR #403: "Anchor sentiment lookbacks to the run
    timestamp"). Defaults to ``None`` (falls back to "now").

    ``errors`` mirrors ``fetch_sec_filings``'s parameter: a message is
    appended when the fetch ITSELF fails (a 401/429, a timeout, malformed
    JSON), distinct from a clean zero-article response, which stays a
    silent ``[]`` as before. Without this, a real NewsAPI outage was
    indistinguishable from "no headlines" in sentiment_score()'s result,
    silently changing an ADD/WATCH/REJECT classification with no
    diagnostic anywhere in scores.json/scores.md (Codex review on PR #403:
    "Preserve NewsAPI failures in scoring artifacts").
    """
    if not news_api_key:
        return []

    if fetcher is None:
        def fetcher() -> dict:
            import requests
            from datetime import datetime, timedelta, timezone

            run_instant = as_of if as_of is not None else datetime.now(timezone.utc)
            since = (run_instant - timedelta(days=days)).date().isoformat()
            # A bare ticker is a poor full-text query for common-word
            # symbols -- "COIN", "SHOP", and active-universe "CAT" all match
            # plenty of unrelated cryptocurrency/retail/animal articles that
            # FinBERT would then score as issuer sentiment. Biasing toward a
            # finance context reduces (doesn't eliminate) that false-positive
            # rate without a company-name-resolution service this tool
            # doesn't have (Codex review on PR #403: "Restrict news searches
            # to the candidate issuer").
            query = f'"{ticker}" AND (stock OR shares OR earnings OR NASDAQ OR NYSE)'
            params = {
                "q": query,
                "from": since,
                # Without an upper bound, NewsAPI can return articles
                # published AFTER the captured `as_of` instant -- this
                # query has no way to ask for "as of a past moment," only
                # "not before a given date," so a long-lived run (a cold
                # multi-GB FinBERT init, a large candidate batch) could
                # still pick up genuinely-new articles that appeared
                # while it was running, making results depend on
                # candidate query order even with `from` anchored
                # (Antigravity review on PR #403: "Bound NewsAPI results
                # at the run timestamp"). The full instant, not just its
                # date, caps exactly at the run's captured moment.
                "to": run_instant.isoformat(),
                "sortBy": "publishedAt",
                "language": "en",
                "pageSize": page_size,
            }
            # The key belongs in a header, not a query parameter -- a URL
            # query string is copied into proxy/access logs and APM tooling
            # far beyond this process, and _sanitize_fetch_error below can
            # only redact it from an already-raised exception, not from
            # copies already captured upstream. The repo's existing
            # NewsCollector (trading-strategies/sentiment_monitor.py) sends
            # it via X-Api-Key for the same reason (Codex review on PR #403:
            # "Keep the NewsAPI key out of the request URL").
            headers = {"X-Api-Key": news_api_key}
            response = requests.get(NEWS_API_URL, params=params, headers=headers, timeout=timeout)
            response.raise_for_status()
            return response.json()

    try:
        payload = fetcher()
    except Exception as exc:  # noqa: BLE001 -- one candidate's NewsAPI lookup failing must not abort the batch
        if errors is not None:
            errors.append(f"NewsAPI fetch failed for {ticker}: {_sanitize_fetch_error(exc)}")
        return []

    if not isinstance(payload, dict):
        # A response that parses as valid JSON but isn't a mapping at the
        # top level (`null`, a bare list, ...) would otherwise raise
        # AttributeError on `.get("articles")` below, OUTSIDE the fetch
        # exception handler above -- and since this fetch runs before the
        # SEC fetch in sentiment_score(), that uncaught error would discard
        # the whole sentiment signal (including otherwise-available SEC
        # filings) for the candidate, the same class of failure already
        # guarded against for "articles": null (Codex review on PR #403:
        # "Validate the top-level NewsAPI payload").
        if errors is not None:
            errors.append(f"NewsAPI fetch failed for {ticker}: unexpected response type {type(payload).__name__}")
        return []

    # A successful NewsAPI response can carry `"articles": null` (not just
    # an absent key), which `.get("articles", [])` passes through as None
    # -- the loop below would then raise TypeError, and since this fetch
    # runs before the SEC fetch in sentiment_score(), that exception would
    # discard the whole sentiment signal (including otherwise-available SEC
    # filings) for the candidate (Codex review on PR #403: "Treat null
    # NewsAPI article lists as empty").
    articles = payload.get("articles") or []
    # `or []` only substitutes when "articles" is falsy (missing/null/
    # empty) -- a truthy non-list value (e.g. `{"articles": 1}`) sails
    # through unchanged, and iterating a non-iterable raises TypeError
    # OUTSIDE the fetch exception handler above. Since this fetch runs
    # before the SEC fetch in sentiment_score(), that would discard
    # otherwise-available SEC sentiment too (Codex review on PR #403:
    # "Validate the NewsAPI articles collection").
    if not isinstance(articles, list):
        articles = []
    headlines = []
    for article in articles:
        if not isinstance(article, dict):
            continue
        # The dict-shape check above only guards the ARTICLE itself -- a
        # dict article whose "title"/"description" is some other JSON type
        # (a number, a nested object) still reaches str.join() below, which
        # raises TypeError on a non-str element outside the fetch exception
        # handler above, discarding the whole sentiment signal for the
        # candidate the same way the article-shape and null-articles bugs
        # did (Codex review on PR #403: "Validate NewsAPI article field
        # types before joining").
        title = article.get("title")
        description = article.get("description")
        fields = [field for field in (title, description) if isinstance(field, str) and field]
        text = " ".join(fields)
        if text:
            headlines.append({"ticker": ticker, "text": text})
    return headlines


def _dedupe_items(items: list[dict[str, str]]) -> list[dict[str, str]]:
    """Drop duplicate/syndicated text before it reaches FinBERT.

    NewsAPI can return the same story under multiple records (wire
    syndication, a near-identical re-publish) -- scoring each copy
    independently would give that one story multiple confidence-weighted
    votes in compute_ticker_sentiment(), letting a single widely duplicated
    article dominate the issuer's sentiment (Codex review on PR #403:
    "Deduplicate repeated news articles before sentiment scoring").
    Normalized on stripped/lowercased text since the dedup key doesn't need
    to be the display text itself.
    """
    seen: set[str] = set()
    deduped: list[dict[str, str]] = []
    for item in items:
        key = item["text"].strip().lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def compute_ticker_sentiment(scored_items: list[dict[str, Any]]) -> float | None:
    """Confidence-weighted average of ``(positive - negative)`` across scored items.

    Pure function, no model/network dependency -- takes the output of
    ``FinbertSentimentAnalyzer.score_texts``. Returns ``None`` for an empty
    input rather than 0.0, so callers can distinguish "no sentiment signal"
    from "neutral sentiment."
    """
    if not scored_items:
        return None
    total_weight = sum(float(item["confidence"]) for item in scored_items)
    if total_weight <= 0:
        return 0.0
    weighted_sum = sum(
        (float(item["positive"]) - float(item["negative"])) * float(item["confidence"]) for item in scored_items
    )
    return weighted_sum / total_weight


def _resolve_finbert_revision(
    model_name: str, revision: str, *, resolver: Callable[[str, str], str | None] | None = None
) -> str:
    """Resolve a mutable Hub ref (e.g. "main") to the commit SHA it currently points at.

    ``resolver`` is an injectable ``(model_name, revision) -> sha | None`` for
    tests -- production code never has to mock ``huggingface_hub`` directly,
    matching this repo's injected-callable convention.

    Raises whatever the resolver raises (no network egress, huggingface_hub
    not importable, an HfApi error) or a ``ValueError`` for an unusable
    response, rather than degrading to the unresolved ``revision`` -- a
    silent fallback would let the analyzer proceed and persist
    ``finbert_revision: "main"`` looking exactly like a successfully pinned
    run, so a later run whose resolution DID succeed (or that happened to
    hit a different upstream commit under the same unresolved "main") could
    silently diverge under identical recorded metadata, defeating the
    auditability this exists for (Codex review on PR #429: "Reject
    unresolved mutable FinBERT revisions"). This is safe to let propagate
    out of ``FinbertSentimentAnalyzer.__init__`` -- its caller in
    advise_tickers.py already wraps construction in a try/except that
    degrades to skipping sentiment for the run (recording the reason in
    ``sentiment_unavailable_reason``) rather than crashing it, the same
    path any other constructor failure already takes.
    """
    if resolver is None:

        def resolver(name: str, rev: str) -> str | None:
            import importlib

            HfApi = importlib.import_module("huggingface_hub").HfApi

            return HfApi().model_info(name, revision=rev).sha

    resolved = resolver(model_name, revision)
    if not isinstance(resolved, str) or not resolved:
        raise ValueError(f"could not resolve a concrete commit SHA for {model_name}@{revision!r}: got {resolved!r}")
    return resolved


class FinbertSentimentAnalyzer:
    """Loads FinBERT once; reuse a single instance across every candidate.

    Model loading (the expensive part) happens in ``__init__``, not per
    call, so a caller scoring N candidates should build one analyzer and
    pass it to every ``sentiment_score`` call rather than let each call
    construct its own.
    """

    def __init__(
        self,
        model_name: str = "ProsusAI/finbert",
        revision: str | None = None,
        revision_resolver: Callable[[str, str], str | None] | None = None,
    ) -> None:
        import importlib
        import os

        transformers = importlib.import_module("transformers")
        AutoModelForSequenceClassification = transformers.AutoModelForSequenceClassification
        AutoTokenizer = transformers.AutoTokenizer

        # Resolve the revision ONCE and pass the SAME value to BOTH
        # downloads -- two separate from_pretrained() calls against the
        # mutable default branch ("main") could each resolve a DIFFERENT
        # commit if the upstream repository changes in between, and even a
        # single commit resolved implicitly at call time isn't recorded
        # anywhere. The same candidates and persisted run parameters could
        # then produce different sentiment scores across runs with no
        # artifact identifying which weights/tokenizer were actually used
        # (Codex review on PR #403: "Pin and record the FinBERT
        # revision"). self.revision is exposed for the caller to persist
        # into run_metadata; overridable via the `revision` argument or
        # $FINBERT_MODEL_REVISION for a deployment that wants to pin a
        # specific commit SHA for true cross-run reproducibility.
        env_revision = os.environ.get("FINBERT_MODEL_REVISION")
        self.revision = revision or (env_revision or "main")
        # An explicit revision/env override is already a caller-chosen pin
        # (a tag, branch, or commit SHA) and resolving it further would
        # silently override that intent. Only the untouched default -- the
        # mutable "main" branch -- still needs to be turned into a concrete
        # commit before use: leaving it as the literal string "main" means
        # identical parameters can silently load a different tokenizer/model
        # commit run over run as the upstream branch advances, while both
        # reports keep recording finbert_revision: "main", defeating the
        # auditability this metadata exists for (Codex review on PR #429:
        # "Resolve an immutable FinBERT revision"). A resolution failure
        # (no network egress, huggingface_hub not importable) propagates
        # rather than silently falling back to the unresolved "main" --
        # advise_tickers.py's caller already degrades a construction
        # failure to skipping sentiment for the run rather than crashing
        # it, so raising here is what actually prevents a silently
        # unpinned run from looking identical to a successfully pinned one
        # (Codex review on PR #429: "Reject unresolved mutable FinBERT
        # revisions").
        if self.revision == "main":
            self.revision = _resolve_finbert_revision(model_name, self.revision, resolver=revision_resolver)
        self._tokenizer = AutoTokenizer.from_pretrained(model_name, revision=self.revision)
        self._model = AutoModelForSequenceClassification.from_pretrained(model_name, revision=self.revision)
        self._model.eval()
        self._labels = [self._model.config.id2label[i] for i in range(len(self._model.config.id2label))]

    def score_texts(self, texts: list[str], *, batch_size: int = 16) -> list[dict[str, Any]]:
        """Run FinBERT over ``texts``, chunked by ``batch_size`` to bound peak memory.

        Returns one dict per input text (same order), each with:
            ``text``: the original input string, echoed back for traceability.
            ``positive``/``negative``/``neutral``: FinBERT's softmax
                probability for that label, each in [0, 1] and summing to 1.
            ``label``: the highest-probability label among the three.
            ``confidence``: that label's probability (i.e. ``max`` of the
                three above) -- the weight ``compute_ticker_sentiment`` uses
                to combine multiple items (Codex review on PR #403:
                "Document the public score_texts method").
        """
        if not texts:
            return []
        import importlib

        torch = importlib.import_module("torch")

        results: list[dict[str, Any]] = []
        # A candidate's news + SEC items are scored in one call; chunking
        # bounds peak memory for an unusually large batch rather than
        # tokenizing/running the whole list through the model in one forward
        # pass (Antigravity code review on PR #403).
        for start in range(0, len(texts), batch_size):
            chunk = texts[start : start + batch_size]
            inputs = self._tokenizer(chunk, return_tensors="pt", padding=True, truncation=True, max_length=512)
            with torch.no_grad():
                logits = self._model(**inputs).logits
            probabilities = torch.nn.functional.softmax(logits, dim=-1).tolist()

            for text, probability_row in zip(chunk, probabilities, strict=True):
                by_label: dict[str, float] = dict(zip(self._labels, probability_row, strict=True))
                label = max(by_label, key=lambda k: by_label[k])
                results.append(
                    {
                        "text": text,
                        "positive": by_label.get("positive", 0.0),
                        "negative": by_label.get("negative", 0.0),
                        "neutral": by_label.get("neutral", 0.0),
                        "label": label,
                        "confidence": by_label[label],
                    }
                )
        return results


def sentiment_score(
    ticker: str,
    *,
    news_api_key: str | None = None,
    analyzer: FinbertSentimentAnalyzer | Any | None = None,
    days: int = 7,
    sec_limit: int = 3,
    as_of: datetime | None = None,
) -> dict[str, Any] | None:
    """News + SEC-filing sentiment for ``ticker``, or ``None`` if no text was found.

    Bypasses ``sentiment_monitor.py``'s ``run_pipeline``/``EmailBriefing``
    entirely -- this never sends an email; it only ever returns a score.

    ``as_of`` anchors both fetches' trailing lookback windows to a single
    caller-supplied instant instead of each one independently recomputing
    "now" -- a multi-candidate run spanning UTC midnight would otherwise
    give candidates before and after midnight different windows purely
    from scoring order (Antigravity review on PR #403: "Anchor sentiment
    lookbacks to the run timestamp"). Defaults to ``None`` (each fetch
    falls back to its own "now").

    A SEC EDGAR or NewsAPI fetch failure (as opposed to a clean "nothing
    found" response) does NOT fall into the plain ``None`` case above when
    there's otherwise no text -- it returns a score-less dict carrying an
    ``"errors"`` list instead, so a caller can tell "the request failed"
    apart from "there was genuinely nothing to report" (Codex review on PR
    #403: "Preserve SEC request failures in the report" / "Preserve
    NewsAPI failures in scoring artifacts").
    """
    fetch_errors: list[str] = []
    news_items = fetch_news_headlines(
        ticker, news_api_key=news_api_key, days=days, errors=fetch_errors, as_of=as_of
    )
    sec_items = fetch_sec_filings(
        ticker, limit=sec_limit, days=days, errors=fetch_errors, as_of=as_of.date() if as_of is not None else None
    )
    # NewsAPI can return the same story under multiple records (wire
    # syndication, a near-identical re-publish) -- scoring each copy
    # independently gives that one story multiple confidence-weighted votes
    # in compute_ticker_sentiment(), letting a single widely duplicated
    # article dominate the issuer's sentiment (Codex review on PR #403:
    # "Deduplicate repeated news articles before sentiment scoring").
    all_items = _dedupe_items(news_items + sec_items)
    if not all_items:
        if fetch_errors:
            return {
                "score": None,
                "n_news_headlines": len(news_items),
                "n_sec_filings": len(sec_items),
                "news_api_key_used": bool(news_api_key),
                "errors": fetch_errors,
            }
        return None

    active_analyzer = analyzer if analyzer is not None else FinbertSentimentAnalyzer()
    scored = active_analyzer.score_texts([item["text"] for item in all_items])
    score = compute_ticker_sentiment(scored)
    result: dict[str, Any] = {
        "score": score,
        "n_news_headlines": len(news_items),
        "n_sec_filings": len(sec_items),
        "news_api_key_used": bool(news_api_key),
    }
    if fetch_errors:
        result["errors"] = fetch_errors
    return result


__all__ = [
    "strategy_health_score",
    "generate_ma_crossover_signals",
    "generate_rsi_reversion_signals",
    "backtest_score",
    "fetch_sec_filings",
    "fetch_news_headlines",
    "compute_ticker_sentiment",
    "FinbertSentimentAnalyzer",
    "sentiment_score",
]
