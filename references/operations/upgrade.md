---
slug: /operations/upgrade
title: Upgrade
description: Update a checkout or immutable bundle while preserving collected history.
---

# Upgrade

## Plain Checkout

Save the current commit and stop the collector. From a clean checkout:

```bash
git rev-parse HEAD
git pull --ff-only
```

Restart the collector with the same state, snapshot and credential paths. If using the example service:

```bash
systemctl --user restart traceonaut.service
```

Check [Collector Health](../reference/collector-health.md). Re-render and import dashboards to pick up dashboard changes. Dashboard-only updates leave collection running.

For rollback, use a separate checkout of the saved commit and restart with that code and the existing state. Preserve local edits and the session database.

## Pinned Bundles

After updating the checkout, build a separate bundle:

```bash
export TRACEONAUT_RELEASES_DIR="$HOME/.local/share/traceonaut/releases"
python3 scripts/build_release.py --component sessions --output-dir "$TRACEONAUT_RELEASES_DIR"
```

The command prints an immutable release path. Point the service at its `scripts/collect_codex_sessions.py`, restart, and check health. Keep the old release path for rollback.

Update session and account collectors together. The [Scripts Reference](../reference/scripts.md) lists other bundle components.
