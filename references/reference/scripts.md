---
slug: /reference/scripts
title: Scripts
description: Purpose, required arguments and defaults for the supported command-line tools.
---

# Scripts

Run `python3 scripts/<name> --help` from the checkout root for argument syntax. Paths in examples are placeholders.

## Collection and Checks

| Script | Required arguments | Behavior and defaults |
| --- | --- | --- |
| `collect_codex_sessions.py` | `--codex-home`, `--session-state-dir`, `--snapshot-file`; `--credential-file` when serving | `--host 127.0.0.1`, `--port 9464`, `--poll-seconds 5` (1–60). `--once` scans one pass and exits without a listener. |
| `collect_codex_account.py` | `--codex-bin`, `--codex-home`, `--snapshot-file` | Account reads about once per minute; `--once` performs one read. Makes upstream requests. |
| `create_metrics_token.py` | `--credential-file` | Creates a new `0600` token file in an existing trusted directory; refuses replacement. Prints no token. |
| `check_metrics.py` | `--credential-file` | `--url http://127.0.0.1:9464/metrics`; four-second timeout. Checks health-metric presence; disables proxies and redirects. |

Session collection also accepts `--session-retention-seconds 2592000` and `--session-export-cap 1000`; see [Retention and Limits](retention-and-limits.md). `--state-dir` adds an existing dispatch ledger, and `--account-snapshot-file` adds the optional account snapshot. These are separate inputs from the session state and snapshot.

## Dashboard Renderers

All four require `--template` and `--output`. Session renderers also require `--snapshot-file`; the dispatch renderer requires `--presentation-file`.

| Script | Output / additional option |
| --- | --- |
| `render_codex_beta_dashboard.py` | Work Overview; optional `--labels-file` aliases. |
| `render_codex_unified_dashboard.py` | Unified, using collected names. |
| `render_codex_sessions_dashboard.py` | All Sessions. |
| `render_observability_dashboard.py` | CWO Dispatches. |

All accept `--datasource-uid` for provisioning and `--watch-seconds` (1–60). Omitting the watch option renders once. Omitting the datasource keeps the import picker.

## Dispatch and Release Tools

| Script | Required arguments | Behavior and defaults |
| --- | --- | --- |
| `export_dispatch_observability.py` | `--state-dir`; `--credential-file` when serving | Loopback `--host 127.0.0.1`, default `--port 9464`. Use **9465** alongside session collection. `--once` prints metrics; `--capacity-report` prints local counts. These modes are exclusive. |
| `export_terminal_observations.py` | `--state-dir`, `--output-dir` | Exports eligible completed jobs, then exits. |
| `run_observed_codex.py` | `--manifest`, `--authorization-file`, `--observability-config`, `--receipt-dir` | Starts authorized model work. `--linger-seconds 0`; optional `--presentation-file` and `--project-name` must be paired. See [Observed Job Runner](../integrations/observed-job-runner.md). |
| `build_release.py` | `--component`, `--output-dir` | Builds a content-hashed bundle. Components: `sessions`, `account`, `stable`, `beta`, `unified`, `dispatch`. |
| `validate_repository.py` | None | Checks source assets and links. `--staged` checks the exact Git index before publication. |
