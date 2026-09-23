"""Incremental, metadata-only accounting for an explicitly selected Codex home.

This source has its own accounting namespace. It never fabricates CWO dispatch
ownership or combines rollout usage with the owned app-server ledger.
"""

from __future__ import annotations

from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import time
import unicodedata
from uuid import UUID, NAMESPACE_URL, uuid5


TOKEN_FIELDS = {
    "input": "input_tokens", "cached_input": "cached_input_tokens",
    "output": "output_tokens", "reasoning_output": "reasoning_output_tokens",
    "total": "total_tokens",
}
SAFE_INTEGER = 2**53 - 1
MAX_LINE = 8 * 1024 * 1024
MAX_SESSIONS = 100_000
SESSION_RETENTION_SECONDS = 30 * 24 * 60 * 60
DEFAULT_SESSION_EXPORT_CAP = 1_000
SKIP_REASONS = (
    "untracked_prefix", "untracked_item", "untracked_event",
    "unsupported_record_type", "non_object_record", "invalid_payload",
    "invalid_metadata", "missing_session", "foreign_session",
    "pre_session_history", "invalid_timestamp", "invalid_identity",
    "invalid_usage", "invalid_record", "oversized_record",
)
EFFORTS = {"minimal", "none", "low", "medium", "high", "xhigh", "max", "ultra"}
COMMAND_FEATURE_VERSION = 1
COMMAND_RETENTION_SECONDS = 7 * 24 * 60 * 60
COMMAND_EXPORT_CAP = 10_000
DEFAULT_COMMAND_EXPORT_CAP = 512
COMMAND_SOURCE = "ambient_session_item_completed_command_execution"
COMMAND_DURATION_SCOPE = "runtime_reported_command_duration"
COMMAND_SOURCE_QUALIFICATION = (
    "references/codex-all-sessions-observability.md#recorded-command-source"
)
COMPACTION_FEATURE_VERSION = 1
COMPACTION_EXPORT_CAP = 512
# Bound compaction detail independently of the command event cap.
DEFAULT_COMPACTION_EXPORT_CAP = 64
COMPACTION_SOURCE = "ambient_session_item_completed_context_compaction"
COMPACTION_SOURCE_QUALIFICATION = (
    "references/codex-all-sessions-observability.md#recorded-compaction-source"
)


def _uuid(value):
    try:
        return str(UUID(value)) if isinstance(value, str) else None
    except ValueError:
        return None


def _number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def session_export_rows(sessions, *, now, retention_seconds, cap):
    """Select exposition rows only; never remove accounting or display metadata."""
    cutoff = max(0.0, now - retention_seconds)
    def activity(row):
        return _number(row.get("last_event")) or _number(row.get("created")) or 0.0
    eligible = [
        row for row in sessions
        if retention_seconds == 0 or activity(row) == 0 or activity(row) >= cutoff
    ]
    eligible.sort(key=lambda row: (-activity(row), row["session_id"]))
    rows = eligible[:cap]
    return rows, {
        "retention_seconds": retention_seconds, "cap": cap, "cutoff": cutoff,
        "evaluated_at": now, "exported_sessions": len(rows),
        "expired_sessions": len(sessions) - len(eligible),
        "cap_omitted_sessions": max(0, len(eligible) - cap),
        "cap_truncated": int(len(eligible) > cap),
    }


def _timestamp(value):
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except (ValueError, OverflowError):
            return None
    return _number(value)


def _text(value, limit=120):
    if not isinstance(value, str):
        return ""
    # Only explicit name metadata reaches this function, never message text.
    value = " ".join(value.split())
    return "".join(c for c in value if not unicodedata.category(c).startswith("C"))[:limit]


def _model(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,95}", value) else "unknown"


def _safe_path(path, *, owner=False):
    path = Path(os.path.abspath(path))
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("symlink in telemetry path")
    if owner and info.st_uid != os.geteuid():
        raise ValueError("telemetry path has another owner")
    return path


def _open_source(path):
    _safe_path(path, owner=True)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
        os.close(fd)
        raise ValueError("source is not an owned regular file")
    return os.fdopen(fd, "rb")


def _usage_prefix(raw):
    match = re.search(rb'"type"\s*:\s*"([^"]+)"', raw[:512])
    return bool(match and match[1] == b"token_usage_record")


def _command_prefix(raw):
    """Recognize only the structured command envelope, never tool output records."""
    return b'"item_completed"' in raw[:512] and b'"CommandExecution"' in raw


def _compaction_prefix(raw):
    return b'"item_completed"' in raw[:512] and b'"ContextCompaction"' in raw


def _item_completed_prefix(raw):
    return b'"item_completed"' in raw[:512]


def _opaque_id(value, *, limit=512):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        return None
    return hashlib.sha256(value.encode()).hexdigest()


