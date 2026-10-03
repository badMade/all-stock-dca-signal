#!/usr/bin/env python3
"""scan_capex_deltas.py: recurring SEC EDGAR capex scanner for watched operating companies.

Extracts quarterly capex figures from SEC filings, evaluates changes against established
baselines, and flags breaches under the 15% quarter-over-quarter delta rule and the
two-tier addition rule ($2,000M hyperscalers, $250M suppliers).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

# Approved SEC EDGAR domains to protect against SSRF
ALLOWED_SEC_DOMAINS = ("data.sec.gov", "www.sec.gov")
DEFAULT_USER_AGENT = "AllStockDCAResearch/1.0 (research@badmade.com)"

# US-GAAP XBRL tags commonly representing corporate capital expenditures
US_GAAP_CAPEX_TAGS = (
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "PaymentsToAcquireProductiveAssets",
    "PaymentsForPropertyPlantAndEquipment",
    "AdditionsToPropertyPlantAndEquipment",
)


# Filers excluded from automated quarterly 10-Q XBRL parsing per capex-scanner-spec.md
UNSCANNABLE_TICKERS = {
    "TSM": "Form 20-F annual filer (NT$ reporting)",
    "ASML": "Form 20-F annual filer (EUR reporting)",
    "STM": "Form 20-F annual filer (EUR reporting)",
    "CBRS": "Private entity / S-1 filing status",
    "SPCX": "Private equity holding",
}


@dataclass(frozen=True)
class CapexScannerConfig:
    """Configuration thresholds for capex delta evaluation."""

    capex_delta_flag_pct: float = 15.0
    hyperscaler_spend_threshold_usd_m: float = 5000.0
    hyperscaler_addition_usd_m: float = 2000.0
    supplier_addition_usd_m: float = 250.0


@dataclass(frozen=True)
class CapexBaseline:
    """Baseline capex recorded in knowledge note."""

    ticker: str
    period_end: str
    raw_capex_text: str
    amount_usd_m: float | None
    source: str


@dataclass(frozen=True)
class CapexEvaluation:
    """Evaluation result comparing current capex against baseline."""

    ticker: str
    current_usd_m: float
    previous_usd_m: float | None
    delta_usd_m: float | None
    pct_delta: float | None
    flagged_pct: bool
    flagged_addition: bool
    tier: str
    status: str = "passed"
    company_name: str = ""
    period_end: str = ""
    notes: str = ""


@dataclass(frozen=True)
class QuarterlyCapexFact:
    """Quarterly capex fact after differencing or direct extraction."""

    fiscal_period: str
    fiscal_year: int
    period_end: str
    amount_usd_m: float
    source: str  # accession(s) of the input fact(s); never a generic label


def difference_quarterly_capex(ytd_facts: list[dict[str, Any]]) -> dict[str, QuarterlyCapexFact]:
    """Calculate standalone quarterly capex amounts from cumulative YTD facts.

    Every input fact must carry an accession (``accn``); one without is dropped, so a derived quarter is
    never labelled with a generic source (PR 1590 review: "no source, no number"). A differenced quarter
    cites both the minuend and the subtrahend filing.
    """
    if not isinstance(ytd_facts, list):
        return {}

    by_fp: dict[str, dict[str, Any]] = {}
    for fact in ytd_facts:
        if not isinstance(fact, dict):
            continue
        fp = str(fact.get("fp", "")).upper()
        if fp in ("Q1", "Q2", "Q3", "FY"):
            by_fp[fp] = fact

    results: dict[str, QuarterlyCapexFact] = {}

    def extract_val_end_fy(fact: dict[str, Any]) -> tuple[float, str, int, str] | None:
        v = fact.get("val")
        e = str(fact.get("end", ""))
        fy = fact.get("fy", 0)
        accn = str(fact.get("accn", "")).strip()
        if isinstance(v, (int, float)) and v > 0 and e and accn:
            return float(v), e, int(fy), accn
        return None

    def difference(later: tuple[float, str, int, str], earlier: tuple[float, str, int, str], fp: str) -> None:
        # Fail closed on a cumulative total that drops (restatement or bad data).
        if later[0] < earlier[0]:
            return
        source = later[3] if later[3] == earlier[3] else f"{later[3]} - {earlier[3]}"
        results[fp] = QuarterlyCapexFact(
            fiscal_period=fp,
            fiscal_year=later[2],
            period_end=later[1],
            amount_usd_m=round((later[0] - earlier[0]) / 1_000_000.0, 2),
            source=source,
        )

    parsed = {fp: extract_val_end_fy(fact) for fp, fact in by_fp.items()}

    # Q1: direct from Q1 fact
    q1 = parsed.get("Q1")
    if q1:
        val, end, fy, accn = q1
        results["Q1"] = QuarterlyCapexFact(
            fiscal_period="Q1",
            fiscal_year=fy,
            period_end=end,
            amount_usd_m=round(val / 1_000_000.0, 2),
            source=accn,
        )

    # Q2 = Q2 - Q1, Q3 = Q3 - Q2, Q4 = FY - Q3
    for fp, later_key, earlier_key in (("Q2", "Q2", "Q1"), ("Q3", "Q3", "Q2"), ("Q4", "FY", "Q3")):
        later, earlier = parsed.get(later_key), parsed.get(earlier_key)
        if later and earlier:
            difference(later, earlier, fp)

    return results


def parse_sec_company_tickers_mapping(json_bytes: bytes) -> dict[str, str]:
    """Parse SEC company_tickers.json mapping ticker symbol to 10-digit zero-padded CIK."""
    try:
        data = json.loads(json_bytes.decode("utf-8"))
    except Exception:
        return {}

    mapping: dict[str, str] = {}
    if isinstance(data, dict):
        for entry in data.values():
            if isinstance(entry, dict):
                ticker = entry.get("ticker")
                cik = entry.get("cik_str")
                if ticker and cik is not None:
                    mapping[str(ticker).strip().upper()] = str(cik).strip().zfill(10)
    return mapping


class SECRateLimiter:
    """Rate limiter enforcing max 10 requests/sec with injectable clock."""

    def __init__(
        self,
        min_interval: float = 0.1,
        time_fn: Callable[[], float] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self.min_interval = min_interval
        self.time_fn = time_fn or time.time
        self.sleep_fn = sleep_fn or time.sleep
        self.last_request_time: float | None = None

    def acquire(self) -> None:
        """Acquire permission to send an SEC request, sleeping if needed to satisfy rate limit."""
        now = self.time_fn()
        if self.last_request_time is not None:
            elapsed = now - self.last_request_time
            if elapsed < self.min_interval:
                to_sleep = self.min_interval - elapsed
                self.sleep_fn(to_sleep)
                now = self.time_fn()
        self.last_request_time = now


def validate_sec_url(url: str) -> bool:
    """Validate that the target URL belongs strictly to approved SEC endpoints."""
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False
    if parsed.scheme != "https":
        return False
    return parsed.hostname in ALLOWED_SEC_DOMAINS


def load_scanner_config(config_path: Path) -> CapexScannerConfig:
    """Load capex thresholds from config.yaml using standard library line parsing."""
    delta_pct = 15.0
    hyperscaler_add = 2000.0
    supplier_add = 250.0
    spend_threshold = 5000.0

    if config_path.exists():
        for line in config_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("capex_delta_flag_pct:"):
                parts = line.split(":", 1)[1].split("#")[0].strip()
                try:
                    delta_pct = float(parts)
                except ValueError:
                    pass
            elif line.startswith("capex_research_addition_usd_m:"):
                parts = line.split(":", 1)[1].split("#")[0].strip()
                try:
                    hyperscaler_add = float(parts)
                except ValueError:
                    pass

    return CapexScannerConfig(
        capex_delta_flag_pct=delta_pct,
        hyperscaler_spend_threshold_usd_m=spend_threshold,
        hyperscaler_addition_usd_m=hyperscaler_add,
        supplier_addition_usd_m=supplier_add,
    )


def parse_watched_companies_and_baselines(
    note_path: Path,
) -> tuple[dict[str, str], dict[str, str], dict[str, CapexBaseline]]:
    """Parse watched filers, non-operating exclusions, and existing baselines from note."""
    if not note_path.exists():
        return {}, {}, {}

    content = note_path.read_text(encoding="utf-8")
    operating: dict[str, str] = {}
    non_operating: dict[str, str] = {}
    baselines: dict[str, CapexBaseline] = {}

    section = ""
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("### Operating Companies"):
            section = "operating"
            continue
        elif line.startswith("### Non-Operating Vehicles"):
            section = "non_operating"
            continue
        elif line.startswith("## Baselines"):
            section = "baselines"
            continue
        elif line.startswith("## "):
            section = "other"
            continue

        if section == "operating" and line.startswith("- "):
            match = re.match(r"^-\s+([A-Z0-9/\s]+)\s*\(([^)]+)\):?\s*(.*)$", line)
            if match:
                tickers_str, name, desc = match.groups()
                primary_ticker = tickers_str.split("/")[0].strip()
                if primary_ticker:
                    operating[primary_ticker] = name.strip()
        elif section == "non_operating" and line.startswith("- "):
            match = re.match(r"^-\s+([A-Z0-9]+):\s*(.*)$", line)
            if match:
                sym, desc = match.groups()
                non_operating[sym.strip()] = desc.strip()
        elif section == "baselines" and line.startswith("|") and not line.startswith("| Company") and not line.startswith("| ---"):
            parts = [p.strip() for p in line.split("|")[1:-1]]
            if len(parts) >= 4:
                sym, period, capex_str, source = parts[0], parts[1], parts[2], parts[3]
                amt_match = re.search(r"[\$€]\s*([\d,]+(?:\.\d+)?)\s*([MB])?", capex_str)
                amt_usd_m: float | None = None
                if amt_match:
                    val = float(amt_match.group(1).replace(",", ""))
                    unit = amt_match.group(2)
                    if unit == "B":
                        amt_usd_m = val * 1000.0
                    else:
                        amt_usd_m = val
                baselines[sym] = CapexBaseline(
                    ticker=sym,
                    period_end=period,
                    raw_capex_text=capex_str,
                    amount_usd_m=amt_usd_m,
                    source=source,
                )

    return operating, non_operating, baselines


def evaluate_capex_change(
    ticker: str,
    current_usd_m: float,
    previous_usd_m: float | None,
    config: CapexScannerConfig,
    company_name: str = "",
    period_end: str = "",
) -> CapexEvaluation:
    """Evaluate quarter-over-quarter change and two-tier capex addition thresholds."""
    if ticker in UNSCANNABLE_TICKERS:
        return CapexEvaluation(
            ticker=ticker,
            current_usd_m=current_usd_m,
            previous_usd_m=previous_usd_m,
            delta_usd_m=None,
            pct_delta=None,
            flagged_pct=False,
            flagged_addition=False,
            tier="hyperscaler" if (previous_usd_m and previous_usd_m >= config.hyperscaler_spend_threshold_usd_m) else "supplier",
            status="not_scannable",
            company_name=company_name,
            period_end=period_end,
            notes=UNSCANNABLE_TICKERS[ticker],
        )

    is_hyperscaler = (
        (previous_usd_m is not None and previous_usd_m >= config.hyperscaler_spend_threshold_usd_m)
        or current_usd_m >= config.hyperscaler_spend_threshold_usd_m
    )
    tier = "hyperscaler" if is_hyperscaler else "supplier"
    addition_limit = config.hyperscaler_addition_usd_m if is_hyperscaler else config.supplier_addition_usd_m

    if previous_usd_m is None or previous_usd_m <= 0:
        return CapexEvaluation(
            ticker=ticker,
            current_usd_m=current_usd_m,
            previous_usd_m=previous_usd_m,
            delta_usd_m=None,
            pct_delta=None,
            flagged_pct=False,
            flagged_addition=False,
            tier=tier,
            status="baseline_recorded",
            company_name=company_name,
            period_end=period_end,
            notes="New baseline established, no prior baseline to compute delta",
        )

    delta_usd_m = current_usd_m - previous_usd_m
    pct_delta = (delta_usd_m / previous_usd_m) * 100.0

    flagged_pct = pct_delta > config.capex_delta_flag_pct
    flagged_addition = delta_usd_m >= addition_limit

    # Decision 1 AND gate: flagged only if BOTH gates trip
    is_flagged = flagged_pct and flagged_addition
    status = "flagged" if is_flagged else "passed"

    notes = []
    if is_flagged:
        notes.append(f"Breached both gates: {pct_delta:+.1f}% QoQ delta and ${delta_usd_m:+,.1f}M {tier} addition")
    elif flagged_pct:
        notes.append(f"Breached {config.capex_delta_flag_pct:.1f}% QoQ delta (+{pct_delta:.1f}%), but under ${addition_limit:,.0f}M {tier} addition")
    elif flagged_addition:
        notes.append(f"Breached ${addition_limit:,.0f}M {tier} addition (+${delta_usd_m:,.1f}M), but under {config.capex_delta_flag_pct:.1f}% QoQ delta")
    else:
        notes.append("Within normal thresholds")

    return CapexEvaluation(
        ticker=ticker,
        current_usd_m=current_usd_m,
        previous_usd_m=previous_usd_m,
        delta_usd_m=round(delta_usd_m, 2),
        pct_delta=round(pct_delta, 2),
        flagged_pct=flagged_pct,
        flagged_addition=flagged_addition,
        tier=tier,
        status=status,
        company_name=company_name,
        period_end=period_end,
        notes="; ".join(notes),
    )


def format_capex_scan_report(
    evaluations: list[CapexEvaluation],
    title: str = "CapEx Scan Report",
) -> str:
    """Format evaluation results into a structured Markdown table and summary."""
    lines = [
        f"## {title}",
        "",
        "| Ticker | Company | Period End | Tier | Current ($M) | Prior ($M) | Delta ($M) | Delta (%) | Status | Notes |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]

    counts = {
        "flagged": 0,
        "passed": 0,
        "baseline_recorded": 0,
        "not_scannable": 0,
        "not_found": 0,
    }

    for ev in evaluations:
        status_key = ev.status.lower()
        counts[status_key] = counts.get(status_key, 0) + 1
        status_display = "PASS" if ev.status == "passed" else ev.status.upper()

        if ev.status == "not_scannable":
            curr_str = "N/A"
            prev_str = "N/A"
            delta_str = "N/A"
            pct_str = "N/A"
            period_str = ev.period_end or "N/A"
        elif ev.status == "baseline_recorded":
            curr_str = f"${ev.current_usd_m:,.1f}M"
            prev_str = "N/A"
            delta_str = "N/A"
            pct_str = "N/A"
            period_str = ev.period_end or "N/A"
        else:
            curr_str = f"${ev.current_usd_m:,.1f}M"
            prev_str = f"${ev.previous_usd_m:,.1f}M" if ev.previous_usd_m is not None else "N/A"
            if ev.delta_usd_m is not None:
                delta_str = f"+${ev.delta_usd_m:,.1f}M" if ev.delta_usd_m >= 0 else f"-${abs(ev.delta_usd_m):,.1f}M"
            else:
                delta_str = "N/A"
            pct_str = f"{ev.pct_delta:+.1f}%".replace("+-", "-") if ev.pct_delta is not None else "N/A"
            period_str = ev.period_end or "N/A"

        co_name = ev.company_name or ev.ticker
        lines.append(
            f"| {ev.ticker} | {co_name} | {period_str} | {ev.tier} | {curr_str} | {prev_str} | {delta_str} | {pct_str} | {status_display} | {ev.notes} |"
        )

    lines.extend([
        "",
        "### Summary",
        f"- Total Filers Evaluated: {len(evaluations)}",
        f"- Flagged: {counts['flagged']}",
        f"- Passed: {counts['passed']}",
        f"- Baselines Recorded: {counts['baseline_recorded']}",
        f"- Not Scannable: {counts['not_scannable']}",
    ])

    return "\n".join(lines)


def extract_capex_from_facts(facts_payload: dict[str, Any]) -> tuple[str, float, str] | None:
    """Extract latest quarterly capex (period_end, amount_usd_m, accession) from XBRL facts."""
    if not isinstance(facts_payload, dict):
        return None

    us_gaap = facts_payload.get("facts", {}).get("us-gaap", {})
    if not isinstance(us_gaap, dict):
        return None

    for tag in US_GAAP_CAPEX_TAGS:
        tag_data = us_gaap.get(tag)
        if not isinstance(tag_data, dict):
            continue
        units = tag_data.get("units", {}).get("USD", [])
        if not isinstance(units, list) or not units:
            continue

        # Filter quarterly 10-Q facts first, falling back to any entry with end and val
        quarterly_facts = [
            f for f in units
            if isinstance(f, dict) and f.get("form") in ("10-Q", "10-K") and "val" in f and "end" in f
        ]
        candidates = quarterly_facts if quarterly_facts else [
            f for f in units if isinstance(f, dict) and "val" in f and "end" in f
        ]
        if not candidates:
            continue

        candidates.sort(key=lambda x: str(x.get("end", "")), reverse=True)
        latest = candidates[0]
        val = latest.get("val")
        end = latest.get("end")
        accn = latest.get("accn", "SEC EDGAR")
        if isinstance(val, (int, float)) and end:
            amount_usd_m = round(float(val) / 1_000_000.0, 2)
            return str(end), amount_usd_m, str(accn)

    return None


class SECRedirectDisallowedHandler(urllib.request.HTTPRedirectHandler):
    """Refuse HTTP redirects to protect against SSRF and credential exposure."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        """Deny following redirects unconditionally."""
        return None


def _build_sec_opener() -> urllib.request.OpenerDirector:
    """Build an opener that disallows redirects to protect against SSRF."""
    return urllib.request.build_opener(SECRedirectDisallowedHandler())


def _http_get_sec(
    url: str,
    user_agent: str = DEFAULT_USER_AGENT,
    timeout: float = 10.0,
    limiter: SECRateLimiter | None = None,
    fetch_fn: Callable[[str, dict[str, str], float], bytes] | None = None,
) -> bytes | None:
    """Fetch bytes from SEC endpoint with SSRF check, rate limiting, and fail-closed handling."""
    if not validate_sec_url(url):
        return None
    if limiter is not None:
        limiter.acquire()
    headers = {
        "User-Agent": user_agent,
        "Accept": "application/json",
    }
    if fetch_fn is not None:
        try:
            return fetch_fn(url, headers, timeout)
        except Exception:
            return None
    try:
        req = urllib.request.Request(url, headers=headers)
        opener = _build_sec_opener()
        with opener.open(req, timeout=timeout) as response:  # noqa: S310
            if response.status != 200:
                return None
            return response.read()
    except Exception:
        return None


def fetch_sec_company_tickers(
    user_agent: str = DEFAULT_USER_AGENT,
    timeout: float = 10.0,
    limiter: SECRateLimiter | None = None,
    fetch_fn: Callable[[str, dict[str, str], float], bytes] | None = None,
) -> dict[str, str]:
    """Fetch and parse SEC company tickers mapping from company_tickers.json.

    Args:
        user_agent: SEC contact User-Agent header.
        timeout: Request timeout in seconds.
        limiter: Optional SECRateLimiter for pacing.
        fetch_fn: Optional injected network fetcher for offline testing.

    Returns:
        Mapping of uppercase ticker symbol to 10-digit zero-padded CIK string.
    """
    url = "https://www.sec.gov/files/company_tickers.json"
    raw_bytes = _http_get_sec(
        url=url,
        user_agent=user_agent,
        timeout=timeout,
        limiter=limiter,
        fetch_fn=fetch_fn,
    )
    if not raw_bytes:
        return {}
    return parse_sec_company_tickers_mapping(raw_bytes)


def fetch_company_facts_from_sec(
    cik: str,
    user_agent: str = DEFAULT_USER_AGENT,
    timeout: float = 10.0,
    limiter: SECRateLimiter | None = None,
    fetch_fn: Callable[[str, dict[str, str], float], bytes] | None = None,
) -> dict[str, Any] | None:
    """Fetch company facts JSON for a given CIK from SEC EDGAR.

    Args:
        cik: Central Index Key (numeric or zero-padded string).
        user_agent: SEC contact User-Agent header.
        timeout: Request timeout in seconds.
        limiter: Optional SECRateLimiter for pacing.
        fetch_fn: Optional injected network fetcher for offline testing.

    Returns:
        Parsed company facts dict, or None on error or non-200 or malformed JSON.
    """
    clean_cik = str(cik).strip().zfill(10)
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{clean_cik}.json"
    raw_bytes = _http_get_sec(
        url=url,
        user_agent=user_agent,
        timeout=timeout,
        limiter=limiter,
        fetch_fn=fetch_fn,
    )
    if not raw_bytes:
        return None
    try:
        data = json.loads(raw_bytes.decode("utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def fetch_company_facts_offline_or_sec(
    ticker: str,
    user_agent: str = DEFAULT_USER_AGENT,
    timeout: float = 10.0,
    cik_mapping: dict[str, str] | None = None,
    limiter: SECRateLimiter | None = None,
    fetch_fn: Callable[[str, dict[str, str], float], bytes] | None = None,
) -> dict[str, Any] | None:
    """Fetch company facts from SEC EDGAR with timeout, CIK lookup, and error containment.

    Args:
        ticker: Stock ticker symbol.
        user_agent: SEC contact User-Agent header.
        timeout: Request timeout in seconds.
        cik_mapping: Optional pre-loaded ticker to CIK mapping.
        limiter: Optional SECRateLimiter for pacing.
        fetch_fn: Optional injected network fetcher for offline testing.

    Returns:
        Parsed company facts dict, or None if ticker not found or fetch fails.
    """
    sym = ticker.strip().upper()
    if sym in UNSCANNABLE_TICKERS:
        return None

    mapping = cik_mapping
    if mapping is None:
        mapping = fetch_sec_company_tickers(
            user_agent=user_agent,
            timeout=timeout,
            limiter=limiter,
            fetch_fn=fetch_fn,
        )

    cik = mapping.get(sym)
    if not cik:
        return None

    return fetch_company_facts_from_sec(
        cik=cik,
        user_agent=user_agent,
        timeout=timeout,
        limiter=limiter,
        fetch_fn=fetch_fn,
    )


def main() -> int:
    """CLI entry point for recurring capex delta scans."""
    parser = argparse.ArgumentParser(description="Scan quarterly capex deltas for watched filers")
    parser.add_argument("--tickers", help="Comma-separated list of symbols to evaluate")
    parser.add_argument("--live", action="store_true", help="Fetch live company facts from SEC EDGAR")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT, help="User-Agent string for SEC requests")
    parser.add_argument("--timeout", type=float, default=10.0, help="HTTP request timeout in seconds")
    parser.add_argument("--dry-run", action="store_true", help="Run scanner using existing baselines only")
    parser.add_argument("--report", action="store_true", help="Format and output structured Markdown report")
    parser.add_argument("--report-file", type=Path, help="File path to save Markdown report")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent.parent
    vault_root = repo_root / "agent-vault"
    kpis_note = vault_root / "vault" / "Knowledge" / "capex-monitoring-kpis.md"
    config_yaml = vault_root / "config.yaml"

    cfg = load_scanner_config(config_yaml)
    operating, non_operating, baselines = parse_watched_companies_and_baselines(kpis_note)

    target_tickers = [t.strip().upper() for t in args.tickers.split(",")] if args.tickers else list(baselines.keys())

    limiter = SECRateLimiter() if args.live else None
    cik_map: dict[str, str] = {}
    if args.live:
        print(f"Fetching SEC company tickers mapping with User-Agent: {args.user_agent}...")
        cik_map = fetch_sec_company_tickers(user_agent=args.user_agent, timeout=args.timeout, limiter=limiter)
        print(f"Loaded {len(cik_map)} CIK mappings from SEC.")

    evaluations: list[CapexEvaluation] = []
    for ticker in target_tickers:
        if ticker in non_operating:
            continue
        base = baselines.get(ticker)
        curr_val = base.amount_usd_m if (base and base.amount_usd_m is not None) else 0.0
        prev_val = base.amount_usd_m if base else None
        period_str = base.period_end if base else ""
        co_name = operating.get(ticker, "")

        if args.live:
            if ticker in UNSCANNABLE_TICKERS:
                ev = evaluate_capex_change(
                    ticker=ticker,
                    current_usd_m=0.0,
                    previous_usd_m=prev_val,
                    config=cfg,
                    company_name=co_name,
                    period_end=period_str,
                )
                evaluations.append(ev)
                continue

            facts = fetch_company_facts_offline_or_sec(
                ticker=ticker,
                user_agent=args.user_agent,
                timeout=args.timeout,
                cik_mapping=cik_map,
                limiter=limiter,
            )
            extracted = extract_capex_from_facts(facts) if facts else None
            if extracted is not None:
                new_period, new_val, new_accn = extracted
                ev = evaluate_capex_change(
                    ticker=ticker,
                    current_usd_m=new_val,
                    previous_usd_m=prev_val,
                    config=cfg,
                    company_name=co_name,
                    period_end=new_period,
                )
            else:
                ev = CapexEvaluation(
                    ticker=ticker,
                    current_usd_m=curr_val,
                    previous_usd_m=prev_val,
                    delta_usd_m=None,
                    pct_delta=None,
                    flagged_pct=False,
                    flagged_addition=False,
                    tier="hyperscaler" if (prev_val and prev_val >= cfg.hyperscaler_spend_threshold_usd_m) else "supplier",
                    status="not_found",
                    company_name=co_name,
                    period_end=period_str,
                    notes="SEC company facts not found or unavailable",
                )
            evaluations.append(ev)
        else:
            ev = evaluate_capex_change(
                ticker=ticker,
                current_usd_m=curr_val,
                previous_usd_m=prev_val,
                config=cfg,
                company_name=co_name,
                period_end=period_str,
            )
            evaluations.append(ev)

    if args.report:
        report_md = format_capex_scan_report(evaluations, title="Quarterly CapEx Delta Scan Report")
        if args.report_file:
            args.report_file.parent.mkdir(parents=True, exist_ok=True)
            args.report_file.write_text(report_md + "\n", encoding="utf-8")
            print(f"Report written to {args.report_file}")
        else:
            print(report_md)
    else:
        print(f"Loaded {len(operating)} operating filers and {len(baselines)} baselines.")
        print(f"Evaluated {len(evaluations)} companies:")
        for ev in evaluations:
            flag_status = "FLAGGED" if ev.status == "flagged" else ev.status.upper()
            print(f"  [{flag_status}] {ev.ticker} ({ev.tier}): ${ev.current_usd_m:,.1f}M - {ev.notes}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
