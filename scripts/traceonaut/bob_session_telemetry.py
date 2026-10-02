"""Read-only Bob Shell 2.0.1 task and saved-message telemetry."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import fcntl
import os
from pathlib import Path
import sqlite3
import time
import unicodedata

from .collector_paths import checked_path

SAFE_INTEGER = 2**53 - 1
TOKEN_FIELDS = {"input": "input", "output": "output", "cacheRead": "cached_input",
                "cacheWrite": "cache_write_input"}
KINDS = {"normal", "subtask", "subagent"}
SKIP_REASONS = ("invalid_record", "invalid_json", "invalid_number", "invalid_timestamp",
                "unsupported_version", "unsupported_kind", "oversized_record", "unknown_tool_outcome")
MAX_ROWS_PER_SCAN = 2_000
MAX_BYTES_PER_SCAN = 4 * 1024**2
MAX_SCAN_SECONDS = 0.5
MAX_TRANSACTION_SECONDS = 60
FULL_RECONCILE_SECONDS = 300
MAX_TASKS = 100_000
MAX_MESSAGES = 1_000_000
MAX_JSON_BYTES = 1024**2
MAX_TASK_BYTES = 64 * 1024
MAX_INDEX_BYTES = 512 * 1024**2


def number(value):
    return value if type(value) is int and 0 <= value <= SAFE_INTEGER else None


def identity(value):
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError("invalid_record")
    return hashlib.sha256(value.encode()).hexdigest()


def text(value):
    if not isinstance(value, str):
        return ""
    return ''.join(c for c in ' '.join(value.split()) if not unicodedata.category(c).startswith('C'))[:120]


def object_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("invalid_json")
            result[key] = value
        return result
    try:
        result = json.loads(raw, object_pairs_hook=unique,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid_json")))
        if not isinstance(result, dict):
            raise ValueError("invalid_json")
        return result
    except (TypeError, ValueError, RecursionError):
        raise ValueError("invalid_json") from None


def timestamp(value, now):
    milliseconds = number(value)
    if milliseconds is None or milliseconds > (now + 300) * 1000:
        raise ValueError("invalid_timestamp")
    return milliseconds / 1000


def project_task(row, now):
    """Keep only explicit presentation names and qualified numeric metadata."""
    if row["version"] not in (None, "2.0.1"):
        raise ValueError("unsupported_version")
    if row["task_type"] not in KINDS:
        raise ValueError("unsupported_kind")
    costs = object_json(row["costs"]) if row["costs"] is not None else {}
    usage = {target: number(costs.get(source)) for source, target in TOKEN_FIELDS.items()}
    return {"session_id": identity(row["id"]), "project_id": identity(row["project_id"]),
            "parent_id": identity(row["parent_id"]) if row["parent_id"] is not None else None,
            "title": text(row["title"]), "project_name": text(Path(row["directory"]).name),
            "kind": row["task_type"], "completed_subtask": row["task_type"] == "subtask" and row["status"] == "completed",
            "created_at": timestamp(row["created_at"], now),
            "updated_at": timestamp(row["updated_at"], now), "usage": usage,
            "partial": int(any(v is None for v in usage.values()))}


def project_message(row, now):
    """Nested messages, prompts, responses and tool arguments never leave this call."""
    data = object_json(row["data"])
    meta = data.get("_meta") or {}
    if not isinstance(meta, dict) or data.get("role", row["role"]) != row["role"]:
        raise ValueError("invalid_record")
    result = {"message_id": identity(row["id"]), "session_id": identity(row["task_id"]),
              "at": timestamp(row["created_at"], now), "responses": 0,
              "tools": 0, "errors": 0, "unknown": 0, "duration": None}
    if row["role"] == "assistant":
        result["responses"] = int(not meta.get("notAi") and not meta.get("renderUI"))
    elif row["role"] == "tool":
        result["tools"] = 1
        usage = data.get("toolUsage")
        signature = usage.get("signature") if isinstance(usage, dict) else None
        outcome = signature.get("isError") if isinstance(signature, dict) else None
        if type(outcome) is bool:
            result["errors"] = int(outcome)
        else:
            result["unknown"] = 1
        duration = meta.get("durationMs")
        if type(duration) in (int, float) and math.isfinite(duration) and 0 <= duration <= SAFE_INTEGER:
            result["duration"] = duration / 1000
    elif row["role"] not in ("user", "system"):
        raise ValueError("invalid_record")
    return result


def exclusive_usage(tasks):
    """Remove completed subtasks already included in parent task costs."""
    children = {}
    for task in tasks:
        if task["completed_subtask"] and task["parent_id"]:
            children.setdefault(task["parent_id"], []).append(task)
    result = {}
    for task in tasks:
        usage = dict(task["usage"])
        for key, value in usage.items():
            completed = children.get(task["session_id"], [])
            operands = [child["usage"][key] for child in completed]
            usage[key] = (value - sum(operands) if value is not None and
                          all(child['updated_at'] <= task['updated_at'] for child in completed) and
                          all(v is not None for v in operands) and sum(operands) <= value else None)
        total = None if usage["input"] is None or usage["output"] is None else usage["input"] + usage["output"]
        usage["total"] = number(total)
        result[task["session_id"]] = usage
    return result


def empty_snapshot():
    return {"version": 1, "source": "ibm-bob", "sessions": [], "source_available": 0,
            "scan_timestamp": 0, "last_success": 0, "collection_complete": 0,
            "pending": 0, "limit_reached": 0, "source_errors": 0,
            "skipped_records": dict.fromkeys(SKIP_REASONS, 0), "indexed_sessions": 0,
            "expired_sessions": 0, "cap_omitted_sessions": 0}


class BobCollector:
    """Thread-owned bounded SQLite reader, with a private numeric staging index.

    A read transaction supplies a coherent generation across scan slices. Only
    completed generations replace the published cache. Changed sources refresh
    tasks and reconcile message identities before reading appended bodies.
    Full reads also reconcile older body edits every five minutes.
    """

    def __init__(self, bob_home, state_dir, *, session_retention_seconds=30 * 86400,
                 session_export_cap=1000, max_rows=MAX_ROWS_PER_SCAN,
                 max_bytes=MAX_BYTES_PER_SCAN, scan_seconds=MAX_SCAN_SECONDS):
        self.home = checked_path(bob_home, missing=True)
        self.path = self.home / "db/bob.db"
        self.state = checked_path(state_dir, missing=True, private=True)
        if self.state.is_relative_to(self.home) or self.home.is_relative_to(self.state):
            raise ValueError("Bob state must be separate from the source")
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        checked_path(self.state, private=True)
        self.retention, self.cap = session_retention_seconds, session_export_cap
        if type(self.retention) is not int or not 0 <= self.retention <= SAFE_INTEGER:
            raise ValueError("invalid retention")
        if type(self.cap) is not int or not 1 <= self.cap <= MAX_TASKS:
            raise ValueError("invalid export cap")
        self.max_rows, self.max_bytes, self.scan_seconds = max_rows, max_bytes, scan_seconds
        lock_path = checked_path(self.state / "bob-writer.lock", missing=True, private=True)
        self.lock = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        self.db = self.source = None
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for suffix in ("", "-wal", "-shm", "-journal"):
                checked_path(self.state / ("bob.sqlite3" + suffix), missing=True, private=True)
            index = self.state / "bob.sqlite3"
            if index.exists() and index.stat().st_size > MAX_INDEX_BYTES:
                raise ValueError("Bob index capacity exceeded")
            fd = os.open(index, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            self.db = sqlite3.connect(index, timeout=.1)
            # Keep the journal file stable: the compatible Codex reader checks
            # this shared private directory while Bob may be committing a scan.
            self.db.execute('PRAGMA journal_mode=PERSIST')
            self.db.execute('PRAGMA journal_size_limit=1048576')
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    at REAL, responses INTEGER, tools INTEGER, errors INTEGER, unknown INTEGER, duration REAL);
                CREATE TABLE IF NOT EXISTS publication (id INTEGER PRIMARY KEY CHECK (id=1), body TEXT NOT NULL);
            ''')
            row = self.db.execute("SELECT body FROM publication WHERE id=1").fetchone()
            self.value = object_json(row[0]) if row else empty_snapshot()
            self.value.update(source_available=0, collection_complete=0, pending=1)
            self.fingerprint = self.data_version = None
            self.stage = None
            self.identity_reader = None
            self.pending_identity = None
            self.message_ids = {}
            self.message_cursor = -(2**63)
            self.message_skips = dict.fromkeys(SKIP_REASONS, 0)
            self.identities_valid = True
            self.reconcile_at = 0
            self.deadline = float('inf')
        except Exception:
            self.close()
            raise

    def close(self):
        self.identity_reader = None
        self.pending_identity = None
        if self.source is not None:
            self.source.close()
            self.source = None
        if self.db is not None:
            self.db.close()
            self.db = None
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None

    def _open(self):
        info = checked_path(self.path).stat()
        for suffix in ("-wal", "-shm", "-journal"):
            checked_path(Path(str(self.path) + suffix), missing=True)
        # Never let a reader create a missing WAL shared-memory sidecar.
        if Path(str(self.path) + '-wal').exists() and not Path(str(self.path) + '-shm').exists():
            raise ValueError("Bob WAL has no shared-memory file")
        fingerprint = (info.st_dev, info.st_ino)
        if self.source is not None and fingerprint != self.fingerprint:
            self.identity_reader = None
            self.source.close()
            self.source = None
            self.stage = None
        if self.source is None:
            self.source = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=.1)
            self.source.row_factory = sqlite3.Row
            self.source.execute('PRAGMA query_only=ON')
            self.source.execute('PRAGMA trusted_schema=OFF')
            self.source.set_progress_handler(lambda: int(time.monotonic() > self.deadline), 1000)
            requirements = {"tasks": {"id": "TEXT", "project_id": "TEXT", "parent_id": "TEXT",
                "title": "TEXT", "status": "TEXT", "directory": "TEXT", "version": "TEXT",
                "task_type": "TEXT", "created_at": "INTEGER", "updated_at": "INTEGER", "costs": "TEXT"},
                "messages": {"id": "TEXT", "task_id": "TEXT", "role": "TEXT", "data": "TEXT", "created_at": "INTEGER"}}
            for table, required in requirements.items():
                kind = self.source.execute('SELECT type FROM sqlite_master WHERE name=?', (table,)).fetchone()
                columns = {r['name']: r for r in self.source.execute('PRAGMA table_info(' + table + ')')}
                if (not kind or kind[0] != 'table' or any(k not in columns or columns[k]['type'].upper() != v
                    for k, v in required.items()) or columns['id']['pk'] != 1):
                    raise ValueError("unsupported Bob schema")
            self.fingerprint = fingerprint
            self.data_version = None
        return self.source

    def _start(self, now, version):
        self.source.execute('BEGIN')
        # Establish the source snapshot before any task/message projection.
        self.source.execute('SELECT rowid FROM tasks LIMIT 1').fetchone()
        with self._index_errors():
            self.db.execute('DELETE FROM tasks')
            self.db.commit()
        self.started = time.monotonic()
        self.generation_time = now
        self.generation_version = version
        self.counts = {'tasks': 0, 'messages': 0}
        self.skips = dict(self.message_skips)
        self.full_read = (self.data_version is None or self.started >= self.reconcile_at
                          or not self.identities_valid)
        if self.full_read:
            self._reset_messages()
        self.stage = 'tasks'
        self.cursor = -(2**63)
        self.identity_reader = None
        self.pending_identity = None
        self.checked_ids = self.matched_ids = 0

    def _reset_messages(self):
        """Fall back within the same source transaction, retaining task results."""
        with self._index_errors():
            self.db.execute('DELETE FROM messages')
        for reason, count in self.message_skips.items():
            self.skips[reason] -= count
        self.message_skips = dict.fromkeys(SKIP_REASONS, 0)
        self.message_ids = {}
        self.message_cursor = -(2**63)
        self.identities_valid = True
        self.full_read = True
        self.counts['messages'] = 0
        self.identity_reader = None
        self.pending_identity = None

    def _read_identity(self):
        if self.pending_identity is not None:
            row, self.pending_identity = self.pending_identity, None
            return row
        if self.identity_reader is None:
            # SQLite can use the primary-key covering index: no message bodies.
            self.identity_reader = self.source.execute(
                'SELECT rowid AS cursor, substr(id,1,513) AS id FROM messages')
        return self.identity_reader.fetchone()

    def _check_identity(self, row):
        self.checked_ids += 1
        if self.checked_ids > MAX_MESSAGES:
            raise OverflowError('record limit')
        try:
            digest = identity(row['id'])
        except ValueError:
            return False
        if row['cursor'] <= self.message_cursor:
            if self.message_ids.get(row['cursor']) != digest:
                return False
            self.matched_ids += 1
        return True

    def _begin_messages(self):
        self.identity_reader = None
        self.pending_identity = None
        self.stage, self.cursor = 'messages', self.message_cursor
        self.counts['messages'] = len(self.message_ids)

    def _skip(self, reason):
        reason = reason if reason in SKIP_REASONS else 'invalid_record'
        self.skips[reason] += 1
        if self.stage == 'messages':
            self.message_skips[reason] += 1

    def _read_row(self):
        if self.stage == 'tasks':
            return self.source.execute('''SELECT rowid AS cursor, substr(id,1,513) AS id,
                substr(project_id,1,513) AS project_id, substr(parent_id,1,513) AS parent_id,
                substr(title,1,120) AS title, substr(status,1,64) AS status,
                substr(directory,1,4096) AS directory, substr(version,1,32) AS version,
                substr(task_type,1,32) AS task_type, created_at, updated_at,
                length(CAST(costs AS BLOB)) AS bytes,
                CASE WHEN length(CAST(costs AS BLOB)) <= ? THEN costs END AS costs
                FROM tasks WHERE rowid > ? ORDER BY rowid LIMIT 1''',
                (MAX_TASK_BYTES, self.cursor)).fetchone()
        return self.source.execute('''SELECT rowid AS cursor, substr(id,1,513) AS id,
            substr(task_id,1,513) AS task_id, substr(role,1,32) AS role, created_at,
            length(CAST(data AS BLOB)) AS bytes,
            CASE WHEN length(CAST(data AS BLOB)) <= ? THEN data END AS data
            FROM messages WHERE rowid > ? ORDER BY rowid LIMIT 1''',
            (MAX_JSON_BYTES, self.cursor)).fetchone()

    @contextmanager
    def _index_errors(self):
        try:
            yield
        except sqlite3.Error:
            # A failed private index is not an outage of Bob's source database.
            raise RuntimeError('Bob collector storage unavailable') from None

    def _finish(self, now):
        self.source.execute('ROLLBACK')
        self.stage = None
        with self._index_errors():
            self._publish(now)
        self.message_cursor = self.cursor
        if self.full_read:
            # Appends and unchanged polls must not postpone older body checks.
            self.reconcile_at = self.started + FULL_RECONCILE_SECONDS

    def _publish(self, now):
        self.db.commit()
        tasks = [json.loads(r[0]) for r in self.db.execute('SELECT body FROM tasks')]
        usages = exclusive_usage(tasks)
        aggregates = {r[0]: r[1:] for r in self.db.execute('''SELECT session_id, max(at),
            sum(responses), sum(tools), sum(errors), sum(unknown), sum(duration), count(duration)
            FROM messages GROUP BY session_id''')}
        sessions = []
        for row in tasks:
            at, responses, tools, errors, unknown, duration, timed = aggregates.get(
                row['session_id'], (None, 0, 0, 0, 0, None, 0))
            usage = usages[row['session_id']]
            partial = int(bool(row['partial'] or unknown or any(v is None for v in usage.values())))
            sessions.append({**row, 'usage': usage, 'last_event': at,
                'responses': responses, 'tool_results': tools, 'tool_errors': errors,
                'tool_unknown': unknown, 'tool_seconds': duration, 'timed_tools': timed,
                'partial': partial})
        self.value = {**empty_snapshot(), 'source_available': 1, 'scan_timestamp': now,
            'last_success': now, 'collection_complete': int(not any(self.skips.values()) and not any(s['partial'] for s in sessions)),
            'skipped_records': dict(self.skips), 'sessions': sessions, 'indexed_sessions': len(sessions)}
        self.db.execute('INSERT OR REPLACE INTO publication VALUES (1, ?)',
                        (json.dumps(self.value, separators=(',', ':')),))
        self.db.commit()
        # Save the version from before the transaction. Writes made during it
        # cause the next scan to start a new generation, rather than being lost.
        self.data_version = self.generation_version

    def scan(self, *, now=None):
        now = time.time() if now is None else now
        self.deadline = time.monotonic() + self.scan_seconds
        self.value['scan_timestamp'] = now
        try:
            source = self._open()
            version = source.execute('PRAGMA data_version').fetchone()[0]
            if (self.stage is None and self.data_version == version
                    and time.monotonic() < self.reconcile_at):
                self.value.update(source_available=1, last_success=now, source_errors=0)
                return self.snapshot(now=now)
            if self.stage is None:
                self._start(now, version)
            if time.monotonic() - self.started > MAX_TRANSACTION_SECONDS:
                raise OverflowError('scan transaction limit')
            self.value.update(source_available=1, pending=1, collection_complete=0, source_errors=0)
            read_rows = read_bytes = 0
            while read_rows < self.max_rows and read_bytes < self.max_bytes and time.monotonic() < self.deadline:
                if self.stage == 'identities':
                    row = self._read_identity()
                    if row is None:
                        if self.matched_ids != len(self.message_ids):
                            self._reset_messages()
                        self._begin_messages()
                    else:
                        length = len(row['id'].encode()) if isinstance(row['id'], str) else 513
                        if read_rows and read_bytes + length > self.max_bytes:
                            self.pending_identity = row
                            break
                        read_rows += 1
                        read_bytes += length
                        if not self._check_identity(row):
                            self._reset_messages()
                            self._begin_messages()
                    continue
                row = self._read_row()
                if row is None:
                    if self.stage == 'tasks':
                        if self.full_read:
                            self._begin_messages()
                        else:
                            self.stage = 'identities'
                        continue
                    self._finish(now)
                    break
                length = row['bytes'] or 0
                bounded_length = min(length, MAX_JSON_BYTES if self.stage == 'messages' else MAX_TASK_BYTES)
                if read_rows and read_bytes + bounded_length > self.max_bytes:
                    break
                self.cursor = row['cursor']
                read_rows += 1
                read_bytes += bounded_length
                self.counts[self.stage] += 1
                if self.counts[self.stage] > (MAX_TASKS if self.stage == 'tasks' else MAX_MESSAGES):
                    raise OverflowError('record limit')
                if self.stage == 'messages':
                    try:
                        self.message_ids[row['cursor']] = identity(row['id'])
                    except ValueError:
                        # Invalid IDs still count toward limits. Without a stable
                        # identity, changed sources must use a complete read.
                        self.message_ids[row['cursor']] = None
                        self.identities_valid = False
                if length > (MAX_TASK_BYTES if self.stage == 'tasks' else MAX_JSON_BYTES):
                    self._skip('oversized_record')
                    continue
                try:
                    if self.stage == 'tasks':
                        item = project_task(row, now)
                        with self._index_errors():
                            self.db.execute('INSERT OR REPLACE INTO tasks VALUES (?, ?)',
                                            (item['session_id'], json.dumps(item)))
                    else:
                        item = project_message(row, now)
                        if item['unknown']:
                            self._skip('unknown_tool_outcome')
                        with self._index_errors():
                            self.db.execute('INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?,?,?,?)',
                                tuple(item[k] for k in ('message_id', 'session_id', 'at', 'responses', 'tools',
                                                       'errors', 'unknown', 'duration')))
                except (ValueError, TypeError, KeyError) as error:
                    self._skip(str(error))
            with self._index_errors():
                self.db.commit()
            try:
                index_bytes = sum(p.stat().st_size for p in self.state.glob('bob.sqlite3*'))
            except OSError:
                raise RuntimeError('Bob collector storage unavailable') from None
            if index_bytes > MAX_INDEX_BYTES:
                raise OverflowError('index capacity')
        except (OSError, sqlite3.Error, ValueError, OverflowError) as error:
            self.value.update(source_available=0, collection_complete=0, pending=0,
                              source_errors=1, limit_reached=int(isinstance(error, OverflowError) or time.monotonic() > self.deadline))
            if self.source is not None:
                self.identity_reader = None
                self.pending_identity = None
                self.source.close()
                self.source = None
            self.stage = None
            self.data_version = None
            self.db.rollback()
        return self.snapshot(now=now)

    def snapshot(self, *, now=None):
        now = time.time() if now is None else now
        result = {**self.value}
        eligible = [s for s in self.value['sessions'] if not self.retention or
                    now - (s['last_event'] if s['last_event'] is not None else s['created_at']) <= self.retention]
        eligible.sort(key=lambda s: (-(s['last_event'] or s['created_at']), s['session_id']))
        result.update(sessions=eligible[:self.cap], expired_sessions=len(self.value['sessions']) - len(eligible),
                      cap_omitted_sessions=max(0, len(eligible) - self.cap),
                      retention_seconds=self.retention, export_cap=self.cap)
        return result


HEALTH = {
    'source_available': 'One when the Bob source was readable on the latest scan.',
    'scan_timestamp_seconds': 'Latest scan attempt, Unix seconds.',
    'last_success_timestamp_seconds': 'Latest successful collection check, Unix seconds; older message bodies are reconciled every five minutes.',
    'collection_complete': 'One when the latest collection has no known skipped records or pending work.',
    'pending': 'One while records remain to be read in the current scan.',
    'limit_reached': 'One when a scan or storage limit prevented completion.',
    'source_errors': 'Source failures in the latest scan.',
    'indexed_sessions': 'Chats in the last completed scan.',
    'expired_sessions': 'Chats excluded from export by inactivity retention.',
    'cap_omitted_sessions': 'Eligible chats omitted by the export cap.',
    'retention_seconds': 'Per-source export inactivity window; zero disables expiry.',
    'export_cap': 'Maximum exported Bob chats.',
}
SESSION_METRICS = {
    'last_event_timestamp_seconds': ('last_event', 'Latest saved message time, Unix seconds.'),
    'response_count': ('responses', 'Saved assistant responses excluding local UI messages.'),
    'tool_result_count': ('tool_results', 'Saved tool results, including unknown outcomes.'),
    'tool_error_count': ('tool_errors', 'Saved tool results explicitly marked as errors.'),
    'tool_unknown_count': ('tool_unknown', 'Saved tool results with unknown outcomes.'),
    'tool_duration_seconds': ('tool_seconds', 'Sum of recorded tool durations in seconds.'),
    'timed_tool_count': ('timed_tools', 'Tool results with recorded durations.'),
    'partial': ('partial', 'One when chat usage or tool outcomes have missing or inconsistent fields.'),
}


def render_bob_metrics(snapshot):
    lines = []
    def family(name, help_text):
        lines.extend([f'# HELP {name} {help_text}', f'# TYPE {name} gauge'])
    def emit(name, value, labels=None):
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= SAFE_INTEGER:
            return
        label = ','.join(k + '=' + json.dumps(v) for k, v in (labels or {}).items())
        lines.append(f'{name}{{{label}}} {value}')
    for key, description in HEALTH.items():
        name = 'traceonaut_bob_collector_' + key
        family(name, description)
        source_key = {'scan_timestamp_seconds': 'scan_timestamp', 'last_success_timestamp_seconds': 'last_success'}.get(key, key)
        emit(name, snapshot.get(source_key))
    name = 'traceonaut_bob_collector_skipped_records'
    family(name, 'Skipped records by reason in the latest completed scan.')
    for reason in SKIP_REASONS:
        emit(name, snapshot['skipped_records'].get(reason, 0), {'reason': reason})
    family('traceonaut_bob_session_info', 'Bob chat identity; value one.')
    family('traceonaut_bob_session_usage_tokens', 'Recorded chat tokens after removing completed subtask usage from parents; cache is included in input.')
    for suffix, (_, description) in SESSION_METRICS.items():
        family('traceonaut_bob_session_' + suffix, description)
    for row in snapshot['sessions']:
        labels = {key: row[key] for key in ('project_id', 'session_id')}
        emit('traceonaut_bob_session_info', 1, {**labels, 'kind': row['kind']})
        for suffix, (key, _) in SESSION_METRICS.items():
            emit('traceonaut_bob_session_' + suffix, row.get(key), labels)
        for kind, value in row['usage'].items():
            emit('traceonaut_bob_session_usage_tokens', value, {**labels, 'token_kind': kind})
    return ('\n'.join(lines) + '\n').encode()
