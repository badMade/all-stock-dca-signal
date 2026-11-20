"""Tests for deriving ``near_close_dca.ACTIVE_TICKERS`` from ``config/tickers.json``.

Written BEFORE the implementation (TDD). Expectations come from
``docs/adr-program-maintained-universe.md`` decision item 1 and from the repo's fail-closed rule
(CLAUDE.md, "Hard invariants"), not from the code:

- ``config/tickers.json`` is the single source of the active list. ``near_close_dca.ACTIVE_TICKERS``
  is read from it at import time, in file order, from the same config directory every other loader
  uses (``$DCA_CONFIG_DIR``, then ``config/`` beside the modules, and so on).
- There is no hardcoded fallback. A missing, unreadable or malformed file refuses the import with a
  message naming ``tickers.json``, rather than silently trading a stale list. This is the same
  strictness ``dca_runner._validated_active_tickers`` already applies.
- A program that rewrites ``tickers.json`` therefore changes what every entry point buys, with no
  second copy to edit and no parity test to fail.

Every ticker below is SYNTHETIC (``AAA``, ``BBB``, ...) except where a test reads the real checkout
file on purpose.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import dca_config
import near_close_dca

REPO_ROOT = Path(__file__).resolve().parents[1]


def write_tickers(config_dir: Path, document: object) -> Path:
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "tickers.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def import_in_subprocess(config_dir: Path) -> subprocess.CompletedProcess:
    """Import near_close_dca fresh, with ``DCA_CONFIG_DIR`` pointing at ``config_dir``."""
    code = (
        "import json, near_close_dca as n; "
        "print(json.dumps({'active': n.ACTIVE_TICKERS, 'default': list(n.StrategyConfig().tickers)}))"
    )
    env = {**os.environ, "DCA_CONFIG_DIR": str(config_dir)}
    return subprocess.run(
        [sys.executable, "-c", code], cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=300
    )


# --- derivation ------------------------------------------------------------


def test_active_tickers_is_a_list_read_from_the_checkout_tickers_json():
    documented = json.loads((REPO_ROOT / "config" / "tickers.json").read_text(encoding="utf-8"))["active"]
    assert isinstance(near_close_dca.ACTIVE_TICKERS, list)
    assert near_close_dca.ACTIVE_TICKERS == documented  # same names, same order


def test_active_tickers_follows_the_configured_tickers_json(tmp_path):
    # A different file must give a different list: proof nothing is hardcoded.
    write_tickers(tmp_path, {"active": ["CCC", "AAA", "BBB"], "excluded": []})
    result = import_in_subprocess(tmp_path)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["active"] == ["CCC", "AAA", "BBB"]  # file order, not sorted
    assert payload["default"] == ["CCC", "AAA", "BBB"]  # a bare StrategyConfig() sees it too


def test_the_default_strategy_config_uses_the_derived_list():
    assert tuple(near_close_dca.StrategyConfig().tickers) == tuple(near_close_dca.ACTIVE_TICKERS)


# --- fail closed -----------------------------------------------------------


def test_a_missing_tickers_json_refuses_the_import(tmp_path):
    tmp_path.mkdir(exist_ok=True)  # an existing directory with no tickers.json
    result = import_in_subprocess(tmp_path)

    assert result.returncode != 0
    assert "tickers.json" in result.stderr


def test_importing_the_module_alone_does_not_need_a_valid_tickers_json(tmp_path):
    # Existing invariant (scripts/tests/test_generate_buy_candidates.py): a plain --help, or a run given
    # a valid --config-dir, must not crash on a stale $DCA_CONFIG_DIR. So the list is read on first USE,
    # which fails closed (test above), not when the module is imported.
    env = {**os.environ, "DCA_CONFIG_DIR": str(tmp_path / "does-not-exist")}
    result = subprocess.run(
        [sys.executable, "-c", "import near_close_dca"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, result.stderr


def test_the_list_is_read_once_and_reused():
    assert near_close_dca.ACTIVE_TICKERS is near_close_dca.ACTIVE_TICKERS


@pytest.mark.parametrize(
    "document",
    [
        pytest.param({"active": "AAPL"}, id="active_is_a_string"),
        pytest.param({"active": []}, id="active_is_empty"),
        pytest.param({"excluded": []}, id="active_key_missing"),
        pytest.param(["AAA", "BBB"], id="document_is_not_an_object"),
        pytest.param({"active": ["AAA", "aaa"]}, id="lowercase_symbol"),
        pytest.param({"active": ["AAA", " BBB"]}, id="padded_symbol"),
        pytest.param({"active": ["AAA", "TOOLONGSYM"]}, id="symbol_fails_the_schema_pattern"),
        pytest.param({"active": ["AAA", "BBB", "AAA"]}, id="duplicate_symbol"),
        pytest.param({"active": ["AAA", 7]}, id="non_string_entry"),
    ],
)
def test_a_malformed_active_list_is_refused(tmp_path, document):
    write_tickers(tmp_path, document)
    with pytest.raises(RuntimeError, match="tickers.json"):
        near_close_dca._load_active_tickers(tmp_path)


def test_a_file_that_is_not_json_is_refused(tmp_path):
    (tmp_path / "tickers.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="tickers.json"):
        near_close_dca._load_active_tickers(tmp_path)


def test_duplicate_json_keys_are_refused(tmp_path):
    # dca_config rejects duplicate keys; a second "active" must not silently win here either.
    (tmp_path / "tickers.json").write_text('{"active": ["AAA"], "active": ["BBB"]}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="tickers.json"):
        near_close_dca._load_active_tickers(tmp_path)


def test_a_valid_file_loads_in_file_order(tmp_path):
    write_tickers(tmp_path, {"active": ["ZZZ", "AAA", "MMM"]})
    assert near_close_dca._load_active_tickers(tmp_path) == ["ZZZ", "AAA", "MMM"]


# --- one config directory rule, not two --------------------------------------


def test_the_import_time_loader_uses_the_same_config_dir_as_dca_config(monkeypatch, tmp_path):
    # near_close_dca cannot import dca_config (dca_config imports near_close_dca), so it carries its
    # own copy of the directory rule. This keeps the two from drifting apart.
    monkeypatch.setenv("DCA_CONFIG_DIR", str(tmp_path))
    assert near_close_dca._universe_config_dir() == dca_config.get_config_dir() == tmp_path

    monkeypatch.delenv("DCA_CONFIG_DIR")
    assert near_close_dca._universe_config_dir() == dca_config.get_config_dir()
