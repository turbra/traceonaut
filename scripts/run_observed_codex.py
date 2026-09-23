#!/usr/bin/env python3
"""Run one or two fresh standard Codex jobs with owned response observation.

The runner owns the app-server process and its fresh ephemeral threads.  It
persists controller receipts and hashes, but never persists model text, raw
reasoning, app-server stderr, or complete protocol messages.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Mapping
import unicodedata
import uuid

from traceonaut.observability_host import open_observability_host
from traceonaut.observability_presentation import record_presentation_submission


SUPPORTED_CODEX_VERSION = "0.154.0"
EXECUTOR_LABEL = "standard_codex"
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_PROMPT_BYTES = 128 * 1024
MAX_STDOUT_FRAME_BYTES = 1024 * 1024
MAX_STDOUT_FRAMES = 100_000
MAX_JSON_DEPTH = 32
MAX_STDERR_BYTES = 16 * 1024 * 1024
MAX_LINGER_SECONDS = 300.0
MAX_TIMEOUT_SECONDS = 3600.0
INTERRUPT_GRACE_SECONDS = 2.0
PROCESS_CLOSE_SECONDS = 5.0
GROUP_TERM_GRACE_SECONDS = 0.5
_LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_EFFORT_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")
_STAGE_RE = re.compile(r"[a-z][a-z0-9-]{0,47}")


class RunnerError(RuntimeError):
    """A bounded failure code suitable for the normalized CLI result."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise RunnerError("json-value-invalid") from None


