"""The six order-history scripts take the account from the environment, not the command line (tests first).

Each of ``assemble_order_pages``, ``find_order_baseline``, ``merge_order_pages``, ``reshape_order_pages``,
``same_day_top_up_gate`` and ``gate_failure_diagnostics`` used to REQUIRE ``--account <number>``. The live prompts
filled it with ``${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}``, which the shell expands, so the real number was on the
command line (AGENTS.md secrets policy). They now resolve it with ``dca_config.resolve_order_account``: the
environment by default, ``--account`` still accepted so an existing caller keeps working.

These tests run each script as a subprocess with every OTHER required argument pointing at files that do not exist.
That is deliberate: the interesting behaviour is what happens BEFORE any file is read. The account resolution must
fail closed on its own, and must never print the number. ``primary_gates.account_pin`` is also brought onto the
repo's canonical 6-to-12-digit shape (it accepted any run of digits).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import primary_gates

REPO = Path(__file__).resolve().parents[2]
ACCOUNT = "999000111"  # conftest.py's synthetic value
VAR = "ROBINHOOD_AGENTIC_ACCOUNT_NUMBER"
DAY = "2026-11-16"


def _argv(name: str, tmp: Path) -> list[str]:
    """Every required argument EXCEPT --account, pointing at files that do not exist."""
    missing = lambda n: str(tmp / n)  # noqa: E731 - a tiny local helper
    return {
        "assemble_order_pages": ["--trading-day", DAY, "--out", missing("o.json"), missing("p1.json")],
        "find_order_baseline": ["--kind", "day", "--slot", "1130", "--trading-day", DAY],
        "merge_order_pages": [
            "--base", missing("b.json"), "--trading-day", DAY, "--created-at-gte", f"{DAY}T09:00:00-05:00",
            "--snapshot-at", f"{DAY}T09:05:00-05:00", "--out", missing("o.json"), missing("p.json"),
        ],
        "reshape_order_pages": ["--envelope", missing("e.json"), "--trading-day", DAY, "--out", missing("o.json")],
        "same_day_top_up_gate": ["--trading-day", DAY, "--orders-pages", missing("o.json"), "--candidates", missing("c.json")],
        "gate_failure_diagnostics": ["orders-pages", "--orders-pages-file", missing("o.json"), "--trading-day", DAY],
    }[name]


SCRIPTS = (
    "assemble_order_pages",
    "find_order_baseline",
    "merge_order_pages",
    "reshape_order_pages",
    "same_day_top_up_gate",
    "gate_failure_diagnostics",
)


def _run(name: str, tmp: Path, *extra: str, env_account: str | None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != VAR}
    env["DCA_DISABLE_DOTENV"] = "1"  # hermetic: never read the operator's .env
    if env_account is not None:
        env[VAR] = env_account
    return subprocess.run(
        [sys.executable, "-m", f"scripts.{name}", *_argv(name, tmp), *extra],
        cwd=REPO, capture_output=True, text=True, timeout=60, env=env,
    )


def _out(done: subprocess.CompletedProcess) -> str:
    return done.stdout + done.stderr


@pytest.mark.parametrize("name", SCRIPTS)
def test_account_is_no_longer_a_required_argument(tmp_path, name):
    done = _run(name, tmp_path, env_account=ACCOUNT)
    assert "arguments are required" not in _out(done)


@pytest.mark.parametrize("name", SCRIPTS)
def test_the_account_flag_still_works_for_an_existing_caller(tmp_path, name):
    done = _run(name, tmp_path, "--account", ACCOUNT, env_account=None)
    text = _out(done)
    assert "arguments are required" not in text
    assert VAR not in text  # it got past account resolution; any failure is about the missing files


@pytest.mark.parametrize("name", SCRIPTS)
def test_no_flag_and_no_environment_account_fails_closed_naming_the_variable(tmp_path, name):
    done = _run(name, tmp_path, env_account=None)
    text = _out(done)
    assert done.returncode != 0
    assert VAR in text
    assert "Traceback" not in text


@pytest.mark.parametrize("name", SCRIPTS)
def test_a_malformed_environment_account_fails_closed_without_echoing_it(tmp_path, name):
    bad = "not-an-account"
    done = _run(name, tmp_path, env_account=bad)
    text = _out(done)
    assert done.returncode != 0
    assert bad not in text
    assert "Traceback" not in text


@pytest.mark.parametrize("name", SCRIPTS)
@pytest.mark.parametrize("via", ["environment", "flag"])
def test_the_account_number_is_never_printed(tmp_path, name, via):
    if via == "environment":
        done = _run(name, tmp_path, env_account=ACCOUNT)
    else:
        done = _run(name, tmp_path, "--account", ACCOUNT, env_account=None)
    assert ACCOUNT not in _out(done)


# --------------------------------------------------------------------------------------------------------------
# primary_gates.account_pin: the repo's canonical shape (dca_config.ACCOUNT_NUMBER_SHAPE, 6 to 12 digits)
# --------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["12345", "9" * 13, "read_only", "12 34", "not-an-account", "", None])
def test_account_pin_refuses_anything_outside_the_canonical_shape(bad):
    out = primary_gates.account_pin(bad)
    assert out["ok"] is False and "account_number" not in out


def test_account_pin_strips_whitespace_and_accepts_a_canonical_account():
    out = primary_gates.account_pin(f" {ACCOUNT}\n")
    assert out == {"ok": True, "account_number": ACCOUNT}
