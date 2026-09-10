#!/usr/bin/env python3
"""
Monday Morning Pre-Market Sentiment Monitor
============================================
Scans NewsAPI headlines and SEC EDGAR filings for a fixed watchlist,
computes FinBERT-based sentiment scores, calculates z-scores against a
rolling 30-day history, and emails a concise alert ONLY when at least one
ticker crosses the configured threshold (default |z| >= 2.0).

Designed to run as a cron job every Monday at 7:00 AM ET before market open.

Cron: 0 7 * * 1 cd /path/to/monday-sentiment && python sentiment_cron.py >> cron.log 2>&1
Timezone note: if system is UTC, use: 0 12 * * 1  (12:00 UTC = 7:00 AM ET)

pip install transformers torch newsapi-python requests
"""

# ---------------------------------------------------------------------------
# Standard library
# ---------------------------------------------------------------------------
import argparse
import asyncio
import concurrent.futures
import html
import json
import logging
import os
import shutil
import smtplib
import ssl
import sys
import traceback
import urllib.error
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from statistics import mean, stdev
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo  # Python 3.9+; use pytz if < 3.9

# ---------------------------------------------------------------------------
# Third-party (installed via pip install transformers torch newsapi-python requests)
# ---------------------------------------------------------------------------
import aiohttp  # type: ignore

# ---------------------------------------------------------------------------
# Module-level logger — configured in main() so callers that import this
# module don't get surprise log output.
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("sentiment_cron")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_WATCHLIST: List[str] = ["NVDA", "AAPL", "MSFT", "TSLA", "META"]
DEFAULT_THRESHOLD: float = 2.0        # |z-score| must exceed this to alert
DEFAULT_LOOKBACK_DAYS: int = 7        # news window
DEFAULT_HISTORY_PATH: str = "./sentiment_history.json"
ROLLING_WINDOW_DAYS: int = 30         # days kept in history
MIN_HISTORY_POINTS: int = 5           # need at least this many days to compute z
MAX_FLAGGED_TICKERS: int = 3          # cap email to top-N most extreme movers
MAX_HEADLINES_PER_TICKER: int = 50    # NewsAPI page size per ticker
MAX_SEC_FILINGS: int = 5              # EDGAR EFTS results per ticker
TOP_HEADLINES_IN_EMAIL: int = 3       # per-ticker headline slots in email body
MAX_CONCURRENT_TICKERS: int = 5       # cap in-flight tickers so EDGAR/NewsAPI calls stay under fair-use limits
# SEC's fair-access policy asks for no more than ~10 requests/second
# (https://www.sec.gov/developer). MAX_CONCURRENT_TICKERS only bounds how
# many ticker tasks are in flight at once — with fast EDGAR responses each
# completed slot is refilled immediately, so concurrency alone does not cap
# the request RATE. SEC_MAX_REQUESTS_PER_SECOND drives an explicit
# time-based limiter (SecRateLimiter, below) around the EDGAR call itself,
# with headroom under SEC's stated ceiling (codex review discussion_r3742435139).
SEC_MAX_REQUESTS_PER_SECOND: float = 8.0

# SEC EDGAR full-text search endpoint — free, no API key required
# https://efts.sec.gov/LATEST/search-index?q=AAPL&forms=8-K,10-K,10-Q
SEC_EFTS_URL = "https://efts.sec.gov/LATEST/search-index"

# SEC requires a descriptive User-Agent per their policy:
# https://www.sec.gov/developer
SEC_USER_AGENT = os.environ.get("SEC_EDGAR_USER_AGENT", "MondaySentimentBot/1.0 contact@example.com")

# FinBERT model from HuggingFace
FINBERT_MODEL = "ProsusAI/finbert"

# CPU PyTorch inference already uses native intra-op threads, so letting the
# default (multi-worker) event loop executor run several score_texts() calls
# at once oversubscribes the host and multiplies activation memory -- on
# small cron hosts that can be slower than the old sequential path or OOM.
# A single-worker executor keeps inference off the event loop while still
# serializing access to the shared FinBERT pipeline (codex review
# discussion_r3848690266).
_SCORING_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=1)

# Eastern timezone — used for market-open countdown
ET = ZoneInfo("America/New_York")

# Company name map for display purposes — extend as you add tickers
COMPANY_NAMES: Dict[str, str] = {
    "NVDA": "NVIDIA Corporation",
    "AAPL": "Apple Inc.",
    "MSFT": "Microsoft Corporation",
    "TSLA": "Tesla, Inc.",
    "META": "Meta Platforms, Inc.",
    "AMZN": "Amazon.com, Inc.",
    "GOOGL": "Alphabet Inc. (GOOGL)",
    "GOOG": "Alphabet Inc. (GOOG)",
    "JPM": "JPMorgan Chase & Co.",
    "AMD": "Advanced Micro Devices, Inc.",
}


# ===========================================================================
# Data structures (simple dicts rather than dataclasses to stay lean)
# ===========================================================================

def _sanitize_exception(exc: BaseException) -> str:
    """Render ``exc`` as its class name plus, only for known-safe cases, a
    short reason -- never its full message.

    A delivery failure can raise exceptions whose message embeds the
    complete offending URL/path -- e.g. ``http.client.InvalidURL`` for a
    webhook URL containing a space or control character quotes the whole
    path back -- and webhook URLs commonly carry auth tokens in that path.
    Logging ``str(exc)`` unconditionally would expose those tokens to stderr.
    Mirrored identically from dca_alerts._sanitize_exception.
    """
    if isinstance(exc, urllib.error.HTTPError):
        return f"{type(exc).__name__}: HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, OSError) and getattr(reason, "errno", None) is not None:
            return f"{type(exc).__name__}: errno {reason.errno}"
        if isinstance(reason, BaseException):
            return f"{type(exc).__name__}: {type(reason).__name__}"
        return type(exc).__name__
    return type(exc).__name__

