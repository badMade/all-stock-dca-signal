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
