"""Render a ranked scoring run as a human report and a proposed config diff.

Neither function here writes to any file inside the core repo -- callers
(``advise_tickers.py``) decide where these strings/dicts land, and per the
approved design they land under ``analysis/<date>-ticker-advisory/``, never
directly into ``config/tickers.json`` or ``near_close_dca.py``.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from scoring import TICKER_SYMBOL_PATTERN, TickerScore, rank

BROKER_VERIFICATION_REMINDER = (
    "Broker tradability NOT verified by this tool -- this repo has no broker/network access. "
    "Before applying, confirm via Robinhood MCP that the symbol resolves to a real tradable "
    "equity/ETF (not a cash-settled index or derivative), exactly as documented in "
    "docs/strategy.md's 'TICKER UNIVERSE UPDATE' entries."
)


def _escape_markdown_table_cell(text: str) -> str:
    """Make ``text`` safe to insert into a Markdown table cell.

    A persisted FinBERT/yfinance/NewsAPI/SEC error can contain a newline or
    "|" character -- realistic for multiline model/download exception
    text -- and inserting it unescaped splits this row across extra
    Markdown rows/columns, making scores.md structurally misleading or
    unreadable (Codex review on PR #403: "Escape rationale text before
    inserting it into the Markdown table").
    """
    return text.replace("|", "\\|").replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


def render_markdown(
    scores: dict[str, TickerScore],
    *,
    generated_at: datetime | None = None,
    run_metadata: dict[str, Any] | None = None,
) -> str:
    """Render a ranked Markdown table of every candidate's score.

    Args:
        scores: ticker -> TickerScore, as produced by scoring.combine_scores().
        generated_at: Timestamp to stamp the report with; defaults to now
            (UTC). Exposed as a parameter so tests get a deterministic report.
        run_metadata: The parameters that produced this run (effective
            weights, benchmark, lookback_years, skip_sentiment, etc. --
            see advise_tickers.main()'s run_metadata dict). When given,
            rendered as a "Run parameters" section so a saved report can be
            reproduced or distinguished from a differently-configured run
            after shell history is gone.

    Returns:
        A complete Markdown document: a title, generated-at line, an
        optional run-parameters section, one row per candidate ordered
        highest-composite-first (scoring.rank()), a note on the z-score
        units, and the broker-verification reminder. Composite/sub-score
        cells read "n/a" when that value is None.
    """
    ranked = rank(scores)
    generated = generated_at or datetime.now(timezone.utc)
    lines = [
        "# Ticker advisory report",
        "",
        f"Generated: {generated.isoformat()}",
        "",
    ]
    if run_metadata:
        lines.append("## Run parameters")
        lines.append("")
        for key, value in run_metadata.items():
            # A run_metadata value is not always a table cell -- e.g.
            # sentiment_unavailable_reason can carry a real multiline FinBERT
            # init traceback, or --history-dir can be an attacker/user
            # supplied path -- and this list-item line was previously
            # rendered with no escaping at all, unlike the score table cells
            # a few lines below. An embedded newline splits this bullet
            # across extra Markdown lines and can inject a heading or other
            # report structure into scores.md (Codex review on PR #429:
            # "Escape run metadata before rendering Markdown").
            lines.append(f"- **{key}**: {_escape_markdown_table_cell(str(value))}")
        lines.append("")
    lines.extend(
        [
            "| Rank | Ticker | Composite | Classification | Health | Backtest | Sentiment | Notes |",
            "|---|---|---|---|---|---|---|---|",
        ]
    )
    for index, score in enumerate(ranked, start=1):
        composite = f"{score.composite:.3f}" if score.composite is not None else "n/a"
        sub = {
            key: (f"{value:.3f}" if value is not None else "n/a") for key, value in score.sub_scores.items()
        }
        notes = "; ".join(_escape_markdown_table_cell(item) for item in score.rationale) if score.rationale else ""
        lines.append(
            f"| {index} | {score.ticker} | {composite} | {score.classification} | "
            f"{sub['health']} | {sub['backtest']} | {sub['sentiment']} | {notes} |"
        )
    lines.append("")
    lines.append(
        "Composite/sub-scores are cross-candidate z-scores (standard-deviation units), not raw "
        "metric values -- see scoring.py's CLASSIFICATION_BANDS for the ADD/WATCH/REJECT cutoffs. "
        "Raw per-signal metrics are in the accompanying scores.json."
    )
    lines.append("")
    lines.append(f"**{BROKER_VERIFICATION_REMINDER}**")
    return "\n".join(lines) + "\n"


def render_proposed_diff(
    scores: dict[str, TickerScore],
    *,
    as_of: date | None = None,
    active_tickers: frozenset[str] = frozenset(),
    excluded_tickers: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Build the literal edit a human/agent would apply to add ADD-classified candidates.

    Mirrors the existing manual workflow documented in docs/strategy.md:
    append new symbols at the end of both config/tickers.json's `active`
    array and near_close_dca.ACTIVE_TICKERS, in the same order, so
    test_config_invariants.py's ordered-list parity check passes once
    applied. This function never touches either file itself.

    ``active_tickers``/``excluded_tickers`` are the *current* contents of
    config/tickers.json (advise_tickers.py reads them, read-only, before
    calling this). Without them, a candidate that happens to already be
    active would get proposed as a duplicate addition
    (test_active_tickers_have_no_duplicates would fail once applied), and a
    candidate that happens to be on the explicit exclusion list would get
    proposed as both active and excluded (the disjointness invariant would
    fail) -- this tool has no other way to know the current universe, since
    it only ever sees the candidate list it was asked to score (Codex
    review on PR #403: "Exclude existing and explicitly excluded tickers
    from additions").
    """
    effective_date = as_of or date.today()
    add_classified = [score.ticker for score in rank(scores) if score.classification == "ADD"]

    additions = [
        t
        for t in add_classified
        if t not in active_tickers and t not in excluded_tickers and TICKER_SYMBOL_PATTERN.match(t)
    ]
    skipped_already_active = [t for t in add_classified if t in active_tickers]
    skipped_explicitly_excluded = [t for t in add_classified if t in excluded_tickers]
    skipped_invalid_symbol_shape = [
        t for t in add_classified if t not in active_tickers and t not in excluded_tickers
        and not TICKER_SYMBOL_PATTERN.match(t)
    ]

    # config/tickers.json's `active` array has no trailing comma after its
    # last element -- pasting a bare `"NEW"` immediately before the closing
    # `]` would produce invalid JSON (`"VT" "NEW"`). Leading with `, ` makes
    # the snippet a literal, directly-pasteable insertion rather than
    # requiring the applier to remember to add the separator themselves
    # (Codex review on PR #403: "Include the separator in the config append
    # snippet").
    tickers_json_snippet = ", " + ", ".join(f'"{t}"' for t in additions) if additions else ""
    near_close_dca_snippet = "\n".join(f'    "{ticker}",' for ticker in additions)

    return {
        "generated_date": effective_date.isoformat(),
        "recommended_additions": additions,
        "skipped_already_active": skipped_already_active,
        "skipped_explicitly_excluded": skipped_explicitly_excluded,
        "skipped_invalid_symbol_shape": skipped_invalid_symbol_shape,
        "broker_verification_reminder": BROKER_VERIFICATION_REMINDER,
        "config_tickers_json_edit": {
            "file": "config/tickers.json",
            "instructions": (
                "Insert this text immediately after the last existing element in the top-level "
                "`active` array, before the closing `]` (the snippet already includes the leading "
                "comma separator)."
            ),
            "snippet": tickers_json_snippet,
        },
        "near_close_dca_py_edit": {
            "file": "near_close_dca.py",
            "instructions": (
                "Append these lines immediately before the closing `]` of the ACTIVE_TICKERS list, "
                "preserving the exact same order as the config/tickers.json edit above -- "
                "test_config_invariants.py checks `active` order, not just membership."
            ),
            "snippet": near_close_dca_snippet,
        },
        # config/scheduled_tasks.json's own "source_of_truth" note on this
        # field says config/tickers.json IS the source of truth here, but
        # nothing keeps them in sync automatically -- test_config_invariants.py
        # has no check comparing this field against config/tickers.json's
        # `active` array, unlike the two edits above. This field ALREADY drifted
        # once in this exact repo (config/scheduled_tasks.json's own
        # "ticker_universe_update_2026_08_04" note records a prior addition
        # that updated config/tickers.json and near_close_dca.py but left this
        # field listing the OLD, smaller universe until a later manual repair)
        # -- describing the addition as a complete two-file update silently
        # reproduces that exact omission for whoever applies this diff (Antigravity
        # review on PR #403: "Update the scheduled-task universe in proposed
        # edits"). Same flat-array shape as config/tickers.json's `active`, so
        # the identical append snippet applies.
        "scheduled_tasks_json_edit": {
            "file": "config/scheduled_tasks.json",
            "instructions": (
                "Insert this text immediately after the last existing element in "
                "canonical_first_buy_and_topup_rule.active_universe.active's array, before its "
                "closing `]` (the snippet already includes the leading comma separator). No "
                "automated check enforces this field staying in sync with config/tickers.json's "
                "`active` array -- verify it by hand."
            ),
            "snippet": tickers_json_snippet,
        },
        "post_apply_checklist": [
            "Verify each symbol via Robinhood MCP (real tradable equity/ETF, not an index).",
            "Run test_config_invariants.py to confirm config/tickers.json and near_close_dca.py "
            "stayed in lockstep (this does NOT check config/scheduled_tasks.json).",
            "Add a 'TICKER UNIVERSE UPDATE' changelog entry to docs/strategy.md and "
            "config/tickers.json's `notes` field, per the existing convention.",
            "Once the ticker has enough archived history, "
            "python -m scripts.generate_per_ticker_sizing will calibrate its sizing tiers "
            "-- until then it uses the global fallback ladder.",
        ],
    }


__all__ = ["BROKER_VERIFICATION_REMINDER", "render_markdown", "render_proposed_diff"]
