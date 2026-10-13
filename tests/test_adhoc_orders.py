"""Behavior contract for scripts/adhoc_orders.py (written first, before the implementation).

The tool turns an operator's list of (ticker, usd) pairs into a validated order sheet, and
can submit that sheet through an injected callable. Contract, derived from CLAUDE.md and
docs/run_classification.md rather than from any implementation:

* Validation is all-or-nothing: any problem yields an empty plan plus structured errors,
  never a partial buy list.
* Universe comes from config/tickers.json; excluded symbols (QBUT) are always refused.
* Per-order, daily and weekly caps, the account-wide daily attempt cap (120) and available buying
  power (docs/safeguards.md) are enforced against figures the caller supplies. Every figure is
  validated: NaN, infinity, bool, strings and negatives fail closed instead of disabling a ceiling.
* The CLI never places orders; placement happens only via place_adhoc() with an injected
  callable AND an explicit confirm=True.
* A broker response only counts as submitted if it positively confirms an order; anything else
  halts the batch. Broker exception text is never stored (only its type).
* Ad hoc output is a manual run: it lands under manual_runs/, never writes a pointer, never
  overwrites an earlier run, and a separate execution-state artifact records what was submitted.
"""

import json
import math

import pytest

from scripts import adhoc_orders as ao

UNIVERSE = frozenset({"ANET", "NVDA", "AMD"})
EXCLUDED = frozenset({"QBUT"})
DAY = "2026-10-13"


def plan(orders, **overrides):
    kwargs = dict(
        trading_day=DAY,
        universe=UNIVERSE,
        excluded=EXCLUDED,
        spent_today_usd=0.0,
        spent_week_usd=0.0,
        daily_cap_usd=180.0,
        weekly_cap_usd=900.0,
        max_per_order_usd=16.0,
        attempts_today=0,
        max_orders_per_day=120,
        buying_power_usd=1000.0,
    )
    kwargs.update(overrides)
    return ao.build_plan(orders, **kwargs)


def codes(result):
    return {e["code"] for e in result.errors}


# --- validation: happy path -------------------------------------------------------------


def test_valid_orders_produce_ok_plan_in_input_order():
    result = plan([("ANET", 2.0), ("NVDA", 4.0)])
    assert result.ok is True
    assert result.errors == []
    assert [(o["ticker"], o["usd"]) for o in result.orders] == [("ANET", 2.0), ("NVDA", 4.0)]


def test_tickers_are_normalised_to_upper_case_and_trimmed():
    result = plan([(" anet ", 2.0)])
    assert result.ok is True
    assert result.orders[0]["ticker"] == "ANET"


def test_order_exactly_at_each_cap_is_accepted():
    assert plan([("ANET", 16.0)]).ok is True
    assert plan([("ANET", 10.0)], spent_today_usd=170.0).ok is True  # lands on 180.00
    assert plan([("ANET", 10.0)], spent_week_usd=890.0).ok is True  # lands on 900.00


# --- validation: refusals, each all-or-nothing -------------------------------------------


@pytest.mark.parametrize(
    "orders, code",
    [
        ([("ZZZZ", 2.0)], "unknown_ticker"),
        ([("QBUT", 2.0)], "excluded_ticker"),
        ([("ANET", 16.01)], "over_per_order_cap"),
        ([("ANET", 0.0)], "invalid_amount"),
        ([("ANET", -1.0)], "invalid_amount"),
        ([("ANET", math.nan)], "invalid_amount"),
        ([("ANET", math.inf)], "invalid_amount"),
        ([("ANET", True)], "invalid_amount"),  # bool is an int subclass; must not pass as 1
        ([("ANET", "2.0")], "invalid_amount"),  # no silent string coercion
        ([("ANET", 2.0), ("anet", 3.0)], "duplicate_ticker"),
        ([("", 2.0)], "unknown_ticker"),
        ([], "empty_order_list"),
    ],
)
def test_invalid_input_returns_empty_plan_with_code(orders, code):
    result = plan(orders)
    assert result.ok is False
    assert result.orders == []
    assert code in codes(result)


def test_one_bad_row_blocks_the_good_rows():
    result = plan([("ANET", 2.0), ("QBUT", 2.0), ("NVDA", 2.0)])
    assert result.ok is False
    assert result.orders == []  # no partial buy list


