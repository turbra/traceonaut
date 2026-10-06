---
slug: /sources/codex
title: Codex
description: Collect saved Codex sessions and add optional account or CWO data.
---

# Codex

## What it reads

Traceonaut reads `sessions/`, `archived_sessions/` and selected metadata from
`state_*.sqlite` under your Codex profile. It collects session and project names, recorded tokens, completed
commands, compactions and activity. The source files stay unchanged.

## Enable

In [Quick Start](../getting-started.mdx#1-run-the-collector), set
`TRACEONAUT_SOURCE_HOME` to your profile and choose **Codex** or **Both**.
Import [Work Overview](../dashboards/work-overview.md).

## Check

Open Prometheus and run:

```promql
cwo_codex_collector_source_available
```

Expect `1`. The Quick Start tab also provides a one-pass source check.
Use [Collector Health](../reference/collector-health.md#codex) if collection fails.

## Optional features

- [Account Allowance](../optional/account-allowance.md) adds weekly allowance and reset information from your Codex login.
- [CWO Integration](../integrations/cwo.md) adds CWO session association, workflow activity and paired review results.

## Limits

Only saved records can be collected. Live activity becomes visible as Codex writes
it. Recorded token totals cover the selected sessions' saved history.
See [Retention and Limits](../reference/retention-and-limits.md#codex) and
[Data Sources and Privacy](../reference/data-sources-and-privacy.md#codex).

## Disable

Stop the collector, remove `--codex-home`, `--snapshot-file` and any Codex optional
arguments, then restart with the remaining source. Stop the account reader too
if enabled. With no sources remaining, leave the collector stopped.
Private collector state and Prometheus history remain available.
