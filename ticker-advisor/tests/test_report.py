"""Tests for report.py -- pure stdlib, no third-party deps needed."""

import json

from report import BROKER_VERIFICATION_REMINDER, render_markdown, render_proposed_diff
from scoring import TickerScore


def _score(ticker, composite, classification, rationale=None):
    return TickerScore(
        ticker=ticker,
        raw={"health": None, "backtest": None, "sentiment": None},
        sub_scores={"health": composite, "backtest": composite, "sentiment": composite},
        composite=composite,
        classification=classification,
        rationale=rationale or [],
    )


def test_render_markdown_contains_ranked_table_and_reminder():
    scores = {
        "ADD_ME": _score("ADD_ME", 1.2, "ADD"),
        "SKIP_ME": _score("SKIP_ME", -1.0, "REJECT"),
    }

    markdown = render_markdown(scores)

    assert "ADD_ME" in markdown
    assert "SKIP_ME" in markdown
    assert markdown.index("ADD_ME") < markdown.index("SKIP_ME")  # higher composite ranks first
    assert BROKER_VERIFICATION_REMINDER in markdown


def test_render_markdown_includes_run_metadata_when_given():
    scores = {"ADDME": _score("ADDME", 1.0, "ADD")}
    run_metadata = {"effective_weights": {"health": 2.0}, "benchmark": "SPY", "lookback_years": 3}

    markdown = render_markdown(scores, run_metadata=run_metadata)

    assert "Run parameters" in markdown
    assert "SPY" in markdown
    assert "effective_weights" in markdown


def test_render_markdown_omits_run_parameters_section_when_not_given():
    scores = {"ADDME": _score("ADDME", 1.0, "ADD")}

    markdown = render_markdown(scores)

    assert "Run parameters" not in markdown


def test_render_markdown_handles_no_composite():
    scores = {"UNSCORED": _score("UNSCORED", None, "WATCH", rationale=["no usable signals"])}

    markdown = render_markdown(scores)

    assert "n/a" in markdown
    assert "UNSCORED" in markdown


def test_render_markdown_escapes_pipes_and_newlines_in_rationale():
    # A persisted FinBERT/yfinance/NewsAPI/SEC error can contain a newline
    # or "|" character -- realistic for multiline model/download exception
    # text -- and inserting it unescaped would split this candidate's row
    # across extra Markdown rows/columns, making scores.md structurally
    # misleading (Codex review on PR #403: "Escape rationale text before
    # inserting it into the Markdown table").
    scores = {
        "BADROW": _score(
            "BADROW",
            None,
            "WATCH",
            rationale=["health signal failed: line one\nline two | fake column"],
        ),
    }

    markdown = render_markdown(scores)
    table_lines = [line for line in markdown.splitlines() if line.startswith("| ") and "BADROW" in line]

    assert len(table_lines) == 1  # the whole rationale stayed on one table row
    assert "\\|" in table_lines[0]  # the raw "|" was escaped, not left as a live column delimiter
    assert "\n" not in table_lines[0]


def test_render_markdown_escapes_newlines_in_run_metadata():
    # A run_metadata value can carry a real multiline FinBERT initialization
    # error (sentiment_unavailable_reason) or a path from --history-dir --
    # inserting either unescaped into this list-item line splits it across
    # extra Markdown lines and can inject a heading or other report
    # structure into scores.md (Codex review on PR #429: "Escape run
    # metadata before rendering Markdown").
    scores = {"ADDME": _score("ADDME", 1.0, "ADD")}
    run_metadata = {"sentiment_unavailable_reason": "init failed: line one\n## Injected heading\nline two"}

    markdown = render_markdown(scores, run_metadata=run_metadata)
    metadata_lines = [line for line in markdown.splitlines() if "sentiment_unavailable_reason" in line]

    assert len(metadata_lines) == 1  # the whole value stayed on one bullet line
    assert "## Injected heading" not in markdown.splitlines()  # never landed on its own line
    assert "\n" not in metadata_lines[0]


def test_render_proposed_diff_only_includes_add_classified():
    scores = {
        "ADDME": _score("ADDME", 1.0, "ADD"),
        "WATCHX": _score("WATCHX", 0.0, "WATCH"),
        "SKIPX": _score("SKIPX", -1.0, "REJECT"),
    }

    diff = render_proposed_diff(scores)

    assert diff["recommended_additions"] == ["ADDME"]
    assert "ADDME" in diff["config_tickers_json_edit"]["snippet"]
    assert "WATCHX" not in diff["config_tickers_json_edit"]["snippet"]
    assert "ADDME" in diff["near_close_dca_py_edit"]["snippet"]
    assert diff["broker_verification_reminder"] == BROKER_VERIFICATION_REMINDER
    assert diff["config_tickers_json_edit"]["file"] == "config/tickers.json"
    assert diff["near_close_dca_py_edit"]["file"] == "near_close_dca.py"


