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
from .command_details import invocation_details, safe_text
from .command_network import network_details
from .codex_session_telemetry import MAX_LINE, _open_source, _uuid, session_export_rows

RETENTION_SECONDS = 30 * 86400
FUTURE_RETRY_SECONDS = 300
CALL_CAP = 10000
INDEX_CAP = 100000
HISTORY_CAP = 1000
NETWORK_CAP = 1000
MAX_FILES = 4096
MAX_SCAN_BYTES = 64 * 1024 * 1024
PER_FILE_BYTES = 32 * 1024 * 1024
PREFIX = "cwo_codex_cwo_shell_"
METRICS = {
    PREFIX + "command_timestamp_seconds": (("project_id", "session_id", "observation_id", "command"), "Recorded command completion time; zero withdraws a conflicting request."),
    PREFIX + "action_timestamp_seconds": (("project_id", "session_id", "observation_id", "action"), "Recorded command action completion time; zero withdraws a conflicting occurrence."),
    PREFIX + "invocation_timestamp_seconds": (("project_id", "session_id", "execution_id"), "Opt-in history invocation completion time; zero withdraws conflicting evidence."),
    PREFIX + "invocation_exit_code": (("project_id", "session_id", "execution_id"), "Recorded exit code of the complete invocation."),
    PREFIX + "invocation_duration_seconds": (("project_id", "session_id", "execution_id"), "Recorded duration of the complete invocation."),
    PREFIX + "invocation_outcome": (("project_id", "session_id", "execution_id"), "Invocation outcome: 0 unknown, 1 completed, 2 failed."),
    PREFIX + "invocation_info": (("project_id", "session_id", "execution_id", "command_description", "recorded_directory", "session_name", "project_name"), "Opt-in sanitized command description and recorded directory for a retained invocation."),
    PREFIX + "network_info": (("project_id", "session_id", "execution_id", "command_description", "session_name", "project_name", "destination", "source_ip", "source_ip_evidence", "network_index", "http_method", "payload_kind", "payload_files", "upload_options"), "Opt-in requested curl method, payload source, endpoint and explicitly recorded local socket IP."),
    PREFIX + "scope": (("scope",), "Command collection scope: cwo-associated sessions or all exported Codex sessions."),
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

    def __init__(self, cwo, *, history=False, scope='cwo'):
        if scope not in {'cwo', 'all'} or (scope == 'all' and not history):
            raise ValueError('all-session command scope requires command history')
        self.home, self.db = cwo.home, cwo.db
        self.history, self.scope = history, scope
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
            CREATE TABLE IF NOT EXISTS shell_history(id TEXT PRIMARY KEY,sid TEXT,at REAL,summary TEXT,directory TEXT,
                exit_code INTEGER,duration REAL,outcome TEXT);
        ''')
        with self.db:
            if 'action' not in {r[1] for r in self.db.execute('PRAGMA table_info(shell_calls)')}:
                self.db.execute('ALTER TABLE shell_calls ADD COLUMN action TEXT')
            if 'projection' not in {r[1] for r in self.db.execute('PRAGMA table_info(shell_runs)')}:
                self.db.execute('ALTER TABLE shell_runs ADD COLUMN projection TEXT')
            if 'network_projection' not in {r[1] for r in self.db.execute('PRAGMA table_info(shell_runs)')}:
                self.db.execute('ALTER TABLE shell_runs ADD COLUMN network_projection TEXT')
            if 'network_projection_version' not in {r[1] for r in self.db.execute('PRAGMA table_info(shell_runs)')}:
                self.db.execute('ALTER TABLE shell_runs ADD COLUMN network_projection_version INTEGER')
            if 'network' not in {r[1] for r in self.db.execute('PRAGMA table_info(shell_history)')}:
                self.db.execute("ALTER TABLE shell_history ADD COLUMN network TEXT NOT NULL DEFAULT '[]'")
            version = self.db.execute("SELECT at FROM shell_watermark WHERE key='projection'").fetchone()
            setting = self.db.execute("SELECT at FROM shell_watermark WHERE key='history_enabled'").fetchone()
            previous_scope = self.db.execute("SELECT at FROM shell_watermark WHERE key='all_sessions'").fetchone()
            scope_changed = bool(previous_scope and previous_scope[0]) != (scope == 'all')
            if version is None or version[0] != 4 or scope_changed or (history and (setting is None or not setting[0])):
                # Rewind only feature-owned cursors; original records and other
                # collectors' progress remain untouched. Identities deduplicate.
                self.db.execute('DELETE FROM shell_files')
            self.db.execute("INSERT OR REPLACE INTO shell_watermark VALUES('projection',4)")
            self.db.execute("INSERT OR REPLACE INTO shell_watermark VALUES('all_sessions',?)", (int(scope == 'all'),))
            self.db.execute("INSERT OR REPLACE INTO shell_watermark VALUES('history_enabled',?)", (int(history),))
            if not history:
                self.db.execute('DELETE FROM shell_history')

    @staticmethod
    def _boundary(stream, offset):
        stream.seek(max(0, offset - 1024))
        return hashlib.sha256(stream.read(min(offset, 1024))).hexdigest()

    def _consume(self, raw, sid, boundary, now):
        if not candidate(raw):
            return 0
        try:
            record = json.loads(raw)
            value = request(record, sid)
            if value is None:
                return 0
            identity, names, at, complete = value
            at = round(at, 6)
            if at < boundary or at < now - RETENTION_SECONDS:
                return 0
            if at > now:
                return -1 if at - now <= FUTURE_RETRY_SECONDS else 1
            execution = hashlib.sha256(json.dumps([sid, identity], separators=(",", ":")).encode()).hexdigest()
            fingerprint = hashlib.sha256(json.dumps([at, names], separators=(",", ":")).encode()).hexdigest()
            item = record['payload']['item']
            actions, _ = command_names(item.get('command'), actions=True)
            projection = hashlib.sha256(json.dumps([at, item.get('command'), item.get('cwd'),
                item.get('status'), item.get('exit_code'), item.get('duration')], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            network = network_details(item) if self.history else []
            network_json = json.dumps(network, sort_keys=True, separators=(',', ':'))
            network_projection = hashlib.sha256(network_json.encode()).hexdigest() if self.history else None
            prior = self.db.execute("SELECT fingerprint,projection,network_projection,network_projection_version,conflict FROM shell_runs WHERE id=?", (execution,)).fetchone()
            network_changed = bool(self.history and prior and prior['network_projection'] is not None
                                   and prior['network_projection'] != network_projection)
            if network_changed and prior['network_projection_version'] != 4:
                # Compare the old projection before adding new derived fields.
                # An old empty projection has no endpoint/IP evidence to contradict.
                legacy = [{k: r[k] for k in ('command', 'destination', 'source_ip',
                           'source_ip_evidence', 'destination_kind')} for r in network]
                legacy_hash = hashlib.sha256(json.dumps(legacy, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                network_changed = prior['network_projection'] not in {legacy_hash, hashlib.sha256(b'[]').hexdigest()}
            conflict = bool(prior and (prior['conflict'] or prior['fingerprint'] != fingerprint
                                      or (prior['projection'] is not None and prior['projection'] != projection)
                                      or network_changed))
            if prior:
                if conflict:
                    self.db.execute("UPDATE shell_runs SET conflict=1 WHERE id=?", (execution,))
                    self.db.execute("UPDATE shell_calls SET conflict=1 WHERE execution_id=?", (execution,))
            else:
                self.db.execute("INSERT INTO shell_runs(id,at,fingerprint,conflict,projection) VALUES(?,?,?,0,?)", (execution, at, fingerprint, projection))
                for position, name in enumerate(names):
                    key = hashlib.sha256(f"{execution}:{position}".encode()).hexdigest()
                    self.db.execute("INSERT INTO shell_calls(id,sid,at,command,conflict,execution_id,action) VALUES(?,?,?,?,0,?,?)", (key, sid, at, name, execution, actions[position]))
            if not conflict:
                self.db.execute('UPDATE shell_runs SET projection=? WHERE id=?', (projection, execution))
                for position, action in enumerate(actions):
                    key = hashlib.sha256(f"{execution}:{position}".encode()).hexdigest()
                    self.db.execute('UPDATE shell_calls SET action=? WHERE id=? AND action IS NULL', (action, key))
                if self.history:
                    self.db.execute('UPDATE shell_runs SET network_projection=?,network_projection_version=4 WHERE id=?', (network_projection, execution))
                    details = invocation_details(item)
                    self.db.execute('INSERT OR REPLACE INTO shell_history VALUES(?,?,?,?,?,?,?,?,?)',
                        (execution, sid, at, details['summary'], details['directory'], details['exit_code'], details['duration'], details['outcome'], network_json))
            elif self.history:
                self.db.execute("UPDATE shell_history SET summary=NULL,directory=NULL,exit_code=NULL,duration=NULL,network='[]',outcome=? WHERE id=?", ('unknown', execution))
            return int(not complete)
        except (ValueError, TypeError, AttributeError, RecursionError):
            return 1

    def scan(self, index, snapshot, *, now=None, byte_budget=MAX_SCAN_BYTES, per_file_budget=PER_FILE_BYTES, cap=CALL_CAP):
        now = round(time.time() if now is None else now, 6)
        sessions = {r['session_id']: r for r in snapshot['sessions']}
        associated = {r['session_id']: r for r in snapshot.get('cwo_sessions', {}).get('associations', [])
                      if r['session_id'] in sessions}
        if self.scope == 'all':
            policy = snapshot.get('session_export')
            if policy is not None:
                selected, _ = session_export_rows(list(sessions.values()), now=policy['evaluated_at'],
                    retention_seconds=policy['retention_seconds'], cap=policy['cap'])
                sessions = {r['session_id']: r for r in selected}
            associated = {sid: {'session_id': sid, 'project_id': row['project_id'],
                               'timestamp': row.get('created') or 0} for sid, row in sessions.items()}
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
                                if added < 0:
                                    offset = position
                                    break
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
            self.db.execute('DELETE FROM shell_history WHERE at<?', (now - RETENTION_SECONDS,))
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
            for row in self.db.execute('SELECT id,sid,at FROM shell_history').fetchall():
                if row['sid'] not in associated or row['at'] < max(sessions[row['sid']].get('created') or 0, associated[row['sid']]['timestamp']):
                    self.db.execute('DELETE FROM shell_history WHERE id=?', (row['id'],))
            self.db.execute('DELETE FROM shell_history WHERE id NOT IN (SELECT id FROM shell_history ORDER BY at DESC,id LIMIT ?)', (HISTORY_CAP,))
            runs = self.db.execute('''SELECT id,at FROM shell_runs ORDER BY
                EXISTS(SELECT 1 FROM shell_history WHERE id=shell_runs.id) DESC,
                EXISTS(SELECT 1 FROM shell_calls WHERE execution_id=shell_runs.id) DESC,
                conflict DESC,at DESC,id''').fetchall()
            for row in runs[INDEX_CAP + (HISTORY_CAP if self.history else 0):]:
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
        ready = ready and (self.scope == 'all' or bool(snapshot.get('cwo_sessions', {}).get('scan_ready'))) and not (set(associated) - found)
        floor = self.db.execute("SELECT at FROM shell_watermark WHERE key='floor'").fetchone()
        history = []
        if self.history:
            for row in self.db.execute('SELECT h.*,r.conflict FROM shell_history h JOIN shell_runs r ON r.id=h.id ORDER BY h.at DESC,h.id'):
                if row['sid'] in associated and row['at'] >= max(sessions[row['sid']].get('created') or 0, associated[row['sid']]['timestamp']):
                    history.append({**dict(row), 'project_id': associated[row['sid']]['project_id'],
                                    'session_name': safe_text(sessions[row['sid']].get('title'), 256) or '',
                                    'project_name': safe_text(sessions[row['sid']].get('project_name'), 256) or '',
                                    'network': json.loads(row['network']),
                                    'timestamp': 0 if row['conflict'] else row['at']})
        if sum(len(r['network']) for r in history if r['timestamp'] > 0) > NETWORK_CAP:
            limited = True
        return {'scope': self.scope, 'scan_timestamp_seconds': now, 'scan_ready': int(ready), 'pending_files': pending,
                'source_errors': errors, 'source_gaps': gaps, 'limit_reached': int(limited),
                'history_floor_timestamp_seconds': floor[0] if floor else 0,
                'history': {'enabled': self.history, 'scope': self.scope, 'limit': HISTORY_CAP, 'rows': history},
                'sessions': [{'project_id': row['project_id'], 'session_id': sid,
                              'complete': int(ready and sid not in bad_sessions and not limited)}
                             for sid, row in sorted(associated.items())],
                'calls': [{**row, 'project_id': associated[row['sid']]['project_id'],
                           'timestamp': 0 if row['conflict'] else row['at']} for row in calls]}


def render_command_usage_metrics(snapshot):
    lines = []
    history = snapshot.get('history', {})
    rows = history.get('rows', [])[:HISTORY_CAP] if history.get('enabled') else []
    for name, (_, help_text) in METRICS.items():
        lines.extend((f'# HELP {name} {help_text}', f'# TYPE {name} gauge'))
        if name.endswith('_scope'):
            values = [({'scope': snapshot.get('scope', 'cwo')}, 1)]
        elif name.endswith('action_timestamp_seconds'):
            values = [({'project_id': r['project_id'], 'session_id': r['sid'],
                        'observation_id': r['id'], 'action': r['action']}, r['timestamp']) for r in snapshot['calls'] if r.get('action')]
        elif name.endswith(('invocation_info', 'network_info')):
            values = []
            for row in rows:
                if row['timestamp'] <= 0:
                    continue
                labels = {'project_id': row['project_id'], 'session_id': row['sid'],
                          'execution_id': row['id'],
                          'command_description': row.get('summary') or 'Not recorded',
                          'session_name': row.get('session_name') or 'Name unavailable',
                          'project_name': row.get('project_name') or 'Project unavailable'}
                if name.endswith('invocation_info'):
                    values.append(({**labels, 'recorded_directory': row.get('directory') or 'Not recorded'}, 1))
                else:
                    for index, network in enumerate(row.get('network', [])[:8]):
                        if len(values) >= NETWORK_CAP:
                            break
                        values.append(({**labels, 'destination': network['destination'],
                            'source_ip': network.get('source_ip') or 'Not recorded',
                            'source_ip_evidence': network['source_ip_evidence'],
                            'http_method': network.get('http_method') or 'Unknown',
                            'payload_kind': network.get('payload_kind') or 'Unknown',
                            'payload_files': network.get('payload_files') or 'Not recorded',
                            'upload_options': network.get('upload_options') or 'None',
                            'network_index': str(index)}, 1))
        elif '_invocation_' in name:
            field = {'timestamp_seconds': 'timestamp', 'exit_code': 'exit_code', 'duration_seconds': 'duration', 'outcome': 'outcome'}[name.split('_invocation_')[1]]
            values = []
            for row in rows:
                value = row[field]
                if field == 'outcome':
                    value = {'unknown': 0, 'completed': 1, 'failed': 2}[value]
                if value is not None:
                    values.append(({'project_id': row['project_id'], 'session_id': row['sid'], 'execution_id': row['id']}, value))
        elif name.endswith('command_timestamp_seconds'):
            values = [({'project_id': r['project_id'], 'session_id': r['sid'],
                        'observation_id': r['id'], 'command': r['command']}, r['timestamp']) for r in snapshot['calls']]
        elif name.endswith('session_complete'):
            values = [({k: r[k] for k in ('project_id', 'session_id')}, r['complete']) for r in snapshot['sessions']]
        else:
            values = [({}, snapshot[name.removeprefix(PREFIX)])]
        for labels, value in values:
            label_text = '{' + ','.join(k + '=' + json.dumps(v, ensure_ascii=False) for k, v in labels.items()) + '}' if labels else ''
            lines.append(name + label_text + ' ' + str(value))
    return ('\n'.join(lines) + '\n').encode()
