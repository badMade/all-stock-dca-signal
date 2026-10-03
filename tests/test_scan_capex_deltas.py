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

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VAULT_ROOT = REPO_ROOT / "agent-vault"
SCRIPTS_DIR = VAULT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import pytest  # noqa: E402
from scan_capex_deltas import (  # noqa: E402
    UNSCANNABLE_TICKERS,
    CapexEvaluation,
    CapexScannerConfig,
    SECRateLimiter,
    SECRedirectDisallowedHandler,
    difference_quarterly_capex,
    evaluate_capex_change,
    extract_capex_from_facts,
    fetch_company_facts_from_sec,
    fetch_company_facts_offline_or_sec,
    fetch_sec_company_tickers,
    format_capex_scan_report,
    load_scanner_config,
    parse_sec_company_tickers_mapping,
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
        "cik": 1_045_810,
        "entityName": "NVIDIA CORP",
        "facts": {
            "us-gaap": {
                "PaymentsToAcquirePropertyPlantAndEquipment": {
                    "label": "Payments to Acquire Property, Plant and Equipment",
                    "units": {
                        "USD": [
                            {
                                "end": "2024-07-28",
                                "val": 1_227_000_000,
                                "fy": 2025,
                                "fp": "Q2",
                                "form": "10-Q",
                                "filed": "2024-08-28",
                                "accn": "ACCN-NVDA-Q2",
                            },
                            {
                                "end": "2024-04-28",
                                "val": 583_000_000,
                                "fy": 2025,
                                "fp": "Q1",
                                "form": "10-Q",
                                "filed": "2024-05-29",
                                "accn": "ACCN-NVDA-Q1",
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
    assert accn == "ACCN-NVDA-Q2"


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


def test_difference_quarterly_capex_from_ytd_cash_flows():
    """Verify quarterly subtraction from cumulative YTD facts (Q1..Q4)."""
    facts = [
        {"fp": "Q1", "fy": 2025, "val": 500_000_000, "end": "2024-04-30", "accn": "ACCN-Q1"},
        {"fp": "Q2", "fy": 2025, "val": 1_200_000_000, "end": "2024-07-31", "accn": "ACCN-Q2"},
        {"fp": "Q3", "fy": 2025, "val": 2_100_000_000, "end": "2024-10-31", "accn": "ACCN-Q3"},
        {"fp": "FY", "fy": 2025, "val": 3_200_000_000, "end": "2025-01-31", "accn": "ACCN-FY"},
    ]
    quarterly = difference_quarterly_capex(facts)
    # Q1: 500M
    assert quarterly["Q1"].amount_usd_m == 500.0
    # Q2: 1200M - 500M = 700M
    assert quarterly["Q2"].amount_usd_m == 700.0
    # Q3: 2100M - 1200M = 900M
    assert quarterly["Q3"].amount_usd_m == 900.0
    # Q4: FY(3200M) - Q3(2100M) = 1100M
    assert quarterly["Q4"].amount_usd_m == 1100.0


def test_difference_quarterly_capex_carries_accession_provenance():
    """Every derived quarter names the accession of each input fact (PR 1590 review: no source, no number)."""
    facts = [
        {"fp": "Q1", "fy": 2025, "val": 500_000_000, "end": "2024-04-30", "accn": "ACCN-Q1"},
        {"fp": "Q2", "fy": 2025, "val": 1_200_000_000, "end": "2024-07-31", "accn": "ACCN-Q2"},
        {"fp": "Q3", "fy": 2025, "val": 2_100_000_000, "end": "2024-10-31", "accn": "ACCN-Q3"},
        {"fp": "FY", "fy": 2025, "val": 3_200_000_000, "end": "2025-01-31", "accn": "ACCN-FY"},
    ]
    quarterly = difference_quarterly_capex(facts)
    # A direct quarter cites its one filing; a differenced quarter cites both minuend and subtrahend.
    assert quarterly["Q1"].source == "ACCN-Q1"
    assert quarterly["Q2"].source == "ACCN-Q2 - ACCN-Q1"
    assert quarterly["Q4"].source == "ACCN-FY - ACCN-Q3"


def test_difference_quarterly_capex_fails_closed_without_provenance():
    """A fact with no accession/URL is dropped rather than labelled with a generic source."""
    facts = [
        {"fp": "Q1", "fy": 2025, "val": 500_000_000, "end": "2024-04-30"},
        {"fp": "Q2", "fy": 2025, "val": 1_200_000_000, "end": "2024-07-31", "accn": "ACCN-Q2"},
    ]
    quarterly = difference_quarterly_capex(facts)
    assert "Q1" not in quarterly
    assert "Q2" not in quarterly  # its subtrahend has no source


def test_difference_quarterly_capex_fails_closed_on_missing_or_decreasing_ytd():
    """Differencing fails closed if a prior quarter is missing or cumulative total drops."""
    # Missing Q2 prior to Q3
    gapped_facts = [
        {"fp": "Q1", "fy": 2025, "val": 500_000_000, "end": "2024-04-30", "accn": "ACCN-Q1"},
        {"fp": "Q3", "fy": 2025, "val": 2_100_000_000, "end": "2024-10-31", "accn": "ACCN-Q3"},
    ]
    q_gapped = difference_quarterly_capex(gapped_facts)
    assert "Q1" in q_gapped
    assert "Q3" not in q_gapped  # Cannot difference without Q2

    # Malformed decreasing cumulative value (accounting revision or error)
    decreasing_facts = [
        {"fp": "Q1", "fy": 2025, "val": 500_000_000, "end": "2024-04-30", "accn": "ACCN-Q1"},
        {"fp": "Q2", "fy": 2025, "val": 400_000_000, "end": "2024-07-31", "accn": "ACCN-Q2"},
    ]
    q_dec = difference_quarterly_capex(decreasing_facts)
    assert "Q1" in q_dec
    assert "Q2" not in q_dec  # Fails closed on negative differenced amount


def test_parse_sec_company_tickers_mapping():
    """Verify parsing SEC company_tickers.json into symbol -> 10-digit CIK map."""
    sample_payload = json.dumps({
        "0": {"cik_str": 320_193, "ticker": "AAPL", "title": "Apple Inc."},
        "1": {"cik_str": 1_045_810, "ticker": "NVDA", "title": "NVIDIA CORP"},
        "2": {"cik_str": 1_652_044, "ticker": "GOOGL", "title": "Alphabet Inc."},
    }).encode("utf-8")

    mapping = parse_sec_company_tickers_mapping(sample_payload)
    assert mapping["AAPL"] == f"{320_193:010d}"
    assert mapping["NVDA"] == f"{1_045_810:010d}"
    assert mapping["GOOGL"] == f"{1_652_044:010d}"
    assert mapping.get("UNKNOWN") is None


def test_sec_rate_limiter_pacing_with_injected_clock():
    """Verify rate limiter enforces max 10 requests/sec using injectable time/sleep."""
    current_time = 100.0
    sleeps: list[float] = []

    def mock_time() -> float:
        return current_time

    def mock_sleep(seconds: float) -> None:
        nonlocal current_time
        sleeps.append(round(seconds, 4))
        current_time += seconds

    limiter = SECRateLimiter(min_interval=0.1, time_fn=mock_time, sleep_fn=mock_sleep)

    # First request: immediate, no sleep
    limiter.acquire()
    assert len(sleeps) == 0

    # Second request 0.02s later: must sleep remaining 0.08s
    current_time += 0.02
    limiter.acquire()
    assert len(sleeps) == 1
    assert sleeps[0] == pytest.approx(0.08, abs=0.001)

    # Third request 0.15s later: exceeds min_interval, no sleep
    current_time += 0.15
    limiter.acquire()
    assert len(sleeps) == 1


def test_evaluate_capex_change_and_gate_contract():
    """Verify decision 1 AND gate and contract states (flagged, passed, baseline_recorded, not_scannable)."""
    cfg = CapexScannerConfig(
        capex_delta_flag_pct=15.0,
        hyperscaler_spend_threshold_usd_m=5000.0,
        hyperscaler_addition_usd_m=2000.0,
        supplier_addition_usd_m=250.0,
    )

    # 1. Hyperscaler trips both gates (+25% delta, +$2,500M) -> flagged
    ev_flagged = evaluate_capex_change(
        ticker="MSFT",
        current_usd_m=12500.0,
        previous_usd_m=10000.0,
        config=cfg,
    )
    assert ev_flagged.status == "flagged"
    assert ev_flagged.flagged_pct is True
    assert ev_flagged.flagged_addition is True

    # 2. Hyperscaler trips addition gate (+2,100M) but NOT delta (2100/30000 = 7%) -> passed
    ev_passed_pct = evaluate_capex_change(
        ticker="AMZN",
        current_usd_m=32100.0,
        previous_usd_m=30000.0,
        config=cfg,
    )
    assert ev_passed_pct.status == "passed"
    assert ev_passed_pct.flagged_addition is True
    assert ev_passed_pct.flagged_pct is False

    # 3. Supplier trips delta gate (+20%) but NOT dollar threshold (+$100M < $250M) -> passed
    ev_passed_add = evaluate_capex_change(
        ticker="LITE",
        current_usd_m=600.0,
        previous_usd_m=500.0,
        config=cfg,
    )
    assert ev_passed_add.status == "passed"
    assert ev_passed_add.flagged_pct is True
    assert ev_passed_add.flagged_addition is False

    # 4. No previous baseline -> baseline_recorded
    ev_baseline = evaluate_capex_change(
        ticker="NEWCO",
        current_usd_m=450.0,
        previous_usd_m=None,
        config=cfg,
    )
    assert ev_baseline.status == "baseline_recorded"

    # 5. Unscannable tickers list check
    assert "TSM" in UNSCANNABLE_TICKERS
    assert "ASML" in UNSCANNABLE_TICKERS
    assert "STM" in UNSCANNABLE_TICKERS
    assert "CBRS" in UNSCANNABLE_TICKERS
    assert "SPCX" in UNSCANNABLE_TICKERS


def test_format_capex_scan_report():
    """Verify Markdown report formatting with summary statistics."""
    evals = [
        CapexEvaluation(
            ticker="NVDA",
            current_usd_m=1500.0,
            previous_usd_m=1000.0,
            delta_usd_m=500.0,
            pct_delta=50.0,
            flagged_pct=True,
            flagged_addition=True,
            tier="supplier",
            status="flagged",
            company_name="NVIDIA Corporation",
            period_end="2025-07-27",
            notes="Breached 15% QoQ delta and $250M supplier tier",
        ),
        CapexEvaluation(
            ticker="MSFT",
            current_usd_m=35802.0,
            previous_usd_m=34000.0,
            delta_usd_m=1802.0,
            pct_delta=5.3,
            flagged_pct=False,
            flagged_addition=False,
            tier="hyperscaler",
            status="passed",
            company_name="Microsoft Corporation",
            period_end="2026-06-30",
            notes="Within thresholds",
        ),
        CapexEvaluation(
            ticker="QBTS",
            current_usd_m=15.0,
            previous_usd_m=None,
            delta_usd_m=None,
            pct_delta=None,
            flagged_pct=False,
            flagged_addition=False,
            tier="supplier",
            status="baseline_recorded",
            company_name="D-Wave Quantum Inc.",
            period_end="2026-06-30",
            notes="New baseline recorded",
        ),
        CapexEvaluation(
            ticker="TSM",
            current_usd_m=0.0,
            previous_usd_m=None,
            delta_usd_m=None,
            pct_delta=None,
            flagged_pct=False,
            flagged_addition=False,
            tier="hyperscaler",
            status="not_scannable",
            company_name="Taiwan Semiconductor Manufacturing Co.",
            period_end="",
            notes="Form 20-F annual filer",
        ),
    ]

    report = format_capex_scan_report(evals, title="Weekly CapEx Scan Report")
    assert "## Weekly CapEx Scan Report" in report
    assert "| Ticker | Company | Period End | Tier | Current ($M) | Prior ($M) | Delta ($M) | Delta (%) | Status | Notes |" in report
    assert "| NVDA | NVIDIA Corporation | 2025-07-27 | supplier | $1,500.0M | $1,000.0M | +$500.0M | +50.0% | FLAGGED |" in report
    assert "| MSFT | Microsoft Corporation | 2026-06-30 | hyperscaler | $35,802.0M | $34,000.0M | +$1,802.0M | +5.3% | PASS |" in report
    assert "| QBTS | D-Wave Quantum Inc. | 2026-06-30 | supplier | $15.0M | N/A | N/A | N/A | BASELINE_RECORDED |" in report
    assert "| TSM | Taiwan Semiconductor Manufacturing Co. | N/A | hyperscaler | N/A | N/A | N/A | N/A | NOT_SCANNABLE |" in report

    # Check summary counts
    assert "Total Filers Evaluated: 4" in report
    assert "Flagged: 1" in report
    assert "Passed: 1" in report
    assert "Baselines Recorded: 1" in report
    assert "Not Scannable: 1" in report


def test_fetch_sec_company_tickers_success():
    """Verify fetching and parsing company tickers mapping from mock SEC endpoint."""
    sample_json = json.dumps({
        "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
        "1": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"},
    }).encode("utf-8")

    def mock_fetch(url: str, headers: dict[str, str], timeout: float) -> bytes:
        assert url == "https://www.sec.gov/files/company_tickers.json"
        assert "User-Agent" in headers
        return sample_json

    mapping = fetch_sec_company_tickers(user_agent="TestAgent/1.0", timeout=5.0, fetch_fn=mock_fetch)
    assert mapping["AAPL"] == f"{320193:010d}"
    assert mapping["NVDA"] == f"{1045810:010d}"


def test_fetch_sec_company_tickers_failure_fails_closed():
    """Network failure or non-200 response when fetching tickers returns empty dict."""
    def mock_fetch(url: str, headers: dict[str, str], timeout: float) -> bytes:
        raise OSError("Connection timed out")

    mapping = fetch_sec_company_tickers(user_agent="TestAgent/1.0", timeout=5.0, fetch_fn=mock_fetch)
    assert mapping == {}


def test_fetch_company_facts_from_sec_success():
    """Verify fetching company facts for a CIK from mock SEC endpoint."""
    sample_facts = {
        "cik": 1045810,
        "entityName": "NVIDIA CORP",
        "facts": {
            "us-gaap": {
                "PaymentsToAcquirePropertyPlantAndEquipment": {
                    "units": {
                        "USD": [
                            {"end": "2024-07-28", "val": 1227000000, "form": "10-Q", "accn": "ACCN-001"}
                        ]
                    }
                }
            }
        },
    }

    def mock_fetch(url: str, headers: dict[str, str], timeout: float) -> bytes:
        assert url == f"https://data.sec.gov/api/xbrl/companyfacts/CIK{1045810:010d}.json"
        assert "User-Agent" in headers
        assert "application/json" in headers.get("Accept", "")
        return json.dumps(sample_facts).encode("utf-8")

    facts = fetch_company_facts_from_sec(
        cik="1045810",
        user_agent="TestAgent/1.0",
        timeout=5.0,
        fetch_fn=mock_fetch,
    )
    assert facts is not None
    assert facts.get("entityName") == "NVIDIA CORP"


def test_fetch_company_facts_from_sec_rate_limiting():
    """Verify SECRateLimiter.acquire() is called during SEC facts fetch."""
    calls = []

    class MockLimiter(SECRateLimiter):
        def acquire(self) -> None:
            calls.append(1)

    limiter = MockLimiter()

    def mock_fetch(url: str, headers: dict[str, str], timeout: float) -> bytes:
        return b'{"cik": 1045810}'

    facts = fetch_company_facts_from_sec(
        cik="1045810",
        limiter=limiter,
        fetch_fn=mock_fetch,
    )
    assert facts == {"cik": 1045810}
    assert len(calls) == 1


def test_fetch_company_facts_from_sec_network_error_fails_closed():
    """Network errors or malformed payloads return None (fail closed)."""
    def mock_error_fetch(url: str, headers: dict[str, str], timeout: float) -> bytes:
        raise OSError("Network unreachable")

    assert fetch_company_facts_from_sec("1045810", fetch_fn=mock_error_fetch) is None

    def mock_bad_json(url: str, headers: dict[str, str], timeout: float) -> bytes:
        return b"not json"

    assert fetch_company_facts_from_sec("1045810", fetch_fn=mock_bad_json) is None


def test_fetch_company_facts_offline_or_sec_end_to_end():
    """End-to-end ticker resolution: ticker -> CIK -> facts -> extracted capex."""
    sample_facts = {
        "cik": 1045810,
        "facts": {
            "us-gaap": {
                "PaymentsToAcquirePropertyPlantAndEquipment": {
                    "units": {
                        "USD": [
                            {"end": "2024-07-28", "val": 1227000000, "form": "10-Q", "accn": "ACCN-NVDA"}
                        ]
                    }
                }
            }
        },
    }
    cik_map = {"NVDA": f"{1045810:010d}"}

    def mock_fetch(url: str, headers: dict[str, str], timeout: float) -> bytes:
        return json.dumps(sample_facts).encode("utf-8")

    facts = fetch_company_facts_offline_or_sec(
        ticker="NVDA",
        cik_mapping=cik_map,
        fetch_fn=mock_fetch,
    )
    assert facts is not None
    extracted = extract_capex_from_facts(facts)
    assert extracted is not None
    end, val, accn = extracted
    assert end == "2024-07-28"
    assert val == 1227.0
    assert accn == "ACCN-NVDA"


def test_fetch_company_facts_offline_or_sec_unknown_ticker():
    """Unknown ticker returns None without making network calls."""
    calls = []

    def mock_fetch(url: str, headers: dict[str, str], timeout: float) -> bytes:
        calls.append(url)
        return b"{}"

    facts = fetch_company_facts_offline_or_sec(
        ticker="UNKNOWNXYZ",
        cik_mapping={"NVDA": f"{1045810:010d}"},
        fetch_fn=mock_fetch,
    )
    assert facts is None
    assert len(calls) == 0


def test_sec_redirect_disallowed_handler_refuses_redirect():
    """Verify SECRedirectDisallowedHandler returns None to prevent following redirects."""
    handler = SECRedirectDisallowedHandler()
    import urllib.request
    req = urllib.request.Request("https://data.sec.gov/test")
    res = handler.redirect_request(req, None, 302, "Found", {}, "https://evil.com")
    assert res is None



