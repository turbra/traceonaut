# Traceonaut

Traceonaut provides Codex collectors, telemetry contracts, Grafana dashboards,
renderers, and validation tools.

- Keep runtime helpers Python standard-library only.
- Keep stable and beta dashboard identities and existing metric names
  compatible. Beta remains the experimentation dashboard.
- Preserve observed versus requested data, unavailable versus zero values,
  interval versus recorded-history totals, and account versus session scope.
- Keep credentials, session records, databases, rendered host configuration,
  browser sessions, local Beads state, and internal work records out of Git.
- Public documentation explains current setup, use, data limits, and supported
  interfaces. Do not publish migration records, review transcripts, experiment
  reports, sprint plans, or implementation diaries.
- Use `bd` in this repository for observability work.
- CWO owns orchestration and its optional integration hook. Do not copy the CWO
  controller into this repository or make session collection depend on it.
- Check the worktree before edits. Preserve unrelated changes. Do not commit or
  push unless the user explicitly requests it.

## Proportionate validation

Run syntax and focused tests for the files and behavior changed. For a layout or
text-only dashboard edit, validate its JSON, relevant dashboard/renderer tests,
and affected live panels. Do not run the full suite solely to remove a heading.

For collector, accounting, import-boundary, or repository-wide changes, run the
relevant behavior suites; run the full Traceonaut suite when the change crosses
those boundaries:

```bash
python3 -m compileall -q scripts tests
python3 scripts/validate_repository.py
python3 -m unittest discover -s tests -v
```

Prometheus query tests require a separately verified test binary configured via
`CWO_TEST_PROMETHEUS_BINARY`. Report skips and preexisting failures separately.
Verify deployed release paths and live behavior separately from local test results.
Keep immutable runtime releases separate from the working checkout.

After selecting and staging files for an authorized commit, run
`python3 scripts/validate_repository.py --staged`. It checks the index, not
unstaged work. Also review the contents for private values and consumer relevance;
syntax and link checks cannot establish those properties.
