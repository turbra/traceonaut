---
slug: /operations/troubleshooting
title: Troubleshooting
description: Check endpoint readiness and diagnose missing metrics or names.
---

# Troubleshooting

From the checkout root, use the credential path from [Quick Start](../getting-started.mdx):

<!-- metrics-check -->
```bash
python3 scripts/check_metrics.py --url http://127.0.0.1:9464/metrics --credential-file "$TRACEONAUT_METRICS_CREDENTIAL"
```

This reports HTTP status and the presence of collector health metrics while keeping the token and payload private.

| Symptom | Check |
| --- | --- |
| Collector exits | Source/state permissions, one writer, and an assigned numeric bind address. |
| Target DOWN | Workstation awake, network route, firewall, listener port and Prometheus token-file access. |
| HTTP 401 | Token copies match and are readable by the correct users. |
| HTTP 503 | First scan is still running; persistent failures need source/state checks. |
| UP but empty data | [Collector Health](../reference/collector-health.md), selected filters and [retention](../reference/retention-and-limits.md). |
| Missing names | [Automatic Name Updates](automatic-name-updates.md). |
| Grafana has no data | Imported datasource selection and Prometheus queries. |
| CWO data appears only in older ranges | [CWO Dispatches](../dashboards/cwo.md#use-the-dashboard): the range must contain stored dispatch samples, and new jobs need an observation-enabled launch. |
| Token counts still show M or G | Re-render and import the current dashboard template. If a watcher provisions it, update the [watcher's template or release](automatic-name-updates.md) too. |
| Account values unavailable | [Account Allowance](../optional/account-allowance.md) collector, login and freshness. |

A successful HTTP check confirms endpoint readiness. Source availability and scan age confirm collection health.