def write_snapshot(path, snapshot):
    path = Path(path)
    _safe_path(path.parent, owner=True)
    if stat.S_IMODE(path.parent.stat().st_mode) != 0o700:
        raise ValueError("snapshot directory must be private")
    if path.exists() or path.is_symlink():
        _safe_path(path, owner=True)
        if not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise ValueError("snapshot file must be private")
    fd, temporary_name = tempfile.mkstemp(prefix=".snapshot-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(snapshot, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class SessionCollector:
    """Single private writer; source databases and rollouts are read-only."""

    def __init__(
        self,
        codex_home,
        state_dir,
        *,
        max_disk_bytes=2 * 1024**3,
        command_export_cap=DEFAULT_COMMAND_EXPORT_CAP,
        command_retention_seconds=COMMAND_RETENTION_SECONDS,
        compaction_export_cap=DEFAULT_COMPACTION_EXPORT_CAP,
        compaction_retention_seconds=COMMAND_RETENTION_SECONDS,
        session_retention_seconds=SESSION_RETENTION_SECONDS,
        session_export_cap=DEFAULT_SESSION_EXPORT_CAP,
    ):
        self.home = _safe_path(Path(codex_home), owner=True)
        if not self.home.is_dir():
            raise ValueError("Codex home must be a directory")
        self.state = Path(os.path.abspath(state_dir))
        if self.state.is_relative_to(self.home):
            raise ValueError("collector state must be outside the Codex source home")
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        _safe_path(self.state, owner=True)
        if stat.S_IMODE(self.state.stat().st_mode) != 0o700:
            raise ValueError("collector state must be private")
        if (
            type(command_export_cap) is not int
            or not 1 <= command_export_cap <= COMMAND_EXPORT_CAP
        ):
            raise ValueError("command export cap must be an integer from 1 to 10000")
        if (
            type(command_retention_seconds) is not int
            or not 1 <= command_retention_seconds <= COMMAND_RETENTION_SECONDS
        ):
            raise ValueError("command retention must be an integer from 1 to 604800 seconds")
        if (
            type(compaction_export_cap) is not int
            or not 1 <= compaction_export_cap <= COMPACTION_EXPORT_CAP
        ):
            raise ValueError("compaction export cap must be an integer from 1 to 512")
        if (
            type(compaction_retention_seconds) is not int
            or not 1 <= compaction_retention_seconds <= COMMAND_RETENTION_SECONDS
        ):
            raise ValueError("compaction retention must be an integer from 1 to 604800 seconds")
        self.max_disk_bytes = max_disk_bytes
        if type(session_retention_seconds) is not int or not 0 <= session_retention_seconds <= SAFE_INTEGER:
            raise ValueError("session retention must be a nonnegative safe integer")
        if type(session_export_cap) is not int or not 1 <= session_export_cap <= MAX_SESSIONS:
            raise ValueError("session export cap must be an integer from 1 to 100000")
        self.session_retention_seconds = session_retention_seconds
        self.session_export_cap = session_export_cap
        self.command_export_cap = command_export_cap
        self.command_retention_seconds = command_retention_seconds
        self.compaction_export_cap = compaction_export_cap
        self.compaction_retention_seconds = compaction_retention_seconds
        self.lock = os.open(self.state / "writer.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.lock)
            raise ValueError("collector already running") from None
        database = self.state / "sessions.sqlite3"
        for path in self.state.iterdir():
            _safe_path(path, owner=True)
            if not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
                os.close(self.lock)
                raise ValueError("collector state file must be private")
        self.db = sqlite3.connect(database)
        database.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA journal_size_limit=16777216")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, project_name TEXT,
                title TEXT, agent_name TEXT, kind TEXT, parent_id TEXT,
                model TEXT, effort TEXT, created REAL, last_event REAL DEFAULT 0,
                turn_id TEXT, turn_started REAL, lifecycle INTEGER DEFAULT 0,
                reported_tokens INTEGER, conflict INTEGER DEFAULT 0,
                archived INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY, session_id TEXT, device INTEGER, inode INTEGER,
                offset INTEGER NOT NULL DEFAULT 0, size INTEGER, modified INTEGER,
                skip_line INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS usage (
                session_id TEXT NOT NULL, response_key TEXT NOT NULL,
                fingerprint TEXT NOT NULL, at REAL NOT NULL,
                input INTEGER, cached_input INTEGER, output INTEGER,
                reasoning_output INTEGER, total INTEGER,
                PRIMARY KEY(session_id, response_key)
            );
            CREATE TABLE IF NOT EXISTS turns (
                session_id TEXT NOT NULL, turn_id TEXT NOT NULL,
                at REAL, outcome INTEGER, duration REAL,
                PRIMARY KEY(session_id, turn_id)
            );
            CREATE TABLE IF NOT EXISTS command_observations (
                session_id TEXT NOT NULL, turn_key TEXT NOT NULL,
                item_key TEXT NOT NULL, observation_id TEXT NOT NULL UNIQUE,
                fingerprint TEXT NOT NULL, at REAL NOT NULL,
                outcome TEXT NOT NULL,
                duration_state TEXT NOT NULL, duration REAL,
                conflict INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(session_id, turn_key, item_key)
            );
            CREATE TABLE IF NOT EXISTS command_files (
                path TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                device INTEGER NOT NULL, inode INTEGER NOT NULL,
                offset INTEGER NOT NULL DEFAULT 0, size INTEGER NOT NULL,
                modified INTEGER NOT NULL, skip_line INTEGER NOT NULL DEFAULT 0,
                complete INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS compaction_observations (
                session_id TEXT NOT NULL, turn_key TEXT NOT NULL,
                item_key TEXT NOT NULL, observation_id TEXT NOT NULL UNIQUE,
                fingerprint TEXT NOT NULL, at REAL NOT NULL,
                conflict INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(session_id, turn_key, item_key)
            );
            CREATE TABLE IF NOT EXISTS compaction_files (
                path TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                device INTEGER NOT NULL, inode INTEGER NOT NULL,
                offset INTEGER NOT NULL DEFAULT 0, size INTEGER NOT NULL,
                modified INTEGER NOT NULL, skip_line INTEGER NOT NULL DEFAULT 0,
                complete INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS reliability_features (
                name TEXT PRIMARY KEY, version INTEGER NOT NULL,
                backfill_complete INTEGER NOT NULL DEFAULT 0,
                source_gap INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS health (key TEXT PRIMARY KEY, value REAL);
            CREATE TABLE IF NOT EXISTS skipped_records (
                reason TEXT PRIMARY KEY, total INTEGER NOT NULL
            );
        """)
        self.db.executemany(
            "INSERT OR IGNORE INTO skipped_records(reason,total) VALUES(?,0)",
            ((reason,) for reason in SKIP_REASONS),
        )
        feature = self.db.execute(
            "SELECT version FROM reliability_features WHERE name='command_telemetry'"
        ).fetchone()
        if feature is None or feature[0] != COMMAND_FEATURE_VERSION:
            # Only this feature's private backfill cursors rewind. The existing
            # usage/turn `files.offset` values are never changed.
            self.db.execute(
                "UPDATE command_files SET offset=0,skip_line=0,complete=0"
            )
            self.db.execute(
                """INSERT INTO reliability_features(name,version,backfill_complete,source_gap)
                   VALUES('command_telemetry',?,0,0)
                   ON CONFLICT(name) DO UPDATE SET
                   version=excluded.version,backfill_complete=0,source_gap=0""",
                (COMMAND_FEATURE_VERSION,),
            )
        feature = self.db.execute(
            "SELECT version FROM reliability_features WHERE name='compaction_telemetry'"
        ).fetchone()
        if feature is None or feature[0] != COMPACTION_FEATURE_VERSION:
            # Compaction replay is independent of both the established and
            # command cursors, so this migration cannot change their coverage.
            self.db.execute(
                "UPDATE compaction_files SET offset=0,skip_line=0,complete=0"
            )
            self.db.execute(
                """INSERT INTO reliability_features(name,version,backfill_complete,source_gap)
                   VALUES('compaction_telemetry',?,0,0)
                   ON CONFLICT(name) DO UPDATE SET
                   version=excluded.version,backfill_complete=0,source_gap=0""",
                (COMPACTION_FEATURE_VERSION,),
            )
        self.db.commit()
        for path in self.state.iterdir():
            path.chmod(0o600)
        self.errors = Counter()
        self.available = False
        self.scanned_at = 0.0
        self.pending = 0
        self.command_pending = 0
        self.compaction_pending = 0

    def close(self):
        self.db.close()
        os.close(self.lock)

    def _record_skip(self, reason):
        """Count classified main-reader encounters in its cursor transaction."""
        if reason not in SKIP_REASONS:
            raise ValueError("unsupported skipped-record reason")
        self.db.execute(
            "UPDATE skipped_records SET total=total+1 WHERE reason=?", (reason,)
        )

    def _command_source_gap(self, reason):
        """Make an unrepresentable command record a durable coverage gap."""
        self.db.execute(
            "UPDATE reliability_features SET source_gap=1 WHERE name='command_telemetry'"
        )
        self.errors["command_" + reason] += 1

    def _compaction_source_gap(self, reason):
        """Make an unrepresentable compaction record a durable coverage gap."""
        self.db.execute(
            "UPDATE reliability_features SET source_gap=1 WHERE name='compaction_telemetry'"
        )
        self.errors["compaction_" + reason] += 1

    def _consume_command(self, record, payload, sid, session, *, track_skips=False):
        """Persist the allowlisted projection of one completed command item."""
        item = payload.get("item")
        if not isinstance(item, dict) or item.get("type") != "CommandExecution":
            return False

        owner = _uuid(payload.get("thread_id"))
        if owner is None:
            self._command_source_gap("missing_owner")
            if track_skips:
                self._record_skip("invalid_identity")
            return True
        if owner != sid:
            # Forked/copied history is not evidence owned by this rollout.
            if track_skips:
                self._record_skip("foreign_session")
            return True

        turn_key = _opaque_id(payload.get("turn_id"))
        item_key = _opaque_id(item.get("id"))
        if turn_key is None or item_key is None:
            self._command_source_gap("invalid_identity")
            if track_skips:
                self._record_skip("invalid_identity")
            return True

        at = _timestamp(record.get("timestamp"))
        if at is None or at < 0 or at > time.time() + 300:
            self._command_source_gap("invalid_timestamp")
            if track_skips:
                self._record_skip("invalid_timestamp")
            return True
        if at < session["created"]:
            # A fork can contain history from before this enclosing session.
            # Match the established non-usage path and ignore that history.
            if track_skips:
                self._record_skip("pre_session_history")
            return True

        status = item.get("status")
        exit_code = item.get("exit_code")
        exit_valid = type(exit_code) is int and 0 <= exit_code <= SAFE_INTEGER
        if status == "completed" and exit_valid and exit_code == 0:
            outcome = "completed"
        elif status == "failed" and exit_valid and exit_code != 0:
            outcome = "failed"
        else:
            outcome = "unknown"

        duration_value = item.get("duration")
        duration = None
        duration_state = "unknown"
        if isinstance(duration_value, dict):
            seconds = duration_value.get("secs")
            nanos = duration_value.get("nanos")
            if (
                type(seconds) is int
                and type(nanos) is int
                and 0 <= seconds <= SAFE_INTEGER
                and 0 <= nanos < 1_000_000_000
                and (seconds < SAFE_INTEGER or nanos == 0)
            ):
                candidate = seconds + nanos / 1_000_000_000
                if math.isfinite(candidate) and candidate <= SAFE_INTEGER:
                    duration, duration_state = candidate, "qualified"

        observation_id = hashlib.sha256(
            (
                "cwo-command-observation-v1\0" + sid + "\0"
                + payload["turn_id"] + "\0" + item["id"]
            ).encode()
        ).hexdigest()
        fingerprint = hashlib.sha256(
            b"cwo-command-payload-v1\0" + json.dumps(
                [at, outcome, duration_state, duration], separators=(",", ":")
            ).encode()
        ).hexdigest()
        old = self.db.execute(
            """SELECT fingerprint,conflict FROM command_observations
               WHERE session_id=? AND turn_key=? AND item_key=?""",
            (sid, turn_key, item_key),
        ).fetchone()
        if old:
            if old["conflict"]:
                return True
            if old["fingerprint"] != fingerprint:
                self.db.execute(
                    """UPDATE command_observations SET conflict=1
                       WHERE session_id=? AND turn_key=? AND item_key=?""",
                    (sid, turn_key, item_key),
                )
                self.errors["command_conflict"] += 1
            return True
        collision = self.db.execute(
            "SELECT session_id,turn_key,item_key FROM command_observations WHERE observation_id=?",
            (observation_id,),
        ).fetchone()
        if collision:
            self.db.execute(
                "UPDATE command_observations SET conflict=1 WHERE observation_id=?",
                (observation_id,),
            )
            self._command_source_gap("identity_collision")
            self.errors["command_conflict"] += 1
            return True
        self.db.execute(
            """INSERT INTO command_observations(
                   session_id,turn_key,item_key,observation_id,fingerprint,at,
                   outcome,duration_state,duration,conflict)
               VALUES(?,?,?,?,?,?,?,?,?,0)""",
            (
                sid, turn_key, item_key, observation_id, fingerprint, at,
                outcome, duration_state, duration,
            ),
        )
        return True

    def _consume_compaction(self, record, payload, sid, session, *, track_skips=False):
        """Persist one content-free observed ContextCompaction occurrence."""
        item = payload.get("item")
        if not isinstance(item, dict) or item.get("type") != "ContextCompaction":
            return False

        owner = _uuid(payload.get("thread_id"))
        if owner is None:
            self._compaction_source_gap("missing_owner")
            if track_skips:
                self._record_skip("invalid_identity")
            return True
        if owner != sid:
            if track_skips:
                self._record_skip("foreign_session")
            return True

        turn_key = _opaque_id(payload.get("turn_id"))
        item_key = _opaque_id(item.get("id"))
        if turn_key is None or item_key is None:
            self._compaction_source_gap("invalid_identity")
            if track_skips:
                self._record_skip("invalid_identity")
            return True

        at = _timestamp(record.get("timestamp"))
        if at is None or at < 0 or at > time.time() + 300:
            self._compaction_source_gap("invalid_timestamp")
            if track_skips:
                self._record_skip("invalid_timestamp")
            return True
        if at < session["created"]:
            if track_skips:
                self._record_skip("pre_session_history")
            return True

        observation_id = hashlib.sha256(
            (
                "cwo-compaction-observation-v1\0" + sid + "\0"
                + payload["turn_id"] + "\0" + item["id"]
            ).encode()
        ).hexdigest()
        fingerprint = hashlib.sha256(
            b"cwo-compaction-payload-v1\0" + json.dumps(
                [at, "ContextCompaction"], separators=(",", ":")
            ).encode()
        ).hexdigest()
        old = self.db.execute(
            """SELECT fingerprint,conflict FROM compaction_observations
               WHERE session_id=? AND turn_key=? AND item_key=?""",
            (sid, turn_key, item_key),
        ).fetchone()
        if old:
            if not old["conflict"] and old["fingerprint"] != fingerprint:
                self.db.execute(
                    """UPDATE compaction_observations SET conflict=1
                       WHERE session_id=? AND turn_key=? AND item_key=?""",
                    (sid, turn_key, item_key),
                )
                self.errors["compaction_conflict"] += 1
            return True
        collision = self.db.execute(
            """SELECT session_id,turn_key,item_key FROM compaction_observations
               WHERE observation_id=?""",
            (observation_id,),
        ).fetchone()
        if collision:
            self.db.execute(
                "UPDATE compaction_observations SET conflict=1 WHERE observation_id=?",
                (observation_id,),
            )
            self._compaction_source_gap("identity_collision")
            self.errors["compaction_conflict"] += 1
            return True
        self.db.execute(
            """INSERT INTO compaction_observations(
                   session_id,turn_key,item_key,observation_id,fingerprint,at,conflict)
               VALUES(?,?,?,?,?,?,0)""",
            (sid, turn_key, item_key, observation_id, fingerprint, at),
        )
        return True

    def _register(self, sid, metadata):
        sid = _uuid(sid)
        if sid is None:
            return
        cwd = metadata.get("cwd")
        project = str(uuid5(NAMESPACE_URL, "codex-project:" + cwd)) if isinstance(cwd, str) else str(uuid5(NAMESPACE_URL, "codex-project:unknown"))
        project_name = _text(Path(cwd).name) if isinstance(cwd, str) else "Unknown project"
        source = metadata.get("source")
        if isinstance(source, str) and source.startswith("{"):
            try:
                source = json.loads(source)
            except ValueError:
                source = None
        spawn = source.get("subagent", {}).get("thread_spawn", {}) if isinstance(source, dict) and isinstance(source.get("subagent"), dict) else {}
        parent = _uuid(metadata.get("parent_thread_id")) or _uuid(spawn.get("parent_thread_id"))
        existing = self.db.execute("SELECT id FROM sessions WHERE id=?", (sid,)).fetchone()
        if existing and "tokens_used" not in metadata:
            if parent:
                self.db.execute("UPDATE sessions SET parent_id=?,kind='subagent' WHERE id=?", (parent, sid))
            return
        thread_source = metadata.get("thread_source")
        kind = "subagent" if parent or thread_source == "subagent" or isinstance(source, dict) and "subagent" in source else "session"
        if thread_source in ("guardian_review", "memory_consolidation") or isinstance(source, dict) and "internal" in source:
            kind = "internal"
        if isinstance(source, dict) and isinstance(source.get("subagent"), dict) and source["subagent"].get("other") == "guardian":
            kind = "internal"
        created = _timestamp(metadata.get("created_at_ms"))
        created = created / 1000 if created else _timestamp(metadata.get("created_at")) or _timestamp(metadata.get("timestamp")) or 0
        explicit_name = _text(metadata.get("name"))
        agent = _text(metadata.get("agent_nickname") or spawn.get("agent_nickname"))
        agent_path = _text(metadata.get("agent_path") or spawn.get("agent_path"))
        title = explicit_name or agent_path.rsplit("/", 1)[-1].replace("_", " ")
        if not title:
            date = datetime.fromtimestamp(created, timezone.utc).strftime("%b %d %H:%M UTC")
            title = f"{project_name} · {kind.capitalize()} · {date}"
        effort = metadata.get("reasoning_effort")
        values = (sid, project, project_name, title, agent or ("Primary agent" if kind == "session" else kind.capitalize()), kind, parent or "none", _model(metadata.get("model")), effort if effort in EFFORTS else "unknown", created, int(bool(metadata.get("archived"))))
        self.db.execute("""INSERT INTO sessions(id,project_id,project_name,title,agent_name,kind,parent_id,model,effort,created,archived)
            VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            project_id=excluded.project_id, project_name=excluded.project_name,
            title=excluded.title, agent_name=excluded.agent_name,kind=excluded.kind,
            parent_id=CASE WHEN excluded.parent_id='none' THEN sessions.parent_id ELSE excluded.parent_id END,
            model=CASE WHEN excluded.model='unknown' THEN sessions.model ELSE excluded.model END,
            effort=CASE WHEN excluded.effort='unknown' THEN sessions.effort ELSE excluded.effort END,
            archived=excluded.archived""", values)
        reported = metadata.get("tokens_used")
        # Codex initializes this DB column to zero before observing usage.
        # Only an actual token_count record can establish a measured zero.
        if type(reported) is int and 0 < reported <= SAFE_INTEGER:
            self.db.execute("UPDATE sessions SET reported_tokens=? WHERE id=?", (reported, sid))

    def sync_inventory(self):
        """Discover every local rollout. Database paths never authorize file reads."""
        self.available = False
        roots = [self.home / "sessions", self.home / "archived_sessions"]
        paths = []
        for root in roots:
            if not root.exists():
                continue
            _safe_path(root, owner=True)
            for directory, dirs, files in os.walk(root, followlinks=False):
                dirs[:] = [name for name in dirs if not (Path(directory) / name).is_symlink()]
                for name in files:
                    if name.startswith("rollout-") and name.endswith(".jsonl"):
                        paths.append(Path(directory) / name)
                        if len(paths) > MAX_SESSIONS:
                            raise ValueError("session inventory capacity exceeded")
        if not (self.home / "sessions").is_dir():
            raise ValueError("session source unavailable")
        present_paths = {str(path.relative_to(self.home)) for path in paths}
        current_by_identity = {}
        current_by_path = {}
        for path in paths:
            info = path.lstat()
            if stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid():
                current_by_path[str(path.relative_to(self.home))] = info
                current_by_identity.setdefault((info.st_dev, info.st_ino), []).append(
                    (str(path.relative_to(self.home)), info)
                )
        for row in self.db.execute("SELECT path FROM files").fetchall():
            if row[0] not in present_paths:
                self.db.execute("DELETE FROM files WHERE path=?", (row[0],))
        for row in self.db.execute("SELECT * FROM command_files").fetchall():
            current_info = current_by_path.get(row["path"])
            if current_info and (
                current_info.st_dev, current_info.st_ino
            ) == (row["device"], row["inode"]):
                continue
            candidates = sorted(
                (candidate for candidate in current_by_identity.get(
                    (row["device"], row["inode"]), []
                ) if candidate[1].st_size >= row["offset"]),
                key=lambda candidate: candidate[0],
            )
            if candidates:
                replacement_path, replacement_info = candidates[0]
                existing_target = self.db.execute(
                    "SELECT path FROM command_files WHERE path=?", (replacement_path,)
                ).fetchone()
                if existing_target:
                    # Another hardlink cursor will replay the same source. Rewind
                    # that feature-local cursor rather than losing incomplete history.
                    self.db.execute(
                        """UPDATE command_files SET offset=0,skip_line=0,complete=0
                           WHERE path=?""",
                        (replacement_path,),
                    )
                    self.db.execute("DELETE FROM command_files WHERE path=?", (row["path"],))
                else:
                    self.db.execute(
                        """UPDATE command_files SET path=?,size=?,modified=?,complete=?
                           WHERE path=?""",
                        (
                            replacement_path,
                            replacement_info.st_size,
                            replacement_info.st_mtime_ns,
                            int(
                                row["offset"] >= replacement_info.st_size
                                and row["skip_line"] == 0
                            ),
                            row["path"],
                        ),
                    )
                continue
            if current_info:
                # A different inode now occupies the path. The normal per-path
                # replacement logic below decides whether the old cursor was complete.
                continue
            if not row["complete"] or row["offset"] < row["size"]:
                self._command_source_gap("incomplete_source_removed")
            self.db.execute("DELETE FROM command_files WHERE path=?", (row["path"],))
        for row in self.db.execute("SELECT * FROM compaction_files").fetchall():
            current_info = current_by_path.get(row["path"])
            if current_info and (
                current_info.st_dev, current_info.st_ino
            ) == (row["device"], row["inode"]):
                continue
            candidates = sorted(
                (candidate for candidate in current_by_identity.get(
                    (row["device"], row["inode"]), []
                ) if candidate[1].st_size >= row["offset"]),
                key=lambda candidate: candidate[0],
            )
            if candidates:
                replacement_path, replacement_info = candidates[0]
                existing_target = self.db.execute(
                    "SELECT path FROM compaction_files WHERE path=?", (replacement_path,)
                ).fetchone()
                if existing_target:
                    self.db.execute(
                        """UPDATE compaction_files SET offset=0,skip_line=0,complete=0
                           WHERE path=?""",
                        (replacement_path,),
                    )
                    self.db.execute(
                        "DELETE FROM compaction_files WHERE path=?", (row["path"],)
                    )
                else:
                    self.db.execute(
                        """UPDATE compaction_files SET path=?,size=?,modified=?,complete=?
                           WHERE path=?""",
                        (
                            replacement_path,
                            replacement_info.st_size,
                            replacement_info.st_mtime_ns,
                            int(
                                row["offset"] >= replacement_info.st_size
                                and row["skip_line"] == 0
                            ),
                            row["path"],
                        ),
                    )
                continue
            if current_info:
                continue
            if not row["complete"] or row["offset"] < row["size"]:
                self._compaction_source_gap("incomplete_source_removed")
            self.db.execute("DELETE FROM compaction_files WHERE path=?", (row["path"],))
        databases = sorted(self.home.glob("state_*.sqlite"), reverse=True)
        if databases:
            path = _safe_path(databases[0], owner=True)
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)) as source:
                source.row_factory = sqlite3.Row
                columns = {r[1] for r in source.execute("PRAGMA table_info(threads)")}
                # Never select title, preview or first_user_message: they can be prompts.
                selected = [c for c in ("id", "cwd", "name", "created_at", "created_at_ms", "source", "thread_source", "agent_nickname", "agent_path", "model", "reasoning_effort", "tokens_used", "archived") if c in columns]
                if "id" in selected:
                    for row in source.execute("SELECT " + ",".join(selected) + " FROM threads LIMIT ?", (MAX_SESSIONS + 1,)):
                        self._register(row["id"], dict(row))
        for path in paths:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                self.errors["unsafe_file"] += 1
                self._command_source_gap("unsafe_source")
                self._compaction_source_gap("unsafe_source")
                self.db.execute(
                    "DELETE FROM command_files WHERE path=?", (str(path.relative_to(self.home)),)
                )
                self.db.execute(
                    "DELETE FROM compaction_files WHERE path=?", (str(path.relative_to(self.home)),)
                )
                continue
            existing = self.db.execute("SELECT * FROM files WHERE path=?", (str(path.relative_to(self.home)),)).fetchone()
            sid = existing["session_id"] if existing else None
            replaced = existing and (existing["inode"] != info.st_ino or existing["device"] != info.st_dev or info.st_size < existing["offset"])
            if not existing or replaced:
                try:
                    with _open_source(path) as stream:
                        first = stream.readline(MAX_LINE + 1)
                    if len(first) > MAX_LINE or not first.endswith(b"\n"):
                        self.errors["missing_metadata"] += 1
                        self._record_skip("invalid_metadata")
                        self._command_source_gap("missing_metadata")
                        self._compaction_source_gap("missing_metadata")
                        continue
                    header = json.loads(first)
                    if not isinstance(header, dict) or header.get("type") != "session_meta":
                        self.errors["missing_metadata"] += 1
                        self._record_skip("invalid_metadata")
                        self._command_source_gap("missing_metadata")
                        self._compaction_source_gap("missing_metadata")
                        continue
                    metadata = header.get("payload", {})
                    sid = _uuid(metadata.get("id")) if isinstance(metadata, dict) else None
                    if sid is None:
                        self.errors["missing_metadata"] += 1
                        self._record_skip("invalid_metadata")
                        self._command_source_gap("missing_metadata")
                        self._compaction_source_gap("missing_metadata")
                        continue
                    self._register(sid, metadata)
                except (OSError, ValueError, TypeError, RecursionError):
                    self.errors["invalid_metadata"] += 1
                    self._record_skip("invalid_metadata")
                    self._command_source_gap("invalid_metadata")
                    self._compaction_source_gap("invalid_metadata")
                    continue
            offset = existing["offset"] if existing else 0
            skip_line = existing["skip_line"] if existing else 0
            if replaced:
                offset = skip_line = 0
                self.errors["source_rewritten"] += 1
            self.db.execute("""INSERT INTO files VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET
                session_id=excluded.session_id,device=excluded.device,inode=excluded.inode,offset=excluded.offset,size=excluded.size,
                modified=excluded.modified,skip_line=excluded.skip_line""",
                (str(path.relative_to(self.home)), sid, info.st_dev, info.st_ino, offset, info.st_size, info.st_mtime_ns, skip_line))
            relative = str(path.relative_to(self.home))
            command_file = self.db.execute(
                "SELECT * FROM command_files WHERE path=?", (relative,)
            ).fetchone()
            command_replaced = command_file and (
                command_file["inode"] != info.st_ino
                or command_file["device"] != info.st_dev
                or info.st_size < command_file["offset"]
            )
            command_offset = command_file["offset"] if command_file else 0
            command_skip = command_file["skip_line"] if command_file else 0
            if command_replaced:
                if not command_file["complete"] or command_file["offset"] < command_file["size"]:
                    self._command_source_gap("incomplete_source_replaced")
                command_offset = command_skip = 0
                self.errors["command_source_rewritten"] += 1
            self.db.execute(
                """INSERT INTO command_files(
                       path,session_id,device,inode,offset,size,modified,skip_line,complete)
                   VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET
                       session_id=excluded.session_id,device=excluded.device,
                       inode=excluded.inode,offset=excluded.offset,size=excluded.size,
                       modified=excluded.modified,skip_line=excluded.skip_line,
                       complete=excluded.complete""",
                (
                    relative, sid, info.st_dev, info.st_ino, command_offset,
                    info.st_size, info.st_mtime_ns, command_skip,
                    int(command_offset >= info.st_size and command_skip == 0),
                ),
            )
            compaction_file = self.db.execute(
                "SELECT * FROM compaction_files WHERE path=?", (relative,)
            ).fetchone()
            compaction_replaced = compaction_file and (
                compaction_file["inode"] != info.st_ino
                or compaction_file["device"] != info.st_dev
                or info.st_size < compaction_file["offset"]
            )
            compaction_offset = compaction_file["offset"] if compaction_file else 0
            compaction_skip = compaction_file["skip_line"] if compaction_file else 0
            if compaction_replaced:
                if (
                    not compaction_file["complete"]
                    or compaction_file["offset"] < compaction_file["size"]
                ):
                    self._compaction_source_gap("incomplete_source_replaced")
                compaction_offset = compaction_skip = 0
                self.errors["compaction_source_rewritten"] += 1
            self.db.execute(
                """INSERT INTO compaction_files(
                       path,session_id,device,inode,offset,size,modified,skip_line,complete)
                   VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET
                       session_id=excluded.session_id,device=excluded.device,
                       inode=excluded.inode,offset=excluded.offset,size=excluded.size,
                       modified=excluded.modified,skip_line=excluded.skip_line,
                       complete=excluded.complete""",
                (
                    relative, sid, info.st_dev, info.st_ino, compaction_offset,
                    info.st_size, info.st_mtime_ns, compaction_skip,
                    int(compaction_offset >= info.st_size and compaction_skip == 0),
                ),
            )
        self.db.commit()
        self.available = True

    def _consume(self, record, sid, *, track_skips=True):
        record_skip = self._record_skip if track_skips else lambda reason: None
        if not isinstance(record, dict):
            record_skip("non_object_record")
            return
        kind, payload = record.get("type"), record.get("payload")
        def invalid_usage():
            self.db.execute("UPDATE sessions SET conflict=1 WHERE id=?", (sid,))
            self.errors["invalid_usage"] += 1
        if not isinstance(payload, dict):
            if kind == "token_usage_record":
                invalid_usage()
            record_skip("invalid_payload")
            return
        if kind == "session_meta":
            if _uuid(payload.get("id")) == sid:
                self._register(sid, payload)
            else:
                record_skip("invalid_metadata")
            return
        session = self.db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if session is None:
            record_skip("missing_session")
            return
        subtype = payload.get("type")
        if kind == "event_msg" and subtype == "item_completed":
            if self._consume_command(record, payload, sid, session, track_skips=track_skips):
                return
            if self._consume_compaction(record, payload, sid, session, track_skips=track_skips):
                return
            record_skip("untracked_item")
            return
        owner = _uuid(payload.get("thread_id"))
        if owner and owner != sid:
            record_skip("foreign_session")
            return
        if kind == "token_usage_record" and owner is None:
            invalid_usage()
            record_skip("invalid_identity")
            return
        at = _timestamp(record.get("timestamp"))
        if at is None or at > time.time() + 300:
            if kind == "token_usage_record":
                invalid_usage()
            record_skip("invalid_timestamp")
            return
        # Historical records copied into a fork predate the new session.
        if kind != "token_usage_record" and at < session["created"]:
            record_skip("pre_session_history")
            return
        relevant = kind in ("turn_context", "token_usage_record") or kind == "event_msg" and subtype in ("task_started", "task_complete", "turn_aborted", "token_count", "thread_settings_applied")
        if not relevant:
            record_skip("untracked_event" if kind == "event_msg" else "unsupported_record_type")
            return
        if kind == "token_usage_record":
            if owner != sid:
                record_skip("foreign_session")
                return
            response = payload.get("response_id")
            usage = payload.get("usage")
            if not isinstance(response, str) or not 1 <= len(response) <= 512 or not isinstance(usage, dict):
                self.db.execute("UPDATE sessions SET conflict=1 WHERE id=?", (sid,))
                self.errors["invalid_usage"] += 1
                record_skip("invalid_usage")
                return
            values = [usage.get(name) if type(usage.get(name)) is int and 0 <= usage[name] <= SAFE_INTEGER else None for name in TOKEN_FIELDS.values()]
            key = hashlib.sha256(response.encode()).hexdigest()
            identity = [payload.get(name) for name in ("thread_id", "turn_id", "session_id", "root_turn_id")]
            if any(not isinstance(value, str) or not 1 <= len(value) <= 512 for value in identity):
                self.db.execute("UPDATE sessions SET conflict=1 WHERE id=?", (sid,))
                self.errors["invalid_usage"] += 1
                record_skip("invalid_usage")
                return
            cache_write = usage.get("cache_write_input_tokens")
            cache_write = cache_write if type(cache_write) is int and 0 <= cache_write <= SAFE_INTEGER else None
            fingerprint = hashlib.sha256(json.dumps([identity, values, cache_write], separators=(",", ":")).encode()).hexdigest()
            if any(value is None for value in (*values, cache_write)) or values[4] != values[0] + values[2] or values[1] > values[0] or values[3] > values[2]:
                self.db.execute("UPDATE sessions SET conflict=1 WHERE id=?", (sid,))
                self.errors["invalid_usage"] += 1
                record_skip("invalid_usage")
                return
            old = self.db.execute("SELECT fingerprint FROM usage WHERE session_id=? AND response_key=?", (sid, key)).fetchone()
            if old:
                if old[0] != fingerprint:
                    self.db.execute("UPDATE sessions SET conflict=1 WHERE id=?", (sid,))
                    self.errors["usage_conflict"] += 1
                self.db.execute("UPDATE sessions SET last_event=max(last_event,?) WHERE id=?", (at, sid))
                return
            self.db.execute("INSERT INTO usage VALUES(?,?,?,?,?,?,?,?,?)", (sid, key, fingerprint, at, *values))
        elif kind == "turn_context" or subtype == "thread_settings_applied":
            context = payload.get("thread_settings", {}) if subtype == "thread_settings_applied" else payload
            if not isinstance(context, dict):
                self.errors["invalid_record"] += 1
                record_skip("invalid_payload")
                return
            model = _model(context.get("model"))
            effort = context.get("effort", context.get("reasoning_effort"))
            effort = effort if isinstance(effort, str) and effort in EFFORTS else "unknown"
            if at >= session["last_event"]:
                self.db.execute("UPDATE sessions SET model=?,effort=? WHERE id=?", (model, effort, sid))
        elif subtype == "token_count":
            info = payload.get("info")
            usage = info.get("total_token_usage") if isinstance(info, dict) else None
            total = usage.get("total_tokens") if isinstance(usage, dict) else None
            if type(total) is int and total >= 0 and at >= session["last_event"]:
                self.db.execute("UPDATE sessions SET reported_tokens=? WHERE id=?", (total, sid))
        elif subtype == "task_started":
            turn = payload.get("turn_id")
            if not isinstance(turn, str) or not 1 <= len(turn) <= 128:
                self.errors["invalid_record"] += 1
                record_skip("invalid_identity")
                return
            if at >= session["last_event"]:
                started = _timestamp(payload.get("started_at")) or at
                self.db.execute("UPDATE sessions SET turn_id=?,turn_started=?,lifecycle=1 WHERE id=?", (turn, started, sid))
        elif subtype in ("task_complete", "turn_aborted"):
            turn = payload.get("turn_id")
            if not isinstance(turn, str) or len(turn) > 128:
                record_skip("invalid_identity")
                return
            outcome = 3 if subtype == "turn_aborted" or payload.get("error") else 2
            duration = _number(payload.get("duration_ms"))
            duration = duration / 1000 if duration is not None else None
            self.db.execute("INSERT OR IGNORE INTO turns VALUES(?,?,?,?,?)", (sid, turn, at, outcome, duration))
            if at >= session["last_event"]:
                self.db.execute("UPDATE sessions SET lifecycle=?,turn_id=?,turn_started=NULL WHERE id=?", (outcome, turn, sid))
        self.db.execute("UPDATE sessions SET last_event=max(last_event,?) WHERE id=?", (at, sid))

    def _scan_command_backfill(self, *, byte_budget, per_file_budget):
        """Advance command cursors without disturbing established file offsets."""
        remaining = byte_budget
        files = self.db.execute(
            """SELECT * FROM command_files WHERE complete=0 OR offset<size
               ORDER BY modified DESC,path"""
        ).fetchall()
        for item in files:
            if remaining <= 0:
                break
            path = self.home / item["path"]
            try:
                with _open_source(path) as stream:
                    info = os.fstat(stream.fileno())
                    if (info.st_dev, info.st_ino) != (item["device"], item["inode"]):
                        continue
                    stream.seek(item["offset"])
                    start = stream.tell()
                    skip = item["skip_line"]
                    limit = min(per_file_budget, remaining)
                    position = stream.tell()
                    with self.db:
                        while stream.tell() - start < limit:
                            position = stream.tell()
                            raw = stream.readline(MAX_LINE + 1)
                            if not raw:
                                position = stream.tell()
                                break
                            if not raw.endswith(b"\n"):
                                if len(raw) > MAX_LINE:
                                    if not skip and _item_completed_prefix(raw):
                                        self._command_source_gap("oversized_record")
                                        self._compaction_source_gap("oversized_record")
                                    skip = 1
                                    self.errors["command_oversized_line"] += 1
                                    position = stream.tell()
                                    continue
                                stream.seek(position)
                                break
                            position = stream.tell()
                            if skip:
                                skip = 0
                                continue
                            if not _command_prefix(raw):
                                continue
                            try:
                                self._consume(json.loads(raw), item["session_id"], track_skips=False)
                            except (ValueError, TypeError, OverflowError, RecursionError):
                                self._command_source_gap("invalid_record")
                        complete = int(position >= info.st_size and skip == 0)
                        self.db.execute(
                            """UPDATE command_files SET offset=?,size=?,skip_line=?,complete=?
                               WHERE path=?""",
                            (position, info.st_size, skip, complete, item["path"]),
                        )
                    remaining -= stream.tell() - start
            except (OSError, ValueError):
                self.errors["command_source_read"] += 1
                self.available = False

    def _scan_compaction_backfill(self, *, byte_budget, per_file_budget):
        """Advance compaction cursors without changing any established cursor."""
        remaining = byte_budget
        files = self.db.execute(
            """SELECT * FROM compaction_files WHERE complete=0 OR offset<size
               ORDER BY modified DESC,path"""
        ).fetchall()
        for item in files:
            if remaining <= 0:
                break
            path = self.home / item["path"]
            try:
                with _open_source(path) as stream:
                    info = os.fstat(stream.fileno())
                    if (info.st_dev, info.st_ino) != (item["device"], item["inode"]):
                        continue
                    stream.seek(item["offset"])
                    start = stream.tell()
                    skip = item["skip_line"]
                    limit = min(per_file_budget, remaining)
                    position = stream.tell()
                    with self.db:
                        while stream.tell() - start < limit:
                            position = stream.tell()
                            raw = stream.readline(MAX_LINE + 1)
                            if not raw:
                                position = stream.tell()
                                break
                            if not raw.endswith(b"\n"):
                                if len(raw) > MAX_LINE:
                                    if not skip and _item_completed_prefix(raw):
                                        self._compaction_source_gap("oversized_record")
                                    skip = 1
                                    self.errors["compaction_oversized_line"] += 1
                                    position = stream.tell()
                                    continue
                                stream.seek(position)
                                break
                            position = stream.tell()
                            if skip:
                                skip = 0
                                continue
                            if not _compaction_prefix(raw):
                                continue
                            try:
                                self._consume(json.loads(raw), item["session_id"], track_skips=False)
                            except (ValueError, TypeError, OverflowError, RecursionError):
                                self._compaction_source_gap("invalid_record")
                        complete = int(position >= info.st_size and skip == 0)
                        self.db.execute(
                            """UPDATE compaction_files SET
                               offset=?,size=?,skip_line=?,complete=? WHERE path=?""",
                            (position, info.st_size, skip, complete, item["path"]),
                        )
                    remaining -= stream.tell() - start
            except (OSError, ValueError):
                self.errors["compaction_source_read"] += 1
                self.available = False

    def scan(
        self,
        *,
        byte_budget=256 * 1024**2,
        per_file_budget=16 * 1024**2,
        command_byte_budget=64 * 1024**2,
        command_per_file_budget=4 * 1024**2,
        compaction_byte_budget=64 * 1024**2,
        compaction_per_file_budget=4 * 1024**2,
    ):
        """Bound work per pass; durable offsets make restart and replay idempotent."""
        self.sync_inventory()
        if sum(p.stat().st_size for p in self.state.iterdir() if p.is_file()) >= self.max_disk_bytes:
            self.available = False
            self.errors["capacity"] += 1
            return
        remaining = byte_budget
        # New/recent files are visited first; a per-file budget prevents starvation.
        files = self.db.execute("SELECT * FROM files WHERE offset<size ORDER BY modified DESC").fetchall()
        for item in files:
            if remaining <= 0:
                break
            path = self.home / item["path"]
            try:
                with _open_source(path) as stream:
                    info = os.fstat(stream.fileno())
                    if (info.st_dev, info.st_ino) != (item["device"], item["inode"]):
                        continue
                    stream.seek(item["offset"])
                    start = stream.tell()
                    skip = item["skip_line"]
                    limit = min(per_file_budget, remaining)
                    with self.db:
                        while stream.tell() - start < limit:
                            position = stream.tell()
                            raw = stream.readline(MAX_LINE + 1)
                            if not raw:
                                break
                            if not raw.endswith(b"\n"):
                                if len(raw) > MAX_LINE:
                                    if not skip:
                                        self._record_skip("oversized_record")
                                    if not skip and _usage_prefix(raw):
                                        self.db.execute("UPDATE sessions SET conflict=1 WHERE id=?", (item["session_id"],))
                                    if not skip and _item_completed_prefix(raw):
                                        self._command_source_gap("oversized_record")
                                    skip = 1
                                    self.errors["oversized_line"] += 1
                                    continue
                                stream.seek(position)
                                break  # partial append: retry after newline arrives
                            if skip:
                                skip = 0
                                continue
                            # Filter before JSON decoding. Payload text and tool output
                            # are never persisted, logged, or used to derive a title.
                            prefix = raw[:512]
                            if not any(marker in prefix for marker in (b'"session_meta"', b'"turn_context"', b'"token_usage_record"', b'"task_started"', b'"task_complete"', b'"turn_aborted"', b'"token_count"', b'"thread_settings_applied"', b'"item_completed"')):
                                self._record_skip("untracked_prefix")
                                continue
                            try:
                                self._consume(json.loads(raw), item["session_id"])
                            except (ValueError, TypeError, OverflowError, RecursionError):
                                if _usage_prefix(raw):
                                    self.db.execute("UPDATE sessions SET conflict=1 WHERE id=?", (item["session_id"],))
                                if _command_prefix(raw):
                                    self._command_source_gap("invalid_record")
                                if _compaction_prefix(raw):
                                    self._compaction_source_gap("invalid_record")
                                self.errors["invalid_record"] += 1
                                self._record_skip("invalid_record")
                        self.db.execute("UPDATE files SET offset=?,skip_line=? WHERE path=?", (stream.tell(), skip, item["path"]))
                    remaining -= stream.tell() - start
            except (OSError, ValueError):
                self.errors["source_read"] += 1
                self.available = False
        # Live ingestion above retains priority. Historical command discovery
        # uses its own bounded cursor and cannot rewind usage/turn accounting.
        self._scan_command_backfill(
            byte_budget=command_byte_budget,
            per_file_budget=command_per_file_budget,
        )
        self._scan_compaction_backfill(
            byte_budget=compaction_byte_budget,
            per_file_budget=compaction_per_file_budget,
        )
        self.pending = self.db.execute("SELECT count(*) FROM files WHERE offset<size").fetchone()[0]
        self.command_pending = self.db.execute(
            "SELECT count(*) FROM command_files WHERE complete=0 OR offset<size"
        ).fetchone()[0]
        self.compaction_pending = self.db.execute(
            "SELECT count(*) FROM compaction_files WHERE complete=0 OR offset<size"
        ).fetchone()[0]
        self.scanned_at = time.time()
        with self.db:
            self.db.execute(
                """UPDATE reliability_features SET backfill_complete=?
                   WHERE name='command_telemetry'""",
                (int(self.command_pending == 0 and self.available),),
            )
            self.db.execute(
                """UPDATE reliability_features SET backfill_complete=?
                   WHERE name='compaction_telemetry'""",
                (int(self.compaction_pending == 0 and self.available),),
            )
            for key, count in self.errors.items():
                self.db.execute("INSERT INTO health VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=value+excluded.value", (key, count))
        self.errors.clear()

    def snapshot(self, *, now=None):
        now = time.time() if now is None else now
        usage = {row["session_id"]: dict(row) for row in self.db.execute("""SELECT session_id,count(*) AS responses,
            sum(CAST(input AS REAL)) AS input,sum(CAST(cached_input AS REAL)) AS cached_input,
            sum(CAST(output AS REAL)) AS output,sum(CAST(reasoning_output AS REAL)) AS reasoning_output,
            sum(CAST(total AS REAL)) AS total FROM usage GROUP BY session_id""")}
        turns = {row["session_id"]: dict(row) for row in self.db.execute("""SELECT session_id,
            sum(outcome=2) AS completed,sum(outcome=3) AS failed,sum(duration) AS seconds
            FROM turns GROUP BY session_id""")}
        pending = {r[0] for r in self.db.execute("SELECT DISTINCT session_id FROM files WHERE offset<size")}
        sessions = []
        for record in self.db.execute("SELECT * FROM sessions ORDER BY last_event DESC,id"):
            row = dict(record)
            sid = row.pop("id")
            row["session_id"] = sid
            row["state"] = 4 if row["lifecycle"] == 1 and now - row["last_event"] > 120 else row["lifecycle"]
            values = usage.get(sid)
            row["usage_state"] = 3 if row["conflict"] else 4 if sid in pending else 1 if values else 2 if row["reported_tokens"] is not None else 0
            row["usage"] = {k: values[k] for k in TOKEN_FIELDS} if values and not row["conflict"] else {}
            row["responses"] = values["responses"] if values and not row["conflict"] else None
            row["completed_turns"] = turns.get(sid, {}).get("completed", 0)
            row["failed_turns"] = turns.get(sid, {}).get("failed", 0)
            row["observed_turn_seconds"] = turns.get(sid, {}).get("seconds")
            sessions.append(row)
        cutoff = max(0.0, now - self.command_retention_seconds)
        command_rows = self.db.execute(
            """SELECT c.*,s.project_id FROM command_observations c
               JOIN sessions s ON s.id=c.session_id WHERE c.at>=?
               ORDER BY c.at DESC,c.observation_id DESC LIMIT ?""",
            (cutoff, self.command_export_cap + 1),
        ).fetchall()
        cap_truncated = int(len(command_rows) > self.command_export_cap)
        retained = command_rows[:self.command_export_cap]
        feature = self.db.execute(
            """SELECT version,backfill_complete,source_gap FROM reliability_features
               WHERE name='command_telemetry'"""
        ).fetchone()
        backfill_complete = int(bool(feature and feature["backfill_complete"]))
        source_gaps = int(bool(feature and feature["source_gap"]))
        conflicts = self.db.execute(
            "SELECT count(*) FROM command_observations WHERE conflict=1"
        ).fetchone()[0]
        ready = int(
            bool(self.available)
            and self.command_pending == 0
            and backfill_complete == 1
            and source_gaps == 0
            and conflicts == 0
        )
        complete_after = None
        if ready:
            complete_after = cutoff
            if cap_truncated:
                # This watermark is exclusive. A dashboard range is complete
                # only when its lower event-time bound is greater than it,
                # including when the cap splits equal-timestamp observations.
                complete_after = max(
                    complete_after, command_rows[self.command_export_cap]["at"]
                )
        commands = []
        for command in retained:
            conflicted = bool(command["conflict"])
            commands.append({
                "project_id": command["project_id"],
                "session_id": command["session_id"],
                "observation_id": command["observation_id"],
                "timestamp": command["at"],
                "outcome": "conflict" if conflicted else command["outcome"],
                "duration_seconds": command["duration"] if not conflicted and command["duration_state"] == "qualified" else None,
            })
        compaction_cutoff = max(0.0, now - self.compaction_retention_seconds)
        compaction_rows = self.db.execute(
            """SELECT c.*,s.project_id FROM compaction_observations c
               JOIN sessions s ON s.id=c.session_id WHERE c.at>=?
               ORDER BY c.at DESC,c.observation_id DESC LIMIT ?""",
            (compaction_cutoff, self.compaction_export_cap + 1),
        ).fetchall()
        compaction_truncated = int(
            len(compaction_rows) > self.compaction_export_cap
        )
        retained_compactions = compaction_rows[:self.compaction_export_cap]
        compaction_feature = self.db.execute(
            """SELECT version,backfill_complete,source_gap FROM reliability_features
               WHERE name='compaction_telemetry'"""
        ).fetchone()
        compaction_backfill_complete = int(bool(
            compaction_feature and compaction_feature["backfill_complete"]
        ))
        compaction_source_gaps = int(bool(
            compaction_feature and compaction_feature["source_gap"]
        ))
        compaction_conflicts = self.db.execute(
            "SELECT count(*) FROM compaction_observations WHERE conflict=1"
        ).fetchone()[0]
        compaction_ready = int(
            bool(self.available)
            and self.compaction_pending == 0
            and compaction_backfill_complete == 1
            and compaction_source_gaps == 0
            and compaction_conflicts == 0
        )
        compaction_complete_after = None
        if compaction_ready:
            compaction_complete_after = compaction_cutoff
            if compaction_truncated:
                compaction_complete_after = max(
                    compaction_complete_after,
                    compaction_rows[self.compaction_export_cap]["at"],
                )
        compactions = [{
            "project_id": row["project_id"],
            "session_id": row["session_id"],
            "observation_id": row["observation_id"],
            "timestamp": row["at"],
            "conflict": int(bool(row["conflict"])),
        } for row in retained_compactions]
        reliability = {
            "version": 1,
            "command_telemetry": {
                "version": feature["version"] if feature else COMMAND_FEATURE_VERSION,
                "source": COMMAND_SOURCE,
                "source_qualification": COMMAND_SOURCE_QUALIFICATION,
                "coverage_scope": "recorded_command_events_in_selected_profile_rollouts",
                "duration_scope": COMMAND_DURATION_SCOPE,
                "snapshot_timestamp": self.scanned_at,
                "complete_after_timestamp": complete_after,
                "retention_seconds": self.command_retention_seconds,
                "cap": self.command_export_cap,
                "cap_truncated": cap_truncated,
                "observations": len(commands),
                "pending_files": self.command_pending,
                "backfill_complete": backfill_complete,
                "conflicts": conflicts,
                "source_gaps": source_gaps,
                "ready": ready,
            },
            "command_observations": commands,
            "compaction_telemetry": {
                "version": (
                    compaction_feature["version"]
                    if compaction_feature else COMPACTION_FEATURE_VERSION
                ),
                "source": COMPACTION_SOURCE,
                "source_qualification": COMPACTION_SOURCE_QUALIFICATION,
                "coverage_scope": (
                    "recorded_context_compaction_events_in_selected_profile_rollouts"
                ),
                "snapshot_timestamp": self.scanned_at,
                "complete_after_timestamp": compaction_complete_after,
                "retention_seconds": self.compaction_retention_seconds,
                "cap": self.compaction_export_cap,
                "cap_truncated": compaction_truncated,
                "observations": len(compactions),
                "pending_files": self.compaction_pending,
                "backfill_complete": compaction_backfill_complete,
                "conflicts": compaction_conflicts,
                "source_gaps": compaction_source_gaps,
                "ready": compaction_ready,
            },
            "compaction_observations": compactions,
        }
        _, session_export = session_export_rows(
            sessions, now=now, retention_seconds=self.session_retention_seconds,
            cap=self.session_export_cap,
        )
        return {"version": 1, "sessions": sessions, "source_available": int(self.available),
                "scan_timestamp": self.scanned_at, "last_event": max((s["last_event"] for s in sessions), default=0),
                "pending_files": self.pending, "errors": dict(self.db.execute("SELECT key,value FROM health")),
                "source": "local_codex_rollout", "token_scope": "deduplicated_observed_response_records",
                "reliability": reliability, "session_export": session_export,
                "skipped_records": dict(self.db.execute("SELECT reason,total FROM skipped_records"))}


def render_session_metrics(snapshot):
    """Render bounded numeric fields and opaque identities; no display metadata."""
    lines = []
    def emit(name, value, labels=None):
        if _number(value) is None or value > SAFE_INTEGER:
            return
        label = ",".join(k + "=" + json.dumps(v) for k, v in (labels or {}).items())
        lines.append(f"{name}{{{label}}} {value}")
    for field, value in (("scan_timestamp_seconds", snapshot["scan_timestamp"]), ("last_event_timestamp_seconds", snapshot["last_event"]), ("sessions", len(snapshot["sessions"])), ("pending_files", snapshot["pending_files"]), ("source_available", snapshot["source_available"])):
        emit("cwo_codex_collector_" + field, value)
    lines.extend([
        "# HELP cwo_codex_collector_errors_total Collector errors by reason, persisted across restarts.",
        "# TYPE cwo_codex_collector_errors_total counter",
    ])
    for reason, count in {"invalid_record": 0, **snapshot["errors"]}.items():
        emit("cwo_codex_collector_errors_total", count, {"reason": reason})
    if "skipped_records" in snapshot:
        lines.extend([
            "# HELP cwo_codex_collector_skipped_records_total Main-reader exclusions by bounded reason since tracking began; retries and file replays may be counted again.",
            "# TYPE cwo_codex_collector_skipped_records_total counter",
        ])
        for reason in SKIP_REASONS:
            emit("cwo_codex_collector_skipped_records_total", snapshot["skipped_records"].get(reason, 0), {"reason": reason})
    sessions = snapshot["sessions"]
    policy = snapshot.get("session_export")
    if policy is not None:
        sessions, export = session_export_rows(
            sessions, now=policy["evaluated_at"],
            retention_seconds=policy["retention_seconds"], cap=policy["cap"],
        )
        for field, description in (
            ("retention_seconds", "Inactivity window for per-session exposition; zero disables age expiry."),
            ("cap", "Maximum number of per-session metric groups exposed."),
            ("exported_sessions", "Session groups selected for the current exposition."),
            ("expired_sessions", "Indexed sessions outside the exposition inactivity window."),
            ("cap_omitted_sessions", "Eligible sessions omitted by the exposition cap."),
            ("cap_truncated", "One when eligible session groups exceed the exposition cap."),
        ):
            name = "cwo_codex_collector_session_export_" + field
            lines.extend([f"# HELP {name} {description}", f"# TYPE {name} gauge"])
            emit(name, export[field])
    reliability = snapshot.get("reliability", {})
    command_telemetry = reliability.get("command_telemetry", {})
    for metric, key in (
        ("snapshot_timestamp_seconds", "snapshot_timestamp"),
        ("complete_after_timestamp_seconds", "complete_after_timestamp"),
        ("observations", "observations"),
        ("retention_seconds", "retention_seconds"),
        ("cap", "cap"),
        ("cap_truncated", "cap_truncated"),
        ("pending_files", "pending_files"),
        ("backfill_complete", "backfill_complete"),
        ("conflicts", "conflicts"),
        ("source_gaps", "source_gaps"),
        ("ready", "ready"),
    ):
        emit("cwo_codex_command_telemetry_" + metric, command_telemetry.get(key))
    for row in reliability.get("command_observations", []):
        labels = {
            **{key: row[key] for key in ("project_id", "session_id", "observation_id")},
            "outcome": row["outcome"],
        }
        emit("cwo_codex_command_event_timestamp_seconds", row.get("timestamp"), labels)
        emit("cwo_codex_command_event_duration_seconds", row.get("duration_seconds"), labels)
    compaction_telemetry = reliability.get("compaction_telemetry", {})
    for metric, key in (
        ("snapshot_timestamp_seconds", "snapshot_timestamp"),
        ("complete_after_timestamp_seconds", "complete_after_timestamp"),
        ("observations", "observations"),
        ("retention_seconds", "retention_seconds"),
        ("cap", "cap"),
        ("cap_truncated", "cap_truncated"),
        ("pending_files", "pending_files"),
        ("backfill_complete", "backfill_complete"),
        ("conflicts", "conflicts"),
        ("source_gaps", "source_gaps"),
        ("ready", "ready"),
    ):
        emit("cwo_codex_compaction_telemetry_" + metric, compaction_telemetry.get(key))
    for row in reliability.get("compaction_observations", []):
        labels = {
            key: row[key]
            for key in ("project_id", "session_id", "observation_id")
        }
        emit(
            "cwo_codex_compaction_observation_timestamp_seconds",
            row.get("timestamp"),
            labels,
        )
    for row in sessions:
        labels = {"project_id": row["project_id"], "session_id": row["session_id"]}
        emit("cwo_codex_session_info", 1, {**labels, **{k: row[k] for k in ("kind", "parent_id", "model", "effort")}})
        for metric, key in (("last_event_timestamp_seconds", "last_event"), ("state", "state"), ("reported_tokens", "reported_tokens"), ("response_count", "responses"), ("completed_turns", "completed_turns"), ("failed_turns", "failed_turns"), ("observed_turn_seconds", "observed_turn_seconds"), ("usage_state", "usage_state")):
            emit("cwo_codex_session_" + metric, row.get(key), labels)
        for kind, value in row["usage"].items():
            emit("cwo_codex_session_usage_tokens", value, {**labels, "token_kind": kind})
    return ("\n".join(lines) + "\n").encode()