def _sha256_value(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _safe_private_ancestors(path: Path) -> None:
    for parent in reversed(path.parents):
        metadata = parent.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise RunnerError("private-path-ancestor-invalid")
        if metadata.st_uid not in (0, os.geteuid()):
            raise RunnerError("private-path-ancestor-invalid")
        writable = metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        if writable and not (
            metadata.st_uid == 0 and metadata.st_mode & stat.S_ISVTX
        ):
            raise RunnerError("private-path-ancestor-invalid")


def _private_file_bytes(path: Path, *, maximum: int) -> bytes:
    if not path.is_absolute():
        raise RunnerError("private-file-path-not-absolute")
    try:
        _safe_private_ancestors(path)
        flags = os.O_RDONLY | os.O_NONBLOCK
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
    except (OSError, RunnerError):
        raise RunnerError("private-file-invalid") from None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise RunnerError("private-file-invalid")
        content = os.read(descriptor, maximum + 1)
        if len(content) > maximum:
            raise RunnerError("private-file-too-large")
        return content
    finally:
        os.close(descriptor)


def _private_receipt_directory(path: Path) -> Path:
    if not path.is_absolute():
        raise RunnerError("receipt-directory-path-not-absolute")
    try:
        _safe_private_ancestors(path)
        metadata = path.lstat()
    except (OSError, RunnerError):
        raise RunnerError("receipt-directory-invalid") from None
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise RunnerError("receipt-directory-invalid")
    return path


def _canonical_uuid(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise RunnerError(f"{field_name}-invalid")
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        raise RunnerError(f"{field_name}-invalid") from None
    if str(parsed) != value:
        raise RunnerError(f"{field_name}-invalid")
    return value


@dataclass(frozen=True)
class JobSpec:
    job_id: str
    cwd: Path
    model: str
    effort: str
    prompt: str
    task_name: str | None = None
    agent_name: str | None = None
    work_item_title: str | None = None


@dataclass(frozen=True)
class JobManifest:
    jobs: tuple[JobSpec, ...]
    timeout_seconds: float
    file_sha256: str


@dataclass(frozen=True)
class RouteAuthorization:
    authorization_id: str
    file_sha256: str
    manifest_sha256: str
    observability_config_sha256: str
    authority_decision_sha256: str
    allowed_jobs: tuple[dict[str, Any], ...]
    max_concurrency: int
    max_wall_seconds: float
    max_linger_seconds: float


@dataclass
class ShutdownLatch:
    signal_number: int | None = None
    _previous: dict[int, Any] = field(default_factory=dict)
    _installed: bool = False

    @property
    def requested(self) -> bool:
        return self.signal_number is not None

    @property
    def failure_code(self) -> str:
        if self.signal_number == signal.SIGINT:
            return "signal-sigint"
        if self.signal_number == signal.SIGTERM:
            return "signal-sigterm"
        return "shutdown-requested"

    def install(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return

        def latch(signum: int, _frame: Any) -> None:
            if self.signal_number is None:
                self.signal_number = signum

        for signum in (signal.SIGINT, signal.SIGTERM):
            self._previous[signum] = signal.getsignal(signum)
            signal.signal(signum, latch)
        self._installed = True

    def restore(self) -> None:
        if not self._installed:
            return
        for signum, handler in self._previous.items():
            signal.signal(signum, handler)
        self._installed = False


def _load_manifest(path: Path) -> JobManifest:
    raw = _private_file_bytes(path, maximum=MAX_MANIFEST_BYTES)
    try:
        source = json.loads(raw, object_pairs_hook=_object_without_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise RunnerError("manifest-json-invalid") from None
    if not isinstance(source, Mapping) or set(source) != {
        "version",
        "jobs",
        "timeout_seconds",
    }:
        raise RunnerError("manifest-fields-invalid")
    if source["version"] != 1 or isinstance(source["version"], bool):
        raise RunnerError("manifest-version-invalid")
    timeout = source["timeout_seconds"]
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(float(timeout))
        or not 0.1 <= float(timeout) <= MAX_TIMEOUT_SECONDS
    ):
        raise RunnerError("manifest-timeout-invalid")
    jobs_source = source["jobs"]
    if not isinstance(jobs_source, list) or not 1 <= len(jobs_source) <= 2:
        raise RunnerError("manifest-job-count-invalid")
    jobs: list[JobSpec] = []
    seen: set[str] = set()
    for value in jobs_source:
        base_fields = {
            "job_id",
            "cwd",
            "model",
            "effort",
            "prompt",
        }
        named_fields = base_fields | {"task_name", "agent_name"}
        titled_fields = named_fields | {"work_item_title"}
        if not isinstance(value, Mapping) or frozenset(value) not in {
            frozenset(base_fields),
            frozenset(named_fields),
            frozenset(titled_fields),
        }:
            raise RunnerError("manifest-job-fields-invalid")
        job_id = _canonical_uuid(value["job_id"], "job-id")
        if job_id in seen:
            raise RunnerError("manifest-job-id-duplicate")
        seen.add(job_id)
        cwd_value = value["cwd"]
        if not isinstance(cwd_value, str) or not cwd_value or "\x00" in cwd_value:
            raise RunnerError("manifest-job-cwd-invalid")
        cwd = Path(cwd_value)
        try:
            resolved = cwd.resolve(strict=True)
        except OSError:
            raise RunnerError("manifest-job-cwd-invalid") from None
        if not cwd.is_absolute() or resolved != cwd or not cwd.is_dir():
            raise RunnerError("manifest-job-cwd-invalid")
        model = value["model"]
        effort = value["effort"]
        prompt = value["prompt"]
        if not isinstance(model, str) or _LABEL_RE.fullmatch(model) is None:
            raise RunnerError("manifest-job-model-invalid")
        if not isinstance(effort, str) or _EFFORT_RE.fullmatch(effort) is None:
            raise RunnerError("manifest-job-effort-invalid")
        if (
            not isinstance(prompt, str)
            or not prompt.strip()
            or len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES
        ):
            raise RunnerError("manifest-job-prompt-invalid")
        task_name = value.get("task_name")
        agent_name = value.get("agent_name")
        work_item_title = value.get("work_item_title")
        for field_name, display_value in (
            ("task-name", task_name),
            ("agent-name", agent_name),
            ("work-item-title", work_item_title),
        ):
            if display_value is not None and (
                not isinstance(display_value, str)
                or not display_value
                or display_value != display_value.strip()
                or len(display_value) > 256
                or any(
                    unicodedata.category(character).startswith("C")
                    for character in display_value
                )
                or len(display_value.encode("utf-8")) > 1024
            ):
                raise RunnerError(f"manifest-job-{field_name}-invalid")
        jobs.append(
            JobSpec(
                job_id,
                cwd,
                model,
                effort,
                prompt,
                task_name,
                agent_name,
                work_item_title,
            )
        )
    return JobManifest(tuple(jobs), float(timeout), _sha256_bytes(raw))


def _finite_limit(value: Any, name: str, *, minimum: float, maximum: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not minimum <= float(value) <= maximum
    ):
        raise RunnerError(f"authorization-{name}-invalid")
    return float(value)


def _load_authorization(
    path: Path,
    *,
    manifest: JobManifest,
    observability_config_sha256: str,
    linger_seconds: float,
) -> RouteAuthorization:
    raw = _private_file_bytes(path, maximum=MAX_MANIFEST_BYTES)
    try:
        source = json.loads(raw, object_pairs_hook=_object_without_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise RunnerError("authorization-json-invalid") from None
    expected_fields = {
        "version",
        "authorization_id",
        "route",
        "manifest_sha256",
        "observability_config_sha256",
        "allowed_jobs",
        "limits",
        "sandbox",
        "network_access",
        "native_attestation_claimed",
        "native_policy_override",
        "authority_kind",
        "authority_scope",
        "authority_decision_sha256",
    }
    if not isinstance(source, Mapping) or set(source) != expected_fields:
        raise RunnerError("authorization-fields-invalid")
    if source["version"] != 1 or isinstance(source["version"], bool):
        raise RunnerError("authorization-version-invalid")
    authorization_id = _canonical_uuid(
        source["authorization_id"], "authorization-id"
    )
    if source["route"] != EXECUTOR_LABEL:
        raise RunnerError("authorization-route-invalid")
    if (
        source["authority_kind"] != "explicit-user-authorization"
        or source["authority_scope"] != "standard-codex-observability"
    ):
        raise RunnerError("authorization-authority-invalid")
    authority_decision_sha256 = source["authority_decision_sha256"]
    if (
        not isinstance(authority_decision_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", authority_decision_sha256) is None
    ):
        raise RunnerError("authorization-decision-invalid")
    if source["manifest_sha256"] != manifest.file_sha256:
        raise RunnerError("authorization-manifest-mismatch")
    if source["observability_config_sha256"] != observability_config_sha256:
        raise RunnerError("authorization-observability-config-mismatch")
    if (
        source["sandbox"] != "read-only"
        or source["network_access"] is not False
        or source["native_attestation_claimed"] is not False
        or source["native_policy_override"] is not False
    ):
        raise RunnerError("authorization-controls-invalid")
    limits = source["limits"]
    if not isinstance(limits, Mapping) or set(limits) != {
        "max_concurrency",
        "max_wall_seconds",
        "max_linger_seconds",
    }:
        raise RunnerError("authorization-limits-fields-invalid")
    max_concurrency = limits["max_concurrency"]
    if (
        type(max_concurrency) is not int
        or not 1 <= max_concurrency <= 2
        or len(manifest.jobs) > max_concurrency
    ):
        raise RunnerError("authorization-max-concurrency-invalid")
    max_wall_seconds = _finite_limit(
        limits["max_wall_seconds"],
        "max-wall-seconds",
        minimum=0.1,
        maximum=MAX_TIMEOUT_SECONDS,
    )
    max_linger_seconds = _finite_limit(
        limits["max_linger_seconds"],
        "max-linger-seconds",
        minimum=0.0,
        maximum=MAX_LINGER_SECONDS,
    )
    if manifest.timeout_seconds > max_wall_seconds:
        raise RunnerError("authorization-wall-limit-exceeded")
    if float(linger_seconds) > max_linger_seconds:
        raise RunnerError("authorization-linger-limit-exceeded")

    jobs_source = source["allowed_jobs"]
    if not isinstance(jobs_source, list) or len(jobs_source) != len(manifest.jobs):
        raise RunnerError("authorization-jobs-invalid")
    allowed: dict[str, dict[str, Any]] = {}
    for value in jobs_source:
        if not isinstance(value, Mapping) or set(value) != {
            "job_id",
            "cwd",
            "model",
            "effort",
            "prompt_sha256",
        }:
            raise RunnerError("authorization-job-fields-invalid")
        job_id = _canonical_uuid(value["job_id"], "authorization-job-id")
        if job_id in allowed:
            raise RunnerError("authorization-job-duplicate")
        allowed[job_id] = dict(value)
    for job in manifest.jobs:
        expected = {
            "job_id": job.job_id,
            "cwd": str(job.cwd),
            "model": job.model,
            "effort": job.effort,
            "prompt_sha256": _sha256_bytes(job.prompt.encode("utf-8")),
        }
        if allowed.get(job.job_id) != expected:
            raise RunnerError("authorization-job-mismatch")
    ordered = tuple(allowed[job.job_id] for job in manifest.jobs)
    return RouteAuthorization(
        authorization_id=authorization_id,
        file_sha256=_sha256_bytes(raw),
        manifest_sha256=manifest.file_sha256,
        observability_config_sha256=observability_config_sha256,
        authority_decision_sha256=authority_decision_sha256,
        allowed_jobs=ordered,
        max_concurrency=max_concurrency,
        max_wall_seconds=max_wall_seconds,
        max_linger_seconds=max_linger_seconds,
    )


class ReceiptWriter:
    def __init__(self, directory: Path) -> None:
        self.directory = _private_receipt_directory(directory)
        self._counts: dict[str, int] = {}
        self._heads: dict[str, str | None] = {}

    def write(
        self,
        job_id: str,
        stage: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        _canonical_uuid(job_id, "receipt-job-id")
        if _STAGE_RE.fullmatch(stage) is None:
            raise RunnerError("receipt-stage-invalid")
        count = self._counts.get(job_id, 0) + 1
        body = {
            "receipt_type": "cwo-observed-codex-controller-receipt",
            "version": 1,
            "job_id": job_id,
            "sequence": count,
            "stage": stage,
            "previous_receipt_sha256": self._heads.get(job_id),
            **dict(payload),
        }
        if set(payload).intersection(
            {
                "receipt_type",
                "version",
                "job_id",
                "sequence",
                "stage",
                "previous_receipt_sha256",
                "receipt_sha256",
            }
        ):
            raise RunnerError("receipt-payload-fields-invalid")
        sealed = dict(body)
        sealed["receipt_sha256"] = _sha256_value(body)
        data = _canonical_bytes(sealed) + b"\n"
        final = self.directory / f"{job_id}.{count:02d}-{stage}.json"
        temporary = self.directory / f".{job_id}.{count:02d}.{uuid.uuid4()}.tmp"
        descriptor = -1
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(temporary, flags, 0o600)
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short receipt write")
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.link(temporary, final, follow_symlinks=False)
            temporary.unlink()
            directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except OSError:
                pass
            raise RunnerError("receipt-write-failed") from None
        self._counts[job_id] = count
        self._heads[job_id] = sealed["receipt_sha256"]
        return sealed


@dataclass(frozen=True)
class PreparedRequest:
    request_id: int
    method: str
    params: dict[str, Any]
    payload: dict[str, Any]
    wire_sha256: str


@dataclass(frozen=True)
class RpcResult:
    result: dict[str, Any]
    controller_sequence: int


@dataclass
class _PendingRequest:
    response: dict[str, Any] | None = None
    controller_sequence: int | None = None


def _json_depth(value: Any) -> int:
    maximum = 0
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        maximum = max(maximum, depth)
        if maximum > MAX_JSON_DEPTH:
            return maximum
        if isinstance(current, Mapping):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
    return maximum


def _valid_rpc_id(value: Any) -> bool:
    return (type(value) is int and value >= 0) or (
        isinstance(value, str) and 0 < len(value) <= 256
    )


_APPROVAL_DENIALS: dict[str, dict[str, Any] | None] = {
    "item/commandExecution/requestApproval": {"decision": "cancel"},
    "item/fileChange/requestApproval": {"decision": "cancel"},
    "applyPatchApproval": {"decision": "abort"},
    "execCommandApproval": {"decision": "abort"},
    "mcpServer/elicitation/request": {"action": "cancel"},
    # The permissions response has no explicit denial variant.  A protocol
    # error denies the request without fabricating a permission profile.
    "item/permissions/requestApproval": None,
}


class AppServerClient:
    """Bounded JSON-RPC client that retains responses only until acknowledged."""

    def __init__(
        self,
        *,
        notification_handler: Callable[[Mapping[str, Any], int], None],
        source_closed_handler: Callable[[bool], None],
        shutdown_latch: ShutdownLatch,
        popen_factory: Callable[..., Any] = subprocess.Popen,
    ) -> None:
        self._notification_handler = notification_handler
        self._source_closed_handler = source_closed_handler
        self._shutdown_latch = shutdown_latch
        self._condition = threading.Condition()
        self._write_lock = threading.Lock()
        self._request_id = 0
        self._pending: dict[int, _PendingRequest] = {}
        self._fatal: str | None = None
        self._expected_close = False
        self._source_closed = False
        self._stdout_frames = 0
        self._stderr_bytes = 0
        self._approval_denials = 0
        try:
            self.process = popen_factory(
                ["codex", "app-server", "--listen", "stdio://"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                close_fds=True,
                start_new_session=True,
                umask=0o077,
            )
        except (OSError, ValueError):
            raise RunnerError("app-server-spawn-failed") from None
        if (
            self.process.stdin is None
            or self.process.stdout is None
            or self.process.stderr is None
        ):
            raise RunnerError("app-server-pipes-unavailable")
        try:
            self._process_group_id = os.getpgid(self.process.pid)
        except (AttributeError, OSError):
            try:
                self.process.terminate()
                self.process.wait(timeout=PROCESS_CLOSE_SECONDS)
            except Exception:
                pass
            raise RunnerError("app-server-process-group-invalid") from None
        if self._process_group_id != self.process.pid:
            try:
                self.process.terminate()
                self.process.wait(timeout=PROCESS_CLOSE_SECONDS)
            except Exception:
                pass
            raise RunnerError("app-server-process-group-invalid")
        self.process_group_closed = False
        self._stdout_thread = threading.Thread(
            target=self._read_stdout,
            name="cwo-observed-app-server-stdout",
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            name="cwo-observed-app-server-stderr",
            daemon=True,
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

    @property
    def approval_denials(self) -> int:
        with self._condition:
            return self._approval_denials

    def _set_fatal(self, code: str) -> None:
        with self._condition:
            if self._fatal is None:
                self._fatal = code
            self._condition.notify_all()

    def raise_if_failed(self) -> None:
        with self._condition:
            if self._fatal is not None:
                raise RunnerError(self._fatal)
            returncode = self.process.poll()
            if returncode is not None and not self._expected_close:
                raise RunnerError("app-server-exited")

    def wait_for_activity(self, timeout: float) -> None:
        with self._condition:
            self._condition.wait(timeout=max(0.0, timeout))

    def prepare_request(self, method: str, params: Mapping[str, Any]) -> PreparedRequest:
        if not isinstance(method, str) or not method:
            raise RunnerError("request-method-invalid")
        with self._condition:
            self.raise_if_failed()
            self._request_id += 1
            request_id = self._request_id
        payload = {"id": request_id, "method": method, "params": dict(params)}
        return PreparedRequest(
            request_id=request_id,
            method=method,
            params=dict(params),
            payload=payload,
            wire_sha256=_sha256_value(payload),
        )

    def _write_payload(self, payload: Mapping[str, Any]) -> None:
        encoded = _canonical_bytes(dict(payload)) + b"\n"
        try:
            with self._write_lock:
                self.process.stdin.write(encoded)
                self.process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            self._set_fatal("app-server-write-failed")
            raise RunnerError("app-server-write-failed") from None

    def submit(
        self,
        request: PreparedRequest,
        *,
        deadline: float,
        allow_during_shutdown: bool = False,
        after_write: Callable[[], None] | None = None,
    ) -> RpcResult:
        pending = _PendingRequest()
        with self._condition:
            self.raise_if_failed()
            if request.request_id in self._pending:
                raise RunnerError("request-id-duplicate")
            self._pending[request.request_id] = pending
        try:
            self._write_payload(request.payload)
            if after_write is not None:
                try:
                    after_write()
                except Exception:
                    # Optional side effects cannot invalidate a submitted RPC.
                    pass
            with self._condition:
                while pending.response is None:
                    if self._shutdown_latch.requested and not allow_during_shutdown:
                        raise RunnerError("shutdown-requested")
                    if self._fatal is not None:
                        raise RunnerError(self._fatal)
                    if self.process.poll() is not None:
                        raise RunnerError("app-server-exited")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise RunnerError("app-server-request-timeout")
                    self._condition.wait(timeout=min(remaining, 0.1))
                response = pending.response
                response_sequence = pending.controller_sequence
        finally:
            with self._condition:
                self._pending.pop(request.request_id, None)
        if not isinstance(response, Mapping):
            raise RunnerError("app-server-response-invalid")
        if "error" in response:
            error = response.get("error")
            if (
                "result" in response
                or not isinstance(error, Mapping)
                or type(error.get("code")) is not int
            ):
                raise RunnerError("app-server-error-response-invalid")
            raise RunnerError(
                f"app-server-rpc-error-{request.method.replace('/', '-')}-{error['code']}"
            )
        result = response.get("result")
        if not isinstance(result, Mapping) or type(response_sequence) is not int:
            raise RunnerError("app-server-result-invalid")
        return RpcResult(dict(result), response_sequence)

    def request(
        self, method: str, params: Mapping[str, Any], *, deadline: float
    ) -> tuple[PreparedRequest, RpcResult]:
        request = self.prepare_request(method, params)
        return request, self.submit(request, deadline=deadline)

    def notify(self, method: str, params: Mapping[str, Any]) -> None:
        self._write_payload({"method": method, "params": dict(params)})

    def _handle_server_request(self, message: Mapping[str, Any]) -> None:
        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params")
        if (
            not _valid_rpc_id(request_id)
            or not isinstance(method, str)
            or not isinstance(params, Mapping)
        ):
            self._set_fatal("server-request-invalid")
            return
        if method not in _APPROVAL_DENIALS:
            self._write_payload(
                {
                    "id": request_id,
                    "error": {"code": -32000, "message": "request rejected"},
                }
            )
            self._set_fatal("server-request-unexpected")
            return
        denial = _APPROVAL_DENIALS[method]
        response = (
            {"id": request_id, "result": denial}
            if denial is not None
            else {
                "id": request_id,
                "error": {"code": -32000, "message": "request denied"},
            }
        )
        self._write_payload(response)
        with self._condition:
            self._approval_denials += 1
            self._condition.notify_all()

    def _handle_message(self, value: Any) -> None:
        if not isinstance(value, Mapping) or _json_depth(value) > MAX_JSON_DEPTH:
            self._set_fatal("app-server-message-invalid")
            return
        message = dict(value)
        has_id = "id" in message
        method = message.get("method")
        if has_id and isinstance(method, str):
            self._handle_server_request(message)
            return
        if has_id and ("result" in message or "error" in message):
            response_id = message.get("id")
            if type(response_id) is not int:
                self._set_fatal("app-server-response-id-invalid")
                return
            with self._condition:
                pending = self._pending.get(response_id)
                if pending is None:
                    self._set_fatal("app-server-response-id-unexpected")
                    return
                if pending.response is not None:
                    self._set_fatal("app-server-response-duplicate")
                    return
                pending.response = message
                pending.controller_sequence = self._stdout_frames
                self._condition.notify_all()
            return
        if not has_id and isinstance(method, str):
            if not isinstance(message.get("params"), Mapping):
                self._set_fatal("app-server-notification-invalid")
                return
            with self._condition:
                sequence = self._stdout_frames
            try:
                self._notification_handler(message, sequence)
            except RunnerError as exc:
                self._set_fatal(exc.code)
            except Exception:
                self._set_fatal("notification-handler-failed")
            return
        self._set_fatal("app-server-message-unexpected")

    def _read_stdout(self) -> None:
        buffer = bytearray()
        try:
            while True:
                chunk = self.process.stdout.read(64 * 1024)
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    self._set_fatal("app-server-stdout-mode-invalid")
                    return
                buffer.extend(chunk)
                while True:
                    newline = buffer.find(b"\n")
                    if newline < 0:
                        break
                    frame = bytes(buffer[:newline])
                    del buffer[: newline + 1]
                    if frame.endswith(b"\r"):
                        frame = frame[:-1]
                    if not frame or len(frame) > MAX_STDOUT_FRAME_BYTES:
                        self._set_fatal("app-server-frame-invalid")
                        return
                    with self._condition:
                        self._stdout_frames += 1
                        if self._stdout_frames > MAX_STDOUT_FRAMES:
                            self._set_fatal("app-server-frame-count-exceeded")
                            return
                    try:
                        value = json.loads(
                            frame.decode("utf-8"),
                            object_pairs_hook=_object_without_duplicates,
                        )
                    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
                        self._set_fatal("app-server-json-invalid")
                        return
                    self._handle_message(value)
                    with self._condition:
                        if self._fatal is not None:
                            return
                if len(buffer) > MAX_STDOUT_FRAME_BYTES:
                    self._set_fatal("app-server-frame-too-large")
                    return
            if buffer:
                self._set_fatal("app-server-frame-truncated")
        except (OSError, ValueError):
            self._set_fatal("app-server-stdout-read-failed")
        finally:
            with self._condition:
                expected = self._expected_close
                if not expected and self._fatal is None:
                    self._fatal = "app-server-output-closed"
                self._condition.notify_all()
            self._report_source_closed(expected)

    def _drain_stderr(self) -> None:
        try:
            while True:
                chunk = self.process.stderr.read(64 * 1024)
                if not chunk:
                    return
                if not isinstance(chunk, bytes):
                    self._set_fatal("app-server-stderr-mode-invalid")
                    return
                with self._condition:
                    self._stderr_bytes = min(
                        MAX_STDERR_BYTES + 1, self._stderr_bytes + len(chunk)
                    )
                    if self._stderr_bytes > MAX_STDERR_BYTES:
                        self._set_fatal("app-server-stderr-bound-exceeded")
                        return
        except (OSError, ValueError):
            return

    def _report_source_closed(self, expected: bool) -> None:
        with self._condition:
            if self._source_closed:
                return
            self._source_closed = True
        try:
            self._source_closed_handler(expected)
        except Exception:
            pass

    def _process_group_exists(self) -> bool:
        try:
            os.killpg(self._process_group_id, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def _signal_process_group(self, signum: int) -> None:
        try:
            os.killpg(self._process_group_id, signum)
        except ProcessLookupError:
            pass

    def close(self) -> bool:
        with self._condition:
            self._expected_close = True
            self._condition.notify_all()
        reaped = False
        try:
            self._signal_process_group(signal.SIGTERM)
        except (OSError, ValueError):
            pass
        term_deadline = time.monotonic() + GROUP_TERM_GRACE_SECONDS
        while time.monotonic() < term_deadline:
            if self.process.poll() is not None:
                reaped = True
            if reaped and not self._process_group_exists():
                break
            time.sleep(0.02)
        if not reaped:
            try:
                self.process.wait(timeout=0)
                reaped = True
            except (OSError, subprocess.TimeoutExpired):
                pass
        if self._process_group_exists() or not reaped:
            try:
                self._signal_process_group(signal.SIGKILL)
            except (OSError, ValueError):
                pass
        if not reaped:
            try:
                self.process.wait(timeout=PROCESS_CLOSE_SECONDS)
                reaped = True
            except (OSError, subprocess.TimeoutExpired):
                reaped = False
        group_deadline = time.monotonic() + 1.0
        while self._process_group_exists() and time.monotonic() < group_deadline:
            time.sleep(0.02)
        self.process_group_closed = not self._process_group_exists()
        try:
            self.process.stdin.close()
        except (OSError, ValueError):
            pass
        self._stdout_thread.join(timeout=1.0)
        self._stderr_thread.join(timeout=1.0)
        for stream in (self.process.stdout, self.process.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                pass
        self._report_source_closed(True)
        return reaped and not self._stdout_thread.is_alive() and not self._stderr_thread.is_alive()


@dataclass(frozen=True)
class TerminalEvent:
    thread_id: str
    turn_id: str
    status: str
    controller_sequence: int
    metadata_sha256: str


@dataclass
class JobState:
    spec: JobSpec
    workload_sha256: str | None = None
    agent_id: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    configured_model: str | None = None
    configured_effort: str | None = None
    dispatch: Any = None
    terminal_event: TerminalEvent | None = None
    terminal_processed: bool = False
    status: str = "pending"
    outcome_receipt_sha256: str | None = None


@dataclass
class _RunControl:
    observer: Any
    jobs_by_thread: dict[str, JobState] = field(default_factory=dict)
    condition: threading.Condition = field(default_factory=threading.Condition)
    observability_degraded: bool = False

    def notification(self, message: Mapping[str, Any], sequence: int) -> None:
        try:
            self.observer.observe_notification(message, sequence=sequence)
        except Exception:
            self.observability_degraded = True
            try:
                self.observer.collector_fault()
            except Exception:
                pass
        if message.get("method") != "turn/completed":
            return
        params = message.get("params")
        turn = params.get("turn") if isinstance(params, Mapping) else None
        thread_id = params.get("threadId") if isinstance(params, Mapping) else None
        turn_id = turn.get("id") if isinstance(turn, Mapping) else None
        status_value = turn.get("status") if isinstance(turn, Mapping) else None
        if (
            not isinstance(thread_id, str)
            or not thread_id
            or not isinstance(turn_id, str)
            or not turn_id
            or status_value not in {"completed", "failed", "interrupted"}
        ):
            raise RunnerError("turn-completed-invalid")
        metadata = {
            "method": "turn/completed",
            "controller_sequence": sequence,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "turn_status": str(status_value),
        }
        event = TerminalEvent(
            thread_id=thread_id,
            turn_id=turn_id,
            status=str(status_value),
            controller_sequence=sequence,
            metadata_sha256=_sha256_value(metadata),
        )
        with self.condition:
            job = self.jobs_by_thread.get(thread_id)
            if job is None:
                raise RunnerError("turn-completed-thread-unexpected")
            if job.turn_id is not None and job.turn_id != turn_id:
                raise RunnerError("turn-completed-turn-unexpected")
            if job.terminal_event is not None:
                raise RunnerError("turn-completed-duplicate")
            job.terminal_event = event
            self.condition.notify_all()


def _safe_lifecycle(control: _RunControl, target: Any, method: str, **values: Any) -> None:
    try:
        getattr(target, method)(**values)
    except Exception:
        control.observability_degraded = True
        try:
            control.observer.collector_fault()
        except Exception:
            pass


def _remaining_deadline(deadline: float, shutdown_latch: ShutdownLatch) -> float:
    if shutdown_latch.requested:
        raise RunnerError("shutdown-requested")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RunnerError("aggregate-timeout")
    return remaining


def _thread_start_params(job: JobSpec) -> dict[str, Any]:
    cwd = str(job.cwd)
    return {
        "model": job.model,
        "allowProviderModelFallback": False,
        "cwd": cwd,
        "runtimeWorkspaceRoots": [cwd],
        "approvalPolicy": "never",
        "sandbox": "read-only",
        "ephemeral": True,
        "historyMode": "legacy",
        "experimentalRawEvents": True,
        "environments": [],
        "config": {"model_reasoning_effort": job.effort},
    }


def _validate_thread_start_result(job: JobSpec, result: Mapping[str, Any]) -> tuple[str, str, str]:
    thread = result.get("thread")
    sandbox = result.get("sandbox")
    if not isinstance(thread, Mapping) or not isinstance(sandbox, Mapping):
        raise RunnerError("thread-start-response-invalid")
    thread_id = _canonical_uuid(thread.get("id"), "thread-id")
    configured_model = result.get("model")
    configured_effort = result.get("reasoningEffort")
    if (
        configured_model != job.model
        or configured_effort != job.effort
        or result.get("cwd") != str(job.cwd)
        or result.get("approvalPolicy") != "never"
        or sandbox.get("type") != "readOnly"
        or sandbox.get("networkAccess") is not False
        or thread.get("cwd") != str(job.cwd)
        or thread.get("ephemeral") is not True
        or thread.get("turns") != []
        or thread.get("model") != job.model
        or thread.get("reasoningEffort") != job.effort
    ):
        raise RunnerError("thread-start-configuration-mismatch")
    return thread_id, str(configured_model), str(configured_effort)


def _validate_turn_start_result(result: Mapping[str, Any]) -> tuple[str, str]:
    turn = result.get("turn")
    if not isinstance(turn, Mapping):
        raise RunnerError("turn-start-response-invalid")
    turn_id = _canonical_uuid(turn.get("id"), "turn-id")
    status_value = turn.get("status")
    if status_value not in {"inProgress", "completed", "failed", "interrupted"}:
        raise RunnerError("turn-start-status-invalid")
    return turn_id, str(status_value)


def _workload(job: JobSpec, manifest_sha256: str) -> dict[str, Any]:
    return {
        "version": 1,
        "job_id": job.job_id,
        "cwd": str(job.cwd),
        "model": job.model,
        "effort": job.effort,
        "prompt": job.prompt,
        "manifest_sha256": manifest_sha256,
        "execution_route": "standard-codex-owned-app-server",
        "sandbox": "read-only",
        "approval_policy": "never",
    }


def _record_workload(
    writer: ReceiptWriter, state: JobState, manifest_sha256: str
) -> None:
    workload = _workload(state.spec, manifest_sha256)
    state.workload_sha256 = _sha256_value(workload)
    writer.write(
        state.spec.job_id,
        "workload",
        {
            "workload_sha256": state.workload_sha256,
            "manifest_sha256": manifest_sha256,
            "prompt_sha256": _sha256_bytes(state.spec.prompt.encode("utf-8")),
            "prompt_bytes": len(state.spec.prompt.encode("utf-8")),
            "cwd": str(state.spec.cwd),
            "requested_model": state.spec.model,
            "requested_effort": state.spec.effort,
            "requested_executor": EXECUTOR_LABEL,
            "sandbox": "read-only",
            "approval_policy": "never",
        },
    )


def _process_terminal(
    writer: ReceiptWriter, control: _RunControl, state: JobState
) -> bool:
    if state.terminal_processed or state.terminal_event is None:
        return False
    event = state.terminal_event
    if state.turn_id is None:
        return False
    if event.turn_id != state.turn_id or event.thread_id != state.thread_id:
        raise RunnerError("terminal-binding-mismatch")
    receipt = writer.write(
        state.spec.job_id,
        "turn-terminal",
        {
            "thread_id": event.thread_id,
            "turn_id": event.turn_id,
            "turn_status": event.status,
            "controller_sequence": event.controller_sequence,
            "terminal_metadata_sha256": event.metadata_sha256,
            "outcome_source": "turn/completed",
        },
    )
    _safe_lifecycle(
        control,
        state.dispatch,
        "terminal",
        state=event.status,
        receipt_sha256=receipt["receipt_sha256"],
    )
    state.status = event.status
    state.terminal_processed = True
    return True


def _record_controller_terminal(
    writer: ReceiptWriter,
    control: _RunControl,
    state: JobState,
    *,
    job_status: str,
    lifecycle_state: str,
    reason: str,
) -> None:
    if state.terminal_processed:
        return
    receipt = writer.write(
        state.spec.job_id,
        "controller-terminal",
        {
            "thread_id": state.thread_id,
            "turn_id": state.turn_id,
            "controller_state": job_status,
            "reason": reason,
            "turn_outcome_observed": False,
        },
    )
    if state.dispatch is not None:
        _safe_lifecycle(
            control,
            state.dispatch,
            "terminal",
            state=lifecycle_state,
            receipt_sha256=receipt["receipt_sha256"],
        )
    state.status = job_status
    state.terminal_processed = True


def _interrupt_active(
    client: AppServerClient,
    writer: ReceiptWriter,
    states: list[JobState],
    *,
    grace_seconds: float,
) -> None:
    interrupt_deadline = time.monotonic() + max(0.1, grace_seconds)
    for state in states:
        if (
            state.turn_id is None
            or state.thread_id is None
            or state.terminal_event is not None
        ):
            continue
        try:
            request = client.prepare_request(
                "turn/interrupt",
                {"threadId": state.thread_id, "turnId": state.turn_id},
            )
            writer.write(
                state.spec.job_id,
                "interrupt-intent",
                {
                    "thread_id": state.thread_id,
                    "turn_id": state.turn_id,
                    "wire_request_sha256": request.wire_sha256,
                },
            )
            result = client.submit(
                request,
                deadline=interrupt_deadline,
                allow_during_shutdown=True,
            )
            response_metadata = {
                "method": "turn/interrupt",
                "controller_request_id": request.request_id,
                "controller_sequence": result.controller_sequence,
                "response_acknowledged": True,
            }
            writer.write(
                state.spec.job_id,
                "interrupt-response",
                {
                    "thread_id": state.thread_id,
                    "turn_id": state.turn_id,
                    "response_metadata_sha256": _sha256_value(response_metadata),
                    "controller_request_id": request.request_id,
                    "controller_sequence": result.controller_sequence,
                    "response_acknowledged": True,
                },
            )
        except RunnerError:
            continue


def _job_outcome(state: JobState) -> dict[str, Any]:
    return {
        "job_id": state.spec.job_id,
        "status": state.status,
        "requested_model": state.spec.model,
        "requested_effort": state.spec.effort,
        "configured_model": state.configured_model,
        "configured_effort": state.configured_effort,
        "thread_id_sha256": (
            _sha256_bytes(state.thread_id.encode("utf-8"))
            if state.thread_id is not None
            else None
        ),
        "turn_id_sha256": (
            _sha256_bytes(state.turn_id.encode("utf-8"))
            if state.turn_id is not None
            else None
        ),
        "outcome_receipt_sha256": state.outcome_receipt_sha256,
    }


def run_observed_jobs(
    *,
    manifest_path: Path,
    authorization_file: Path,
    observability_config: Path,
    receipt_dir: Path,
    linger_seconds: float = 0.0,
    presentation_file: Path | None = None,
    project_name: str | None = None,
    popen_factory: Callable[..., Any] = subprocess.Popen,
    host_factory: Callable[[Path], Any] = open_observability_host,
    interrupt_grace_seconds: float = INTERRUPT_GRACE_SECONDS,
) -> dict[str, Any]:
    if (presentation_file is None) != (project_name is None):
        raise RunnerError("presentation-options-incomplete")
    if (
        isinstance(linger_seconds, bool)
        or not isinstance(linger_seconds, (int, float))
        or not math.isfinite(float(linger_seconds))
        or not 0 <= float(linger_seconds) <= MAX_LINGER_SECONDS
    ):
        raise RunnerError("linger-seconds-invalid")
    manifest = _load_manifest(manifest_path)
    config_bytes = _private_file_bytes(observability_config, maximum=MAX_MANIFEST_BYTES)
    config_sha256 = _sha256_bytes(config_bytes)
    authorization = _load_authorization(
        authorization_file,
        manifest=manifest,
        observability_config_sha256=config_sha256,
        linger_seconds=float(linger_seconds),
    )
    writer = ReceiptWriter(receipt_dir)
    states = [JobState(job) for job in manifest.jobs]
    for state, allowed_job in zip(states, authorization.allowed_jobs, strict=True):
        controls = {
            "route": EXECUTOR_LABEL,
            "sandbox": "read-only",
            "network_access": False,
            "native_attestation_claimed": False,
            "native_policy_override": False,
        }
        writer.write(
            state.spec.job_id,
            "route-authorization",
            {
                "authorization_id": authorization.authorization_id,
                "authorization_file_sha256": authorization.file_sha256,
                "authority_kind": "explicit-user-authorization",
                "authority_scope": "standard-codex-observability",
                "authority_decision_sha256": (
                    authorization.authority_decision_sha256
                ),
                "manifest_sha256": authorization.manifest_sha256,
                "observability_config_sha256": (
                    authorization.observability_config_sha256
                ),
                "job_binding_sha256": _sha256_value(allowed_job),
                "controls_sha256": _sha256_value(controls),
                "limits": {
                    "max_concurrency": authorization.max_concurrency,
                    "max_wall_seconds": authorization.max_wall_seconds,
                    "max_linger_seconds": authorization.max_linger_seconds,
                },
            },
        )
    deadline = time.monotonic() + manifest.timeout_seconds
    shutdown_latch = ShutdownLatch()
    shutdown_latch.install()
    host: Any = None
    client: AppServerClient | None = None
    control: _RunControl | None = None
    failure_code: str | None = None
    timed_out = False
    process_reaped = False
    process_group_closed = False
    host_closed = False
    presentation_state = (
        "not-configured" if presentation_file is None else "not-recorded"
    )
    presentation_warning: str | None = None
    try:
        try:
            host = host_factory(observability_config)
        except Exception:
            raise RunnerError("observability-unavailable") from None
        control = _RunControl(host.observer)
        client = AppServerClient(
            notification_handler=control.notification,
            source_closed_handler=lambda expected: _safe_lifecycle(
                control, control.observer, "source_disconnected", expected=expected
            ),
            shutdown_latch=shutdown_latch,
            popen_factory=popen_factory,
        )
        try:
            _remaining_deadline(deadline, shutdown_latch)
            _initialize_request, initialize_rpc = client.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "cwo-observed-runner",
                        "title": "CWO observed runner",
                        "version": "1.0.0",
                    },
                    "capabilities": {"experimentalApi": True},
                },
                deadline=deadline,
            )
            initialize_result = initialize_rpc.result
            expected_agent_prefix = f"cwo-observed-runner/{SUPPORTED_CODEX_VERSION} "
            if (
                not isinstance(initialize_result.get("codexHome"), str)
                or not isinstance(initialize_result.get("userAgent"), str)
                or not initialize_result["userAgent"].startswith(expected_agent_prefix)
            ):
                raise RunnerError("app-server-version-unsupported")
            client.notify("initialized", {})

            for state in states:
                _remaining_deadline(deadline, shutdown_latch)
                _record_workload(writer, state, manifest.file_sha256)
                params = _thread_start_params(state.spec)
                request = client.prepare_request("thread/start", params)
                writer.write(
                    state.spec.job_id,
                    "thread-start-intent",
                    {
                        "wire_request_sha256": request.wire_sha256,
                        "thread_start_params_sha256": _sha256_value(params),
                        "cwd": str(state.spec.cwd),
                        "requested_model": state.spec.model,
                        "requested_effort": state.spec.effort,
                        "experimental_raw_events": True,
                        "ephemeral": True,
                        "sandbox": "read-only",
                        "approval_policy": "never",
                    },
                )
                rpc = client.submit(request, deadline=deadline)
                result = rpc.result
                thread_id, configured_model, configured_effort = (
                    _validate_thread_start_result(state.spec, result)
                )
                state.thread_id = thread_id
                state.configured_model = configured_model
                state.configured_effort = configured_effort
                with control.condition:
                    if thread_id in control.jobs_by_thread:
                        raise RunnerError("thread-id-duplicate")
                    control.jobs_by_thread[thread_id] = state
                response_metadata = {
                    "method": "thread/start",
                    "controller_request_id": request.request_id,
                    "controller_sequence": rpc.controller_sequence,
                    "thread_id": thread_id,
                    "configured_model": configured_model,
                    "configured_effort": configured_effort,
                    "cwd": str(state.spec.cwd),
                    "sandbox": "read-only",
                    "approval_policy": "never",
                    "ephemeral": True,
                }
                writer.write(
                    state.spec.job_id,
                    "thread-start-response",
                    {
                        "thread_id": thread_id,
                        "response_metadata_sha256": _sha256_value(
                            response_metadata
                        ),
                        "controller_request_id": request.request_id,
                        "controller_sequence": rpc.controller_sequence,
                        "configured_model": configured_model,
                        "configured_effort": configured_effort,
                        "sandbox": "read-only",
                        "approval_policy": "never",
                        "ephemeral": True,
                    },
                )

            for state in states:
                _remaining_deadline(deadline, shutdown_latch)
                if state.workload_sha256 is None or state.thread_id is None:
                    raise RunnerError("job-preparation-incomplete")
                state.agent_id = str(uuid.uuid4())
                try:
                    state.dispatch = control.observer.prepare_dispatch(
                        agent_id=state.agent_id,
                        packet_sha256=state.workload_sha256,
                        supervisor_submission_ref=(
                            f"observed-{state.spec.job_id}-{uuid.uuid4()}"
                        ),
                        requested_executor=EXECUTOR_LABEL,
                        requested_model=state.spec.model,
                        requested_effort=state.spec.effort,
                        declared_elapsed_allowance_seconds=manifest.timeout_seconds,
                        enforced_runtime_limit_seconds=manifest.timeout_seconds,
                    )
                except Exception:
                    raise RunnerError("observability-dispatch-prepare-failed") from None
                params = {
                    "threadId": state.thread_id,
                    "input": [
                        {
                            "type": "text",
                            "text": state.spec.prompt,
                            "text_elements": [],
                        }
                    ],
                    "model": state.spec.model,
                    "effort": state.spec.effort,
                    "cwd": str(state.spec.cwd),
                    "approvalPolicy": "never",
                    "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
                    "environments": [],
                    "clientUserMessageId": str(uuid.uuid4()),
                }
                request = client.prepare_request("turn/start", params)
                intent = writer.write(
                    state.spec.job_id,
                    "turn-start-intent",
                    {
                        "thread_id": state.thread_id,
                        "wire_request_sha256": request.wire_sha256,
                        "turn_start_params_sha256": _sha256_value(params),
                        "prompt_sha256": _sha256_bytes(
                            state.spec.prompt.encode("utf-8")
                        ),
                        "requested_model": state.spec.model,
                        "requested_effort": state.spec.effort,
                        "sandbox": "read-only",
                        "approval_policy": "never",
                    },
                )
                _safe_lifecycle(
                    control,
                    state.dispatch,
                    "submitted",
                    receipt_sha256=intent["receipt_sha256"],
                )

                def record_presentation(state: JobState = state) -> None:
                    nonlocal presentation_state, presentation_warning
                    if presentation_file is None or project_name is None:
                        return
                    try:
                        project_id = host.project_id
                        dispatch_id = state.dispatch.dispatch_id
                        record_presentation_submission(
                            presentation_file,
                            project_id=project_id,
                            project_name=project_name,
                            dispatch_id=dispatch_id,
                            task_name=state.spec.task_name,
                            agent_name=state.spec.agent_name,
                            work_item_title=state.spec.work_item_title,
                            excluded_roots=(receipt_dir,),
                        )
                        if presentation_state != "degraded":
                            presentation_state = "recorded"
                    except Exception:
                        presentation_state = "degraded"
                        presentation_warning = "presentation-write-failed"

                rpc = client.submit(
                    request,
                    deadline=deadline,
                    after_write=record_presentation,
                )
                result = rpc.result
                turn_id, turn_status = _validate_turn_start_result(result)
                state.turn_id = turn_id
                response_metadata = {
                    "method": "turn/start",
                    "controller_request_id": request.request_id,
                    "controller_sequence": rpc.controller_sequence,
                    "thread_id": state.thread_id,
                    "turn_id": turn_id,
                    "turn_status": turn_status,
                    "configured_model": state.configured_model,
                    "configured_effort": state.configured_effort,
                }
                response_receipt = writer.write(
                    state.spec.job_id,
                    "turn-start-response",
                    {
                        "thread_id": state.thread_id,
                        "turn_id": turn_id,
                        "response_metadata_sha256": _sha256_value(
                            response_metadata
                        ),
                        "controller_request_id": request.request_id,
                        "controller_sequence": rpc.controller_sequence,
                        "acknowledged_turn_status": turn_status,
                        "configured_model": state.configured_model,
                        "configured_effort": state.configured_effort,
                    },
                )
                _safe_lifecycle(
                    control,
                    state.dispatch,
                    "acknowledged",
                    thread_id=state.thread_id,
                    turn_id=turn_id,
                    receipt_sha256=response_receipt["receipt_sha256"],
                    configured_model=state.configured_model,
                    configured_effort=state.configured_effort,
                )
                running_receipt = writer.write(
                    state.spec.job_id,
                    "turn-running",
                    {
                        "thread_id": state.thread_id,
                        "turn_id": turn_id,
                        "source_receipt_sha256": response_receipt["receipt_sha256"],
                    },
                )
                _safe_lifecycle(
                    control,
                    state.dispatch,
                    "running",
                    receipt_sha256=running_receipt["receipt_sha256"],
                )
                state.status = "running"
                _process_terminal(writer, control, state)

            while any(not state.terminal_processed for state in states):
                for state in states:
                    _process_terminal(writer, control, state)
                if all(state.terminal_processed for state in states):
                    break
                client.raise_if_failed()
                if shutdown_latch.requested:
                    raise RunnerError("shutdown-requested")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                with control.condition:
                    control.condition.wait(timeout=min(remaining, 0.25))

            if timed_out:
                _interrupt_active(
                    client,
                    writer,
                    states,
                    grace_seconds=interrupt_grace_seconds,
                )
                grace_deadline = time.monotonic() + max(
                    0.0, float(interrupt_grace_seconds)
                )
                while time.monotonic() < grace_deadline:
                    for state in states:
                        _process_terminal(writer, control, state)
                    if all(state.terminal_processed for state in states):
                        break
                    with control.condition:
                        control.condition.wait(
                            timeout=min(0.1, grace_deadline - time.monotonic())
                        )
                for state in states:
                    _process_terminal(writer, control, state)
                    if not state.terminal_processed:
                        _record_controller_terminal(
                            writer,
                            control,
                            state,
                            job_status="timeout",
                            lifecycle_state="control-lost",
                            reason="aggregate-wall-deadline",
                        )

            linger_deadline = time.monotonic() + float(linger_seconds)
            while time.monotonic() < linger_deadline:
                client.raise_if_failed()
                if shutdown_latch.requested:
                    raise RunnerError("shutdown-requested")
                time.sleep(min(0.1, linger_deadline - time.monotonic()))
        except RunnerError as exc:
            deadline_expired = time.monotonic() >= deadline or exc.code == "aggregate-timeout"
            timed_out = timed_out or deadline_expired
            failure_code = (
                shutdown_latch.failure_code
                if exc.code == "shutdown-requested" and shutdown_latch.requested
                else ("aggregate-timeout" if deadline_expired else exc.code)
            )
            if client is not None:
                _interrupt_active(
                    client,
                    writer,
                    states,
                    grace_seconds=min(0.25, interrupt_grace_seconds),
                )
                signal_grace_deadline = time.monotonic() + min(
                    0.25, max(0.0, interrupt_grace_seconds)
                )
                while time.monotonic() < signal_grace_deadline:
                    for state in states:
                        if control is not None:
                            _process_terminal(writer, control, state)
                    if all(state.terminal_processed for state in states):
                        break
                    with control.condition:
                        control.condition.wait(
                            timeout=min(
                                0.05,
                                signal_grace_deadline - time.monotonic(),
                            )
                        )
            for state in states:
                if control is not None:
                    _process_terminal(writer, control, state)
                if not state.terminal_processed:
                    _record_controller_terminal(
                        writer,
                        control,
                        state,
                        job_status=(
                            "timeout"
                            if deadline_expired and state.turn_id is not None
                            else (
                                "control_lost"
                                if state.turn_id is not None
                                else "not_started"
                            )
                        ),
                        lifecycle_state="control-lost",
                        reason=(
                            "aggregate-wall-deadline"
                            if deadline_expired
                            else (
                                "signal-latched"
                                if shutdown_latch.requested
                                else "runner-failure"
                            )
                        ),
                    )
    finally:
        if client is not None:
            process_reaped = client.close()
            process_group_closed = client.process_group_closed
        if host is not None:
            try:
                host_closed = bool(host.close(timeout_seconds=5.0))
            except Exception:
                host_closed = False
        shutdown_latch.restore()

    for state in states:
        if not state.terminal_processed:
            state.status = "not_started"
        outcome_payload = {
            "status": state.status,
            "requested_model": state.spec.model,
            "requested_effort": state.spec.effort,
            "configured_model": state.configured_model,
            "configured_effort": state.configured_effort,
            "thread_id_sha256": (
                _sha256_bytes(state.thread_id.encode("utf-8"))
                if state.thread_id is not None
                else None
            ),
            "turn_id_sha256": (
                _sha256_bytes(state.turn_id.encode("utf-8"))
                if state.turn_id is not None
                else None
            ),
            "turn_outcome_observed": state.terminal_event is not None,
        }
        receipt = writer.write(state.spec.job_id, "outcome", outcome_payload)
        state.outcome_receipt_sha256 = receipt["receipt_sha256"]

    if not process_reaped and failure_code is None:
        failure_code = "app-server-not-reaped"
    if not process_group_closed and failure_code is None:
        failure_code = "app-server-process-group-open"
    if not host_closed and failure_code is None:
        failure_code = "observability-close-incomplete"
    observability_state = (
        "close-incomplete"
        if not host_closed
        else (
            "degraded"
            if control is not None and control.observability_degraded
            else "closed"
        )
    )
    return {
        "version": 1,
        "execution_route": "standard-codex-owned-app-server",
        "runner_status": "failed" if failure_code is not None else "completed",
        "failure_code": failure_code,
        "timed_out": timed_out,
        "observability_state": observability_state,
        "app_server_reaped": process_reaped,
        "app_server_process_group_closed": process_group_closed,
        "approval_requests_denied": client.approval_denials if client else 0,
        "authorization_id": authorization.authorization_id,
        "authorization_file_sha256": authorization.file_sha256,
        "manifest_sha256": manifest.file_sha256,
        "observability_config_sha256": _sha256_bytes(config_bytes),
        "presentation_state": presentation_state,
        "presentation_warning": presentation_warning,
        "jobs": [_job_outcome(state) for state in states],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run fresh read-only Codex jobs with owned completion observation."
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--authorization-file", type=Path, required=True)
    parser.add_argument("--observability-config", type=Path, required=True)
    parser.add_argument("--receipt-dir", type=Path, required=True)
    parser.add_argument("--linger-seconds", type=float, default=0.0)
    parser.add_argument(
        "--presentation-file",
        type=Path,
        help="owner-only presentation registry outside the receipt directory",
    )
    parser.add_argument("--project-name")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        outcome = run_observed_jobs(
            manifest_path=args.manifest,
            authorization_file=args.authorization_file,
            observability_config=args.observability_config,
            receipt_dir=args.receipt_dir,
            linger_seconds=args.linger_seconds,
            presentation_file=args.presentation_file,
            project_name=args.project_name,
        )
    except RunnerError as exc:
        outcome = {
            "version": 1,
            "execution_route": "standard-codex-owned-app-server",
            "runner_status": "failed",
            "failure_code": exc.code,
            "jobs": [],
        }
    sys.stdout.write(json.dumps(outcome, sort_keys=True, separators=(",", ":")) + "\n")
    jobs = outcome.get("jobs")
    return (
        0
        if outcome.get("runner_status") == "completed"
        and isinstance(jobs, list)
        and jobs
        and all(job.get("status") == "completed" for job in jobs)
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
