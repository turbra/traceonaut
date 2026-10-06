"""Optional, bounded reader for the sanitized Bob 2.0.5 generation journal.

The existing Bob worker owns the connection and writer lock. Only projected
identities, numeric usage and cursor state enter its additive SQLite tables.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import time

from .collector_paths import checked_path

SAFE_INTEGER = 2**53 - 1
PRODUCER_VERSION = '2.0.5'
MAX_LINE_BYTES = 1024**2
MAX_FILES = 64
MAX_EVENTS = 100_000
MAX_INDEX_BYTES = 512 * 1024**2
FIELDS = {'gen_ai.usage.input_tokens': 'input', 'gen_ai.usage.output_tokens': 'output',
          'gen_ai.usage.cache_read.input_tokens': 'cached_input',
          'gen_ai.usage.cache_creation.input_tokens': 'cache_write_input',
          'gen_ai.usage.reasoning.output_tokens': 'reasoning', 'gen_ai.usage.total_tokens': 'total'}
ATTRIBUTES = {*FIELDS, 'bob.event.type', 'gen_ai.conversation.id', 'bob.producer.version'}
CAPTURE_STATUS = {'disabled': 0, 'no_activity': 1, 'zero_partial': 2,
                  'missing': 3, 'partial': 4, 'stale': 5}


def integer(value):
    if isinstance(value, str) and re.fullmatch(r'[0-9]{1,16}', value):
        value = int(value)
    return value if type(value) is int and 0 <= value <= SAFE_INTEGER else None


def _object(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('invalid journal')
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=unique,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid journal')))
    if not isinstance(value, dict):
        raise ValueError('invalid journal')
    return value


def project(raw, now):
    """Validate the disk privacy boundary before retaining any event fields."""
    packet = _object(raw)
    if set(packet) != {'resourceSpans'} or not isinstance(packet['resourceSpans'], list):
        raise ValueError('invalid journal')
    rows = []
    for resource in packet['resourceSpans']:
        if set(resource) - {'resource', 'scopeSpans', 'schemaUrl'} or resource.get('schemaUrl'):
            raise ValueError('invalid journal')
        if resource.get('resource', {}) not in ({}, {'attributes': []}):
            raise ValueError('private metadata in journal')
        for scope in resource.get('scopeSpans', []):
            if set(scope) - {'scope', 'spans', 'schemaUrl'} or scope.get('schemaUrl'):
                raise ValueError('invalid journal')
            if any(value for value in scope.get('scope', {}).values()):
                raise ValueError('private metadata in journal')
            for span in scope.get('spans', []):
                if set(span) - {'traceId', 'spanId', 'parentSpanId', 'name', 'kind', 'startTimeUnixNano',
                                'endTimeUnixNano', 'attributes', 'droppedAttributesCount', 'status',
                                'events', 'links', 'traceState', 'flags', 'droppedEventsCount', 'droppedLinksCount'}:
                    raise ValueError('invalid journal')
                if span.get('events') or span.get('links') or span.get('traceState') or span.get('status', {}).get('message'):
                    raise ValueError('private metadata in journal')
                attrs = {}
                for attribute in span.get('attributes', []):
                    if set(attribute) != {'key', 'value'} or attribute['key'] not in ATTRIBUTES or attribute['key'] in attrs:
                        raise ValueError('unexpected journal attribute')
                    attrs[attribute['key']] = attribute['value']
                if span.get('name') != 'LLM Generation' or attrs.get('bob.event.type') != {'stringValue': 'LLM Generation'}:
                    raise ValueError('unsupported journal event')
                conversation = attrs.get('gen_ai.conversation.id', {}).get('stringValue')
                if attrs.get('gen_ai.conversation.id') != {'stringValue': conversation} or not re.fullmatch(r'[a-f0-9]{64}', conversation or ''):
                    raise ValueError('unhashed journal identity')
                trace, sid = span.get('traceId', '').lower(), span.get('spanId', '').lower()
                if not re.fullmatch(r'[a-f0-9]{32}', trace) or not int(trace, 16) or not re.fullmatch(r'[a-f0-9]{16}', sid) or not int(sid, 16):
                    raise ValueError('invalid event identity')
                end = span.get('endTimeUnixNano')
                if not isinstance(end, str) or not re.fullmatch(r'[0-9]{1,19}', end) or not 0 < int(end) <= int((now + 300) * 1e9):
                    raise ValueError('invalid event time')
                start = span.get('startTimeUnixNano', end)
                if not isinstance(start, str) or not re.fullmatch(r'[0-9]{1,19}', start) or not 0 <= int(start) <= int(end):
                    raise ValueError('invalid event time')
                usage, invalid = {}, False
                for key, target in FIELDS.items():
                    if key in attrs:
                        value = attrs[key]
                        n = integer(value.get('intValue')) if isinstance(value, dict) and set(value) == {'intValue'} else None
                        invalid |= n is None
                        if n is not None:
                            usage[target] = n
                qualified = attrs.get('bob.producer.version') == {'stringValue': PRODUCER_VERSION}
                complete = qualified and not invalid and 'input' in usage and 'output' in usage
                total = usage.get('input', 0) + usage.get('output', 0)
                invalid |= complete and (total > SAFE_INTEGER or ('total' in usage and usage['total'] != total))
                complete &= not invalid
                if complete:
                    usage['total'] = total
                rows.append({'trace': trace, 'span': sid, 'conversation': conversation,
                             'at': int(end) / 1e9, 'usage': usage if complete else {},
                             'state': 'recorded' if complete else 'invalid' if invalid or not qualified else 'missing'})
                if len(rows) > 2000:
                    raise ValueError('journal event bound exceeded')
    return rows


def journal_directory(path):
    """Accept a private rootless writer directory with one named reader/writer ACL."""
    path = Path(os.path.abspath(path))
    checked_path(path.parent)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o007:
        raise ValueError('journal must be a private directory')
    allowed = {info.st_uid, os.geteuid()}
    try:
        acl = os.getxattr(path, 'system.posix_acl_access', follow_symlinks=False)
    except OSError:
        if info.st_mode & 0o077:
            raise ValueError('journal must be private') from None
    else:
        if len(acl) < 4 or struct.unpack('<I', acl[:4])[0] != 2 or (len(acl) - 4) % 8:
            raise ValueError('unsupported journal ACL')
        named = []
        for tag, permissions, uid in struct.iter_unpack('<HHI', acl[4:]):
            if tag in (4, 8, 32) and permissions:  # owning group, named group, other
                raise ValueError('journal ACL is not private')
            if tag == 2:
                named.append(uid)
        if info.st_uid != os.geteuid() and any(uid != os.geteuid() for uid in named):
            raise ValueError('unexpected journal reader')
        if info.st_uid == os.geteuid() and len(named) > 1:
            raise ValueError('unexpected journal writers')
        allowed.update(named)
    return path, allowed


class JournalReader:
    def __init__(self, directory, db, state_dir, *, retention=30 * 86400,
                 max_events=MAX_EVENTS, max_rows=2000, max_bytes=4 * 1024**2, scan_seconds=.5):
        self.directory, self.db = Path(directory), db
        self.marker = Path(state_dir) / 'bob-otel-epoch.json'
        self.retention, self.max_events = retention, max_events
        self.max_rows, self.max_bytes, self.scan_seconds = max_rows, max_bytes, scan_seconds
        self.available, self.backlog, self.errors = 0, 0, 0
        db.executescript('''
            CREATE TABLE IF NOT EXISTS bob_capture_meta (id INTEGER PRIMARY KEY CHECK(id=1),
                version INTEGER NOT NULL, epoch REAL, last_success REAL, losses INTEGER,
                resolve_cursor INTEGER, reset_pending INTEGER, replay_floor REAL);
            CREATE TABLE IF NOT EXISTS bob_capture_files (id TEXT PRIMARY KEY, head TEXT, offset INTEGER,
                size INTEGER, discard INTEGER);
            CREATE TABLE IF NOT EXISTS bob_capture_events (id TEXT PRIMARY KEY, conversation TEXT,
                body TEXT, first_seen REAL, state INTEGER, conflict INTEGER DEFAULT 0);
            CREATE INDEX IF NOT EXISTS bob_capture_pending ON bob_capture_events(state);
            CREATE TABLE IF NOT EXISTS bob_capture_totals (conversation TEXT PRIMARY KEY, body TEXT);
            CREATE TABLE IF NOT EXISTS bob_capture_scan (id INTEGER PRIMARY KEY CHECK(id=1), next_file TEXT);
        ''')
        meta = db.execute('SELECT version,reset_pending,replay_floor FROM bob_capture_meta WHERE id=1').fetchone()
        if meta and meta[0] != 1:
            raise ValueError('unsupported capture state')
        self.reset = bool(meta[1]) if meta else self.marker.exists()
        self.replay_floor = meta[2] if meta else 0
        if not meta:
            now = time.time()
            self.replay_floor = now if self.reset else 0
            db.execute('INSERT INTO bob_capture_meta VALUES (1,1,?,0,?,0,?,?)',
                       (now, int(self.reset), int(self.reset), self.replay_floor))
            db.commit()
        checked_path(self.marker, missing=True, private=True)
        if not self.marker.exists():
            fd = os.open(self.marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump({'version': 1}, stream)

    def _loss(self):
        self.db.execute('UPDATE bob_capture_meta SET losses=min(losses+1,?) WHERE id=1', (SAFE_INTEGER,))

    def _add(self, event):
        conversation = event['conversation']
        saved = self.db.execute('SELECT body FROM bob_capture_totals WHERE conversation=?', (conversation,)).fetchone()
        total = json.loads(saved[0]) if saved else {'usage': {}, 'recorded': 0, 'missing': 0, 'last_event': 0}
        total['last_event'] = max(total['last_event'], event['at'])
        if event['state'] != 'recorded':
            total['missing'] += 1
        else:
            usage = event['usage']
            keys = set(usage) if not total['recorded'] else set(total['usage']) & set(usage)
            added = {key: total['usage'].get(key, 0) + usage[key] for key in keys}
            if any(value > SAFE_INTEGER for value in added.values()):
                self._loss()
                total['missing'] += 1
            else:
                total['usage'] = added
                total['recorded'] += 1
        self.db.execute('INSERT OR REPLACE INTO bob_capture_totals VALUES (?,?)',
                        (conversation, json.dumps(total, sort_keys=True, separators=(',', ':'))))

    def _event(self, event, known, now):
        if self.replay_floor and event['at'] <= self.replay_floor:
            self._loss()
            return  # Lost dedup state cannot authorize older replay in the new epoch.
        key = event['trace'] + ':' + event['span']
        body = json.dumps(event, sort_keys=True, separators=(',', ':'))
        previous = self.db.execute('SELECT body,conflict FROM bob_capture_events WHERE id=?', (key,)).fetchone()
        if previous:
            if previous[0] != body and not previous[1]:
                self.db.execute('UPDATE bob_capture_events SET conflict=1 WHERE id=?', (key,))
                self._loss()
            return
        allocated = (self.db.execute('PRAGMA page_count').fetchone()[0]
                     * self.db.execute('PRAGMA page_size').fetchone()[0])
        if (self.db.execute('SELECT count(*) FROM bob_capture_events').fetchone()[0] >= self.max_events
                or allocated > MAX_INDEX_BYTES - 64 * 1024):
            self._loss()
            return
        joined = event['conversation'] in known
        self.db.execute('INSERT INTO bob_capture_events VALUES (?,?,?,?,?,0)',
                        (key, event['conversation'], body, now, int(joined)))
        if joined:
            self._add(event)

    def _resolve(self, known, now):
        cursor = self.db.execute('SELECT resolve_cursor FROM bob_capture_meta WHERE id=1').fetchone()[0]
        pending = self.db.execute('SELECT rowid,id,conversation,body,first_seen FROM bob_capture_events '
                                  'WHERE state=0 AND rowid>? ORDER BY rowid LIMIT ?', (cursor, self.max_rows)).fetchall()
        if not pending:
            self.db.execute('UPDATE bob_capture_meta SET resolve_cursor=0 WHERE id=1')
            pending = self.db.execute('SELECT rowid,id,conversation,body,first_seen FROM bob_capture_events '
                                      'WHERE state=0 ORDER BY rowid LIMIT ?', (self.max_rows,)).fetchall()
        for rowid, key, conversation, body, seen in pending:
            if self.retention and now - seen > self.retention:
                self.db.execute('UPDATE bob_capture_events SET state=2 WHERE id=?', (key,))
                self._loss()
            elif conversation in known:
                self._add(json.loads(body))
                self.db.execute('UPDATE bob_capture_events SET state=1 WHERE id=?', (key,))
            self.db.execute('UPDATE bob_capture_meta SET resolve_cursor=? WHERE id=1', (rowid,))

    def scan(self, known_sessions, now):
        known = {row['session_id'] for row in known_sessions}
        self.available = self.errors = self.backlog = 0
        deadline = time.monotonic() + self.scan_seconds
        rows = consumed = 0
        try:
            directory, owners = journal_directory(self.directory)
            files = sorted(directory.glob('bob-usage*.json'), key=lambda path: path.lstat().st_mtime_ns)
            if len(files) > MAX_FILES:
                raise ValueError('journal file bound exceeded')
            self.db.execute('BEGIN')
            cursor = self.db.execute('SELECT next_file FROM bob_capture_scan WHERE id=1').fetchone()
            if cursor and cursor[0]:
                for at, path in enumerate(files):
                    info = path.lstat()
                    if cursor[0] == f'{info.st_dev}:{info.st_ino}':
                        files = files[at:] + files[:at]
                        break
            next_file = None
            seen = set()
            for path in files:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, 'rb') as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid not in owners:
                        raise ValueError('unsafe journal file')
                    key = f'{info.st_dev}:{info.st_ino}'
                    seen.add(key)
                    old = self.db.execute('SELECT head,offset,size,discard FROM bob_capture_files WHERE id=?', (key,)).fetchone()
                    if (rows >= self.max_rows or consumed >= self.max_bytes or time.monotonic() >= deadline) and not self.reset:
                        self.backlog += max(0, info.st_size - (old[1] if old else 0))
                        if next_file is None:
                            next_file = key
                        continue
                    first = stream.readline(min(MAX_LINE_BYTES + 1, max(1, self.max_bytes - consumed))) if not self.reset else b''
                    consumed += len(first)
                    head_limited = (bool(first) and len(first) <= MAX_LINE_BYTES and
                                    not first.endswith(b'\n') and stream.tell() < info.st_size)
                    try:
                        # Hash only projected numeric/identity fields, never raw payloads.
                        head = hashlib.sha256(json.dumps(project(first, now), sort_keys=True).encode()).hexdigest() if first.endswith(b'\n') else ''
                    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
                        head = ''
                    offset, discard = (old[1], old[3]) if old else (0, 0)
                    if self.reset:
                        offset, discard = info.st_size, int(bool(info.st_size and not first.endswith(b'\n')))
                        if info.st_size:
                            stream.seek(-1, os.SEEK_END)
                            discard = int(stream.read(1) != b'\n')
                    elif old and (info.st_size < offset or old[0] and head and head != old[0]):
                        self._loss()
                        offset, discard = 0, 0
                    start_offset = offset
                    stream.seek(offset)
                    while rows < self.max_rows and consumed < self.max_bytes and time.monotonic() < deadline:
                        raw = stream.readline(min(MAX_LINE_BYTES + 1, self.max_bytes - consumed + 1))
                        if not raw:
                            break
                        consumed += len(raw)
                        if discard:
                            offset += len(raw)
                            discard = int(not raw.endswith(b'\n'))
                            continue
                        if len(raw) > MAX_LINE_BYTES:
                            self._loss()
                            offset += len(raw)
                            discard = int(not raw.endswith(b'\n'))
                            continue
                        if not raw.endswith(b'\n'):
                            break  # Never advance a partial trailing record.
                        try:
                            events = project(raw, now)
                        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
                            self._loss()
                            events = []
                        for event in events:
                            self._event(event, known, now)
                        rows += max(1, len(events))
                        offset += len(raw)
                    if ((head_limited or offset == start_offset and offset < info.st_size) and
                            (rows >= self.max_rows or consumed >= self.max_bytes or time.monotonic() >= deadline)):
                        # Retry incomplete verification or an unread record with
                        # a full budget; otherwise rotate past completed work.
                        next_file = key
                    self.backlog += max(0, info.st_size - offset)
                    self.db.execute('INSERT OR REPLACE INTO bob_capture_files VALUES (?,?,?,?,?)',
                                    (key, head or (old[0] if old else ''), offset, info.st_size, discard))
            for key, offset, size in self.db.execute('SELECT id,offset,size FROM bob_capture_files').fetchall():
                if key not in seen:
                    if offset < size:
                        self._loss()
                    self.db.execute('DELETE FROM bob_capture_files WHERE id=?', (key,))
            self._resolve(known, now)
            self.db.execute('INSERT OR REPLACE INTO bob_capture_scan VALUES (1,?)', (next_file,))
            self.db.execute('UPDATE bob_capture_meta SET last_success=?,reset_pending=0 WHERE id=1', (now,))
            self.db.commit()
            self.reset, self.available = False, 1
        except (OSError, ValueError, TypeError):
            self.db.rollback()
            self.errors = 1

    def snapshot(self, sessions):
        epoch, success, losses = self.db.execute('SELECT epoch,last_success,losses FROM bob_capture_meta WHERE id=1').fetchone()
        health = {'enabled': 1, 'source_available': self.available, 'last_success': success,
                  'epoch': epoch, 'losses': losses, 'errors': self.errors, 'backlog_bytes': self.backlog,
                  'pending_joins': self.db.execute('SELECT count(*) FROM bob_capture_events WHERE state=0').fetchone()[0],
                  'scope_partial': 1}
        for row in sessions:
            saved = self.db.execute('SELECT body FROM bob_capture_totals WHERE conversation=?', (row['session_id'],)).fetchone()
            total = json.loads(saved[0]) if saved else {'usage': {}, 'recorded': 0, 'missing': 0, 'last_event': 0}
            status = ('stale' if not self.available else 'zero_partial' if total['recorded'] and total['usage']['total'] == 0
                      else 'partial' if total['recorded'] else 'missing' if total['missing'] or row.get('last_event') is not None or row.get('responses') or row.get('tool_results')
                      else 'no_activity')
            row.update(captured_usage=total['usage'], capture_status=CAPTURE_STATUS[status],
                       captured_events=total['recorded'], capture_missing_events=total['missing'],
                       capture_last_event=total['last_event'])
        return health