def make_item(text: str, source: str, item_type: str) -> dict:
    """
    Create a raw news/filing item before scoring.
    item_type: 'news' | 'sec'
    """
    return {"text": text, "source": source, "type": item_type, "score": None, "probs": None}


def make_scored_item(item: dict, positive: float, negative: float, neutral: float) -> dict:
    """
    Attach FinBERT probability outputs to an item.
    Weighted score = (positive - negative) * max(positive, negative, neutral)
    The confidence factor rewards decisive predictions and down-weights
    near-uniform distributions where the model is uncertain.
    """
    confidence = max(positive, negative, neutral)
    weighted_score = (positive - negative) * confidence
    return {
        **item,
        "probs": {"positive": positive, "negative": negative, "neutral": neutral},
        "score": weighted_score,
    }


# ===========================================================================
# FinBERT loader — done once per run, fail fast if unavailable
# ===========================================================================

def load_finbert_pipeline():
    """
    Load ProsusAI/finbert as a HuggingFace text-classification pipeline.
    Returns the pipeline callable.
    Exits with code 1 on failure because without NLP the run is meaningless.
    """
    try:
        # Import here so the module can be imported without torch installed
        # (useful for unit-testing stubs).
        from transformers import pipeline as hf_pipeline  # type: ignore
        log.info("Loading FinBERT model '%s' — first run will download ~500 MB", FINBERT_MODEL)
        pipe = hf_pipeline(
            "text-classification",
            model=FINBERT_MODEL,
            # Return all three label probabilities (positive/negative/neutral)
            top_k=None,
            # Truncate inputs that exceed BERT's 512-token limit automatically
            truncation=True,
            max_length=512,
        )
        log.info("FinBERT pipeline ready.")
        return pipe
    except Exception as exc:
        log.error("FATAL: Could not load FinBERT model. Is 'transformers' and 'torch' installed?")
        log.error("  pip install transformers torch")
        log.error("  Detail: %s", exc)
        sys.exit(1)


def score_texts(pipe, texts: List[str]) -> List[dict]:
    """
    Run FinBERT on a batch of text strings.
    Returns list of dicts with keys positive/negative/neutral probabilities.

    FinBERT returns e.g.:
      [[{"label": "positive", "score": 0.87}, {"label": "negative", ...}, ...], ...]
    We normalise to a clean {positive, negative, neutral} dict per text.
    """
    if not texts:
        return []

    # Batch inference — FinBERT is fast on CPU for short strings
    raw_outputs = pipe(texts)

    results = []
    for output in raw_outputs:
        probs = {entry["label"].lower(): entry["score"] for entry in output}
        # Guard against missing labels (shouldn't happen with finbert but be safe)
        positive = probs.get("positive", 0.0)
        negative = probs.get("negative", 0.0)
        neutral  = probs.get("neutral",  0.0)
        results.append({"positive": positive, "negative": negative, "neutral": neutral})

    return results


# ===========================================================================
# News collection — NewsAPI
# ===========================================================================

