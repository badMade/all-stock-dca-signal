# Deferred backlog — automated self-healing

Items raised during the `rocket-fuel/self-heal` cycle and deliberately left out of it.
Each entry says why, so the next cycle re-evaluates rather than re-derives.

- **Auto-merge of any repair.** Operator decision (interview Q4): this repository drives
  live trading, so every repair lands as a labeled PR a human merges. Revisit only with a
  hard allowlist (lockfile bumps, `ruff format`, docs) and a green-CI precondition.
- **Agent/LLM-driven repair of core trading logic.** The repair path stays deterministic
  (`scripts/self_heal.py`'s existing steps). The spend ledger and budget gates are built now
  so an agent-assisted path has a ceiling waiting for it when it arrives.
- **Repairing the sibling research suites** (`production-safe-agent-loop/`,
  `ticker-advisor/`, `ml-forecaster/`, `wondrous-octopus/`, `ticker-market-breadth/`).
  `healthcheck.py` cannot install their heavy deps, and `self-heal.yml`'s `scope_check`
  already documents this exclusion.
- **Multi-machine coordination.** One operator, one Mac holding the artifact tree.
- **Actually fixing the shard-1 MPS `NotImplementedError`.** This cycle must *observe and
  route* it; repairing it is separate work.