def test_excluded_wins_even_if_ticker_is_also_in_universe():
    result = plan([("QBUT", 2.0)], universe=UNIVERSE | {"QBUT"})
    assert "excluded_ticker" in codes(result)


def test_daily_cap_breach_counts_prior_spend():
    result = plan([("ANET", 10.0)], spent_today_usd=170.01)
    assert result.ok is False
    assert "over_daily_cap" in codes(result)


def test_weekly_cap_breach_counts_prior_spend():
    result = plan([("ANET", 10.0)], spent_week_usd=890.01)
    assert result.ok is False
    assert "over_weekly_cap" in codes(result)


def test_batch_total_is_checked_not_just_each_order():
    orders = [(t, 16.0) for t in ("ANET", "NVDA", "AMD")]
    result = plan(orders, daily_cap_usd=40.0)
    assert result.ok is False
    assert "over_daily_cap" in codes(result)


def test_cap_check_is_exact_in_cents_not_float_drift():
    # 0.1 + 0.2 style drift must not flip a cap that is met exactly.
    result = plan([("ANET", 0.1), ("NVDA", 0.2)], daily_cap_usd=0.3)
    assert result.ok is True


def test_negative_prior_spend_is_rejected():
    result = plan([("ANET", 2.0)], spent_today_usd=-1.0)
    assert result.ok is False
    assert "invalid_spend" in codes(result)


def test_all_errors_are_reported_together():
    result = plan([("QBUT", 2.0), ("ZZZZ", 99.0)])
    assert {"excluded_ticker", "unknown_ticker", "over_per_order_cap"} <= codes(result)


BAD_NUMBERS = [math.nan, math.inf, -math.inf, True, "5", None, -1.0, 10**400]


@pytest.mark.parametrize("field", ["spent_today_usd", "spent_week_usd", "attempts_today", "buying_power_usd"])
@pytest.mark.parametrize("bad", BAD_NUMBERS, ids=repr)
def test_bad_supplied_figures_fail_closed(field, bad):
    result = plan([("ANET", 2.0)], **{field: bad})
    assert result.ok is False
    assert result.orders == []


@pytest.mark.parametrize("field", ["daily_cap_usd", "weekly_cap_usd", "max_per_order_usd", "max_orders_per_day"])
@pytest.mark.parametrize("bad", [math.nan, math.inf, True, "5", None, -1.0, 0, 10**400], ids=repr)
def test_bad_caps_fail_closed_instead_of_disabling_the_ceiling(field, bad):
    # NaN compares False to everything, so an unvalidated NaN cap would silently allow any spend.
    result = plan([("ANET", 2.0)], **{field: bad})
    assert result.ok is False
    assert result.orders == []


def test_attempt_cap_counts_prior_attempts_and_this_batch():
    three = [("ANET", 1.0), ("NVDA", 1.0), ("AMD", 1.0)]
    assert plan(three, attempts_today=117).ok is True  # lands on exactly 120
    over = plan(three, attempts_today=118)
    assert over.ok is False
    assert "over_attempt_cap" in codes(over)


def test_buying_power_is_a_hard_ceiling():
    orders = [("ANET", 3.0), ("NVDA", 3.0)]
    assert plan(orders, buying_power_usd=6.0).ok is True
    short = plan(orders, buying_power_usd=5.99)
    assert short.ok is False
    assert "over_buying_power" in codes(short)


# --- config-backed defaults -------------------------------------------------------------


def test_default_policy_matches_repo_config():
    policy = ao.load_policy()
    import dca_runner

    tickers = json.load(open(ao.TICKERS_CONFIG_PATH))
    assert policy.universe == frozenset(tickers["active"])
    assert "QBUT" in policy.excluded
    assert policy.max_per_order_usd == dca_runner.MAX_PER_ORDER_USD
    budgets = json.load(open(ao.BUDGETS_CONFIG_PATH))
    assert policy.max_orders_per_day == budgets["circuit_breakers"]["max_orders_per_day"]


# --- input parsing ----------------------------------------------------------------------


def test_parse_csv_with_header(tmp_path):
    f = tmp_path / "o.csv"
    f.write_text("ticker,usd\nANET,2.50\nNVDA,4\n")
    assert ao.parse_orders(f) == [("ANET", 2.5), ("NVDA", 4.0)]


