#!/usr/bin/env python3
"""Serve committed local dispatch observations without attaching to workers."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

from traceonaut.observability_exporter import (
    MetricsEndpoint,
    build_samples,
    read_credential,
    render_prometheus,
)
from traceonaut.observability_ledger import ObservabilityLedger


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state-dir",
        type=Path,
        required=True,
        help="existing private observation ledger, never a Codex session directory",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--once", action="store_true", help="print one committed metrics snapshot"
    )
    mode.add_argument(
        "--capacity-report",
        action="store_true",
        help="print measured local series and retained-row counts, not annual acceptance",
    )
    parser.add_argument(
        "--credential-file", type=Path, help="owner-only bearer credential file"
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="numeric loopback address only"
    )
    parser.add_argument("--port", type=int, default=9464)
    args = parser.parse_args(argv)
    if not args.once and not args.capacity_report and args.credential_file is None:
        parser.error("--credential-file is required for the HTTP endpoint")
    endpoint = None
    try:
        with ObservabilityLedger(args.state_dir, readonly=True) as ledger:
            snapshot = ledger.snapshot()
            samples, _ = build_samples(snapshot)
            if args.once:
                sys.stdout.buffer.write(render_prometheus(samples))
                return 0
            if args.capacity_report:
                dispatches = [
                    row
                    for project in snapshot["projects"]
                    for row in project["dispatches"]
                ]
                print(
                    json.dumps(
                        {
                            "scope": "local ledger only; annual Prometheus capacity unqualified",
                            "registered_projects": len(snapshot["projects"]),
                            "retained_dispatches": len(dispatches),
                            "retained_cycles": sum(
                                len(row["cycles"]) for row in dispatches
                            ),
                            "exposed_series": len(samples),
                            "ledger_bytes": max(
                                (
                                    p["health"]["ledger_bytes"]
                                    for p in snapshot["projects"]
                                ),
                                default=0,
                            ),
                        },
                        indent=2,
                    )
                )
                return 0
            endpoint = MetricsEndpoint(
                args.host, args.port, read_credential(args.credential_file)
            )
            endpoint.update(snapshot)
            endpoint.start()
            print(
                "Serving committed telemetry on a protected loopback endpoint.",
                file=sys.stderr,
            )
            while True:
                time.sleep(1)
                try:
                    endpoint.update(ledger.snapshot())
                except Exception:
                    endpoint.invalidate()
    except KeyboardInterrupt:
        return 0
    except Exception:
        # Local state paths, credentials, raw source records, and exception
        # details must not enter service logs.
        print(
            "Telemetry unavailable: verify private state, credentials, and loopback configuration.",
            file=sys.stderr,
        )
        return 1
    finally:
        if endpoint is not None:
            endpoint.close()


if __name__ == "__main__":
    raise SystemExit(main())
