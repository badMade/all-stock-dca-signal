<!-- Paste location: append this section to the end of live task 041d9d90's prompt (Weekly DCA Notion performance dashboard refresh, Fridays 5:30 PM ET). -->

# Friday 041d9d90 "Goal progress" section

GOAL PROGRESS SECTION (Fridays only). This task, `041d9d90`, runs Fridays at 5:30 PM ET. After the
dashboard refresh above, add one Notion section titled "Goal progress". It is analytics only and
places no orders.

1. Confirm the day with `dca_goal_progress.friday_refresh_sections(today)`. If it does not return
   `["Goal progress"]`, skip this section entirely.

2. Read the latest row per goal from the ledger: `LedgerStore(<GOAL_LEDGER_DB>).goal_progress()`
   (`dca-sizing-signals`, its own venv), keeping for each `goal_id` the row with the latest `day`.

3. Build the section text with the module, not by hand:
   ```
   section = dca_goal_progress.goal_progress_section(rows, today=today)
   ```
   Publish `section["title"]` as the heading and `section["text"]` as the body, unchanged.

4. A goal whose window is under one year shows its period return only, never an annualized
   figure: the function already enforces that, so do not add, compute or restate an annualized
   number anywhere in the section. With no rows, the function returns "no goal progress recorded
   yet"; publish that line as is.

5. No account number, no position-level dollar values, and no order or trade tool in this section.
