---
slug: /operations/run-as-a-service
title: Run as a Service
description: Run the collector persistently with your existing service manager.
---

# Run as a Service

Use the collector command from [Quick Start](../getting-started.mdx#1-run-the-collector)
or [IBM Bob Collection](../bob-collection.md#run-the-collector). Run it as the user
who owns the source profiles, with absolute paths and one collector per state directory.

For a Linux user service, save this as `~/.config/systemd/user/traceonaut.service`.
Replace `/absolute/path/to/traceonaut` with your checkout path in `WorkingDirectory`
and your chosen `ExecStart` line.
This unit collects Codex only. `%h` expands to the service user's home directory;
adjust the profile and storage paths if you used different locations during setup.
Create the metrics token using the collection guide before starting the service.

<!-- codex-only-service -->
```ini
[Unit]
Description=Traceonaut session collector

[Service]
Type=simple
WorkingDirectory=/absolute/path/to/traceonaut
ExecStart=/usr/bin/python3 /absolute/path/to/traceonaut/scripts/collect_sessions.py --codex-home %h/.codex --session-state-dir %h/.local/share/traceonaut/session-state --snapshot-file %h/.local/share/traceonaut/sessions.json --credential-file %h/.local/share/traceonaut/metrics.token
Restart=on-failure
RestartSec=5
UMask=0077

[Install]
WantedBy=default.target
```

For **Bob only**, replace the unit's entire `ExecStart` line with:

<!-- bob-only-service -->
```ini
ExecStart=/usr/bin/python3 /absolute/path/to/traceonaut/scripts/collect_sessions.py --bob-home %h/.bob --session-state-dir %h/.local/share/traceonaut/session-state --bob-snapshot-file %h/.local/share/traceonaut/bob.json --credential-file %h/.local/share/traceonaut/metrics.token
```

For **Bob and Codex**, replace it with:

<!-- both-sources-service -->
```ini
ExecStart=/usr/bin/python3 /absolute/path/to/traceonaut/scripts/collect_sessions.py --codex-home %h/.codex --bob-home %h/.bob --session-state-dir %h/.local/share/traceonaut/session-state --snapshot-file %h/.local/share/traceonaut/sessions.json --bob-snapshot-file %h/.local/share/traceonaut/bob.json --credential-file %h/.local/share/traceonaut/metrics.token
```

Keep exactly one `ExecStart` line in the unit. Keep any existing Codex account or CWO
options when enabling both sources.

Stop the foreground collector before enabling the service:

```bash
systemctl --user daemon-reload
systemctl --user enable --now traceonaut.service
systemctl --user status traceonaut.service
```

To start the user service at boot and keep it running after logout, enable lingering:

```bash
loginctl enable-linger "$USER"
```

This optional setting applies to your user services. Your system may request administrator authentication.

For remote Prometheus, append `--host <workstation-LAN-or-VPN-IP>` to `ExecStart`; see [Network and Security](network-and-security.md).

A sleeping or disconnected workstation stops delivering samples. Stored history remains available; missed scrape intervals remain gaps.
