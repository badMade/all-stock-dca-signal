"""Tests for universe_apply.py -- the gated writer that applies an approved universe proposal.

Written BEFORE the implementation (TDD). Every expectation below is derived from
``docs/adr-program-maintained-universe.md`` (decision items 3 to 8), not from code:

- the program may rewrite ``active`` in ``config/tickers.json`` only from a proposal that passed
  every safeguard (``status == "pending_approval"``), whose content hash still matches, and that
  carries an approval id;
- any failed check leaves the list exactly as it was (fail closed, byte for byte);
- there is NO limit on how many names change in one day (operator-directed 2026-11-19); the
  cap of 100 and the 90 name floor still apply;
- ``excluded``, ``whole_share``, ``symbol_corrections`` and every note stay hand-held;
- the write is atomic and touches nothing but ``tickers.json`` and the history file;
- it is a buy list only: dropping a name records "stopped buying" and nothing else.

Every input is SYNTHETIC: placeholder tickers ``SAA``, ``SAB``, ... (letters only, because the
ticker schema pattern is ``^[A-Z.]{1,6}$``). No figure is real market data.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from universe import Proposal, UniverseConfig, write_proposal
from universe_apply import ApplyRefused, apply_proposal

TODAY = date(2026, 11, 20)


def names(count: int, start: int = 0) -> list[str]:
    """Placeholder tickers SAA, SAB, ... in a fixed order."""
    return [f"S{chr(65 + (i // 26))}{chr(65 + (i % 26))}" for i in range(start, start + count)]


def make_proposal(
    prior: list[str],
    active: list[str],
    *,
    status: str = "pending_approval",
    hard_fail_drops: tuple[str, ...] = (),
) -> Proposal:
    adds = tuple(t for t in active if t not in prior)
    drops = tuple(t for t in prior if t not in active)
    return Proposal(
        review_id="2026-11-20",
        as_of_date=TODAY,
        generated_at=datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc),
        prior_active=tuple(prior),
        active=tuple(active),
        adds=adds,
        drops=drops,
        hard_fail_drops=hard_fail_drops,
        holds=(),
        bench=(),
        ranks={t: i + 1 for i, t in enumerate(active)},
        reasons={},
        pool_stats=None,
        config=UniverseConfig(),
        status=status,
        recommended_active=tuple(active) if status == "pending_approval" else tuple(prior),
    )


@pytest.fixture
def workspace(tmp_path: Path):
    """A tickers.json with 100 active names plus the hand-held fields, and a history file."""
    prior = names(100)
    tickers = tmp_path / "tickers.json"
    document = {
        "version": "2026-11-06",
        "active": prior,
        "excluded": ["QBUT"],
        "whole_share": ["BYDDY"],
        "symbol_corrections": {"GLWR": "GLW"},
        "data_source_notes": {prior[0]: "operator note"},
        "notes": "hand-written",
    }
    tickers.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    history = tmp_path / "ticker_history.csv"
    history.write_text("date,ticker\n2026-11-06,SAA\n", encoding="utf-8")
    return tmp_path, tickers, history, prior


def proposal_file(tmp_path: Path, proposal: Proposal) -> Path:
    path = tmp_path / "universe_proposal.json"
    write_proposal(path, proposal)
    return path


def snapshot(*paths: Path) -> dict[Path, bytes]:
    return {p: p.read_bytes() for p in paths}


def apply(tmp_path, tickers, history, proposal, **overrides):
    kwargs = {"approval_id": "adr-standing-approval", "applied_on": TODAY}
    kwargs.update(overrides)
    return apply_proposal(proposal_file(tmp_path, proposal), tickers, history, **kwargs)


def assert_refused_and_unchanged(code, tmp_path, tickers, history, proposal, **overrides):
    before = snapshot(tickers, history)
    with pytest.raises(ApplyRefused) as excinfo:
        apply(tmp_path, tickers, history, proposal, **overrides)
    assert excinfo.value.code == code
    assert snapshot(tickers, history) == before


# --- the happy path -------------------------------------------------------


def test_applies_a_passing_proposal(workspace):
    tmp_path, tickers, history, prior = workspace
    new_active = prior[3:] + names(3, start=100)
    result = apply(tmp_path, tickers, history, make_proposal(prior, new_active))

    document = json.loads(tickers.read_text(encoding="utf-8"))
    assert document["active"] == new_active
    assert result.applied is True
    assert result.added == tuple(names(3, start=100))
    assert result.stopped_buying == tuple(prior[:3])
    assert len(result.content_sha256) == 64


def test_hand_held_fields_are_never_rewritten(workspace):
    tmp_path, tickers, history, prior = workspace
    before = json.loads(tickers.read_text(encoding="utf-8"))
    apply(tmp_path, tickers, history, make_proposal(prior, prior[1:] + names(1, start=100)))
    after = json.loads(tickers.read_text(encoding="utf-8"))

    for key in ("version", "excluded", "whole_share", "symbol_corrections", "data_source_notes", "notes"):
        assert after[key] == before[key], key


def test_appends_the_new_active_set_to_history_in_the_existing_format(workspace):
    tmp_path, tickers, history, prior = workspace
    new_active = prior[1:] + names(1, start=100)
    apply(tmp_path, tickers, history, make_proposal(prior, new_active))

    lines = history.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "date,ticker"
    assert lines[1] == "2026-11-06,SAA"  # earlier rows are never rewritten
    assert lines[2:] == [f"{TODAY.isoformat()},{t}" for t in new_active]


def test_writes_nothing_but_the_two_target_files(workspace):
    tmp_path, tickers, history, prior = workspace
    proposal = make_proposal(prior, prior[1:] + names(1, start=100))
    proposal_path = proposal_file(tmp_path, proposal)
    apply_proposal(proposal_path, tickers, history, approval_id="a", applied_on=TODAY)

    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
        ["tickers.json", "ticker_history.csv", "universe_proposal.json"]
    )


# --- fail closed: every refusal leaves both files byte-identical -------------


def test_refuses_without_an_approval_id(workspace):
    tmp_path, tickers, history, prior = workspace
    assert_refused_and_unchanged(
        "no_approval_id", tmp_path, tickers, history, make_proposal(prior, prior[1:] + names(1, start=100)),
        approval_id="",
    )


def test_refuses_a_proposal_that_did_not_pass_the_safeguards(workspace):
    tmp_path, tickers, history, prior = workspace
    assert_refused_and_unchanged(
        "status_not_pending_approval", tmp_path, tickers, history,
        make_proposal(prior, prior[1:] + names(1, start=100), status="rejected"),
    )


def test_refuses_a_tampered_proposal(workspace):
    tmp_path, tickers, history, prior = workspace
    path = proposal_file(tmp_path, make_proposal(prior, prior[1:] + names(1, start=100)))
    document = json.loads(path.read_text(encoding="utf-8"))
    document["active"] = document["active"] + ["ZZZ"]  # content changed, content_sha256 left stale
    path.write_text(json.dumps(document), encoding="utf-8")

    before = snapshot(tickers, history)
    with pytest.raises(ApplyRefused) as excinfo:
        apply_proposal(path, tickers, history, approval_id="a", applied_on=TODAY)
    assert excinfo.value.code == "hash_mismatch"
    assert snapshot(tickers, history) == before


def test_refuses_a_proposal_built_on_a_stale_base(workspace):
    tmp_path, tickers, history, prior = workspace
    stale_prior = prior[1:] + names(1, start=200)  # not what tickers.json holds today
    assert_refused_and_unchanged(
        "stale_base", tmp_path, tickers, history, make_proposal(stale_prior, stale_prior[1:] + names(1, start=300)),
    )


def test_applies_any_number_of_changes_in_one_day(workspace):
    tmp_path, tickers, history, prior = workspace
    # 20 exits and 20 entries in one day is still applied: the operator set no per-day limit.
    new_active = prior[20:] + names(20, start=100)
    result = apply(tmp_path, tickers, history, make_proposal(prior, new_active))
    assert result.applied is True
    assert json.loads(tickers.read_text(encoding="utf-8"))["active"] == new_active


def test_never_exceeds_the_cap_of_100(workspace):
    tmp_path, tickers, history, prior = workspace
    assert_refused_and_unchanged(
        "over_max_active", tmp_path, tickers, history, make_proposal(prior, names(101, start=500)),
    )


def test_refuses_a_name_on_the_excluded_list(workspace):
    tmp_path, tickers, history, prior = workspace
    assert_refused_and_unchanged(
        "excluded_name", tmp_path, tickers, history, make_proposal(prior, prior[1:] + ["QBUT"]),
    )


def test_refuses_a_symbol_the_schema_would_reject(workspace):
    tmp_path, tickers, history, prior = workspace
    assert_refused_and_unchanged(
        "invalid_symbol", tmp_path, tickers, history, make_proposal(prior, prior[1:] + ["bad1"]),
    )


def test_refuses_a_list_below_the_floor_of_90(workspace):
    tmp_path, tickers, history, prior = workspace
    assert_refused_and_unchanged(
        "below_min_active", tmp_path, tickers, history, make_proposal(prior, prior[:80]),
    )


# --- atomic write ----------------------------------------------------------


def test_a_failed_write_leaves_the_original_and_no_temp_files(workspace, monkeypatch):
    tmp_path, tickers, history, prior = workspace
    proposal_path = proposal_file(tmp_path, make_proposal(prior, prior[1:] + names(1, start=100)))
    before = snapshot(tickers, history)

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        apply_proposal(proposal_path, tickers, history, approval_id="a", applied_on=TODAY)

    assert snapshot(tickers, history) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
        ["tickers.json", "ticker_history.csv", "universe_proposal.json"]
    )
