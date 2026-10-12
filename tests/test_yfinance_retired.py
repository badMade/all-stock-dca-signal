"""Owner direction (2026-10-12): no scheduled run may depend on Yahoo Finance (yfinance).

"remove yahoo finance from all scheduled runs ... I will need to find another backup. not yahoo finance."
Robinhood is the only market-data source for the Robinhood-primary slots; when it is unavailable or incomplete the
run aborts with nothing placed, it never routes to a labelled yfinance fallback. Written before the removal; these
fail until it lands.

Out of scope on purpose: ``yfinance-prefetch/`` and the research trees (``analysis/``, ``trading-strategies/``,
``ticker-advisor/`` ...) are not scheduled paths, and the Finance *connector* (``SourceName.FINANCE``, the 15:30
primary) is a different source from the yfinance fallback.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REAL_CONFIG_DIR = ROOT / "config"
DAY = "2026-08-05"
FALLBACK = "yahoo_finance_yfinance"

GENERATORS = [
    "scripts.generate_market_open_candidates",
    "scripts.generate_morning_candidates",
    "scripts.generate_midday_candidates",
    "scripts.generate_intraday_candidates",
]


def _run(module_name, tmp_path, payload, *extra):
    module = pytest.importorskip(module_name)
    path = tmp_path / "market_data.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    argv = ["--trading-day", DAY, "--market-data", str(path), "--config-dir", str(REAL_CONFIG_DIR), *extra]
    if module_name.endswith("intraday_candidates"):
        argv += ["--slot", "1130"]
    if module_name.endswith("midday_candidates"):  # the only generator whose producer contract needs the kill-switch input
        argv += ["--deployed-last-7d-usd", "0"]
    with contextlib.redirect_stdout(io.StringIO()):
        return module.main(argv)


@pytest.mark.parametrize("module_name", GENERATORS)
def test_a_yfinance_payload_is_rejected_as_an_unsupported_source(tmp_path, module_name):
    payload = {"sources": {"robinhood": {}, FALLBACK: {}}, "source_errors": [], "ticker_errors": {"robinhood": []}}
    with pytest.raises(ValueError, match=FALLBACK):
        _run(module_name, tmp_path, payload, "--already-covered-tickers", "TEST")


@pytest.mark.parametrize("module_name", GENERATORS)
def test_a_declared_yfinance_source_error_is_rejected_as_unsupported(tmp_path, module_name):
    payload = {"sources": {"robinhood": {}}, "source_errors": [FALLBACK], "ticker_errors": {"robinhood": []}}
    with pytest.raises(ValueError, match=FALLBACK):
        _run(module_name, tmp_path, payload, "--already-covered-tickers", "TEST")


@pytest.mark.parametrize("module_name", GENERATORS)
@pytest.mark.parametrize(
    "payload",
    [
        {"sources": {"robinhood": {}}, "source_errors": ["robinhood"]},  # declared outage
        {"sources": {"robinhood": {}}, "source_errors": []},  # undeclared empty payload
    ],
    ids=["declared-outage", "undeclared-empty"],
)
def test_an_unusable_robinhood_payload_aborts_without_offering_a_fallback(tmp_path, module_name, payload):
    with pytest.raises(ValueError) as caught:
        _run(module_name, tmp_path, payload)
    assert "yfinance" not in str(caught.value).lower()


def test_the_fallback_source_no_longer_exists():
    from scripts import stream_candidates

    assert not hasattr(stream_candidates.SourceName, "YFINANCE_FALLBACK")


# --- nothing operative in config, schemas, prompts or the scheduled code names it ---------------------------------

_NAMES = re.compile(r"yfinance|yahoo", re.IGNORECASE)
# Python is scanned for the identifiers only, so prose in a docstring does not trip it.
_CODE_NAMES = re.compile(r"yahoo_finance_yfinance|YFINANCE_FALLBACK|import yfinance|from yfinance", re.IGNORECASE)
_HISTORY_KEYS = re.compile(r"^change_note|_20\d\d_\d\d|^supersedes_|^added_reason$|^id_note$|^must_not_reintroduce$")

# The live-mirror prompts are the canonical executor instructions; drafts/ holds superseded bridge-shell drafts that no
# scheduled task reads.
TEXT_GLOBS = ("config/*.json", "schemas/*.json", "claude/dca-executor-prompts/live-mirror/*.txt")
CODE_GLOBS = (
    "scripts/stream_candidates.py", "scripts/generate_*_candidates.py", "scripts/backup_*.py",
    "scripts/ingest_market_data.py", "scripts/close_window_signal.py", "dca_live_adapters.py",
    "near_close_dca.py", "market_open_dca.py", "morning_dca.py", "midday_dca.py", "intraday_dca.py",
    "deploy/**/*.py", "deploy/**/*.sh",
)


def _operative_json_strings(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if _HISTORY_KEYS.search(key):
                continue
            yield key
            yield from _operative_json_strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from _operative_json_strings(item)
    elif isinstance(node, str):
        yield node


def _offenders(globs, pattern, *, json_aware):
    found = {}
    for glob in globs:
        for path in sorted(ROOT.glob(glob)):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if json_aware and path.suffix == ".json":
                text = "\n".join(_operative_json_strings(json.loads(text)))
            hits = sorted({m.group(0).lower() for m in pattern.finditer(text)})
            if hits:
                found[path.relative_to(ROOT).as_posix()] = hits[:4]
    return found


def test_no_operative_config_schema_or_prompt_names_yfinance():
    offenders = _offenders(TEXT_GLOBS, _NAMES, json_aware=True)
    assert offenders == {}, json.dumps(offenders, indent=1)


def test_no_scheduled_code_path_references_the_fallback():
    offenders = _offenders(CODE_GLOBS, _CODE_NAMES, json_aware=False)
    assert offenders == {}, json.dumps(offenders, indent=1)
