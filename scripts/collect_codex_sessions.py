#!/usr/bin/env python3
"""Continuously collect local Codex session metadata into private accounting."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import threading

from traceonaut.codex_session_telemetry import (
    DEFAULT_SESSION_EXPORT_CAP, MAX_SESSIONS, SAFE_INTEGER,
    SESSION_RETENTION_SECONDS, SessionCollector, render_session_metrics, write_snapshot,
)
from traceonaut.codex_account_telemetry import AccountSnapshotMetrics
from traceonaut.observability_exporter import (
    MetricsEndpoint, build_samples, metrics_bind_address, read_credential, render_prometheus,
)
from traceonaut.observability_ledger import ObservabilityLedger


class SessionMetricsEndpoint(MetricsEndpoint):
    def update_sessions(self, snapshot, legacy=None, account=None):
        payload = render_session_metrics(snapshot)
        if legacy is not None:
            payload += render_prometheus(build_samples(legacy)[0])
        if account is not None:
            payload += account.render_metrics()
        with self._lock:
            self._payload = payload


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", type=Path, required=True)
    parser.add_argument("--session-state-dir", type=Path, required=True)
    parser.add_argument("--snapshot-file", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, help="optional existing owned-dispatch ledger, read-only")
    parser.add_argument("--credential-file", type=Path)
    parser.add_argument("--host", default="127.0.0.1",
                        help="numeric bind IP (default: 127.0.0.1); use a specific LAN/VPN IP for remote scraping; HTTP only")
    parser.add_argument("--port", type=int, default=9464)
    parser.add_argument("--poll-seconds", type=float, default=5)
    parser.add_argument("--session-retention-seconds", type=int, default=SESSION_RETENTION_SECONDS,
                        help="per-session exposition inactivity window; 0 disables age expiry")
    parser.add_argument("--session-export-cap", type=int, default=DEFAULT_SESSION_EXPORT_CAP,
                        help="maximum exposed session groups; indexed history is not deleted")
    parser.add_argument("--account-snapshot-file", type=Path,
                        help="optional private numeric snapshot from collect_codex_account.py")
    parser.add_argument("--once", action="store_true", help="collect one bounded pass without a listener")
    args = parser.parse_args(argv)
    if not 1 <= args.poll_seconds <= 60:
        parser.error("poll interval must be between 1 and 60 seconds")
    if not 0 <= args.session_retention_seconds <= SAFE_INTEGER:
        parser.error("session retention must be a nonnegative safe integer")
    if not 1 <= args.session_export_cap <= MAX_SESSIONS:
        parser.error("session export cap must be between 1 and 100000")
    if not args.once and args.credential_file is None:
        parser.error("credential file required when serving metrics")
    try:
        metrics_bind_address(args.host, allow_remote=True)
    except ValueError as error:
        parser.error(str(error))
    if any(not p.is_absolute() for p in (args.codex_home, args.session_state_dir, args.snapshot_file)):
        parser.error("source, state, and snapshot paths must be absolute")
    source = Path(os.path.abspath(args.codex_home))
    snapshot = Path(os.path.abspath(args.snapshot_file))
    state = Path(os.path.abspath(args.session_state_dir))
    if snapshot.is_relative_to(source) or state.is_relative_to(source):
        parser.error("collector output must be outside the Codex source home")
    if snapshot.parent == state and snapshot.name in ("sessions.sqlite3", "sessions.sqlite3-wal", "sessions.sqlite3-shm", "writer.lock"):
        parser.error("snapshot must not replace collector state")
    if args.account_snapshot_file:
        account_path = Path(os.path.abspath(args.account_snapshot_file))
        if (not args.account_snapshot_file.is_absolute() or account_path == snapshot
                or account_path.is_relative_to(source) or account_path.is_relative_to(state)):
            parser.error("account snapshot must be separate from Codex source and session state")
    os.umask(0o077)
    stopping = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopping.set())
    collector = endpoint = ledger = account = None
    try:
        collector = SessionCollector(
            args.codex_home, args.session_state_dir,
            session_retention_seconds=args.session_retention_seconds,
            session_export_cap=args.session_export_cap,
        )
        if args.state_dir:
            ledger = ObservabilityLedger(args.state_dir, readonly=True)
        if not args.once:
            endpoint = SessionMetricsEndpoint(
                args.host, args.port, read_credential(args.credential_file), allow_remote=True,
            )
            endpoint.start()
        if args.account_snapshot_file:
            account = AccountSnapshotMetrics(args.account_snapshot_file)
        while not stopping.is_set():
            try:
                collector.scan()
            except (OSError, ValueError):
                collector.available = False
            snapshot = collector.snapshot()
            write_snapshot(args.snapshot_file, snapshot)
            if endpoint:
                endpoint.update_sessions(snapshot, ledger.snapshot() if ledger else None, account)
            if args.once:
                print(json.dumps({"sessions": len(snapshot["sessions"]), "pending_files": snapshot["pending_files"], "source_available": snapshot["source_available"]}))
                break
            stopping.wait(args.poll_seconds)
        return 0
    except Exception:
        # No paths, messages, raw source lines, credential contents, or traceback.
        print("Session telemetry unavailable; inspect source access, private state, and listener configuration.",
              file=__import__("sys").stderr)
        return 1
    finally:
        if endpoint:
            endpoint.close()
        if ledger:
            ledger.close()
        if collector:
            collector.close()


if __name__ == "__main__":
    raise SystemExit(main())
