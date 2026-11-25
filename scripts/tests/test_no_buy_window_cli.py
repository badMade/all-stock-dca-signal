"""Tests for ``scripts/no_buy_window.py``: the operator's early-release command for the dated ``no_buy`` block.

Written test-first from the operator's 2026-11-20 request ("a way to unblock them early if I decide") and the
compliance report's safety rule: releasing a ticker BEFORE its window ends can wash the loss, so it must
refuse unless the operator says so explicitly (``--accept-wash``) and gives a reason. A release never deletes
the entry (the audit trail stays in the file) and touches no other line of ``tickers.json``.
"""

import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dca_config import no_buy_blocked_tickers  # noqa: E402
from scripts import no_buy_window  # noqa: E402

BODY = """{
  "active": ["AAA", "BBB", "CCC"],
  "excluded": ["XXX"],
  "whole_share": ["WWW"],
  "no_buy": {
    "AAA": {"blocked_on": "2026-11-20", "until": "2026-12-21", "reason": "synthetic harvest", "released_on": null},
    "BBB": {"blocked_on": "2026-11-20", "until": "2026-12-21", "reason": "synthetic harvest", "released_on": null}
  },
  "notes": "untouched"
}
"""
IN_WINDOW = "2026-11-24"


@pytest.fixture
def cfg_dir(tmp_path):
    (tmp_path / "tickers.json").write_text(BODY)
    return tmp_path


def _run(cfg_dir, *argv):
    return no_buy_window.main([*argv, "--config-dir", str(cfg_dir)])


def _text(cfg_dir):
    return (cfg_dir / "tickers.json").read_text()


def test_release_inside_the_window_refuses_without_accept_wash(cfg_dir, capsys):
    rc = _run(cfg_dir, "release", "AAA", "--reason", "changed my mind", "--on", IN_WINDOW)
    assert rc == 2
    assert _text(cfg_dir) == BODY, "a refused release must not touch the file"
    assert "--accept-wash" in capsys.readouterr().err


def test_release_requires_a_reason(cfg_dir):
    assert _run(cfg_dir, "release", "AAA", "--accept-wash", "--on", IN_WINDOW) == 2
    assert _run(cfg_dir, "release", "AAA", "--accept-wash", "--reason", "  ", "--on", IN_WINDOW) == 2
    assert _text(cfg_dir) == BODY


def test_accepted_release_unblocks_from_that_day_and_keeps_the_entry(cfg_dir):
    assert _run(cfg_dir, "release", "AAA", "--accept-wash", "--reason", "want it back", "--on", IN_WINDOW) == 0
    cfg = json.loads(_text(cfg_dir))
    entry = cfg["no_buy"]["AAA"]
    assert entry["released_on"] == IN_WINDOW
    assert entry["release_reason"] == "want it back"
    assert entry["blocked_on"] == "2026-11-20" and entry["until"] == "2026-12-21"  # history preserved
    assert no_buy_blocked_tickers(cfg, date(2026, 11, 23)) == {"AAA", "BBB"}  # the day before: still blocked
    assert no_buy_blocked_tickers(cfg, date(2026, 11, 24)) == {"BBB"}  # released from that day
    assert no_buy_blocked_tickers(cfg, date(2026, 12, 1)) == {"BBB"}


def test_release_changes_only_the_released_entrys_line(cfg_dir):
    _run(cfg_dir, "release", "AAA", "--accept-wash", "--reason", "x", "--on", IN_WINDOW)
    before, after = BODY.splitlines(), _text(cfg_dir).splitlines()
    assert len(before) == len(after)
    changed = [i for i, (a, b) in enumerate(zip(before, after, strict=True)) if a != b]
    assert len(changed) == 1 and '"AAA"' in after[changed[0]]


def test_release_prints_the_wash_warning_and_the_resume_day(cfg_dir, capsys):
    _run(cfg_dir, "release", "AAA", "--accept-wash", "--reason", "x", "--on", IN_WINDOW)
    out = capsys.readouterr().out
    assert "AAA" in out and "2026-11-24" in out
    assert "wash" in out.lower()


def test_unknown_symbol_is_an_error(cfg_dir):
    assert _run(cfg_dir, "release", "ZZZ", "--accept-wash", "--reason", "x", "--on", IN_WINDOW) == 2
    assert _text(cfg_dir) == BODY


def test_already_released_symbol_is_an_error(cfg_dir):
    assert _run(cfg_dir, "release", "AAA", "--accept-wash", "--reason", "x", "--on", IN_WINDOW) == 0
    snapshot = _text(cfg_dir)
    assert _run(cfg_dir, "release", "AAA", "--accept-wash", "--reason", "again", "--on", "2026-11-25") == 2
    assert _text(cfg_dir) == snapshot


def test_release_after_the_window_needs_no_flag_and_changes_nothing(cfg_dir, capsys):
    assert _run(cfg_dir, "release", "AAA", "--reason", "x", "--on", "2026-12-22") == 2
    assert _text(cfg_dir) == BODY
    assert "already over" in capsys.readouterr().err.lower()


def test_release_dated_before_the_block_began_is_refused(cfg_dir):
    assert _run(cfg_dir, "release", "AAA", "--accept-wash", "--reason", "x", "--on", "2026-11-01") == 2
    assert _text(cfg_dir) == BODY


def test_a_malformed_block_is_refused_not_edited(cfg_dir):
    (cfg_dir / "tickers.json").write_text(BODY.replace('"until": "2026-12-21"', '"until": "nope"', 1))
    snapshot = _text(cfg_dir)
    assert _run(cfg_dir, "release", "BBB", "--accept-wash", "--reason", "x", "--on", IN_WINDOW) == 2
    assert _text(cfg_dir) == snapshot


def test_status_lists_every_entry_with_its_state(cfg_dir, capsys):
    _run(cfg_dir, "release", "AAA", "--accept-wash", "--reason", "x", "--on", IN_WINDOW)
    capsys.readouterr()
    assert _run(cfg_dir, "status", "--on", "2026-11-25") == 0
    out = capsys.readouterr().out
    assert "AAA" in out and "released" in out
    assert "BBB" in out and "blocked" in out