def test_parse_json_list_of_objects(tmp_path):
    f = tmp_path / "o.json"
    f.write_text(json.dumps([{"ticker": "ANET", "usd": 2.5}]))
    assert ao.parse_orders(f) == [("ANET", 2.5)]


@pytest.mark.parametrize(
    "text",
    ["ticker,usd\nANET,abc\n", "ticker,usd\nANET\n", "ticker,usd\nANET,1,2\n", "ticker,usd\nANET,NaN\n"],
)
def test_malformed_csv_fails_closed(tmp_path, text):
    f = tmp_path / "o.csv"
    f.write_text(text)
    with pytest.raises(ao.AdhocInputError):
        ao.parse_orders(f)


def test_json_that_is_not_a_list_fails_closed(tmp_path):
    f = tmp_path / "o.json"
    f.write_text(json.dumps({"ticker": "ANET", "usd": 2}))
    with pytest.raises(ao.AdhocInputError):
        ao.parse_orders(f)


def test_duplicate_json_keys_fail_closed(tmp_path):
    f = tmp_path / "o.json"
    f.write_text('[{"ticker": "ANET", "usd": 2, "usd": 3}]')
    with pytest.raises(ao.AdhocInputError):
        ao.parse_orders(f)


# --- placement --------------------------------------------------------------------------


class Recorder:
    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["ticker"] == self.fail_on:
            raise RuntimeError("broker rejected")
        return {"id": f"order-{kwargs['ticker']}", "state": "queued"}


def test_place_is_dry_run_unless_confirmed():
    rec = Recorder()
    result = ao.place_adhoc(plan([("ANET", 2.0)]), rec)
    assert rec.calls == []
    assert result.submitted == []
    assert result.dry_run is True


def test_confirmed_placement_calls_once_per_order_with_idempotency_key():
    rec = Recorder()
    result = ao.place_adhoc(plan([("ANET", 2.0), ("NVDA", 4.0)]), rec, confirm=True)
    assert [c["ticker"] for c in rec.calls] == ["ANET", "NVDA"]
    assert [c["usd"] for c in rec.calls] == [2.0, 4.0]
    assert rec.calls[0]["idempotency_key"] == f"dca-adhoc-{DAY}-ANET"
    assert [s["ticker"] for s in result.submitted] == ["ANET", "NVDA"]
    assert result.failed is None


def test_refuses_to_place_an_invalid_plan_even_when_confirmed():
    rec = Recorder()
    bad = plan([("QBUT", 2.0)])
    with pytest.raises(ao.AdhocInputError):
        ao.place_adhoc(bad, rec, confirm=True)
    assert rec.calls == []


def test_stops_at_first_failure_and_never_retries():
    rec = Recorder(fail_on="NVDA")
    result = ao.place_adhoc(plan([("ANET", 2.0), ("NVDA", 4.0), ("AMD", 3.0)]), rec, confirm=True)
    assert [c["ticker"] for c in rec.calls] == ["ANET", "NVDA"]  # one try on NVDA, AMD untouched
    assert [s["ticker"] for s in result.submitted] == ["ANET"]
    assert result.failed["ticker"] == "NVDA"
    assert result.failed["error"] == "RuntimeError"  # type only; the message may echo account data
    assert "broker rejected" not in json.dumps(result.failed)
    assert [o["ticker"] for o in result.not_attempted] == ["AMD"]


@pytest.mark.parametrize(
    "response",
    [
        None,
        {},
        "ok",
        {"id": ""},
        {"id": "o-1", "state": "rejected"},
        {"id": "o-1", "state": "Cancelled"},
        {"id": "o-1", "state": "failed"},
        {"id": "o-1", "success": False},
        {"id": "o-1", "symbol": "SOMETHING_ELSE"},
        {"id": "o-1", "ref_id": "not-our-key"},
    ],
    ids=repr,
)
def test_unconfirmed_broker_response_halts_the_batch_and_is_not_recorded_as_submitted(response):
    calls = []

    def place(**kwargs):
        calls.append(kwargs["ticker"])
        return response

    result = ao.place_adhoc(plan([("ANET", 2.0), ("NVDA", 4.0)]), place, confirm=True)
    assert result.submitted == []
    assert result.failed["ticker"] == "ANET"
    assert result.failed["error"] == "unconfirmed_response"
    assert calls == ["ANET"]  # NVDA untouched
    assert [o["ticker"] for o in result.not_attempted] == ["NVDA"]


