from __future__ import annotations

from contextlib import closing
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import uuid
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_observed_codex as observed
from traceonaut.observability_ledger import DATABASE_NAME, ObservabilityLedger


REAL_POPEN = subprocess.Popen
MODEL = "gpt-5.6-sol"
EFFORT = "xhigh"
PROMPT_SECRET = "APPROVED_PROMPT_MUST_NOT_ENTER_RECEIPTS"
RAW_SECRET = "RAW_REASONING_MUST_NOT_ENTER_RECEIPTS"


FAKE_SERVER = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

mode = os.environ.get("FAKE_APP_MODE", "success")
capture_path = Path(os.environ["FAKE_CAPTURE"])
thread_ids = [
    "123e4567-e89b-42d3-a456-426614174001",
    "123e4567-e89b-42d3-a456-426614174003",
]
turn_ids = [
    "123e4567-e89b-42d3-a456-426614174002",
    "123e4567-e89b-42d3-a456-426614174004",
]
thread_index = 0
turns = {}
pending_terminals = []

if mode == "stubborn-child":
    import signal
    import subprocess
    import time
    child_ready = Path(os.environ["FAKE_CHILD_PID"] + ".ready")
    child = subprocess.Popen([
        sys.executable,
        "-c",
        "import pathlib,signal,sys,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); pathlib.Path(sys.argv[1]).write_text('ready'); time.sleep(60)",
        str(child_ready),
    ])
    Path(os.environ["FAKE_CHILD_PID"]).write_text(str(child.pid), encoding="ascii")
    while not child_ready.exists():
        time.sleep(0.01)

def emit(value):
    sys.stdout.buffer.write(json.dumps(value, separators=(",", ":")).encode() + b"\n")
    sys.stdout.buffer.flush()

def capture(value):
    with capture_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, separators=(",", ":")) + "\n")
        stream.flush()

while True:
    line = sys.stdin.buffer.readline()
    if not line:
        break
    message = json.loads(line)
    capture(message)
    method = message.get("method")
    if method == "initialize":
        response_id = 999 if mode == "wrong-id" else message["id"]
        emit({
            "id": response_id,
            "result": {
                "userAgent": "cwo-observed-runner/0.154.0 (test)",
                "codexHome": "/tmp/fake-codex-home",
                "platformFamily": "unix",
                "platformOs": "linux",
            },
        })
        if mode == "unexpected-message":
            emit([])
    elif method == "thread/start":
        params = message["params"]
        thread_id = thread_ids[thread_index]
        turn_id = turn_ids[thread_index]
        turns[thread_id] = turn_id
        thread_index += 1
        emit({
            "id": message["id"],
            "result": {
                "model": params["model"],
                "reasoningEffort": params["config"]["model_reasoning_effort"],
                "cwd": params["cwd"],
                "approvalPolicy": params["approvalPolicy"],
                "approvalsReviewer": "user",
                "sandbox": {"type": "readOnly", "networkAccess": False},
                "modelProvider": "openai",
                "thread": {
                    "id": thread_id,
                    "cwd": params["cwd"],
                    "ephemeral": True,
                    "turns": [],
                    "model": params["model"],
                    "reasoningEffort": params["config"]["model_reasoning_effort"],
                },
            },
        })
    elif method == "turn/start":
        thread_id = message["params"]["threadId"]
        turn_id = turns[thread_id]
        if mode in {"success", "two-job-success", "stubborn-child"}:
            approval_id = "approval-" + turn_id[-1]
            emit({
                "id": approval_id,
                "method": "item/commandExecution/requestApproval",
                "params": {"threadId": thread_id, "turnId": turn_id},
            })
            denial = json.loads(sys.stdin.buffer.readline())
            capture(denial)
            emit({
                "method": "rawResponseItem/added",
                "params": {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "item": {"type": "reasoning", "text": "RAW_REASONING_MUST_NOT_ENTER_RECEIPTS"},
                },
            })
            emit({
                "method": "rawResponse/completed",
                "params": {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "responseId": "response-" + turn_id[-1],
                    "usage": None,
                },
            })
        emit({
            "id": message["id"],
            "result": {
                "turn": {"id": turn_id, "status": "inProgress", "items": []},
            },
        })
        if mode in {"success", "stubborn-child"}:
            pending_terminals.append((thread_id, turn_id))
        elif mode == "two-job-success":
            pending_terminals.append((thread_id, turn_id))
            if len(pending_terminals) < 2:
                continue
        if pending_terminals and mode in {"success", "two-job-success", "stubborn-child"}:
            for completed_thread_id, completed_turn_id in pending_terminals:
                emit({
                "method": "turn/completed",
                "params": {
                    "threadId": completed_thread_id,
                    "turn": {
                        "id": completed_turn_id,
                        "status": "completed",
                        "items": [{"type": "reasoning", "text": "RAW_REASONING_MUST_NOT_ENTER_RECEIPTS"}],
                    },
                },
            })
            pending_terminals.clear()
    elif method == "turn/interrupt":
        emit({"id": message["id"], "result": {}})
