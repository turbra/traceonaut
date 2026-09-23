# Traceonaut

Monitor local Codex sessions in Grafana: recorded token usage, session activity,
command outcomes, and collector health. Traceonaut reads existing session files.
It does not launch work or require changes to Codex telemetry settings.

## How it works

```text
Codex session files + local SQLite metadata (read-only)
  -> Python collector + private incremental index
  -> authenticated /metrics endpoint
  -> Prometheus scrapes and stores samples
  -> Grafana queries Prometheus

Collector's private name snapshot
  -> dashboard renderer
  -> Grafana dashboard definitions with readable names
```

The renderer supplies names and selectors, not metric samples. OpenTelemetry,
node_exporter, and Complex Work Orchestration (CWO) are not required. Runtime
helpers use Python's standard library. No alerts or notification services are
installed.

## Get started

Follow [Set up Traceonaut](references/deployment.md) to connect the collector,
Prometheus, and the Beta dashboard. You need Linux, Python, an existing
Prometheus/Grafana installation, and a readable local Codex profile.

By default, session metrics stop being exposed after **30 days of inactivity**,
with at most **1,000 sessions** exposed at once. This does not delete source files,
the private index, or stored Prometheus samples. Current totals cover exported
sessions. To view expired sessions, choose a past end time for which Prometheus
has samples.

## Guides

| Task | Guide |
| --- | --- |
| Set up, troubleshoot, upgrade, or roll back | [Setup](references/deployment.md) |
| Understand collected data, retention, and missing values | [Session collection](references/codex-all-sessions-observability.md) |
| Read health, commands, and recorded usage | [Beta dashboard](references/codex-beta-dashboard.md) |
| View sessions and agents together | [Unified dashboard](references/codex-unified-dashboard.md) |
| Add account-wide allowance | [Account collection](references/codex-beta-dashboard.md#account-allowance) |
| Observe dispatches owned by CWO | [Optional CWO integration](references/cwo-integration.md) |

Beta is the experimentation dashboard. Unified provides a combined overview.
Stable is an alternative session view; its setup is in the session guide.
Existing metric names and dashboard UIDs retain their `cwo` prefixes for
compatibility. They do not imply a dependency on CWO.

Recorded session totals are not interval spending or billing. Account allowance
is not per-session usage. Missing data is not zero. Selected model and effort
describe configuration, not proof of the model serving each response.

## Development

See [repository instructions](AGENTS.md) for compatibility constraints and test
commands. Beads is a local maintainer tracker, not a runtime requirement.

Licensed under [GPLv3](LICENSE).
