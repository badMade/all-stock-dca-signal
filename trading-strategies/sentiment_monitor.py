"""
task2_sentiment.py
==================
NLP-based stock sentiment monitoring and daily email briefing.

Requirements:
    pip install transformers torch requests pandas numpy jinja2

Environment variables:
    NEWS_API_KEY      - NewsAPI.org API key
    SMTP_HOST         - SMTP server hostname (default: smtp.gmail.com)
    SMTP_PORT         - SMTP server port (default: 587)
    SMTP_USER         - SMTP login username / sender address
    SMTP_PASSWORD     - SMTP login password (or app password)
    RECIPIENT_EMAIL   - Destination email address
    WATCHLIST         - Comma-separated ticker list (overridden by --tickers)

Usage examples:
    python task2_sentiment.py
    python task2_sentiment.py --tickers AAPL MSFT GOOGL --days 5 --dry-run
    python task2_sentiment.py --threshold 2.0 --dry-run
"""

# ---------------------------------------------------------------------------
# Standard library imports
# ---------------------------------------------------------------------------
import argparse
import concurrent.futures
import json
import logging
import os
import smtplib
import sys
import traceback
import urllib.error
from datetime import date, datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Third-party imports
# ---------------------------------------------------------------------------
import numpy as np
import requests
try:
    import torch  # type: ignore
    from jinja2 import Environment  # type: ignore
    from transformers import AutoModelForSequenceClassification, AutoTokenizer, pipeline  # type: ignore
except ImportError:
    torch = None  # type: ignore
    Environment = None  # type: ignore
    AutoModelForSequenceClassification = None  # type: ignore
    AutoTokenizer = None  # type: ignore
    pipeline = None  # type: ignore

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("sentiment_monitor")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
HISTORY_FILE = Path("./sentiment_history.json")
FINBERT_MODEL = "ProsusAI/finbert"
DEFAULT_TICKERS = ["AAPL", "TSLA", "NVDA", "META", "AMZN"]
DEFAULT_DAYS = 7
DEFAULT_THRESHOLD = 1.5
SEC_EDGAR_URL = "https://efts.sec.gov/LATEST/search-index"
NEWSAPI_PAGE_SIZE = 100  # max per NewsAPI request

