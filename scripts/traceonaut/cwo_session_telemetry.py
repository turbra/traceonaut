"""CWO session association from structured skill blocks and direct tool calls.

Association describes a session's context, not ownership of every token or turn.
Source text is inspected transiently and never retained in this private index.
"""
from __future__ import annotations

from collections import defaultdict
import fcntl
import hashlib
import heapq
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import stat
import time

from .codex_session_telemetry import (
    MAX_LINE, _number, _open_source, _safe_path, _timestamp, _uuid, session_export_rows,
)

RETENTION_SECONDS = 30 * 86400
COMMAND_CAP = 2000
MAX_FILES = 4096
MAX_SCAN_BYTES = 64 * 1024 * 1024
PER_FILE_BYTES = 32 * 1024 * 1024
TOOLS = frozenset({
    "coach_prompt", "route_work", "build_contractor_packet", "dispatch_work",
    "evaluate_return", "normalize_contractor_return", "supervise_native_worker",
    "supervise_native_pool", "run_checked_command", "validate_operator_handoff",
    "validate_run_readiness_plan", "render_execution_status_report", "close_bead_with_summary",
})
METRICS = {
    "cwo_codex_cwo_scan_timestamp_seconds": ((), "Latest CWO session source scan attempt, Unix seconds."),
    "cwo_codex_cwo_scan_ready": ((), "Selected session sources scanned without pending files, access errors or retained gaps."),
    "cwo_codex_cwo_pending_files": ((), "Selected rollout files still awaiting CWO association scanning."),
    "cwo_codex_cwo_source_errors": ((), "CWO source access failures in the latest scan."),
    "cwo_codex_cwo_source_gaps": ((), "Persisted malformed or oversized candidate records and command conflicts."),
    "cwo_codex_cwo_limit_reached": ((), "Source-file or completed-command export cap reached."),
    "cwo_codex_session_cwo_association_timestamp_seconds": (("project_id", "session_id", "source"), "First CWO association: structured skill block, direct helper command, or parent session; not exclusive token attribution."),
    "cwo_codex_cwo_command_timestamp_seconds": (("project_id", "session_id", "observation_id", "tool", "outcome"), "Source completion time of a supported CWO helper invocation."),
    "cwo_codex_cwo_command_duration_seconds": (("project_id", "session_id", "observation_id", "tool", "outcome"), "Reported duration when the CWO helper is the sole command."),
}


def helper_invocation(command):
    """Return (helper, sole command); a shell suffix cannot attest its outcome.

    Only an unconditional first Python invocation is supported. Shell expansion,
    pipelines, redirection, control flow and help requests are deliberately excluded.
    """
    if not isinstance(command, list) or not command or any(not isinstance(v, str) for v in command):
        return None
    argv, sole = command, True
    if Path(argv[0]).name in {"bash", "sh", "zsh"}:
        if len(argv) != 3 or argv[1] not in {"-c", "-lc"}:
            return None
        shell = argv[2]
        if any(c in shell for c in ("\n", "\r", "$", "`")):
            return None
        try:
            lexer = shlex.shlex(shell, posix=True, punctuation_chars=";&|()<>")
            lexer.whitespace_split = True
            lexer.commenters = ""
            tokens = list(lexer)
        except ValueError:
            return None
        # Accept a sequence of simple commands only; never infer that a later
        # conditional invocation ran. Quoted program text remains one token.
        if any(v in {"||", "|", "&", "(", ")", "<", ">", "<<", ">>", "#"} for v in tokens):
            return None
        split = next((i for i, v in enumerate(tokens) if v in {";", "&&"}), len(tokens))
        if split < len(tokens):
            suffix = tokens[split + 1:]
            groups, current = [], []
            for token in suffix:
                if token in {";", "&&"}:
                    groups.append(current); current = []
                else:
                    current.append(token)
            groups.append(current)
            if any(not g or (g[0] != "cat" and not (re.fullmatch(r"python(?:3(?:\.\d+)?)?", Path(g[0]).name) and len(g) >= 3 and g[1] == "-c")) for g in groups):
                return None
            sole = False
        argv = tokens[:split]
    if not argv or re.fullmatch(r"python(?:3(?:\.\d+)?)?", Path(argv[0]).name) is None:
        return None
    position = 1
    while position < len(argv) and argv[position] in {"-B", "-u", "-I", "-E", "-s"}:
        position += 1
    if position >= len(argv) or any(v in {"-h", "--help", "--version"} for v in argv[position + 1:]):
        return None
    path = Path(argv[position])
    if len(path.parts) < 3 or path.parts[-3:-1] != ("complex-work-orchestration", "scripts"):
        return None
    if path.suffix != ".py" or path.stem not in TOOLS:
        return None
    return path.stem, sole


