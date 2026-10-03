"""Tests for scan_capex_deltas.py: recurring SEC EDGAR capex scanner.

Validates:
- Parsing watched operating filers from capex-monitoring-kpis.md (51 companies)
- Exclusions of non-operating vehicles (ETFs, closed-end funds)
- Two-tier capex addition thresholds ($2,000M hyperscalers, $250M suppliers)
- Quarter-over-quarter percentage delta threshold (15%)
- SEC XBRL company facts parsing from mock payloads
- Fail-closed handling for missing or malformed filing data
- Host validation preventing SSRF to untrusted endpoints
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VAULT_ROOT = REPO_ROOT / "agent-vault"
SCRIPTS_DIR = VAULT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import pytest  # noqa: E402
from scan_capex_deltas import (  # noqa: E402
    CapexScannerConfig,
    evaluate_capex_change,
    extract_capex_from_facts,
    load_scanner_config,
    parse_watched_companies_and_baselines,
    validate_sec_url,
)

KPIS_NOTE = VAULT_ROOT / "vault" / "Knowledge" / "capex-monitoring-kpis.md"
CONFIG_YAML = VAULT_ROOT / "config.yaml"


def test_parse_watched_companies_extracts_51_operating_filers():
    """Verify that capex-monitoring-kpis.md yields exactly 51 operating filers."""
    operating_filers, non_operating, _ = parse_watched_companies_and_baselines(KPIS_NOTE)
    assert len(operating_filers) == 51
    assert "NVDA" in operating_filers
    assert "MSFT" in operating_filers
    assert "AAPL" in operating_filers
    assert "TSM" in operating_filers
    assert "ASML" in operating_filers

    # Check non-operating exclusions are separated
    assert len(non_operating) >= 15
    assert "AAAU" in non_operating
    assert "QQQ" in non_operating
    assert "DRAM" in non_operating


def test_parse_baselines_table():
    """Verify parsing existing baselines table from the KPI note."""
    _, _, baselines = parse_watched_companies_and_baselines(KPIS_NOTE)
    assert len(baselines) >= 8
    assert "NVDA" in baselines
    assert "MSFT" in baselines
    assert baselines["NVDA"].period_end == "2025-07-27"
    assert "SEC EDGAR" in baselines["NVDA"].source


def test_load_scanner_config_from_yaml():
    """Verify reading thresholds from config.yaml."""
    cfg = load_scanner_config(CONFIG_YAML)
    assert cfg.capex_delta_flag_pct == 15.0
    assert cfg.hyperscaler_addition_usd_m == 2000.0
    assert cfg.supplier_addition_usd_m == 250.0
    assert cfg.hyperscaler_spend_threshold_usd_m == 5000.0


def test_evaluate_hyperscaler_capex_addition():
    """Test two-tier addition logic for hyperscalers (> $5,000M spend)."""
    cfg = CapexScannerConfig(
        capex_delta_flag_pct=15.0,
        hyperscaler_spend_threshold_usd_m=5000.0,
        hyperscaler_addition_usd_m=2000.0,
        supplier_addition_usd_m=250.0,
    )

    # Hyperscaler adding $2,500M (exceeds $2,000M)
    eval_breach = evaluate_capex_change(
        ticker="MSFT",
        current_usd_m=35000.0,
        previous_usd_m=32500.0,
        config=cfg,
    )
    assert eval_breach.tier == "hyperscaler"
    assert eval_breach.delta_usd_m == 2500.0
    assert eval_breach.flagged_addition is True
    # 2500 / 32500 = 7.69% (< 15%)
    assert eval_breach.flagged_pct is False

    # Hyperscaler adding $1,200M (under $2,000M threshold)
    eval_pass = evaluate_capex_change(
        ticker="GOOGL",
        current_usd_m=20000.0,
        previous_usd_m=18800.0,
        config=cfg,
    )
    assert eval_pass.tier == "hyperscaler"
    assert eval_pass.delta_usd_m == 1200.0
    assert eval_pass.flagged_addition is False


def test_evaluate_supplier_capex_addition():
    """Test two-tier addition logic for suppliers (< $5,000M spend)."""
    cfg = CapexScannerConfig(
        capex_delta_flag_pct=15.0,
        hyperscaler_spend_threshold_usd_m=5000.0,
        hyperscaler_addition_usd_m=2000.0,
        supplier_addition_usd_m=250.0,
    )

    # Supplier adding $300M (exceeds $250M)
    eval_breach = evaluate_capex_change(
        ticker="AMAT",
        current_usd_m=1800.0,
        previous_usd_m=1500.0,
        config=cfg,
    )
    assert eval_breach.tier == "supplier"
    assert eval_breach.delta_usd_m == 300.0
    assert eval_breach.flagged_addition is True
    # 300 / 1500 = 20.0% (> 15%)
    assert eval_breach.flagged_pct is True

    # Supplier adding $100M (under $250M and under 15%)
    eval_pass = evaluate_capex_change(
        ticker="LITE",
        current_usd_m=1100.0,
        previous_usd_m=1000.0,
        config=cfg,
    )
    assert eval_pass.tier == "supplier"
    assert eval_pass.delta_usd_m == 100.0
    assert eval_pass.flagged_addition is False
    assert eval_pass.flagged_pct is False


def test_evaluate_pct_delta_threshold():
    """Test quarter-over-quarter percentage changes against 15% threshold."""
    cfg = CapexScannerConfig(capex_delta_flag_pct=15.0)

    # +20.1% change -> flagged_pct = True
    ev = evaluate_capex_change("AMZN", current_usd_m=12010.0, previous_usd_m=10000.0, config=cfg)
    assert ev.pct_delta == pytest.approx(20.1, abs=0.1)
    assert ev.flagged_pct is True

    # +3.3% change -> flagged_pct = False
    ev_steady = evaluate_capex_change("MSFT", current_usd_m=10330.0, previous_usd_m=10000.0, config=cfg)
    assert ev_steady.pct_delta == pytest.approx(3.3, abs=0.1)
    assert ev_steady.flagged_pct is False

    # Negative change -> flagged_pct = False
    ev_drop = evaluate_capex_change("INTC", current_usd_m=8000.0, previous_usd_m=10000.0, config=cfg)
    assert ev_drop.pct_delta == pytest.approx(-20.0, abs=0.1)
    assert ev_drop.flagged_pct is False


def test_evaluate_missing_previous_baseline_fails_closed():
    """When no previous baseline exists, delta is None and flagged is False."""
    cfg = CapexScannerConfig()
    ev = evaluate_capex_change("NEWTICKER", current_usd_m=500.0, previous_usd_m=None, config=cfg)
    assert ev.delta_usd_m is None
    assert ev.pct_delta is None
    assert ev.flagged_pct is False
    assert ev.flagged_addition is False
    assert ev.tier == "supplier"


def test_extract_capex_from_xbrl_facts_payload():
    """Verify extraction of quarterly capex facts from SEC XBRL facts structure."""
    sample_facts = {
        "cik": 1045810,
        "entityName": "NVIDIA CORP",
        "facts": {
            "us-gaap": {
                "PaymentsToAcquirePropertyPlantAndEquipment": {
                    "label": "Payments to Acquire Property, Plant and Equipment",
                    "units": {
                        "USD": [
                            {
                                "end": "2024-07-28",
                                "val": 1227000000,
                                "fy": 2025,
                                "fp": "Q2",
                                "form": "10-Q",
                                "filed": "2024-08-28",
                                "accn": "0001045810-24-000213",
                            },
                            {
                                "end": "2024-04-28",
                                "val": 583000000,
                                "fy": 2025,
                                "fp": "Q1",
                                "form": "10-Q",
                                "filed": "2024-05-29",
                                "accn": "0001045810-24-000120",
                            },
                        ]
                    },
                }
            }
        },
    }

    result = extract_capex_from_facts(sample_facts)
    assert result is not None
    period_end, amount_usd_m, accn = result
    assert period_end == "2024-07-28"
    assert amount_usd_m == 1227.0
    assert accn == "0001045810-24-000213"


def test_extract_capex_from_empty_facts_returns_none():
    """Missing or empty facts structure returns None (fail-closed)."""
    assert extract_capex_from_facts({}) is None
    assert extract_capex_from_facts({"facts": {}}) is None
    assert extract_capex_from_facts({"facts": {"us-gaap": {}}}) is None


def test_validate_sec_url_enforces_ssrf_safety():
    """Verify only approved SEC endpoints are allowed."""
    assert validate_sec_url("https://data.sec.gov/api/xbrl/companyfacts/CIK0001045810.json") is True
    assert validate_sec_url("https://www.sec.gov/files/company_tickers.json") is True

    # Reject non-SEC domains
    assert validate_sec_url("https://evil.com/data") is False
    assert validate_sec_url("http://data.sec.gov/insecure") is False
    assert validate_sec_url("ftp://data.sec.gov/file") is False
