"""Trial registry: every configuration ever evaluated is recorded with its metrics.

The count matters more than any single row: a deflated-Sharpe promotion bar needs the
honest number of trials, so nothing may hide one. ``promotion_threshold`` is a FIELD only
(proposed default 0.95, the DSR bar from the universe research spec); no promotion logic
exists yet.

Fixture (synthetic): experiment "ladder" tries configs A and B; experiment "pacing" tries
config C; then config A is re-evaluated on a longer window as a new trial id.
"""

from __future__ import annotations

import pytest

from .conftest import FIRST_SEEN, LATER, utc

KNOW_ALL = utc(2026, 6, 1)


def trial(trial_id, experiment, config, sharpe, *, n=250):
    return {
        "trial_id": trial_id,
        "experiment": experiment,
        "config": config,
        "metrics": {"sharpe": sharpe, "money_weighted_return": sharpe / 10},
        "n_observations": n,
        "evaluated_at": utc(2026, 4, 1),
    }


CONFIG_A = {"tiers": [1, 2, 3], "cap": 50}
CONFIG_B = {"tiers": [1, 1.5, 2], "cap": 50}
CONFIG_C = {"pace": "even"}
TRIALS = [
    trial("t1", "ladder", CONFIG_A, 0.8),
    trial("t2", "ladder", CONFIG_B, 1.1),
    trial("t3", "pacing", CONFIG_C, 0.4),
]


def test_every_trial_is_recorded_with_its_metrics(ledger):
    assert ledger.ingest_trials(TRIALS, source="backtest", run_id="bt1", as_of=FIRST_SEEN) == 3
    rows = {r["trial_id"]: r for r in ledger.trials(known_at=KNOW_ALL)}
    assert rows["t2"]["metrics"] == {"sharpe": 1.1, "money_weighted_return": pytest.approx(0.11)}
    assert rows["t2"]["config"] == CONFIG_B
    assert rows["t2"]["sharpe"] == pytest.approx(1.1)
    assert rows["t2"]["n_observations"] == 250


def test_config_hash_is_canonical(ledger):
    # Key order must not change the hash: {"cap", "tiers"} and {"tiers", "cap"} are one config.
    reordered = {"cap": 50, "tiers": [1, 2, 3]}
    ledger.ingest_trials([trial("t1", "ladder", CONFIG_A, 0.8), trial("t9", "ladder", reordered, 0.8)],
                         source="backtest", run_id="bt1", as_of=FIRST_SEEN)
    hashes = {r["trial_id"]: r["config_hash"] for r in ledger.trials(known_at=KNOW_ALL)}
    assert hashes["t1"] == hashes["t9"]


def test_trial_count_query(ledger):
    ledger.ingest_trials(TRIALS, source="backtest", run_id="bt1", as_of=FIRST_SEEN)
    assert ledger.ingest_trials(TRIALS, source="backtest", run_id="bt2", as_of=LATER) == 0  # identical: no-op
    assert ledger.trial_count() == 3
    # A re-evaluation of config A is a NEW trial and must be counted (4 trials), even
    # though only 3 distinct configurations exist.
    ledger.ingest_trials([trial("t4", "ladder", CONFIG_A, 0.9, n=500)], source="backtest", run_id="bt3", as_of=LATER)
    assert ledger.trial_count() == 4
    assert ledger.trial_count(experiment="ladder") == 3
    assert ledger.trial_count(experiment="pacing") == 1
    counts = {r["experiment"]: r for r in ledger.trial_counts()}
    # ladder: t1, t2, t4 -> 3 trials over 2 distinct configs (A, B).
    assert counts["ladder"]["n_trials"] == 3
    assert counts["ladder"]["n_configs"] == 2
    assert counts["pacing"]["n_trials"] == 1


def test_promotion_threshold_is_a_recorded_field_only(ledger):
    from market_data_ingest.ledger import defaults

    assert defaults.PROMOTION_DSR_THRESHOLD == 0.95
    ledger.ingest_trials(TRIALS[:1], source="backtest", run_id="bt1", as_of=FIRST_SEEN)
    (row,) = ledger.trials(known_at=KNOW_ALL)
    assert row["promotion_threshold"] == pytest.approx(0.95)
    assert not [n for n in dir(ledger) if "promot" in n.lower()], "no promotion logic in Phase 0"


def test_metrics_must_be_finite_numbers(ledger):
    from market_data_ingest.ledger.errors import LedgerValidationError

    bad = trial("t1", "ladder", CONFIG_A, float("nan"))
    with pytest.raises(LedgerValidationError):
        ledger.ingest_trials([bad], source="backtest", run_id="bt1", as_of=FIRST_SEEN)
