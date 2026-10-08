---
slug: /sources/ibm-bob
title: IBM Bob
description: Collect saved Bob chats and add optional token capture.
---

# IBM Bob

## What it reads

Traceonaut reads `db/bob.db` under your Bob Shell profile. It collects chat and
project names, saved token counts, responses, tool results and message activity.
Database collection needs no Bob API credentials and keeps the source records unchanged.
Saved collection is tested with Bob Shell **2.0.1 and 2.0.5** on Linux.

## Enable

In [Quick Start](../getting-started.mdx#1-run-the-collector), set
`TRACEONAUT_BOB_HOME` to your profile and choose **IBM Bob** or **Both**.
Import [IBM Bob · Beta](../dashboards/ibm-bob-beta.md).

## Check

Open Prometheus and run:

```promql
traceonaut_bob_collector_source_available
```

Expect `1`. The Quick Start tab also provides a one-pass source check.
Use [Collector Health](../reference/collector-health.md#ibm-bob) if collection fails.

## Optional features

[Token Capture](../optional/bob-token-capture.md) supplies generation counts through
your existing OTel Collector. Chat names, responses and tool activity remain useful
with database collection alone.

## Limits

Bob 2.0.5 can omit token counts from newly saved chats. The dashboard labels these
values **Not recorded by Bob**. See [Retention and Limits](../reference/retention-and-limits.md#ibm-bob)
and [Data Sources and Privacy](../reference/data-sources-and-privacy.md#ibm-bob).

## Disable

Stop the collector, remove `--bob-home`, `--bob-snapshot-file` and
`--bob-otel-journal-dir` if used, then restart with the remaining source.
With no sources remaining, leave the collector stopped.
If token capture is enabled, follow its [rollback](../optional/bob-token-capture.md#rollback).
Private collector state and Prometheus history remain available.
