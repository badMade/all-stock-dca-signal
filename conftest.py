"""Repo-wide pytest configuration.

``near_close_dca.py`` reads ``ROBINHOOD_AGENTIC_ACCOUNT_NUMBER`` from the
environment at import time (falling back to a clearly-invalid placeholder
when unset) instead of hardcoding the real account number in source, per the
security fix in this branch. Tests that assert on the account number
(e.g. ``test_morning_dca.py::test_run_state_path_and_classification``) need a
deterministic value to compare against, so set it here before any test module
gets collected/imported -- this keeps the fixture value out of production
source while still giving the test suite a stable, known account number to
assert on.
"""

import os

os.environ.setdefault("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", "562057042")