# ---------------------------------------------------------------------------
# Jinja2 HTML email template (defined as a module-level constant)
# ---------------------------------------------------------------------------
EMAIL_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Daily Sentiment Briefing</title>
  <style>
    body {
      font-family: 'Segoe UI', Arial, sans-serif;
      background-color: #f4f6f9;
      color: #333;
      margin: 0;
      padding: 0;
    }
    .container {
      max-width: 760px;
      margin: 24px auto;
      background: #ffffff;
      border-radius: 8px;
      box-shadow: 0 2px 8px rgba(0,0,0,0.08);
      overflow: hidden;
    }
    .header {
      background: linear-gradient(135deg, #1a237e 0%, #283593 100%);
      color: white;
      padding: 28px 32px;
    }
    .header h1 {
      margin: 0;
      font-size: 22px;
      font-weight: 700;
      letter-spacing: 0.3px;
    }
    .header .subtitle {
      margin: 6px 0 0;
      opacity: 0.85;
      font-size: 13px;
    }
    .section-title {
      font-size: 15px;
      font-weight: 700;
      color: #1a237e;
      margin: 0 0 12px;
      padding-bottom: 6px;
      border-bottom: 2px solid #e8eaf6;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }
    .section {
      padding: 24px 32px;
      border-bottom: 1px solid #f0f0f0;
    }
    .no-shift-note {
      background: #e8f5e9;
      border-left: 4px solid #43a047;
      color: #2e7d32;
      padding: 12px 16px;
      border-radius: 4px;
      font-size: 14px;
    }
    /* Ticker cards */
    .ticker-card {
      border-radius: 6px;
      padding: 18px 20px;
      margin-bottom: 16px;
      border-left: 5px solid #ccc;
    }
    .ticker-card.bullish {
      background: #f1f8e9;
      border-left-color: #43a047;
    }
    .ticker-card.bearish {
      background: #fce4ec;
      border-left-color: #e53935;
    }
    .ticker-card.neutral {
      background: #f5f5f5;
      border-left-color: #9e9e9e;
    }
    .ticker-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 10px;
    }
    .ticker-symbol {
      font-size: 20px;
      font-weight: 800;
      letter-spacing: 0.5px;
    }
    .ticker-symbol.bullish { color: #2e7d32; }
    .ticker-symbol.bearish { color: #c62828; }
    .ticker-symbol.neutral { color: #616161; }
    .direction-badge {
      display: inline-block;
      padding: 4px 10px;
      border-radius: 12px;
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }
    .direction-badge.bullish { background: #43a047; color: white; }
    .direction-badge.bearish { background: #e53935; color: white; }
    .direction-badge.neutral { background: #9e9e9e; color: white; }
    .ticker-metrics {
      display: flex;
      gap: 24px;
      margin-bottom: 12px;
    }
    .metric {
      text-align: center;
    }
    .metric-value {
      font-size: 18px;
      font-weight: 700;
      display: block;
    }
    .metric-label {
      font-size: 11px;
      color: #757575;
      text-transform: uppercase;
      letter-spacing: 0.4px;
    }
    .headlines-label {
      font-size: 12px;
      font-weight: 700;
      color: #555;
      margin: 10px 0 6px;
      text-transform: uppercase;
      letter-spacing: 0.4px;
    }
    .headline-item {
      font-size: 13px;
      padding: 6px 8px;
      background: rgba(255,255,255,0.6);
      border-radius: 4px;
      margin-bottom: 4px;
      display: flex;
      align-items: flex-start;
      gap: 6px;
    }
    .headline-sentiment {
      font-size: 11px;
      font-weight: 700;
      min-width: 56px;
      padding: 2px 6px;
      border-radius: 3px;
      text-align: center;
    }
    .sent-pos { background: #c8e6c9; color: #1b5e20; }
    .sent-neg { background: #ffcdd2; color: #b71c1c; }
    .sent-neu { background: #eeeeee; color: #424242; }
    .headline-source {
      font-size: 11px;
      color: #78909c;
      font-style: italic;
    }
    /* Summary table */
    table.summary {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }
    table.summary th {
      background: #e8eaf6;
      color: #1a237e;
      padding: 9px 12px;
      text-align: left;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.4px;
      font-size: 11px;
    }
    table.summary td {
      padding: 9px 12px;
      border-bottom: 1px solid #f0f0f0;
    }
    table.summary tr:last-child td { border-bottom: none; }
    table.summary tr:hover td { background: #f9fbe7; }
    .z-alert { font-weight: 700; }
    .z-bull { color: #2e7d32; }
    .z-bear { color: #c62828; }
    .footer {
      background: #fafafa;
      padding: 16px 32px;
      font-size: 11px;
      color: #9e9e9e;
      text-align: center;
    }
    .pill-shift {
      display: inline-block;
      padding: 2px 8px;
      border-radius: 10px;
      font-size: 11px;
      font-weight: 700;
    }
    .pill-yes-bull { background: #c8e6c9; color: #1b5e20; }
    .pill-yes-bear { background: #ffcdd2; color: #b71c1c; }
    .pill-no { background: #eeeeee; color: #616161; }
  </style>
</head>
<body>
<div class="container">

  <!-- Header -->
  <div class="header">
    <h1>Daily Sentiment Briefing &mdash; {{ report_date }}</h1>
    <div class="subtitle">
      Watchlist: {{ tickers | join(', ') }} &nbsp;|&nbsp;
      {{ alert_count }} significant alert{{ 's' if alert_count != 1 else '' }}
      (|z| &ge; {{ threshold }})
    </div>
  </div>

  <!-- Top movers section -->
  <div class="section">
    <div class="section-title">Top Sentiment Shifts</div>

    {% if top_movers %}
      {% for item in top_movers %}
      <div class="ticker-card {{ item.direction }}">
        <div class="ticker-header">
          <span class="ticker-symbol {{ item.direction }}">{{ item.ticker }}</span>
          <span class="direction-badge {{ item.direction }}">{{ item.direction }}</span>
        </div>
        <div class="ticker-metrics">
          <div class="metric">
            <span class="metric-value">{{ "%.3f" | format(item.sentiment_score) }}</span>
            <span class="metric-label">Sentiment Score</span>
          </div>
          <div class="metric">
            <span class="metric-value">{{ "%+.2f" | format(item.zscore) }}</span>
            <span class="metric-label">Z-Score</span>
          </div>
          <div class="metric">
            <span class="metric-value">{{ item.headline_count }}</span>
            <span class="metric-label">Headlines</span>
          </div>
        </div>
        {% if item.top_headlines %}
        <div class="headlines-label">Top triggering headlines</div>
        {% for h in item.top_headlines %}
        <div class="headline-item">
          <span class="headline-sentiment sent-{{ h.label_short }}">{{ h.label }}</span>
          <span>
            {% if h.url %}<a href="{{ h.url }}" style="color:inherit;text-decoration:underline;" target="_blank" rel="noopener noreferrer">{{ h.title }}</a>{% else %}{{ h.title }}{% endif %}
            <br><span class="headline-source">{{ h.source }}</span>
          </span>
        </div>
        {% endfor %}
        {% endif %}
      </div>
      {% endfor %}
    {% else %}
      <div class="no-shift-note">
        No significant sentiment shifts detected today (|z-score| &lt; {{ threshold }}).
        All tickers within normal range.
      </div>
    {% endif %}
  </div>

  <!-- Full watchlist summary -->
  <div class="section">
    <div class="section-title">Full Watchlist Summary</div>
    <table class="summary">
      <thead>
        <tr>
          <th>Ticker</th>
          <th>Sentiment Score</th>
          <th>30d Avg</th>
          <th>Z-Score</th>
          <th>Direction</th>
          <th>Headlines</th>
          <th>Significant?</th>
        </tr>
      </thead>
      <tbody>
        {% for row in summary_rows %}
        <tr>
          <td><strong>{{ row.ticker }}</strong></td>
          <td>{{ "%.4f" | format(row.sentiment_score) }}</td>
          <td>{{ "%.4f" | format(row.mean_30d) if row.mean_30d is not none else "N/A" }}</td>
          <td class="z-alert {{ 'z-bull' if row.zscore > 0 else 'z-bear' if row.zscore < 0 else '' }}">
            {{ "%+.2f" | format(row.zscore) if row.zscore is not none else "N/A" }}
          </td>
          <td>
            <span class="direction-badge {{ row.direction }}">{{ row.direction }}</span>
          </td>
          <td>{{ row.headline_count }}</td>
          <td>
            {% if row.is_significant %}
              {% if row.zscore > 0 %}
                <span class="pill-shift pill-yes-bull">&#8679; BULLISH</span>
              {% else %}
                <span class="pill-shift pill-yes-bear">&#8681; BEARISH</span>
              {% endif %}
            {% else %}
              <span class="pill-shift pill-no">Normal</span>
            {% endif %}
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>

  <div class="footer">
    Generated {{ generated_at }} &nbsp;|&nbsp;
    Data: NewsAPI + SEC EDGAR &nbsp;|&nbsp;
    NLP Model: ProsusAI/finbert
  </div>
</div>
</body>
</html>
"""


# ===========================================================================
# Class: SentimentAnalyzer
# ===========================================================================
class SentimentAnalyzer:
    """
    Wraps the ProsusAI/finbert HuggingFace model for financial sentiment analysis.

    The model is loaded once at construction time and reused for all inference
    calls, avoiding repeated disk I/O and GPU/CPU memory allocations.

    Label mapping for FinBERT:
        LABEL_0 → positive
        LABEL_1 → negative
        LABEL_2 → neutral
    (FinBERT outputs these labels directly by name as well.)
    """

    # FinBERT max token length; truncate inputs to avoid OOM on long texts.
    MAX_LENGTH = 512

    def __init__(self, model_name: str = FINBERT_MODEL):
        logger.info("Loading FinBERT model '%s' — this may take a moment…", model_name)
        self.tokenizer = AutoTokenizer.from_pretrained(model_name)  # type: ignore
        self.model = AutoModelForSequenceClassification.from_pretrained(model_name)  # type: ignore
        self.model.eval()
        # Use GPU if available, otherwise CPU
        self.device = 0 if (torch and torch.cuda.is_available()) else -1  # type: ignore
        self._pipeline = pipeline(  # type: ignore
            "text-classification",
            model=self.model,
            tokenizer=self.tokenizer,
            device=self.device,
            truncation=True,
            max_length=self.MAX_LENGTH,
            top_k=None,          # return scores for all 3 labels
        )
        logger.info("FinBERT loaded on device=%s", "cuda" if self.device == 0 else "cpu")

    def score_texts(self, texts: List[str]) -> List[Dict]:
        """
        Score a batch of text strings with FinBERT.

        Args:
            texts: List of headline / filing snippet strings.

        Returns:
            List of dicts with keys:
                text        — original text
                positive    — probability for positive sentiment
                negative    — probability for negative sentiment
                neutral     — probability for neutral sentiment
                label       — dominant label ('positive'/'negative'/'neutral')
                confidence  — probability of the dominant label
                score       — (positive - negative) * confidence  [−1, +1]
        """
        if not texts:
            return []

        # Batch inference — HuggingFace pipeline handles batching internally
        raw_results = self._pipeline(texts)

        output = []
        for text, label_scores in zip(texts, raw_results):
            # label_scores is a list of {'label': str, 'score': float}
            score_map = {item["label"].lower(): item["score"] for item in label_scores}
            pos = score_map.get("positive", 0.0)
            neg = score_map.get("negative", 0.0)
            neu = score_map.get("neutral", 0.0)

            # Determine dominant label
            dominant = max(score_map, key=lambda k: score_map[k])
            confidence = score_map[dominant]

            # Composite score: direction * confidence
            composite = (pos - neg) * confidence

            output.append({
                "text": text,
                "positive": pos,
                "negative": neg,
                "neutral": neu,
                "label": dominant,
                "confidence": confidence,
                "score": composite,
            })
        return output

    def compute_ticker_sentiment(self, scored_items: List[Dict]) -> float:
        """
        Compute a single weighted sentiment score for a ticker.

        Weighted average of (positive - negative) * confidence across all items,
        weighted by confidence itself so more certain predictions count more.

        Args:
            scored_items: List of dicts returned by score_texts().

        Returns:
            Weighted sentiment float in [−1, +1], or 0.0 if no items.
        """
        if not scored_items:
            return 0.0
        weights = np.array([item["confidence"] for item in scored_items])
        scores = np.array([item["score"] for item in scored_items])
        total_weight = weights.sum()
        if total_weight == 0:
            return 0.0
        return float(np.dot(weights, scores) / total_weight)


# ===========================================================================
# Class: NewsCollector
# ===========================================================================
class NewsCollector:
    """
    Collects news headlines for a list of tickers using the NewsAPI.

    Requires a valid NEWS_API_KEY environment variable or explicit api_key arg.
    Falls back gracefully (empty list) if the API key is missing or the request
    fails, so the pipeline can still run with SEC data only.
    """

    def __init__(self, api_key: Optional[str] = None, days: int = DEFAULT_DAYS):
        self.api_key = api_key or os.getenv("NEWS_API_KEY", "")
        self.days = days
        if not self.api_key:
            logger.warning(
                "NEWS_API_KEY not set — NewsAPI will be skipped. "
                "Set the env var to enable news headline collection."
            )

    def fetch_headlines(self, ticker: str) -> List[Dict]:
        """
        Fetch up to NEWSAPI_PAGE_SIZE headlines mentioning *ticker* over the
        last self.days days.

        Args:
            ticker: Stock symbol string, e.g. 'AAPL'.

        Returns:
            List of dicts with keys: title, source, published_at, url.
        """
        if not self.api_key:
            return []

        now = datetime.now(timezone.utc)
        from_date = (now - timedelta(days=self.days)).strftime("%Y-%m-%d")
        to_date = now.strftime("%Y-%m-%d")
        params = {
            "q": ticker,
            "from": from_date,
            "to": to_date,
            "language": "en",
            "sortBy": "relevancy",
            "pageSize": NEWSAPI_PAGE_SIZE,
        }
        headers = {
            "X-Api-Key": self.api_key,
            "User-Agent": "all-stock-dca/1.0",
        }

        try:
            resp = requests.get("https://newsapi.org/v2/everything", params=params, headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") != "ok":
                logger.warning("NewsAPI error for %s: %s", ticker, data.get("message", "Unknown error"))
                return []
            articles = data.get("articles") or []
            results = []
            for art in articles:
                title = art.get("title") or ""
                description = art.get("description") or ""
                # Use title + description for richer NLP signal
                combined = f"{title}. {description}".strip(". ")
                url = art.get("url", "")
                if url and not url.lower().startswith(("http://", "https://")):
                    url = ""
                results.append({
                    "title": title or description,
                    "text": combined,
                    "source": art.get("source", {}).get("name", "Unknown"),
                    "published_at": art.get("publishedAt", ""),
                    "url": url,
                })
            logger.info("NewsAPI: %d articles for %s", len(results), ticker)
            return results
        except Exception as exc:
            # self.api_key is the only secret that can appear here (it's sent
            # as a header, never in the URL), so redact it directly rather
            # than collapsing to _sanitize_exception's class-name-only
            # fallback, which drops the redaction marker regression tests
            # rely on (codex review discussion_r3742559536).
            safe_exc = str(exc)
            if self.api_key:
                safe_exc = safe_exc.replace(self.api_key, "***")
            logger.error("NewsAPI error for %s: %s", ticker, safe_exc)
            return []


# ===========================================================================
# Class: SECCollector
# ===========================================================================
class SECCollector:
    """
    Collects recent SEC EDGAR filings (8-K, 10-K, 10-Q) for a given ticker
    using the free SEC EDGAR full-text search API (efts.sec.gov).

    No API key required. Respects SEC's recommended request rate.
    """

    FILING_TYPES = ["8-K", "10-K", "10-Q"]
    MAX_FILINGS = 3  # filings per form type (9 total per ticker)

    @property
    def headers(self) -> dict:
        import os
        return {
            "User-Agent": os.environ.get("SEC_EDGAR_USER_AGENT", "SentimentMonitor/1.0 (contact@example.com)"),
            "Accept-Encoding": "gzip, deflate",
        }

    def fetch_filings(self, ticker: str) -> List[Dict]:
        """
        Fetch the last MAX_FILINGS filings of each FILING_TYPE for a ticker.

        Args:
            ticker: Stock symbol string.

        Returns:
            List of dicts with keys: title, description, form_type, filed_at.
        """
        results = []
        for form_type in self.FILING_TYPES:
            try:
                params = {
                    "q": f'"{ticker}"',
                    "dateRange": "custom",
                    "startdt": (datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%d"),
                    "enddt": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    "forms": form_type,
                    "_source": "period_of_report,file_date,display_names,period_of_report,entity_name",
                }
                url = SEC_EDGAR_URL
                resp = requests.get(
                    url,
                    params=params,
                    headers=self.headers,
                    timeout=15,
                )
                resp.raise_for_status()
                data = resp.json()

                hits = data.get("hits", {}).get("hits", [])
                for hit in hits[: self.MAX_FILINGS]:
                    src = hit.get("_source", {})
                    entity = src.get("entity_name", ticker)
                    period = src.get("period_of_report", "")
                    filed = src.get("file_date", "")
                    title = f"{entity} {form_type} — Period: {period}"
                    description = (
                        f"{entity} filed a {form_type} with the SEC on {filed}. "
                        f"Reporting period: {period}."
                    )
                    results.append({
                        "title": title,
                        "text": description,
                        "source": f"SEC EDGAR ({form_type})",
                        "published_at": filed,
                        "url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={ticker}&type={form_type}",
                    })
            except Exception as exc:
                logger.warning("SEC EDGAR error for %s / %s: %s", ticker, form_type, _sanitize_exception(exc))

        logger.info("SEC EDGAR: %d filing snippets for %s", len(results), ticker)
        return results


# ===========================================================================
# Class: HistoryManager
# ===========================================================================
class HistoryManager:
    """
    Manages a rolling 30-day sentiment history stored in a local JSON file.

    File structure (sentiment_history.json):
    {
        "AAPL": [
            {"date": "2024-01-01", "score": 0.123},
            ...
        ],
        ...
    }

    Provides:
        - load / save  operations
        - appending today's score
        - computing z-score vs 30-day history
    """

    WINDOW_DAYS = 30

    def __init__(self, filepath: Path = HISTORY_FILE):
        self.filepath = filepath
        self.data: Dict[str, List[Dict]] = {}
        self._load()

    def _load(self):
        """Load history from disk, or initialise empty if file doesn't exist."""
        if self.filepath.exists():
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
                logger.info("Loaded sentiment history from %s", self.filepath)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Could not load history file (%s); starting fresh.", exc)
                self.data = {}
        else:
            logger.info("No history file found at %s — will create on save.", self.filepath)
            self.data = {}

    def save(self):
        """Persist current history to disk."""
        with open(self.filepath, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2)
        logger.info("Saved sentiment history to %s", self.filepath)

    def append_score(self, ticker: str, score: float, record_date: Optional[str] = None):
        """
        Append today's sentiment score for a ticker and trim to last 30 days.

        Args:
            ticker:      Ticker symbol.
            score:       Today's weighted sentiment score.
            record_date: Date string (YYYY-MM-DD); defaults to today.
        """
        today_str = record_date or date.today().isoformat()
        if ticker not in self.data:
            self.data[ticker] = []
        # Avoid duplicate entries for the same date (idempotent re-runs)
        self.data[ticker] = [e for e in self.data[ticker] if e["date"] != today_str]
        self.data[ticker].append({"date": today_str, "score": score})
        # Keep only the rolling window
        cutoff = (date.today() - timedelta(days=self.WINDOW_DAYS)).isoformat()
        self.data[ticker] = [e for e in self.data[ticker] if e["date"] >= cutoff]
        # Sort chronologically
        self.data[ticker].sort(key=lambda x: x["date"])

    def compute_zscore(self, ticker: str, today_score: float) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        """
        Compute the z-score of today_score relative to the 30-day history
        (excluding today).

        Returns:
            Tuple of (z_score, mean_30d, std_30d).
            Returns (None, None, None) if fewer than 2 historical data points.
        """
        history = self.data.get(ticker, [])
        today_str = date.today().isoformat()
        past_scores = [e["score"] for e in history if e["date"] != today_str]

        if len(past_scores) < 2:
            logger.info(
                "%s: only %d historical points; z-score skipped.", ticker, len(past_scores)
            )
            return None, None, None

        arr = np.array(past_scores)
        mean = float(arr.mean())
        std = float(arr.std(ddof=1))  # sample std
        if std == 0:
            return 0.0, mean, 0.0
        z = (today_score - mean) / std
        return z, mean, std

    def get_history_scores(self, ticker: str) -> List[float]:
        """Return list of all stored scores for a ticker (for diagnostics)."""
        return [e["score"] for e in self.data.get(ticker, [])]


# ===========================================================================
# Class: EmailBriefing
# ===========================================================================
class EmailBriefing:
    """
    Builds and sends (or prints) the HTML daily sentiment email.

    SMTP credentials are read from environment variables:
        SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, RECIPIENT_EMAIL

    The HTML template is rendered with Jinja2, then sent via smtplib.
    """

    def __init__(self):
        self.smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
        self.smtp_port = int(os.getenv("SMTP_PORT", "587"))
        self.smtp_user = os.getenv("SMTP_USER", "")
        self.smtp_password = os.getenv("SMTP_PASSWORD", "")
        self.recipient = os.getenv("RECIPIENT_EMAIL", "")

    def build_html(
        self,
        tickers: List[str],
        results: List[Dict],
        threshold: float,
    ) -> str:
        """
        Render the Jinja2 HTML email template with the analysis results.

        Args:
            tickers:   Ordered list of all tickers in the watchlist.
            results:   List of per-ticker result dicts (from pipeline).
            threshold: Z-score threshold used for alerting.

        Returns:
            Rendered HTML string.
        """
        today_str = date.today().strftime("%B %d, %Y")
        generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        # Sort by |z-score| descending for "top movers"
        def sort_key(r):
            z = r.get("zscore")
            return abs(z) if z is not None else 0.0

        sorted_results = sorted(results, key=sort_key, reverse=True)
        top_movers = [r for r in sorted_results if r.get("is_significant", False)][:3]

        alert_count = len([r for r in results if r.get("is_significant", False)])

        # Build top-movers context dicts
        top_movers_ctx = []
        for r in top_movers:
            z = r.get("zscore", 0.0) or 0.0
            direction = _direction(r["sentiment_score"], z)
            top_headlines = []
            for h in r.get("top_headlines", [])[:3]:
                label = h.get("label", "neutral")
                top_headlines.append({
                    "title": h.get("title", ""),
                    "source": h.get("source", ""),
                    "label": label,
                    "label_short": label[:3],  # pos/neg/neu for CSS class
                    "url": h.get("url", ""),
                })
            top_movers_ctx.append({
                "ticker": r["ticker"],
                "sentiment_score": r["sentiment_score"],
                "zscore": z,
                "direction": direction,
                "headline_count": r.get("headline_count", 0),
                "top_headlines": top_headlines,
            })

        # Build summary-table rows for all tickers
        summary_rows = []
        for ticker in tickers:
            r = next((x for x in results if x["ticker"] == ticker), None)
            if r is None:
                continue
            z = r.get("zscore")
            direction = _direction(r["sentiment_score"], z)
            summary_rows.append({
                "ticker": ticker,
                "sentiment_score": r["sentiment_score"],
                "mean_30d": r.get("mean_30d"),
                "zscore": z,
                "direction": direction,
                "headline_count": r.get("headline_count", 0),
                "is_significant": r.get("is_significant", False),
            })

        template = Environment(autoescape=True).from_string(EMAIL_TEMPLATE)  # type: ignore
        html = template.render(
            report_date=today_str,
            tickers=tickers,
            alert_count=alert_count,
            threshold=threshold,
            top_movers=top_movers_ctx,
            summary_rows=summary_rows,
            generated_at=generated_at,
        )
        return html

    def build_subject(self, results: List[Dict]) -> str:
        """Build the email subject line with date and alert count."""
        today_str = date.today().strftime("%Y-%m-%d")
        n_alerts = len([r for r in results if r.get("is_significant", False)])
        return f"Daily Sentiment Briefing — {today_str} | {n_alerts} alert{'s' if n_alerts != 1 else ''}"

    def send(self, subject: str, html_body: str, dry_run: bool = False):
        """
        Send the HTML email via SMTP, or print it to stdout in dry-run mode.

        Args:
            subject:  Email subject line.
            html_body: Rendered HTML content.
            dry_run:  If True, print to stdout instead of sending.
        """
        if dry_run:
            print("=" * 72)
            print(f"DRY RUN — Subject: {subject}")
            print("=" * 72)
            print(html_body)
            return

        if not self.smtp_user or not self.recipient:
            logger.error(
                "SMTP_USER or RECIPIENT_EMAIL not set — cannot send email. "
                "Use --dry-run to preview the email."
            )
            return

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self.smtp_user
        msg["To"] = self.recipient
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        try:
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30) as server:
                server.ehlo()
                server.starttls()
                server.login(self.smtp_user, self.smtp_password)
                server.sendmail(self.smtp_user, [self.recipient], msg.as_string())
            logger.info("Email sent to %s via %s:%d", self.recipient, self.smtp_host, self.smtp_port)
        except smtplib.SMTPException as exc:
            safe_exc = str(exc)
            if self.smtp_password:
                safe_exc = safe_exc.replace(self.smtp_password, "***")
            logger.error("Failed to send email: %s", safe_exc)
            raise


# ===========================================================================
# Helper functions
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

def _log_fatal_exception(exc: BaseException) -> None:
    """Log a fatal pipeline exception without rendering its message."""
    safe_exc = _sanitize_exception(exc)
    # format_exc()/format_exception() append str(exc), which may contain a
    # credential-bearing URL.  format_tb() emits stack frames only.
    safe_traceback = "".join(traceback.format_tb(exc.__traceback__))
    logger.error("Fatal error in pipeline: %s", safe_exc)
    logger.error("Exception details (stack only):\n%s", safe_traceback)
def _direction(score: float, zscore: Optional[float]) -> str:
    """
    Map a sentiment score + z-score to a direction string for CSS/display.
    Uses z-score direction when available, otherwise raw score sign.
    """
    ref = zscore if zscore is not None else score
    if ref > 0.05:
        return "bullish"
    elif ref < -0.05:
        return "bearish"
    return "neutral"


def build_argparser() -> argparse.ArgumentParser:
    """Define and return the CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="NLP-based stock sentiment monitor with daily email briefing.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Environment variables:
  NEWS_API_KEY       NewsAPI.org key (required for news headlines)
  SMTP_HOST          SMTP server (default: smtp.gmail.com)
  SMTP_PORT          SMTP port (default: 587)
  SMTP_USER          Sender email address
  SMTP_PASSWORD      Sender email password / app password
  RECIPIENT_EMAIL    Destination email address
  WATCHLIST          Comma-separated tickers (e.g. AAPL,MSFT,GOOGL)
        """,
    )
    parser.add_argument(
        "--tickers",
        nargs="+",
        metavar="TICKER",
        help="Space-separated list of tickers (overrides WATCHLIST env var and defaults).",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        metavar="N",
        help=f"Number of past days to fetch news for (default: {DEFAULT_DAYS}).",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        metavar="Z",
        help=f"Z-score magnitude threshold for flagging significant shifts (default: {DEFAULT_THRESHOLD}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the HTML email to stdout instead of sending via SMTP.",
    )
    parser.add_argument(
        "--history-file",
        default=str(HISTORY_FILE),
        metavar="PATH",
        help=f"Path to the rolling sentiment history JSON file (default: {HISTORY_FILE}).",
    )
    return parser


def resolve_tickers(args: argparse.Namespace) -> List[str]:
    """
    Determine the final watchlist in priority order:
        1. --tickers CLI arg
        2. WATCHLIST environment variable
        3. DEFAULT_TICKERS constant
    Returns uppercase ticker symbols.
    """
    if args.tickers:
        return [t.upper() for t in args.tickers]
    env_list = os.getenv("WATCHLIST", "")
    if env_list.strip():
        return [t.strip().upper() for t in env_list.split(",") if t.strip()]
    return DEFAULT_TICKERS


# ===========================================================================
# Main pipeline
# ===========================================================================

def run_pipeline(
    tickers: List[str],
    days: int,
    threshold: float,
    dry_run: bool,
    history_file: Path,
) -> List[Dict]:
    """
    Execute the full sentiment-analysis pipeline:

    1. Initialise SentimentAnalyzer (loads FinBERT once).
    2. For each ticker, collect news + SEC data.
    3. Batch-score all texts with FinBERT.
    4. Compute weighted sentiment score per ticker.
    5. Update rolling history and compute z-scores.
    6. Build and send (or print) the HTML briefing.

    Returns:
        List of per-ticker result dicts (for testing / further use).
    """
    # ------------------------------------------------------------------
    # Initialise components
    # ------------------------------------------------------------------
    analyzer = SentimentAnalyzer()
    news_collector = NewsCollector(days=days)
    sec_collector = SECCollector()
    history = HistoryManager(filepath=history_file)
    emailer = EmailBriefing()

    results: List[Dict] = []

    # ------------------------------------------------------------------
    # Per-ticker data collection and scoring
    # ------------------------------------------------------------------
    # Run network IO concurrently for all tickers and sources (avoid nested thread pools).
    max_workers = min(10, len(tickers) * 2) if tickers else 1
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {}
        for t in tickers:
            futures[(t, "news")] = executor.submit(news_collector.fetch_headlines, t)
            futures[(t, "sec")] = executor.submit(sec_collector.fetch_filings, t)

        fetch_results = {
            t: (t, futures[(t, "news")].result(), futures[(t, "sec")].result())
            for t in tickers
        }
    for ticker in tickers:
        logger.info("Processing ticker: %s", ticker)
        _, news_items, sec_items = fetch_results[ticker]

        # Combine all text sources
        all_items = news_items + sec_items
        if not all_items:
            logger.warning("%s: no data sources returned any items.", ticker)
            results.append({
                "ticker": ticker,
                "sentiment_score": 0.0,
                "zscore": None,
                "mean_30d": None,
                "std_30d": None,
                "is_significant": False,
                "headline_count": 0,
                "top_headlines": [],
            })
            continue

        # Extract text strings for batch NLP scoring
        texts = [item["text"] for item in all_items]

        # (c) Batch NLP scoring — single pass through FinBERT for all texts
        logger.info("%s: scoring %d texts with FinBERT…", ticker, len(texts))
        scored = analyzer.score_texts(texts)

        # Attach scores back to items for headline selection
        for item, score_info in zip(all_items, scored):
            item.update(score_info)

        # (d) Compute weighted daily sentiment score
        sentiment_score = analyzer.compute_ticker_sentiment(scored)
        logger.info("%s: sentiment_score=%.4f", ticker, sentiment_score)

        # (e) Update history and compute z-score
        history.append_score(ticker, sentiment_score)
        zscore, mean_30d, std_30d = history.compute_zscore(ticker, sentiment_score)

        is_significant = (
            zscore is not None and abs(zscore) >= threshold
        )
        if is_significant:
            logger.info(
                "%s: SIGNIFICANT SHIFT — zscore=%.2f (threshold=%.1f)",
                ticker,
                zscore,
                threshold,
            )

        # (f) Select top 3 triggering headlines (highest |score|, prefer non-neutral)
        non_neutral = [i for i in all_items if i.get("label") != "neutral"]
        sorted_items = sorted(
            non_neutral or all_items,
            key=lambda x: abs(x.get("score", 0.0)),
            reverse=True,
        )
        top_headlines = sorted_items[:3]

        results.append({
            "ticker": ticker,
            "sentiment_score": sentiment_score,
            "zscore": zscore,
            "mean_30d": mean_30d,
            "std_30d": std_30d,
            "is_significant": is_significant,
            "headline_count": len(all_items),
            "top_headlines": [
                {
                    "title": h.get("title", h.get("text", "")[:120]),
                    "source": h.get("source", ""),
                    "label": h.get("label", "neutral"),
                    "score": h.get("score", 0.0),
                    "url": h.get("url", ""),
                }
                for h in top_headlines
            ],
        })

    # ------------------------------------------------------------------
    # Persist updated history
    # ------------------------------------------------------------------
    history.save()

    # ------------------------------------------------------------------
    # Build and send email briefing
    # ------------------------------------------------------------------
    html_body = emailer.build_html(tickers, results, threshold)
    subject = emailer.build_subject(results)
    emailer.send(subject, html_body, dry_run=dry_run)

    return results


# ===========================================================================
# Entry point
# ===========================================================================
if __name__ == "__main__":
    parser = build_argparser()
    args = parser.parse_args()

    tickers = resolve_tickers(args)
    history_path = Path(args.history_file)

    logger.info(
        "Starting sentiment monitor | tickers=%s | days=%d | threshold=%.1f | dry_run=%s",
        tickers,
        args.days,
        args.threshold,
        args.dry_run,
    )

    try:
        pipeline_results = run_pipeline(
            tickers=tickers,
            days=args.days,
            threshold=args.threshold,
            dry_run=args.dry_run,
            history_file=history_path,
        )
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
        sys.exit(0)
    except Exception as exc:
        _log_fatal_exception(exc)
        sys.exit(1)

    # Summary printout (non-dry-run mode)
    if not args.dry_run:
        print("\n--- Sentiment Summary ---")
        for r in pipeline_results:
            z_str = f"z={r['zscore']:+.2f}" if r["zscore"] is not None else "z=N/A"
            flag = " *** ALERT ***" if r["is_significant"] else ""
            print(
                f"  {r['ticker']:<6} score={r['sentiment_score']:+.4f}  "
                f"{z_str:<12}  headlines={r['headline_count']}{flag}"
            )
        print()
