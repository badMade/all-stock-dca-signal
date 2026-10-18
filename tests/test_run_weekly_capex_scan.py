"""Tests for run_weekly_capex_scan.py: recurring runner for CapEx monitoring."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VAULT_ROOT = REPO_ROOT / "agent-vault"
SCRIPTS_DIR = VAULT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from run_weekly_capex_scan import execute_capex_scan, main  # noqa: E402


def test_execute_capex_scan_generates_report_files(tmp_path: Path):
    """Verify execute_capex_scan produces capex-scan-{date}.md and latest-capex-scan.md."""
    reports_dir = tmp_path / "reports"
    code, report_path, evals = execute_capex_scan(
        vault_root=VAULT_ROOT,
        reports_dir=reports_dir,
        today_iso="2026-10-03",
        live=False,
    )
    assert code == 0
    assert report_path.is_file()
    assert report_path.name == "capex-scan-2026-10-03.md"
    assert (reports_dir / "latest-capex-scan.md").is_file()

    content = report_path.read_text(encoding="utf-8")
    assert "## CapEx Scan Report (2026-10-03)" in content
    assert "| NVDA |" in content
    assert "| MSFT |" in content
    assert len(evals) >= 8


def test_execute_capex_scan_updates_existing_daily_note(tmp_path: Path):
    """Verify that execute_capex_scan logs execution outcome to Daily/YYYY-MM-DD.md if present."""
    vault_copy = tmp_path / "vault_root"
    daily_dir = vault_copy / "vault" / "Daily"
    daily_dir.mkdir(parents=True)
    daily_note = daily_dir / "2026-10-03.md"
    daily_note.write_text("# 2026-10-03\n\n## Sessions\n", encoding="utf-8")

    # Copy minimal required files to vault_copy
    knowledge_dir = vault_copy / "vault" / "Knowledge"
    knowledge_dir.mkdir(parents=True)
    (knowledge_dir / "capex-monitoring-kpis.md").write_text(
        (VAULT_ROOT / "vault" / "Knowledge" / "capex-monitoring-kpis.md").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (vault_copy / "config.yaml").write_text(
        (VAULT_ROOT / "config.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    reports_dir = tmp_path / "reports"
    code, _, _ = execute_capex_scan(
        vault_root=vault_copy,
        reports_dir=reports_dir,
        today_iso="2026-10-03",
        live=False,
        target_tickers=["NVDA"],
    )
    assert code == 0
    updated_daily = daily_note.read_text(encoding="utf-8")
    assert "### CapEx Scan (2026-10-03)" in updated_daily
    assert "- Mode: offline baseline" in updated_daily
    assert "- Evaluated: 1 companies" in updated_daily


def test_main_cli_execution(tmp_path: Path):
    """Verify main() CLI execution parses flags and succeeds."""
    reports_dir = tmp_path / "reports"
    exit_code = main([
        "--now",
        "2026-10-03",
        "--reports-dir",
        str(reports_dir),
        "--tickers",
        "NVDA,MSFT",
    ])
    assert exit_code == 0
    assert (reports_dir / "capex-scan-2026-10-03.md").is_file()


# --- watchlist coverage check: an active DCA ticker missing from the capex note must be visible ---

def _vault_with_note(tmp_path: Path, note_body: str) -> Path:
    vault = tmp_path / "agent-vault"
    (vault / "vault" / "Knowledge").mkdir(parents=True)
    (vault / "vault" / "Knowledge" / "capex-monitoring-kpis.md").write_text(note_body, encoding="utf-8")
    (vault / "config.yaml").write_text((VAULT_ROOT / "config.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    return vault


_NOTE = """## Watched companies

### Operating Companies (SEC 10-Q / 10-K / 20-F Filers)
- MSFT (Microsoft Corporation): "Additions to property and equipment", fiscal year end: June 30.
- GOOGL / GOOG (Alphabet Inc.): "Capital expenditures", fiscal year end: December 31.

### Non-Operating Vehicles (Excluded from Capex Analysis)
- QQQ: Invesco QQQ Trust (Index ETF)
"""


def test_full_scan_reports_active_tickers_missing_from_the_watchlist(tmp_path: Path, capsys):
    vault = _vault_with_note(tmp_path, _NOTE)
    config = tmp_path / "tickers.json"
    config.write_text('{"active": ["MSFT", "GOOG", "QQQ", "APH", "TEL"]}', encoding="utf-8")
    code, report_path, _ = execute_capex_scan(
        vault_root=vault, reports_dir=tmp_path / "reports", today_iso="2026-10-18", tickers_config=config,
    )
    assert code == 0
    report = report_path.read_text(encoding="utf-8")
    assert "### Watchlist gaps" in report and "APH, TEL" in report and "NOT scanned" in report
    assert "GOOG," not in report and "QQQ" not in report.split("### Watchlist gaps")[1]  # alias and ETF are covered
    assert "APH, TEL" in capsys.readouterr().err


def test_full_scan_says_so_when_coverage_is_complete(tmp_path: Path, capsys):
    vault = _vault_with_note(tmp_path, _NOTE)
    config = tmp_path / "tickers.json"
    config.write_text('{"active": ["MSFT", "GOOG", "QQQ"]}', encoding="utf-8")
    _, report_path, _ = execute_capex_scan(
        vault_root=vault, reports_dir=tmp_path / "reports", today_iso="2026-10-18", tickers_config=config,
    )
    assert "Every active DCA ticker is on the capex watchlist" in report_path.read_text(encoding="utf-8")
    assert capsys.readouterr().err == ""


def test_unreadable_ticker_config_never_stops_the_scan(tmp_path: Path):
    vault = _vault_with_note(tmp_path, _NOTE)
    for bad in (tmp_path / "missing.json", tmp_path / "bad.json", tmp_path / "wrong.json"):
        if bad.name == "bad.json":
            bad.write_text("{not json", encoding="utf-8")
        elif bad.name == "wrong.json":
            bad.write_text('{"active": 5}', encoding="utf-8")
        code, report_path, _ = execute_capex_scan(
            vault_root=vault, reports_dir=tmp_path / "reports", today_iso="2026-10-18", tickers_config=bad,
        )
        assert code == 0
        assert "coverage not checked" in report_path.read_text(encoding="utf-8")


def test_partial_ticker_run_skips_the_coverage_check(tmp_path: Path):
    vault = _vault_with_note(tmp_path, _NOTE)
    config = tmp_path / "tickers.json"
    config.write_text('{"active": ["MSFT", "APH"]}', encoding="utf-8")
    _, report_path, _ = execute_capex_scan(
        vault_root=vault, reports_dir=tmp_path / "reports", today_iso="2026-10-18",
        target_tickers=["MSFT"], tickers_config=config,
    )
    assert "Watchlist" not in report_path.read_text(encoding="utf-8")


def test_the_real_watchlist_now_covers_the_three_tickers_that_were_missing():
    from scan_capex_deltas import parse_watched_aliases, parse_watched_companies_and_baselines

    note = VAULT_ROOT / "vault" / "Knowledge" / "capex-monitoring-kpis.md"
    operating, _, _ = parse_watched_companies_and_baselines(note)
    assert {"APH", "GFS", "TEL"} <= set(operating)
    assert "GOOG" in parse_watched_aliases(note) and "GOOG" not in operating