def test_matching_symbol_and_ref_id_in_the_response_are_accepted():
    def place(**kwargs):
        return {"id": "o-1", "symbol": kwargs["ticker"], "ref_id": kwargs["idempotency_key"], "state": "queued"}

    result = ao.place_adhoc(plan([("ANET", 2.0)]), place, confirm=True)
    assert [s["ticker"] for s in result.submitted] == ["ANET"]


# --- artifacts and run classification ---------------------------------------------------


def test_write_plan_lands_under_manual_runs_and_writes_no_pointer(tmp_path):
    path = ao.write_plan(plan([("ANET", 2.0)]), root=tmp_path, timestamp="20261013T101500")
    assert path.parent == tmp_path / "manual_runs" / "20261013T101500"
    doc = json.loads(path.read_text())
    assert doc["run_origin"] == "manual"
    assert doc["manual_run"] is True
    assert doc["orders"][0]["ticker"] == "ANET"
    pointers = [p for p in tmp_path.rglob("*") if p.name.startswith("latest_")]
    assert pointers == []


def test_plan_artifact_never_contains_an_account_number(tmp_path):
    path = ao.write_plan(plan([("ANET", 2.0)]), root=tmp_path, timestamp="20261013T101500")
    assert "999000111" not in path.read_text()


def test_write_plan_refuses_an_invalid_plan(tmp_path):
    with pytest.raises(ao.AdhocInputError):
        ao.write_plan(plan([("QBUT", 2.0)]), root=tmp_path, timestamp="t")
    assert list(tmp_path.rglob("*.json")) == []


def test_second_write_at_the_same_timestamp_fails_without_touching_the_first(tmp_path):
    first = ao.write_plan(plan([("ANET", 2.0)]), root=tmp_path, timestamp="t1")
    before = first.read_text()
    with pytest.raises(ao.AdhocInputError):
        ao.write_plan(plan([("NVDA", 9.0)]), root=tmp_path, timestamp="t1")
    assert first.read_text() == before


@pytest.mark.parametrize("stamp", ["../escape", "a/b", "", "a b"])
def test_timestamp_cannot_escape_the_manual_runs_directory(tmp_path, stamp):
    with pytest.raises(ao.AdhocInputError):
        ao.write_plan(plan([("ANET", 2.0)]), root=tmp_path, timestamp=stamp)


def test_execution_state_records_order_ids_and_distinguishes_outcomes(tmp_path):
    p = plan([("ANET", 2.0), ("NVDA", 4.0), ("AMD", 3.0)])
    ao.write_plan(p, root=tmp_path, timestamp="t1")
    result = ao.place_adhoc(p, Recorder(fail_on="NVDA"), confirm=True)
    path = ao.write_execution_state(p, result, root=tmp_path, timestamp="t1")
    doc = json.loads(path.read_text())
    assert path.name != ao.PLAN_FILENAME  # the pre-placement plan is never rewritten
    assert doc["run_origin"] == "manual" and doc["manual_run"] is True
    assert doc["status"] == "halted"
    assert doc["submitted"] == [{"ticker": "ANET", "usd": 2.0, "order_id": "order-ANET"}]
    assert doc["failed"]["ticker"] == "NVDA" and "broker rejected" not in json.dumps(doc)
    assert [o["ticker"] for o in doc["not_attempted"]] == ["AMD"]


def test_execution_state_for_a_dry_run_says_dry_run(tmp_path):
    p = plan([("ANET", 2.0)])
    ao.write_plan(p, root=tmp_path, timestamp="t1")
    path = ao.write_execution_state(p, ao.place_adhoc(p, Recorder()), root=tmp_path, timestamp="t1")
    doc = json.loads(path.read_text())
    assert doc["status"] == "dry_run" and doc["submitted"] == []