'''


class FakeDispatch:
    def __init__(self) -> None:
        self.dispatch_id = str(uuid.uuid4())
        self.events: list[tuple[str, dict]] = []

    def submitted(self, **values):
        self.events.append(("submitted", values))

    def acknowledged(self, **values):
        self.events.append(("acknowledged", values))

    def running(self, **values):
        self.events.append(("running", values))

    def terminal(self, **values):
        self.events.append(("terminal", values))


class FakeObserver:
    def __init__(self) -> None:
        self.preparations: list[dict] = []
        self.dispatches: list[FakeDispatch] = []
        self.notifications: list[tuple[str, int]] = []
        self.disconnects: list[bool] = []
        self.collector_faults = 0

    def prepare_dispatch(self, **values):
        self.preparations.append(values)
        dispatch = FakeDispatch()
        self.dispatches.append(dispatch)
        return dispatch

    def observe_notification(self, message, *, sequence):
        # Deliberately retain only the method and sequence, matching the
        # runner's content-free control path.
        self.notifications.append((message["method"], sequence))

    def source_disconnected(self, *, expected=False):
        self.disconnects.append(expected)

    def collector_fault(self):
        self.collector_faults += 1


class FakeHost:
    def __init__(self) -> None:
        self.project_id = str(uuid.uuid4())
        self.observer = FakeObserver()
        self.close_calls: list[float] = []

    def close(self, *, timeout_seconds=2.0):
        self.close_calls.append(timeout_seconds)
        return True


class RecordingPopen:
    def __init__(self, script: Path) -> None:
        self.script = script
        self.argv: list[list[str]] = []
        self.processes: list[subprocess.Popen] = []

    def __call__(self, args, **kwargs):
        self.argv.append(list(args))
        process = REAL_POPEN([sys.executable, str(self.script)], **kwargs)
        self.processes.append(process)
        return process


class ObservedCodexRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.receipts = self.root / "receipts"
        self.receipts.mkdir(mode=0o700)
        self.config = self.root / "observability.json"
        self._write_private(self.config, {"test": True})
        self.server = self.root / "fake_app_server.py"
        self.server.write_text(textwrap.dedent(FAKE_SERVER), encoding="utf-8")
        self.server.chmod(0o700)
        self.capture = self.root / "protocol-capture.jsonl"
        self.job_id = str(uuid.uuid4())

    @staticmethod
    def _write_private(path: Path, value: dict) -> None:
        path.write_text(json.dumps(value), encoding="utf-8")
        path.chmod(0o600)

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.bind(("127.0.0.1", 0))
            return int(server.getsockname()[1])

    def _manifest(
        self,
        *,
        timeout: float = 3.0,
        prompt: str = PROMPT_SECRET,
        presentation_context: dict[str, str] | None = None,
    ) -> Path:
        path = self.root / "manifest.json"
        job = {
            "job_id": self.job_id,
            "cwd": str(ROOT),
            "model": MODEL,
            "effort": EFFORT,
            "prompt": prompt,
        }
        job.update(presentation_context or {})
        self._write_private(
            path,
            {
                "version": 1,
                "jobs": [job],
                "timeout_seconds": timeout,
            },
        )
        return path

    def _authorization(
        self,
        manifest: Path,
        *,
        config: Path | None = None,
        max_concurrency: int = 1,
        max_wall_seconds: float = 3.0,
        max_linger_seconds: float = 0.0,
    ) -> Path:
        manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
        config_path = config or self.config
        path = self.root / "authorization.json"
        self._write_private(
            path,
            {
                "version": 1,
                "authorization_id": str(uuid.uuid4()),
                "route": "standard_codex",
                "authority_kind": "explicit-user-authorization",
                "authority_scope": "standard-codex-observability",
                "authority_decision_sha256": hashlib.sha256(
                    b"test-live-integration-decision"
                ).hexdigest(),
                "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
                "observability_config_sha256": hashlib.sha256(
                    config_path.read_bytes()
                ).hexdigest(),
                "allowed_jobs": [
                    {
                        "job_id": job["job_id"],
                        "cwd": job["cwd"],
                        "model": job["model"],
                        "effort": job["effort"],
                        "prompt_sha256": hashlib.sha256(
                            job["prompt"].encode("utf-8")
                        ).hexdigest(),
                    }
                    for job in manifest_payload["jobs"]
                ],
                "limits": {
                    "max_concurrency": max_concurrency,
                    "max_wall_seconds": max_wall_seconds,
                    "max_linger_seconds": max_linger_seconds,
                },
                "sandbox": "read-only",
                "network_access": False,
                "native_attestation_claimed": False,
                "native_policy_override": False,
            },
        )
        return path

    def _run(
        self,
        mode: str,
        *,
        timeout: float = 3.0,
        grace: float = 0.1,
        extra_environment: dict[str, str] | None = None,
        presentation_file: Path | None = None,
        project_name: str | None = None,
        presentation_context: dict[str, str] | None = None,
    ):
        host = FakeHost()
        popen = RecordingPopen(self.server)
        environment = {
            "FAKE_APP_MODE": mode,
            "FAKE_CAPTURE": str(self.capture),
        }
        environment.update(extra_environment or {})
        manifest = self._manifest(
            timeout=timeout, presentation_context=presentation_context
        )
        authorization = self._authorization(
            manifest,
            max_wall_seconds=max(3.0, timeout),
        )
        with mock.patch.dict(os.environ, environment):
            outcome = observed.run_observed_jobs(
                manifest_path=manifest,
                authorization_file=authorization,
                observability_config=self.config,
                receipt_dir=self.receipts,
                presentation_file=presentation_file,
                project_name=project_name,
                popen_factory=popen,
                host_factory=lambda _path: host,
                interrupt_grace_seconds=grace,
            )
        return outcome, host, popen

    def _receipt_records(self) -> list[dict]:
        return [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(self.receipts.glob("*.json"))
        ]

    def test_pre_ack_raw_completion_binds_to_receipts_without_raw_persistence(self):
        outcome, host, popen = self._run("success")

        self.assertEqual(outcome["runner_status"], "completed")
        self.assertEqual(outcome["jobs"][0]["status"], "completed")
        self.assertEqual(outcome["approval_requests_denied"], 1)
        self.assertEqual(outcome["presentation_state"], "not-configured")
        self.assertIsNone(outcome["presentation_warning"])
        self.assertTrue(outcome["app_server_reaped"])
        self.assertTrue(outcome["app_server_process_group_closed"])
        self.assertEqual(
            popen.argv,
            [["codex", "app-server", "--listen", "stdio://"]],
        )
        self.assertIsNotNone(popen.processes[0].poll())

        preparation = host.observer.preparations[0]
        self.assertEqual(preparation["requested_executor"], "standard_codex")
        self.assertEqual(preparation["requested_model"], MODEL)
        self.assertEqual(preparation["requested_effort"], EFFORT)
        lifecycle = host.observer.dispatches[0].events
        self.assertEqual(
            [name for name, _values in lifecycle],
            ["submitted", "acknowledged", "running", "terminal"],
        )
        acknowledgement = lifecycle[1][1]
        self.assertEqual(acknowledgement["configured_model"], MODEL)
        self.assertEqual(acknowledgement["configured_effort"], EFFORT)
        self.assertEqual(lifecycle[-1][1]["state"], "completed")
        methods = [method for method, _sequence in host.observer.notifications]
        self.assertLess(
            methods.index("rawResponse/completed"), methods.index("turn/completed")
        )
        self.assertEqual(host.observer.disconnects, [True])

        receipts = self._receipt_records()
        previous = None
        for receipt in receipts:
            supplied = receipt.pop("receipt_sha256")
            self.assertEqual(supplied, observed._sha256_value(receipt))
            self.assertEqual(receipt["previous_receipt_sha256"], previous)
            previous = supplied
            receipt["receipt_sha256"] = supplied
        stages = [record["stage"] for record in receipts]
        self.assertEqual(
            stages,
            [
                "route-authorization",
                "workload",
                "thread-start-intent",
                "thread-start-response",
                "turn-start-intent",
                "turn-start-response",
                "turn-running",
                "turn-terminal",
                "outcome",
            ],
        )
        persisted = b"".join(
            path.read_bytes() for path in sorted(self.receipts.glob("*.json"))
        )
        self.assertNotIn(PROMPT_SECRET.encode(), persisted)
        self.assertNotIn(RAW_SECRET.encode(), persisted)
        self.assertNotIn(PROMPT_SECRET, json.dumps(outcome))
        self.assertNotIn(RAW_SECRET, json.dumps(outcome))

        captured = [
            json.loads(line)
            for line in self.capture.read_text(encoding="utf-8").splitlines()
        ]
        turn_request = next(
            value for value in captured if value.get("method") == "turn/start"
        )
        turn_intent = next(
            value for value in receipts if value["stage"] == "turn-start-intent"
        )
        self.assertEqual(
            turn_intent["wire_request_sha256"], observed._sha256_value(turn_request)
        )
        turn_response = next(
            value for value in receipts if value["stage"] == "turn-start-response"
        )
        turn_response_metadata = {
            "method": "turn/start",
            "controller_request_id": turn_response["controller_request_id"],
            "controller_sequence": turn_response["controller_sequence"],
            "thread_id": turn_response["thread_id"],
            "turn_id": turn_response["turn_id"],
            "turn_status": turn_response["acknowledged_turn_status"],
            "configured_model": turn_response["configured_model"],
            "configured_effort": turn_response["configured_effort"],
        }
        self.assertEqual(
            turn_response["response_metadata_sha256"],
            observed._sha256_value(turn_response_metadata),
        )
        terminal = next(
            value for value in receipts if value["stage"] == "turn-terminal"
        )
        terminal_metadata = {
            "method": "turn/completed",
            "controller_sequence": terminal["controller_sequence"],
            "thread_id": terminal["thread_id"],
            "turn_id": terminal["turn_id"],
            "turn_status": terminal["turn_status"],
        }
        self.assertEqual(
            terminal["terminal_metadata_sha256"],
            observed._sha256_value(terminal_metadata),
        )
        self.assertNotIn("response_sha256", turn_response)
        self.assertNotIn("turn_completed_message_sha256", terminal)
        denial = next(
            value
            for value in captured
            if isinstance(value.get("id"), str)
            and value["id"].startswith("approval-")
        )
        self.assertEqual(denial["result"], {"decision": "cancel"})

    def test_explicit_presentation_context_is_written_only_after_submission(self):
        presentation_dir = self.root / "presentation"
        presentation_dir.mkdir(mode=0o700)
        presentation_file = presentation_dir / "registry.json"
        context = {
            "task_name": "Qualification job one",
            "agent_name": "Standard Codex Sol xhigh",
            "work_item_title": "Human context qualification",
        }

        outcome, host, _popen = self._run(
            "success",
            presentation_file=presentation_file,
            project_name="Supervisor Dispatch Observability",
            presentation_context=context,
        )

        registry = json.loads(presentation_file.read_text(encoding="utf-8"))
        dispatch_id = host.observer.dispatches[0].dispatch_id
        self.assertEqual(outcome["presentation_state"], "recorded")
        self.assertIsNone(outcome["presentation_warning"])
        self.assertEqual(
            registry["projects"][host.project_id]["name"],
            "Supervisor Dispatch Observability",
        )
        self.assertEqual(registry["dispatches"][dispatch_id], context)
        self.assertNotIn(PROMPT_SECRET, json.dumps(registry))

    def test_no_presentation_context_is_written_without_submission(self):
        presentation_dir = self.root / "presentation-no-submit"
        presentation_dir.mkdir(mode=0o700)
        presentation_file = presentation_dir / "registry.json"

        outcome, _host, _popen = self._run(
            "wrong-id",
            presentation_file=presentation_file,
            project_name="Supervisor Dispatch Observability",
            presentation_context={
                "task_name": "Qualification job one",
                "agent_name": "Standard Codex Sol xhigh",
            },
        )

        self.assertEqual(outcome["presentation_state"], "not-recorded")
        self.assertFalse(presentation_file.exists())

    def test_presentation_write_failure_does_not_stop_model_work(self):
        outcome, _host, _popen = self._run(
            "success",
            presentation_file=self.receipts / "presentation.json",
            project_name="Supervisor Dispatch Observability",
            presentation_context={
                "task_name": "Qualification job one",
                "agent_name": "Standard Codex Sol xhigh",
            },
        )

        self.assertEqual(outcome["runner_status"], "completed")
        self.assertEqual(outcome["jobs"][0]["status"], "completed")
        self.assertEqual(outcome["presentation_state"], "degraded")
        self.assertEqual(
            outcome["presentation_warning"], "presentation-write-failed"
        )
        self.assertFalse((self.receipts / "presentation.json").exists())

    def test_presentation_lock_contention_does_not_stall_model_work(self):
        presentation_dir = self.root / "presentation-contended"
        presentation_dir.mkdir(mode=0o700)
        presentation_file = presentation_dir / "registry.json"
        lock_file = presentation_dir / ".registry.json.lock"
        lock_descriptor = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o600)
        os.fchmod(lock_descriptor, 0o600)
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        result: dict[str, object] = {}

        def run_job() -> None:
            try:
                result["value"] = self._run(
                    "success",
                    presentation_file=presentation_file,
                    project_name="Supervisor Dispatch Observability",
                    presentation_context={
                        "task_name": "Qualification job one",
                        "agent_name": "Standard Codex Sol xhigh",
                    },
                )
            except BaseException as exc:
                result["error"] = exc

        runner = threading.Thread(target=run_job, daemon=True)
        try:
            runner.start()
            runner.join(timeout=2.0)
            stalled = runner.is_alive()
        finally:
            fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            os.close(lock_descriptor)
        runner.join(timeout=5.0)

        self.assertFalse(stalled, "presentation lock contention stalled model work")
        self.assertNotIn("error", result)
        outcome, _host, _popen = result["value"]
        self.assertEqual(outcome["runner_status"], "completed")
        self.assertEqual(outcome["jobs"][0]["status"], "completed")
        self.assertEqual(outcome["presentation_state"], "degraded")
        self.assertEqual(
            outcome["presentation_warning"], "presentation-write-failed"
        )
        self.assertFalse(presentation_file.exists())

    def test_manifest_presentation_fields_are_paired_and_bounded(self):
        cases = (
            ({"task_name": "Task"}, "manifest-job-fields-invalid"),
            (
                {"task_name": "Task", "agent_name": "Agent\nname"},
                "manifest-job-agent-name-invalid",
            ),
            (
                {"task_name": "Task", "agent_name": "Agent\ud800name"},
                "manifest-job-agent-name-invalid",
            ),
            (
                {
                    "task_name": "Task",
                    "agent_name": "Agent",
                    "work_item_title": "x" * 257,
                },
                "manifest-job-work-item-title-invalid",
            ),
        )
        for context, expected in cases:
            with self.subTest(context=context):
                manifest = self._manifest(presentation_context=context)
                with self.assertRaisesRegex(observed.RunnerError, expected):
                    observed._load_manifest(manifest)

    def test_timeout_interrupts_turn_and_reaps_owned_process(self):
        outcome, host, popen = self._run("timeout", timeout=0.25, grace=0.1)

        self.assertEqual(outcome["runner_status"], "completed")
        self.assertTrue(outcome["timed_out"])
        self.assertEqual(outcome["jobs"][0]["status"], "timeout")
        self.assertTrue(outcome["app_server_reaped"])
        self.assertTrue(outcome["app_server_process_group_closed"])
        self.assertIsNotNone(popen.processes[0].poll())
        captured = [
            json.loads(line)
            for line in self.capture.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            len([value for value in captured if value.get("method") == "turn/interrupt"]),
            1,
        )
        lifecycle = host.observer.dispatches[0].events
        self.assertEqual(lifecycle[-1][0], "terminal")
        self.assertEqual(lifecycle[-1][1]["state"], "control-lost")
        controller_terminal = next(
            value
            for value in self._receipt_records()
            if value["stage"] == "controller-terminal"
        )
        self.assertFalse(controller_terminal["turn_outcome_observed"])
        self.assertEqual(controller_terminal["reason"], "aggregate-wall-deadline")

    def test_wrong_response_id_and_unexpected_message_fail_closed(self):
        for mode, expected in (
            ("wrong-id", "app-server-response-id-unexpected"),
            ("unexpected-message", "app-server-message-invalid"),
        ):
            with self.subTest(mode=mode):
                # Use a fresh private receipt directory for each protocol fault.
                if any(self.receipts.iterdir()):
                    for path in self.receipts.iterdir():
                        path.unlink()
                self.capture.unlink(missing_ok=True)
                outcome, _host, popen = self._run(mode)
                self.assertEqual(outcome["runner_status"], "failed")
                self.assertEqual(outcome["failure_code"], expected)
                self.assertEqual(outcome["jobs"][0]["status"], "not_started")
                self.assertTrue(outcome["app_server_reaped"])
                self.assertIsNotNone(popen.processes[0].poll())

    def test_sigterm_latches_interrupts_restores_handler_and_closes_group(self):
        prior = signal.getsignal(signal.SIGTERM)

        def signal_after_turn_start() -> None:
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if self.capture.exists() and '"method":"turn/start"' in self.capture.read_text(
                    encoding="utf-8"
                ):
                    os.kill(os.getpid(), signal.SIGTERM)
                    return
                time.sleep(0.01)

        sender = threading.Thread(target=signal_after_turn_start, daemon=True)
        sender.start()
        outcome, _host, popen = self._run("timeout", timeout=3.0, grace=0.1)
        sender.join(timeout=2)

        self.assertEqual(outcome["runner_status"], "failed")
        self.assertEqual(outcome["failure_code"], "signal-sigterm")
        self.assertEqual(outcome["jobs"][0]["status"], "control_lost")
        self.assertTrue(outcome["app_server_reaped"])
        self.assertTrue(outcome["app_server_process_group_closed"])
        self.assertEqual(signal.getsignal(signal.SIGTERM), prior)
        self.assertIsNotNone(popen.processes[0].poll())
        captured = [
            json.loads(line)
            for line in self.capture.read_text(encoding="utf-8").splitlines()
        ]
        self.assertTrue(
            any(value.get("method") == "turn/interrupt" for value in captured)
        )

    def test_close_forces_owned_process_group_after_term_grace(self):
        child_pid_file = self.root / "stubborn-child.pid"
        real_killpg = os.killpg
        signals: list[int] = []

        def recording_killpg(process_group: int, signum: int) -> None:
            signals.append(signum)
            real_killpg(process_group, signum)

        with mock.patch.object(observed.os, "killpg", side_effect=recording_killpg):
            outcome, _host, popen = self._run(
                "stubborn-child",
                extra_environment={"FAKE_CHILD_PID": str(child_pid_file)},
            )

        self.assertEqual(outcome["jobs"][0]["status"], "completed")
        self.assertTrue(outcome["app_server_reaped"])
        self.assertTrue(outcome["app_server_process_group_closed"])
        self.assertIn(signal.SIGTERM, signals)
        self.assertIn(signal.SIGKILL, signals)
        self.assertIsNotNone(popen.processes[0].poll())
        child_pid = int(child_pid_file.read_text(encoding="ascii"))
        proc_stat = Path(f"/proc/{child_pid}/stat")
        if proc_stat.exists():
            state = proc_stat.read_text(encoding="ascii").split()[2]
            self.assertEqual(state, "Z")

    def test_authorization_mismatches_fail_before_host_or_process(self):
        manifest = self._manifest(timeout=3.0)
        cases = {
            "prompt": ("authorization-job-mismatch", lambda value: value["allowed_jobs"][0].__setitem__("prompt_sha256", "0" * 64)),
            "cwd": ("authorization-job-mismatch", lambda value: value["allowed_jobs"][0].__setitem__("cwd", str(self.root))),
            "model": ("authorization-job-mismatch", lambda value: value["allowed_jobs"][0].__setitem__("model", "different-model")),
            "timeout": ("authorization-wall-limit-exceeded", lambda value: value["limits"].__setitem__("max_wall_seconds", 1)),
            "config": ("authorization-observability-config-mismatch", lambda value: value.__setitem__("observability_config_sha256", "0" * 64)),
        }
        for name, (expected, mutate) in cases.items():
            with self.subTest(name=name):
                authorization = self._authorization(manifest)
                value = json.loads(authorization.read_text(encoding="utf-8"))
                mutate(value)
                self._write_private(authorization, value)
                host_factory = mock.Mock(side_effect=AssertionError("must not open"))
                popen_factory = mock.Mock(side_effect=AssertionError("must not spawn"))
                with self.assertRaisesRegex(observed.RunnerError, expected):
                    observed.run_observed_jobs(
                        manifest_path=manifest,
                        authorization_file=authorization,
                        observability_config=self.config,
                        receipt_dir=self.receipts,
                        popen_factory=popen_factory,
                        host_factory=host_factory,
                    )
                host_factory.assert_not_called()
                popen_factory.assert_not_called()

    def test_two_job_pre_ack_completions_reach_real_durable_ledger(self):
        state_dir = self.root / "real-state"
        credential = self.root / "metrics.token"
        credential.write_text("test-observability-token", encoding="ascii")
        credential.chmod(0o600)
        real_config = self.root / "real-observability.json"
        project_id = str(uuid.uuid4())
        self._write_private(
            real_config,
            {
                "state_dir": str(state_dir),
                "registration": {
                    "project_id": project_id,
                    "owning_principal_id": os.geteuid(),
                    "runtime_owner_id": str(uuid.uuid4()),
                    "state": "enabled",
                    "generation": 1,
                    "resource_limits": {
                        "max_frame_bytes": 8 * 1024 * 1024,
                        "max_json_depth": 32,
                        "max_normalized_record_bytes": 16 * 1024,
                        "max_pending_records": 32,
                        "max_queue_records": 64,
                        "pending_binding_deadline_seconds": 60,
                        "max_registrations": 4,
                        "max_disk_bytes": 32 * 1024 * 1024,
                    },
                },
                "executor_vocabulary": ["standard_codex"],
                "model_vocabulary": [MODEL],
                "effort_vocabulary": [EFFORT],
                "source_compatibility_sha256": hashlib.sha256(
                    b"qualified-test-source"
                ).hexdigest(),
                "metrics": {
                    "host": "127.0.0.1",
                    "port": self._free_port(),
                    "credential_file": str(credential),
                },
            },
        )
        job_ids = [self.job_id, str(uuid.uuid4())]
        prompts = [PROMPT_SECRET + "-one", PROMPT_SECRET + "-two"]
        manifest = self.root / "two-job-manifest.json"
        self._write_private(
            manifest,
            {
                "version": 1,
                "jobs": [
                    {
                        "job_id": job_id,
                        "cwd": str(ROOT),
                        "model": MODEL,
                        "effort": EFFORT,
                        "prompt": prompt,
                    }
                    for job_id, prompt in zip(job_ids, prompts, strict=True)
                ],
                "timeout_seconds": 5,
            },
        )
        authorization = self._authorization(
            manifest,
            config=real_config,
            max_concurrency=2,
            max_wall_seconds=5,
        )
        popen = RecordingPopen(self.server)
        with mock.patch.dict(
            os.environ,
            {
                "FAKE_APP_MODE": "two-job-success",
                "FAKE_CAPTURE": str(self.capture),
            },
        ):
            outcome = observed.run_observed_jobs(
                manifest_path=manifest,
                authorization_file=authorization,
                observability_config=real_config,
                receipt_dir=self.receipts,
                popen_factory=popen,
            )

        self.assertEqual(outcome["runner_status"], "completed")
        self.assertEqual([job["status"] for job in outcome["jobs"]], ["completed"] * 2)
        self.assertEqual(outcome["approval_requests_denied"], 2)
        self.assertTrue(outcome["app_server_reaped"])
        self.assertTrue(outcome["app_server_process_group_closed"])

        snapshot = ObservabilityLedger.read_snapshot(state_dir)
        project = snapshot["projects"][0]
        self.assertEqual(project["registration"]["project_id"], project_id)
        self.assertEqual(project["health"]["connection_state"], "disconnected")
        dispatches = project["dispatches"]
        self.assertEqual(len(dispatches), 2)
        receipts = self._receipt_records()
        by_job: dict[str, list[dict]] = {job_id: [] for job_id in job_ids}
        for receipt in receipts:
            by_job[receipt["job_id"]].append(receipt)
        workload_to_job = {
            next(
                record["workload_sha256"]
                for record in records
                if record["stage"] == "workload"
            ): job_id
            for job_id, records in by_job.items()
        }
        for dispatch in dispatches:
            observation = dispatch["observation"]
            job_id = workload_to_job[observation["packet_sha256"]]
            records = by_job[job_id]
            acknowledgement = next(
                record for record in records if record["stage"] == "turn-start-response"
            )
            self.assertEqual(observation["requested_executor"], "standard_codex")
            self.assertEqual(observation["lifecycle_state"], "completed")
            self.assertEqual(dispatch["aggregate"]["completed_cycles"], 1)
            self.assertEqual(len(dispatch["cycles"]), 1)
            self.assertIsNotNone(dispatch["binding"])
            self.assertEqual(
                dispatch["binding"]["acknowledgement_receipt_sha256"],
                acknowledgement["receipt_sha256"],
            )
            self.assertEqual(dispatch["binding"]["configured_model"], MODEL)
            self.assertEqual(dispatch["binding"]["configured_effort"], EFFORT)

        lifecycle_receipts = {
            record["receipt_sha256"]
            for record in receipts
            if record["stage"]
            in {
                "turn-start-intent",
                "turn-start-response",
                "turn-running",
                "turn-terminal",
            }
        }
        with closing(sqlite3.connect(state_dir / DATABASE_NAME)) as database:
            durable_lifecycle = {
                row[0]
                for row in database.execute(
                    "SELECT receipt_sha256 FROM lifecycle_receipts"
                ).fetchall()
            }
        self.assertEqual(durable_lifecycle, lifecycle_receipts)

        second_response_sequence = max(
            record["controller_sequence"]
            for record in receipts
            if record["stage"] == "turn-start-response"
        )
        self.assertTrue(
            all(
                record["controller_sequence"] > second_response_sequence
                for record in receipts
                if record["stage"] == "turn-terminal"
            )
        )
        protected_bytes = b"".join(
            path.read_bytes() for path in state_dir.rglob("*") if path.is_file()
        )
        protected_bytes += b"".join(
            path.read_bytes() for path in self.receipts.iterdir()
        )
        self.assertNotIn(RAW_SECRET.encode(), protected_bytes)
        for prompt in prompts:
            self.assertNotIn(prompt.encode(), protected_bytes)

    def test_manifest_rejects_more_than_two_jobs_before_spawning(self):
        jobs = []
        for _ in range(3):
            jobs.append(
                {
                    "job_id": str(uuid.uuid4()),
                    "cwd": str(ROOT),
                    "model": MODEL,
                    "effort": EFFORT,
                    "prompt": "bounded test",
                }
            )
        manifest = self.root / "too-many.json"
        self._write_private(
            manifest,
            {"version": 1, "jobs": jobs, "timeout_seconds": 3},
        )
        with self.assertRaisesRegex(observed.RunnerError, "manifest-job-count-invalid"):
            observed.run_observed_jobs(
                manifest_path=manifest,
                authorization_file=self.config,
                observability_config=self.config,
                receipt_dir=self.receipts,
                popen_factory=mock.Mock(side_effect=AssertionError("must not spawn")),
                host_factory=mock.Mock(side_effect=AssertionError("must not open")),
            )


if __name__ == "__main__":
    unittest.main()
