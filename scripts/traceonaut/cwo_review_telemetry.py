"""Opt-in reader for paired CWO launch receipts and Claude CLI JSON results.

This adapter reads an existing artifact layout, not the observed Codex ledger.
Launch time is explicit; a result's duration does not establish a finish timestamp.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import time

from .codex_session_telemetry import _open_source, _timestamp, _uuid
from .cwo_audit_telemetry import AuditCollector, _object, _parse, RETENTION_SECONDS

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_SCAN_BYTES = 32 * 1024 * 1024
EXPORT_CAP = 256
SKIP_REASONS = ("invalid_record", "unmatched_dispatch", "future_timestamp", "conflicting_result")
LABELS = ("review_id", "outcome", "requested_model", "reported_model", "effort")
KINDS = ("input", "cache_creation", "cache_read", "output", "thinking")
METRICS = {
    "cwo_review_source_available": ((), "At least one configured CLI launch receipt was read."),
    "cwo_review_collection_complete": ((), "Configured paired review artifacts read without errors, pending results or limits."),
    "cwo_review_scan_timestamp_seconds": ((), "Unix time of the latest CLI review scan attempt."),
    "cwo_review_source_files": ((), "CLI launch receipts read in the latest scan."),
    "cwo_review_source_errors": ((), "Review artifact access failures in the latest scan."),
    "cwo_review_pending_results": ((), "Launch receipts whose paired result file is absent."),
    "cwo_review_limit_reached": ((), "Review artifact discovery, byte or export cap reached."),
    "cwo_review_skipped_records": (("reason",), "Review artifacts omitted in the latest scan by bounded reason."),
    "cwo_review_started_timestamp_seconds": (LABELS, "Recorded launch time of a CLI review with a collected result; not its finish time."),
    "cwo_review_tokens": ((*LABELS, "kind"), "CLI top-level usage by kind; thinking is a subset of output. Input excludes cache creation and reads."),
    "cwo_review_duration_seconds": (LABELS, "CLI-reported result duration; not time inferred from file timestamps."),
}


def _integer(value):
    return type(value) is int and 0 <= value <= 2**53 - 1


def _label(value):
    # Model/effort metadata only; paths, prose and arbitrary result text stay local.
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+\[\]-]{0,127}", value) is None:
        raise ValueError("invalid metadata")
    return value


def _json(content):
    value = json.loads(content, object_pairs_hook=_object,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    if not isinstance(value, dict):
        raise ValueError("invalid object")
    return value


def project(launch, result, prepared, now):
    dispatch, packet = launch.get("dispatch_id"), launch.get("packet_sha256")
    if (not isinstance(dispatch, str) or not 0 < len(dispatch) <= 256
            or not isinstance(packet, str) or re.fullmatch(r"[0-9a-f]{64}", packet) is None):
        raise ValueError("invalid_record")
    at = _timestamp(launch.get("started_at"))
    # Require timezone-qualified source time, never filesystem mtime.
    if not isinstance(launch.get("started_at"), str) or not re.search(r"(?:Z|[+-]\d\d:\d\d)$", launch["started_at"]) or at is None or at <= 0:
        raise ValueError("invalid_record")
    if at > now:
        raise ValueError("future_timestamp")
    if (dispatch, packet) not in prepared or prepared[dispatch, packet] > at:
        raise ValueError("unmatched_dispatch")
    identity, session = _uuid(result.get("uuid")), _uuid(result.get("session_id"))
    if result.get("type") != "result" or type(result.get("is_error")) is not bool or not identity or not session:
        raise ValueError("invalid_record")
    usage = result.get("usage")
    if usage is not None and not isinstance(usage, dict):
        raise ValueError("invalid_record")
    tokens = {}
    for kind, key in zip(KINDS[:4], ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")):
        if usage is not None and key in usage:
            if not _integer(usage[key]):raise ValueError("invalid_record")
            tokens[kind] = usage[key]
    if sum(tokens.values()) > 2**53 - 1:
        raise ValueError("invalid_record")
    details = (usage or {}).get("output_tokens_details", {})
    if not isinstance(details, dict):raise ValueError("invalid_record")
    if "thinking_tokens" in details:
        if not _integer(details["thinking_tokens"]) or ("output" in tokens and details["thinking_tokens"] > tokens["output"]):
            raise ValueError("invalid_record")
        tokens["thinking"] = details["thinking_tokens"]
    duration = result.get("duration_ms")
    if duration is not None and not _integer(duration):raise ValueError("invalid_record")
    models = result.get("modelUsage", {})
    if not isinstance(models, dict):raise ValueError("invalid_record")
    reported = _label(next(iter(models))) if len(models) == 1 else "multiple" if models else "unknown"
    review_id = hashlib.sha256((session + ":" + identity).encode()).hexdigest()
    return {"review_id": review_id, "timestamp": at,
            "outcome": "failed" if result["is_error"] else "completed",
            "requested_model": _label(launch.get("requested_model")), "reported_model": reported,
            "effort": _label(launch.get("effort", "unknown")), "tokens": tokens,
            "duration": duration / 1000 if duration is not None else None,
            # Retained in the private numeric projection for conflict checks only.
            "binding": hashlib.sha256((dispatch + ":" + packet).encode()).hexdigest()}


class ReviewCollector(AuditCollector):
    def __init__(self, *, directories):
        super().__init__(directories=directories)

    def _candidate(self, name):
        return name.endswith("-launch-receipt.json")

    def scan(self, *, now=None):
        now = time.time() if now is None else now
        files, errors, limited = self._discover()
        read = pending = 0
        remaining = MAX_SCAN_BYTES
        reviews, conflicts, skipped = {}, set(), Counter()
        audits = {}

        def content(path):
            nonlocal remaining
            try:
                source = _open_source(path)
            except ValueError:
                raise OSError("unsafe source") from None
            with source as stream:
                before = os.fstat(stream.fileno())
                if before.st_size > min(MAX_FILE_BYTES, remaining):raise OverflowError()
                raw = stream.read(min(MAX_FILE_BYTES, remaining) + 1)
                after = os.fstat(stream.fileno())
            remaining -= len(raw)
            if len(raw) > MAX_FILE_BYTES or remaining < 0:raise OverflowError()
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise OSError("source changed")
            return raw

        for path in files:
            try:
                launch = _json(content(path)); read += 1
                raw_path = path.with_name(path.name.removesuffix("-launch-receipt.json") + "-response.raw.json")
                try:
                    result = _json(content(raw_path))
                except FileNotFoundError:
                    pending += 1;continue
                audit_path = path.parent / "audit.jsonl"
                if audit_path not in audits:
                    raw = content(audit_path)
                    events, omissions = _parse(raw)
                    if omissions:raise ValueError("invalid_record")
                    valid = {event[0]: event[2] for event in events if event[1] == "dispatch_prepared" and event[2] <= now}
                    prepared = {}
                    for line in raw.splitlines():
                        if not line.strip():continue
                        record = _json(line)
                        at = valid.get(record.get("event_hash"))
                        key = record.get("dispatch_id"), record.get("packet_sha256")
                        if at is not None and all(isinstance(v, str) for v in key):
                            prepared[key] = min(at, prepared.get(key, at))
                    audits[audit_path] = prepared
                row = project(launch, result, audits[audit_path], now)
                if row["timestamp"] < now - RETENTION_SECONDS:continue
                identity = row["review_id"]
                if identity in reviews and reviews[identity] != row:
                    conflicts.add(identity)
                else:
                    reviews[identity] = row
            except OverflowError:
                limited = True
            except (OSError, UnicodeError):
                errors += 1
            except (ValueError, TypeError, RecursionError) as error:
                # Bounded reasons only; never expose source exception strings.
                reason = str(error)
                skipped[reason if reason in SKIP_REASONS else "invalid_record"] += 1
        for identity in conflicts:reviews.pop(identity, None)
        skipped["conflicting_result"] = len(conflicts)
        ordered = sorted(reviews.values(), key=lambda r: (r["timestamp"], r["review_id"]), reverse=True)
        limited = limited or len(ordered) > EXPORT_CAP
        return {"source_available": int(read > 0), "collection_complete": int(read > 0 and not errors and not pending and not limited and not any(skipped.values())),
                "scan_timestamp_seconds": now, "source_files": read, "source_errors": errors,
                "pending_results": pending, "limit_reached": int(limited), "skipped_records": dict(skipped), "reviews": ordered[:EXPORT_CAP]}


def render_review_metrics(snapshot):
    lines = []
    for name, (labels, help_text) in METRICS.items():
        lines.extend((f"# HELP {name} {help_text}", f"# TYPE {name} gauge"))
        key = name.removeprefix("cwo_review_")
        if key == "skipped_records":
            lines.extend(f'{name}{{reason="{reason}"}} {snapshot[key].get(reason, 0)}' for reason in SKIP_REASONS)
        elif labels:
            for row in snapshot["reviews"]:
                values = row["tokens"].items() if key == "tokens" else [(None, row["timestamp"] if key == "started_timestamp_seconds" else row["duration"])]
                for kind, value in values:
                    if value is None:continue
                    dimensions = {label: row[label] for label in LABELS}
                    if kind is not None:dimensions["kind"] = kind
                    lines.append(name + "{" + ",".join(k + "=" + json.dumps(v) for k, v in dimensions.items()) + "} " + str(value))
        else:
            lines.append(name + " " + str(snapshot[key]))
    return ("\n".join(lines) + "\n").encode()
