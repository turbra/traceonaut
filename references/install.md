---
slug: /install
title: Install
description: Get the collector and check the supported environment.
---

# Install

Run Traceonaut on the workstation that holds your Codex profile. Use your existing Prometheus and Grafana.

## Requirements

| Requirement | Tested with |
| --- | --- |
| Linux workstation | Linux. macOS has not been tested. |
| Python | 3.13.13 |
| Codex session files | Fixtures for the [supported record formats](reference/data-sources-and-privacy.md). Compatibility is checked by record format rather than CLI version. |
| Prometheus | 3.1.0 |
| Grafana with built-in panels | 11.5.0 |
| Git and Bash | Used by the setup commands. |

Use the tested versions above; older versions have not been tested. The optional [Observed Job Runner](integrations/observed-job-runner.md) requires an exact Codex CLI version.

## Get the Code

```bash
git clone https://github.com/turbra/traceonaut.git
cd traceonaut
python3 scripts/collect_codex_sessions.py --help
```

The collector uses Python's standard library. Keep this checkout and run the documented commands from its root. Node.js is needed only to build this documentation site.

Continue with [Quick Start](getting-started.mdx).
