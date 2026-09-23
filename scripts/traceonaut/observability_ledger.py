"""Durable, owner-private storage for CWO dispatch observability.

The ledger accepts authoritative supervisor records and normalized projections
from an explicitly owned app-server connection.  It never reads Codex session
files and it has no worker-control operations.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from contextlib import contextmanager
import fcntl
import hashlib
import hmac
import json
from math import isfinite
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import threading
import time
from typing import Any
from urllib.parse import quote
from uuid import UUID

from .observability_contract import (
    BindingState,
    CompletionPurpose,
    ConnectionState,
    CoverageGap,
    CoverageState,
    DiskState,
    FieldState,
    HealthReason,
    LifecycleState,
    MAX_FRAME_BYTES,
    MAX_NORMALIZED_RECORD_BYTES,
    METRIC_FAMILIES,
    PublicationState,
    QueueState,
    RegistrationState,
    SchemaState,
    TelemetryDisposition,
    TERMINAL_LIFECYCLE_STATES,
    TokenKind,
    ToolCategory,
    ToolLifecycle,
    TurnOutcome,
    aggregate_token_state,
    normalize_dispatch_observation,
    normalize_model_completion,
    normalize_project_registration,
    normalize_runtime_binding,
    normalize_telemetry_health,
    validate_lifecycle_transition,
)


SCHEMA_VERSION = 1
DATABASE_NAME = "observability.sqlite3"
KEY_DIRECTORY_NAME = "keys"
LOCK_NAME = "writer.lock"
_SHA256 = frozenset("0123456789abcdef")
# SQLite can retain committed WAL frames while a separate read transaction is
# open. The fixed allowance covers bounded input, WAL frame overhead, SHM, keys,
# and the capacity marker. The database cap is one quarter of the remainder;
# admission reserves two complete capped-database dirty-page passes, one for the
# admitted transaction and one for the durable capacity marker. Thus even a
# long reader cannot push the protected files beyond the registered total.
SQLITE_CAPACITY_FIXED_OVERHEAD_BYTES = 2 * MAX_FRAME_BYTES
SQLITE_CAPACITY_MARKER = "capacity_blocked"


def _capacity_envelope(limit: int) -> tuple[int, int]:
    database_cap = (limit - SQLITE_CAPACITY_FIXED_OVERHEAD_BYTES) // 4
    if database_cap < 4096:
        raise ObservabilityLedgerError("observability-disk-limit-below-reserve")
    reserve = 2 * database_cap + SQLITE_CAPACITY_FIXED_OVERHEAD_BYTES
    return database_cap, reserve


class ObservabilityLedgerError(ValueError):
    """Stable failure raised for invalid or unsafe ledger operations."""


def _is_sqlite_full(exc: BaseException) -> bool:
    return (
        isinstance(exc, sqlite3.OperationalError)
        and ((getattr(exc, "sqlite_errorcode", 0) or 0) & 0xFF) == sqlite3.SQLITE_FULL
    )


def _json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ObservabilityLedgerError("observability-value-not-json") from exc


def _load(value: str) -> Any:
    return json.loads(value)


def _semantic_payload(value: Mapping[str, Any]) -> str:
    """Canonical event content, excluding local receipt-time observations."""

    return _json(
        {
            key: item
            for key, item in value.items()
            if key
            not in {
                "observed_at_seconds",
                "received_monotonic_ns",
                "runtime_emitted_at_seconds",
            }
        }
    )


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and not set(value).difference(_SHA256)
    )


def _require_sha256(value: Any, field: str) -> str:
    if not _is_sha256(value):
        raise ObservabilityLedgerError(f"{field}-invalid")
    return value


def _require_uuid(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ObservabilityLedgerError(f"{field}-invalid")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ObservabilityLedgerError(f"{field}-invalid") from exc
    if parsed.int == 0 or str(parsed) != value:
        raise ObservabilityLedgerError(f"{field}-invalid")
    return value


def _now() -> float:
    return time.time()


def _reject_unsafe_path(path: Path, *, may_not_exist: bool) -> None:
    if not path.is_absolute():
        raise ObservabilityLedgerError("observability-state-path-not-absolute")
    cursor = Path(path.anchor)
    for index, part in enumerate(path.parts[1:], start=1):
        cursor /= part
        try:
            metadata = cursor.lstat()
        except FileNotFoundError:
            if may_not_exist:
                break
            raise ObservabilityLedgerError("observability-state-path-missing") from None
        if stat.S_ISLNK(metadata.st_mode):
            raise ObservabilityLedgerError("observability-state-path-symlink")
        if index < len(path.parts) - 1:
            if not stat.S_ISDIR(metadata.st_mode):
                raise ObservabilityLedgerError(
                    "observability-state-parent-not-directory"
                )
            writable = metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            sticky = metadata.st_mode & stat.S_ISVTX
            if metadata.st_uid not in {0, os.geteuid()}:
                raise ObservabilityLedgerError(
                    "observability-state-parent-owner-unsafe"
                )
            if writable and (not sticky or metadata.st_uid != 0):
                raise ObservabilityLedgerError("observability-state-parent-unsafe")


def _open_private_file(path: Path) -> int:
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise ObservabilityLedgerError("observability-private-file-unsafe") from exc
    metadata = os.fstat(fd)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        os.close(fd)
        raise ObservabilityLedgerError("observability-private-file-unsafe")
    return fd


class ObservabilityLedger:
    """Serialized SQLite ledger for one runtime-owner process.

    Writable instances are single-process owners.  Methods are additionally
    guarded by an ``RLock`` so a reader thread, adapter drain thread, exporter,
    and publication confirmer can safely share that instance.
    """

    def __init__(
        self,
        state_dir: str | os.PathLike[str],
        *,
        executor_vocabulary: Collection[str] | None = None,
        model_vocabulary: Collection[str] | None = None,
        effort_vocabulary: Collection[str] | None = None,
        readonly: bool = False,
    ) -> None:
        self.state_dir = Path(state_dir).expanduser()
        self.readonly = readonly
        if not readonly and (
            executor_vocabulary is None
            or model_vocabulary is None
            or effort_vocabulary is None
        ):
            raise ObservabilityLedgerError("observability-vocabularies-required")
        self.executor_vocabulary = tuple(executor_vocabulary or ())
        self.model_vocabulary = tuple(model_vocabulary or ())
        self.effort_vocabulary = tuple(effort_vocabulary or ())
        self._lock = threading.RLock()
        self._closed = False
        _reject_unsafe_path(self.state_dir, may_not_exist=not readonly)
        if readonly:
            self._open_readonly()
        else:
            self._open_writer()

    @classmethod
    def open(
        cls, state_dir: str | os.PathLike[str], **kwargs: Any
    ) -> "ObservabilityLedger":
        return cls(state_dir, **kwargs)

    @classmethod
    def read_snapshot(
        cls,
        state_dir: str | os.PathLike[str],
        *,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        with cls(state_dir, readonly=True) as ledger:
            return ledger.snapshot(project_id=project_id)

    def _open_writer(self) -> None:
        if not self.state_dir.exists():
            self.state_dir.mkdir(mode=0o700)
        metadata = self.state_dir.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise ObservabilityLedgerError("observability-state-owner-invalid")
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            raise ObservabilityLedgerError("observability-state-mode-invalid")
        key_dir = self.state_dir / KEY_DIRECTORY_NAME
        try:
            key_metadata = key_dir.lstat()
        except FileNotFoundError:
            key_dir.mkdir(mode=0o700)
            key_metadata = key_dir.lstat()
        if (
            not stat.S_ISDIR(key_metadata.st_mode)
            or key_metadata.st_uid != os.geteuid()
            or stat.S_IMODE(key_metadata.st_mode) != 0o700
        ):
            raise ObservabilityLedgerError("observability-key-directory-mode-invalid")
        self._writer_lock_fd = _open_private_file(self.state_dir / LOCK_NAME)
        try:
            fcntl.flock(self._writer_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self._writer_lock_fd)
            raise ObservabilityLedgerError(
                "observability-writer-already-active"
            ) from None
        try:
            database = self.state_dir / DATABASE_NAME
            database_fd = _open_private_file(database)
            os.close(database_fd)
            self._db = sqlite3.connect(
                database, isolation_level=None, check_same_thread=False
            )
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA foreign_keys=ON")
            for suffix in ("-wal", "-shm"):
                sidecar_fd = _open_private_file(
                    self.state_dir / f"{DATABASE_NAME}{suffix}"
                )
                os.close(sidecar_fd)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._create_schema()
            self._enforce_disk_limit()
            self._protect_sqlite_files()
            self.recover_incomplete_connections()
            self._recover_missing_keys()
        except Exception:
            database_handle = getattr(self, "_db", None)
            if database_handle is not None:
                database_handle.close()
            fcntl.flock(self._writer_lock_fd, fcntl.LOCK_UN)
            os.close(self._writer_lock_fd)
            raise

    def _open_readonly(self) -> None:
        state_metadata = self.state_dir.lstat()
        if (
            not stat.S_ISDIR(state_metadata.st_mode)
            or state_metadata.st_uid != os.geteuid()
            or stat.S_IMODE(state_metadata.st_mode) != 0o700
        ):
            raise ObservabilityLedgerError("observability-state-mode-invalid")
        database = self.state_dir / DATABASE_NAME
        _reject_unsafe_path(database, may_not_exist=False)
        metadata = database.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise ObservabilityLedgerError("observability-database-path-invalid")
        uri = f"file:{quote(str(database), safe='/')}?mode=ro"
        self._db = sqlite3.connect(
            uri, uri=True, isolation_level=None, check_same_thread=False
        )
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA query_only=ON")
        row = self._db.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()
        if row is None or int(row[0]) != SCHEMA_VERSION:
            raise ObservabilityLedgerError("observability-state-version-unsupported")

    def _create_schema(self) -> None:
        self._db.executescript(
            """
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS metadata(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS registrations(
                project_id TEXT PRIMARY KEY,
                generation INTEGER NOT NULL,
                record_json TEXT NOT NULL,
                key_sha256 TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS registration_history(
                project_id TEXT NOT NULL,
                generation INTEGER NOT NULL,
                record_json TEXT NOT NULL,
                recorded_at REAL NOT NULL,
                PRIMARY KEY(project_id, generation)
            );
            CREATE TABLE IF NOT EXISTS connections(
                project_id TEXT NOT NULL,
                generation INTEGER NOT NULL,
                connection_id TEXT NOT NULL,
                compatibility_sha256 TEXT NOT NULL,
                state TEXT NOT NULL,
                schema_state TEXT NOT NULL,
                connected_at REAL NOT NULL,
                disconnected_at REAL,
                PRIMARY KEY(project_id, generation, connection_id)
            );
            CREATE TABLE IF NOT EXISTS dispatches(
                project_id TEXT NOT NULL,
                dispatch_id TEXT PRIMARY KEY,
                record_json TEXT NOT NULL,
                revision INTEGER NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS dispatch_project_idx
                ON dispatches(project_id, created_at, dispatch_id);
            CREATE TABLE IF NOT EXISTS bindings(
                binding_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                generation INTEGER NOT NULL,
                connection_id TEXT NOT NULL,
                dispatch_id TEXT NOT NULL,
                thread_fingerprint TEXT NOT NULL,
                turn_fingerprint TEXT NOT NULL,
                state TEXT NOT NULL,
                record_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                UNIQUE(project_id, generation, thread_fingerprint, turn_fingerprint)
            );
            CREATE INDEX IF NOT EXISTS binding_dispatch_idx ON bindings(dispatch_id);
            CREATE TABLE IF NOT EXISTS completions(
                project_id TEXT NOT NULL,
                dispatch_id TEXT NOT NULL,
                binding_id TEXT NOT NULL,
                thread_fingerprint TEXT NOT NULL,
                turn_fingerprint TEXT NOT NULL,
                response_fingerprint TEXT NOT NULL,
                accounting_sha256 TEXT NOT NULL,
                cycle_ordinal INTEGER NOT NULL,
                record_json TEXT NOT NULL,
                PRIMARY KEY(project_id, thread_fingerprint, turn_fingerprint, response_fingerprint),
                UNIQUE(dispatch_id, cycle_ordinal)
            );
            CREATE TABLE IF NOT EXISTS pending_events(
                project_id TEXT NOT NULL,
                generation INTEGER NOT NULL,
                connection_id TEXT NOT NULL,
                event_kind TEXT NOT NULL,
                event_fingerprint TEXT NOT NULL,
                thread_fingerprint TEXT NOT NULL,
                turn_fingerprint TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                received_at REAL NOT NULL,
                PRIMARY KEY(project_id, event_kind, event_fingerprint)
            );
            CREATE INDEX IF NOT EXISTS pending_binding_idx ON pending_events(
                project_id, generation, connection_id, thread_fingerprint, turn_fingerprint
            );
            CREATE TABLE IF NOT EXISTS event_dedup(
                project_id TEXT NOT NULL,
                event_kind TEXT NOT NULL,
                event_fingerprint TEXT NOT NULL,
                dispatch_id TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                PRIMARY KEY(project_id, event_kind, event_fingerprint)
            );
            CREATE TABLE IF NOT EXISTS activity(
                dispatch_id TEXT PRIMARY KEY,
                available INTEGER NOT NULL,
                retry_notices INTEGER NOT NULL,
                completed INTEGER NOT NULL,
                failed INTEGER NOT NULL,
                interrupted INTEGER NOT NULL,
                tool_counts_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS health(
                project_id TEXT PRIMARY KEY,
                reason_counts_json TEXT NOT NULL,
                event_counts_json TEXT NOT NULL,
                known_gaps_json TEXT NOT NULL,
                queue_depth INTEGER NOT NULL,
                queue_state TEXT NOT NULL,
                ledger_state TEXT NOT NULL,
                publication_state INTEGER NOT NULL,
                last_event REAL
            );
            CREATE TABLE IF NOT EXISTS conflicts(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                dispatch_id TEXT,
                event_kind TEXT NOT NULL,
                event_fingerprint TEXT NOT NULL,
                observed_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS lifecycle_receipts(
                receipt_sha256 TEXT PRIMARY KEY,
                dispatch_id TEXT NOT NULL,
                lifecycle_state TEXT NOT NULL,
                timing_json TEXT NOT NULL,
                observed_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS publication_manifests(
                dispatch_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                manifest_sha256 TEXT NOT NULL,
                sample_timestamp REAL NOT NULL,
                samples_json TEXT NOT NULL,
                staged_at REAL NOT NULL,
                confirmed_at REAL,
                PRIMARY KEY(dispatch_id, revision)
            );
            INSERT OR IGNORE INTO metadata(key, value) VALUES('schema_version', '1');
            INSERT OR IGNORE INTO metadata(key, value) VALUES('snapshot_revision', '0');
            INSERT OR IGNORE INTO metadata(key, value) VALUES('capacity_blocked', '0');
            COMMIT;
            """
        )
        row = self._db.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()
        if row is None or int(row[0]) != SCHEMA_VERSION:
            raise ObservabilityLedgerError("observability-state-version-unsupported")

    def _protect_sqlite_files(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            path = self.state_dir / f"{DATABASE_NAME}{suffix}"
            if path.exists():
                metadata = path.lstat()
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.geteuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                ):
                    raise ObservabilityLedgerError(
                        "observability-database-path-invalid"
                    )

    def _write_required(self) -> None:
        if self.readonly:
            raise ObservabilityLedgerError("observability-ledger-readonly")
        if self._closed:
            raise ObservabilityLedgerError("observability-ledger-closed")

    @contextmanager
    def _transaction(self):
        self._write_required()
        with self._lock:
            if self._capacity_admission_blocked():
                self._persist_capacity_blocked()
                raise ObservabilityLedgerError("observability-capacity-blocked")
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except Exception as exc:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                if _is_sqlite_full(exc):
                    self._persist_capacity_blocked()
                    raise ObservabilityLedgerError(
                        "observability-capacity-blocked"
                    ) from exc
                raise
            else:
                self._db.execute("COMMIT")
                self._protect_sqlite_files()
                if (
                    self._capacity_limit_reached()
                    or self._database_page_capacity_reached()
                ):
                    self._persist_capacity_blocked()

    def _bump_global(self) -> int:
        value = (
            int(
                self._db.execute(
                    "SELECT value FROM metadata WHERE key='snapshot_revision'"
                ).fetchone()[0]
            )
            + 1
        )
        self._db.execute(
            "UPDATE metadata SET value=? WHERE key='snapshot_revision'", (str(value),)
        )
        return value

    def _bump_dispatch(self, dispatch_id: str) -> int:
        row = self._db.execute(
            "SELECT revision FROM dispatches WHERE dispatch_id=?", (dispatch_id,)
        ).fetchone()
        if row is None:
            raise ObservabilityLedgerError("observability-dispatch-unknown")
        revision = int(row[0]) + 1
        self._db.execute(
            "UPDATE dispatches SET revision=?, updated_at=? WHERE dispatch_id=?",
            (revision, _now(), dispatch_id),
        )
        self._bump_global()
        return revision

    def _registration(self, project_id: str) -> tuple[dict[str, Any], int]:
        row = self._db.execute(
            "SELECT record_json, generation FROM registrations WHERE project_id=?",
            (project_id,),
        ).fetchone()
        if row is None:
            raise ObservabilityLedgerError("observability-project-unregistered")
        record = _load(row[0])
        if record["state"] != RegistrationState.ENABLED.value:
            raise ObservabilityLedgerError("observability-project-revoked")
        return record, int(row[1])

    def _require_telemetry_ready(self, project_id: str) -> None:
        row = self._db.execute(
            "SELECT ledger_state FROM health WHERE project_id=?", (project_id,)
        ).fetchone()
        if row is not None and row[0] == "recovery":
            raise ObservabilityLedgerError("observability-telemetry-recovery")

    def _ensure_health(self, project_id: str) -> None:
        reasons = {reason.value: 0 for reason in HealthReason}
        events = {item.value: 0 for item in TelemetryDisposition}
        self._db.execute(
            """INSERT OR IGNORE INTO health(
                   project_id, reason_counts_json, event_counts_json, known_gaps_json,
                   queue_depth, queue_state, ledger_state, publication_state, last_event
               ) VALUES(?,?,?,?,0,?,?,?,NULL)""",
            (
                project_id,
                _json(reasons),
                _json(events),
                _json([]),
                QueueState.HEALTHY.value,
                "healthy",
                int(PublicationState.DISABLED),
            ),
        )

    def _health_event(
        self,
        project_id: str,
        disposition: TelemetryDisposition | str,
        *,
        reason: HealthReason | str | None = None,
        observed_at: float | None = None,
    ) -> None:
        self._ensure_health(project_id)
        row = self._db.execute(
            "SELECT reason_counts_json, event_counts_json FROM health WHERE project_id=?",
            (project_id,),
        ).fetchone()
        reasons = _load(row[0])
        events = _load(row[1])
        disposition_value = TelemetryDisposition(disposition).value
        events[disposition_value] += 1
        if reason is not None:
            reasons[HealthReason(reason).value] += 1
        self._db.execute(
            "UPDATE health SET reason_counts_json=?, event_counts_json=?, last_event=? WHERE project_id=?",
            (
                _json(reasons),
                _json(events),
                observed_at if observed_at is not None else _now(),
                project_id,
            ),
        )

    def register_project(self, value: Mapping[str, Any]) -> dict[str, Any]:
        record = normalize_project_registration(value)
        if record["owning_principal_id"] != os.geteuid():
            raise ObservabilityLedgerError("observability-owner-principal-mismatch")
        with self._lock:
            registered = self._db.execute(
                "SELECT key_sha256 FROM registrations WHERE project_id=?",
                (record["project_id"],),
            ).fetchone()
        if registered is not None:
            try:
                self._read_key(record["project_id"], expected_sha256=registered[0])
            except ObservabilityLedgerError:
                self._mark_key_unavailable(record["project_id"])
                raise
        encoded = _json(record)
        now = _now()
        with self._transaction():
            current = self._db.execute(
                """SELECT generation,record_json,key_sha256
                   FROM registrations WHERE project_id=?""",
                (record["project_id"],),
            ).fetchone()
            if current is not None:
                prior = _load(current[1])
                if (
                    prior["state"] == RegistrationState.REVOKED.value
                    and record["state"] != prior["state"]
                ):
                    raise ObservabilityLedgerError(
                        "observability-project-revocation-terminal"
                    )
                if record["generation"] < int(current[0]):
                    raise ObservabilityLedgerError(
                        "observability-registration-generation-stale"
                    )
                if record["generation"] == int(current[0]):
                    if encoded == current[1]:
                        return record
                    raise ObservabilityLedgerError(
                        "observability-registration-generation-conflict"
                    )
                self._retire_registration_generation(
                    record["project_id"], int(current[0])
                )
            else:
                count = int(
                    self._db.execute("SELECT COUNT(*) FROM registrations").fetchone()[0]
                )
                existing_limits = [
                    _load(row[0])["resource_limits"]["max_registrations"]
                    for row in self._db.execute(
                        "SELECT record_json FROM registrations"
                    ).fetchall()
                ]
                effective_limit = min(
                    [record["resource_limits"]["max_registrations"], *existing_limits]
                )
                if count >= effective_limit:
                    raise ObservabilityLedgerError(
                        "observability-registration-capacity"
                    )
            if current is None:
                key_sha256 = hashlib.sha256(
                    self._create_key(record["project_id"])
                ).hexdigest()
            else:
                key_sha256 = current[2]
            self._db.execute(
                "INSERT OR REPLACE INTO registrations VALUES(?,?,?,?,?)",
                (
                    record["project_id"],
                    record["generation"],
                    encoded,
                    key_sha256,
                    now,
                ),
            )
            self._db.execute(
                "INSERT INTO registration_history VALUES(?,?,?,?)",
                (record["project_id"], record["generation"], encoded, now),
            )
            self._ensure_health(record["project_id"])
            self._enforce_disk_limit()
            self._bump_global()
        return record

    def _retire_registration_generation(
        self,
        project_id: str,
        generation: int,
    ) -> None:
        now = _now()
        self._db.execute(
            """UPDATE connections SET state=?,disconnected_at=?
               WHERE project_id=? AND generation=? AND state=?""",
            (
                ConnectionState.DISCONNECTED.value,
                now,
                project_id,
                generation,
                ConnectionState.CONNECTED.value,
            ),
        )
        binding_rows = self._db.execute(
            """SELECT binding_id,dispatch_id,record_json FROM bindings
               WHERE project_id=? AND generation=? AND state=?""",
            (project_id, generation, BindingState.ACTIVE.value),
        ).fetchall()
        for binding_row in binding_rows:
            binding = _load(binding_row["record_json"])
            binding["state"] = BindingState.REVOKED.value
            self._db.execute(
                "UPDATE bindings SET state=?,record_json=? WHERE binding_id=?",
                (
                    BindingState.REVOKED.value,
                    _json(binding),
                    binding_row["binding_id"],
                ),
            )
        affected = {row["dispatch_id"] for row in binding_rows}
        if affected:
            self._add_gap(project_id, CoverageGap.SOURCE_DISCONNECT)
        for dispatch_id in affected:
            self._set_dispatch_coverage(dispatch_id, CoverageState.KNOWN_GAP)
            self._bump_dispatch(dispatch_id)

    def _enforce_disk_limit(self) -> None:
        limits = [
            _load(row[0])["resource_limits"]["max_disk_bytes"]
            for row in self._db.execute("SELECT record_json FROM registrations")
        ]
        if not limits:
            return
        page_size = int(self._db.execute("PRAGMA page_size").fetchone()[0])
        database_cap, _reserve = _capacity_envelope(min(limits))
        pages = database_cap // page_size
        current = int(self._db.execute("PRAGMA page_count").fetchone()[0])
        if pages < current:
            raise ObservabilityLedgerError("observability-disk-limit-below-ledger")
        self._db.execute(f"PRAGMA max_page_count={pages}")

    def _capacity_limit(self) -> int | None:
        limits = [
            _load(row[0])["resource_limits"]["max_disk_bytes"]
            for row in self._db.execute("SELECT record_json FROM registrations")
        ]
        return min(limits) if limits else None

    def _capacity_limit_reached(self) -> bool:
        limit = self._capacity_limit()
        if limit is None:
            return False
        _database_cap, reserve = _capacity_envelope(limit)
        return self._protected_state_bytes() + reserve >= limit

    def _database_page_capacity_reached(self) -> bool:
        page_count = int(self._db.execute("PRAGMA page_count").fetchone()[0])
        maximum_pages = int(self._db.execute("PRAGMA max_page_count").fetchone()[0])
        # Keep a quarter of the capped database, with at least sixteen pages,
        # available for the one bounded normalized write and the capacity
        # marker. SQLITE_FULL is also translated below as a final backstop.
        headroom = max(16, maximum_pages // 4)
        return page_count + headroom >= maximum_pages

    def _capacity_admission_blocked(self) -> bool:
        marker = self._db.execute(
            "SELECT value FROM metadata WHERE key=?", (SQLITE_CAPACITY_MARKER,)
        ).fetchone()
        return (
            bool(marker is not None and marker[0] == "1")
            or self._capacity_limit_reached()
            or self._database_page_capacity_reached()
        )

    def _persist_capacity_blocked(self) -> None:
        marker = self._db.execute(
            "SELECT value FROM metadata WHERE key=?", (SQLITE_CAPACITY_MARKER,)
        ).fetchone()
        marker_needed = marker is None or marker[0] != "1"
        if not marker_needed:
            return
        self._db.execute("BEGIN IMMEDIATE")
        try:
            self._db.execute(
                "INSERT OR REPLACE INTO metadata(key,value) VALUES(?, '1')",
                (SQLITE_CAPACITY_MARKER,),
            )
            for row in self._db.execute(
                "SELECT project_id,reason_counts_json,known_gaps_json FROM health"
            ).fetchall():
                reasons = _load(row["reason_counts_json"])
                reasons[HealthReason.DISK_FULL.value] = (
                    int(reasons.get(HealthReason.DISK_FULL.value, 0)) + 1
                )
                gaps = set(_load(row["known_gaps_json"]))
                gaps.add(CoverageGap.COLLECTOR_WRITE_FAILURE.value)
                self._db.execute(
                    """UPDATE health SET reason_counts_json=?,known_gaps_json=?,
                              ledger_state='fault',publication_state=?
                       WHERE project_id=?""",
                    (
                        _json(reasons),
                        _json(sorted(gaps)),
                        int(PublicationState.CAPACITY_BLOCKED),
                        row["project_id"],
                    ),
                )
            dispatch_rows = self._db.execute(
                "SELECT dispatch_id,record_json FROM dispatches"
            ).fetchall()
            for row in dispatch_rows:
                record = _load(row["record_json"])
                record["coverage_state"] = max(
                    int(record["coverage_state"]),
                    int(CoverageState.KNOWN_GAP),
                )
                self._db.execute(
                    """UPDATE dispatches SET record_json=?,revision=revision+1,
                              updated_at=? WHERE dispatch_id=?""",
                    (_json(record), _now(), row["dispatch_id"]),
                )
            self._bump_global()
        except Exception:
            self._db.execute("ROLLBACK")
            raise
        else:
            self._db.execute("COMMIT")
            self._protect_sqlite_files()

    def project_resource_limits(self, project_id: str) -> dict[str, int]:
        with self._lock:
            registration, _generation = self._registration(project_id)
            return dict(registration["resource_limits"])

    def _key_path(self, project_id: str) -> Path:
        if not isinstance(project_id, str) or any(
            ch not in "0123456789abcdef-" for ch in project_id
        ):
            raise ObservabilityLedgerError("observability-project-id-invalid")
        return self.state_dir / KEY_DIRECTORY_NAME / f"{project_id}.key"

    def _read_key(
        self,
        project_id: str,
        *,
        expected_sha256: str | None = None,
    ) -> bytes:
        path = self._key_path(project_id)
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            raise ObservabilityLedgerError("observability-key-unavailable") from exc
        try:
            metadata = os.fstat(fd)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise ObservabilityLedgerError("observability-key-path-invalid")
            key = os.read(fd, 33)
        finally:
            os.close(fd)
        if len(key) != 32:
            raise ObservabilityLedgerError("observability-key-invalid")
        if expected_sha256 is not None and not hmac.compare_digest(
            hashlib.sha256(key).hexdigest(), expected_sha256
        ):
            raise ObservabilityLedgerError("observability-key-mismatch")
        return key

    def _create_key(self, project_id: str) -> bytes:
        path = self._key_path(project_id)
        try:
            return self._read_key(project_id)
        except ObservabilityLedgerError:
            pass
        key = secrets.token_bytes(32)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags, 0o600)
        except OSError as exc:
            raise ObservabilityLedgerError("observability-key-create-failed") from exc
        try:
            os.write(fd, key)
            os.fsync(fd)
        finally:
            os.close(fd)
        return key

    def _mark_key_unavailable(self, project_id: str) -> None:
        with self._transaction():
            self._ensure_health(project_id)
            self._health_event(
                project_id,
                TelemetryDisposition.REJECTED,
                reason=HealthReason.KEY_UNAVAILABLE,
            )
            self._add_gap(project_id, CoverageGap.KEY_LOSS)
            self._db.execute(
                "UPDATE health SET ledger_state='recovery' WHERE project_id=?",
                (project_id,),
            )
            self._bump_global()

    def _recover_missing_keys(self) -> None:
        rows = self._db.execute(
            "SELECT project_id,key_sha256 FROM registrations"
        ).fetchall()
        for row in rows:
            try:
                self._read_key(row[0], expected_sha256=row[1])
            except ObservabilityLedgerError:
                self._mark_key_unavailable(row[0])

    def _clear_key_recovery(self, project_id: str) -> None:
        with self._transaction():
            row = self._db.execute(
                "SELECT ledger_state FROM health WHERE project_id=?", (project_id,)
            ).fetchone()
            if row is not None and row[0] == "recovery":
                self._db.execute(
                    "UPDATE health SET ledger_state='healthy' WHERE project_id=?",
                    (project_id,),
                )
                self._bump_global()

    def source_fingerprinter(self, project_id: str):
        self._write_required()
        with self._lock:
            self._registration(project_id)
            expected = self._db.execute(
                "SELECT key_sha256 FROM registrations WHERE project_id=?",
                (project_id,),
            ).fetchone()[0]
        try:
            key = self._read_key(project_id, expected_sha256=expected)
        except ObservabilityLedgerError:
            self._mark_key_unavailable(project_id)
            raise
        self._clear_key_recovery(project_id)

        def fingerprint(domain: str, source_id: str) -> str:
            if domain not in {"thread", "turn", "response", "event"}:
                raise ObservabilityLedgerError(
                    "observability-fingerprint-domain-invalid"
                )
            if (
                not isinstance(source_id, str)
                or not source_id
                or len(source_id.encode("utf-8")) > 4096
            ):
                raise ObservabilityLedgerError("observability-source-id-invalid")
            material = (
                b"cwo-observability-v1\0"
                + domain.encode("ascii")
                + b"\0"
                + source_id.encode("utf-8")
            )
            return hmac.new(key, material, hashlib.sha256).hexdigest()

        return fingerprint

    def fingerprint_source_id(
        self, project_id: str, domain: str, source_id: str
    ) -> str:
        return self.source_fingerprinter(project_id)(domain, source_id)

    def register_connection(
        self,
        project_id: str,
        generation: int,
        connection_id: str,
        source_compatibility_sha256: str,
        *,
        schema_state: SchemaState | str = SchemaState.COMPATIBLE,
        connected_at_seconds: float | None = None,
    ) -> None:
        compatibility = _require_sha256(
            source_compatibility_sha256, "source-compatibility"
        )
        schema = SchemaState(schema_state).value
        connected_at = (
            _now() if connected_at_seconds is None else float(connected_at_seconds)
        )
        with self._transaction():
            _record, current_generation = self._registration(project_id)
            self._require_telemetry_ready(project_id)
            if generation != current_generation:
                raise ObservabilityLedgerError(
                    "observability-connection-generation-mismatch"
                )
            existing = self._db.execute(
                "SELECT compatibility_sha256, state, schema_state FROM connections WHERE project_id=? AND generation=? AND connection_id=?",
                (project_id, generation, connection_id),
            ).fetchone()
            if existing is not None:
                if tuple(existing) == (
                    compatibility,
                    ConnectionState.CONNECTED.value,
                    schema,
                ):
                    return
                raise ObservabilityLedgerError("observability-connection-conflict")
            self._db.execute(
                "INSERT INTO connections VALUES(?,?,?,?,?,?,?,NULL)",
                (
                    project_id,
                    generation,
                    connection_id,
                    compatibility,
                    ConnectionState.CONNECTED.value,
                    schema,
                    connected_at,
                ),
            )
            self._bump_global()

    def mark_source_incompatible(
        self,
        project_id: str,
        generation: int,
        connection_id: str,
        *,
        reason: HealthReason | str = HealthReason.SCHEMA_INCOMPATIBLE,
        count: int = 1,
    ) -> None:
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ObservabilityLedgerError("observability-health-count-invalid")
        normalized_reason = HealthReason(reason)
        if normalized_reason not in {
            HealthReason.SCHEMA_INCOMPATIBLE,
            HealthReason.INVALID_IDENTITY,
        }:
            raise ObservabilityLedgerError("observability-schema-reason-invalid")
        with self._transaction():
            _record, current_generation = self._registration(project_id)
            if generation != current_generation:
                raise ObservabilityLedgerError(
                    "observability-connection-generation-mismatch"
                )
            connection = self._db.execute(
                """SELECT state FROM connections
                   WHERE project_id=? AND generation=? AND connection_id=?""",
                (project_id, generation, connection_id),
            ).fetchone()
            if connection is None or connection[0] != ConnectionState.CONNECTED.value:
                raise ObservabilityLedgerError(
                    "observability-binding-connection-unavailable"
                )
            self._db.execute(
                """UPDATE connections SET schema_state=?
                   WHERE project_id=? AND generation=? AND connection_id=?""",
                (
                    SchemaState.INCOMPATIBLE.value,
                    project_id,
                    generation,
                    connection_id,
                ),
            )
            for _ in range(count):
                self._health_event(
                    project_id,
                    TelemetryDisposition.REJECTED,
                    reason=normalized_reason,
                )
            self._add_gap(project_id, CoverageGap.UNSUPPORTED_SOURCE)
            for dispatch in self._db.execute(
                "SELECT dispatch_id FROM dispatches WHERE project_id=?",
                (project_id,),
            ).fetchall():
                self._set_dispatch_coverage(
                    dispatch["dispatch_id"], CoverageState.KNOWN_GAP
                )
                self._bump_dispatch(dispatch["dispatch_id"])
            self._bump_global()

    def record_submission(self, value: Mapping[str, Any]) -> dict[str, Any]:
        record = normalize_dispatch_observation(
            value,
            executor_vocabulary=self.executor_vocabulary,
            model_vocabulary=self.model_vocabulary,
            effort_vocabulary=self.effort_vocabulary,
        )
        encoded = _json(record)
        now = _now()
        with self._transaction():
            self._registration(record["project_id"])
            existing = self._db.execute(
                "SELECT record_json FROM dispatches WHERE dispatch_id=?",
                (record["dispatch_id"],),
            ).fetchone()
            if existing is not None:
                if existing[0] == encoded:
                    return record
                raise ObservabilityLedgerError("observability-dispatch-conflict")
            self._db.execute(
                "INSERT INTO dispatches VALUES(?,?,?,?,?,?)",
                (record["project_id"], record["dispatch_id"], encoded, 1, now, now),
            )
            self._db.execute(
                "INSERT INTO activity VALUES(?,0,0,0,0,0,?)",
                (record["dispatch_id"], _json({})),
            )
            self._bump_global()
        return record

    def record_lifecycle(
        self,
        dispatch_id: str,
        *,
        lifecycle_state: LifecycleState | str,
        timing: Mapping[str, Any],
        receipt_sha256: str,
    ) -> dict[str, Any]:
        receipt = _require_sha256(receipt_sha256, "lifecycle-receipt")
        state = LifecycleState(lifecycle_state).value
        with self._transaction():
            row = self._db.execute(
                "SELECT record_json FROM dispatches WHERE dispatch_id=?",
                (dispatch_id,),
            ).fetchone()
            if row is None:
                raise ObservabilityLedgerError("observability-dispatch-unknown")
            current = _load(row[0])
            incoming_timing = dict(timing)
            prior_epoch = current["timing"]["clock_epoch_id"]
            incoming_epoch = incoming_timing.get("clock_epoch_id")
            clock_gap = incoming_epoch != prior_epoch
            if clock_gap:
                # The schema has one epoch for all timing points. Preserve the
                # original epoch and its attributable points; the later receipt
                # still advances lifecycle truth, but cannot establish duration.
                incoming_timing = dict(current["timing"])
                incoming_timing["elapsed_seconds"] = None
                incoming_timing["elapsed_state"] = int(FieldState.CLOCK_GAP)
            candidate = {
                **current,
                "lifecycle_state": state,
                "timing": incoming_timing,
            }
            normalized = normalize_dispatch_observation(
                candidate,
                executor_vocabulary=self.executor_vocabulary,
                model_vocabulary=self.model_vocabulary,
                effort_vocabulary=self.effort_vocabulary,
            )
            seen = self._db.execute(
                "SELECT dispatch_id, lifecycle_state, timing_json FROM lifecycle_receipts WHERE receipt_sha256=?",
                (receipt,),
            ).fetchone()
            timing_json = _json(normalized["timing"])
            if seen is not None:
                if tuple(seen) == (dispatch_id, state, timing_json):
                    return current
                raise ObservabilityLedgerError(
                    "observability-lifecycle-receipt-conflict"
                )
            validate_lifecycle_transition(current["lifecycle_state"], state)
            if clock_gap:
                normalized["coverage_state"] = max(
                    int(normalized["coverage_state"]),
                    int(CoverageState.KNOWN_GAP),
                )
                self._add_gap(normalized["project_id"], CoverageGap.TIMING_EPOCH_GAP)
            self._db.execute(
                "UPDATE dispatches SET record_json=? WHERE dispatch_id=?",
                (_json(normalized), dispatch_id),
            )
            self._db.execute(
                "INSERT INTO lifecycle_receipts VALUES(?,?,?,?,?)",
                (receipt, dispatch_id, state, _json(normalized["timing"]), _now()),
            )
            self._bump_dispatch(dispatch_id)
            return normalized

    def reconcile_controller_receipts(
        self, receipts: Iterable[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        results = []
        for receipt in receipts:
            results.append(
                self.record_lifecycle(
                    str(receipt["dispatch_id"]),
                    lifecycle_state=receipt["lifecycle_state"],
                    timing=receipt["timing"],
                    receipt_sha256=str(receipt["receipt_sha256"]),
                )
            )
        return results

    def bind_runtime(self, value: Mapping[str, Any]) -> dict[str, Any]:
        record = normalize_runtime_binding(
            value,
            model_vocabulary=self.model_vocabulary,
            effort_vocabulary=self.effort_vocabulary,
        )
        encoded = _json(record)
        ownership_conflict = False
        with self._transaction():
            _registration, generation = self._registration(record["project_id"])
            self._require_telemetry_ready(record["project_id"])
            if generation != record["registration_generation"]:
                raise ObservabilityLedgerError(
                    "observability-binding-generation-mismatch"
                )
            dispatch = self._db.execute(
                "SELECT project_id, record_json FROM dispatches WHERE dispatch_id=?",
                (record["dispatch_id"],),
            ).fetchone()
            if dispatch is None or dispatch[0] != record["project_id"]:
                raise ObservabilityLedgerError(
                    "observability-binding-dispatch-mismatch"
                )
            observed = _load(dispatch[1])
            for field in (
                "agent_id",
                "packet_ref",
                "packet_sha256",
                "supervisor_submission_ref",
            ):
                if record[field] != observed[field]:
                    raise ObservabilityLedgerError(
                        "observability-binding-authority-mismatch"
                    )
            connection = self._db.execute(
                "SELECT state, schema_state FROM connections WHERE project_id=? AND generation=? AND connection_id=?",
                (record["project_id"], generation, record["connection_id"]),
            ).fetchone()
            if connection is None or connection[0] != ConnectionState.CONNECTED.value:
                raise ObservabilityLedgerError(
                    "observability-binding-connection-unavailable"
                )
            existing = self._db.execute(
                "SELECT record_json FROM bindings WHERE binding_id=?",
                (record["binding_id"],),
            ).fetchone()
            if existing is not None:
                if existing[0] == encoded:
                    return record
                raise ObservabilityLedgerError("observability-binding-conflict")
            try:
                self._db.execute(
                    "INSERT INTO bindings VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        record["binding_id"],
                        record["project_id"],
                        generation,
                        record["connection_id"],
                        record["dispatch_id"],
                        record["thread_fingerprint"],
                        record["turn_fingerprint"],
                        record["state"],
                        encoded,
                        _now(),
                    ),
                )
            except sqlite3.IntegrityError:
                self._health_event(
                    record["project_id"],
                    TelemetryDisposition.CONFLICT,
                    reason=HealthReason.BINDING_CONFLICT,
                )
                self._bump_global()
                ownership_conflict = True
            if not ownership_conflict:
                if connection[1] == SchemaState.COMPATIBLE.value:
                    self._db.execute(
                        "UPDATE activity SET available=1 WHERE dispatch_id=?",
                        (record["dispatch_id"],),
                    )
                    health = self._db.execute(
                        "SELECT known_gaps_json FROM health WHERE project_id=?",
                        (record["project_id"],),
                    ).fetchone()
                    known_gaps = set(_load(health[0])) if health is not None else set()
                    self._set_dispatch_coverage(
                        record["dispatch_id"],
                        (
                            CoverageState.KNOWN_GAP
                            if known_gaps
                            else CoverageState.OBSERVED_NO_KNOWN_GAP
                        ),
                    )
                self._bump_dispatch(record["dispatch_id"])
                self._drain_pending_for_binding(record)
        if ownership_conflict:
            raise ObservabilityLedgerError("observability-binding-ownership-conflict")
        return record

    def _find_binding(self, payload: Mapping[str, Any]) -> sqlite3.Row | None:
        rows = self._db.execute(
            """SELECT b.*, c.compatibility_sha256, c.schema_state
               FROM bindings b JOIN connections c
                 ON c.project_id=b.project_id AND c.generation=b.generation
                AND c.connection_id=b.connection_id
               WHERE b.project_id=? AND b.generation=? AND b.connection_id=?
                 AND b.thread_fingerprint=? AND b.turn_fingerprint=?
                 AND b.state=? AND c.state=?""",
            (
                payload["project_id"],
                payload["registration_generation"],
                payload["connection_id"],
                payload["thread_fingerprint"],
                payload["turn_fingerprint"],
                BindingState.ACTIVE.value,
                ConnectionState.CONNECTED.value,
            ),
        ).fetchall()
        if len(rows) > 1:
            raise ObservabilityLedgerError("observability-binding-ambiguous")
        return rows[0] if rows else None

    def ingest_completion(self, payload: Mapping[str, Any]) -> str:
        return self.ingest_projection("completion", payload)

    def _normalize_projection(
        self,
        event_kind: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise ObservabilityLedgerError("observability-event-not-mapping")
        common = {
            "project_id",
            "registration_generation",
            "connection_id",
            "thread_fingerprint",
            "turn_fingerprint",
            "event_fingerprint",
            "observed_at_seconds",
        }
        optional_common = {"received_monotonic_ns"}
        specific = {
            "completion": {
                "response_fingerprint",
                "usage",
                "purpose",
                "runtime_emitted_at_seconds",
            },
            "retry": set(),
            "turn_outcome": {"outcome"},
            "tool_event": {"category", "lifecycle"},
        }[event_kind]
        allowed = common | optional_common | specific
        if set(payload).difference(allowed) or common.difference(payload):
            raise ObservabilityLedgerError("observability-event-fields-invalid")
        safe = {
            "project_id": _require_uuid(payload["project_id"], "project-id"),
            "connection_id": _require_uuid(payload["connection_id"], "connection-id"),
        }
        generation = payload["registration_generation"]
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 1
        ):
            raise ObservabilityLedgerError("observability-event-generation-invalid")
        safe["registration_generation"] = generation
        for field in ("thread_fingerprint", "turn_fingerprint", "event_fingerprint"):
            safe[field] = _require_sha256(payload[field], field)
        observed = payload["observed_at_seconds"]
        if (
            isinstance(observed, bool)
            or not isinstance(observed, (int, float))
            or observed < 0
            or not isfinite(observed)
        ):
            raise ObservabilityLedgerError("observability-event-time-invalid")
        safe["observed_at_seconds"] = observed
        if "received_monotonic_ns" in payload:
            monotonic = payload["received_monotonic_ns"]
            if (
                isinstance(monotonic, bool)
                or not isinstance(monotonic, int)
                or monotonic < 0
            ):
                raise ObservabilityLedgerError("observability-event-monotonic-invalid")
            safe["received_monotonic_ns"] = monotonic
        if event_kind == "completion":
            safe["response_fingerprint"] = _require_sha256(
                payload.get("response_fingerprint"), "response-fingerprint"
            )
            usage = payload.get("usage")
            if usage is None:
                safe["usage"] = None
            elif isinstance(usage, Mapping) and set(usage) == {
                kind.value for kind in TokenKind
            }:
                safe["usage"] = {
                    kind.value: (
                        usage[kind.value]
                        if (
                            usage[kind.value] is None
                            or (
                                not isinstance(usage[kind.value], bool)
                                and isinstance(usage[kind.value], int)
                                and usage[kind.value] >= 0
                            )
                        )
                        else "invalid"
                    )
                    for kind in TokenKind
                }
            else:
                safe["usage"] = "invalid"
            try:
                safe["purpose"] = CompletionPurpose(payload.get("purpose")).value
            except (TypeError, ValueError):
                safe["purpose"] = CompletionPurpose.UNKNOWN.value
            emitted = payload.get("runtime_emitted_at_seconds")
            if emitted is not None and (
                isinstance(emitted, bool)
                or not isinstance(emitted, (int, float))
                or emitted < 0
                or not isfinite(emitted)
            ):
                emitted = None
            safe["runtime_emitted_at_seconds"] = emitted
        elif event_kind == "turn_outcome":
            safe["outcome"] = TurnOutcome(payload.get("outcome")).value
        elif event_kind == "tool_event":
            safe["category"] = ToolCategory(payload.get("category")).value
            safe["lifecycle"] = ToolLifecycle(payload.get("lifecycle")).value
        return safe

    def ingest_projection(self, event_kind: str, payload: Mapping[str, Any]) -> str:
        if event_kind not in {"completion", "retry", "turn_outcome", "tool_event"}:
            raise ObservabilityLedgerError("observability-event-kind-unsupported")
        safe = self._normalize_projection(event_kind, payload)
        encoded = _json(safe)
        if len(encoded.encode("utf-8")) > MAX_NORMALIZED_RECORD_BYTES:
            raise ObservabilityLedgerError("observability-event-too-large")
        with self._transaction():
            _registration, current_generation = self._registration(
                str(safe["project_id"])
            )
            self._require_telemetry_ready(str(safe["project_id"]))
            self._expire_pending(str(safe["project_id"]))
            if safe["registration_generation"] != current_generation:
                self._health_event(
                    safe["project_id"],
                    TelemetryDisposition.REJECTED,
                    reason=HealthReason.INVALID_IDENTITY,
                )
                self._bump_global()
                return TelemetryDisposition.REJECTED.value
            connection = self._db.execute(
                """SELECT state FROM connections
                   WHERE project_id=? AND generation=? AND connection_id=?""",
                (
                    safe["project_id"],
                    safe["registration_generation"],
                    safe["connection_id"],
                ),
            ).fetchone()
            if connection is None or connection[0] != ConnectionState.CONNECTED.value:
                self._health_event(
                    safe["project_id"],
                    TelemetryDisposition.REJECTED,
                    reason=(
                        HealthReason.SOURCE_DISCONNECTED
                        if connection is not None
                        else HealthReason.INVALID_IDENTITY
                    ),
                )
                self._bump_global()
                return TelemetryDisposition.REJECTED.value
            binding = self._find_binding(safe)
            if binding is None:
                result = self._store_pending(event_kind, safe)
            else:
                result = self._apply_bound_projection(event_kind, safe, binding)
        return result

    def _store_pending(self, event_kind: str, payload: Mapping[str, Any]) -> str:
        project_id = str(payload["project_id"])
        registration_row = self._db.execute(
            "SELECT record_json FROM registrations WHERE project_id=?", (project_id,)
        ).fetchone()
        registration = _load(registration_row[0])
        existing = self._db.execute(
            "SELECT payload_json FROM pending_events WHERE project_id=? AND event_kind=? AND event_fingerprint=?",
            (project_id, event_kind, payload["event_fingerprint"]),
        ).fetchone()
        encoded = _json(payload)
        if existing is not None:
            disposition = (
                TelemetryDisposition.DUPLICATE
                if _semantic_payload(_load(existing[0])) == _semantic_payload(payload)
                else TelemetryDisposition.CONFLICT
            )
            self._health_event(
                project_id,
                disposition,
                reason=(
                    HealthReason.ACCOUNTING_CONFLICT
                    if disposition is TelemetryDisposition.CONFLICT
                    else None
                ),
            )
            self._bump_global()
            return disposition.value
        count = int(
            self._db.execute(
                "SELECT COUNT(*) FROM pending_events WHERE project_id=?", (project_id,)
            ).fetchone()[0]
        )
        if count >= registration["resource_limits"]["max_pending_records"]:
            self._health_event(
                project_id,
                TelemetryDisposition.LOST,
                reason=HealthReason.QUEUE_OVERFLOW,
            )
            self._add_gap(project_id, CoverageGap.QUEUE_OVERFLOW)
            self._mark_project_nonterminal_gap(project_id)
            self._bump_global()
            return TelemetryDisposition.LOST.value
        self._db.execute(
            "INSERT INTO pending_events VALUES(?,?,?,?,?,?,?,?,?)",
            (
                project_id,
                payload["registration_generation"],
                payload["connection_id"],
                event_kind,
                payload["event_fingerprint"],
                payload["thread_fingerprint"],
                payload["turn_fingerprint"],
                encoded,
                payload["observed_at_seconds"],
            ),
        )
        self._health_event(project_id, TelemetryDisposition.UNASSIGNED)
        self._bump_global()
        return TelemetryDisposition.UNASSIGNED.value

    def _drain_pending_for_binding(self, binding: Mapping[str, Any]) -> None:
        rows = self._db.execute(
            """SELECT event_kind, payload_json FROM pending_events
               WHERE project_id=? AND generation=? AND connection_id=?
                 AND thread_fingerprint=? AND turn_fingerprint=? ORDER BY received_at""",
            (
                binding["project_id"],
                binding["registration_generation"],
                binding["connection_id"],
                binding["thread_fingerprint"],
                binding["turn_fingerprint"],
            ),
        ).fetchall()
        row_binding = self._db.execute(
            "SELECT b.*, c.compatibility_sha256, c.schema_state FROM bindings b JOIN connections c ON c.project_id=b.project_id AND c.generation=b.generation AND c.connection_id=b.connection_id WHERE b.binding_id=?",
            (binding["binding_id"],),
        ).fetchone()
        for row in rows:
            payload = _load(row[1])
            self._apply_bound_projection(row[0], payload, row_binding)
            self._db.execute(
                "DELETE FROM pending_events WHERE project_id=? AND event_kind=? AND event_fingerprint=?",
                (payload["project_id"], row[0], payload["event_fingerprint"]),
            )

    def _apply_bound_projection(
        self, event_kind: str, payload: Mapping[str, Any], binding: sqlite3.Row
    ) -> str:
        project_id = str(payload["project_id"])
        dispatch_id = str(binding["dispatch_id"])
        payload_sha = hashlib.sha256(_semantic_payload(payload).encode()).hexdigest()
        prior = self._db.execute(
            "SELECT dispatch_id, payload_sha256 FROM event_dedup WHERE project_id=? AND event_kind=? AND event_fingerprint=?",
            (project_id, event_kind, payload["event_fingerprint"]),
        ).fetchone()
        if prior is not None:
            disposition = (
                TelemetryDisposition.DUPLICATE
                if tuple(prior) == (dispatch_id, payload_sha)
                else TelemetryDisposition.CONFLICT
            )
            if disposition is TelemetryDisposition.CONFLICT:
                self._record_conflict(
                    project_id, dispatch_id, event_kind, payload["event_fingerprint"]
                )
            self._health_event(
                project_id,
                disposition,
                reason=(
                    HealthReason.ACCOUNTING_CONFLICT
                    if disposition is TelemetryDisposition.CONFLICT
                    else None
                ),
            )
            if disposition is TelemetryDisposition.DUPLICATE:
                self._bump_global()
            return disposition.value
        if binding["schema_state"] != SchemaState.COMPATIBLE.value:
            self._health_event(
                project_id,
                TelemetryDisposition.REJECTED,
                reason=HealthReason.SCHEMA_INCOMPATIBLE,
            )
            self._bump_global()
            return TelemetryDisposition.REJECTED.value
        if event_kind == "completion":
            identity = self._db.execute(
                """SELECT dispatch_id,accounting_sha256 FROM completions
                   WHERE project_id=? AND thread_fingerprint=?
                     AND turn_fingerprint=? AND response_fingerprint=?""",
                (
                    project_id,
                    payload["thread_fingerprint"],
                    payload["turn_fingerprint"],
                    payload["response_fingerprint"],
                ),
            ).fetchone()
            if identity is not None:
                accounting_sha = self._completion_accounting_sha(payload, binding)
                self._db.execute(
                    "INSERT INTO event_dedup VALUES(?,?,?,?,?)",
                    (
                        project_id,
                        event_kind,
                        payload["event_fingerprint"],
                        dispatch_id,
                        payload_sha,
                    ),
                )
                if tuple(identity) == (dispatch_id, accounting_sha):
                    self._health_event(project_id, TelemetryDisposition.DUPLICATE)
                    self._bump_global()
                    return TelemetryDisposition.DUPLICATE.value
                affected = {dispatch_id, str(identity["dispatch_id"])}
                for affected_dispatch in affected:
                    self._record_conflict(
                        project_id,
                        affected_dispatch,
                        event_kind,
                        payload["response_fingerprint"],
                    )
                self._health_event(
                    project_id,
                    TelemetryDisposition.CONFLICT,
                    reason=HealthReason.ACCOUNTING_CONFLICT,
                )
                return TelemetryDisposition.CONFLICT.value
            self._insert_completion(payload, binding)
        else:
            self._increment_activity(dispatch_id, event_kind, payload)
        self._db.execute(
            "INSERT INTO event_dedup VALUES(?,?,?,?,?)",
            (
                project_id,
                event_kind,
                payload["event_fingerprint"],
                dispatch_id,
                payload_sha,
            ),
        )
        self._health_event(
            project_id,
            TelemetryDisposition.ACCEPTED,
            observed_at=float(payload["observed_at_seconds"]),
        )
        self._bump_dispatch(dispatch_id)
        return TelemetryDisposition.ACCEPTED.value

    def _completion_accounting_sha(
        self,
        payload: Mapping[str, Any],
        binding: sqlite3.Row,
    ) -> str:
        candidate = {
            "binding_id": binding["binding_id"],
            "project_id": payload["project_id"],
            "dispatch_id": binding["dispatch_id"],
            "thread_fingerprint": payload["thread_fingerprint"],
            "turn_fingerprint": payload["turn_fingerprint"],
            "response_fingerprint": payload["response_fingerprint"],
            "cycle_ordinal": 1,
            "usage": payload.get("usage"),
            "purpose": payload.get("purpose", "unknown"),
            "source_compatibility_sha256": binding["compatibility_sha256"],
            "observed_at_seconds": payload["observed_at_seconds"],
            "runtime_emitted_at_seconds": payload.get("runtime_emitted_at_seconds"),
        }
        completion = normalize_model_completion(candidate)
        accounting = {
            "usage": completion["usage"],
            "purpose": completion["purpose"],
            "source_compatibility_sha256": completion["source_compatibility_sha256"],
        }
        return hashlib.sha256(_json(accounting).encode()).hexdigest()

    def _insert_completion(
        self, payload: Mapping[str, Any], binding: sqlite3.Row
    ) -> None:
        dispatch_id = str(binding["dispatch_id"])
        ordinal = (
            int(
                self._db.execute(
                    "SELECT COUNT(*) FROM completions WHERE dispatch_id=?",
                    (dispatch_id,),
                ).fetchone()[0]
            )
            + 1
        )
        candidate = {
            "binding_id": binding["binding_id"],
            "project_id": payload["project_id"],
            "dispatch_id": dispatch_id,
            "thread_fingerprint": payload["thread_fingerprint"],
            "turn_fingerprint": payload["turn_fingerprint"],
            "response_fingerprint": payload.get(
                "response_fingerprint", payload["event_fingerprint"]
            ),
            "cycle_ordinal": ordinal,
            "usage": payload.get("usage"),
            "purpose": payload.get("purpose", "unknown"),
            "source_compatibility_sha256": binding["compatibility_sha256"],
            "observed_at_seconds": payload["observed_at_seconds"],
            "runtime_emitted_at_seconds": payload.get("runtime_emitted_at_seconds"),
        }
        completion = normalize_model_completion(candidate)
        accounting = {
            "usage": completion["usage"],
            "purpose": completion["purpose"],
            "source_compatibility_sha256": completion["source_compatibility_sha256"],
        }
        accounting_sha = hashlib.sha256(_json(accounting).encode()).hexdigest()
        try:
            self._db.execute(
                "INSERT INTO completions VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    completion["project_id"],
                    dispatch_id,
                    completion["binding_id"],
                    completion["thread_fingerprint"],
                    completion["turn_fingerprint"],
                    completion["response_fingerprint"],
                    accounting_sha,
                    ordinal,
                    _json(completion),
                ),
            )
        except sqlite3.IntegrityError as exc:
            existing = self._db.execute(
                "SELECT dispatch_id, accounting_sha256 FROM completions WHERE project_id=? AND thread_fingerprint=? AND turn_fingerprint=? AND response_fingerprint=?",
                (
                    completion["project_id"],
                    completion["thread_fingerprint"],
                    completion["turn_fingerprint"],
                    completion["response_fingerprint"],
                ),
            ).fetchone()
            if existing is not None and tuple(existing) == (
                dispatch_id,
                accounting_sha,
            ):
                return
            raise ObservabilityLedgerError("observability-completion-conflict") from exc

    def _increment_activity(
        self, dispatch_id: str, event_kind: str, payload: Mapping[str, Any]
    ) -> None:
        row = self._db.execute(
            "SELECT * FROM activity WHERE dispatch_id=?", (dispatch_id,)
        ).fetchone()
        if row is None or not row["available"]:
            raise ObservabilityLedgerError("observability-activity-unavailable")
        if event_kind == "retry":
            self._db.execute(
                "UPDATE activity SET retry_notices=retry_notices+1 WHERE dispatch_id=?",
                (dispatch_id,),
            )
        elif event_kind == "turn_outcome":
            outcome = TurnOutcome(payload["outcome"]).value
            self._db.execute(
                f"UPDATE activity SET {outcome}={outcome}+1 WHERE dispatch_id=?",
                (dispatch_id,),
            )
        else:
            category = ToolCategory(payload["category"]).value
            lifecycle = ToolLifecycle(payload["lifecycle"]).value
            counts = _load(row["tool_counts_json"])
            key = f"{category}:{lifecycle}"
            counts[key] = int(counts.get(key, 0)) + 1
            self._db.execute(
                "UPDATE activity SET tool_counts_json=? WHERE dispatch_id=?",
                (_json(counts), dispatch_id),
            )

    def _record_conflict(
        self,
        project_id: str,
        dispatch_id: str | None,
        event_kind: str,
        fingerprint: str,
    ) -> None:
        self._db.execute(
            "INSERT INTO conflicts(project_id,dispatch_id,event_kind,event_fingerprint,observed_at) VALUES(?,?,?,?,?)",
            (project_id, dispatch_id, event_kind, fingerprint, _now()),
        )
        registration_row = self._db.execute(
            "SELECT record_json FROM registrations WHERE project_id=?", (project_id,)
        ).fetchone()
        registration = _load(registration_row[0])
        maximum = registration["resource_limits"]["max_pending_records"]
        self._db.execute(
            "DELETE FROM conflicts WHERE project_id=? AND id NOT IN (SELECT id FROM conflicts WHERE project_id=? ORDER BY id DESC LIMIT ?)",
            (project_id, project_id, maximum),
        )
        if dispatch_id is not None:
            self._set_dispatch_coverage(dispatch_id, CoverageState.ACCOUNTING_CONFLICT)
            self._bump_dispatch(dispatch_id)

    def _expire_pending(self, project_id: str) -> int:
        registration_row = self._db.execute(
            "SELECT record_json FROM registrations WHERE project_id=?",
            (project_id,),
        ).fetchone()
        if registration_row is None:
            raise ObservabilityLedgerError("observability-project-unregistered")
        registration = _load(registration_row[0])
        if registration["state"] != RegistrationState.ENABLED.value:
            return 0
        cutoff = (
            _now() - registration["resource_limits"]["pending_binding_deadline_seconds"]
        )
        count = int(
            self._db.execute(
                "SELECT COUNT(*) FROM pending_events WHERE project_id=? AND received_at<?",
                (project_id, cutoff),
            ).fetchone()[0]
        )
        if not count:
            return 0
        self._db.execute(
            "DELETE FROM pending_events WHERE project_id=? AND received_at<?",
            (project_id, cutoff),
        )
        for _ in range(count):
            self._health_event(
                project_id,
                TelemetryDisposition.LOST,
                reason=HealthReason.PENDING_EXPIRED,
            )
        self._add_gap(project_id, CoverageGap.UNASSIGNED_BINDING)
        self._mark_project_nonterminal_gap(project_id)
        self._bump_global()
        return count

    def expire_pending(self, project_id: str | None = None) -> int:
        """Expire unmatched projections even when the source has gone idle."""

        with self._transaction():
            if project_id is None:
                projects = [
                    row[0]
                    for row in self._db.execute("SELECT project_id FROM registrations")
                ]
            else:
                projects = [project_id]
            return sum(self._expire_pending(item) for item in projects)

    def _set_dispatch_coverage(self, dispatch_id: str, state: CoverageState) -> None:
        row = self._db.execute(
            "SELECT record_json FROM dispatches WHERE dispatch_id=?", (dispatch_id,)
        ).fetchone()
        if row is None:
            return
        record = _load(row[0])
        record["coverage_state"] = max(int(record["coverage_state"]), int(state))
        self._db.execute(
            "UPDATE dispatches SET record_json=? WHERE dispatch_id=?",
            (_json(record), dispatch_id),
        )

    def _mark_project_nonterminal_gap(self, project_id: str) -> list[str]:
        affected: list[str] = []
        rows = self._db.execute(
            "SELECT dispatch_id,record_json FROM dispatches WHERE project_id=?",
            (project_id,),
        ).fetchall()
        for row in rows:
            record = _load(row["record_json"])
            if LifecycleState(record["lifecycle_state"]) in TERMINAL_LIFECYCLE_STATES:
                continue
            self._set_dispatch_coverage(row["dispatch_id"], CoverageState.KNOWN_GAP)
            self._bump_dispatch(row["dispatch_id"])
            affected.append(str(row["dispatch_id"]))
        return affected

    def _mark_project_attributable_gap(self, project_id: str) -> list[str]:
        """Gap active work and terminal work still attributable to a source."""

        affected: list[str] = []
        rows = self._db.execute(
            """SELECT d.dispatch_id,d.record_json,
                      EXISTS(
                          SELECT 1 FROM bindings b
                          WHERE b.dispatch_id=d.dispatch_id AND b.state=?
                      ) AS has_active_binding
               FROM dispatches d WHERE d.project_id=?""",
            (BindingState.ACTIVE.value, project_id),
        ).fetchall()
        for row in rows:
            record = _load(row["record_json"])
            terminal = (
                LifecycleState(record["lifecycle_state"]) in TERMINAL_LIFECYCLE_STATES
            )
            if terminal and not bool(row["has_active_binding"]):
                continue
            self._set_dispatch_coverage(row["dispatch_id"], CoverageState.KNOWN_GAP)
            self._bump_dispatch(row["dispatch_id"])
            affected.append(str(row["dispatch_id"]))
        return affected

    def _add_gap(self, project_id: str, gap: CoverageGap | str) -> None:
        self._ensure_health(project_id)
        row = self._db.execute(
            "SELECT known_gaps_json FROM health WHERE project_id=?", (project_id,)
        ).fetchone()
        gaps = set(_load(row[0]))
        gaps.add(CoverageGap(gap).value)
        self._db.execute(
            "UPDATE health SET known_gaps_json=? WHERE project_id=?",
            (_json(sorted(gaps)), project_id),
        )

    def mark_gap(
        self,
        project_id: str,
        gap: CoverageGap | str,
        *,
        dispatch_ids: Sequence[str] | None = None,
    ) -> None:
        with self._transaction():
            self._registration(project_id)
            self._add_gap(project_id, gap)
            if dispatch_ids is None:
                targets = self._mark_project_nonterminal_gap(project_id)
                if not targets:
                    self._bump_global()
                return
            targets = list(dispatch_ids)
            for dispatch_id in targets:
                row = self._db.execute(
                    "SELECT project_id FROM dispatches WHERE dispatch_id=?",
                    (dispatch_id,),
                ).fetchone()
                if row is None or row[0] != project_id:
                    raise ObservabilityLedgerError(
                        "observability-gap-dispatch-mismatch"
                    )
                self._set_dispatch_coverage(dispatch_id, CoverageState.KNOWN_GAP)
                self._bump_dispatch(dispatch_id)
            if not targets:
                self._bump_global()

    def mark_source_disconnected(
        self,
        project_id: str,
        generation: int,
        connection_id: str,
        *,
        disconnected_at_seconds: float | None = None,
        expected: bool = False,
    ) -> None:
        if not isinstance(expected, bool):
            raise ObservabilityLedgerError("observability-disconnect-expected-invalid")
        with self._transaction():
            row = self._db.execute(
                "SELECT state FROM connections WHERE project_id=? AND generation=? AND connection_id=?",
                (project_id, generation, connection_id),
            ).fetchone()
            if row is None:
                raise ObservabilityLedgerError("observability-connection-unknown")
            if row[0] == ConnectionState.DISCONNECTED.value:
                return
            self._db.execute(
                "UPDATE connections SET state=?, disconnected_at=? WHERE project_id=? AND generation=? AND connection_id=?",
                (
                    ConnectionState.DISCONNECTED.value,
                    disconnected_at_seconds or _now(),
                    project_id,
                    generation,
                    connection_id,
                ),
            )
            binding_rows = self._db.execute(
                """SELECT b.binding_id,b.dispatch_id,b.record_json,d.record_json AS dispatch_json
                   FROM bindings b JOIN dispatches d ON d.dispatch_id=b.dispatch_id
                   WHERE b.project_id=? AND b.generation=? AND b.connection_id=?
                     AND b.state=?""",
                (
                    project_id,
                    generation,
                    connection_id,
                    BindingState.ACTIVE.value,
                ),
            ).fetchall()
            for binding_row in binding_rows:
                binding = _load(binding_row["record_json"])
                binding["state"] = BindingState.CLOSED.value
                self._db.execute(
                    "UPDATE bindings SET state=?,record_json=? WHERE binding_id=?",
                    (
                        BindingState.CLOSED.value,
                        _json(binding),
                        binding_row["binding_id"],
                    ),
                )
            dispatches = {
                row["dispatch_id"]
                for row in binding_rows
                if (
                    not expected
                    or LifecycleState(_load(row["dispatch_json"])["lifecycle_state"])
                    not in TERMINAL_LIFECYCLE_STATES
                )
            }
            if not expected:
                dispatches.update(
                    row["dispatch_id"]
                    for row in self._db.execute(
                        "SELECT dispatch_id,record_json FROM dispatches WHERE project_id=?",
                        (project_id,),
                    ).fetchall()
                    if LifecycleState(_load(row["record_json"])["lifecycle_state"])
                    not in TERMINAL_LIFECYCLE_STATES
                )
            if dispatches:
                self._health_event(
                    project_id,
                    TelemetryDisposition.LOST,
                    reason=HealthReason.SOURCE_DISCONNECTED,
                )
                self._add_gap(project_id, CoverageGap.SOURCE_DISCONNECT)
            for dispatch_id in dispatches:
                self._set_dispatch_coverage(dispatch_id, CoverageState.KNOWN_GAP)
                self._bump_dispatch(dispatch_id)
            if not dispatches:
                self._bump_global()

    def recover_incomplete_connections(self, project_id: str | None = None) -> int:
        with self._transaction():
            query = "SELECT project_id,generation,connection_id FROM connections WHERE state=?"
            args: list[Any] = [ConnectionState.CONNECTED.value]
            if project_id is not None:
                query += " AND project_id=?"
                args.append(project_id)
            rows = self._db.execute(query, args).fetchall()
            for row in rows:
                self._db.execute(
                    "UPDATE connections SET state=?,disconnected_at=? WHERE project_id=? AND generation=? AND connection_id=?",
                    (
                        ConnectionState.DISCONNECTED.value,
                        _now(),
                        row[0],
                        row[1],
                        row[2],
                    ),
                )
                self._health_event(
                    row[0],
                    TelemetryDisposition.LOST,
                    reason=HealthReason.SOURCE_DISCONNECTED,
                )
                self._add_gap(row[0], CoverageGap.CRASH_BEFORE_COMMIT)
                bindings = self._db.execute(
                    """SELECT binding_id,dispatch_id,record_json FROM bindings
                       WHERE project_id=? AND generation=? AND connection_id=?
                         AND state=?""",
                    (*tuple(row), BindingState.ACTIVE.value),
                ).fetchall()
                affected = {binding_row["dispatch_id"] for binding_row in bindings}
                for binding_row in bindings:
                    binding = _load(binding_row["record_json"])
                    binding["state"] = BindingState.CLOSED.value
                    self._db.execute(
                        "UPDATE bindings SET state=?,record_json=? WHERE binding_id=?",
                        (
                            BindingState.CLOSED.value,
                            _json(binding),
                            binding_row["binding_id"],
                        ),
                    )
                affected.update(
                    dispatch_row["dispatch_id"]
                    for dispatch_row in self._db.execute(
                        "SELECT dispatch_id,record_json FROM dispatches WHERE project_id=?",
                        (row[0],),
                    ).fetchall()
                    if LifecycleState(
                        _load(dispatch_row["record_json"])["lifecycle_state"]
                    )
                    not in TERMINAL_LIFECYCLE_STATES
                )
                for dispatch_id in affected:
                    self._set_dispatch_coverage(dispatch_id, CoverageState.KNOWN_GAP)
                    self._bump_dispatch(dispatch_id)
            if rows:
                self._bump_global()
            return len(rows)

    def set_queue_depth(
        self, project_id: str, depth: int, *, overflowed: bool = False
    ) -> None:
        if isinstance(depth, bool) or not isinstance(depth, int) or depth < 0:
            raise ObservabilityLedgerError("observability-queue-depth-invalid")
        with self._transaction():
            self._ensure_health(project_id)
            prior = self._db.execute(
                "SELECT queue_depth,queue_state FROM health WHERE project_id=?",
                (project_id,),
            ).fetchone()
            state = (
                QueueState.OVERLOADED.value if overflowed else QueueState.HEALTHY.value
            )
            if tuple(prior) == (depth, state):
                return
            self._db.execute(
                "UPDATE health SET queue_depth=?,queue_state=? WHERE project_id=?",
                (depth, state, project_id),
            )
            if overflowed:
                self._add_gap(project_id, CoverageGap.QUEUE_OVERFLOW)
            self._bump_global()

    def set_publication_state(
        self,
        project_id: str,
        state: PublicationState | int,
    ) -> None:
        try:
            normalized = PublicationState(state)
        except (TypeError, ValueError) as exc:
            raise ObservabilityLedgerError(
                "observability-publication-state-invalid"
            ) from exc
        with self._transaction():
            self._registration(project_id)
            self._ensure_health(project_id)
            prior = self._db.execute(
                "SELECT publication_state FROM health WHERE project_id=?",
                (project_id,),
            ).fetchone()[0]
            if int(prior) == int(normalized):
                return
            self._db.execute(
                "UPDATE health SET publication_state=? WHERE project_id=?",
                (int(normalized), project_id),
            )
            self._bump_global()

    def record_health_event(
        self,
        project_id: str,
        disposition: TelemetryDisposition | str,
        *,
        reason: HealthReason | str | None = None,
        gap: CoverageGap | str | None = None,
        count: int = 1,
    ) -> None:
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ObservabilityLedgerError("observability-health-count-invalid")
        with self._transaction():
            self._registration(project_id)
            for _ in range(count):
                self._health_event(project_id, disposition, reason=reason)
            if gap is not None:
                self._add_gap(project_id, gap)
                self._mark_project_attributable_gap(project_id)
            self._bump_global()

    def _normalized_samples(
        self, samples: Sequence[Mapping[str, Any]], timestamp: float
    ) -> list[dict[str, Any]]:
        if not isinstance(samples, Sequence) or isinstance(samples, (str, bytes)):
            raise ObservabilityLedgerError("observability-publication-samples-invalid")
        if len(samples) > 4096:
            raise ObservabilityLedgerError("observability-publication-samples-capacity")
        result = []
        identities: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
        for sample in samples:
            if set(sample) != {"name", "labels", "value", "timestamp_seconds"}:
                raise ObservabilityLedgerError("observability-publication-sample-shape")
            name = sample["name"]
            labels = sample["labels"]
            if name not in METRIC_FAMILIES or not isinstance(labels, Mapping):
                raise ObservabilityLedgerError(
                    "observability-publication-sample-invalid"
                )
            if set(labels) != set(METRIC_FAMILIES[name].labels):
                raise ObservabilityLedgerError(
                    "observability-publication-label-set-invalid"
                )
            normalized_labels = {}
            for key, value in labels.items():
                if (
                    not isinstance(key, str)
                    or not isinstance(value, str)
                    or len(key) > 128
                    or len(value) > 256
                    or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:+/-]*", value) is None
                ):
                    raise ObservabilityLedgerError(
                        "observability-publication-label-invalid"
                    )
                normalized_labels[key] = value
            if float(sample["timestamp_seconds"]) != timestamp:
                raise ObservabilityLedgerError(
                    "observability-publication-timestamp-mismatch"
                )
            value = sample["value"]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or value < 0
                or not isfinite(value)
            ):
                raise ObservabilityLedgerError(
                    "observability-publication-value-invalid"
                )
            identity = (name, tuple(sorted(normalized_labels.items())))
            if identity in identities:
                raise ObservabilityLedgerError(
                    "observability-publication-sample-duplicate"
                )
            identities.add(identity)
            result.append(
                {
                    "name": name,
                    "labels": dict(sorted(normalized_labels.items())),
                    "value": value,
                    "timestamp_seconds": timestamp,
                }
            )
        result.sort(key=lambda item: (item["name"], tuple(item["labels"].items())))
        if len(_json(result).encode()) > MAX_FRAME_BYTES:
            raise ObservabilityLedgerError(
                "observability-publication-manifest-too-large"
            )
        return result

    def stage_publication_manifest(
        self,
        dispatch_id: str,
        *,
        revision: int,
        sample_timestamp_seconds: float,
        samples: Sequence[Mapping[str, Any]],
    ) -> str:
        if (
            isinstance(sample_timestamp_seconds, bool)
            or not isinstance(sample_timestamp_seconds, (int, float))
            or sample_timestamp_seconds < 0
            or not isfinite(sample_timestamp_seconds)
        ):
            raise ObservabilityLedgerError(
                "observability-publication-timestamp-invalid"
            )
        timestamp = float(sample_timestamp_seconds)
        normalized = self._normalized_samples(samples, timestamp)
        manifest_sha = hashlib.sha256(_json(normalized).encode()).hexdigest()
        with self._transaction():
            row = self._db.execute(
                "SELECT revision,record_json FROM dispatches WHERE dispatch_id=?",
                (dispatch_id,),
            ).fetchone()
            if row is None or int(row[0]) != revision:
                raise ObservabilityLedgerError(
                    "observability-publication-revision-stale"
                )
            if (
                LifecycleState(_load(row[1])["lifecycle_state"])
                not in TERMINAL_LIFECYCLE_STATES
            ):
                raise ObservabilityLedgerError(
                    "observability-publication-dispatch-not-terminal"
                )
            existing = self._db.execute(
                "SELECT manifest_sha256,sample_timestamp,samples_json FROM publication_manifests WHERE dispatch_id=? AND revision=?",
                (dispatch_id, revision),
            ).fetchone()
            expected = (manifest_sha, timestamp, _json(normalized))
            if existing is not None:
                if tuple(existing) == expected:
                    return manifest_sha
                raise ObservabilityLedgerError(
                    "observability-publication-manifest-conflict"
                )
            self._db.execute(
                "INSERT INTO publication_manifests VALUES(?,?,?,?,?,?,NULL)",
                (dispatch_id, revision, manifest_sha, timestamp, expected[2], _now()),
            )
            self._bump_global()
        return manifest_sha

    def confirm_publication(
        self,
        dispatch_id: str,
        *,
        revision: int,
        manifest_sha256: str,
        sample_timestamp_seconds: float,
        confirmed_at_seconds: float,
    ) -> None:
        manifest = _require_sha256(manifest_sha256, "publication-manifest")
        for value in (sample_timestamp_seconds, confirmed_at_seconds):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or value < 0
                or not isfinite(value)
            ):
                raise ObservabilityLedgerError(
                    "observability-publication-timestamp-invalid"
                )
        with self._transaction():
            row = self._db.execute(
                "SELECT revision FROM dispatches WHERE dispatch_id=?", (dispatch_id,)
            ).fetchone()
            if row is None or int(row[0]) != revision:
                raise ObservabilityLedgerError(
                    "observability-publication-revision-stale"
                )
            staged = self._db.execute(
                "SELECT manifest_sha256,sample_timestamp,confirmed_at FROM publication_manifests WHERE dispatch_id=? AND revision=?",
                (dispatch_id, revision),
            ).fetchone()
            if (
                staged is None
                or staged[0] != manifest
                or float(staged[1]) != float(sample_timestamp_seconds)
            ):
                raise ObservabilityLedgerError(
                    "observability-publication-confirmation-mismatch"
                )
            if staged[2] is None:
                self._db.execute(
                    "UPDATE publication_manifests SET confirmed_at=? WHERE dispatch_id=? AND revision=?",
                    (float(confirmed_at_seconds), dispatch_id, revision),
                )
                self._bump_global()

    def pending_publication_manifests(
        self, project_id: str | None = None
    ) -> list[dict[str, Any]]:
        query = """SELECT p.*,d.project_id FROM publication_manifests p JOIN dispatches d ON d.dispatch_id=p.dispatch_id WHERE p.confirmed_at IS NULL AND p.revision=d.revision"""
        args: list[Any] = []
        if project_id is not None:
            query += " AND d.project_id=?"
            args.append(project_id)
        query += " ORDER BY p.staged_at"
        with self._lock:
            rows = self._db.execute(query, args).fetchall()
            return [
                {
                    "project_id": row["project_id"],
                    "dispatch_id": row["dispatch_id"],
                    "revision": row["revision"],
                    "manifest_sha256": row["manifest_sha256"],
                    "sample_timestamp_seconds": row["sample_timestamp"],
                    "samples": _load(row["samples_json"]),
                }
                for row in rows
            ]

    def pending_terminal_snapshots(
        self, project_id: str | None = None
    ) -> list[dict[str, Any]]:
        snap = self.snapshot(project_id=project_id)
        pending = []
        for project in snap["projects"]:
            for dispatch in project["dispatches"]:
                if (
                    LifecycleState(dispatch["observation"]["lifecycle_state"])
                    in TERMINAL_LIFECYCLE_STATES
                    and dispatch["publication"]["pending"]
                ):
                    pending.append(dispatch)
        return pending

    def _ledger_bytes(self) -> int:
        return sum(
            path.stat().st_size
            for suffix in ("", "-wal", "-shm")
            if (path := self.state_dir / f"{DATABASE_NAME}{suffix}").exists()
        )

    def _protected_state_bytes(self) -> int:
        paths = [
            self.state_dir / DATABASE_NAME,
            self.state_dir / f"{DATABASE_NAME}-wal",
            self.state_dir / f"{DATABASE_NAME}-shm",
            self.state_dir / LOCK_NAME,
        ]
        key_dir = self.state_dir / KEY_DIRECTORY_NAME
        if key_dir.exists():
            paths.extend(key_dir.iterdir())
        total = 0
        for path in paths:
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISREG(metadata.st_mode):
                total += metadata.st_size
        return total

    @contextmanager
    def _read_transaction(self):
        """Pin one committed SQLite view for a complete multi-table read."""

        with self._lock:
            if self._closed:
                raise ObservabilityLedgerError("observability-ledger-closed")
            if self._db.in_transaction:
                raise ObservabilityLedgerError(
                    "observability-snapshot-active-transaction"
                )
            self._db.execute("BEGIN")
            try:
                yield
            finally:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")

    def snapshot(self, *, project_id: str | None = None) -> dict[str, Any]:
        with self._read_transaction():
            return self._snapshot(project_id=project_id, terminal_projection=False)

    def terminal_projection_snapshot(
        self, *, project_id: str | None = None
    ) -> dict[str, Any]:
        """Return a coherent snapshot with read-only terminal-drain facts."""

        with self._read_transaction():
            return self._snapshot(project_id=project_id, terminal_projection=True)

    def _snapshot(
        self, *, project_id: str | None, terminal_projection: bool
    ) -> dict[str, Any]:
        revision = int(
            self._db.execute(
                "SELECT value FROM metadata WHERE key='snapshot_revision'"
            ).fetchone()[0]
        )
        query = "SELECT project_id,record_json FROM registrations"
        args: tuple[Any, ...] = ()
        if project_id is not None:
            query += " WHERE project_id=?"
            args = (project_id,)
        query += " ORDER BY project_id"
        projects = []
        for registration_row in self._db.execute(query, args).fetchall():
            pid = registration_row["project_id"]
            registration = _load(registration_row["record_json"])
            health = self._snapshot_health(pid)
            event_counts = self._snapshot_event_counts(pid)
            project_scope = (
                self._terminal_project_scope(pid) if terminal_projection else None
            )
            dispatches = [
                self._snapshot_dispatch(
                    row,
                    terminal_projection=terminal_projection,
                )
                for row in self._db.execute(
                    "SELECT * FROM dispatches WHERE project_id=? ORDER BY created_at,dispatch_id",
                    (pid,),
                ).fetchall()
            ]
            projects.append(
                {
                    "registration": registration,
                    "health": health,
                    "event_counts": event_counts,
                    "dispatches": dispatches,
                    **(
                        {"terminal_projection_scope": project_scope}
                        if terminal_projection
                        else {}
                    ),
                }
            )
        return {
            "schema_version": SCHEMA_VERSION,
            "snapshot_revision": revision,
            "projects": projects,
        }

    def _terminal_project_scope(self, project_id: str) -> dict[str, int]:
        dispatch_count = int(
            self._db.execute(
                "SELECT COUNT(*) FROM dispatches WHERE project_id=?", (project_id,)
            ).fetchone()[0]
        )
        connection_count = int(
            self._db.execute(
                "SELECT COUNT(*) FROM connections WHERE project_id=?", (project_id,)
            ).fetchone()[0]
        )
        pending_event_count = int(
            self._db.execute(
                "SELECT COUNT(*) FROM pending_events WHERE project_id=?", (project_id,)
            ).fetchone()[0]
        )
        return {
            "dispatch_count": dispatch_count,
            "connection_count": connection_count,
            "pending_event_count": pending_event_count,
        }

    def _terminal_dispatch_scope(self, dispatch_id: str) -> dict[str, int]:
        binding_rows = self._db.execute(
            "SELECT state FROM bindings WHERE dispatch_id=?", (dispatch_id,)
        ).fetchall()
        connection_rows = self._db.execute(
            """SELECT DISTINCT c.project_id,c.generation,c.connection_id,c.state
               FROM bindings b JOIN connections c
                 ON c.project_id=b.project_id AND c.generation=b.generation
                AND c.connection_id=b.connection_id
               WHERE b.dispatch_id=?""",
            (dispatch_id,),
        ).fetchall()
        pending_source_records = int(
            self._db.execute(
                """SELECT COUNT(*) FROM pending_events p
                   WHERE EXISTS(
                       SELECT 1 FROM bindings b
                       WHERE b.dispatch_id=? AND b.project_id=p.project_id
                         AND b.generation=p.generation
                         AND b.connection_id=p.connection_id
                   )""",
                (dispatch_id,),
            ).fetchone()[0]
        )
        pending_binding_records = int(
            self._db.execute(
                """SELECT COUNT(*) FROM pending_events p
                   WHERE EXISTS(
                       SELECT 1 FROM bindings b
                       WHERE b.dispatch_id=? AND b.project_id=p.project_id
                         AND b.generation=p.generation
                         AND b.connection_id=p.connection_id
                         AND b.thread_fingerprint=p.thread_fingerprint
                         AND b.turn_fingerprint=p.turn_fingerprint
                   )""",
                (dispatch_id,),
            ).fetchone()[0]
        )
        return {
            "binding_count": len(binding_rows),
            "closed_binding_count": sum(
                row["state"] == BindingState.CLOSED.value for row in binding_rows
            ),
            "source_connection_count": len(connection_rows),
            "disconnected_source_connection_count": sum(
                row["state"] == ConnectionState.DISCONNECTED.value
                for row in connection_rows
            ),
            "pending_source_records": pending_source_records,
            "pending_binding_records": pending_binding_records,
        }

    def _snapshot_health(self, project_id: str) -> dict[str, Any]:
        row = self._db.execute(
            "SELECT * FROM health WHERE project_id=?", (project_id,)
        ).fetchone()
        registration_row = self._db.execute(
            "SELECT generation,record_json FROM registrations WHERE project_id=?",
            (project_id,),
        ).fetchone()
        connections = self._db.execute(
            """SELECT state,schema_state FROM connections
               WHERE project_id=? AND generation=?""",
            (project_id, registration_row["generation"]),
        ).fetchall()
        if any(item[0] == ConnectionState.CONNECTED.value for item in connections):
            connection_state = ConnectionState.CONNECTED.value
        elif connections:
            connection_state = ConnectionState.DISCONNECTED.value
        else:
            connection_state = ConnectionState.DISABLED.value
        if any(item[1] == SchemaState.INCOMPATIBLE.value for item in connections):
            schema_state = SchemaState.INCOMPATIBLE.value
        elif any(item[1] == SchemaState.COMPATIBLE.value for item in connections):
            schema_state = SchemaState.COMPATIBLE.value
        else:
            schema_state = SchemaState.UNQUALIFIED.value
        events = _load(row["event_counts_json"])
        registration = _load(registration_row["record_json"])
        ledger_bytes = self._ledger_bytes()
        capacity_blocked = self._capacity_status()
        maximum_bytes = registration["resource_limits"]["max_disk_bytes"]
        if capacity_blocked or ledger_bytes >= maximum_bytes:
            disk_state = DiskState.FULL.value
        elif ledger_bytes >= maximum_bytes * 9 // 10:
            disk_state = DiskState.PRESSURE.value
        else:
            disk_state = DiskState.HEALTHY.value
        reasons = _load(row["reason_counts_json"])
        gaps = _load(row["known_gaps_json"])
        if capacity_blocked:
            reasons[HealthReason.DISK_FULL.value] = max(
                1, int(reasons.get(HealthReason.DISK_FULL.value, 0))
            )
        return normalize_telemetry_health(
            {
                "project_id": project_id,
                "connection_state": connection_state,
                "schema_state": schema_state,
                "ledger_state": "fault" if capacity_blocked else row["ledger_state"],
                "queue_state": row["queue_state"],
                "disk_state": disk_state,
                "publication_state": int(
                    PublicationState.CAPACITY_BLOCKED
                    if capacity_blocked
                    else row["publication_state"]
                ),
                "reason_counts": reasons,
                "unassigned_count": events[TelemetryDisposition.UNASSIGNED.value],
                "conflict_count": events[TelemetryDisposition.CONFLICT.value],
                "rejected_count": events[TelemetryDisposition.REJECTED.value],
                "queue_depth": row["queue_depth"],
                "ledger_bytes": ledger_bytes,
                "last_event_timestamp_seconds": row["last_event"],
                "known_coverage_gaps": sorted(gaps),
            }
        )

    def _capacity_status(self) -> bool:
        row = self._db.execute(
            "SELECT value FROM metadata WHERE key=?", (SQLITE_CAPACITY_MARKER,)
        ).fetchone()
        return row is not None and row[0] == "1"

    def _snapshot_event_counts(self, project_id: str) -> dict[str, int]:
        row = self._db.execute(
            "SELECT event_counts_json FROM health WHERE project_id=?", (project_id,)
        ).fetchone()
        return _load(row[0])

    def _snapshot_dispatch(
        self, row: sqlite3.Row, *, terminal_projection: bool = False
    ) -> dict[str, Any]:
        dispatch_id = row["dispatch_id"]
        observation = _load(row["record_json"])
        binding_rows = self._db.execute(
            "SELECT record_json FROM bindings WHERE dispatch_id=? ORDER BY created_at,binding_id",
            (dispatch_id,),
        ).fetchall()
        bindings = [_load(item[0]) for item in binding_rows]
        cycles = [
            _load(item[0])
            for item in self._db.execute(
                "SELECT record_json FROM completions WHERE dispatch_id=? ORDER BY cycle_ordinal",
                (dispatch_id,),
            ).fetchall()
        ]
        sums = {kind.value: 0 for kind in TokenKind}
        counts = {kind.value: 0 for kind in TokenKind}
        for cycle in cycles:
            if cycle["usage"] is None:
                continue
            for kind in TokenKind:
                entry = cycle["usage"][kind.value]
                if entry["value"] is not None and entry["state"] in (
                    int(FieldState.PRESENT),
                    int(FieldState.RUNTIME_NORMALIZED),
                ):
                    sums[kind.value] += entry["value"]
                    counts[kind.value] += 1
        states = {
            kind.value: int(
                aggregate_token_state(
                    kind,
                    contributing_cycles=counts[kind.value],
                    observed_cycles=len(cycles),
                )
            )
            for kind in TokenKind
        }
        conflicts = int(
            self._db.execute(
                "SELECT COUNT(*) FROM conflicts WHERE dispatch_id=?", (dispatch_id,)
            ).fetchone()[0]
        )
        activity = self._snapshot_activity(dispatch_id)
        publication = self._snapshot_publication(dispatch_id, int(row["revision"]))
        return {
            "observation": observation,
            "snapshot_revision": int(row["revision"]),
            "binding": bindings[-1] if bindings else None,
            "bindings": bindings,
            "aggregate": {
                "completed_cycles": len(cycles),
                "token_sums": sums,
                "token_cycles": counts,
                "token_states": states,
                "conflict_count": conflicts,
                "coverage_state": observation["coverage_state"],
            },
            "activity": activity,
            "cycles": cycles,
            "publication": publication,
            **(
                {"terminal_projection_scope": self._terminal_dispatch_scope(dispatch_id)}
                if terminal_projection
                else {}
            ),
        }

    def _snapshot_activity(self, dispatch_id: str) -> dict[str, Any]:
        row = self._db.execute(
            "SELECT * FROM activity WHERE dispatch_id=?", (dispatch_id,)
        ).fetchone()
        if row is None or not row["available"]:
            return {"retry_notices": None, "turn_outcomes": None, "tool_events": None}
        counts = _load(row["tool_counts_json"])
        tools = []
        for category in ToolCategory:
            for lifecycle in ToolLifecycle:
                tools.append(
                    {
                        "category": category.value,
                        "lifecycle": lifecycle.value,
                        "count": int(
                            counts.get(f"{category.value}:{lifecycle.value}", 0)
                        ),
                    }
                )
        return {
            "retry_notices": int(row["retry_notices"]),
            "turn_outcomes": {item.value: int(row[item.value]) for item in TurnOutcome},
            "tool_events": tools,
        }

    def _snapshot_publication(self, dispatch_id: str, revision: int) -> dict[str, Any]:
        latest = self._db.execute(
            "SELECT MAX(revision) FROM publication_manifests WHERE dispatch_id=? AND confirmed_at IS NOT NULL",
            (dispatch_id,),
        ).fetchone()[0]
        staged = self._db.execute(
            "SELECT manifest_sha256,sample_timestamp FROM publication_manifests WHERE dispatch_id=? AND revision=?",
            (dispatch_id, revision),
        ).fetchone()
        return {
            "current_revision": revision,
            "latest_confirmed_revision": latest,
            "pending": latest != revision,
            "staged_manifest_sha256": staged[0] if staged else None,
            "staged_sample_timestamp_seconds": staged[1] if staged else None,
        }

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._db.close()
                if not self.readonly:
                    fcntl.flock(self._writer_lock_fd, fcntl.LOCK_UN)
                    os.close(self._writer_lock_fd)
                self._closed = True

    def __enter__(self) -> "ObservabilityLedger":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


__all__ = ["ObservabilityLedger", "ObservabilityLedgerError", "SCHEMA_VERSION"]
