"""Run documented user-service commands with synthetic profiles, without systemd."""

import configparser
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from build_release import build_release
from bob_fixtures import database, message, task

CHECKOUT = "/absolute/path/to/traceonaut"
MODES = {
    "codex-only-service": {"codex"},
    "bob-only-service": {"bob"},
    "both-sources-service": {"codex", "bob"},
}
HEALTH = {
    "codex": "cwo_codex_collector_source_available",
    "bob": "traceonaut_bob_collector_source_available",
}


def service_examples():
    guide = (ROOT / "references/operations/run-as-a-service.md").read_text()
    blocks = {}
    for marker in MODES:
        match = re.search(r"<!-- " + marker + r" -->\s*```ini\n(.*?)\n```", guide, re.S)
        if match is None:
            raise AssertionError("Missing documented service example: " + marker)
        blocks[marker] = match[1]
    base = blocks["codex-only-service"]
    return {
        marker: base if marker == "codex-only-service" else re.sub(
            r"^ExecStart=.*$", lambda _: block, base, flags=re.M)
        for marker, block in blocks.items()
    }


def parse_unit(text):
    unit = configparser.ConfigParser(interpolation=None)
    unit.optionxform = str
    unit.read_string(text)
    return unit


class ServiceExampleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith("TRACEONAUT_")}

    def test_examples_are_complete_systemd_commands(self):
        for marker, text in service_examples().items():
            with self.subTest(marker=marker):
                unit = parse_unit(text)
                self.assertEqual(unit.sections(), ["Unit", "Service", "Install"])
                self.assertEqual(unit["Service"]["Type"], "simple")
                self.assertEqual(unit["Service"]["WorkingDirectory"], CHECKOUT)
                self.assertEqual(unit["Service"]["Restart"], "on-failure")
                self.assertEqual(unit["Service"]["UMask"], "0077")
                self.assertEqual(unit["Install"]["WantedBy"], "default.target")
                self.assertEqual(len(re.findall(r"^ExecStart=", text, re.M)), 1)
                command = unit["Service"]["ExecStart"]
                self.assertNotIn("$", command)
                self.assertNotIn("TRACEONAUT_", text)
                self.assertNotIn("Environment", text)
                argv = shlex.split(command)
                self.assertEqual(argv[:2], ["/usr/bin/python3", CHECKOUT + "/scripts/collect_sessions.py"])
                self.assertEqual(len(argv[2:]) % 2, 0)
                options = dict(zip(argv[2::2], argv[3::2]))
                expected = {
                    "--session-state-dir": "%h/.local/share/traceonaut/session-state",
                    "--credential-file": "%h/.local/share/traceonaut/metrics.token",
                }
                for source in MODES[marker]:
                    expected["--" + source + "-home"] = "%h/." + source
                    option = "--snapshot-file" if source == "codex" else "--bob-snapshot-file"
                    filename = "sessions.json" if source == "codex" else "bob.json"
                    expected[option] = "%h/.local/share/traceonaut/" + filename
                self.assertEqual(options, expected)
                self.assertEqual(len(options) * 2, len(argv[2:]))

    @unittest.skipUnless(shutil.which("systemd-analyze"), "optional systemd-analyze is unavailable")
    def test_service_units_pass_systemd_verification(self):
        release = build_release("sessions", self.root / "releases")
        for marker, text in service_examples().items():
            with self.subTest(marker=marker):
                path = self.root / (marker + ".service")
                path.write_text(text.replace(CHECKOUT, str(release)).replace("%h", str(self.root)))
                result = subprocess.run(
                    ["systemd-analyze", "verify", "--man=no", str(path)],
                    env=self.env, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_documented_commands_serve_only_enabled_sources_with_authentication(self):
        release = build_release("sessions", self.root / "releases")
        opener = build_opener(ProxyHandler({}))
        for marker, text in service_examples().items():
            with self.subTest(marker=marker):
                fixture = self.root / marker
                fixture.mkdir(mode=0o700)
                output = fixture / ".local/share/traceonaut"
                output.mkdir(parents=True, mode=0o700)
                sources = MODES[marker]
                if "codex" in sources:
                    sessions = fixture / ".codex/sessions"
                    sessions.mkdir(parents=True)
                    now = datetime.now(timezone.utc).isoformat()
                    record = {"type": "session_meta", "timestamp": now,
                              "payload": {"id": str(uuid4()), "timestamp": now,
                                          "cwd": "/workspace/example"}}
                    (sessions / "rollout-synthetic.jsonl").write_text(json.dumps(record) + "\n")
                if "bob" in sources:
                    now = time.time()
                    database(fixture / ".bob/db/bob.db", [task(now=now)], [message(now=now)]).close()

                credential = output / "metrics.token"
                created = subprocess.run(
                    [sys.executable, str(ROOT / "scripts/create_metrics_token.py"),
                     "--credential-file", str(credential)],
                    env=self.env, capture_output=True, text=True, timeout=10)
                self.assertEqual(created.returncode, 0, created.stderr)
                self.assertEqual(created.stdout, "")
                token = credential.read_text().strip()
                self.assertEqual(credential.stat().st_mode & 0o777, 0o600)
                unit = parse_unit(text)
                argv = [arg.replace(CHECKOUT, str(release)).replace("%h", str(fixture))
                        for arg in shlex.split(unit["Service"]["ExecStart"])]
                cwd = unit["Service"]["WorkingDirectory"].replace(CHECKOUT, str(release))
                with socket.socket() as reserved:
                    reserved.bind(("127.0.0.1", 0))
                    port = reserved.getsockname()[1]
                # Keep each test listener separate from the documented default port.
                process = subprocess.Popen(argv + ["--port", str(port)], cwd=cwd,
                                           env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                url = f"http://127.0.0.1:{port}/metrics"
                try:
                    deadline = time.monotonic() + 12
                    while True:
                        self.assertIsNone(process.poll(), "documented service command exited before readiness")
                        try:
                            request = Request(url, headers={"Authorization": "Bearer " + token})
                            with opener.open(request, timeout=1) as response:
                                self.assertEqual(response.status, 200)
                                payload = response.read().decode()
                            if all(f"{HEALTH[source]}{{}} 1\n" in payload for source in sources):
                                break
                        except (OSError, URLError):
                            pass
                        self.assertLess(time.monotonic(), deadline, "documented service did not become ready")
                        time.sleep(.05)
                    for headers in ({}, {"Authorization": "Bearer invalid-test-credential"}):
                        with self.assertRaises(HTTPError) as rejected:
                            opener.open(Request(url, headers=headers), timeout=1)
                        self.assertEqual(rejected.exception.code, 401)
                        rejected.exception.close()
                    for source in sources:
                        prefix = "cwo_codex_" if source == "codex" else "traceonaut_bob_"
                        self.assertIn(prefix + "session_info{", payload)
                        filename = "sessions.json" if source == "codex" else "bob.json"
                        snapshot = json.loads((output / filename).read_text())
                        self.assertEqual(len(snapshot["sessions"]), 1)
                    for source in set(HEALTH) - sources:
                        prefix = "cwo_codex_" if source == "codex" else "traceonaut_bob_"
                        self.assertNotIn(prefix, payload)
                        self.assertFalse((fixture / ("." + source)).exists())
                        basename = "sessions" if source == "codex" else "bob"
                        self.assertFalse((output / (basename + ".json")).exists())
                        self.assertFalse((output / "session-state" / (basename + ".sqlite3")).exists())
                    self.assertTrue(token not in payload, "credential leaked into metrics")
                    self.assertNotIn("PRIVATE_SENTINEL", payload)
                finally:
                    if process.poll() is None:
                        process.terminate()
                    try:
                        stdout, stderr = process.communicate(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate(timeout=5)
                        raise
                self.assertTrue(token.encode() not in stdout + stderr, "credential leaked into process output")
                self.assertEqual(process.returncode, 0, stderr.decode())
                self.assertEqual(stdout, b"")
                self.assertEqual(stderr, b"")


if __name__ == "__main__":
    unittest.main()
