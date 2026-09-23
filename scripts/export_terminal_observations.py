#!/usr/bin/env python3
"""Export drained terminal ledger observations to a private offline sink."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from traceonaut.observability_terminal_export import (
    export_terminal_observations,
)


def _absolute(path: Path) -> Path:
    return path.expanduser() if path.is_absolute() else Path.cwd() / path.expanduser()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state-dir",
        required=True,
        type=Path,
        help="existing owner-private observability ledger",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="owner-private directory for allowlisted terminal observations",
    )
    args = parser.parse_args(argv)
    try:
        result = export_terminal_observations(
            _absolute(args.state_dir), _absolute(args.output_dir)
        )
    except Exception:
        print(
            "Terminal observation export unavailable: verify private source and output state.",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
