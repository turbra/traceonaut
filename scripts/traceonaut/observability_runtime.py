"""Optional observation of the existing CWO-owned app-server controller.

This module has no submission, interrupt, close-worker, or executor operation.
The controller calls these side-channel hooks after its own authority checks.
Preparation validates metadata in memory. Receipt-backed hooks queue all writes.
"""

from __future__ import annotations

import copy
import queue
import threading
import time
from typing import Any, Mapping
from uuid import uuid4

from .observability_contract import (
    CoverageState,
    FieldState,
    MAX_QUEUE_RECORDS,
    TERMINAL_LIFECYCLE_STATES,
    normalize_bounded_label,
    normalize_dispatch_observation,
)


class OwnedRuntimeObserver:
    def __init__(
        self,
        adapter: Any,
        *,
        clock_epoch_id: str | None = None,
        monotonic: Any = time.monotonic,
        auto_start: bool = True,
    ) -> None:
        self.adapter = adapter
        self.ledger = adapter.ledger
        self.clock_epoch_id = clock_epoch_id or str(uuid4())
        self.monotonic = monotonic
        limits = self.ledger.project_resource_limits(adapter.project_id)
        self._queue: queue.Queue[tuple[str, Any]] = queue.Queue(
            maxsize=min(limits["max_queue_records"], MAX_QUEUE_RECORDS)
        )
        self._stop = threading.Event()
        self._loss_lock = threading.Lock()
        self._losses: dict[str, int] = {}
        self._dispatch_ids: set[str] = set()
        self._thread: threading.Thread | None = None
        if auto_start:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def prepare_dispatch(
        self,
        *,
        agent_id: str,
        packet_sha256: str,
        supervisor_submission_ref: str,
        requested_executor: str,
        requested_model: str | None,
        requested_effort: str | None,
        declared_cycle_allowance: int | None = None,
        declared_elapsed_allowance_seconds: float | None = None,
        enforced_tool_call_limit: int | None = None,
        enforced_runtime_limit_seconds: float | None = None,
    ) -> "DispatchObserver":
        """Prepare metadata without reporting a dispatch before submission.

        Call only for an authorized controller submission, outside its timed
        control turn. Existing controller receipts remain the source of truth.
        """
        dispatch_id = str(uuid4())
        observation = {
            "project_id": self.adapter.project_id,
            "dispatch_id": dispatch_id,
            "agent_id": agent_id,
            "packet_ref": "packet-" + str(uuid4()),
            "packet_sha256": packet_sha256,
            "supervisor_submission_ref": supervisor_submission_ref,
            "requested_executor": requested_executor,
            "requested_model": requested_model,
            "requested_effort": requested_effort,
            "declared_cycle_allowance": declared_cycle_allowance,
            "declared_elapsed_allowance_seconds": declared_elapsed_allowance_seconds,
            "enforced_tool_call_limit": enforced_tool_call_limit,
            "enforced_runtime_limit_seconds": enforced_runtime_limit_seconds,
            "lifecycle_state": "submitted",
            "coverage_state": int(CoverageState.UNKNOWN),
            "timing": {
                "clock_source": "supervisor-monotonic",
                "clock_epoch_id": self.clock_epoch_id,
                "provenance": "supervisor-lifecycle-receipt",
                "submitted_seconds": None,
                "acknowledged_seconds": None,
                "running_seconds": None,
                "terminal_seconds": None,
                "elapsed_seconds": None,
                "elapsed_state": int(FieldState.UNAVAILABLE),
            },
        }
        record = normalize_dispatch_observation(
            observation,
            executor_vocabulary=self.ledger.executor_vocabulary,
            model_vocabulary=self.ledger.model_vocabulary,
            effort_vocabulary=self.ledger.effort_vocabulary,
        )
        return DispatchObserver(self, record)

    def _submit(self, kind: str, payload: Any) -> None:
        if self._stop.is_set():
            self._record_loss("shutdown-with-pending-events")
            return
        try:
            self._queue.put_nowait((kind, payload))
        except queue.Full:
            self._record_loss("queue-overflow")

    def _record_loss(self, cause: str, count: int = 1) -> None:
        with self._loss_lock:
            self._losses[cause] = self._losses.get(cause, 0) + count

    def collector_fault(self) -> None:
        self._record_loss("collector-hook-failure")

    def observe_notification(
        self, message: Mapping[str, Any], *, sequence: int
    ) -> None:
        self.adapter.try_submit(
            message,
            received_unix_ns=time.time_ns(),
            received_monotonic_ns=time.monotonic_ns(),
            source_sequence=sequence,
        )

    def source_disconnected(self, *, expected: bool = False) -> None:
        self._submit("disconnect", expected)

    def drain_once(self, *, max_records: int = 128) -> int:
        count = 0
        while count < max_records:
            try:
                kind, payload = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                if kind == "submission":
                    observation, lifecycle = payload
                    self.ledger.record_submission(observation)
                    with self._loss_lock:
                        self._dispatch_ids.add(observation["dispatch_id"])
                    self.ledger.record_lifecycle(**lifecycle)
                elif kind == "binding":
                    self.ledger.bind_runtime(payload)
                elif kind == "lifecycle":
                    self.ledger.record_lifecycle(**payload)
                elif kind == "disconnect":
                    self.adapter.request_source_disconnected(expected=payload)
            except Exception:
                self._record_loss("collector-write-failure")
            finally:
                self._queue.task_done()
            count += 1
        with self._loss_lock:
            losses, self._losses = self._losses, {}
            dispatch_ids = list(self._dispatch_ids)
        for cause, lost_count in losses.items():
            try:
                self.ledger.mark_gap(
                    self.adapter.project_id, cause, dispatch_ids=dispatch_ids
                )
                self.ledger.record_health_event(
                    self.adapter.project_id, "lost", count=lost_count, reason=cause
                )
            except Exception:
                self._record_loss(cause, lost_count)
        return count

    def _run(self) -> None:
        while not self._stop.is_set() or not self._queue.empty():
            if not self.drain_once():
                self._stop.wait(0.05)
        self.drain_once()

    def close(self, *, timeout_seconds: float = 0.1) -> bool:
        """Stop the telemetry thread without extending the worker close path."""
        if not 0 <= timeout_seconds <= 5:
            raise ValueError("bounded telemetry close timeout required")
        self._stop.set()
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        if self._thread is not None:
            self._thread.join(timeout=timeout_seconds)
        with self._loss_lock:
            return (
                not self._thread.is_alive() and self._queue.empty() and not self._losses
            )


