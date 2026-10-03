# Taste Profile - badMade

## Core Identity
Architecture and code judgment prioritize system simplicity, durability, and explicit safety boundaries over novelty, speculative design, or generative velocity. An abstraction earns its place only by eliminating an existing cost, enforcing a safety gate, or providing an injection seam for tests. Work is judged complete when tests precede code, gates pass, and all unproven claims, omitted scope, and mitigations are explicitly stated.

## Product & Engineering Judgment
**Q: What distinguishes elegant, production-grade software architecture from architecture that is bloated, decorative, or over-engineered? What are the immediate tell-tale signs to you that an abstraction or layer did not need to exist?**
An abstraction earns its place when it removes a cost that already exists: duplication, a safety boundary, or a seam for testing. If it only renames, forwards, or prepares for a second use that does not exist yet, it is decoration.
- What earns its place:
  - Injected callables (mapping 1:1 to tools and keeping code free of credentials and direct network calls).
  - Pure functional cores with side-effects pushed to the edges.
  - Single sources of truth with parity tests between code constants and configuration JSON.
  - Fail-closed gates that refuse when evidence is missing.
- Tell-tale signs a layer did not need to exist:
  - One implementation and one caller with no concrete second caller planned.
  - Forwarding layers that add no validation, no fail-closed behavior, and no injection seam.
  - Reinventing a helper that already existed in the repo.
  - Merging call sites with different freshness requirements (e.g. memoizing a shared quote helper that stale-serves breaker gates).
  - Requiring reflection, metaprogramming, or nesting deeper than 3 levels.
  - Adding dependencies where standard library suffices.
  - Untestable without mocking the universe.
  - Refactors smuggled inside feature or bug-fix PRs.

## Real Problems vs Loud Ones (Rule vs Reality)
**Q: When a diagnostic finds a divergence between a stated rule and operational reality, how do you decide whether to fix the code, change the rule, or ignore it?**
When a diagnostic finds a rule that was never active, ask what the rule is for.
- If it contradicts how the system is designed (e.g. an 8-orders-per-day cap contradicting a multi-slot graded ladder across 59 tickers where dollar caps are the true spend controls), change the rule to match the architectural intent and turn the proxy into a runaway-batch breaker (120).
- If it protects the thing the design depends on (e.g. daily/weekly dollar budgets, risk limits, fail-closed preflights), make it bind and prove it binds.
- Never leave a "binding" rule decorative.

## "Good Enough to Ship" and Anti-Churn Boundaries
**Q: What defines "good enough to ship" versus perfectionism or unfinished work?**
Ready to merge means: tests first, checks green, and the PR states what it did not prove and what it deliberately left out, with the reason and the mitigation.
- Minimum criteria before merge:
  - Tests written first, derived from expected behavior (bug fixes require a failing test first).
  - Standard linting (`ruff check .`) and test suite passing with >=90% coverage on supported runtimes.
  - Explicit statement of which of the three entry points is touched (`run_slot`, executor classes, or `dca_runner`).
  - Companion edits included in the same commit (`pyproject.toml` for modules, console configs for JSONs).
  - PR body containing the 7 fixed headers, explicit non-goals, and session link.
  - Secrets and diff churn verified clean.
- Shippable while unfinished:
  - A PR is shippable when gaps are acknowledged explicitly (`UNVERIFIED` labels, omitted scope with mitigations, `DEFERRED.md` tracking) rather than hidden.
- Where cleanup becomes churn:
  - Stop cleanup when the remaining issue fails closed, is unreachable off-host, or is mitigated.
  - Keep going when the code fails open or touches money.

## Quick Reference Card
**Always:**
- Anti-Entropy: ensure changes leave the system as simple or simpler than before.
- Test first: derive tests from expected behavior before touching implementation.
- Name the gaps: explicitly list what was not tested, not changed, and left unverified.
- Stop refactoring when code is clean enough to meet the goal; keep blast radius minimal.

**Never:**
- Never create future-proof abstractions for hypothetical second callers.
- Never leave decorative or unproven safety rules in place.
- Never mix refactoring into feature or bug-fix PRs.
- Never add third-party dependencies when standard library accomplishes the task.

**Calibration quotes:**
- "An abstraction earns its place when it removes a cost that already exists: duplication, a safety boundary, a place that needs a seam for testing."
- "When a diagnostic finds a rule that was never active, I ask what the rule is for. If it contradicts how the system is designed, I change the rule. If it protects the thing the design depends on, I make it bind and then prove it binds."
- "Ready to merge: tests first, checks green, and the PR states what it didn't prove and what it deliberately left out, with the reason and the mitigation."
