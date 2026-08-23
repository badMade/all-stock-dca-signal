# Share-class equivalence — proposal only, NOT applied

Status: **PROPOSAL. Nothing in the code or config implements this.** Written
2026-07-30 alongside the same-day top-up guard fix, and deliberately kept out of
that change. This is a portfolio-construction decision, not a bug.

---

## 1. The observation

On 2026-07-30 the 09:35 slot bought both halves of Alphabet in one batch:

| Time (ET) | Ticker | Amount | Fill price |
|---|---|---|---|
| 09:58:27 | GOOGL | $1.50 | 332.0699 |
| 09:58:37 | GOOG  | $1.50 | 331.5199 |

Ten seconds apart, $3.00 total, same company. The 11:30 slot then came back for
GOOGL again at 331.7699.

Nothing malfunctioned. `config/tickers.json` lists GOOG and GOOGL as two
separate entries; `dca_runner._normalize_ticker` only upper-cases and strips
whitespace; there is no aliasing layer anywhere in the repo. The only place the
pair is mentioned at all is a warning against prefix-matching them in ref-id
lookups (`dca_live_adapters.py:631-634`). So the system did exactly what it was
configured to do.

## 2. Why it is worth a decision

Two effects, pulling in opposite directions:

**Argument for treating them as one.** Economically they are near-identical
claims on the same cash flows. Two independent $1.50 buys is a $3.00 position in
Alphabet, so the effective per-name ladder multiplier is double what the config
says. The same-day top-up guard reinforces this: because the priors are tracked
separately, a slot that has already deployed into GOOGL sees $0.00 prior for
GOOG and authorizes a full first allocation. Across a day with seven slots that
compounds.

**Argument for leaving them separate.** They are separately tradeable, they
carry different voting rights, and the spread between them is itself a real
(small) source of averaging. A DCA strategy that buys the cheaper class on any
given tick is arguably doing something sensible rather than something
accidental. Collapsing them also throws away the option of the ladder firing on
one class and not the other when they diverge intraday.

I do not think one of these is obviously right, which is exactly why this is a
proposal rather than a patch.

## 3. Proposed shape, if adopted

### 3.1 Where it should live

`config/tickers.json`, as a new top-level key. Reasons:

* It is portfolio composition, and that file already owns the active universe.
  Putting it in `budgets.json` would imply it is a spend rule; putting it in
  `sizing_ladder.json` would imply it is a multiplier rule. It is neither.
* It has to be readable by both the Python engine and the executor prompts, and
  `config/tickers.json` is already in every prompt's Gate 2 reachability check
  by proximity.
* Keeping it out of `same_day_top_up_policy` matters: the policy is about
  *timing and sizing*, this is about *identity*. Conflating them would make a
  future reader think the equivalence only applies to top-ups.

```jsonc
{
  "equivalence_groups": {
    "$comment": "Tickers that represent the same underlying economic exposure. A group's members share one prior-allocation bucket and one reference price for the same-day top-up guard. Membership does NOT merge orders: each ticker is still bought and held separately.",
    "adopted": "PENDING HUMAN DECISION",
    "groups": [
      { "id": "alphabet",       "members": ["GOOG", "GOOGL"] },
      { "id": "berkshire",      "members": ["BRK.A", "BRK.B"] },
      { "id": "fox",            "members": ["FOX", "FOXA"] },
      { "id": "news-corp",      "members": ["NWS", "NWSA"] },
      { "id": "liberty-media",  "members": ["LSXMA", "LSXMK"] },
      { "id": "under-armour",   "members": ["UA", "UAA"] },
      { "id": "zillow",         "members": ["Z", "ZG"] },
      { "id": "paramount",      "members": ["PARA", "PARAA"] }
    ]
  }
}
```

Only `alphabet` is live today — GOOG and GOOGL are the only pair currently in
`config/tickers.json`. The rest are listed so the shape is obviously general and
so adding a second class of an existing holding does not silently reopen the
question. **Any group whose members are not in the active universe is inert.**

### 3.2 What it would change

Exactly one thing, in one place: `scripts/same_day_top_up_gate.evaluate_slot`
would map each ticker to its group id before looking up the effective prior, so
that a group's members share:

* one `effective_prior_usd` (the sum of same-day buy exposure across the group), and
* one `reference_price` (the minimum confirmed same-day fill price across the group).

Under that rule, today's Alphabet buys would have gone: GOOGL $1.50 at 09:58
(first allocation), then GOOG at 331.5199 — strictly below the group reference
of 332.0699, but with a $1.50 group prior against a $1.50 desired, so
`no_incremental_difference`, **$0.00**. Day total would drop from $17.00 to
$15.50.

### 3.3 What it would deliberately NOT change

* **Order placement.** Orders stay per-ticker. No netting, no substitution, no
  "buy whichever class is cheaper" logic. That is a much larger change and is
  not proposed.
* **The whitelist.** Both tickers stay independently tradeable.
* **Budget accounting.** `budget_snapshot` already counts every order into one
  shared pool, so equivalence has no effect there.
* **Cost basis, positions, reporting.** All stay per-ticker.

### 3.4 Migration and testing

* Default to **off**: `"adopted": "PENDING HUMAN DECISION"` means the gate reads
  the file, finds no adopted groups, and behaves exactly as it does today.
  Flipping it to `"2026-XX-XX"` turns it on.
* `test_same_day_top_up_gate.py::test_goog_and_googl_are_scored_as_independent_tickers`
  currently pins the OFF behavior. Adopting the proposal means adding an ON
  counterpart, not deleting that test — both behaviors should stay covered so
  the switch is provably a switch.
* One backfill question worth answering before flipping it: a group whose
  members were bought at different prices earlier the same day gets a reference
  price of the group minimum, which is stricter than either ticker's own. That
  is the safe direction, but it will suppress some top-ups that today would
  proceed. Expect deployment to fall slightly.

## 4. The decision needed

1. Adopt group-shared priors for GOOG/GOOGL, yes or no?
2. If yes: shared reference price = **minimum across the group** (proposed,
   conservative) or **each ticker's own** (permits a top-up in the cheaper class
   while the dearer class is above its own reference)?
3. If yes: does the same rule apply to a manual buy of one class, or only to
   scheduled slots? (`same_day_top_up_policy.manual_override_handling` already
   folds manual fills into the guard as prior context, so the default answer is
   "yes, manual counts", but it is worth stating.)

Until 1 is answered, the gate treats every ticker independently and today's
Alphabet behavior repeats.
