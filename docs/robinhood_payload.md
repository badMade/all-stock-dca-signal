# Prebuilt Robinhood payload (Codex on the Mac)

**Problem (2026-10-15).** With the yfinance fallback retired (692b5887), the live sessions had to pull ~67
tickers of 5-minute bars through MCP tool output themselves. They judged that infeasible inside the slot
window; the 11:30, 1:30 and 2:30 runs all aborted at Gate 5 with nothing placed.

**Why not repo code.** Repo code cannot call Robinhood: a headless client cannot obtain tokens (verified
2026-10-13), and this repo holds no credentials. The Codex CLI on the operator's Mac already holds an approved
Robinhood MCP connection (`codex mcp list` -> `robinhood-trading`).

**What runs.** `deploy/launchd/robinhood_payload.sh auto`, fired one minute after each Robinhood-primary slot's
decision time (09:35 10:30 11:30 12:30 13:30 14:30, Mon-Fri; the 15:30 slot is Finance-primary and not covered):

1. `scripts/robinhood_payload.py plan` resolves the slot, the exact universe that slot's Gate 5 generator scores,
   the earliest bar the slot's 60-minute trend needs, and the output path.
2. The universe is split into chunks of 8 and each runs through `codex exec` (read-only sandbox, 3 at a time,
   240 s per chunk). The worker may only read historicals and quotes.
3. `scripts/robinhood_payload.py merge` validates every ticker (tz-aware timestamps, consistent OHLC, the
   slot's window, newest bar near the anchor, no outlier bars, quote within 25% of its own last bar) and
   publishes `cron_tracking/robinhood_payload/<day>/robinhood_payload_<day>_<slot>.json` atomically, with a
   `.meta.json` sidecar. A ticker that fails goes to `ticker_errors.robinhood`; coverage under 80% publishes
   nothing, so the slot aborts instead of trading a thin universe.
   Each worker's reply counts only for the tickers its own chunk was assigned (`chunk_<i>.json` is
   `plan["chunks"][i]`): any other key is dropped and reported, a ticker two replies carry fails, and a plan
   whose chunks do not partition its universe publishes nothing.

The payload is Robinhood data under `sources.robinhood` and nothing else. It is not a second source, and the
generators are unchanged.

**What the live prompts do.** Gate 5 (six `live-mirror` prompts) runs `plan` then `check`; on success it copies
the payload to `$ROOT/market_data_<day>_<slot>.json` and skips the hand fetch. If the file is missing or fails
`check` after 5 minutes of re-checking, the run aborts with `market_data_unavailable`. It never hand-fetches.

**Install.** Copy `deploy/launchd/com.all-stock-dca.robinhood-payload.plist.template` to
`~/Library/LaunchAgents/`, replace the three `REPLACE_WITH_*` values, and `launchctl load` it. The Mac must be on
ET (or shift `Hour`). Logs: `/tmp/dca-robinhood-payload.log`.

**Honest limits.**
- An LLM copies the numbers. The validation catches malformed, inconsistent and outlier data; it cannot prove a
  plausible-looking bar is exact. Treat this as the trade-off for having any Robinhood path off-session.
- Needs the Mac awake and Codex logged in. A failure leaves the slot to abort, as before.
- `~/.codex/config.toml`'s default model is used. Pin one with `DCA_CODEX_MODEL` only if needed (model names
  the CLI/account accepts change between releases; `gpt-5.6-sol` worked on 0.145 and is refused on 0.160).
- First live proof (2026-10-15, 2 tickers, one chunk): 23 s, validated 2/2.