class DispatchObserver:
    def __init__(
        self, owner: OwnedRuntimeObserver, observation: Mapping[str, Any]
    ) -> None:
        self.owner = owner
        self.observation = copy.deepcopy(dict(observation))
        self.dispatch_id = observation["dispatch_id"]
        self._timing = copy.deepcopy(observation["timing"])
        self._terminal = False
        self._submission_queued = False

    def _lifecycle(self, state: str, receipt_sha256: str) -> None:
        if self._terminal:
            return
        if (
            not isinstance(receipt_sha256, str)
            or len(receipt_sha256) != 64
            or any(character not in "0123456789abcdef" for character in receipt_sha256)
        ):
            self.owner.collector_fault()
            raise ValueError("valid controller receipt fingerprint required")
        if state != "submitted" and not self._submission_queued:
            self.owner.collector_fault()
            return
        now = self.owner.monotonic()
        point = {
            "submitted": "submitted_seconds",
            "acknowledged": "acknowledged_seconds",
            "running": "running_seconds",
        }.get(state, "terminal_seconds")
        if self._timing[point] is None:
            self._timing[point] = now
        submitted = self._timing["submitted_seconds"]
        if submitted is not None and now >= submitted:
            self._timing["elapsed_seconds"] = now - submitted
            self._timing["elapsed_state"] = int(FieldState.PRESENT)
        lifecycle = {
            "dispatch_id": self.dispatch_id,
            "lifecycle_state": state,
            "timing": dict(self._timing),
            "receipt_sha256": receipt_sha256,
        }
        if state == "submitted" and not self._submission_queued:
            self._submission_queued = True
            self.owner._submit("submission", (self.observation, lifecycle))
        else:
            self.owner._submit("lifecycle", lifecycle)
        self._terminal = state in TERMINAL_LIFECYCLE_STATES

    def submitted(self, *, receipt_sha256: str) -> None:
        self._lifecycle("submitted", receipt_sha256)

    def acknowledged(
        self,
        *,
        thread_id: str,
        turn_id: str,
        receipt_sha256: str,
        configured_model: str | None,
        configured_effort: str | None,
    ) -> None:
        if not self._submission_queued or self._terminal:
            self.owner.collector_fault()
            return
        adapter = self.owner.adapter
        binding = {
            "binding_id": str(uuid4()),
            "project_id": adapter.project_id,
            "registration_generation": adapter.registration_generation,
            "connection_id": adapter.connection_id,
            "dispatch_id": self.dispatch_id,
            **{
                field: self.observation[field]
                for field in (
                    "agent_id",
                    "packet_ref",
                    "packet_sha256",
                    "supervisor_submission_ref",
                )
            },
            "thread_fingerprint": adapter.fingerprint("thread", thread_id),
            "turn_fingerprint": adapter.fingerprint("turn", turn_id),
            "acknowledgement_receipt_sha256": receipt_sha256,
            "acknowledgement_provenance": "trusted-controller-receipt",
            "state": "active",
        }
        for field, value, vocabulary in (
            ("model", configured_model, self.owner.ledger.model_vocabulary),
            ("effort", configured_effort, self.owner.ledger.effort_vocabulary),
        ):
            binding["configured_" + field] = normalize_bounded_label(
                value, vocabulary, nullable=True
            )
            binding["configured_" + field + "_provenance"] = (
                "acknowledged-thread-configuration" if value is not None else None
            )
        self.owner._submit("binding", binding)
        self._lifecycle("acknowledged", receipt_sha256)

    def running(self, *, receipt_sha256: str) -> None:
        self._lifecycle("running", receipt_sha256)

    def terminal(self, *, state: str, receipt_sha256: str) -> None:
        if state not in TERMINAL_LIFECYCLE_STATES:
            raise ValueError("terminal controller state required")
        self._lifecycle(state, receipt_sha256)
