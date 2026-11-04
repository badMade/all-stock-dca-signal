# VTO: triage-digest hardening, cycle 2026-11-02

**What:** make the daily dca-triage-digest trustworthy: it must not report a fix as done without evidence, and must say so loudly when it ran blind.
**Why now:** the 2026-11-02 digest ran with no logs (`DCA_CRON_TRACKING_ROOT` unset), matched git evidence by hand, and found 4 Approved rows marked repaired with no PR or commit.
**Who:** the operator reading the digest each morning.
**Success:** a digest that (a) flags unsupported "Repaired" claims, (b) fails visibly when logs are unreachable, (c) handles shallow clones, (d) has a defined path for `window_insufficient`.
**Non-negotiables:** stdlib only; no live-path changes; no Notion writes from code in this cycle; no secrets; every change has a regression test; draft PRs only.
