#8 operator-note labelling policy: needs an operator decision first.

2026-11-20 top-100 buying list not wired into the live path (operator to fix later).
Intent (operator): the top 100 of the composite rank list become the new buying list; the composite score stays a factor in that ranking.
Finding (11:30 ET slot session, 2026-11-20): the change is not in this checkout, so the slot still buys from the full 122-ticker active universe in `config/tickers.json`. Nothing in the config, the signal generator or the gate scripts limits buys to the composite rank's top 100.
- The rank tool is read-only. Its README says it does not touch `config/` or any order path. It writes a review-first proposal to `artifacts/composite_rank/latest_tickers_proposal.md`; the 2026-11-19 proposal says nothing has been applied.
- That proposal is a different rule: it is built on the daily trade list (score above 80, plus holdings above 50), not a top-100 cutoff. It would add 15 names and stop buying 73, including DFTX, SPCX, DXYZ, GFUZ, VCX, and also NVDA, AMD, AVGO, TSM, MSFT, AMZN, GS and CAT.
- The universe has not changed: `config/tickers.json` still shows 122 active names, and its recent commits only add names. None applies a rank filter.
Effect seen: on 2026-11-20 the slot and manual buys included names outside the top 100 (SPCX is unranked; LH 4, MAR 19, HLT 52 are inside it).
Addition (operator, 2026-11-20): the operator will also add 10 slots for custom adds to the top-100 buying list, i.e. up to 10 hand-picked tickers beyond the rank-based 100. Not built yet; how custom adds interact with the composite score, the active universe and the gates is still to be defined.
