# Handoff: deprecate the plan-first courier (2026-10-27)

Branch: `claude/rescue-no-data-tickers` (also carries the payload no-data rescue, see `HANDOFF_payload_rescue.md`).
Open any PR as a DRAFT (repo rule).

## Outcome
- The 5-minute Claude plan-first courier is marked DEPRECATED, nothing deleted: notice atop
  `claude/dca-executor-prompts/drafts/plan-first/courier.draft.txt`, docstring in `scripts/snapshot_cycle.py`,
  banner in `docs/plan_first_recommendations.md`, note in the `docs/README.md` index.
- A port of the courier's order-history and buying-power fetch into `scripts/robinhood_payload.py` was tried and
  DROPPED (reverted). Reasons: plan-first is shadow-only; it needs a Codex worker to copy paginated order history
  verbatim in one reply; and payload quotes would need bid/ask or Gate 6.7 drops every order as `spread_unavailable`.
  Revisit when plan-first goes live. `snapshot_cycle ingest` stays as the reference for that contract.
- Windows-only fix in `scripts/robinhood_payload.py` `publish`: the temp file is now written with `newline=""` so the
  bytes match the sidecar SHA-256. On Windows, text mode wrote CRLF, so `check` reported "payload does not match its
  sidecar checksum" and 4 tests failed. No effect on the Mac.
- `tests/test_robinhood_payload.py` and `tests/test_robinhood_payload_1530_shadow_launchd.py` pass; ruff is clean.
  The full suite (about 5 minutes) has not been run.

## Remaining (operator / cloud)
1. Run the full suite and `ruff check .`.
2. Open a DRAFT PR for the branch (no `gh` on this Windows shell; use the GitHub UI or a session with `gh`).
3. After merge, `git pull` on the Mac so launchd picks up the rescue. Confirm the Mac has the keep-logs code
   (`<payload>.chunk_logs/`), or the next failure leaves no log.
4. Not yet checked: did the 14:30 payload pass on 2026-10-26, and did the same 8 tickers (DFTX, TRIN, AAAU, SPCX, EVR,
   LUNR, IRDM, DXYZ) fail at 09:35? See `robinhood_payload_2026-10-26_0935.refused.json` in the Mac repo folder.
5. Not covered by the rescue: a ticker that replies with data but fails validation (stale quote, bad bar).

## Side note: PR #1968
Docstring-only change to `_is_finite_scalar` in `dca_inverse_martingale.py` plus a rebuilt `backup_planner.pyz` and six
regenerated Perplexity prompts. No behavior change, unrelated to the courier. Low necessity; merge or close.

## Tooling gotchas
- `gh` is not installed on this Windows shell; `git fetch origin pull/<n>/head:<branch>` reads a PR.
- The repo is private, so the built-in browser cannot open PR pages.
