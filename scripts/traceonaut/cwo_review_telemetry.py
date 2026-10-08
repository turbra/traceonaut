"""Opt-in reader for audited contractor launches and Claude CLI results.

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
from .cwo_review_provenance import digest, receipt_keys

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_SCAN_BYTES = 32 * 1024 * 1024
EXPORT_CAP = 256
SNAPSHOT_HISTORY_CAP = 4096
SKIP_REASONS = ("invalid_record", "unmatched_dispatch", "future_timestamp", "conflicting_result")
DISCOVERY_REASONS = ('missing_result','conflicting_result','invalid_record','oversized_output',
                     'changing_output','unreadable_output','reused_output','unsupported_launch')
LABELS = ("review_id", "outcome", "requested_model", "reported_model", "effort")
KINDS = ("input", "cache_creation", "cache_read", "output", "thinking")
SNAPSHOT_LABELS = (*LABELS, 'history', 'state', 'record_state', 'verdict', 'project_id', 'session_id',
                   'input_available', 'output_available', 'duration_available')
METRICS = {
    "cwo_review_source_available": ((), "At least one configured review launch or provenance record was read."),
    "cwo_review_collection_complete": ((), "Enabled review sources read without errors, missing results or limits."),
    "cwo_review_scan_timestamp_seconds": ((), "Unix time of the latest CLI review scan attempt."),
    "cwo_review_source_files": ((), "Review launch and provenance records read in the latest scan."),
    "cwo_review_source_errors": ((), "Review artifact access failures in the latest scan."),
    "cwo_review_pending_results": ((), "Recorded launches without a saved final result."),
    "cwo_review_limit_reached": ((), "Review artifact discovery, byte or export cap reached."),
    "cwo_review_skipped_records": (("reason",), "Review artifacts omitted in the latest scan by bounded reason."),
    "cwo_review_started_timestamp_seconds": (LABELS, "Recorded launch time of an external review invocation."),
    "cwo_review_tokens": ((*LABELS, "kind"), "CLI top-level usage by kind; thinking is a subset of output. Input excludes cache creation and reads."),
    "cwo_review_duration_seconds": (LABELS, "CLI-reported result duration; not time inferred from file timestamps."),
    "cwo_review_session_info": (("review_id", "project_id", "session_id"), "Proven immediate launching Codex session for a collected CLI review."),
    "cwo_review_attribution_state": (("review_id", "state"), "Review attribution: linked, unlinked, ambiguous or pending source scanning; one state per review."),
    "cwo_review_discovered_launches": ((), "External review launches found in retained CWO session command records; excludes explicit model checks."),
    "cwo_review_discovery_gaps": (("reason",), "Discovered review executions with incomplete or conflicting saved evidence."),
    "cwo_review_record_state": (("review_id","record_state"), "Availability of the saved invocation result; not review acceptance."),
    "cwo_review_evaluation_info": (("review_id","verdict"), "Latest recorded evaluator verdict for the matching audited dispatch; not final human acceptance."),
    "cwo_review_snapshot_timestamp_seconds": (SNAPSHOT_LABELS, "Scan timestamp identifying the current review projection, including missing values and source linkage."),
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


def normalize_launch(launch):
    """Accept recorded CLI effort aliases without claiming provider attestation."""
    launch = dict(launch)
    efforts = {launch[key] for key in ("effort", "requested_effort", "executed_effort")
               if key in launch and isinstance(launch[key], str)}
    if len(efforts) > 1 or any(key in launch and not isinstance(launch[key], str)
                             for key in ("effort", "requested_effort", "executed_effort")):
        raise ValueError("invalid_record")
    if efforts:
        launch["effort"] = efforts.pop()
    return launch


def launch_time(launch, now):
    value = launch.get("started_at")
    at = _timestamp(value)
    if (not isinstance(value, str) or not re.search(r"(?:Z|[+-]\d\d:\d\d)$", value)
            or at is None or at <= 0):
        raise ValueError("invalid_record")
    if at > now:
        raise ValueError("future_timestamp")
    _label(launch.get("requested_model"))
    _label(launch.get("effort", "unknown"))
    return at


def project(launch, result, prepared, now, *, attempt_identity=False):
    launch = normalize_launch(launch)
    dispatch, packet = launch.get("dispatch_id"), launch.get("packet_sha256")
    if (not isinstance(dispatch, str) or not 0 < len(dispatch) <= 256
            or not isinstance(packet, str) or re.fullmatch(r"[0-9a-f]{64}", packet) is None):
        raise ValueError("invalid_record")
    at = launch_time(launch, now)
    if (dispatch, packet) not in prepared or prepared[dispatch, packet] > at:
        raise ValueError("unmatched_dispatch")
    identity, session = _uuid(result.get("uuid")), _uuid(result.get("session_id"))
    if attempt_identity and any(key in result and not _uuid(result[key]) for key in ("uuid", "session_id")):
        raise ValueError("invalid_record")
    if (result.get("type") != "result" or type(result.get("is_error")) is not bool
            or (not attempt_identity and (not identity or not session))):
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
    attempt = digest(["review-attempt", dispatch, packet, at])
    review_id = (hashlib.sha256((session + ":" + identity).encode()).hexdigest()
                 if identity and session else digest([attempt, launch.get("prompt_sha256")]))
    return {"review_id": review_id, "timestamp": at,
            "outcome": "failed" if result["is_error"] else "completed",
            "requested_model": _label(launch.get("requested_model")), "reported_model": reported,
            "effort": _label(launch.get("effort", "unknown")), "tokens": tokens,
            "duration": duration / 1000 if duration is not None else None,
            # Retained in the private numeric projection for conflict checks only.
            "binding": hashlib.sha256((dispatch + ":" + packet).encode()).hexdigest(),
            "attempt": attempt, "cli_identity": bool(identity and session),
            "provenance_keys": receipt_keys(launch, result) if identity and session else [],
            "provider_session_id": session, "provider_result_id": identity}


def pending_projection(launch, prepared, now):
    at=launch_time(launch,now)
    key=launch.get('dispatch_id'),launch.get('packet_sha256')
    if key not in prepared or prepared[key]>at:
        raise ValueError('unmatched_dispatch')
    identity=digest(['pending-launch',*key,at])
    return {'review_id':identity,'timestamp':at,'outcome':'unknown',
            'requested_model':launch['requested_model'],'reported_model':'unknown','effort':launch.get('effort','unknown'),
            'tokens':{},'duration':None,'record_state':'missing_result','provenance_keys':[],
            'binding':hashlib.sha256((key[0]+':'+key[1]).encode()).hexdigest(),
            'attempt':digest(['review-attempt',*key,at]),'cli_identity':False}


def saved_metadata(row, launch, path, now):
    finished = _timestamp(launch.get('finished_at'))
    if finished is not None and row['timestamp'] <= finished <= now:
        row['finished_timestamp'] = finished
    if row.get('record_state') == 'missing_result' and launch.get('model_attestation') == 'timed-out':
        row['outcome'] = 'failed'
        row['execution_detail'] = 'timed_out'
    return row


class ReviewCollector(AuditCollector):
    def __init__(self, *, directories):
        if directories:
            super().__init__(directories=directories)
        else:
            self.directories, self.files, self._cache = (), (), {}

    def _candidate(self, name):
        return name.endswith(("-launch-receipt.json", "-launch.json", "-provenance.json"))

    def scan(self, *, now=None, files=None, reader=None):
        now = time.time() if now is None else now
        files, errors, limited = self._discover() if files is None else (files, 0, False)
        read = pending = 0
        remaining = MAX_SCAN_BYTES
        reviews, conflicts, poisoned_attempts, skipped = {}, set(), set(), Counter()
        audits, evaluations = {}, {}

        def content(path):
            nonlocal remaining
            if reader is not None:
                return reader(path)
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

        def prepared_at(directory):
            nonlocal errors, limited
            if directory not in audits:
                prepared = {}
                found = False
                for name in ("audit.jsonl", "contract-audit.jsonl"):
                    try:
                        raw = content(directory / name)
                    except FileNotFoundError:
                        continue
                    except OverflowError:
                        found = limited = True
                        continue
                    except (OSError, UnicodeError):
                        found = True
                        errors += 1
                        continue
                    found = True
                    events, omissions = _parse(raw)
                    if omissions:
                        skipped["invalid_record"] += 1
                        continue
                    valid = {event[0]: (event[1],event[2]) for event in events if event[2] <= now}
                    for line in raw.splitlines():
                        if not line.strip():
                            continue
                        record = _json(line)
                        event = valid.get(record.get("event_hash"))
                        key = record.get("dispatch_id"), record.get("packet_sha256")
                        if event is not None and all(isinstance(v, str) for v in key):
                            kind,at=event
                            if kind=='dispatch_prepared':
                                prepared[key] = min(at, prepared.get(key, at))
                            elif kind=='return_evaluated' and record.get('verdict') in {'accept','reject'}:
                                binding=hashlib.sha256((key[0]+':'+key[1]).encode()).hexdigest()
                                if at>=evaluations.get(binding,(0,''))[0]:
                                    evaluations[binding]=(at,record['verdict'])
                if not found:
                    raise FileNotFoundError()
                audits[directory] = prepared
            return audits[directory]

        def provenance(path, launch):
            # The prompt is hashed in memory, never retained or exported. Only
            # its explicit CWO dispatch headers bind this attempt to the audit.
            prompt = content(path.with_name(path.name.removesuffix("-provenance.json") + "-prompt.txt"))
            if hashlib.sha256(prompt).hexdigest() != launch.get("prompt_sha256"):
                raise ValueError("invalid_record")
            text = prompt.decode("utf-8")
            dispatches = re.findall(r"^Dispatch ID: ([^\r\n]+)$", text, re.MULTILINE)
            packets = re.findall(r"^Packet SHA-256: ([0-9a-f]{64})$", text, re.MULTILINE)
            if len(dispatches) != 1 or len(packets) != 1:
                raise ValueError("invalid_record")
            for key, value in (("dispatch_id", dispatches[0]), ("packet_sha256", packets[0])):
                if key in launch and launch[key] != value:
                    raise ValueError("invalid_record")
                launch[key] = value
            events = launch.get("events")
            if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
                raise ValueError("invalid_record")
            finals = [event for event in events if event.get("type") == "result"]
            if not finals:
                return None
            if len(finals) != 1:
                raise ValueError("invalid_record")
            return finals[0]

        for path in files:
            try:
                # A preparation file is sometimes retained alongside the actual
                # timed launch receipt. A complete second launch is still read.
                launch = _json(content(path))
                if (path.name.endswith("-launch.json") and
                        "started_at" not in launch and
                        path.with_name(path.name.removesuffix("-launch.json") + "-launch-receipt.json") in files):
                    continue
                read += 1
                launch = normalize_launch(launch)
                launch_time(launch, now)
                is_provenance = path.name.endswith("-provenance.json")
                if is_provenance:
                    result = provenance(path, launch)
                    if result is None:
                        pending += 1
                        row=saved_metadata(pending_projection(launch,prepared_at(path.parent),now), launch, path, now)
                        if row['timestamp']>=now-RETENTION_SECONDS:
                            reviews[row['review_id']]=row
                        continue
                else:
                    suffix, result_suffix = ("-launch-receipt.json", "-response.raw.json") if path.name.endswith("-launch-receipt.json") else ("-launch.json", "-response.json")
                    try:
                        result = _json(content(path.with_name(path.name.removesuffix(suffix) + result_suffix)))
                    except FileNotFoundError:
                        pending += 1;continue
                row = project(launch, result, prepared_at(path.parent), now,
                              attempt_identity=is_provenance)
                saved_metadata(row, launch, path, now)
                if row["timestamp"] < now - RETENTION_SECONDS:continue
                identity = row["review_id"]
                if identity in reviews:
                    prior = reviews[identity]
                    core = lambda value: {k:v for k,v in value.items() if k not in {"provenance_keys", "provenance_conflict"}}
                    if core(prior) != core(row):
                        conflicts.add(identity)
                        poisoned_attempts.update((prior["attempt"], row["attempt"]))
                    elif prior["provenance_keys"] != row["provenance_keys"]:
                        prior["provenance_conflict"] = True
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
        for identity, row in list(reviews.items()):
            if row["attempt"] in poisoned_attempts:
                conflicts.add(identity)
                reviews.pop(identity)
        # A copied launch can have both a full CLI result and a sanitized
        # provenance result. Reconcile on the audited launch, never token values.
        attempts = {}
        for row in reviews.values():
            attempts.setdefault(row["attempt"], []).append(row)
        for group in attempts.values():
            if len(group) == 1 or all(row["cli_identity"] for row in group):
                continue
            prior = next((row for row in group if row["cli_identity"]), group[0])
            fields = ("timestamp", "outcome", "requested_model", "reported_model", "effort", "tokens", "duration", "binding")
            if (any(any(prior[key] != row[key] for key in fields) for row in group)
                    or sum(row["cli_identity"] for row in group) > 1):
                conflicts.update(row["review_id"] for row in group)
            else:
                for row in group:
                    if row is not prior:
                        reviews.pop(row["review_id"], None)
        for identity in conflicts:reviews.pop(identity, None)
        for row in reviews.values():
            row['evaluation']=evaluations.get(row['binding'],(0,'unknown'))[1]
        skipped["conflicting_result"] = len(conflicts)
        ordered = sorted(reviews.values(), key=lambda r: (r["timestamp"], r["review_id"]), reverse=True)
        limited = limited or len(ordered) > EXPORT_CAP
        return {"source_available": int(read > 0), "collection_complete": int(read > 0 and not errors and not pending and not limited and not any(skipped.values())),
                "scan_timestamp_seconds": now, "source_files": read, "source_errors": errors,
                "pending_results": pending, "limit_reached": int(limited), "skipped_records": dict(skipped), "reviews": ordered[:EXPORT_CAP]}


def preserve_review_markers(db, snapshot):
    """Retain withdrawals separately from ordinary age expiry in private state."""
    now = snapshot['scan_timestamp_seconds']
    active = {row['review_id']: row for row in snapshot['reviews']
              if row['timestamp'] + RETENTION_SECONDS > now}
    with db:
        db.execute('''CREATE TABLE IF NOT EXISTS review_snapshot_history(
            review_id TEXT PRIMARY KEY, started REAL NOT NULL,
            projection TEXT NOT NULL, removed INTEGER NOT NULL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS review_snapshot_history_limit(
            singleton INTEGER PRIMARY KEY CHECK(singleton=1), until REAL NOT NULL)''')
        # Expiry leaves the final positive (or withdrawal) sample in Prometheus.
        db.execute('DELETE FROM review_snapshot_history WHERE started<=?',
                   (now - RETENTION_SECONDS,))
        previous = {row[0]: row for row in db.execute(
            'SELECT review_id,started,projection,removed FROM review_snapshot_history')}
        for identity, row in active.items():
            projection = {key: row[key] for key in (*LABELS, 'timestamp', 'tokens', 'duration')}
            for key in ('attribution', 'record_state', 'evaluation', 'source_session'):
                if key in row:
                    projection[key] = row[key]
            encoded = json.dumps(projection, sort_keys=True, separators=(',', ':'))
            old = previous.get(identity)
            if old is None or old[1] != row['timestamp'] or old[2] != encoded or old[3]:
                db.execute('INSERT OR REPLACE INTO review_snapshot_history VALUES(?,?,?,0)',
                           (identity, row['timestamp'], encoded))
        for identity, row in previous.items():
            if identity not in active and not row[3]:
                db.execute('UPDATE review_snapshot_history SET removed=1 WHERE review_id=?', (identity,))
        retained = list(db.execute('SELECT review_id,started FROM review_snapshot_history ORDER BY started DESC,review_id'))
        dropped = retained[SNAPSHOT_HISTORY_CAP:]
        if dropped:
            db.executemany('DELETE FROM review_snapshot_history WHERE review_id=?',
                           ((row[0],) for row in dropped))
            until = max(row[1] + RETENTION_SECONDS for row in dropped)
            db.execute('''INSERT INTO review_snapshot_history_limit VALUES(1,?)
                ON CONFLICT(singleton) DO UPDATE SET until=max(until,excluded.until)''', (until,))
        limit = db.execute('SELECT until FROM review_snapshot_history_limit WHERE singleton=1').fetchone()
        limited = bool(limit and limit[0] > now)
        retired = []
        for encoded, in db.execute('SELECT projection FROM review_snapshot_history WHERE removed=1 ORDER BY review_id'):
            row = json.loads(encoded)
            row.update(record_state='removed', tokens={}, duration=None)
            retired.append(row)
    snapshot['retired_reviews'] = retired
    snapshot['history_tracked'] = True
    if limited:
        snapshot['limit_reached'] = 1
        snapshot['collection_complete'] = 0
    return snapshot


def render_review_metrics(snapshot):
    lines = []
    for name, (labels, help_text) in METRICS.items():
        lines.extend((f"# HELP {name} {help_text}", f"# TYPE {name} gauge"))
        key = name.removeprefix("cwo_review_")
        if key == "skipped_records":
            lines.extend(f'{name}{{reason="{reason}"}} {snapshot[key].get(reason, 0)}' for reason in SKIP_REASONS)
        elif key == 'discovered_launches':
            lines.append(f"{name} {snapshot.get('discovery',{}).get('launches',0)}")
        elif key == 'discovery_gaps':
            lines.extend(f'{name}{{reason="{reason}"}} {snapshot.get("discovery",{}).get("gaps",{}).get(reason,0)}' for reason in DISCOVERY_REASONS)
        elif key == 'record_state':
            lines.extend(name+'{review_id='+json.dumps(row['review_id'])+',record_state='+json.dumps(row.get('record_state','complete'))+'} 1' for row in snapshot['reviews'])
        elif key == 'evaluation_info':
            lines.extend(name+'{review_id='+json.dumps(row['review_id'])+',verdict='+json.dumps(row.get('evaluation','unknown'))+'} 1' for row in snapshot['reviews'])
        elif key == 'snapshot_timestamp_seconds':
            for row in (*snapshot['reviews'], *snapshot.get('retired_reviews', [])):
                dimensions={label:row[label] for label in LABELS}
                dimensions.update(history='tracked' if snapshot.get('history_tracked') else 'legacy',
                                  state=row.get('attribution','unlinked'),record_state=row.get('record_state','complete'),
                                  verdict=row.get('evaluation','unknown'),
                                  project_id=row.get('source_session',{}).get('project_id',''),
                                  session_id=row.get('source_session',{}).get('session_id',''),
                                  input_available=str(int(all(k in row['tokens'] for k in KINDS[:3]))),
                                  output_available=str(int('output' in row['tokens'])),
                                  duration_available=str(int(row['duration'] is not None)))
                lines.append(name+'{'+','.join(k+'='+json.dumps(v) for k,v in dimensions.items())+'} '+str(snapshot['scan_timestamp_seconds']))
        elif key in {"session_info", "attribution_state"}:
            for row in snapshot["reviews"]:
                if key == "session_info":
                    if not row.get("source_session"):continue
                    dimensions = {"review_id": row["review_id"], **row["source_session"]}
                else:
                    dimensions = {"review_id": row["review_id"], "state": row.get("attribution", "unlinked")}
                lines.append(name + "{" + ",".join(k + "=" + json.dumps(v) for k,v in dimensions.items()) + "} 1")
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
