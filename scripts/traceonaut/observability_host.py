"""Composition for the optional CWO-owned app-server telemetry side channel.

The host owns only observation components.  It exposes an
``OwnedRuntimeObserver`` for an existing authorized controller and has no
worker submission, interrupt, or stop operation.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
from typing import Any, Mapping
from uuid import uuid4

from .observability_adapter import AppServerCompletionAdapter
from .observability_contract import (
    CoverageGap,
    HealthReason,
    RegistrationState,
    TelemetryDisposition,
    normalize_bounded_label,
    normalize_project_registration,
)
from .observability_exporter import (
    MetricsEndpoint,
    ObservabilityExportService,
    PrometheusQueryClient,
    PublicationConfirmer,
    read_credential,
)
from .observability_ledger import ObservabilityLedger
from .observability_runtime import OwnedRuntimeObserver


MAX_CONFIG_BYTES = 1024 * 1024
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_TOP_LEVEL_FIELDS = {
    "state_dir",
    "registration",
    "executor_vocabulary",
    "model_vocabulary",
    "effort_vocabulary",
    "source_compatibility_sha256",
    "metrics",
}
_REGISTRATION_FIELDS = {
    "project_id",
    "owning_principal_id",
    "runtime_owner_id",
    "state",
    "generation",
    "resource_limits",
}
_RESOURCE_LIMIT_FIELDS = {
    "max_frame_bytes",
    "max_json_depth",
    "max_normalized_record_bytes",
    "max_pending_records",
    "max_queue_records",
    "pending_binding_deadline_seconds",
    "max_registrations",
    "max_disk_bytes",
}


class ObservabilityHostError(ValueError):
    """Fixed, non-sensitive composition failure."""


@dataclass(frozen=True)
class _ValidatedConfig:
    state_dir: Path
    registration: dict[str, Any]
    executor_vocabulary: tuple[str, ...]
    model_vocabulary: tuple[str, ...]
    effort_vocabulary: tuple[str, ...]
    source_compatibility_sha256: str
    metrics_host: str
    metrics_port: int
    metrics_credential: bytes
    prometheus_client: PrometheusQueryClient | None
    prometheus_job: str | None
    prometheus_instance: str | None


def _private_file_bytes(path: Path, *, maximum: int) -> bytes:
    if not path.is_absolute():
        raise ValueError("private path must be absolute")
    for parent in reversed(path.parents):
        metadata = parent.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("unsafe private-file ancestor")
        if metadata.st_uid not in (0, os.geteuid()):
            raise ValueError("unsafe private-file ancestor")
        writable = metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        if writable and not (metadata.st_uid == 0 and metadata.st_mode & stat.S_ISVTX):
            raise ValueError("unsafe private-file ancestor")
    flags = os.O_RDONLY | os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise ValueError("private file must be an owner-only regular file")
        content = os.read(fd, maximum + 1)
        if len(content) > maximum:
            raise ValueError("private file exceeds its bound")
        return content
    finally:
        os.close(fd)


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _exact_mapping(value: Any, fields: set[str], name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(f"{name} fields invalid")
    return value


def _vocabulary(value: Any, name: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= 128
        or any(not isinstance(item, str) for item in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError(f"{name} invalid")
    result = tuple(value)
    for item in result:
        normalize_bounded_label(item, result, field=name)
    return result


def _absolute_path(value: Any, name: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{name} invalid")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{name} must be absolute")
    return path


def _validate_config(config_file: Path) -> _ValidatedConfig:
    raw = _private_file_bytes(config_file, maximum=MAX_CONFIG_BYTES)
    try:
        source = json.loads(raw, object_pairs_hook=_object_without_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ValueError("configuration JSON invalid") from None
    if not isinstance(source, Mapping):
        raise ValueError("configuration must be an object")
    optional = {"prometheus"}
    if not _TOP_LEVEL_FIELDS.issubset(source) or set(source).difference(
        _TOP_LEVEL_FIELDS | optional
    ):
        raise ValueError("configuration fields invalid")

    state_dir = _absolute_path(source["state_dir"], "state_dir")
    registration_source = _exact_mapping(
        source["registration"], _REGISTRATION_FIELDS, "registration"
    )
    _exact_mapping(
        registration_source["resource_limits"],
        _RESOURCE_LIMIT_FIELDS,
        "registration.resource_limits",
    )
    registration = normalize_project_registration(registration_source)
    if (
        registration["owning_principal_id"] != os.geteuid()
        or registration["state"] != RegistrationState.ENABLED.value
    ):
        raise ValueError("registration owner or state invalid")

    executor_vocabulary = _vocabulary(
        source["executor_vocabulary"], "executor_vocabulary"
    )
    model_vocabulary = _vocabulary(source["model_vocabulary"], "model_vocabulary")
    effort_vocabulary = _vocabulary(source["effort_vocabulary"], "effort_vocabulary")
    compatibility = source["source_compatibility_sha256"]
    if (
        not isinstance(compatibility, str)
        or _SHA256_RE.fullmatch(compatibility) is None
    ):
        raise ValueError("source compatibility invalid")

    metrics = _exact_mapping(
        source["metrics"], {"host", "port", "credential_file"}, "metrics"
    )
    host = metrics["host"]
    try:
        address = ipaddress.ip_address(host)
    except (TypeError, ValueError):
        raise ValueError("metrics host must be numeric loopback") from None
    if not address.is_loopback:
        raise ValueError("metrics host must be numeric loopback")
    port = metrics["port"]
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("metrics port invalid")
    metrics_credential_path = _absolute_path(
        metrics["credential_file"], "metrics.credential_file"
    )
    metrics_credential = read_credential(metrics_credential_path)

    prometheus_client: PrometheusQueryClient | None = None
    prometheus_job: str | None = None
    prometheus_instance: str | None = None
    if "prometheus" in source:
        prometheus = source["prometheus"]
        if not isinstance(prometheus, Mapping):
            raise ValueError("prometheus fields invalid")
        required = {"url", "job", "instance"}
        if not required.issubset(prometheus) or set(prometheus).difference(
            required | {"credential_file"}
        ):
            raise ValueError("prometheus fields invalid")
        prometheus_credential = None
        if prometheus.get("credential_file") is not None:
            prometheus_credential = read_credential(
                _absolute_path(
                    prometheus["credential_file"],
                    "prometheus.credential_file",
                )
            )
        prometheus_job = prometheus["job"]
        prometheus_instance = prometheus["instance"]
        if not isinstance(prometheus_job, str) or not isinstance(
            prometheus_instance, str
        ):
            raise ValueError("prometheus identity invalid")
        prometheus_client = PrometheusQueryClient(
            prometheus["url"], credential=prometheus_credential
        )
        # Validate the bounded scrape identity before opening durable state.
        PublicationConfirmer(
            object(),
            prometheus_client,
            job=prometheus_job,
            instance=prometheus_instance,
        )

    return _ValidatedConfig(
        state_dir=state_dir,
        registration=registration,
        executor_vocabulary=executor_vocabulary,
        model_vocabulary=model_vocabulary,
        effort_vocabulary=effort_vocabulary,
        source_compatibility_sha256=compatibility,
        metrics_host=str(address),
        metrics_port=port,
        metrics_credential=metrics_credential,
        prometheus_client=prometheus_client,
        prometheus_job=prometheus_job,
        prometheus_instance=prometheus_instance,
    )


class ObservabilityHost:
    """Resources shared by one explicitly owned app-server connection."""

    def __init__(
        self,
        *,
        ledger: ObservabilityLedger,
        adapter: AppServerCompletionAdapter,
        observer: OwnedRuntimeObserver,
        endpoint: MetricsEndpoint,
        export_service: ObservabilityExportService,
    ) -> None:
        self.ledger = ledger
        self.adapter = adapter
        self.observer = observer
        self.metrics_endpoint = endpoint
        self.export_service = export_service
        self.project_id = adapter.project_id
        self._lock = threading.Lock()
        self._closed = False
        self._service_closed = False
        self._shutdown_gap_recorded = False
        self._shutdown_gap_lock = threading.Lock()
        self._close_thread: threading.Thread | None = None
        self._close_finished = threading.Event()
        self._close_result = False

    def _record_shutdown_gap(self) -> bool:
        with self._shutdown_gap_lock:
            if self._shutdown_gap_recorded:
                return True
            try:
                with self.observer._loss_lock:
                    dispatch_ids = sorted(self.observer._dispatch_ids)
                self.ledger.mark_gap(
                    self.project_id,
                    CoverageGap.SHUTDOWN_PENDING,
                    dispatch_ids=dispatch_ids,
                )
                self.ledger.record_health_event(
                    self.project_id,
                    TelemetryDisposition.LOST,
                    reason=HealthReason.SHUTDOWN_PENDING,
                )
                self._shutdown_gap_recorded = True
                return True
            except Exception:
                return False

    def _close_worker(self) -> None:
        try:
            service_ok = True
            if not self._service_closed:
                try:
                    self.export_service.close()
                except Exception:
                    service_ok = False
                service_ok = service_ok and all(
                    not thread.is_alive() for thread in self.export_service._threads
                )
                self._service_closed = service_ok

            owner_ok = self.observer.close(timeout_seconds=5.0)
            owner_thread = self.observer._thread
            if not owner_ok and (owner_thread is None or not owner_thread.is_alive()):
                try:
                    self.observer.drain_once(max_records=128)
                except Exception:
                    pass
                with self.observer._loss_lock:
                    owner_ok = (
                        self.observer._queue.empty() and not self.observer._losses
                    )
            self.adapter.close(
                drain=True,
                mark_disconnected=True,
                expected_disconnect=service_ok and owner_ok,
                timeout_seconds=5.0,
            )
            adapter_thread = self.adapter._thread
            if adapter_thread is not None and adapter_thread.is_alive():
                adapter_thread.join(timeout=5.0)
            if adapter_thread is None or not adapter_thread.is_alive():
                self.adapter._attempt_pending_disconnect()
                self.adapter._flush_health()
            with self.adapter._state_lock:
                pending_disconnect = self.adapter._pending_disconnect
            with self.adapter._counter_lock:
                adapter_counters_clear = not any(
                    (
                        self.adapter._lost,
                        self.adapter._commit_lost,
                        self.adapter._rejected,
                        sum(self.adapter._malformed.values()),
                    )
                )
            adapter_ok = (
                (adapter_thread is None or not adapter_thread.is_alive())
                and self.adapter.queue_depth == 0
                and pending_disconnect is None
                and adapter_counters_clear
            )

            safe = owner_ok and adapter_ok and service_ok
            if not safe:
                self._record_shutdown_gap()
            if safe:
                self.ledger.close()
                self._closed = True
            self._close_result = safe
        except Exception:
            self._record_shutdown_gap()
            self._close_result = False
        finally:
            self._close_finished.set()

    def close(self, *, timeout_seconds: float = 2.0) -> bool:
        """Wait at most ``timeout_seconds`` while cleanup continues safely."""

        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0 <= timeout_seconds <= 5
        ):
            raise ValueError("bounded observability close timeout required")
        with self._lock:
            if self._closed:
                return True
            if self._close_thread is None or (
                not self._close_thread.is_alive() and not self._close_result
            ):
                self._close_finished.clear()
                self._close_thread = threading.Thread(
                    target=self._close_worker,
                    name="cwo-observability-close",
                    daemon=True,
                )
                self._close_thread.start()
            close_thread = self._close_thread
        close_thread.join(timeout=float(timeout_seconds))
        if close_thread.is_alive():
            return False
        return self._close_result

    def __enter__(self) -> "ObservabilityHost":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


def _cleanup_partial_host(
    ledger: ObservabilityLedger | None,
    adapter: AppServerCompletionAdapter | None,
    observer: OwnedRuntimeObserver | None,
    endpoint: MetricsEndpoint | None,
    service: ObservabilityExportService | None,
) -> None:
    """Continue reverse-order cleanup even after the factory returns failure."""

    try:
        if observer is not None:
            observer.close(timeout_seconds=5.0)
        if adapter is not None:
            adapter.close(timeout_seconds=5.0)
            adapter_thread = adapter._thread
            if adapter_thread is not None and adapter_thread.is_alive():
                adapter_thread.join(timeout=5.0)
            if adapter_thread is None or not adapter_thread.is_alive():
                adapter._attempt_pending_disconnect()
                adapter._flush_health()
        if service is not None:
            service.close()
        elif endpoint is not None:
            endpoint.close()
        if ledger is None:
            return
        adapter_thread = adapter._thread if adapter is not None else None
        observer_thread = observer._thread if observer is not None else None
        service_threads = service._threads if service is not None else ()
        if (
            (adapter_thread is None or not adapter_thread.is_alive())
            and (observer_thread is None or not observer_thread.is_alive())
            and not any(thread.is_alive() for thread in service_threads)
        ):
            ledger.close()
    except Exception:
        # The still-live daemon component retains the ledger reference and
        # writer lock. Closing it underneath that component would corrupt the
        # cleanup ordering; process exit remains the final bound.
        pass


def open_observability_host(config_file: Path) -> ObservabilityHost:
    """Open the optional host or raise one fixed, non-sensitive error."""

    ledger: ObservabilityLedger | None = None
    adapter: AppServerCompletionAdapter | None = None
    observer: OwnedRuntimeObserver | None = None
    endpoint: MetricsEndpoint | None = None
    service: ObservabilityExportService | None = None
    try:
        if not isinstance(config_file, Path):
            raise ValueError("configuration path invalid")
        config = _validate_config(config_file)
        ledger = ObservabilityLedger(
            config.state_dir,
            executor_vocabulary=config.executor_vocabulary,
            model_vocabulary=config.model_vocabulary,
            effort_vocabulary=config.effort_vocabulary,
        )
        # The writer constructor reconciles incomplete owned connections before
        # this registration generation and fresh connection are admitted.
        ledger.register_project(config.registration)
        adapter = AppServerCompletionAdapter(
            ledger,
            project_id=config.registration["project_id"],
            registration_generation=config.registration["generation"],
            connection_id=str(uuid4()),
            source_compatibility_sha256=config.source_compatibility_sha256,
        )
        observer = OwnedRuntimeObserver(adapter)
        endpoint = MetricsEndpoint(
            config.metrics_host,
            config.metrics_port,
            config.metrics_credential,
        )
        confirmer = None
        if config.prometheus_client is not None:
            assert config.prometheus_job is not None
            assert config.prometheus_instance is not None
            confirmer = PublicationConfirmer(
                ledger,
                config.prometheus_client,
                job=config.prometheus_job,
                instance=config.prometheus_instance,
            )
        service = ObservabilityExportService(
            ledger.snapshot,
            endpoint,
            confirmer=confirmer,
            clock_epoch_id=observer.clock_epoch_id,
        )
        service.start()
        return ObservabilityHost(
            ledger=ledger,
            adapter=adapter,
            observer=observer,
            endpoint=endpoint,
            export_service=service,
        )
    except Exception:
        cleanup = threading.Thread(
            target=_cleanup_partial_host,
            args=(ledger, adapter, observer, endpoint, service),
            name="cwo-observability-open-cleanup",
            daemon=True,
        )
        cleanup.start()
        cleanup.join(timeout=2.0)
        raise ObservabilityHostError("observability-unavailable") from None


__all__ = [
    "MAX_CONFIG_BYTES",
    "ObservabilityHost",
    "ObservabilityHostError",
    "open_observability_host",
]
