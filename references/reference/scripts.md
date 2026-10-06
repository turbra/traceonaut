---
slug: /reference/scripts
title: Scripts
description: Purpose, required arguments and defaults for the supported command-line tools.
---

# Scripts

Run `python3 scripts/<name> --help` from the checkout root for syntax.
Paths in examples are placeholders.

## Collection and Checks

| Script | Arguments and behavior |
| --- | --- |
| `collect_sessions.py` | `--session-state-dir` and at least one source pair below. `--credential-file` is required when serving. Defaults: `--host 127.0.0.1`, `--port 9464`, `--poll-seconds 5` (1–60). `--once` reports enabled sources under `codex` / `bob` and exits without a listener. |
| `create_metrics_token.py` | `--credential-file`: creates a new `0600` token file in an existing trusted directory, refuses replacement and prints no token. |
| `check_metrics.py` | `--credential-file`; default `--url http://127.0.0.1:9464/metrics`, four-second timeout. Checks health-metric presence with proxies and redirects disabled. |

Both sources accept `--session-retention-seconds 2592000` and
`--session-export-cap 1000`; see [retention](retention-and-limits.md).
Invalid configuration and fatal state/snapshot writes exit nonzero. Source read
failures keep the listener running with that source's availability at 0.
`--once` performs one bounded scan slice per source; unavailable sources exit
nonzero, while readable sources with pending work exit 0.

## Codex

### Collection

Enable with `--codex-home` and `--snapshot-file`.

| Script | Required arguments | Behavior and defaults |
| --- | --- | --- |
| `collect_codex_sessions.py` | `--codex-home`, `--session-state-dir`, `--snapshot-file`; `--credential-file` when serving | Compatibility entry point for existing Codex commands. Defaults: `--host 127.0.0.1`, `--port 9464`, `--poll-seconds 5` (1–60). `--once` performs one bounded scan and exits without a listener. |

### Optional features

| Script | Required arguments | Behavior and defaults |
| --- | --- | --- |
| `collect_codex_account.py` | `--codex-bin`, `--codex-home`, `--snapshot-file` | Makes upstream account reads about once per minute. `--once` performs one read and exits. |

| Option | Purpose |
| --- | --- |
| `--account-snapshot-file` | Adds the private numeric account snapshot. |
| `--cwo-sessions` | Associates supported skill blocks and direct helper commands with Codex sessions. |
| `--cwo-audit-dir`, `--cwo-audit-file` | Adds selected read-only CWO workflow logs; repeat for multiple absolute paths. |
| `--cwo-review-dir` | Reads paired artifacts supplied by a custom adapter; repeat for multiple absolute directories. |
| `--state-dir` | Adds an existing read-only controller dispatch ledger. |

Account and CWO options require Codex collection. Inputs must be separate from
collector output. `--once` includes health and counts for enabled optional inputs.
See [account allowance](../optional/account-allowance.md),
[CWO integration](../integrations/cwo.md) and the
[custom review adapter](../integrations/custom-review-adapter.md).

### Renderers

| Script | Dashboard |
| --- | --- |
| `render_codex_beta_dashboard.py` | Work Overview; optional `--labels-file` aliases. |
| `render_codex_sessions_dashboard.py` | All Sessions or Codex TUI · Beta, selected by `--template`. |
| `render_observability_dashboard.py` | CWO Overview; accepts `--session-snapshot-file` or `--presentation-file`. |

## IBM Bob

### Collection

Enable with `--bob-home` and `--bob-snapshot-file`. Source paths, snapshots and
`--session-state-dir` must be absolute, with collector output outside source homes.

### Optional features

`--bob-otel-journal-dir` reads the private sanitized generation journal and requires
`--bob-home`. Keep the journal separate from source homes, private state, snapshots
and credentials. See [token capture setup](../optional/bob-token-capture.md).

### Renderers

`render_bob_dashboard.py` renders IBM Bob · Beta from the private Bob snapshot.

## Dashboard Renderers

All renderers require `--template` and `--output`. Session renderers require
`--snapshot-file`. CWO Overview requires at least one of `--session-snapshot-file` or `--presentation-file`; readable project/session names use the former.
All accept `--datasource-uid` and `--watch-seconds` (1–60). Omitting the watch option
renders once; omitting the datasource retains the import picker.
See [dashboard identities](dashboards.md) for UIDs, templates and renderer names.

| Dashboard | Identity | Renderer | Release bundle |
| --- | --- | --- | --- |
| Work Overview | `cwo-codex-beta` | `render_codex_beta_dashboard.py` | `beta` |
| All Sessions | `cwo-supervisor-observability-v1` | `render_codex_sessions_dashboard.py` | `stable` |
| CWO Overview | `cwo-dispatch-observability-v1` | `render_observability_dashboard.py` | `dispatch` |
| Codex TUI · Beta | `traceonaut-codex-tui-beta` | `render_codex_sessions_dashboard.py` | `tui-beta` |
| IBM Bob · Beta | `traceonaut-ibm-bob-beta` | `render_bob_dashboard.py` | `bob-beta` |

## Dispatch and Release Tools

| Script | Required arguments | Behavior and defaults |
| --- | --- | --- |
| `export_dispatch_observability.py` | `--state-dir`; `--credential-file` when serving | Loopback `--host 127.0.0.1`, default `--port 9464`. Use **9465** alongside session collection. `--once` prints metrics; `--capacity-report` prints local counts. These modes are exclusive. |
| `export_terminal_observations.py` | `--state-dir`, `--output-dir` | Exports eligible completed jobs, then exits. |
| `build_release.py` | `--component`, `--output-dir` | Builds a content-hashed bundle. Components: `sessions`, `account`, `stable`, `beta`, `tui-beta`, `bob-beta`, `dispatch`. The `sessions` bundle supports all three source configurations. |
| `validate_repository.py` | None | Checks source assets and links. `--staged` checks the exact Git index before publication. |
| `run_observed_codex.py` | `--manifest`, `--authorization-file`, `--observability-config`, `--receipt-dir` | Deprecated source-checkout launcher, excluded from release bundles and unrelated to session collection. See [Controller Metrics](../integrations/controller-metrics.md). |
