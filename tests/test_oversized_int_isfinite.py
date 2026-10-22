"""math.isfinite() raises OverflowError on an int too large for a float (10**400); the console and executor paths
that call it on untrusted JSON values must fail closed instead of crashing (#1859's intended fix, restored
after that PR's stale squash merge also deleted unrelated work)."""

import dca_console_config
import morning_dca

HUGE = 10**400


def test_console_config_finite_rejects_an_oversized_int():
    assert dca_console_config._finite(HUGE) is None
    assert dca_console_config._finite(12) == 12.0


def test_a_broker_order_id_that_is_an_oversized_int_is_skipped_not_fatal():
    assert morning_dca._stable_broker_order_id({"id": HUGE}) is None
