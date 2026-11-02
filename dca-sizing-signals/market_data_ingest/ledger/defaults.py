"""Proposed defaults for the outcome-ledger ADR's open questions.

PROPOSED, NOT OPERATOR-APPROVED. Each constant answers one open question in
docs/adr-outcome-ledger-pit-harness.md (PR #2096) with the default proposed when this
package was built. They are module constants on purpose: changing one is a one-line,
reviewable diff, and nothing here is read by the live DCA paths.
"""

from __future__ import annotations

# Status label carried in docs and the PR so nobody mistakes these for approved policy.
PROPOSED_DEFAULTS_STATUS = "proposed, not operator-approved (ADR PR #2096 open questions 2-5)"

# Open question 2 (location): extend dca-sizing-signals as market_data_ingest/ledger/, a
# sibling of prices/, sharing its DuckDB file and boundary test. Recorded for reference.
LEDGER_LOCATION = "dca-sizing-signals/market_data_ingest/ledger"

# Open question 3 (account alias): salted SHA-256 (HMAC-SHA256) of the raw account
# identifier. The salt is read at runtime from this environment variable and is never
# stored in the repo or the database; only the alias is stored.
ACCOUNT_SALT_ENV = "DCA_LEDGER_ACCOUNT_SALT"
ALIAS_SCHEME = "hmac_sha256_v1"
MIN_SALT_LENGTH = 16

# Open question 4 (baseline): a flat 1x buy of the slot's base amount at the same slot
# timestamp and the same price as the graded decision.
BASELINE_RULE = "flat_1x_same_slot_price"
BASELINE_MULTIPLIER = 1.0

# Open question 5 (horizons and promotion): outcomes at t+5 and t+20 trading sessions after
# the fill session. The promotion threshold is a recorded FIELD only -- a deflated Sharpe
# bar of 0.95, matching the universe research spec's DSR >= 0.95. No promotion logic exists.
OUTCOME_HORIZONS_SESSIONS: tuple[int, ...] = (5, 20)
PROMOTION_DSR_THRESHOLD = 0.95

# Not an open question: the repo's scheduled/manual vocabulary (docs/run_classification.md,
# ``run_origin``), stored here as ``run_kind``. Manual runs and manual order ids never enter
# scheduled metrics.
RUN_KINDS: tuple[str, ...] = ("scheduled", "manual")
