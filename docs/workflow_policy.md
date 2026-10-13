# Workflow policy checks

`scripts/workflow_policy.py` statically enforces three org-wide policies
against every file in `.github/workflows/*.yml`, run as ordinary pytest tests
(`scripts/tests/test_workflow_policy.py`) rather than a separate CI job:

* every job sets `timeout-minutes`;
* every workflow (or job) declares a `concurrency` block with a `group` and
  an explicit `cancel-in-progress`;
* every `actions/checkout` step sets `persist-credentials: false`.

A workflow file can declare an explicit, reasoned exception via a top-level
`x-policy-exceptions` key (GitHub Actions ignores unrecognized top-level keys,
the same mechanism already used for YAML anchor blocks in shared workflows):

```yaml
x-policy-exceptions:
  timeout-minutes:
    - job: some-job
      reason: "why this job is exempt"
  persist-credentials:
    - job: some-job
      step: "Some step name"   # optional: omit to exempt the whole job
      reason: "why this checkout is exempt"
```

An exception with no `reason` is rejected at parse time rather than silently
accepted. No workflow in this repository currently declares one; if that
changes, `test_no_real_workflow_declares_a_policy_exception` in the test file
above will fail until the test is updated to name and justify it explicitly,
so an exception cannot accumulate unnoticed.
