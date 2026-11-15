"""``--account`` defaults to ``$ROBINHOOD_AGENTIC_ACCOUNT_NUMBER`` in every gate script (scripts/account_arg.py).

2026-11-14: the live prompts passed the account number on the command line to seven gate scripts, so it sat in
the session transcript and the process list. Each script now reads the environment when ``--account`` is
omitted, still honours an explicit one (the prompts already pasted keep passing it until re-pasted), and exits 2
rather than run on an empty account when neither is usable.
"""

from __future__ import annotations

import argparse
import json

import pytest

from scripts import (
    account_arg,
    assemble_order_pages,
    find_order_baseline,
    gate_failure_diagnostics,
    merge_order_pages,
    primary_gates,
    reshape_order_pages,
    same_day_top_up_gate,
)

ACCOUNT = "999000111"  # synthetic; conftest assigns the same value
OTHER = "12345"  # synthetic; 5 digits keeps it under the repo-wide account-shape scan
DAY = "2026-09-09"

# Every CLI this change covers, with its other required arguments and no --account. A new gate script that takes
# the account belongs here too, so its wiring cannot silently regress to ``required=True``.
_CLIS = {
    "assemble_order_pages": (assemble_order_pages.main, ["--trading-day", DAY, "--out", "x.json", "p1.json"]),
    "find_order_baseline": (find_order_baseline.main, ["--kind", "week", "--slot", "1030", "--trading-day", DAY]),
    "merge_order_pages": (
        merge_order_pages.main,
        [
            "--base", "b.json", "--trading-day", DAY, "--created-at-gte", "2026-09-09T10:00:00-04:00",
            "--snapshot-at", "2026-09-09T10:05:00-04:00", "--out", "o.json", "p1.json",
        ],
    ),
    "reshape_order_pages": (reshape_order_pages.main, ["--envelope", "e.json", "--trading-day", DAY, "--out", "o.json"]),
    "same_day_top_up_gate": (
        same_day_top_up_gate.main,
        ["--trading-day", DAY, "--orders-pages", "o.json", "--candidates", "c.json"],
    ),
    "gate_failure_diagnostics orders-pages": (
        gate_failure_diagnostics.main,
        ["orders-pages", "--orders-pages-file", "o.json", "--trading-day", DAY],
    ),
    "primary_gates budget": (
        primary_gates.main,
        ["budget", "--orders-pages", "o.json", "--trading-day", DAY, "--buying-power", "100"],
    ),
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="t")
    account_arg.add_account_argument(parser, "Account.")
    return parser


def test_omitted_account_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv(account_arg.ACCOUNT_ENV, f"  {ACCOUNT}\n")
    parser = _parser()
    args = parser.parse_args([])
    assert account_arg.resolve_account(parser, args) == ACCOUNT
    assert args.account == ACCOUNT


def test_explicit_account_still_wins_over_the_environment(monkeypatch):
    """The prompts already pasted into the scheduled tasks keep passing --account until the operator re-pastes."""
    monkeypatch.setenv(account_arg.ACCOUNT_ENV, ACCOUNT)
    parser = _parser()
    args = parser.parse_args(["--account", OTHER])
    assert account_arg.resolve_account(parser, args) == OTHER


def test_explicit_blank_account_is_left_for_the_script_to_refuse(monkeypatch):
    """Each script refuses a blank account itself (``missing_account``); the helper must not mask that shape."""
    monkeypatch.setenv(account_arg.ACCOUNT_ENV, ACCOUNT)
    parser = _parser()
    args = parser.parse_args(["--account", "  "])
    assert account_arg.resolve_account(parser, args) == "  "


@pytest.mark.parametrize(
    ("value", "reason"),
    [(None, "unset or empty"), ("", "unset or empty"), ("   ", "unset or empty"),
     ("${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}", "unexpanded")],
)
def test_unusable_environment_fails_closed_with_exit_2(monkeypatch, capsys, value, reason):
    # dca_paths only loads .env without overriding, and the suite disables it (DCA_DISABLE_DOTENV), so the
    # variable stays exactly what the test sets.
    if value is None:
        monkeypatch.delenv(account_arg.ACCOUNT_ENV, raising=False)
    else:
        monkeypatch.setenv(account_arg.ACCOUNT_ENV, value)
    parser = _parser()
    args = parser.parse_args([])
    with pytest.raises(SystemExit) as exc:
        account_arg.resolve_account(parser, args)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "no usable account number" in err and reason in err


