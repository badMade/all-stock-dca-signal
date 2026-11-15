"""``primary_gates.account_pin`` uses the repo's canonical account shape (6 to 12 digits), not "any run of digits".

2026-11-15: the CLI wiring tests that used to live here (the six order scripts reading the account from the
environment) were superseded by scripts/tests/test_account_arg.py when #2323 was rebased onto #2320's
scripts/account_arg.py; only the account_pin tests remain.
"""

from __future__ import annotations

import pytest

from scripts import primary_gates

ACCOUNT = "999000111"  # conftest.py's synthetic value


@pytest.mark.parametrize("bad", ["12345", "9" * 13, "read_only", "12 34", "not-an-account", "", None])
def test_account_pin_refuses_anything_outside_the_canonical_shape(bad):
    out = primary_gates.account_pin(bad)
    assert out["ok"] is False and "account_number" not in out


def test_account_pin_strips_whitespace_and_accepts_a_canonical_account():
    out = primary_gates.account_pin(f" {ACCOUNT}\n")
    assert out == {"ok": True, "account_number": ACCOUNT}
