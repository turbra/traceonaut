---
slug: /operations/troubleshooting
title: Troubleshooting
description: Check endpoint readiness and diagnose missing metrics or names.
---

# Troubleshooting

From the checkout root, use the token created during setup. Change the path if
you stored it elsewhere:

<!-- metrics-check -->
```bash
export TRACEONAUT_METRICS_CREDENTIAL="$HOME/.local/share/traceonaut/metrics.token"
python3 scripts/check_metrics.py --url http://127.0.0.1:9464/metrics --credential-file "$TRACEONAUT_METRICS_CREDENTIAL"
```

For a LAN or VPN listener, replace `127.0.0.1` with its bind address.
This reports HTTP status and the presence of collector health metrics while keeping the token and payload private.

| Symptom | Check |
| --- | --- |
| Collector exits | Configuration, credential and state permissions; one writer; snapshot write access; and an assigned numeric bind address. The service exits nonzero for fatal setup or output failures. |
| Target DOWN | Workstation awake, network route, firewall, listener port and Prometheus token-file access. |
| HTTP 401 | Token copies match and are readable by the correct users. |
| HTTP 200 with source availability `0` | The endpoint is running. The first scan may still be pending, or the source read failed; check its path and permissions and [Collector Health](../reference/collector-health.md). |
| UP but empty data | [Collector Health](../reference/collector-health.md), selected filters and [retention](../reference/retention-and-limits.md). |
| Bob source unavailable | Check access to the configured Bob home and `db/bob.db`, then read [Bob health](../reference/collector-health.md#ibm-bob). Codex collection can continue. |
| Bob chats missing or partial | Check Bob pending work, skipped records and export limits in [Bob health](../reference/collector-health.md#ibm-bob), then the dashboard filters and [retention](../reference/retention-and-limits.md#ibm-bob). |
| Missing names | [Automatic Name Updates](automatic-name-updates.md). |
| Grafana has no data | Imported datasource selection and Prometheus queries. |
| CWO data appears only in older ranges | Enable [`--cwo-sessions`](../integrations/cwo.md#collect-cwo-sessions) for CWO-associated sessions across your Codex profile. Check pending files and source gaps. Audit logs are a separate optional source. |
| Token counts still show M or G | Re-render and import the current dashboard template. If a watcher provisions it, update the [watcher's template or release](automatic-name-updates.md) too. |
| Account values unavailable | [Account Allowance](../optional/account-allowance.md) collector, login and freshness. |

A successful HTTP check confirms endpoint readiness. Source availability and scan age confirm collection health.