def direct_tool(command):
    result = helper_invocation(command)
    return result[0] if result else None


def skill_block(payload):
    if payload.get("type") != "message" or payload.get("role") != "user":
        return False
    content = payload.get("content")
    if not isinstance(content, list):
        return False
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "input_text":
            continue
        text = block.get("text")
        if isinstance(text, str) and re.fullmatch(
                r"\s*<skill>\s*<name>complex-work-orchestration</name>\s*<path>[^<>\n]+/SKILL\.md</path>\s*[\s\S]*</skill>\s*", text):
            return True
    return False


def oversized_candidate(raw):
    """Large unrelated command output does not obscure a recognized CWO signal."""
    prefix = raw[:65536]
    if b'"item_completed"' in prefix[:512]:
        match = re.search(rb'"command"\s*:\s*', prefix)
        if match:
            try:
                argv, _ = json.JSONDecoder().raw_decode(prefix[match.end():].decode("utf-8"))
                return direct_tool(argv) is not None
            except (ValueError, UnicodeError):
                return b"complex-work-orchestration/scripts/" in prefix
    return (b'"message"' in prefix[:512] and b'"user"' in prefix[:512]
            and re.search(rb'"text"\s*:\s*"(?:\\n|\s)*<skill>', prefix) is not None
            and b'<name>complex-work-orchestration</name>' in prefix)


