# Deferred — plan-first recommendations cycle

Valid items kept out of this cycle's scope (see `PLAN-plan-first.md`).

- Deeper object-oriented refactor of `near_close_dca`, the executor classes, and
  consolidating the three entry points. Per CLAUDE.md, consolidation needs its own
  decision and acceptance run.
- A console page for fill comparison and the 5-minute live data.
- Existing ref_id inconsistencies: `dca_runner.execute_plan` and
  `dca_order_plan._expected_ref_id` still produce raw `dca-runner-…` strings, and the
  Perplexity plan-first drafts require the ref_id to echo back. Fix these only if Rock 2
  parity needs it.
- Any disagreement found between `dca_runner`'s Gate 6 code and the live prompts' Gate 6
  prose. Log it here; the code is the parity reference.
- Repasting the live prompts. The operator does this after at least 5 trading days of
  shadow mode.
- **PR #1660 conflicts with main in the 09:35 live-mirror prompt** (found 2026-10-13). Main
  (2026-10-12) says "NO FALLBACK DATA SOURCE". PR #1660's branch says "PREFETCH-FIRST", with
  the launchd yfinance file as the primary source. The operator must resolve this on #1660
  itself. This cycle copied only `scripts/validate_native_placement.py` and its test from
  that branch.
- **Rock 2 Gate 6 tier mismatch** (found 2026-10-13). `RecommendationEngine` follows the
  requested `dca_runner.assess()` chain, whose `_tier_selection_inputs()` and
  `select_budget_tier()` choose a dynamic tier. The 09:35, 10:30, 11:30, 13:30, and 14:30
  live-mirror prompts explicitly require the Normal tier unconditionally; their stream
  configs cap those streams at $180/$900. The 15:30 prompt instead selects by the count of
  3x-or-greater signals. **Resolved by PLAN Rev 4**: the stream slots follow the live rule (Normal only), and 15:30
  keeps `select_budget_tier`. The mismatch itself stays in `dca_runner.assess`, which is not
  live-wired; no change is made there this cycle.
- **Headless Robinhood client** (Rock 0, removed 2026-10-13). Robinhood's authorize page
  does not complete OAuth for a self-registered client; only known agent apps can obtain
  tokens. Reconsider `dca_mcp_client` (read-only allowlist, Keychain token store, PKCE
  loopback) only if Robinhood publishes client registration for third-party apps.
- **Rock 6 Gate 4 classification difference (2026-10-13):** the 10:30 live prompt classifies `placed_agent` values `user`, `recurring`, and `drip` as the operator's own activity and treats every other value (including unknown) as rogue. `dca_runner.gate_sole_executor` is stricter and would classify operator-origin orders as another executor. The slot CLI follows the cited 10:30 prompt rule; no change was made to the runner.
