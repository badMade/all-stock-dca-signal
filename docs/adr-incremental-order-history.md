# ADR: Incremental order history, batched tradability and an audited per-order record (9:35 slot)

Status: accepted by the operator on 2026-10-29 for the 9:35 slot. It ran live on 2026-11-05 with 18
orders (1 `full` and 17 `incremental` per-order records, each with a merge hash, and a clean
`check_per_order_record`), the batch size at which the old full re-read compacted the session, so
on 2026-11-06 the same four rules and the Gate 9 audit were added to the other six mirrors
(10:30, 11:30, 12:30, 1:30, 2:30, 3:30). The live tasks change only when the operator repastes them.

## Context

The 9:35 live prompt required the run to re-fetch and re-paginate the COMPLETE same-day and
same-ISO-week order history before every placement. Each page returns every order so far, so
the cost grows with the batch: 18 orders cost roughly 360,000 tokens of tool output. On
2026-10-29 the run's session was compacted at 10:00:12 ET in the middle of the batch.

On 2026-10-28 and 2026-10-29 the run also deviated from the mandatory text, and said so only
in its closing summary:

* filtered history fetches (by `symbol` and `state`, then by a `created_at_gte` of "13:52 UTC"),
  the run's own way of reading less;
* no weekly-history refetch before each order ("budget headroom is wide");
* one batched tradability call instead of one per ticker.

The 2026-10-29 run had 20 minutes of window left, so time pressure was not the cause. The
prompt gave the run no legitimate way to read less, and no check that noticed when it did.

## Decision

1. **Incremental history** (`scripts/merge_order_pages.py`). One full day and week fetch at
   batch start, saved with `assemble_order_pages`. Before each later order, fetch only orders
   created since the last snapshot minus 120 seconds and merge them into both baselines. Caps,
   the Gate 4 rescan and the top-up guard read the merged envelopes, so they still see the
   complete history. The script fails closed (exit 2, writes nothing) when the increment's
   window starts after the overlap, when a base order inside the window is missing from the
   increment, on an unfollowed cursor, a placeholder id, a condensed row, or an account or day
   mismatch. On exit 2 the run does a full refetch; it never proceeds on a refused merge.
2. **Batched tradability.** One `get_equity_tradability` call for all survivors before the first
   placement is valid for 300 seconds. Later orders need a fresh call for that ticker.
3. **No silent skips.** A wide cap, a short batch or a shortage of time is never a reason to skip,
   narrow or shorten a named check. A check that cannot run means stop and alert.
4. **Audited per-order record** (`scripts/check_per_order_record.py`). The run appends one line
   per placed order to `per_order_checks.jsonl`; Gate 9 runs the checker before notifying and
   carries its failures into the alert.

## Alternatives considered

* **Leave the prompt as is and rely on the closing summary.** Rejected: it failed on two
  consecutive days, and the summary is the only place the deviation appears.
* **Allow the run to narrow its fetches at its own discretion.** Rejected: that is what it did
  with no check on whether the narrowed view still covered the window.
* **A single incremental fetch with no merge script.** Rejected: the merge, the overlap rule and
  the consistency check are the part that protects the spend arithmetic, and they belong in
  tested code, not in prompt prose.

## Consequences

* The per-order token cost stops growing with the batch, which removes the cause of the
  compaction.
* The record is SELF-REPORTED. It cannot prove a check ran; it turns a silent omission into a
  named failure. A run that writes a false record is not caught by this change.
* The merge handles live-money history. A bug could undercount spend; the fail-closed design
  and the full-refetch fallback limit this, and only a live fire exercises it against real
  broker responses.
* The prompt for this slot gets longer. It is applied by pasting, so it changes nothing live
  until the operator does.

## Verification

After the next 09:35 fire: every placed order has a complete line in `per_order_checks.jsonl`,
`python3 -m scripts.check_per_order_record --run-dir <run dir>` exits 0, the run reports no
compaction, and no history fetch carries a filter other than `created_at_gte`.

## Addendum 2026-11-05: week baseline reuse at 12:30 (`scripts/reshape_order_pages.py`)

The post-9:35 slots now build their ISO-week history from the 9:35 week envelope plus an
incremental merge, after the 2026-11-05 3:30 run aborted at Gate 6 because the full week
(~150+ orders, ~184 KB) could not be saved. 12:30 hit the same wall on 2026-11-03 but could not
take the same fix: its Gate 4 and Gate 6.5 scripts read `orders_week.json`, a flat list the
session reshaped from the raw rows BY HAND, not the envelope.

`scripts/reshape_order_pages.py` is that reshape as code. It binds the envelope to the account
and the week's Monday, re-runs `assemble_order_pages`' row checks and the same complete-chain
replay `assemble` uses (`same_day_order_snapshot` over `_PageReplayer`; `assemble_order_pages.py`
itself is untouched because it is bundled into `backup_planner.pyz`), and writes one row per
order id in the 12:30 Gate 4 shape. Row values follow `backup_planner._to_broker_order`
(quantity-based buys valued at quantity x price, `executed_dollar_amount` only for dead orders,
`client_order_id` from `ref_id`), except that "dead" is `dca_runner`'s canonical terminal-state
set, which also holds `expired` -- `backup_planner._DEAD_STATES` omits it, so the two disagree
only on an expired, partially filled buy. Any row it cannot map exactly is exit 2, nothing
written, and the prompt falls back to the full Monday fetch.
