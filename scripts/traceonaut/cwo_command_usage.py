"""Bounded command-name counts from completed CWO-associated shell invocations."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import time

from .shell_command_names import command_names
from .codex_session_telemetry import MAX_LINE, _open_source, _uuid

RETENTION_SECONDS = 30 * 86400
CALL_CAP = 10000
INDEX_CAP = 100000
MAX_FILES = 4096
MAX_SCAN_BYTES = 64 * 1024 * 1024
PER_FILE_BYTES = 32 * 1024 * 1024
PREFIX = "cwo_codex_cwo_shell_"
METRICS = {
    PREFIX + "command_timestamp_seconds": (("project_id", "session_id", "observation_id", "command"), "Recorded command completion time; zero withdraws a conflicting request."),
    PREFIX + "session_complete": (("project_id", "session_id"), "Selected session command coverage: 1 complete, 0 partial for missing sources or unsupported shell syntax."),
    PREFIX + "scan_timestamp_seconds": ((), "Latest command source scan attempt, Unix seconds."),
    PREFIX + "scan_ready": ((), "Associated command sources are readable and caught up."),
    PREFIX + "pending_files": ((), "Associated rollout files with command records still to read."),
    PREFIX + "source_errors": ((), "Command source access or identity failures in the latest scan."),
    PREFIX + "source_gaps": ((), "Retained malformed, unsupported or conflicting command records."),
    PREFIX + "limit_reached": ((), "Command source or retained request bound reached."),
    PREFIX + "history_floor_timestamp_seconds": ((), "Exclude requests at or before this time after identity or withdrawal overflow."),
}


def candidate(raw):
    return b'"item_completed"' in raw and b'"CommandExecution"' in raw


def oversized_candidate(raw):
    # Only a positively identified top-level unrelated record is safe to skip.
    # Item type can follow arbitrarily large output in a completed event.
    prefix = re.match(rb'\s*\{\s*(?:"timestamp"\s*:\s*"[^"\\]*"\s*,\s*)?"type"\s*:\s*"([a-z_]+)"', raw)
    return not prefix or prefix[1] not in {b'response_item', b'session_meta', b'turn_context'}


def request(record, sid):
    """Completed shell invocation metadata, without arguments or output."""
    if record.get("type") != "event_msg":
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "item_completed":
        return None
    item = payload.get("item")
    if not isinstance(item, dict) or item.get("type") != "CommandExecution":
        return None
    owner = _uuid(payload.get("thread_id"))
    if owner is None:
        raise ValueError("missing command owner")
    if owner != sid:
        return None
    identity, turn = item.get("id"), payload.get("turn_id")
    if not all(isinstance(v, str) and v.strip() and len(v) <= 512 for v in (identity, turn)):
        raise ValueError("missing command identity")
    completed = payload.get("completed_at_ms")
    if type(completed) not in (int, float) or not math.isfinite(completed) or completed <= 0:
        raise ValueError("missing command completion time")
    names, complete = command_names(item.get("command"))
    return json.dumps([turn, identity], separators=(",", ":")), names, completed / 1000, complete


class CommandCollector:
    """Additive tables and independent cursors under the existing CWO writer lock."""

    def __init__(self, cwo):
        self.home, self.db = cwo.home, cwo.db
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS shell_files(path TEXT PRIMARY KEY,sid TEXT,device INTEGER,inode INTEGER,
                size INTEGER,changed INTEGER,offset INTEGER,skip INTEGER,gaps INTEGER,gap_until REAL,
                boundary TEXT,association REAL);
            CREATE TABLE IF NOT EXISTS shell_calls(id TEXT PRIMARY KEY,sid TEXT,at REAL,command TEXT,conflict INTEGER,execution_id TEXT);
            CREATE INDEX IF NOT EXISTS shell_calls_time ON shell_calls(at);
            CREATE INDEX IF NOT EXISTS shell_calls_execution ON shell_calls(execution_id);
            CREATE TABLE IF NOT EXISTS shell_runs(id TEXT PRIMARY KEY,at REAL,fingerprint TEXT,conflict INTEGER);
            CREATE TABLE IF NOT EXISTS shell_limits(sid TEXT PRIMARY KEY,dropped_at REAL);
            CREATE TABLE IF NOT EXISTS shell_watermark(key TEXT PRIMARY KEY,at REAL);
        ''')

    @staticmethod
    def _boundary(stream, offset):
        stream.seek(max(0, offset - 1024))
        return hashlib.sha256(stream.read(min(offset, 1024))).hexdigest()

    def _consume(self, raw, sid, boundary, now):
        if not candidate(raw):
            return 0
        try:
            value = request(json.loads(raw), sid)
            if value is None:
                return 0
            identity, names, at, complete = value
            at = round(at, 6)
            if at < boundary or at < now - RETENTION_SECONDS:
                return 0
            if at > now:
                return 1
            execution = hashlib.sha256(json.dumps([sid, identity], separators=(",", ":")).encode()).hexdigest()
            fingerprint = hashlib.sha256(json.dumps([at, names], separators=(",", ":")).encode()).hexdigest()
            prior = self.db.execute("SELECT fingerprint FROM shell_runs WHERE id=?", (execution,)).fetchone()
            if prior:
                if prior["fingerprint"] != fingerprint:
                    self.db.execute("UPDATE shell_runs SET conflict=1 WHERE id=?", (execution,))
                    self.db.execute("UPDATE shell_calls SET conflict=1 WHERE execution_id=?", (execution,))
            else:
                self.db.execute("INSERT INTO shell_runs VALUES(?,?,?,0)", (execution, at, fingerprint))
                for position, name in enumerate(names):
                    key = hashlib.sha256(f"{execution}:{position}".encode()).hexdigest()
                    self.db.execute("INSERT INTO shell_calls VALUES(?,?,?,?,0,?)", (key, sid, at, name, execution))
            return int(not complete)
        except (ValueError, TypeError, AttributeError, RecursionError):
            return 1

    def scan(self, index, snapshot, *, now=None, byte_budget=MAX_SCAN_BYTES, per_file_budget=PER_FILE_BYTES, cap=CALL_CAP):
        now = round(time.time() if now is None else now, 6)
        sessions = {r['session_id']: r for r in snapshot['sessions']}
        associated = {r['session_id']: r for r in snapshot.get('cwo_sessions', {}).get('associations', [])
                      if r['session_id'] in sessions}
        sources = [dict(r) for r in index.execute('SELECT path,session_id FROM files ORDER BY modified DESC,path')
                   if r['session_id'] in associated]
        limited = len(sources) > MAX_FILES
        sources = sources[:MAX_FILES]
        remaining, pending, errors = byte_budget, 0, 0
        bad_sessions, found = set(), set()
        with self.db:
            paths = {r['path'] for r in sources}
            for row in self.db.execute('SELECT path FROM shell_files').fetchall():
                if row[0] not in paths:
                    self.db.execute('DELETE FROM shell_files WHERE path=?', (row[0],))
            for source in sources:
                path, sid = source['path'], source['session_id']
                created = sessions[sid].get('created')
                if type(created) not in (int, float) or created <= 0:
                    bad_sessions.add(sid)
                    errors += 1
                    continue
                boundary = max(created, associated[sid]['timestamp'])
                relative = Path(path)
                if relative.is_absolute() or '..' in relative.parts:
                    bad_sessions.add(sid); errors += 1; continue
                try:
                    with _open_source(self.home / relative) as stream:
                        info = os.fstat(stream.fileno())
                        old = self.db.execute('SELECT * FROM shell_files WHERE path=?', (path,)).fetchone()
                        reset = (old is None or old['sid'] != sid or old['association'] != boundary
                                 or (old['device'], old['inode']) != (info.st_dev, info.st_ino)
                                 or info.st_size < old['offset']
                                 or (info.st_size == old['size'] and info.st_ctime_ns != old['changed']))
                        if old is not None and not reset:
                            reset = self._boundary(stream, old['offset']) != old['boundary']
                        offset, skip, gaps, gap_until = (0, 0, 0, 0) if reset else (old['offset'], old['skip'], old['gaps'], old['gap_until'])
                        stream.seek(0)
                        header = json.loads(stream.readline(MAX_LINE + 1))
                        if header.get('type') != 'session_meta' or _uuid(header.get('payload', {}).get('id')) != sid:
                            raise ValueError('source identity mismatch')
                        found.add(sid)
                        stream.seek(offset)
                        spent = 0
                        while offset < info.st_size and remaining > 0 and spent < per_file_budget:
                            position = stream.tell()
                            raw = stream.readline(min(MAX_LINE + 1, remaining, per_file_budget - spent, info.st_size - position))
                            if not raw:
                                break
                            spent += len(raw); remaining -= len(raw)
                            if not raw.endswith(b'\n'):
                                if len(raw) > MAX_LINE or skip:
                                    if not skip and oversized_candidate(raw):
                                        gaps += 1; gap_until = now + RETENTION_SECONDS
                                    skip = 1; offset = stream.tell(); continue
                                stream.seek(position); break
                            if skip:
                                skip = 0
                            else:
                                added = self._consume(raw, sid, boundary, now)
                                gaps += added
                                if added: gap_until = now + RETENTION_SECONDS
                            offset = stream.tell()
                        self.db.execute('INSERT OR REPLACE INTO shell_files VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                            (path, sid, info.st_dev, info.st_ino, info.st_size, info.st_ctime_ns,
                             offset, skip, gaps, gap_until, self._boundary(stream, offset), boundary))
                        unfinished = offset < info.st_size or bool(skip)
                        pending += int(unfinished)
                        if unfinished or (gaps and gap_until >= now):
                            bad_sessions.add(sid)
                except (OSError, ValueError, TypeError, AttributeError, RecursionError):
                    errors += 1; bad_sessions.add(sid)
            bad_sessions.update(set(associated) - found)
            self.db.execute('DELETE FROM shell_calls WHERE at<?', (now - RETENTION_SECONDS,))
            self.db.execute('DELETE FROM shell_runs WHERE at<?', (now - RETENTION_SECONDS,))
            self.db.execute('DELETE FROM shell_limits WHERE dropped_at<?', (now - RETENTION_SECONDS,))
            # Apply the cap to eligible current associations, after source-time filtering.
            eligible = [dict(r) for r in self.db.execute('SELECT * FROM shell_calls ORDER BY at DESC,id')
                        if r['sid'] in associated and r['at'] >= max(sessions[r['sid']].get('created') or 0, associated[r['sid']]['timestamp'])]
            # Withdrawals must reach Prometheus even when newer positive calls
            # fill the cap. If withdrawals alone overflow, exclude the affected
            # older interval explicitly instead of resurrecting positive history.
            eligible.sort(key=lambda r: (not r['conflict'], -r['at'], r['id']))
            for row in eligible[cap:]:
                self.db.execute('INSERT INTO shell_limits VALUES(?,?) ON CONFLICT(sid) DO UPDATE SET dropped_at=max(dropped_at,excluded.dropped_at)', (row['sid'], row['at']))
                if row['conflict']:
                    self.db.execute("INSERT INTO shell_watermark VALUES('floor',?) ON CONFLICT(key) DO UPDATE SET at=max(at,excluded.at)", (row['at'],))
            # Keep identity evidence beyond the exposition cap: a copied record
            # or late conflict must not become a new invocation after eviction.
            # The independent storage bound has an explicit history floor.
            stored = self.db.execute('SELECT id,at,sid FROM shell_calls ORDER BY conflict DESC,at DESC,id').fetchall()
            for row in stored[INDEX_CAP:]:
                self.db.execute("INSERT INTO shell_watermark VALUES('floor',?) ON CONFLICT(key) DO UPDATE SET at=max(at,excluded.at)", (row['at'],))
                self.db.execute('INSERT INTO shell_limits VALUES(?,?) ON CONFLICT(sid) DO UPDATE SET dropped_at=max(dropped_at,excluded.dropped_at)', (row['sid'], row['at']))
                self.db.execute('DELETE FROM shell_calls WHERE id=?', (row['id'],))
            # Every retained occurrence keeps its invocation fingerprint, even
            # when an old conflict takes priority over newer positive records.
            runs = self.db.execute('''SELECT id,at FROM shell_runs ORDER BY
                EXISTS(SELECT 1 FROM shell_calls WHERE execution_id=shell_runs.id) DESC,
                conflict DESC,at DESC,id''').fetchall()
            for row in runs[INDEX_CAP:]:
                self.db.execute("INSERT INTO shell_watermark VALUES('floor',?) ON CONFLICT(key) DO UPDATE SET at=max(at,excluded.at)", (row['at'],))
                self.db.execute('DELETE FROM shell_runs WHERE id=?', (row['id'],))
            for row in self.db.execute('SELECT sid FROM shell_limits WHERE dropped_at>=?', (now - RETENTION_SECONDS,)):
                if row[0] in associated:
                    bad_sessions.add(row[0]); limited = True
        calls = eligible[:cap]
        for row in calls:
            if row['conflict']:
                bad_sessions.add(row['sid'])
        gaps = sum(r[0] for r in self.db.execute('SELECT gaps FROM shell_files WHERE gap_until>=?', (now,))) + sum(r['conflict'] for r in calls)
        ready = bool(snapshot['source_available']) and not snapshot['pending_files'] and not pending and not errors
        ready = ready and bool(snapshot.get('cwo_sessions', {}).get('scan_ready')) and not (set(associated) - found)
        floor = self.db.execute("SELECT at FROM shell_watermark WHERE key='floor'").fetchone()
        return {'scan_timestamp_seconds': now, 'scan_ready': int(ready), 'pending_files': pending,
                'source_errors': errors, 'source_gaps': gaps, 'limit_reached': int(limited),
                'history_floor_timestamp_seconds': floor[0] if floor else 0,
                'sessions': [{'project_id': row['project_id'], 'session_id': sid,
                              'complete': int(ready and sid not in bad_sessions and not limited)}
                             for sid, row in sorted(associated.items())],
                'calls': [{**row, 'project_id': associated[row['sid']]['project_id'],
                           'timestamp': 0 if row['conflict'] else row['at']} for row in calls]}


def render_command_usage_metrics(snapshot):
    lines = []
    for name, (_, help_text) in METRICS.items():
        lines.extend((f'# HELP {name} {help_text}', f'# TYPE {name} gauge'))
        if name.endswith('command_timestamp_seconds'):
            values = [({'project_id': r['project_id'], 'session_id': r['sid'],
                        'observation_id': r['id'], 'command': r['command']}, r['timestamp']) for r in snapshot['calls']]
        elif name.endswith('session_complete'):
            values = [({k: r[k] for k in ('project_id', 'session_id')}, r['complete']) for r in snapshot['sessions']]
        else:
            values = [({}, snapshot[name.removeprefix(PREFIX)])]
        for labels, value in values:
            label_text = '{' + ','.join(k + '=' + json.dumps(v) for k, v in labels.items()) + '}' if labels else ''
            lines.append(name + label_text + ' ' + str(value))
    return ('\n'.join(lines) + '\n').encode()
