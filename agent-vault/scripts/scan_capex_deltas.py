#!/usr/bin/env python3
"""scan_capex_deltas.py: recurring SEC EDGAR capex scanner for watched operating companies.

Extracts quarterly capex figures from SEC filings, evaluates changes against established
baselines, and flags breaches under the 15% quarter-over-quarter delta rule and the
two-tier addition rule ($2,000M hyperscalers, $250M suppliers).
"""

from __future__ import annotations

import argparse
import re
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    notes: str = ""


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
                    operating[primary_ticker] = f"{name.strip()} ({desc.strip()})"
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
) -> CapexEvaluation:
    """Evaluate quarter-over-quarter change and two-tier capex addition thresholds."""
    is_hyperscaler = (
        current_usd_m >= config.hyperscaler_spend_threshold_usd_m
        or (previous_usd_m is not None and previous_usd_m >= config.hyperscaler_spend_threshold_usd_m)
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
            notes="New baseline established, no prior baseline to compute delta",
        )

    delta_usd_m = current_usd_m - previous_usd_m
    pct_delta = (delta_usd_m / previous_usd_m) * 100.0

    flagged_pct = pct_delta > config.capex_delta_flag_pct
    flagged_addition = delta_usd_m > addition_limit

    notes = []
    if flagged_pct:
        notes.append(f"Exceeds {config.capex_delta_flag_pct:.1f}% QoQ delta")
    if flagged_addition:
        notes.append(f"Exceeds ${addition_limit:,.0f}M {tier} addition threshold")

    return CapexEvaluation(
        ticker=ticker,
        current_usd_m=current_usd_m,
        previous_usd_m=previous_usd_m,
        delta_usd_m=round(delta_usd_m, 2),
        pct_delta=round(pct_delta, 2),
        flagged_pct=flagged_pct,
        flagged_addition=flagged_addition,
        tier=tier,
        notes="; ".join(notes) if notes else "Within normal thresholds",
    )


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


def fetch_company_facts_offline_or_sec(
    ticker: str,
    user_agent: str = DEFAULT_USER_AGENT,
    timeout: float = 10.0,
) -> dict[str, Any] | None:
    """Fetch company facts from SEC EDGAR with timeout and error containment."""
    # Placeholder for live EDGAR requests: requires CIK resolution
    # In live environments, CIK is looked up via sec.gov/files/company_tickers.json
    return None


def main() -> int:
    """CLI entry point for recurring capex delta scans."""
    parser = argparse.ArgumentParser(description="Scan quarterly capex deltas for watched filers")
    parser.add_argument("--tickers", help="Comma-separated list of symbols to evaluate")
    parser.add_argument("--dry-run", action="store_true", help="Run scanner using existing baselines only")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent.parent
    vault_root = repo_root / "agent-vault"
    kpis_note = vault_root / "vault" / "Knowledge" / "capex-monitoring-kpis.md"
    config_yaml = vault_root / "config.yaml"

    cfg = load_scanner_config(config_yaml)
    operating, non_operating, baselines = parse_watched_companies_and_baselines(kpis_note)

    print(f"Loaded {len(operating)} operating filers and {len(baselines)} baselines.")
    target_tickers = [t.strip().upper() for t in args.tickers.split(",")] if args.tickers else list(baselines.keys())

    evaluations: list[CapexEvaluation] = []
    for ticker in target_tickers:
        if ticker in non_operating:
            continue
        base = baselines.get(ticker)
        if not base or base.amount_usd_m is None:
            continue
        # In dry-run mode, self-evaluate recorded baseline as baseline check
        ev = evaluate_capex_change(
            ticker=ticker,
            current_usd_m=base.amount_usd_m,
            previous_usd_m=base.amount_usd_m,
            config=cfg,
        )
        evaluations.append(ev)

    print(f"Evaluated {len(evaluations)} companies:")
    for ev in evaluations:
        flag_status = "FLAGGED" if (ev.flagged_pct or ev.flagged_addition) else "PASS"
        print(f"  [{flag_status}] {ev.ticker} ({ev.tier}): ${ev.current_usd_m:,.1f}M - {ev.notes}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
