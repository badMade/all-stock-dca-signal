#!/usr/bin/env python3
"""run_weekly_capex_scan.py: recurring runner script for CapEx delta monitoring.

Executes recurring or scheduled scans of CapEx deltas across watched companies,
writing structured Markdown reports to the reports directory and logging the
outcome to the daily note.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path
from typing import Sequence

# Add parent directory to path to locate scan_capex_deltas
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from scan_capex_deltas import (  # noqa: E402
    DEFAULT_USER_AGENT,
    UNSCANNABLE_TICKERS,
    CapexEvaluation,
    SECRateLimiter,
    evaluate_capex_change,
    extract_capex_from_facts,
    fetch_company_facts_offline_or_sec,
    fetch_sec_company_tickers,
    format_capex_scan_report,
    load_scanner_config,
    parse_watched_companies_and_baselines,
)


def execute_capex_scan(
    vault_root: Path,
    reports_dir: Path,
    today_iso: str,
    live: bool = False,
    user_agent: str = DEFAULT_USER_AGENT,
    timeout: float = 10.0,
    target_tickers: Sequence[str] | None = None,
) -> tuple[int, Path, list[CapexEvaluation]]:
    """Execute scan and archive report.

    Args:
        vault_root: Path to agent-vault directory.
        reports_dir: Destination path for archived markdown reports.
        today_iso: ISO date string for run (YYYY-MM-DD).
        live: If True, query SEC EDGAR for current facts.
        user_agent: SEC contact User-Agent header.
        timeout: HTTP request timeout in seconds.
        target_tickers: Optional list of tickers to scan.

    Returns:
        Tuple of (exit_code, report_path, evaluations).
    """
    kpis_note = vault_root / "vault" / "Knowledge" / "capex-monitoring-kpis.md"
    config_yaml = vault_root / "config.yaml"

    cfg = load_scanner_config(config_yaml)
    operating, non_operating, baselines = parse_watched_companies_and_baselines(kpis_note)

    symbols = [t.strip().upper() for t in target_tickers] if target_tickers else list(baselines.keys())

    limiter = SECRateLimiter() if live else None
    cik_map: dict[str, str] = {}
    if live:
        cik_map = fetch_sec_company_tickers(user_agent=user_agent, timeout=timeout, limiter=limiter)

    evaluations: list[CapexEvaluation] = []
    for ticker in symbols:
        if ticker in non_operating:
            continue
        base = baselines.get(ticker)
        curr_val = base.amount_usd_m if (base and base.amount_usd_m is not None) else 0.0
        prev_val = base.amount_usd_m if base else None
        period_str = base.period_end if base else ""
        co_name = operating.get(ticker, "")

        if live:
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
                user_agent=user_agent,
                timeout=timeout,
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

    reports_dir.mkdir(parents=True, exist_ok=True)
    report_file = reports_dir / f"capex-scan-{today_iso}.md"
    report_md = format_capex_scan_report(evaluations, title=f"CapEx Scan Report ({today_iso})")
    report_file.write_text(report_md + "\n", encoding="utf-8")

    latest_file = reports_dir / "latest-capex-scan.md"
    latest_file.write_text(report_md + "\n", encoding="utf-8")

    # Update daily note if present for today
    daily_note = vault_root / "vault" / "Daily" / f"{today_iso}.md"
    if daily_note.exists():
        flagged_count = sum(1 for e in evaluations if e.status == "flagged")
        scan_mode = "live" if live else "offline baseline"
        entry = (
            f"\n### CapEx Scan ({today_iso})\n\n"
            f"- Mode: {scan_mode}\n"
            f"- Evaluated: {len(evaluations)} companies\n"
            f"- Flagged: {flagged_count} breaches\n"
            f"- Report: `{report_file.relative_to(vault_root.parent) if report_file.is_relative_to(vault_root.parent) else report_file.name}`\n"
        )
        content = daily_note.read_text(encoding="utf-8")
        marker = f"### CapEx Scan ({today_iso})"
        if marker not in content:
            daily_note.write_text(content.rstrip() + "\n" + entry, encoding="utf-8")

    return 0, report_file, evaluations


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for recurring weekly CapEx scan runner."""
    parser = argparse.ArgumentParser(description="Recurring weekly runner for CapEx delta scanner")
    parser.add_argument("--now", help="ISO date for scan run (YYYY-MM-DD, defaults to today)")
    parser.add_argument("--live", action="store_true", help="Fetch live filings from SEC EDGAR")
    parser.add_argument("--reports-dir", type=Path, help="Directory to store markdown reports")
    parser.add_argument("--tickers", help="Comma-separated ticker list")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT, help="User-Agent string for SEC requests")
    parser.add_argument("--timeout", type=float, default=10.0, help="HTTP request timeout in seconds")
    args = parser.parse_args(argv)

    vault_root = Path(__file__).resolve().parent.parent
    today_iso = args.now or date.today().isoformat()
    reports_dir = args.reports_dir or (vault_root / "reports")

    target_tickers = [t.strip().upper() for t in args.tickers.split(",")] if args.tickers else None

    code, report_file, evals = execute_capex_scan(
        vault_root=vault_root,
        reports_dir=reports_dir,
        today_iso=today_iso,
        live=args.live,
        user_agent=args.user_agent,
        timeout=args.timeout,
        target_tickers=target_tickers,
    )
    print(f"CapEx scan complete for {today_iso}. Evaluated {len(evals)} filers.")
    print(f"Report saved to {report_file}")
    return code


if __name__ == "__main__":
    sys.exit(main())
