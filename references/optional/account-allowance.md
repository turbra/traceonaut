---
slug: /optional/account-allowance
title: Account Allowance
description: Enable the optional account reader for weekly allowance and reset credits.
---

# Account Allowance

The account strip shows available earned resets, remaining weekly allowance, scheduled reset and read freshness. It appears in Work Overview, Unified and All Sessions, independently of their session filters.

This collector makes upstream account requests using an existing Codex login. It reads the account signed into the selected profile. History begins when collection is enabled.

## Start the Reader

From the checkout root, choose your profile and absolute Codex binary path:

```bash
TRACEONAUT_DATA_DIR="$HOME/.local/share/traceonaut"
TRACEONAUT_SOURCE_HOME="$HOME/.codex"
TRACEONAUT_CODEX_BINARY="/absolute/path/to/codex"
install -d -m 700 "$TRACEONAUT_DATA_DIR/account-state"
python3 scripts/collect_codex_account.py \
  --codex-bin "$TRACEONAUT_CODEX_BINARY" \
  --codex-home "$TRACEONAUT_SOURCE_HOME" \
  --snapshot-file "$TRACEONAUT_DATA_DIR/account-state/allowance.json"
```

Add `--once` for a single read. For continuous use, polling runs about once per minute.

Restart the session collector with:

```bash
--account-snapshot-file "$TRACEONAUT_DATA_DIR/account-state/allowance.json"
```

Use the same code version for both collectors and keep the account snapshot separate from session state. The [service guide](../operations/run-as-a-service.md) applies to this reader too; it needs upstream network access.

## Read the Strip

A successful read is current for 150 seconds. A failed read clears numeric values; a stopped reader becomes stale. **Awaiting update** means a scheduled reset has passed and a fresh account response is needed.

Values are measured at the selected range end and apply to the signed-in account. They are allowance percentages and credits, not per-session token budgets. Switching logins changes the source of subsequent samples.

The reader requests account limits without starting model work or redeeming resets. Its protected snapshot contains numeric values and timestamps. Account identities and credentials stay out of the metrics.
