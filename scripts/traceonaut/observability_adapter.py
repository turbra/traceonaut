"""Non-blocking app-server notification adapter for dispatch observability.

``try_submit`` is suitable for an owned app-server stdout reader.  It performs
only bounded validation, domain-separated fingerprinting, and ``put_nowait``.
Raw messages and source identifiers never enter the durable queue or ledger.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
import queue
import threading
import time
from typing import Any

from .observability_contract import (
    CompletionPurpose,
    CoverageGap,
    HealthReason,
    MAX_FRAME_BYTES,
    MAX_JSON_DEPTH,
    MAX_NORMALIZED_RECORD_BYTES,
    TelemetryDisposition,
    ToolCategory,
    ToolLifecycle,
    TurnOutcome,
)
from .observability_ledger import ObservabilityLedger, ObservabilityLedgerError


_TOKEN_FIELDS = {
    "inputTokens": "input",
    "cachedInputTokens": "cached_input",
    "cacheWriteInputTokens": "cache_write_input",
    "outputTokens": "output",
    "reasoningOutputTokens": "reasoning_output",
    "totalTokens": "total",
}
_REQUIRED_USAGE = frozenset(_TOKEN_FIELDS).difference({"cacheWriteInputTokens"})
_TOOL_TYPES = {
    "commandExecution": ToolCategory.COMMAND.value,
    "fileChange": ToolCategory.FILE_CHANGE.value,
    "dynamicToolCall": ToolCategory.DYNAMIC.value,
    "mcpToolCall": ToolCategory.MCP.value,
    "webSearch": ToolCategory.WEB.value,
}
_OUTCOMES = frozenset(item.value for item in TurnOutcome)
_SUPPORTED_METHODS = frozenset(
    {"rawResponse/completed", "turn/completed", "item/started", "item/completed"}
)


class _ProjectionError(ValueError):
    def __init__(self, reason: HealthReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


class AppServerCompletionAdapter:
    """Project-bound, fail-open side channel for an owned app-server stream."""

    def __init__(
        self,
        ledger: ObservabilityLedger,
        *,
        project_id: str,
        registration_generation: int,
        connection_id: str,
        source_compatibility_sha256: str,
        auto_start: bool = True,
    ) -> None:
        if ledger.readonly:
            raise ObservabilityLedgerError("observability-adapter-readonly-ledger")
        self.ledger = ledger
        self.project_id = project_id
        self.registration_generation = registration_generation
        self.connection_id = connection_id
        self.source_compatibility_sha256 = source_compatibility_sha256
        limits = ledger.project_resource_limits(project_id)
        self._max_frame_bytes = limits["max_frame_bytes"]
        self._max_depth = limits["max_json_depth"]
        self._max_record_bytes = limits["max_normalized_record_bytes"]
        self._queue: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue(
            maxsize=limits["max_queue_records"]
        )
        self._fingerprint = ledger.source_fingerprinter(project_id)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._accepting = True
        self._pending_disconnect: bool | None = None
        self._counter_lock = threading.Lock()
        self._lost = 0
        self._commit_lost = 0
        self._rejected = 0
        self._malformed: dict[str, int] = {}
        self._last_overflow = False
        self._closed = False
        ledger.register_connection(
            project_id,
            registration_generation,
            connection_id,
            source_compatibility_sha256,
        )
        if auto_start:
            self.start()

    def start(self) -> None:
        if self._closed:
            raise ObservabilityLedgerError("observability-adapter-closed")
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="cwo-observability-adapter",
            daemon=True,
        )
        self._thread.start()

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    def fingerprint(self, domain: str, source_id: str) -> str:
        """Fingerprint one source identifier using the cached project key."""

        return self._fingerprint(domain, source_id)

    def try_submit(
        self,
        message: Any,
        *,
        received_unix_ns: int | None = None,
        received_monotonic_ns: int | None = None,
        source_sequence: int | None = None,
    ) -> str:
        """Project an allowlisted notification without blocking its reader."""

        with self._state_lock:
            if self._closed or not self._accepting:
                return self._reject()
        try:
            if not isinstance(message, Mapping):
                return self._reject()
            method = message.get("method")
            params = message.get("params")
            supported = method in _SUPPORTED_METHODS or (
                method == "error"
                and isinstance(params, Mapping)
                and params.get("willRetry") is True
            )
            if not supported:
                return "ignored"
            if self._json_size(message) > self._max_frame_bytes:
                return self._reject(HealthReason.SCHEMA_INCOMPATIBLE)
            if self._depth(message) > self._max_depth:
                return self._reject(HealthReason.SCHEMA_INCOMPATIBLE)
            observed_ns = (
                time.time_ns() if received_unix_ns is None else received_unix_ns
            )
            if (
                isinstance(observed_ns, bool)
                or not isinstance(observed_ns, int)
                or observed_ns < 0
            ):
                return self._reject()
            projected = self._project(
                message,
                observed_at_seconds=observed_ns / 1_000_000_000,
                received_monotonic_ns=received_monotonic_ns,
                source_sequence=source_sequence,
            )
            if projected is None:
                return "ignored"
            event_kind, payload = projected
            if self._json_size(payload) > self._max_record_bytes:
                return self._reject()
            with self._state_lock:
                if self._closed or not self._accepting:
                    return self._reject()
                try:
                    self._queue.put_nowait((event_kind, payload))
                except queue.Full:
                    with self._counter_lock:
                        self._lost += 1
                        self._last_overflow = True
                    return TelemetryDisposition.LOST.value
            return "queued"
        except _ProjectionError as exc:
            return self._reject(exc.reason)
        except Exception:
            return self._reject(HealthReason.SCHEMA_INCOMPATIBLE)

    def _reject(self, reason: HealthReason | None = None) -> str:
        with self._counter_lock:
            if reason is None:
                self._rejected += 1
            else:
                self._malformed[reason.value] = self._malformed.get(reason.value, 0) + 1
        return TelemetryDisposition.REJECTED.value

    def _json_size(self, value: Any) -> int:
        return len(
            json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )

    def _depth(self, value: Any) -> int:
        maximum = 0
        stack = [(value, 1)]
        visited = 0
        while stack:
            current, depth = stack.pop()
            visited += 1
            if visited > 100_000:
                return self._max_depth + 1
            maximum = max(maximum, depth)
            if isinstance(current, Mapping):
                stack.extend((item, depth + 1) for item in current.values())
            elif isinstance(current, (list, tuple)):
                stack.extend((item, depth + 1) for item in current)
        return maximum

    @staticmethod
    def _source_id(value: Any) -> str | None:
        if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 4096:
            return None
        return value

    def _event_source(self, kind: str, *parts: str | int) -> str:
        return json.dumps(
            [kind, self.connection_id, *parts],
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def _base(
        self,
        *,
        thread_id: str,
        turn_id: str,
        event_source: str,
        observed_at_seconds: float,
        received_monotonic_ns: int | None,
    ) -> dict[str, Any]:
        result = {
            "project_id": self.project_id,
            "registration_generation": self.registration_generation,
            "connection_id": self.connection_id,
            "thread_fingerprint": self._fingerprint("thread", thread_id),
            "turn_fingerprint": self._fingerprint("turn", turn_id),
            "event_fingerprint": self._fingerprint("event", event_source),
            "observed_at_seconds": observed_at_seconds,
        }
        if (
            not isinstance(received_monotonic_ns, bool)
            and isinstance(received_monotonic_ns, int)
            and received_monotonic_ns >= 0
        ):
            result["received_monotonic_ns"] = received_monotonic_ns
        return result

    def _project(
        self,
        message: Mapping[str, Any],
        *,
        observed_at_seconds: float,
        received_monotonic_ns: int | None,
        source_sequence: int | None,
    ) -> tuple[str, dict[str, Any]] | None:
        method = message.get("method")
        params = message.get("params")
        if not isinstance(params, Mapping):
            raise _ProjectionError(HealthReason.SCHEMA_INCOMPATIBLE)
        emitted_at = message.get("emittedAtMs")
        emitted_seconds = (
            emitted_at / 1000
            if not isinstance(emitted_at, bool)
            and isinstance(emitted_at, (int, float))
            and emitted_at >= 0
            else None
        )
        if method == "rawResponse/completed":
            thread = self._source_id(params.get("threadId"))
            turn = self._source_id(params.get("turnId"))
            response = self._source_id(params.get("responseId"))
            if thread is None or turn is None or response is None:
                raise _ProjectionError(HealthReason.INVALID_IDENTITY)
            base = self._base(
                thread_id=thread,
                turn_id=turn,
                event_source=self._event_source("completion", thread, turn, response),
                observed_at_seconds=observed_at_seconds,
                received_monotonic_ns=received_monotonic_ns,
            )
            response_fingerprint = self._fingerprint("response", response)
            base.update(
                {
                    "event_fingerprint": response_fingerprint,
                    "response_fingerprint": response_fingerprint,
                    "usage": self._usage(params.get("usage")),
                    "purpose": self._purpose(params),
                    "runtime_emitted_at_seconds": emitted_seconds,
                }
            )
            return "completion", base
        if method == "turn/completed":
            turn_value = params.get("turn")
            turn_record = turn_value if isinstance(turn_value, Mapping) else {}
            thread = self._source_id(params.get("threadId"))
            turn = self._source_id(turn_record.get("id") or params.get("turnId"))
            outcome = turn_record.get("status")
            if thread is None or turn is None or outcome not in _OUTCOMES:
                raise _ProjectionError(HealthReason.INVALID_IDENTITY)
            base = self._base(
                thread_id=thread,
                turn_id=turn,
                event_source=self._event_source("outcome", thread, turn, outcome),
                observed_at_seconds=observed_at_seconds,
                received_monotonic_ns=received_monotonic_ns,
            )
            base["outcome"] = outcome
            return "turn_outcome", base
        if method == "error" and params.get("willRetry") is True:
            thread = self._source_id(params.get("threadId"))
            turn = self._source_id(params.get("turnId"))
            if (
                thread is None
                or turn is None
                or isinstance(source_sequence, bool)
                or not isinstance(source_sequence, int)
                or source_sequence < 0
            ):
                raise _ProjectionError(HealthReason.INVALID_IDENTITY)
            base = self._base(
                thread_id=thread,
                turn_id=turn,
                event_source=self._event_source("retry", thread, turn, source_sequence),
                observed_at_seconds=observed_at_seconds,
                received_monotonic_ns=received_monotonic_ns,
            )
            return "retry", base
        if method in {"item/started", "item/completed"}:
            thread = self._source_id(params.get("threadId"))
            turn = self._source_id(params.get("turnId"))
            item_value = params.get("item")
            item = item_value if isinstance(item_value, Mapping) else {}
            item_id = self._source_id(item.get("id"))
            if thread is None or turn is None or item_id is None:
                raise _ProjectionError(HealthReason.INVALID_IDENTITY)
            category = _TOOL_TYPES.get(item.get("type"))
            if category is None:
                # Message/reasoning items are not tool activity.  Unknown item
                # kinds remain bounded as "other" without retaining the kind.
                if item.get("type") in {
                    "agentMessage",
                    "userMessage",
                    "reasoning",
                    "message",
                    "contextCompaction",
                    "compaction",
                }:
                    return None
                category = ToolCategory.OTHER.value
            lifecycle = ToolLifecycle.STARTED.value
            if method == "item/completed":
                lifecycle = (
                    ToolLifecycle.FAILED.value
                    if item.get("status") in {"failed", "declined"}
                    else ToolLifecycle.COMPLETED.value
                )
            base = self._base(
                thread_id=thread,
                turn_id=turn,
                event_source=self._event_source("tool", thread, turn, item_id, method),
                observed_at_seconds=observed_at_seconds,
                received_monotonic_ns=received_monotonic_ns,
            )
            base.update({"category": category, "lifecycle": lifecycle})
            return "tool_event", base
        return None

    @staticmethod
    def _usage(value: Any) -> Mapping[str, Any] | None:
        if value is None:
            return None
        if not isinstance(value, Mapping):
            return {field: "invalid" for field in _TOKEN_FIELDS.values()}
        result = {}
        for upstream, normalized in _TOKEN_FIELDS.items():
            item = value.get(upstream)
            if upstream in _REQUIRED_USAGE and upstream not in value:
                item = "invalid"
            result[normalized] = item
        return result

    @staticmethod
    def _purpose(params: Mapping[str, Any]) -> str:
        purpose = params.get("purpose")
        try:
            return CompletionPurpose(purpose).value
        except (TypeError, ValueError):
            return CompletionPurpose.UNKNOWN.value

    def drain_once(self, *, max_records: int = 128) -> int:
        if (
            isinstance(max_records, bool)
            or not isinstance(max_records, int)
            or max_records < 1
        ):
            raise ValueError("max_records must be positive")
        drained = 0
        while drained < max_records:
            try:
                event_kind, payload = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                if event_kind == "__disconnect__":
                    with self._state_lock:
                        self._pending_disconnect = bool(payload["expected"])
                else:
                    self.ledger.ingest_projection(event_kind, payload)
            except Exception:
                with self._counter_lock:
                    self._commit_lost += 1
                if event_kind == "__disconnect__":
                    with self._state_lock:
                        self._pending_disconnect = bool(payload["expected"])
            finally:
                self._queue.task_done()
            drained += 1
        gap_was_pending = self._gap_pending()
        self._flush_health()
        self._attempt_pending_disconnect(
            flush_health=False, force_unexpected=gap_was_pending
        )
        return drained

    def _gap_pending(self) -> bool:
        with self._counter_lock:
            return bool(
                self._lost
                or self._commit_lost
                or self._malformed
                or self._last_overflow
            )

    def _attempt_pending_disconnect(
        self, *, flush_health: bool = True, force_unexpected: bool = False
    ) -> None:
        if not self._queue.empty():
            return
        if flush_health:
            force_unexpected = force_unexpected or self._gap_pending()
            self._flush_health()
        with self._state_lock:
            requested_expected = self._pending_disconnect
        if requested_expected is None:
            return
        expected = (
            requested_expected and not force_unexpected and not self._gap_pending()
        )
        try:
            self.ledger.mark_source_disconnected(
                self.project_id,
                self.registration_generation,
                self.connection_id,
                expected=expected,
            )
        except Exception:
            return
        with self._state_lock:
            if self._pending_disconnect == requested_expected:
                self._pending_disconnect = None

    def _flush_health(self) -> None:
        with self._counter_lock:
            lost = self._lost
            commit_lost = self._commit_lost
            rejected = self._rejected
            malformed = self._malformed
            overflowed = self._last_overflow
            self._lost = 0
            self._commit_lost = 0
            self._rejected = 0
            self._malformed = {}
            self._last_overflow = False
        if lost:
            try:
                self.ledger.record_health_event(
                    self.project_id,
                    TelemetryDisposition.LOST,
                    reason=HealthReason.QUEUE_OVERFLOW,
                    gap=CoverageGap.QUEUE_OVERFLOW,
                    count=lost,
                )
            except Exception:
                with self._counter_lock:
                    self._lost += lost
                    self._last_overflow = True
        if commit_lost:
            try:
                self.ledger.record_health_event(
                    self.project_id,
                    TelemetryDisposition.LOST,
                    reason=HealthReason.COLLECTOR_WRITE_FAILURE,
                    gap=CoverageGap.COLLECTOR_WRITE_FAILURE,
                    count=commit_lost,
                )
            except Exception:
                with self._counter_lock:
                    self._commit_lost += commit_lost
        if rejected:
            try:
                self.ledger.record_health_event(
                    self.project_id,
                    TelemetryDisposition.REJECTED,
                    count=rejected,
                )
            except Exception:
                with self._counter_lock:
                    self._rejected += rejected
        for reason, count in malformed.items():
            try:
                self.ledger.mark_source_incompatible(
                    self.project_id,
                    self.registration_generation,
                    self.connection_id,
                    reason=reason,
                    count=count,
                )
            except Exception:
                with self._counter_lock:
                    self._malformed[reason] = self._malformed.get(reason, 0) + count
        try:
            self.ledger.set_queue_depth(
                self.project_id,
                self._queue.qsize(),
                overflowed=overflowed,
            )
        except Exception:
            # Observability remains a fail-open side channel.  Preserve the
            # overload indication for the next successful health flush.
            with self._counter_lock:
                self._last_overflow = self._last_overflow or overflowed

    def _run(self) -> None:
        next_expiry = 0.0
        while not self._stop.is_set() or not self._queue.empty():
            drained = self.drain_once()
            current = time.monotonic()
            if current >= next_expiry:
                try:
                    self.ledger.expire_pending(self.project_id)
                except Exception:
                    pass
                next_expiry = current + 1.0
            if drained == 0:
                self._stop.wait(0.05)

    def mark_source_disconnected(self, *, expected: bool = False) -> None:
        force_unexpected = self._gap_pending()
        self._flush_health()
        self.ledger.mark_source_disconnected(
            self.project_id,
            self.registration_generation,
            self.connection_id,
            expected=(expected and not force_unexpected and not self._gap_pending()),
        )

    def request_source_disconnected(self, *, expected: bool = False) -> str:
        """Queue disconnect behind every notification already received."""

        if not isinstance(expected, bool):
            return self._reject()
        with self._state_lock:
            if self._closed or not self._accepting:
                return TelemetryDisposition.REJECTED.value
            try:
                self._queue.put_nowait(("__disconnect__", {"expected": expected}))
            except queue.Full:
                self._accepting = False
                # The disconnect ordering marker itself was lost, so terminal
                # coverage can no longer remain exact even for a planned close.
                self._pending_disconnect = False
                with self._counter_lock:
                    self._lost += 1
                    self._last_overflow = True
                return TelemetryDisposition.LOST.value
            self._accepting = False
            return "queued"

    def close(
        self,
        *,
        drain: bool = True,
        mark_disconnected: bool = True,
        expected_disconnect: bool = True,
        timeout_seconds: float = 5.0,
    ) -> None:
        """Stop within ``timeout_seconds``; remaining records become a gap."""

        if self._closed:
            return
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        with self._state_lock:
            self._closed = True
            self._accepting = False
            if mark_disconnected and self._pending_disconnect is None:
                self._pending_disconnect = expected_disconnect
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if drain and (self._thread is None or not self._thread.is_alive()):
            while time.monotonic() < deadline and self.drain_once(max_records=1024):
                continue
        remaining = self._queue.qsize()
        if remaining:
            with self._state_lock:
                if mark_disconnected:
                    self._pending_disconnect = False
            try:
                self.ledger.record_health_event(
                    self.project_id,
                    TelemetryDisposition.LOST,
                    gap=CoverageGap.CRASH_BEFORE_COMMIT,
                    count=remaining,
                )
            except Exception:
                pass
            if self._thread is None or not self._thread.is_alive():
                while True:
                    try:
                        self._queue.get_nowait()
                    except queue.Empty:
                        break
                    else:
                        self._queue.task_done()
                if mark_disconnected:
                    self._attempt_pending_disconnect()
        elif mark_disconnected:
            self._attempt_pending_disconnect()

    def __enter__(self) -> "AppServerCompletionAdapter":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


__all__ = ["AppServerCompletionAdapter"]
