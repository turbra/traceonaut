#!/usr/bin/env python3
"""Continuously collect local Codex session metadata into private accounting."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import threading
import time

from traceonaut.codex_session_telemetry import (
    DEFAULT_SESSION_EXPORT_CAP, MAX_SESSIONS, SAFE_INTEGER,
    SESSION_RETENTION_SECONDS, SessionCollector, SessionSourceUnavailable,
    render_session_metrics, write_snapshot,
)
from traceonaut.codex_account_telemetry import AccountSnapshotMetrics
from traceonaut.cwo_audit_telemetry import AuditCollector, render_audit_metrics
from traceonaut.cwo_review_telemetry import ReviewCollector, render_review_metrics
from traceonaut.cwo_review_provenance import ProvenanceIndex
from traceonaut.cwo_session_telemetry import CwoSessionCollector, render_cwo_session_metrics
from traceonaut.observability_exporter import (
    MetricsEndpoint, build_samples, metrics_bind_address, read_credential, render_prometheus,
)
from traceonaut.observability_ledger import ObservabilityLedger
from traceonaut.bob_session_telemetry import BobCollector, empty_snapshot, render_bob_metrics
from traceonaut.collector_paths import validate_outputs


class SessionMetricsEndpoint(MetricsEndpoint):
    def update_part(self, source, payload):
        with self._lock:
            if not hasattr(self, '_parts'):
                self._parts = {}
            self._parts[source] = payload
            self._payload = b''.join(self._parts[key] for key in sorted(self._parts))

    def update_sessions(self, snapshot, legacy=None, account=None, audit=None, reviews=None):
        self.update_part('codex', codex_payload(snapshot, legacy, account, audit, reviews))


def codex_payload(snapshot, legacy=None, account=None, audit=None, reviews=None):
    payload = render_session_metrics(snapshot)
    if legacy is not None:
        payload += render_prometheus(build_samples(legacy)[0])
    if account is not None:
        payload += account.render_metrics()
    if audit is not None:
        payload += render_audit_metrics(audit)
    if reviews is not None:
        payload += render_review_metrics(reviews)
    if "cwo_sessions" in snapshot:
        payload += render_cwo_session_metrics(snapshot["cwo_sessions"])
    return payload


class CodexSource:
    """Existing Codex collection and optional attachments, owned by one thread."""

    def __init__(self, args, audit, reviews):
        self.args, self.audit, self.reviews = args, audit, reviews
        self.collector = self.cwo = self.ledger = self.account = self.provenance = None
        try:
            self.collector = SessionCollector(args.codex_home, args.session_state_dir,
                session_retention_seconds=args.session_retention_seconds, session_export_cap=args.session_export_cap)
            if args.cwo_sessions:
                self.cwo = CwoSessionCollector(args.codex_home, args.session_state_dir)
                if reviews:
                    self.provenance = ProvenanceIndex(self.cwo)
            if args.state_dir:
                self.ledger = ObservabilityLedger(args.state_dir, readonly=True)
            if args.account_snapshot_file:
                self.account = AccountSnapshotMetrics(args.account_snapshot_file)
        except Exception:
            self.close()
            raise

    def close(self):
        for resource in (self.ledger, self.cwo, self.collector):
            if resource:
                resource.close()

    def scan(self):
        try:
            self.collector.scan()
        except SessionSourceUnavailable:
            self.collector.available = False
        snapshot = self.collector.snapshot()
        if self.cwo:
            snapshot['cwo_sessions'] = self.cwo.scan(self.collector.db, snapshot)
        audit = self.audit.scan() if self.audit else None
        reviews = self.reviews.scan() if self.reviews else None
        if self.provenance:
            reviews['provenance'] = self.provenance.scan(self.collector.db, snapshot, reviews,
                                                        now=reviews['scan_timestamp_seconds'])
        status = {"sessions": len(snapshot["sessions"]), "pending_files": snapshot["pending_files"],
                  "source_available": snapshot["source_available"]}
        if self.cwo:
            status['cwo_sessions'] = {k: snapshot['cwo_sessions'][k] for k in
                ('scan_ready', 'pending_files', 'source_errors', 'source_gaps')}
            status['cwo_sessions']['associated_sessions'] = len(snapshot['cwo_sessions']['associations'])
        if audit is not None:
            status['cwo_audit'] = {k: audit[k] for k in ('source_available', 'collection_complete', 'source_files', 'source_errors', 'limit_reached')}
            status['cwo_audit']['exported_events'] = len(audit['events'])
        if reviews is not None:
            status['cwo_reviews'] = {k: reviews[k] for k in ('source_available', 'collection_complete', 'source_files', 'source_errors', 'pending_results', 'limit_reached', 'skipped_records')}
            status['cwo_reviews']['exported_reviews'] = len(reviews['reviews'])
            if self.provenance:
                status['cwo_reviews']['provenance'] = reviews['provenance']
                status['cwo_reviews']['attribution'] = {state: sum(r['attribution'] == state for r in reviews['reviews'])
                    for state in ('linked', 'unlinked', 'ambiguous', 'pending')}
        payload = codex_payload(snapshot, self.ledger.snapshot() if self.ledger else None,
                                self.account, audit, reviews)
        return snapshot, payload, status


class SourceWorker:
    """One thread and independent cache per enabled source; no scrape-path I/O."""

    def __init__(self, name, args, stopping, endpoint=None, audit=None, reviews=None):
        self.name, self.args, self.stopping, self.endpoint = name, args, stopping, endpoint
        self.audit, self.reviews = audit, reviews
        self.failed = False
        self.snapshot = (empty_snapshot() if name == 'bob' else {
            'version': 1, 'sessions': [], 'source_available': 0, 'scan_timestamp': 0,
            'last_event': 0, 'pending_files': 0, 'errors': {}})
        self.status = {'sessions': 0, 'source_available': 0, 'pending_files': 0}
        self.render = render_bob_metrics if name == 'bob' else render_session_metrics
        self.output = args.bob_snapshot_file if name == 'bob' else args.snapshot_file
        if endpoint:
            endpoint.update_part(name, self.render(self.snapshot))

    def run(self):
        source = None
        try:
            while not self.stopping.is_set():
                started = time.monotonic()
                try:
                    if source is None:
                        source = (BobCollector(self.args.bob_home, self.args.session_state_dir,
                            session_retention_seconds=self.args.session_retention_seconds,
                            session_export_cap=self.args.session_export_cap,
                            otel_journal_dir=getattr(self.args, 'bob_otel_journal_dir', None)) if self.name == 'bob' else
                            CodexSource(self.args, self.audit, self.reviews))
                    if self.name == 'bob':
                        snapshot = source.scan()
                        payload = self.render(snapshot)
                        status = {k: snapshot[k] for k in ('source_available', 'collection_complete', 'pending', 'limit_reached')}
                        status['sessions'] = len(snapshot['sessions'])
                    else:
                        snapshot, payload, status = source.scan()
                except SessionSourceUnavailable:
                    snapshot = {**self.snapshot, 'source_available': 0}
                    status = {**self.status, 'source_available': 0}
                    payload = self.render(snapshot)
                write_snapshot(self.output, snapshot)
                self.snapshot, self.status = snapshot, status
                if self.endpoint:
                    self.endpoint.update_part(self.name, payload)
                if self.args.once:
                    break
                if self.name == 'bob' and snapshot['source_available'] and snapshot['pending']:
                    # Finish the bounded read before waiting for the next poll.
                    continue
                self.stopping.wait(max(0, self.args.poll_seconds - (time.monotonic() - started)))
        except Exception:
            # Reader-reported outages are recoverable; storage and unexpected
            # failures must reach the process exit status without leaking data.
            self.failed = True
            self.snapshot = {**self.snapshot, 'source_available': 0}
            self.status = {**self.status, 'source_available': 0}
            self.stopping.set()
        finally:
            if source:
                try:
                    source.close()
                except Exception:
                    self.failed = True
                    self.stopping.set()


def main(argv=None, *, require_codex=True):
    parser = argparse.ArgumentParser(description=__doc__ if require_codex else
        "Collect local Codex sessions, IBM Bob chats, or both on one metrics endpoint.")
    parser.add_argument("--codex-home", type=Path, required=require_codex)
    parser.add_argument("--bob-home", type=Path, help="enable Bob collection from db/bob.db inside this home")
    parser.add_argument("--bob-snapshot-file", type=Path, help="private Bob presentation snapshot")
    parser.add_argument("--bob-otel-journal-dir", type=Path,
                        help="optional private sanitized Bob generation journal; requires --bob-home")
    parser.add_argument("--session-state-dir", type=Path, required=True)
    parser.add_argument("--snapshot-file", type=Path, required=require_codex)
    parser.add_argument("--state-dir", type=Path, help="optional directory containing an existing controller task database; read-only")
    parser.add_argument("--cwo-sessions", action="store_true",
                        help="associate CWO skill blocks and direct helper commands across the selected Codex profile")
    parser.add_argument("--cwo-audit-file", type=Path, action="append", default=[],
                        help="optional CWO audit JSONL file, read-only; repeat for multiple files")
    parser.add_argument("--cwo-audit-dir", type=Path, action="append", default=[],
                        help="optional audit directory; recursively read audit.jsonl and *-audit.jsonl files")
    parser.add_argument("--cwo-review-dir", type=Path, action="append", default=[],
                        help="optional review artifact directory; reads supported launch/result pairs or provenance bundles with matching CWO audits; repeat for multiple roots")
    parser.add_argument("--credential-file", type=Path)
    parser.add_argument("--host", default="127.0.0.1",
                        help="numeric bind IP (default: 127.0.0.1); use a specific LAN/VPN IP for remote scraping; HTTP only")
    parser.add_argument("--port", type=int, default=9464)
    parser.add_argument("--poll-seconds", type=float, default=5)
    parser.add_argument("--session-retention-seconds", type=int, default=SESSION_RETENTION_SECONDS,
                        help="stop exporting sessions after this many inactive seconds; 0 disables age expiry")
    parser.add_argument("--session-export-cap", type=int, default=DEFAULT_SESSION_EXPORT_CAP,
                        help="maximum exposed session groups; indexed history is not deleted")
    parser.add_argument("--account-snapshot-file", type=Path,
                        help="optional private numeric snapshot from collect_codex_account.py")
    parser.add_argument("--once", action="store_true", help="scan once without starting the metrics server")
    args = parser.parse_args(argv)
    if not args.codex_home and not args.bob_home:
        parser.error("enable at least one source with --codex-home or --bob-home")
    if bool(args.codex_home) != bool(args.snapshot_file):
        parser.error("--codex-home and --snapshot-file must be supplied together")
    if bool(args.bob_home) != bool(args.bob_snapshot_file):
        parser.error("--bob-home and --bob-snapshot-file must be supplied together")
    if args.bob_otel_journal_dir and not args.bob_home:
        parser.error('--bob-otel-journal-dir requires --bob-home')
    if not args.codex_home and any((args.cwo_sessions, args.cwo_audit_file, args.cwo_audit_dir,
                                   args.cwo_review_dir, args.account_snapshot_file, args.state_dir)):
        parser.error("CWO and account options require --codex-home")
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
    if any(not p.is_absolute() for p in (args.codex_home, args.bob_home, args.session_state_dir,
                                        args.snapshot_file, args.bob_snapshot_file, args.bob_otel_journal_dir) if p):
        parser.error("source, state, and snapshot paths must be absolute")
    source = Path(os.path.abspath(args.codex_home)) if args.codex_home else None
    snapshot = Path(os.path.abspath(args.snapshot_file)) if args.snapshot_file else None
    state = Path(os.path.abspath(args.session_state_dir))
    if source and (snapshot.is_relative_to(source) or state.is_relative_to(source)):
        parser.error("collector output must be outside the Codex source home")
    try:
        validate_outputs([p for p in (args.codex_home, args.bob_home) if p], state,
                         [p for p in (snapshot, args.bob_snapshot_file) if p])
        if args.credential_file and Path(os.path.abspath(args.credential_file)) in (snapshot, args.bob_snapshot_file):
            raise ValueError("snapshot must be separate from the metrics credential")
        if args.bob_otel_journal_dir:
            journal = args.bob_otel_journal_dir = Path(os.path.abspath(args.bob_otel_journal_dir))
            for path in (args.codex_home, args.bob_home, state, snapshot, args.bob_snapshot_file,
                         args.credential_file, args.state_dir, *args.cwo_audit_dir, *args.cwo_review_dir):
                normalized = Path(os.path.abspath(path)) if path else None
                if normalized and (journal.is_relative_to(normalized) or normalized.is_relative_to(journal)):
                    raise ValueError('Bob journal must be separate from sources, state, snapshots and credentials')
    except (OSError, ValueError) as error:
        parser.error(str(error))
    if args.account_snapshot_file:
        account_path = Path(os.path.abspath(args.account_snapshot_file))
        if (not args.account_snapshot_file.is_absolute() or account_path == snapshot
                or account_path.is_relative_to(source) or account_path.is_relative_to(state)
                or (args.bob_home and account_path.is_relative_to(args.bob_home))
                or account_path == args.bob_snapshot_file):
            parser.error("account snapshot must be separate from Codex source and session state")
    try:
        review_collector = ReviewCollector(directories=args.cwo_review_dir) if args.cwo_review_dir else None
        audit_collector = (AuditCollector(files=args.cwo_audit_file, directories=args.cwo_audit_dir)
                           if args.cwo_audit_file or args.cwo_audit_dir else None)
    except ValueError as error:
        parser.error(str(error))
    if audit_collector:
        for audit_file in audit_collector.files:
            if audit_file == snapshot or audit_file.is_relative_to(state):
                parser.error("audit inputs must be separate from collector output")
        for audit_dir in audit_collector.directories:
            if (snapshot.is_relative_to(audit_dir) or state.is_relative_to(audit_dir)
                    or audit_dir.is_relative_to(state)):
                parser.error("audit inputs must be separate from collector output")
    if review_collector:
        for directory in review_collector.directories:
            if snapshot.is_relative_to(directory) or state.is_relative_to(directory) or directory.is_relative_to(state):
                parser.error("review inputs must be separate from collector output")
    if args.bob_snapshot_file:
        for directory in [*args.cwo_audit_dir, *args.cwo_review_dir]:
            if args.bob_snapshot_file.is_relative_to(directory):
                parser.error("Bob snapshot must be separate from CWO inputs")
        if args.bob_snapshot_file in args.cwo_audit_file:
            parser.error("Bob snapshot must be separate from CWO inputs")
    os.umask(0o077)
    stopping = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopping.set())
    endpoint = None
    workers, threads = [], []
    try:
        if not args.once:
            endpoint = SessionMetricsEndpoint(
                args.host, args.port, read_credential(args.credential_file), allow_remote=True,
            )
        for name, enabled in (('codex', args.codex_home), ('bob', args.bob_home)):
            if enabled:
                workers.append(SourceWorker(name, args, stopping, endpoint, audit_collector, review_collector))
        if endpoint:
            endpoint.start()
        for worker in workers:
            if stopping.is_set():
                break
            thread = threading.Thread(target=worker.run, name='traceonaut-' + worker.name)
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join()
        if args.once:
            statuses = {worker.name: worker.status for worker in workers}
            print(json.dumps(statuses['codex'] if require_codex and not args.bob_home else statuses))
        if any(worker.failed for worker in workers):
            raise RuntimeError("collector worker failed")
        if args.once and any(not worker.status['source_available'] for worker in workers):
            print("Collection failed: one or more enabled sources are unavailable.", file=__import__("sys").stderr)
            return 1
        return 0
    except Exception:
        # No paths, messages, raw source lines, credential contents, or traceback.
        print("Session telemetry unavailable; inspect source access, private state, and listener configuration.",
              file=__import__("sys").stderr)
        return 1
    finally:
        stopping.set()
        for thread in threads:
            thread.join()
        if endpoint:
            endpoint.close()


if __name__ == "__main__":
    raise SystemExit(main())