async def fetch_news_headlines(session: aiohttp.ClientSession, ticker: str, api_key: str, lookback_days: int) -> List[dict]:
    """
    Pull recent headlines from NewsAPI for a given ticker symbol.
    Returns a list of item dicts (unsorted).

    NewsAPI free tier allows 100 requests/day and returns up to 100 articles
    per request on paid plans (20 on free). We request the max page size and
    accept whatever we get.

    Docs: https://newsapi.org/docs/endpoints/everything
    """
    from_date = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    to_date   = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    params = {
        "q": ticker,
        "from": from_date,
        "to": to_date,
        "language": "en",
        "sortBy": "relevancy",
        "pageSize": MAX_HEADLINES_PER_TICKER,
    }
    headers = {
        "X-Api-Key": api_key,
    }

    try:
        async with session.get(
            "https://newsapi.org/v2/everything", params=params, headers=headers, timeout=15
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        # api_key is the only secret that could plausibly appear in an
        # aiohttp exception string for this call (it's sent as a header,
        # never in the URL/params) -- redact it directly rather than
        # collapsing to _sanitize_exception's class-name-only fallback,
        # which would erase useful diagnostic detail for no security gain.
        safe_exc = str(exc).replace(api_key, "***") if api_key else str(exc)
        log.warning("NewsAPI request failed for %s: %s", ticker, safe_exc)
        return []
    except json.JSONDecodeError:
        log.warning("NewsAPI returned non-JSON for %s", ticker)
        return []

    if data.get("status") != "ok":
        log.warning("NewsAPI error for %s: %s", ticker, data.get("message", "unknown"))
        return []

    items = []
    for article in data.get("articles", []):
        # Combine title + description for richer context; strip None values
        title   = article.get("title") or ""
        desc    = article.get("description") or ""
        text    = (title + " " + desc).strip()
        source  = (article.get("source") or {}).get("name", "NewsAPI")
        if text:
            items.append(make_item(text, source, "news"))

    log.info("  [%s] Fetched %d news headlines from NewsAPI", ticker, len(items))
    return items


# ===========================================================================
# SEC EDGAR collection
# ===========================================================================

class SecRateLimiter:
    """
    Time-based limiter enforcing SEC EDGAR's fair-access request rate.

    Unlike a plain asyncio.Semaphore — which only bounds how many requests
    are concurrently in flight and lets a completed slot be refilled
    instantly — this enforces a minimum spacing between requests, so the
    achieved rate never exceeds `max_per_second` regardless of how many
    ticker tasks are running or how fast EDGAR responds
    (codex review discussion_r3742435139). One instance is shared across
    all ticker tasks in a run.
    """

    def __init__(self, max_per_second: float) -> None:
        self._min_interval = 1.0 / max_per_second
        self._lock = asyncio.Lock()
        self._next_allowed_at = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            loop = asyncio.get_event_loop()
            now = loop.time()
            wait = self._next_allowed_at - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = loop.time()
            self._next_allowed_at = max(now, self._next_allowed_at) + self._min_interval


async def fetch_sec_filings(
    session: aiohttp.ClientSession,
    ticker: str,
    lookback_days: int,
    rate_limiter: SecRateLimiter,
) -> List[dict]:
    """
    Pull recent 8-K, 10-K, and 10-Q filings from SEC EDGAR Full-Text Search.

    EDGAR EFTS is free — no API key — but requires a User-Agent header that
    identifies your application per SEC developer guidelines.

    We take the filing title + extracted text snippet (if present) as the
    item text. The snippet is typically the first few hundred words of the
    filing, which is enough for reliable FinBERT classification.

    `rate_limiter` paces the request itself against SEC's fair-access
    policy — see SecRateLimiter above for why this can't just be the
    ticker-level concurrency cap.

    Endpoint docs: https://efts.sec.gov/LATEST/search-index (unofficial)
    """
    start_dt = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    end_dt   = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    params = {
        "q": ticker,
        "dateRange": "custom",
        "startdt": start_dt,
        "enddt": end_dt,
        "forms": "8-K,10-K,10-Q",
        "_source": "file_date,period_of_report,display_names,form_type,file_num,period_of_report,entity_name,biz_location,inc_states,category",
        "from": 0,
        "size": MAX_SEC_FILINGS,
    }

    headers = {
        "User-Agent": SEC_USER_AGENT,
        "Accept": "application/json",
    }

    await rate_limiter.acquire()
    try:
        async with session.get(SEC_EFTS_URL, params=params, headers=headers, timeout=20) as resp:
            resp.raise_for_status()
            data = await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        # No secret is involved in this request, but the raw exception can
        # still embed the full request URL (query params/host) -- sanitize
        # it the same way the other network calls in this module do.
        log.warning("SEC EDGAR request failed for %s: %s", ticker, _sanitize_exception(exc))
        return []
    except json.JSONDecodeError:
        log.warning("SEC EDGAR returned non-JSON for %s", ticker)
        return []

    hits = data.get("hits", {}).get("hits", [])
    items = []
    for hit in hits:
        src = hit.get("_source", {})
        form_type   = src.get("form_type", "SEC Filing")
        entity_name = src.get("entity_name", ticker)
        file_date   = src.get("file_date", "")

        # Build a brief descriptive text that FinBERT can classify
        # format: "<FormType> filed by <Entity> on <Date>"
        # This is minimal but still captures the filing context. When the
        # filing snippet ("_text") is available we prepend it for richer signal.
        snippet = hit.get("_text", "").strip()
        if snippet:
            text = f"{form_type} filed by {entity_name} on {file_date}: {snippet[:400]}"
        else:
            text = f"{form_type} filed by {entity_name} on {file_date}"

        source = f"SEC EDGAR ({form_type})"
        items.append(make_item(text, source, "sec"))

    log.info("  [%s] Fetched %d SEC filings from EDGAR", ticker, len(items))
    return items


# ===========================================================================
# Scoring pipeline — ties together data collection and FinBERT
# ===========================================================================

async def compute_ticker_score(
    session: aiohttp.ClientSession,
    ticker: str,
    pipe,
    news_api_key: Optional[str],
    lookback_days: int,
    sec_rate_limiter: SecRateLimiter,
) -> Tuple[float, List[dict]]:
    """
    Collect all available text items for a ticker, score them with FinBERT,
    and return (mean_score, scored_items_list).

    If no items are found at all, returns (0.0, []).
    The mean score is the unweighted average of all individual item scores.
    Equal-weighting is intentional: one dramatic headline should not drown
    out fifty neutral ones — the z-score comparison handles magnitude.
    """
    items: List[dict] = []

    # --- NewsAPI ---
    if news_api_key:
        news_items = await fetch_news_headlines(session, ticker, news_api_key, lookback_days)
        items.extend(news_items)
    else:
        log.debug("Skipping NewsAPI for %s (no key)", ticker)

    # --- SEC EDGAR ---
    sec_items = await fetch_sec_filings(session, ticker, lookback_days, sec_rate_limiter)
    items.extend(sec_items)

    if not items:
        log.warning("  [%s] No data items found — score defaults to 0.0", ticker)
        return 0.0, []

    # Batch-score all texts in one pass for efficiency
    texts = [item["text"] for item in items]
    log.info("  [%s] Scoring %d items with FinBERT…", ticker, len(texts))

    # Run CPU-bound inference in the single-worker scoring executor so it
    # doesn't block the event loop, without letting overlapping ticker tasks
    # oversubscribe the host by hitting the shared FinBERT pipeline at once.
    loop = asyncio.get_running_loop()
    prob_results = await loop.run_in_executor(_SCORING_EXECUTOR, score_texts, pipe, texts)

    scored_items = []
    for item, probs in zip(items, prob_results, strict=True):
        scored = make_scored_item(
            item,
            probs["positive"],
            probs["negative"],
            probs["neutral"],
        )
        scored_items.append(scored)

    # Mean of all individual weighted scores
    all_scores = [s["score"] for s in scored_items]
    ticker_score = mean(all_scores)

    log.info("  [%s] Score: %.4f  (from %d items)", ticker, ticker_score, len(scored_items))
    return ticker_score, scored_items


# ===========================================================================
# History management — rolling 30-day window in JSON
# ===========================================================================

def load_history(history_path: str) -> dict:
    """
    Load the JSON history file. Structure:
    {
      "NVDA": [{"date": "2026-06-20", "score": 0.123}, ...],
      "AAPL": [...],
      ...
    }
    If the file is missing or corrupted, starts fresh with an empty dict.
    A corrupted file is backed up as <file>.bak before being replaced.
    """
    path = Path(history_path)
    if not path.exists():
        log.info("History file not found — starting fresh: %s", history_path)
        return {}

    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("Top-level structure must be a dict")
        log.info("Loaded history from %s", history_path)
        return data
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        backup = str(path) + ".bak"
        log.warning("History file corrupted (%s) — backing up to %s and starting fresh", exc, backup)
        try:
            shutil.copy2(str(path), backup)
        except OSError as bak_err:
            log.warning("Could not back up history: %s", bak_err)
        return {}


def save_history(history: dict, history_path: str) -> None:
    """
    Persist the history dict to disk as JSON.
    Writes to a temp file first, then atomically renames to avoid partial
    writes corrupting the history on an unexpected crash.
    """
    path = Path(history_path)
    tmp_path = path.with_suffix(".tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(history, f, indent=2, default=str)
        tmp_path.rename(path)
        log.info("History saved to %s", history_path)
    except OSError as exc:
        log.error("Failed to save history: %s", exc)


def prune_history(history: dict, today_str: str) -> dict:
    """
    Remove entries older than ROLLING_WINDOW_DAYS from every ticker's list.
    This keeps the file small and the z-score window accurate.
    """
    cutoff = (datetime.strptime(today_str, "%Y-%m-%d") - timedelta(days=ROLLING_WINDOW_DAYS)).strftime("%Y-%m-%d")
    for ticker in history:
        history[ticker] = [
            entry for entry in history[ticker]
            if entry.get("date", "") >= cutoff
        ]
    return history


def update_history(history: dict, ticker: str, today_str: str, score: float) -> dict:
    """
    Add today's score for a ticker. If an entry for today already exists
    (e.g. script run twice in a day), replace it rather than append.
    Idempotent for the same calendar day.
    """
    if ticker not in history:
        history[ticker] = []

    # Remove any existing entry for today (handles re-runs)
    history[ticker] = [e for e in history[ticker] if e.get("date") != today_str]
    history[ticker].append({"date": today_str, "score": score})

    # Keep list sorted by date for readability
    history[ticker].sort(key=lambda e: e.get("date", ""))
    return history


def compute_zscore(history: dict, ticker: str, today_score: float) -> Optional[float]:
    """
    Compute z-score for today's score relative to the rolling window.
    Returns None if the ticker has fewer than MIN_HISTORY_POINTS data points
    (not enough history for a meaningful z-score).

    z = (x - μ) / σ  where the population is the N historical scores
    excluding today (so today is the observation, history is the reference).
    """
    entries = history.get(ticker, [])
    # Exclude today from the reference population
    today_str = datetime.now(ET).strftime("%Y-%m-%d")
    historical_scores = [
        e["score"] for e in entries
        if e.get("date") != today_str and isinstance(e.get("score"), (int, float))
    ]

    if len(historical_scores) < MIN_HISTORY_POINTS:
        log.info("  [%s] Only %d historical points — need %d; skipping z-score", ticker, len(historical_scores), MIN_HISTORY_POINTS)
        return None

    mu = mean(historical_scores)
    sigma = stdev(historical_scores)  # sample stdev (denominator N-1)

    if sigma == 0:
        # All historical scores identical — z is undefined; treat as 0
        log.debug("  [%s] Zero stdev in history — z-score undefined, returning 0", ticker)
        return 0.0

    z = (today_score - mu) / sigma
    log.info("  [%s] μ=%.4f σ=%.4f z=%.3f", ticker, mu, sigma, z)
    return z


# ===========================================================================
# Filtering and ranking
# ===========================================================================

def filter_and_rank(
    results: Dict[str, dict],
    threshold: float,
    max_flagged: int,
) -> Tuple[List[dict], List[dict]]:
    """
    Separate tickers into flagged (|z| >= threshold) and within-normal-range.
    Returns (flagged_sorted_desc_by_abs_z, normal_list).

    Only tickers with a computed z-score are eligible for flagging.
    Tickers without enough history are included in the normal list with
    a note but never trigger an alert (conservative design choice — we need
    a baseline before declaring anomalies).
    """
    flagged = []
    normal  = []

    for _ticker, info in results.items():
        z = info.get("zscore")
        if z is None:
            # Not enough history — benign default
            info["status"] = "insufficient_history"
            normal.append(info)
            continue

        abs_z = abs(z)
        if abs_z >= threshold:
            info["status"] = "flagged"
            flagged.append(info)
        else:
            info["status"] = "normal"
            normal.append(info)

    # Sort flagged tickers by magnitude descending, cap at max_flagged
    flagged.sort(key=lambda x: abs(x.get("zscore", 0.0)), reverse=True)
    flagged = flagged[:max_flagged]

    return flagged, normal


# ===========================================================================
# Sentiment badge helpers
# ===========================================================================

def sentiment_badge(zscore: float) -> Tuple[str, str, str]:
    """
    Return (emoji, label, css_color) for a z-score.
    Sign determines bullish/bearish; magnitude drives intensity language.
    |z| >= threshold already guaranteed by caller.
    """
    if zscore >= 3.0:
        return "🟢", "STRONG BULLISH SURGE", "#1a7a2e"
    elif zscore >= 2.0:
        return "🟢", "BULLISH SURGE", "#2da44e"
    elif zscore <= -3.0:
        return "🔴", "STRONG BEARISH DROP", "#8b0000"
    elif zscore <= -2.0:
        return "🔴", "BEARISH DROP", "#cf222e"
    else:
        # Shouldn't reach here given threshold, but safe fallback
        return "🟡", "VOLATILE", "#e3a000"


def finbert_badge_html(probs: dict) -> str:
    """
    Render a compact inline FinBERT probability badge as HTML.
    e.g. [+87% | -8% | ~5%]
    """
    pos = probs.get("positive", 0) * 100
    neg = probs.get("negative", 0) * 100
    neu = probs.get("neutral",  0) * 100
    return (
        f'<span style="font-family:monospace;font-size:11px;color:#555;">'
        f'[+{pos:.0f}% | -{neg:.0f}% | ~{neu:.0f}%]'
        f'</span>'
    )


# ===========================================================================
# HTML email builder — inline CSS only, no external dependencies
# ===========================================================================

def build_email_html(
    flagged: List[dict],
    normal:  List[dict],
    run_dt:  datetime,
    threshold: float,
) -> str:
    """
    Construct the full HTML email body as a single string.

    Design principles:
    - Inline CSS only (many email clients strip <style> blocks)
    - Table-based layout for Outlook compatibility
    - No JavaScript, no external images, no webfonts
    - All colours reference the sentiment badge scheme above
    """

    # --- Market-open countdown ---
    # Next 9:30 AM ET — if it's already past 9:30 today, show tomorrow
    et_now  = run_dt.astimezone(ET)
    market_open = et_now.replace(hour=9, minute=30, second=0, microsecond=0)
    if et_now >= market_open:
        market_open += timedelta(days=1)
    delta_min = int((market_open - et_now).total_seconds() / 60)
    countdown_str = f"{delta_min // 60}h {delta_min % 60}m until market open (9:30 AM ET)"

    today_str   = et_now.strftime("%A, %B %-d, %Y")
    run_ts_str  = et_now.strftime("%Y-%m-%d %H:%M %Z")

    # ---- Helper: ticker card ----
    def ticker_card_html(info: dict) -> str:
        raw_ticker = str(info["ticker"])
        ticker   = html.escape(raw_ticker)
        company  = html.escape(COMPANY_NAMES.get(raw_ticker, raw_ticker))
        zscore   = info.get("zscore", 0.0)
        score    = info.get("score", 0.0)
        mu       = info.get("history_mean", 0.0)
        items    = info.get("scored_items", [])

        emoji, label, badge_color = sentiment_badge(zscore)

        # Top 3 items sorted by absolute weighted score (most extreme first)
        top_items = sorted(items, key=lambda x: abs(x.get("score", 0.0)), reverse=True)[:TOP_HEADLINES_IN_EMAIL]

        headlines_html = ""
        for item in top_items:
            source_type = "📰" if item.get("type") == "news" else "📄"
            item_score  = item.get("score", 0.0)
            score_color = "#1a7a2e" if item_score > 0 else ("#cf222e" if item_score < 0 else "#555")
            badge       = finbert_badge_html(item.get("probs", {})) if item.get("probs") else ""
            # Truncate headline for display, escaping HTML to prevent XSS
            raw_text = item.get('text') or ''
            truncated_text = raw_text[:160] + ("…" if len(raw_text) > 160 else "")
            headline_text = html.escape(truncated_text)
            headlines_html += f"""
            <tr>
              <td style="padding:6px 0;border-bottom:1px solid #f0f0f0;vertical-align:top;">
                <span style="font-size:13px;color:#333;">{source_type} {headline_text}</span><br>
                <span style="font-size:11px;color:#888;">Source: {html.escape(item.get('source') or '—')} &nbsp;</span>
                <span style="font-size:12px;font-weight:bold;color:{score_color};">score: {item_score:+.3f}</span>
                &nbsp;{badge}
              </td>
            </tr>"""

        card = f"""
        <table width="100%" cellpadding="0" cellspacing="0" style="
            border:1px solid #e0e0e0;
            border-radius:8px;
            margin-bottom:20px;
            background:#ffffff;
            overflow:hidden;
        ">
          <!-- Card header -->
          <tr>
            <td style="background:#1a1a2e;padding:14px 18px;">
              <span style="font-size:26px;font-weight:900;color:#ffffff;font-family:Arial,sans-serif;">{ticker}</span>
              <span style="font-size:13px;color:#aaa;font-family:Arial,sans-serif;margin-left:10px;">{company}</span>
            </td>
          </tr>
          <!-- Badge row -->
          <tr>
            <td style="padding:12px 18px;background:#f7f7f9;border-bottom:1px solid #e0e0e0;">
              <span style="
                display:inline-block;
                background:{badge_color};
                color:#fff;
                font-weight:bold;
                font-size:13px;
                padding:4px 12px;
                border-radius:12px;
                font-family:Arial,sans-serif;
              ">{emoji} {label}</span>
            </td>
          </tr>
          <!-- Stats row -->
          <tr>
            <td style="padding:12px 18px;">
              <table cellpadding="0" cellspacing="0">
                <tr>
                  <td style="padding-right:30px;">
                    <div style="font-size:11px;color:#888;font-family:Arial,sans-serif;">Z-SCORE</div>
                    <div style="font-size:22px;font-weight:bold;color:{badge_color};font-family:Arial,sans-serif;">{zscore:+.2f}</div>
                  </td>
                  <td style="padding-right:30px;">
                    <div style="font-size:11px;color:#888;font-family:Arial,sans-serif;">TODAY'S SCORE</div>
                    <div style="font-size:22px;font-weight:bold;color:#333;font-family:Arial,sans-serif;">{score:+.4f}</div>
                  </td>
                  <td>
                    <div style="font-size:11px;color:#888;font-family:Arial,sans-serif;">30-DAY MEAN</div>
                    <div style="font-size:22px;font-weight:bold;color:#333;font-family:Arial,sans-serif;">{mu:+.4f}</div>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          <!-- Top headlines -->
          <tr>
            <td style="padding:0 18px 14px 18px;">
              <div style="font-size:12px;font-weight:bold;color:#555;font-family:Arial,sans-serif;margin-bottom:6px;">TOP TRIGGERING ITEMS</div>
              <table width="100%" cellpadding="0" cellspacing="0">
                {headlines_html}
              </table>
            </td>
          </tr>
        </table>
        """
        return card

    # ---- Helper: summary table row ----
    def summary_row_html(info: dict) -> str:
        raw_ticker = str(info["ticker"])
        ticker   = html.escape(raw_ticker)
        company  = html.escape(COMPANY_NAMES.get(raw_ticker, raw_ticker))
        score    = info.get("score", 0.0)
        zscore   = info.get("zscore")
        status   = info.get("status", "normal")

        if zscore is None:
            z_str   = "N/A"
            z_color = "#888"
            st_str  = "Insufficient history"
            st_bg   = "#f0f0f0"
        elif status == "flagged":
            z_str   = f"{zscore:+.2f}"
            z_color = "#1a7a2e" if zscore > 0 else "#cf222e"
            st_str  = "⚠️ FLAGGED"
            st_bg   = "#fff3cd"
        else:
            z_str   = f"{zscore:+.2f}"
            z_color = "#333"
            st_str  = "✅ Normal"
            st_bg   = "#ffffff"

        company = html.escape(COMPANY_NAMES.get(raw_ticker, raw_ticker))
        return f"""
          <tr style="background:{st_bg};">
            <td style="padding:8px 12px;font-weight:bold;font-family:Arial,sans-serif;font-size:13px;">{ticker}</td>
            <td style="padding:8px 12px;font-family:Arial,sans-serif;font-size:13px;">{company}</td>
            <td style="padding:8px 12px;font-family:monospace;font-size:13px;">{score:+.4f}</td>
            <td style="padding:8px 12px;font-family:monospace;font-size:13px;color:{z_color};font-weight:bold;">{z_str}</td>
            <td style="padding:8px 12px;font-family:Arial,sans-serif;font-size:13px;">{st_str}</td>
          </tr>"""

    # ---- Assemble all flagged cards ----
    cards_html = ""
    for info in flagged:
        cards_html += ticker_card_html(info)

    # ---- "No action required" notice ----
    normal_tickers = [html.escape(str(i["ticker"])) for i in normal if i.get("status") == "normal"]
    if normal_tickers:
        safe_normal_tickers = [html.escape(t) for t in normal_tickers]
        normal_notice = f"""
        <p style="font-size:13px;color:#555;font-family:Arial,sans-serif;margin-top:0;">
          <strong>No action required:</strong> {', '.join(safe_normal_tickers)} are within normal range (|z| &lt; {threshold}).
        </p>"""
    else:
        normal_notice = ""

    # ---- Full watchlist summary table ----
    all_results = flagged + normal
    # Sort alphabetically for the summary table
    all_results_sorted = sorted(all_results, key=lambda x: x["ticker"])
    summary_rows = "".join(summary_row_html(info) for info in all_results_sorted)

    summary_table = f"""
    <table width="100%" cellpadding="0" cellspacing="0" style="
        border-collapse:collapse;
        border:1px solid #e0e0e0;
        border-radius:6px;
        overflow:hidden;
        margin-top:8px;
    ">
      <thead>
        <tr style="background:#1a1a2e;color:#fff;">
          <th style="padding:10px 12px;text-align:left;font-family:Arial,sans-serif;font-size:12px;">TICKER</th>
          <th style="padding:10px 12px;text-align:left;font-family:Arial,sans-serif;font-size:12px;">COMPANY</th>
          <th style="padding:10px 12px;text-align:left;font-family:Arial,sans-serif;font-size:12px;">TODAY SCORE</th>
          <th style="padding:10px 12px;text-align:left;font-family:Arial,sans-serif;font-size:12px;">Z-SCORE</th>
          <th style="padding:10px 12px;text-align:left;font-family:Arial,sans-serif;font-size:12px;">STATUS</th>
        </tr>
      </thead>
      <tbody>
        {summary_rows}
      </tbody>
    </table>
    """

    n_flagged = len(flagged)
    subject_preview = f"{n_flagged} stock{'s' if n_flagged != 1 else ''} flagged"

    email_html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Pre-Market Sentiment Alert</title>
</head>
<body style="margin:0;padding:0;background:#f4f4f8;font-family:Arial,Helvetica,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f8;padding:30px 0;">
    <tr>
      <td align="center">
        <table width="620" cellpadding="0" cellspacing="0" style="max-width:620px;width:100%;">

          <!-- Header banner -->
          <tr>
            <td style="
              background:linear-gradient(135deg,#1a1a2e 0%,#16213e 60%,#0f3460 100%);
              padding:28px 30px 22px 30px;
              border-radius:10px 10px 0 0;
            ">
              <div style="font-size:22px;font-weight:900;color:#ffffff;">
                🔔 Pre-Market Sentiment Alert
              </div>
              <div style="font-size:14px;color:#94a3b8;margin-top:6px;">{today_str}</div>
              <div style="font-size:13px;color:#64748b;margin-top:2px;">
                Run at {run_ts_str} &nbsp;·&nbsp; {countdown_str}
              </div>
              <div style="
                display:inline-block;
                background:#e74c3c;
                color:#fff;
                font-size:13px;
                font-weight:bold;
                padding:5px 14px;
                border-radius:20px;
                margin-top:12px;
              ">{subject_preview}</div>
            </td>
          </tr>

          <!-- Body -->
          <tr>
            <td style="background:#f4f4f8;padding:24px 10px 0 10px;">
              <p style="font-size:14px;color:#444;margin:0 0 18px 0;">
                The following tickers crossed the |z| ≥ {threshold} sentiment threshold
                based on {DEFAULT_LOOKBACK_DAYS}-day NewsAPI headlines and SEC EDGAR filings
                scored by <strong>FinBERT</strong>.
              </p>

              {cards_html}

              {normal_notice}

              <!-- Full summary table -->
              <h3 style="font-size:13px;font-weight:bold;color:#555;letter-spacing:1px;text-transform:uppercase;margin-bottom:6px;">
                Full Watchlist Summary
              </h3>
              {summary_table}
            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="
              background:#1a1a2e;
              padding:16px 30px;
              margin-top:20px;
              border-radius:0 0 10px 10px;
            ">
              <p style="font-size:11px;color:#64748b;margin:0;">
                Generated by monday-sentiment/sentiment_cron.py &nbsp;·&nbsp;
                Model: ProsusAI/finbert &nbsp;·&nbsp;
                Threshold: |z| ≥ {threshold} &nbsp;·&nbsp;
                This is not investment advice.
              </p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""
    return email_html_content


# ===========================================================================
# Email sending
# ===========================================================================

def send_email(
    html_body: str,
    subject: str,
    smtp_user: str,
    smtp_password: str,
    recipient: str,
    smtp_host: str = "smtp.gmail.com",
    smtp_port: int = 587,
) -> bool:
    """
    Send an HTML email via SMTP with STARTTLS.
    Returns True on success, False on failure.
    Failure is logged but does NOT raise — the cron job should not fail
    just because an email couldn't be delivered.
    """
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = smtp_user
    msg["To"]      = recipient

    # Attach a plain-text fallback for email clients that don't render HTML
    plain_fallback = (
        "Your email client does not support HTML. "
        "Please view this email in a modern client."
    )
    msg.attach(MIMEText(plain_fallback, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
            server.login(smtp_user, smtp_password)
            server.sendmail(smtp_user, recipient, msg.as_string())
        log.info("Email sent to %s via %s:%d", recipient, smtp_host, smtp_port)
        return True
    except smtplib.SMTPAuthenticationError as exc:
        safe_exc = str(exc)
        if smtp_password:
            safe_exc = safe_exc.replace(smtp_password, "***")
        log.error("SMTP authentication failed: %s", safe_exc)
    except smtplib.SMTPException as exc:
        safe_exc = str(exc)
        if smtp_password:
            safe_exc = safe_exc.replace(smtp_password, "***")
        log.error("SMTP error: %s", safe_exc)
    except OSError as exc:
        safe_exc = str(exc)
        if smtp_password:
            safe_exc = safe_exc.replace(smtp_password, "***")
        log.error("Network error sending email: %s", safe_exc)
    return False


# ===========================================================================
# Main orchestration
# ===========================================================================

def run(args: argparse.Namespace) -> int:
    """
    Main pipeline:
      1. Load FinBERT
      2. Load history
      3. For each ticker: collect data → score → z-score
      4. Update + save history
      5. Filter tickers by threshold
      6. If none flagged: log and exit 0
      7. Build HTML email
      8. Send (or --dry-run print)
    Returns exit code (0 = success).
    """
    run_dt  = datetime.now(timezone.utc)
    today_str = run_dt.astimezone(ET).strftime("%Y-%m-%d")

    tickers    = [t.upper().strip() for t in args.tickers]
    threshold  = args.threshold
    lookback   = args.days
    hist_path  = args.history
    dry_run    = args.dry_run

    log.info("=== Monday Sentiment Cron starting ===")
    log.info("Watchlist : %s", ", ".join(tickers))
    log.info("Threshold : |z| >= %.1f", threshold)
    log.info("Lookback  : %d days", lookback)
    log.info("History   : %s", hist_path)
    log.info("Dry-run   : %s", dry_run)

    # ------------------------------------------------------------------
    # Step 1: Load FinBERT (fails fast with exit 1 if unavailable)
    # ------------------------------------------------------------------
    pipe = load_finbert_pipeline()

    # ------------------------------------------------------------------
    # Step 2: Load history
    # ------------------------------------------------------------------
    history = load_history(hist_path)
    history = prune_history(history, today_str)

    # ------------------------------------------------------------------
    # Step 3: NewsAPI key (optional)
    # ------------------------------------------------------------------
    news_api_key = os.environ.get("NEWS_API_KEY")
    if not news_api_key:
        log.warning(
            "NEWS_API_KEY not set — running with SEC EDGAR only. "
            "Set the env var for richer signal."
        )

    # ------------------------------------------------------------------
    # Step 4: Score each ticker
    # ------------------------------------------------------------------
    results: Dict[str, dict] = {}

    async def process_all_tickers():
        # Bound concurrent SEC EDGAR / NewsAPI requests so a large watchlist
        # does not create too many simultaneous in-flight calls.
        sem = asyncio.Semaphore(MAX_CONCURRENT_TICKERS)
        # Separately, pace the actual EDGAR request rate — the semaphore
        # above only caps how many ticker tasks run at once, and with fast
        # responses that lets far more than SEC_MAX_REQUESTS_PER_SECOND
        # requests start within a second (codex review discussion_r3742435139).
        # One limiter instance is shared by every ticker task in this run.
        sec_rate_limiter = SecRateLimiter(SEC_MAX_REQUESTS_PER_SECOND)

        async def _run_one(session: aiohttp.ClientSession, ticker: str):
            async with sem:
                log.info("[%s] Queuing…", ticker)
                return await compute_ticker_score(session, ticker, pipe, news_api_key, lookback, sec_rate_limiter)

        async with aiohttp.ClientSession() as session:
            tasks = [_run_one(session, ticker) for ticker in tickers]
            return await asyncio.gather(*tasks, return_exceptions=True)

    scored_results = asyncio.run(process_all_tickers())

    for ticker, score_res in zip(tickers, scored_results, strict=True):
        if isinstance(score_res, BaseException):
            safe_exc = _sanitize_exception(score_res)
            # Stack frames only (traceback.format_tb), never format_exception
            # -- the latter's final line is the exception's own str(), which
            # is exactly the unredacted message _sanitize_exception exists to
            # suppress.
            safe_traceback = "".join(traceback.format_tb(score_res.__traceback__))
            if news_api_key:
                safe_exc = safe_exc.replace(news_api_key, "***")
                safe_traceback = safe_traceback.replace(news_api_key, "***")
            log.error("[%s] Unexpected error during scoring: %s", ticker, safe_exc)
            log.debug("Exception details (stack only):\n%s", safe_traceback)
            score, scored_items = 0.0, []
        else:
            score, scored_items = score_res

        # Update history *before* computing z so today's score is in the file
        # but we exclude today from the reference population in compute_zscore().
        history = update_history(history, ticker, today_str, score)

        zscore = compute_zscore(history, ticker, score)

        # Compute history mean for display in email
        hist_entries = history.get(ticker, [])
        hist_scores  = [e["score"] for e in hist_entries if e.get("date") != today_str]
        hist_mean    = mean(hist_scores) if hist_scores else 0.0

        results[ticker] = {
            "ticker":        ticker,
            "score":         score,
            "zscore":        zscore,
            "history_mean":  hist_mean,
            "scored_items":  scored_items,
        }

    # ------------------------------------------------------------------
    # Step 5: Save updated history (always, even if no email is sent)
    # ------------------------------------------------------------------
    save_history(history, hist_path)

    # ------------------------------------------------------------------
    # Step 6: Filter and rank
    # ------------------------------------------------------------------
    flagged, normal = filter_and_rank(results, threshold, MAX_FLAGGED_TICKERS)

    if not flagged:
        log.info("No significant sentiment shifts detected (threshold |z| >= %.1f) — skipping email.", threshold)
        return 0

    log.info("%d ticker(s) crossed threshold: %s", len(flagged), [f["ticker"] for f in flagged])

    # ------------------------------------------------------------------
    # Step 7: Build email
    # ------------------------------------------------------------------
    n       = len(flagged)
    subject = (
        f"🔔 Pre-Market Sentiment Alert — "
        f"{run_dt.astimezone(ET).strftime('%Y-%m-%d')} | "
        f"{n} stock{'s' if n != 1 else ''} flagged"
    )
    html_body = build_email_html(flagged, normal, run_dt, threshold)

    # ------------------------------------------------------------------
    # Step 8: Send or dry-run
    # ------------------------------------------------------------------
    if dry_run:
        log.info("--- DRY RUN: printing HTML email to stdout ---")
        print(html_body)
        log.info("--- END DRY RUN ---")
        return 0

    # Validate required SMTP environment variables
    smtp_user   = os.environ.get("SMTP_USER")
    smtp_pass   = os.environ.get("SMTP_PASSWORD")
    recipient   = os.environ.get("RECIPIENT_EMAIL")
    smtp_host   = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port_s = os.environ.get("SMTP_PORT", "587")

    if not (smtp_user and smtp_pass and recipient):
        log.error(
            "Missing SMTP environment variables. "
            "Set SMTP_USER, SMTP_PASSWORD, RECIPIENT_EMAIL."
        )
        # Don't exit 1 — the analysis ran fine; email is best-effort
        return 0

    try:
        smtp_port = int(smtp_port_s)
    except ValueError:
        log.warning("Invalid SMTP_PORT '%s'; defaulting to 587", smtp_port_s)
        smtp_port = 587

    success = send_email(
        html_body  = html_body,
        subject    = subject,
        smtp_user  = smtp_user,
        smtp_password = smtp_pass,
        recipient  = recipient,
        smtp_host  = smtp_host,
        smtp_port  = smtp_port,
    )

    if not success:
        # Log but return 0 so cron doesn't report job failure over an email issue
        log.error("Email delivery failed — see above. Exiting cleanly (0) to avoid cron noise.")

    return 0


# ===========================================================================
# CLI entry point
# ===========================================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Monday morning pre-market sentiment monitor with z-score alerting.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Normal cron run
  python sentiment_cron.py

  # Override watchlist and threshold
  python sentiment_cron.py --tickers NVDA AMD INTC --threshold 1.5

  # Dry-run: print email to stdout, skip sending
  python sentiment_cron.py --dry-run

  # Use a custom history file path
  python sentiment_cron.py --history /data/sentiment/history.json
        """,
    )

    parser.add_argument(
        "--tickers",
        nargs="+",
        default=DEFAULT_WATCHLIST,
        metavar="TICKER",
        help=f"Watchlist tickers (default: {' '.join(DEFAULT_WATCHLIST)})",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        metavar="Z",
        help=f"Z-score magnitude threshold to trigger email (default: {DEFAULT_THRESHOLD})",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_LOOKBACK_DAYS,
        metavar="N",
        help=f"News lookback window in days (default: {DEFAULT_LOOKBACK_DAYS})",
    )
    parser.add_argument(
        "--history",
        default=DEFAULT_HISTORY_PATH,
        metavar="PATH",
        help=f"Path to rolling sentiment history JSON (default: {DEFAULT_HISTORY_PATH})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print HTML email body to stdout; do not send via SMTP",
    )

    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    sys.exit(run(args))
