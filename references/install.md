---
slug: /install
title: Install
description: Get the collector and check the supported environment.
---

# Install

Run Traceonaut on the workstation that holds your Codex profile. Use your existing Prometheus and Grafana.

## Requirements

| Component | Supported baseline | Tested / qualified |
| --- | --- | --- |
| Operating system | Linux | Linux; macOS is untested. |
| Python | 3.13 | Python 3.13.13. Earlier versions are unqualified. |
| Codex session files | [Supported record formats](reference/data-sources-and-privacy.md) | Parser fixtures cover those formats; no CLI-release compatibility matrix is claimed. |
| Prometheus | 3.1 | Prometheus 3.1.0 query tests. |
| Grafana | 11.5 | Grafana 11.5.0, using built-in panels. |
| Git and Bash | Available on the workstation | Used by the commands below. |

These are supported baselines, not claims that older releases cannot work. The optional [observed-job runner](integrations/observed-job-runner.md) has its own exact Codex version requirement.

## Get the Code

```bash
git clone https://github.com/turbra/traceonaut.git
cd traceonaut
python3 scripts/collect_codex_sessions.py --help
```

The collector uses Python's standard library. Keep this checkout and run the documented commands from its root. Node.js is needed only to build this documentation site.

Continue with [Quick Start](getting-started.mdx).