@pytest.mark.parametrize("name", sorted(_CLIS))
def test_every_gate_cli_accepts_an_omitted_account_and_refuses_an_unset_one(monkeypatch, capsys, tmp_path, name):
    """Wiring check: without --account the CLI no longer stops at argparse's ``required`` check, and with the
    variable unset it stops at the helper's own usage error, never running on an empty account."""
    main, argv = _CLIS[name]
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(account_arg.ACCOUNT_ENV, raising=False)
    with pytest.raises(SystemExit) as exc:
        main(list(argv))
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "no usable account number" in err
    assert "--account is required" not in err and "the following arguments are required" not in err


def test_assemble_without_account_binds_the_envelope_to_the_environment_account(monkeypatch, tmp_path):
    """End to end on one script: the written envelope carries the environment's number, exactly as an explicit
    ``--account`` with the same value would."""
    monkeypatch.setenv(account_arg.ACCOUNT_ENV, ACCOUNT)
    page = tmp_path / "p1.json"
    page.write_text(json.dumps({"orders": []}), encoding="utf-8")
    out = tmp_path / "out.json"
    code = assemble_order_pages.main(["--trading-day", DAY, "--out", str(out), str(page)])
    assert code == 0
    assert json.loads(out.read_text(encoding="utf-8"))["account_number"] == ACCOUNT


def test_the_account_number_is_never_echoed_in_the_usage_error(monkeypatch, capsys):
    monkeypatch.setenv(account_arg.ACCOUNT_ENV, "${" + ACCOUNT + "}")
    parser = _parser()
    with pytest.raises(SystemExit):
        account_arg.resolve_account(parser, parser.parse_args([]))
    assert ACCOUNT not in capsys.readouterr().err


# --------------------------------------------------------------------------------------------------------------
# 2026-11-15 (#2323 rebased onto this helper): the environment value must also have the repo's canonical account
# shape (dca_config.ACCOUNT_NUMBER_SHAPE, 6 to 12 digits). "Set and not a placeholder" let a malformed value such
# as a stray word or the morning modes' ``read_only`` literal through, to surface only later as a confusing
# ``orders_pages_account_mismatch``. An explicit --account stays untouched (tests above): each script validates it
# against its envelope, and the prompts already pasted keep passing it.
# --------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["12345", "9" * 13, "12 34", "not-an-account", "read_only"])
def test_malformed_environment_account_fails_closed_with_exit_2(monkeypatch, capsys, value):
    monkeypatch.setenv(account_arg.ACCOUNT_ENV, value)
    parser = _parser()
    with pytest.raises(SystemExit) as exc:
        account_arg.resolve_account(parser, parser.parse_args([]))
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "no usable account number" in err and "6 to 12 digits" in err
    assert value not in err  # never echo the value, malformed or not (AGENTS.md secrets policy)


@pytest.mark.parametrize("value", [str(10**5), "9" * 12])
def test_environment_account_at_either_shape_bound_is_accepted(monkeypatch, value):
    monkeypatch.setenv(account_arg.ACCOUNT_ENV, value)
    parser = _parser()
    assert account_arg.resolve_account(parser, parser.parse_args([])) == value


@pytest.mark.parametrize("name", sorted(_CLIS))
def test_every_gate_cli_refuses_a_malformed_environment_account(monkeypatch, capsys, tmp_path, name):
    """Wiring check: the shape rule reaches every CLI before any file is read."""
    main, argv = _CLIS[name]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(account_arg.ACCOUNT_ENV, "not-an-account")
    with pytest.raises(SystemExit) as exc:
        main(list(argv))
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "6 to 12 digits" in err and "not-an-account" not in err
