"""Structural rules: live slots never reach this package (D5), every adapter implements the
source interface (D2), the shipped config matches the ADR, and fixtures stay clean."""

from __future__ import annotations

import ast
import inspect
import json
import os
import re
from pathlib import Path

import pytest

from .conftest import FIXTURES, INTL, US, FakeSession, pm

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(os.environ.get("DCA_REPO_ROOT", str(PACKAGE_ROOT.parent)))
PM_DIR = PACKAGE_ROOT / "market_data_ingest" / "prediction_markets"

# The modules that run the live buying slots (09:35, 10:30, 11:30, 12:30, 13:30, 14:30, 15:30;
# CLAUDE.md "Three entry points" plus the stream executors and the broker adapters).
LIVE_SLOT_MODULES = [
    "near_close_dca.py",
    "run_dca.py",
    "dca_runner.py",
    "morning_dca.py",
    "market_open_dca.py",
    "midday_dca.py",
    "intraday_dca.py",
    "dca_live_adapters.py",
]


def _imports(py_file: Path) -> set[str]:
    tree = ast.parse(py_file.read_text(), filename=str(py_file))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_live_slots_never_import_pm():
    assert PM_DIR.is_dir(), f"{PM_DIR} does not exist yet: nothing to guard"
    missing = [m for m in LIVE_SLOT_MODULES if not (REPO_ROOT / m).exists()]
    assert not missing, f"live-slot modules not found under REPO_ROOT={REPO_ROOT}: {missing}"
    hits = []
    for module in LIVE_SLOT_MODULES:
        for name in _imports(REPO_ROOT / module):
            if name.startswith(("market_data_ingest", "prediction_markets")):
                hits.append(f"{module} imports {name}")
    assert not hits, "a live buying slot reaches prediction-market data:\n" + "\n".join(hits)


def test_pm_never_imports_live_engine():
    assert PM_DIR.is_dir()
    forbidden = {"near_close_dca", "run_dca", "dca_runner", "morning_dca", "dca_live_adapters"}
    hits = [f"{p.name} imports {n}" for p in PM_DIR.rglob("*.py") for n in _imports(p) if n.split(".")[0] in forbidden]
    assert not hits, hits


@pytest.mark.parametrize(("module", "cls", "venue"), [
    ("polymarket_intl", "PolymarketIntlSource", INTL),
    ("polymarket_us", "PolymarketUSSource", US),
])
def test_adapters_implement_source_interface(config, clock, module, cls, venue):
    base = pm("source").PredictionMarketSource
    adapter = getattr(pm(module), cls)

    assert inspect.isabstract(base)
    assert issubclass(adapter, base)
    assert not inspect.isabstract(adapter), "every abstract method is implemented"
    assert adapter.venue == venue
    instance = adapter(FakeSession([]), config=config, clock=clock.now, sleep=clock.sleep)
    for method in ("fetch_event", "fetch_price_history", "fetch_orderbook"):
        assert callable(getattr(instance, method))


def test_shipped_config_matches_adr():
    """dca-sizing-signals/config/prediction_markets.json: ADR §3 initial scopes, §4 limits, §7 threshold."""
    cfg = pm("config").load_config(pm("config").DEFAULT_CONFIG_PATH)
    assert cfg.sources[INTL].license_scope == "store_ok"
    assert cfg.sources[US].license_scope == "live_only"
    assert cfg.liquidity_threshold_usd == 50000
    assert cfg.cadence.default_interval_s == 3600
    assert cfg.sources[INTL].max_history_batch <= 20
    raw = pm("config").DEFAULT_CONFIG_PATH.read_text()
    assert not re.search(r"(?i)(secret|private_key|password)\"\s*:", raw)


# --- fixture hygiene (passes today; guards future re-captures) -------------------------------

FIXTURE_FILES = sorted(FIXTURES.glob("*.json"))
CREDENTIAL_PATTERNS = [
    re.compile(r"(?i)x-pm-(access-key|signature|timestamp)\"?\s*[:=]"),
    re.compile(r"(?i)\"(api_?key|secret(_key)?|private_key|password|authorization|bearer)\"\s*:"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]


def test_fixtures_exist():
    assert len(FIXTURE_FILES) >= 10


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda p: p.name)
def test_fixture_is_a_documented_real_capture(path):
    doc = json.loads(path.read_text())
    prov = doc["_provenance"]
    assert prov["captured_at_utc"] and prov["url"].startswith("https://")
    assert "live response" in prov["recorded"]
    assert prov["http_status"] in (200, 400, 401, 410)
    assert "response" in doc
    host = prov["url"].split("/")[2]
    assert host in {"gamma-api.polymarket.com", "clob.polymarket.com", "data-api.polymarket.com",
                    "gateway.polymarket.us", "api.polymarket.us"}, "documented API hosts only, never web pages"


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda p: p.name)
def test_fixture_has_no_credentials(path):
    text = path.read_text()
    for pattern in CREDENTIAL_PATTERNS:
        assert not pattern.search(text), f"{path.name} matches {pattern.pattern}"
