---
slug: /operations/network-and-security
title: Network and Security
description: Connect Prometheus to the workstation and protect the scrape credential.
---

# Network and Security

Prometheus pulls `/metrics` from the workstation. Bind the session collector to `127.0.0.1` for same-machine use, or a specific numeric LAN/VPN address for remote use. The scrape target may use a DNS name.

:::warning HTTP and remote access
Bearer authentication leaves HTTP traffic unencrypted. Use a trusted private LAN or encrypted VPN. For other networks, use an existing HTTPS proxy with a verified certificate and set Prometheus `scheme: https`. Restrict the workstation firewall to the Prometheus server.
:::

A container needs a route to the workstation and a read-only mount of the token. Its loopback is separate unless it uses host networking.

## Credentials and Private Files

- Token: owned by the collector user, mode `0600`, regular file.
- State and snapshot directories: mode `0700`, outside the Codex profile.
- Paths: trusted owners, with symlinks and writable ancestor directories rejected.
- Prometheus: its own protected copy of the token, readable by its service user.
- Grafana: access to Prometheus and rendered dashboard JSON; the collector's private files stay on the workstation.

Create tokens with `scripts/create_metrics_token.py`. Rotate all copies together, restart the collector, and reload Prometheus. Rendered dashboards contain display names; protect those files too.

The optional standalone CWO endpoint remains loopback-only. Its [integration guide](../integrations/cwo.md) explains the shared-endpoint alternative.

See [Limits and Internals](../reference/limits-and-internals.md) for exact credential and bind validation rules.