class CwoSessionCollector:
    """Independent feature cursors; the session/usage accounting index is read-only here."""
    def __init__(self, home, state_dir):
        self.home = _safe_path(Path(home), owner=True)
        self.state = _safe_path(Path(state_dir), owner=True)
        if not self.home.is_dir() or not self.state.is_dir() or self.state.is_relative_to(self.home):
            raise ValueError("invalid CWO source or state directory")
        if stat.S_IMODE(self.state.stat().st_mode) != 0o700:
            raise ValueError("CWO state must be private")
        lock_path = self.state / "cwo-writer.lock"
        if lock_path.exists():
            _safe_path(lock_path, owner=True)
            if not lock_path.is_file() or stat.S_IMODE(lock_path.stat().st_mode) != 0o600:
                raise ValueError("CWO lock must be private")
        self.lock = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.lock)
            raise ValueError("CWO collector already running") from None
        self.path = self.state / "cwo-sessions.sqlite3"
        self.db = None
        try:
            for path in (self.path, Path(str(self.path)+"-wal"), Path(str(self.path)+"-shm")):
                if path.exists() or path.is_symlink():
                    _safe_path(path, owner=True)
                    if not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
                        raise ValueError("CWO state must be private")
            self.db = sqlite3.connect(self.path)
            self.path.chmod(0o600)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA journal_size_limit=16777216")
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY,sid TEXT,device INTEGER,inode INTEGER,size INTEGER,
                    modified INTEGER,changed INTEGER,offset INTEGER,skip INTEGER);
                CREATE TABLE IF NOT EXISTS signals(sid TEXT,source TEXT,at REAL,PRIMARY KEY(sid,source));
                CREATE TABLE IF NOT EXISTS commands(id TEXT PRIMARY KEY,sid TEXT,at REAL,tool TEXT,outcome TEXT,duration REAL,
                    fingerprint TEXT,conflict INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS health(key TEXT PRIMARY KEY,value INTEGER);
            ''')
            # Rebuild only this derived feature index after recognition changes.
            # Ordinary session accounting and its cursors are untouched.
            if self.db.execute("PRAGMA user_version").fetchone()[0] < 2:
                with self.db:
                    for table in ("files", "signals", "commands", "health"):
                        self.db.execute("DELETE FROM " + table)
                    self.db.execute("PRAGMA user_version=2")
            for suffix in ("", "-wal", "-shm"):
                path = Path(str(self.path)+suffix)
                if path.exists():path.chmod(0o600)
        except Exception:
            self.close()
            raise

    def close(self):
        if self.db is not None:self.db.close()
        os.close(self.lock)

    def _gap(self):
        self.db.execute("INSERT INTO health VALUES('gaps',1) ON CONFLICT(key) DO UPDATE SET value=value+1")

    def _consume(self, raw, sid, created, now):
        if b"complex-work-orchestration" not in raw:
            return
        try:
            record = json.loads(raw)
            payload = record.get("payload")
            if not isinstance(payload, dict):
                return
            is_skill = record.get("type") == "response_item" and skill_block(payload)
            item = payload.get("item")
            invocation = (helper_invocation(item.get("command")) if record.get("type") == "event_msg"
                    and payload.get("type") == "item_completed" and isinstance(item, dict)
                    and item.get("type") == "CommandExecution" else None)
            if not is_skill and invocation is None:
                return
            at = _timestamp(record.get("timestamp"))
            if at is None:
                self._gap();return
            if at < created:
                return
            if is_skill:
                self.db.execute("INSERT INTO signals VALUES(?,?,?) ON CONFLICT(sid,source) DO UPDATE SET at=min(at,excluded.at)", (sid, "skill_block", at))
                return
            if payload.get("thread_id") != sid:
                return
            if item.get("status") not in {"completed", "failed"}:
                self._gap();return
            turn, identity = payload.get("turn_id"), item.get("id")
            if not all(isinstance(v, str) and 0 < len(v) <= 512 for v in (turn, identity)):
                self._gap();return
            completed = _number(payload.get("completed_at_ms"))
            if completed is not None:at = completed / 1000
            if at < created:return
            self.db.execute("INSERT INTO signals VALUES(?,?,?) ON CONFLICT(sid,source) DO UPDATE SET at=min(at,excluded.at)", (sid, "tool_execution", at))
            if at < now - RETENTION_SECONDS:return
            tool, sole = invocation
            code = item.get("exit_code") if sole else None
            outcome = "completed" if item["status"] == "completed" and type(code) is int and code == 0 else "failed" if type(code) is int else "unknown"
            duration = item.get("duration") if sole else None
            seconds = None
            if isinstance(duration, dict):
                secs, nanos = duration.get("secs"), duration.get("nanos")
                if type(secs) is int and secs >= 0 and type(nanos) is int and 0 <= nanos < 1000000000:
                    seconds = secs + nanos / 1000000000
            identity = hashlib.sha256(json.dumps([sid, turn, identity]).encode()).hexdigest()
            fingerprint = hashlib.sha256(json.dumps([at, tool, outcome, seconds]).encode()).hexdigest()
            prior = self.db.execute("SELECT fingerprint FROM commands WHERE id=?", (identity,)).fetchone()
            if prior and prior[0] != fingerprint:
                self.db.execute("UPDATE commands SET conflict=1 WHERE id=?", (identity,))
            elif not prior:
                self.db.execute("INSERT INTO commands VALUES(?,?,?,?,?,?,?,0)", (identity, sid, at, tool, outcome, seconds, fingerprint))
        except (ValueError, TypeError, AttributeError, RecursionError):
            self._gap()

    def scan(self, index, snapshot, *, now=None, byte_budget=MAX_SCAN_BYTES, per_file_budget=PER_FILE_BYTES):
        now = time.time() if now is None else now
        rows = {r["session_id"]: r for r in snapshot["sessions"]}
        policy = snapshot.get("session_export", {})
        retained, _ = session_export_rows(list(rows.values()), now=now,
            retention_seconds=policy.get("retention_seconds", RETENTION_SECONDS), cap=policy.get("cap", 1000))
        selected = {r["session_id"] for r in retained if r['kind'] in {'session', 'subagent'}}
        # Include older parents solely to establish actual ancestry, without exporting them.
        eligible = set(selected)
        queue = list(selected)
        visited = set()
        while queue:
            sid = queue.pop()
            if sid not in rows or sid in visited:continue
            visited.add(sid);eligible.add(sid)
            queue.append(rows[sid].get("parent_id"))
        sources = [dict(r) for r in index.execute("SELECT path,session_id FROM files ORDER BY modified DESC,path") if r["session_id"] in eligible]
        limited = len(sources) > MAX_FILES
        sources = sources[:MAX_FILES]
        pending = errors = 0
        remaining = byte_budget
        with self.db:
            self.db.execute("CREATE TEMP TABLE IF NOT EXISTS selected_files(path TEXT PRIMARY KEY)")
            self.db.execute("DELETE FROM selected_files")
            self.db.executemany("INSERT INTO selected_files VALUES(?)", ((r['path'],) for r in sources))
            self.db.execute("DELETE FROM files WHERE path NOT IN (SELECT path FROM selected_files)")
            for source in sources:
                relative, sid = Path(source["path"]), source["session_id"]
                if relative.is_absolute() or ".." in relative.parts:
                    errors += 1;continue
                try:
                    with _open_source(self.home / relative) as stream:
                        info = os.fstat(stream.fileno())
                        old = self.db.execute("SELECT * FROM files WHERE path=?", (str(relative),)).fetchone()
                        reset = old is None or old["sid"] != sid or (old["device"],old["inode"]) != (info.st_dev,info.st_ino) or info.st_size < old["offset"] or (info.st_size == old["size"] and info.st_ctime_ns != old["changed"])
                        offset = 0 if reset else old["offset"]
                        skip = 0 if reset else old["skip"]
                        if offset < info.st_size and remaining > 0:
                            header = json.loads(stream.readline(MAX_LINE+1))
                            if header.get("type") != "session_meta" or _uuid(header.get("payload",{}).get("id")) != sid:
                                errors += 1;continue
                            stream.seek(offset)
                            spent = 0
                            while remaining > 0 and spent < per_file_budget:
                                position = stream.tell()
                                raw = stream.readline(min(MAX_LINE+1, remaining, per_file_budget-spent))
                                if not raw:break
                                spent += len(raw);remaining -= len(raw)
                                if not raw.endswith(b"\n"):
                                    if len(raw) > MAX_LINE or skip:
                                        if not skip and oversized_candidate(raw):self._gap()
                                        skip=1;continue
                                    stream.seek(position);break
                                if skip:skip=0;continue
                                self._consume(raw, sid, _number(rows[sid].get("created")) or 0, now)
                            offset = stream.tell()
                        self.db.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?,?,?)", (str(relative),sid,info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns,offset,skip))
                        pending += int(offset < info.st_size or bool(skip))
                except (OSError, ValueError, TypeError, AttributeError):
                    errors += 1
            self.db.execute("DELETE FROM commands WHERE at<?", (now-RETENTION_SECONDS,))
            command_count = self.db.execute("SELECT count(*) FROM commands").fetchone()[0]
            limited = limited or command_count > COMMAND_CAP
            if command_count > COMMAND_CAP:
                dropped = self.db.execute("SELECT at FROM commands ORDER BY at DESC,id DESC LIMIT 1 OFFSET ?", (COMMAND_CAP,)).fetchone()[0]
                self.db.execute("INSERT INTO health VALUES('cap_until',?) ON CONFLICT(key) DO UPDATE SET value=max(value,excluded.value)", (dropped+RETENTION_SECONDS,))
            cap_until = self.db.execute("SELECT value FROM health WHERE key='cap_until'").fetchone()
            limited = limited or bool(cap_until and cap_until[0] > now)
            self.db.execute("DELETE FROM commands WHERE id NOT IN (SELECT id FROM commands ORDER BY at DESC,id DESC LIMIT ?)", (COMMAND_CAP,))
        associations = {}
        for row in self.db.execute("SELECT * FROM signals WHERE at<=? ORDER BY at,source", (now,)):
            if row["sid"] in rows:
                associations.setdefault(row["sid"], (row["at"],row["source"]))
        for row in self.db.execute("SELECT sid,min(at) AS at FROM commands WHERE conflict=0 AND at<=? GROUP BY sid", (now,)):
            if row["sid"] in rows and row["at"] < associations.get(row["sid"],(float('inf'),''))[0]:
                associations[row["sid"]] = (row["at"], "tool_execution")
        children = defaultdict(list)
        for sid,row in rows.items():children[row.get("parent_id")].append(sid)
        queue = [(at,sid) for sid,(at,_) in associations.items()];heapq.heapify(queue)
        while queue:
            at,parent = heapq.heappop(queue)
            for sid in children[parent]:
                created = _number(rows[sid].get("created")) or 0
                if rows[sid]['kind'] == 'subagent' and created >= at and created < associations.get(sid,(float('inf'),''))[0]:
                    associations[sid] = (created,"parent_session");heapq.heappush(queue,(created,sid))
        future = self.db.execute("SELECT count(*) FROM signals WHERE at>?", (now,)).fetchone()[0] + self.db.execute("SELECT count(*) FROM commands WHERE at>?", (now,)).fetchone()[0]
        gaps = future + sum(r[0] for r in self.db.execute("SELECT value FROM health WHERE key='gaps'")) + self.db.execute("SELECT count(*) FROM commands WHERE conflict=1").fetchone()[0]
        commands = [dict(r) for r in self.db.execute("SELECT * FROM commands WHERE conflict=0 AND at<=? ORDER BY at DESC,id", (now,)) if r['sid'] in selected]
        return {"scan_timestamp_seconds":now,"scan_ready":int(bool(snapshot['source_available']) and not snapshot['pending_files'] and not pending and not errors and not gaps and not limited),
                "pending_files":pending,"source_errors":errors,"source_gaps":gaps,"limit_reached":int(limited),
                "associations":[{"session_id":sid,"project_id":rows[sid]['project_id'],"timestamp":at,"source":source} for sid,(at,source) in sorted(associations.items()) if sid in selected],
                "commands":[{**r,"project_id":rows[r['sid']]['project_id']} for r in commands]}


def render_cwo_session_metrics(snapshot):
    lines=[]
    for name,(_,help_text) in METRICS.items():
        lines.extend((f"# HELP {name} {help_text}",f"# TYPE {name} gauge"))
        if name == "cwo_codex_session_cwo_association_timestamp_seconds":
            for row in snapshot['associations']:
                labels={k:row[k] for k in ('project_id','session_id','source')}
                lines.append(name+'{'+','.join(k+'='+json.dumps(v) for k,v in labels.items())+'} '+str(row['timestamp']))
        elif name in {'cwo_codex_cwo_command_timestamp_seconds','cwo_codex_cwo_command_duration_seconds'}:
            for row in snapshot['commands']:
                value=row['at'] if name.endswith('timestamp_seconds') else row['duration']
                if value is None:continue
                labels={'project_id':row['project_id'],'session_id':row['sid'],'observation_id':row['id'],'tool':row['tool'],'outcome':row['outcome']}
                lines.append(name+'{'+','.join(k+'='+json.dumps(v) for k,v in labels.items())+'} '+str(value))
        else:
            lines.append(name+' '+str(snapshot[name.removeprefix('cwo_codex_cwo_')]))
    return ('\n'.join(lines)+'\n').encode()