def test_render_proposed_diff_includes_scheduled_tasks_json_edit():
    # config/scheduled_tasks.json's canonical_first_buy_and_topup_rule.
    # active_universe.active list mirrors config/tickers.json's `active`
    # array (its own "source_of_truth" note says so) but nothing keeps
    # them in sync automatically -- this exact field already drifted once
    # in this repo (config/scheduled_tasks.json's own
    # "ticker_universe_update_2026_08_04" note records a prior addition
    # that updated config/tickers.json and near_close_dca.py but left
    # this field listing the OLD, smaller universe). Describing an
    # addition as a complete two-file update reproduces that exact
    # omission for whoever applies the diff (Antigravity review on PR
    # #403: "Update the scheduled-task universe in proposed edits").
    scores = {"ADDME": _score("ADDME", 1.0, "ADD")}

    diff = render_proposed_diff(scores)

    assert diff["scheduled_tasks_json_edit"]["file"] == "config/scheduled_tasks.json"
    assert "ADDME" in diff["scheduled_tasks_json_edit"]["snippet"]
    assert diff["scheduled_tasks_json_edit"]["snippet"] == diff["config_tickers_json_edit"]["snippet"]


def test_render_proposed_diff_empty_when_nothing_qualifies():
    scores = {"REJECT_ME": _score("REJECT_ME", -2.0, "REJECT")}

    diff = render_proposed_diff(scores)

    assert diff["recommended_additions"] == []
    assert diff["config_tickers_json_edit"]["snippet"] == ""


def test_render_proposed_diff_preserves_rank_order_in_snippet():
    scores = {
        "SECOND": _score("SECOND", 1.0, "ADD"),
        "FIRST": _score("FIRST", 2.0, "ADD"),
    }

    diff = render_proposed_diff(scores)

    assert diff["recommended_additions"] == ["FIRST", "SECOND"]


def test_render_proposed_diff_is_json_serializable():
    scores = {"ADDME": _score("ADDME", 1.0, "ADD")}

    diff = render_proposed_diff(scores)

    json.dumps(diff)  # must not raise


def test_config_tickers_json_snippet_starts_with_a_comma_separator():
    # config/tickers.json's active array has no trailing comma after its
    # last element -- the snippet must supply the separator itself so
    # pasting it right before the closing `]` produces valid JSON.
    scores = {"ADDME": _score("ADDME", 1.0, "ADD")}

    diff = render_proposed_diff(scores)

    assert diff["config_tickers_json_edit"]["snippet"] == ', "ADDME"'


def test_render_proposed_diff_skips_already_active_ticker():
    scores = {"ACTIVE": _score("ACTIVE", 1.0, "ADD")}

    diff = render_proposed_diff(scores, active_tickers=frozenset({"ACTIVE"}))

    assert diff["recommended_additions"] == []
    assert diff["skipped_already_active"] == ["ACTIVE"]
    assert diff["config_tickers_json_edit"]["snippet"] == ""


def test_render_proposed_diff_skips_invalid_symbol_shape():
    # "BRK-B" is a real ticker but not representable in config/tickers.json's
    # `^[A-Z.]{1,6}$` schema pattern -- must not be proposed as an addition.
    scores = {"BRK-B": _score("BRK-B", 1.0, "ADD"), "GOOD": _score("GOOD", 1.0, "ADD")}

    diff = render_proposed_diff(scores)

    assert diff["recommended_additions"] == ["GOOD"]
    assert diff["skipped_invalid_symbol_shape"] == ["BRK-B"]


def test_render_proposed_diff_skips_explicitly_excluded_ticker():
    scores = {"BANNED": _score("BANNED", 1.0, "ADD")}

    diff = render_proposed_diff(scores, excluded_tickers=frozenset({"BANNED"}))

    assert diff["recommended_additions"] == []
    assert diff["skipped_explicitly_excluded"] == ["BANNED"]


def test_render_proposed_diff_still_proposes_genuinely_new_tickers():
    scores = {
        "ACTIVE": _score("ACTIVE", 2.0, "ADD"),
        "NEWTIC": _score("NEWTIC", 1.0, "ADD"),
    }

    diff = render_proposed_diff(scores, active_tickers=frozenset({"ACTIVE"}))

    assert diff["recommended_additions"] == ["NEWTIC"]
    assert diff["skipped_already_active"] == ["ACTIVE"]
