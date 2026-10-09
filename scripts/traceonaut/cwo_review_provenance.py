"""Read-only review attribution from completed Codex command records.

Only numeric metadata, hashes, source identities and cursors persist. Source
programs are never evaluated. Optional discovery reads literal output paths.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shlex

from .codex_session_telemetry import MAX_LINE, _open_source, _timestamp, _uuid, session_export_rows

MAX_FILES = 4096
MAX_BYTES = 64 * 1024 * 1024
PER_FILE_BYTES = 32 * 1024 * 1024
MAX_EVIDENCE = 4096
RESULT_FIELDS = ("type", "is_error", "usage", "modelUsage", "duration_api_ms", "num_turns")
ANSI = re.compile(r"\x1b\][^\x07]*\x07|\x1b\[[0-?]*[ -/]*[@-~]")


def candidate(raw):
    types = re.findall(rb'"type"\s*:\s*"([a-zA-Z_]+)"', raw[:1024])
    return types[:3] == [b"event_msg", b"item_completed", b"CommandExecution"] or types[:2] == [b"response_item", b"custom_tool_call"]


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def result_key(value):
    return digest({k: value.get(k) for k in RESULT_FIELDS})


def receipt_keys(launch, result):
    """Bind a launcher summary to the already validated dispatch/result pair."""
    keys = [digest([digest(["cli", result.get("session_id"), result.get("uuid")]),
                    digest([launch.get("requested_model"), launch.get("effort")])])]
    prompt = launch.get("prompt_sha256")
    if isinstance(prompt, str) and re.fullmatch(r"[0-9a-f]{64}", prompt):
        keys.append(digest(["summary", prompt, launch.get("requested_model"),
                            launch.get("effort"), launch.get("lane"), launch.get("account"), result_key(result)]))
    return keys


def simple_argv(command):
    if not isinstance(command, list) or not command or any(not isinstance(v, str) for v in command):
        return None
    if Path(command[0]).name in {"bash", "sh", "zsh"}:
        if len(command) != 3 or command[1] not in {"-c", "-lc"}:
            return None
        text = command[2]
        if any(c in text for c in ("\n", "\r", "$", "`")):
            return None
        try:
            parser = shlex.shlex(text, posix=True, punctuation_chars=";&|()<>")
            parser.whitespace_split = True
            parser.commenters = ""
            command = list(parser)
        except ValueError:
            return None
        if any(v in {";", "&&", "||", "|", "&", "(", ")", "<", ">", ">>", "<<"} for v in command):
            return None
    return command


def launcher(command):
    argv = simple_argv(command)
    if not argv:
        return None
    if Path(argv[0]).name == "claude" and "--model" in argv and "--effort" in argv:
        try:
            return ("cli", digest([argv[argv.index("--model") + 1], argv[argv.index("--effort") + 1]]))
        except IndexError:
            return None
    # The retained paired-result runner accepts lane and working directory.
    # Neither its name nor its location establishes attribution by itself.
    if (len(argv) == 4 and re.fullmatch(r"python(?:3(?:\.\d+)?)?", Path(argv[0]).name)
            and Path(argv[1]).name == "runner.py" and Path(argv[1]).parent == Path(argv[3])
            and Path(argv[3]).is_absolute()):
        return ("summary", digest(argv[2]), digest(argv[3]))
    return None


def stdin_launches(code):
    """Accept literal, unconditional tool calls, not arbitrary JavaScript text.

    This narrowly recognizes the recorded exec/write_stdin envelope used by
    interactive contractor launches. It does not evaluate JavaScript.
    """
    if not isinstance(code, str):
        return []
    code = re.sub(r"^\s*// @exec:[^\n]*\n", "", code)
    decoder = json.JSONDecoder()
    calls = []
    while code.strip():
        match = re.match(r"\s*text\(await tools\.(exec_command|write_stdin)\(\s*\{", code)
        if not match:
            return []
        kind = match[1]
        code = code[match.end():]
        values = {}
        while True:
            key = re.match(r'\s*(?:"([A-Za-z_]+)"|([A-Za-z_]+))\s*:\s*', code)
            if not key:
                return []
            name = key[1] or key[2]
            code = code[key.end():]
            try:
                value, end = decoder.raw_decode(code)
            except ValueError:
                return []
            if name in values or isinstance(value, (list, dict)):
                return []
            values[name] = value
            code = code[end:].lstrip()
            if code.startswith(","):
                code = code[1:]
                continue
            closing = re.match(r"\}\s*\)\s*\)\s*;", code)
            if not closing:
                return []
            code = code[closing.end():]
            break
        if kind == "write_stdin":
            chars, process = values.get("chars"), values.get("session_id")
            if isinstance(chars, str) and type(process) is int and chars.endswith("\n"):
                launch = launcher(["sh", "-c", chars[:-1]])
                if launch:
                    calls.append((str(process), launch))
    return calls


def summaries(output):
    if not isinstance(output, str):
        return []
    text = ANSI.sub("", output)
    values = []
    for match in re.finditer(r"(?m)^\s*\{", text):
        try:
            value, _ = json.JSONDecoder().raw_decode(text[match.end()-1:])
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("type") == "result" and type(value.get("is_error")) is bool:
            values.append(value)
    return values


class ProvenanceIndex:
    """Additive derived tables in the existing, locked private CWO database."""
    def __init__(self, cwo, *, discover=False):
        self.home, self.db = cwo.home, cwo.db
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS review_files(path TEXT PRIMARY KEY,sid TEXT,device INTEGER,inode INTEGER,
                size INTEGER,changed INTEGER,offset INTEGER,gaps INTEGER,boundary TEXT);
            CREATE TABLE IF NOT EXISTS review_stdin(path TEXT,process TEXT,at REAL,launch TEXT,
                PRIMARY KEY(path,process,at,launch));
            CREATE TABLE IF NOT EXISTS review_evidence(path TEXT,sid TEXT,identity TEXT,key TEXT,start REAL,end REAL,
                PRIMARY KEY(path,identity,key));
        ''')
        self.discovery = None
        if discover:
            from .cwo_review_discovery import LaunchDiscovery
            existed = self.db.execute("SELECT 1 FROM sqlite_master WHERE name='review_launches'").fetchone()
            self.discovery = LaunchDiscovery(self.db)
            if not existed:
                self.db.execute('UPDATE review_files SET offset=0')
            self.db.execute('CREATE TABLE IF NOT EXISTS review_parser_version(version INTEGER PRIMARY KEY)')
            if not self.db.execute('SELECT 1 FROM review_parser_version WHERE version=2').fetchone():
                # Replay retained commands after parser changes, preserving
                # numeric projections whose temporary outputs have disappeared.
                self.db.execute('UPDATE review_files SET offset=0,gaps=0,boundary=?',
                                (hashlib.sha256(b'').hexdigest(),))
                self.db.execute('DELETE FROM review_parser_version')
                self.db.execute('INSERT INTO review_parser_version VALUES(2)')

    def _consume(self, raw, path, sid):
        if not candidate(raw):
            return
        record = json.loads(raw)
        if self.discovery:
            self.discovery.consume(record, path, sid)
        payload = record.get("payload", {})
        if not isinstance(payload, dict):
            return
        at = _timestamp(record.get("timestamp"))
        if at is None:
            return
        if record.get("type") == "response_item" and payload.get("type") == "custom_tool_call" and payload.get("name") == "exec":
            for process, launch in stdin_launches(payload.get("input")):
                self.db.execute("INSERT OR IGNORE INTO review_stdin VALUES(?,?,?,?)",
                                (path, digest(process), at, json.dumps(launch)))
            return
        item = payload.get("item")
        if (record.get("type") != "event_msg" or payload.get("type") != "item_completed"
                or payload.get("thread_id") != sid or not isinstance(item, dict)
                or item.get("type") != "CommandExecution" or item.get("status") not in {"completed", "failed"}):
            return
        start, end = payload.get("started_at_ms"), payload.get("completed_at_ms")
        if any(type(v) not in (int, float) or not 0 < v < 2**53 for v in (start, end)) or start > end:
            return
        start, end = start / 1000, end / 1000
        if abs(end-at) > 5:
            return
        identity = item.get("id")
        if not isinstance(identity, str) or not identity:
            return
        direct = launcher(item.get("command"))
        launches = [direct] if direct else []
        argv = simple_argv(item.get("command"))
        # Interactive sudo/su shells require the actual recorded stdin launch,
        # tied to this process. Terminal echoes and receipt reads do not suffice.
        if argv in (["sudo", "-n", "su"], ["sudo", "su"], ["su", "-"], ["bash"], ["sh"]):
            launches.extend(json.loads(r[0]) for r in self.db.execute(
                "SELECT launch FROM review_stdin WHERE path=? AND process=? AND at>=? AND at<=?",
                (path, digest(str(item.get("process_id"))), start, end)))
        for value in summaries(item.get("stdout")):
            for launch in launches:
                if launch[0] == "cli" and _uuid(value.get("session_id")) and _uuid(value.get("uuid")):
                    key = digest(["cli", value["session_id"], value["uuid"]])
                    # Model and effort travel separately so the receipt must agree.
                    key = digest([key, launch[1]])
                elif (launch[0] == "summary" and digest(value.get("lane")) == launch[1]
                        and isinstance(value.get("prompt_sha256"), str)
                        and re.fullmatch(r"[0-9a-f]{64}", value["prompt_sha256"])):
                    key = digest(["summary", value["prompt_sha256"], value.get("requested_model"),
                                  value.get("requested_effort"), value.get("lane"), value.get("account"), result_key(value)])
                else:
                    continue
                self.db.execute("INSERT OR REPLACE INTO review_evidence VALUES(?,?,?,?,?,?)",
                                (path, sid, digest([payload.get("turn_id"), identity]), key, start, end))

    def scan(self, index, snapshot, reviews, *, now, byte_budget=MAX_BYTES, per_file_budget=PER_FILE_BYTES):
        if self.discovery:
            from .cwo_review_discovery import MAX_SCAN_OUTPUT_BYTES
            self.discovery.remaining=MAX_SCAN_OUTPUT_BYTES
            self.discovery.limited=False
            self.discovery.audit_errors=0
            self.discovery.audit_seen=set()
            self.discovery.audit_skipped={}
        rows = {r["session_id"]: r for r in snapshot["sessions"]}
        stamps = [r["timestamp"] for r in reviews["reviews"]]
        policy = snapshot.get("session_export", {})
        retained, _ = session_export_rows(list(rows.values()), now=now,
            retention_seconds=policy.get("retention_seconds",30*86400), cap=policy.get("cap",1000))
        selected = {r["session_id"] for r in retained
                    if self.discovery or (stamps and (r.get("created") or 0) <= max(stamps)+2)}
        if self.discovery:
            selected &= {r['session_id'] for r in snapshot.get('cwo_sessions',{}).get('associations',[])}
        sources = [dict(r) for r in index.execute("SELECT path,session_id FROM files ORDER BY modified DESC,path") if r["session_id"] in selected]
        limited = len(sources) > MAX_FILES
        sources = sources[:MAX_FILES]
        remaining, pending, errors = byte_budget, 0, 0
        with self.db:
            paths = {r["path"] for r in sources}
            for old in self.db.execute("SELECT path FROM review_files").fetchall():
                if old[0] not in paths:
                    self._discard(old[0])
            for source in sources:
                path, sid = source["path"], source["session_id"]
                relative = Path(path)
                if relative.is_absolute() or ".." in relative.parts:
                    errors += 1
                    continue
                try:
                    with _open_source(self.home / relative) as stream:
                        info = os.fstat(stream.fileno())
                        old = self.db.execute("SELECT * FROM review_files WHERE path=?", (path,)).fetchone()
                        reset = old is None or old["sid"] != sid or (old["device"],old["inode"]) != (info.st_dev,info.st_ino) or info.st_size < old["offset"] or (info.st_size == old["size"] and info.st_ctime_ns != old["changed"])
                        if old is not None and not reset:
                            reset = self._boundary(stream,old["offset"]) != old["boundary"]
                        if reset:
                            self._discard(path)
                        offset, gaps = (0, 0) if reset else (old["offset"], old["gaps"])
                        stream.seek(0)
                        header = json.loads(stream.readline(MAX_LINE+1))
                        if header.get("type") != "session_meta" or _uuid(header.get("payload", {}).get("id")) != sid:
                            raise ValueError("source identity mismatch")
                        stream.seek(offset)
                        spent = 0
                        while offset < info.st_size and remaining > 0 and spent < per_file_budget:
                            position = stream.tell()
                            raw = stream.readline(min(MAX_LINE+1, remaining, per_file_budget-spent))
                            spent += len(raw)
                            remaining -= len(raw)
                            if not raw.endswith(b"\n"):
                                # A partial append or budget boundary is retried.
                                # Oversized records keep attribution pending.
                                if len(raw) > MAX_LINE:
                                    gaps += int(candidate(raw))
                                    offset = stream.tell()
                                    continue
                                stream.seek(position)
                                break
                            try:
                                self._consume(raw, path, sid)
                            except (ValueError, TypeError, AttributeError, RecursionError):
                                gaps += 1
                            offset = stream.tell()
                        after = os.fstat(stream.fileno())
                        if (after.st_size,after.st_ctime_ns) != (info.st_size,info.st_ctime_ns):
                            errors += 1
                        self.db.execute("INSERT OR REPLACE INTO review_files VALUES(?,?,?,?,?,?,?,?,?)",
                            (path,sid,info.st_dev,info.st_ino,info.st_size,info.st_ctime_ns,offset,gaps,self._boundary(stream,offset)))
                        pending += int(offset < info.st_size or bool(gaps))
                except (OSError, ValueError, TypeError, AttributeError):
                    errors += 1
            for table in ("review_evidence", "review_stdin"):
                count = self.db.execute("SELECT count(*) FROM " + table).fetchone()[0]
                if count > MAX_EVIDENCE:
                    limited = True
                    # Fail closed; no unbounded queue or silent partial linkage.
                    self.db.execute("DELETE FROM " + table)
                    self.db.execute("UPDATE review_files SET offset=0")
        ready = bool(snapshot["source_available"]) and not snapshot["pending_files"] and not pending and not errors and not limited
        evidence = [dict(r) for r in self.db.execute("SELECT * FROM review_evidence")]
        matches, uses = {}, {}
        for row in reviews["reviews"]:
            keys = row.get("provenance_keys", [])
            hits = [e for e in evidence if e["key"] in keys and e["start"]-2 <= row["timestamp"] <= e["end"]
                    and e["end"] <= now and e["sid"] in rows and e["start"] >= (rows[e["sid"]].get("created") or 0)]
            matches[row["review_id"]] = hits
            for e in hits:
                uses.setdefault((e["sid"],e["identity"],e["key"]),set()).add(row["review_id"])
        for row in reviews["reviews"]:
            hits = matches[row["review_id"]]
            sids = {e["sid"] for e in hits}
            ambiguous = row.get("provenance_conflict",False) or len(sids) > 1 or any(len(uses[e["sid"],e["identity"],e["key"]]) > 1 for e in hits)
            state = "ambiguous" if ambiguous else "pending" if not ready else "linked" if sids else "unlinked"
            row["attribution"] = state
            if state == "linked":
                sid = next(iter(sids))
                row["source_session"] = {"session_id":sid, "project_id":rows[sid]["project_id"]}
            else:
                row.pop("source_session", None)
        if self.discovery:
            with self.db:
                self.discovery.merge(reviews,snapshot,now)
            if not ready:
                reviews['collection_complete'] = 0
                reviews['membership_complete'] = False
        return {"ready":int(ready), "pending_files":pending, "source_errors":errors, "limit_reached":int(limited)}

    @staticmethod
    def _boundary(stream, offset):
        stream.seek(0)
        head = stream.read(min(offset,4096))
        stream.seek(max(0,offset-4096))
        return hashlib.sha256(head + stream.read(min(offset,4096))).hexdigest()

    def _discard(self, path):
        for table in ("review_files", "review_stdin", "review_evidence"):
            self.db.execute("DELETE FROM " + table + " WHERE path=?", (path,))
        if self.discovery:
            self.db.execute('DELETE FROM review_launches WHERE path=?',(path,))
            self.db.execute('DELETE FROM review_discovery_errors WHERE path=?',(path,))
