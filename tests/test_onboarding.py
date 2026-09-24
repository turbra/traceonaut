"""Exercise the documented credentials and file-collector flow on synthetic data."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from build_release import build_release
import collect_codex_sessions as session_cli
from collect_codex_sessions import SessionMetricsEndpoint
from traceonaut.observability_exporter import read_credential


def documented_block(marker, guide_name="getting-started.mdx"):
    guide = (ROOT / "references" / guide_name).read_text()
    return re.search(r"<!-- " + re.escape(marker) + r" -->(?:\s*\*/})?\s*```bash\n(.*?)\n```", guide, re.S)[1]


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / ".codex"
        (self.source / "sessions").mkdir(parents=True, mode=0o700)
        self.source.chmod(0o700)
        self.presentation = self.root / ".local/share/traceonaut"
        self.state = self.presentation / "session-state"
        self.snapshot = self.presentation / "sessions.json"
        self.token = self.presentation / "metrics.token"
        self.env = {**os.environ, "HOME": str(self.root),
                    "TRACEONAUT_SOURCE_HOME": str(self.source),
                    "TRACEONAUT_LISTEN_ADDRESS": "127.0.0.1",
                    "TRACEONAUT_DATA_DIR": str(self.presentation),
                    "TRACEONAUT_METRICS_CREDENTIAL": str(self.token)}
        prepared = subprocess.run(["bash", "-eu", "-c", documented_block("setup-paths")],
                                  env=self.env, cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        self.assertEqual(self.presentation.stat().st_mode & 0o777, 0o700)
        self.sid = str(uuid4())
        stamp = datetime.now(timezone.utc).isoformat()
        records = [
            {"timestamp": stamp, "type": "session_meta", "payload": {
                "id": self.sid, "cwd": "/workspace/example", "timestamp": stamp, "name": "Synthetic work"}},
            {"timestamp": stamp, "type": "token_usage_record", "payload": {
                "thread_id": self.sid, "turn_id": "turn-1", "session_id": "connection",
                "root_turn_id": "root-turn", "response_id": "response-1",
                "usage": {"input_tokens": 20, "cached_input_tokens": 5, "cache_write_input_tokens": 0,
                          "output_tokens": 10, "reasoning_output_tokens": 2, "total_tokens": 30}}},
        ]
        self.rollout = self.source / "sessions" / ("rollout-" + self.sid + ".jsonl")
        self.rollout.write_text("".join(json.dumps(record) + "\n" for record in records))
        self.original = hashlib.sha256(self.rollout.read_bytes()).hexdigest()
        self.release = build_release("sessions", self.root / "releases")

    def credential(self):
        result = subprocess.run(["bash", "-eu", "-c", documented_block("credential-create")],
                                env=self.env, cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.token.stat().st_mode & 0o777, 0o600)
        return read_credential(self.token)

    def command(self):
        return [sys.executable, "-B", str(self.release / "scripts/collect_codex_sessions.py"),
                "--codex-home", str(self.source), "--session-state-dir", str(self.state),
                "--snapshot-file", str(self.snapshot)]

    def assert_source_unchanged(self):
        self.assertEqual(hashlib.sha256(self.rollout.read_bytes()).hexdigest(), self.original)
        self.assertEqual(sorted(p.relative_to(self.source).as_posix() for p in self.source.rglob("*") if p.is_file()),
                         [self.rollout.relative_to(self.source).as_posix()])

    def test_once_requires_available_source_and_reports_backlog(self):
        result = subprocess.run(self.command() + ["--once"], cwd=self.root, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"sessions": 1, "pending_files": 0, "source_available": 1})
        snapshot = json.loads(self.snapshot.read_text())
        self.assertEqual(snapshot["sessions"][0]["usage"]["total"], 30)
        self.assertEqual(snapshot["sessions"][0]["session_id"], self.sid)
        self.assertEqual(self.snapshot.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        self.assert_source_unchanged()

    def test_documented_once_checks_source_without_a_credential(self):
        result = subprocess.run(["bash", "-eu", "-c", documented_block("collect-once")],
                                env=self.env, cwd=ROOT, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"sessions": 1, "pending_files": 0, "source_available": 1})
        self.assertFalse(self.token.exists())
        self.assert_source_unchanged()

    def test_documented_credential_creation_does_not_overwrite(self):
        value = self.credential()
        result = subprocess.run(["bash", "-eu", "-c", documented_block("credential-create")],
                                env=self.env, cwd=ROOT, capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(read_credential(self.token), value)
        self.assertNotIn(value, result.stdout + result.stderr)

    def test_cli_bind_validation_precedes_state_creation(self):
        for host in ("workstation.example", "0.0.0.0", "::", "224.0.0.1", "ff02::1"):
            with self.subTest(host=host):
                result = subprocess.run(self.command() + ["--once", "--host", host],
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("metrics host", result.stderr)
                self.assertFalse(self.state.exists())
                self.assertFalse(self.snapshot.exists())
        self.assert_source_unchanged()

    def test_cli_default_and_explicit_host_reach_the_endpoint(self):
        credential = self.credential()
        for host in (None, "192.0.2.10", "2001:db8::10"):
            with self.subTest(host=host), \
                    patch.object(session_cli, "SessionCollector"), \
                    patch.object(session_cli, "SessionMetricsEndpoint") as endpoint, \
                    patch.object(session_cli.threading, "Event") as stopping, \
                    patch.object(session_cli.signal, "signal"), \
                    patch.object(session_cli.os, "umask"):
                stopping.return_value.is_set.return_value = True
                argv = self.command()[3:] + ["--credential-file", str(self.token)]
                if host is not None:
                    argv += ["--host", host]
                self.assertEqual(session_cli.main(argv), 0)
                endpoint.assert_called_once_with(host or "127.0.0.1", 9464, credential, allow_remote=True)
                endpoint.return_value.start.assert_called_once_with()
                endpoint.return_value.close.assert_called_once_with()

    def test_credential_negatives_match_documented_constraints(self):
        value = self.credential()
        for mode in (0o644, 0o640, 0o400):
            with self.subTest(mode=mode):
                self.token.chmod(mode)
                with self.assertRaisesRegex(ValueError, "owner-only regular file"):
                    read_credential(self.token)
        self.token.chmod(0o600)
        for data in (b"short", b"abcdefgh\nijklmnopqrst", b"abcd efghijklmnopq", b"x" * 4097):
            with self.subTest(length=len(data)):
                self.token.write_bytes(data)
                with self.assertRaisesRegex(ValueError, "encoding or length"):
                    read_credential(self.token)
        self.token.write_bytes(value)
        link = self.root / "symlink.token"
        link.symlink_to(self.token)
        with self.assertRaises(OSError):
            read_credential(link)
        self.root.chmod(0o770)
        try:
            with self.assertRaisesRegex(ValueError, "writable credential ancestor"):
                read_credential(self.token)
        finally:
            self.root.chmod(0o700)

    def test_endpoint_before_payload_is_503_not_ready_or_dead(self):
        value = self.credential()
        endpoint = SessionMetricsEndpoint("127.0.0.1", 0, value)
        self.addCleanup(endpoint.close)
        endpoint.start()
        url = f"http://127.0.0.1:{endpoint.server.server_port}/metrics"
        with self.assertRaises(HTTPError) as failure:
            build_opener(ProxyHandler({})).open(Request(url, headers={"Authorization": "Bearer " + value.decode()}), timeout=4)
        self.assertEqual(failure.exception.code, 503)
        failure.exception.close()

    def test_continuous_cli_authenticated_client_and_invalid_credentials(self):
        value = self.credential()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        # Run the literal quick-start collector command, with only its port changed.
        block = documented_block("run-collector")
        self.assertEqual(block.count("--port 9464"), 1)
        process = subprocess.Popen(["bash", "-eu", "-c", "exec " + block.replace("--port 9464", f"--port {port}")],
                                   env=self.env, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            opener = build_opener(ProxyHandler({}))
            url = f"http://127.0.0.1:{port}/metrics"
            request = Request(url, headers={"Authorization": "Bearer " + value.decode()})
            deadline = time.monotonic() + 15
            while True:
                self.assertIsNone(process.poll(), "collector exited before readiness")
                try:
                    with opener.open(request, timeout=2) as response:
                        payload = response.read()
                    break
                except HTTPError as error:
                    error.close()
                    if error.code != 503:
                        raise
                except URLError:
                    pass
                self.assertLess(time.monotonic(), deadline, "synthetic collector did not become ready")
                time.sleep(0.05)
            self.assertIn(b"cwo_codex_collector_source_available{} 1", payload.splitlines())
            self.assertIn(self.sid.encode(), payload)
            for headers in ({}, {"Authorization": "Bearer synthetic-invalid-token"}):
                with self.assertRaises(HTTPError) as failure:
                    opener.open(Request(url, headers=headers), timeout=4)
                self.assertEqual(failure.exception.code, 401)
                failure.exception.close()
            # Only the test port differs from the literal first-use client.
            block = documented_block("metrics-check", "operations/troubleshooting.md")
            self.assertEqual(block.count("127.0.0.1:9464"), 1)
            result = subprocess.run(["bash", "-eu", "-c", block.replace("127.0.0.1:9464", f"127.0.0.1:{port}")],
                                    env=self.env, cwd=ROOT, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(b"HTTP 200", result.stdout)
            self.assertNotIn(value, result.stdout + result.stderr)
            self.assert_source_unchanged()
        finally:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)

    def test_documented_dashboard_commands_keep_import_datasource_selection(self):
        result = subprocess.run(self.command() + ["--once"], cwd=ROOT,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        for guide, marker, output, uid in (
            ("getting-started.mdx", "render-beta", "work-overview.json", "cwo-codex-beta"),
            ("dashboards/unified.md", "render-unified", "unified.json", "cwo-codex-unified"),
            ("dashboards/all-sessions.md", "render-stable", "all-sessions.json", "cwo-supervisor-observability-v1"),
        ):
            with self.subTest(dashboard=output):
                result = subprocess.run(["bash", "-eu", "-c", documented_block(marker, guide)],
                                        env=self.env, cwd=ROOT, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                dashboard = json.loads((self.presentation / output).read_text())
                self.assertEqual(dashboard["uid"], uid)
                self.assertEqual([item["name"] for item in dashboard["__inputs"]], ["DS_PROMETHEUS"])
                self.assertIn('"${DS_PROMETHEUS}"', json.dumps(dashboard))
                work = next(item for item in dashboard["templating"]["list"] if item["name"] == "session")
                self.assertIn(self.sid, work["query"])
                self.assertIn("Synthetic work", json.dumps(dashboard))
        self.assert_source_unchanged()


if __name__ == "__main__":
    unittest.main()
