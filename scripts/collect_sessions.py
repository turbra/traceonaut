#!/usr/bin/env python3
"""Collect independently enabled Codex and IBM Bob sources on one endpoint."""

from collect_codex_sessions import main


if __name__ == '__main__':
    raise SystemExit(main(require_codex=False))