def test_execution_state_needs_an_existing_plan_and_never_overwrites(tmp_path):
    p = plan([("ANET", 2.0)])
    result = ao.place_adhoc(p, Recorder(), confirm=True)
    with pytest.raises(ao.AdhocInputError):
        ao.write_execution_state(p, result, root=tmp_path, timestamp="missing")
    ao.write_plan(p, root=tmp_path, timestamp="t1")
    ao.write_execution_state(p, result, root=tmp_path, timestamp="t1")
    with pytest.raises(ao.AdhocInputError):
        ao.write_execution_state(p, result, root=tmp_path, timestamp="t1")


# --- CLI --------------------------------------------------------------------------------


FIGURES = ["--spent-today", "0", "--spent-week", "0", "--attempts-today", "0", "--buying-power", "1000"]


def _cli(tmp_path, text, *extra, figures=FIGURES):
    f = tmp_path / "o.csv"
    f.write_text(text)
    argv = ["--orders", str(f), "--trading-day", DAY, "--out-root", str(tmp_path), *figures, *extra]
    return ao.main(argv, policy=_policy())


def test_cli_valid_list_exits_0_and_prints_sheet(tmp_path, capsys):
    rc = _cli(tmp_path, "ticker,usd\nANET,2\n")
    assert rc == 0
    assert "ANET" in capsys.readouterr().out


def test_cli_invalid_list_exits_2_and_writes_nothing(tmp_path, capsys):
    rc = _cli(tmp_path, "ticker,usd\nQBUT,2\n")
    assert rc == 2
    assert "excluded_ticker" in capsys.readouterr().out
    assert list(tmp_path.rglob("manual_runs")) == []


@pytest.mark.parametrize("missing", ["--spent-today", "--spent-week", "--attempts-today", "--buying-power"])
def test_cli_refuses_to_run_without_each_current_figure(tmp_path, missing):
    # A silent default of 0 would let a sheet look valid right after a heavy spending day.
    figures = list(FIGURES)
    at = figures.index(missing)
    del figures[at : at + 2]
    with pytest.raises(SystemExit):
        _cli(tmp_path, "ticker,usd\nANET,2\n", figures=figures)
    assert list(tmp_path.rglob("manual_runs")) == []


@pytest.mark.parametrize(
    "figures, code",
    [
        (["--spent-today", "179", "--spent-week", "0", "--attempts-today", "0", "--buying-power", "1000"], "over_daily_cap"),
        (["--spent-today", "0", "--spent-week", "899", "--attempts-today", "0", "--buying-power", "1000"], "over_weekly_cap"),
        (["--spent-today", "0", "--spent-week", "0", "--attempts-today", "120", "--buying-power", "1000"], "over_attempt_cap"),
        (["--spent-today", "0", "--spent-week", "0", "--attempts-today", "0", "--buying-power", "1"], "over_buying_power"),
    ],
)
def test_cli_prior_figures_can_breach_a_limit(tmp_path, capsys, figures, code):
    rc = _cli(tmp_path, "ticker,usd\nANET,2\n", figures=figures)
    assert rc == 2
    assert code in capsys.readouterr().out
    assert list(tmp_path.rglob("manual_runs")) == []


def test_cli_rejects_non_finite_figures(tmp_path, capsys):
    figures = ["--spent-today", "nan", "--spent-week", "0", "--attempts-today", "0", "--buying-power", "1000"]
    assert _cli(tmp_path, "ticker,usd\nANET,2\n", figures=figures) == 2


def test_cli_reusing_a_timestamp_exits_2_and_keeps_the_first_plan(tmp_path, capsys):
    assert _cli(tmp_path, "ticker,usd\nANET,2\n", "--timestamp", "t1") == 0
    first = (tmp_path / "manual_runs" / "t1" / ao.PLAN_FILENAME).read_text()
    assert _cli(tmp_path, "ticker,usd\nNVDA,3\n", "--timestamp", "t1") == 2
    assert (tmp_path / "manual_runs" / "t1" / ao.PLAN_FILENAME).read_text() == first


def test_cli_has_no_way_to_place_orders():
    with pytest.raises(SystemExit):
        ao.main(["--orders", "x.csv", "--trading-day", DAY, *FIGURES, "--confirm"], policy=_policy())


def _policy():
    return ao.Policy(
        universe=UNIVERSE,
        excluded=EXCLUDED,
        daily_cap_usd=180.0,
        weekly_cap_usd=900.0,
        max_per_order_usd=16.0,
        max_orders_per_day=120,
    )
