"""Bounded, read-only projection of CWO's hash-stamped workflow audit JSONL."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

from .codex_session_telemetry import _open_source, _safe_path

RETENTION_SECONDS = 30 * 86400
EXPORT_CAP = 2000
MAX_FILES = 256
MAX_SCAN_BYTES = 32 * 1024 * 1024
MAX_LINE_BYTES = 256 * 1024
MAX_ENTRIES = 8192
MAX_DEPTH = 16
EVENT_TYPES = frozenset({
    "packet_built", "dispatch_prepared", "return_evaluated", "prompt_coached",
    "review_cli_started", "review_cli_finished", "astra_adjudication_received",
    "native_pool_rendered", "native_pool_status", "native_pool_interrupt_requested",
    "native_pool_terminal",
})
SKIP_REASONS = (
    "invalid_json", "unsupported_record", "invalid_hash", "invalid_timestamp",
    "future_timestamp", "oversized_line", "partial_line",
)
METRICS = {
    "cwo_audit_source_available": ((), "At least one configured audit file was read."),
    "cwo_audit_collection_complete": ((), "All configured audit sources were read without gaps or export limits."),
    "cwo_audit_scan_timestamp_seconds": ((), "Unix time of the latest completed audit scan attempt."),
    "cwo_audit_source_files": ((), "Distinct audit files successfully read in the latest scan."),
    "cwo_audit_source_errors": ((), "Source access failures in the latest scan."),
    "cwo_audit_limit_reached": ((), "A discovery, byte or event export limit was reached."),
    "cwo_audit_exported_events": ((), "Unique audit events currently exported within the retention window."),
    "cwo_audit_skipped_records": (("reason",), "Records omitted in the latest scan, by bounded reason."),
    "cwo_audit_event_timestamp_seconds": (("event_id", "event_type"), "Source Unix timestamp of one unique CWO audit event; not a job or token count."),
}


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _parse(content):
    events, skipped = [], Counter()
    # A writer can be between write and newline. Retry that line on the next scan.
    for line in content.splitlines(keepends=True):
        if not line.strip():
            continue
        if len(line) > MAX_LINE_BYTES:
            skipped["oversized_line"] += 1
            continue
        if not line.endswith(b"\n"):
            skipped["partial_line"] += 1
            continue
        try:
            record = json.loads(line, object_pairs_hook=_object,
                                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, UnicodeError, RecursionError):
            skipped["invalid_json"] += 1
            continue
        if (not isinstance(record, dict) or not isinstance(record.get("event_type"), str)
                or not record["event_type"] or "event_hash" not in record):
            skipped["unsupported_record"] += 1
            continue
        event_id = record.pop("event_hash")
        try:
            digest = hashlib.sha256(json.dumps(record, sort_keys=True, allow_nan=False).encode()).hexdigest()
        except (ValueError, UnicodeError, RecursionError):
            skipped["invalid_json"] += 1
            continue
        if not isinstance(event_id, str) or re.fullmatch(r"[0-9a-f]{64}", event_id) is None or digest != event_id:
            skipped["invalid_hash"] += 1
            continue
        try:
            date = datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))
            if date.utcoffset() is None:
                raise ValueError("missing timezone")
            timestamp = date.timestamp()
            if timestamp <= 0:
                raise ValueError("nonpositive timestamp")
        except (KeyError, TypeError, AttributeError, ValueError, OverflowError, OSError):
            skipped["invalid_timestamp"] += 1
            continue
        kind = record["event_type"] if record["event_type"] in EVENT_TYPES else "other"
        events.append((event_id, kind, timestamp))
    return events, skipped


class AuditCollector:
    """Rebuild current gauges from bounded files; replay needs no persistent ledger.

    The cache retains only digests and numeric/allowlisted projections. Reading and
    hashing every scan detects same-size/same-mtime rewrites, rotation and truncation.
    """

    def __init__(self, *, files=(), directories=()):
        paths = [Path(p) for p in (*files, *directories)]
        if not paths or len(paths) > MAX_FILES or any(not p.is_absolute() for p in paths):
            raise ValueError("audit inputs must be 1 to 256 absolute file or directory paths")
        self.files = tuple(dict.fromkeys(Path(os.path.abspath(p)) for p in files))
        self.directories = tuple(dict.fromkeys(Path(os.path.abspath(p)) for p in directories))
        self._cache = {}

    def _discover(self):
        files, errors, limited, visited = set(self.files), 0, False, set()
        pending = [(p, 0) for p in self.directories]
        entries = 0
        while pending:
            directory, depth = pending.pop()
            if directory in visited:
                continue
            visited.add(directory)
            try:
                _safe_path(directory, owner=True)
                if not stat.S_ISDIR(directory.lstat().st_mode):
                    raise ValueError("not a directory")
                with os.scandir(directory) as children:
                    for child in children:
                        entries += 1
                        if entries > MAX_ENTRIES:
                            return sorted(files)[:MAX_FILES], errors, True
                        path = Path(child.path)
                        if child.is_symlink():
                            # Never descend through a link or open a linked audit file.
                            if child.name.endswith("audit.jsonl"):
                                errors += 1
                            continue
                        if child.is_dir(follow_symlinks=False):
                            if depth < MAX_DEPTH:
                                pending.append((path, depth + 1))
                            else:
                                limited = True
                        elif child.name == "audit.jsonl" or child.name.endswith("-audit.jsonl"):
                            files.add(path)
                            if len(files) > MAX_FILES:
                                return sorted(files)[:MAX_FILES], errors, True
            except (OSError, ValueError):
                errors += 1
        return sorted(files), errors, limited

    def scan(self, *, now=None):
        now = time.time() if now is None else now
        files, errors, limited = self._discover()
        read, remaining, events, skipped, cache = 0, MAX_SCAN_BYTES, {}, Counter(), {}
        for path in files:
            try:
                with _open_source(path) as source:
                    before = os.fstat(source.fileno())
                    if before.st_size > remaining:
                        limited = True
                        continue
                    content = source.read(remaining + 1)
                    after = os.fstat(source.fileno())
                if len(content) > remaining:
                    limited = True
                    remaining = 0
                    continue
                remaining -= len(content)
                # A concurrent append is retried without publishing a torn snapshot.
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                        after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    errors += 1
                    continue
                digest = hashlib.sha256(content).digest()
                prior = self._cache.get(path)
                parsed, omissions = prior[1:] if prior and prior[0] == digest else _parse(content)
                cache[path] = (digest, parsed, omissions)
                read += 1
                skipped.update(omissions)
                for event_id, kind, timestamp in parsed:
                    if timestamp > now:
                        skipped["future_timestamp"] += 1
                    elif timestamp >= now - RETENTION_SECONDS:
                        events[event_id] = (event_id, kind, timestamp)
            except (OSError, ValueError):
                errors += 1
        self._cache = cache
        ordered = sorted(events.values(), key=lambda event: (event[2], event[0]), reverse=True)
        limited = limited or len(ordered) > EXPORT_CAP
        return {
            "source_available": int(read > 0), "collection_complete": int(read > 0 and not errors and not limited and not any(skipped.values())),
            "scan_timestamp_seconds": now, "source_files": read, "source_errors": errors,
            "limit_reached": int(limited), "events": ordered[:EXPORT_CAP], "skipped_records": dict(skipped),
        }


def render_audit_metrics(snapshot):
    lines = []
    for name, (_, help_text) in METRICS.items():
        lines.extend((f"# HELP {name} {help_text}", f"# TYPE {name} gauge"))
        key = name.removeprefix("cwo_audit_")
        if key == "event_timestamp_seconds":
            lines.extend(f'{name}{{event_id="{identity}",event_type="{kind}"}} {timestamp}'
                         for identity, kind, timestamp in snapshot["events"])
        elif key == "skipped_records":
            lines.extend(f'{name}{{reason="{reason}"}} {snapshot[key].get(reason, 0)}' for reason in SKIP_REASONS)
        else:
            value = len(snapshot["events"]) if key == "exported_events" else snapshot[key]
            lines.append(f"{name} {value}")
    return ("\n".join(lines) + "\n").encode()
