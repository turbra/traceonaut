"""Optional real Prometheus validation, with no existing services or inference.

Set CWO_TEST_PROMETHEUS_BINARY to a separately verified local binary. Nothing is
downloaded or installed by this test. The temporary server binds loopback only.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import unittest
from unittest import mock
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from traceonaut.observability_exporter import (
    MetricsEndpoint,
    PrometheusQueryClient,
    PublicationConfirmer,
)
from render_observability_dashboard import walk_panels
import test_observability_ledger_adapter as fixtures


@unittest.skipUnless(
    os.environ.get("CWO_TEST_PROMETHEUS_BINARY"),
    "separately verified Prometheus binary not supplied",
)
class StoredSampleIntegrationTests(unittest.TestCase):
    def test_exact_publication_late_update_and_dashboard_queries(self):
        fixture = fixtures.ObservabilityLedgerAdapterTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        ledger = fixture.ledger
        ledger.record_submission(fixture.observation())
        adapter = fixture.adapter()
        fixture.bind(adapter)
        adapter.try_submit(fixture.completion(17))
        missing_usage = fixture.completion(0)
        missing_usage["params"]["responseId"] = "response-null-usage"
        missing_usage["params"]["usage"] = None
        adapter.try_submit(missing_usage)
        adapter.drain_once()
        timing = fixture.observation()["timing"]
        timing.update(terminal_seconds=420, elapsed_seconds=410, elapsed_state=1)
        ledger.record_lifecycle(
            fixture.dispatch_id,
            lifecycle_state="completed",
            timing=timing,
            receipt_sha256=fixtures.sha("terminal"),
        )
        credential = b"test-fixture-credential-not-a-real-secret"
        endpoint = MetricsEndpoint("127.0.0.1", 0, credential)
        endpoint.update(ledger.snapshot())
        endpoint.start()
        self.addCleanup(endpoint.close)
        token_path = fixture.root / "scrape.token"
        token_path.write_bytes(credential)
        token_path.chmod(0o600)
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        instance = "127.0.0.1:" + str(endpoint.server.server_address[1])
        config = fixture.root / "prometheus.json"
        config.write_text(
            json.dumps(
                {
                    "global": {"scrape_interval": "1s"},
                    "scrape_configs": [
                        {
                            "job_name": "cwo-integration",
                            "scrape_timeout": "1s",
                            "authorization": {
                                "type": "Bearer",
                                "credentials_file": str(token_path),
                            },
                            "static_configs": [{"targets": [instance]}],
                        }
                    ],
                }
            )
        )
        process = subprocess.Popen(
            [
                os.environ["CWO_TEST_PROMETHEUS_BINARY"],
                "--config.file=" + str(config),
                "--storage.tsdb.path=" + str(fixture.root / "tsdb"),
                "--web.listen-address=127.0.0.1:" + str(port),
                "--storage.tsdb.retention.time=1h",
                "--log.level=error",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        def stop():
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

        self.addCleanup(stop)
        address = "http://127.0.0.1:" + str(port)
        deadline = time.monotonic() + 15
        while True:
            self.assertIsNone(process.poll(), "temporary Prometheus exited")
            try:
                with urlopen(address + "/-/ready", timeout=1) as response:
                    if response.status == 200:
                        break
            except OSError:
                if time.monotonic() >= deadline:
                    self.fail("temporary Prometheus did not become ready")
                time.sleep(0.05)
        client = PrometheusQueryClient(address)
        confirmer = PublicationConfirmer(
            ledger, client, job="cwo-integration", instance=instance
        )

        def confirm_current():
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                result = confirmer.run_once()
                if result["confirmed"] == 1:
                    return
                time.sleep(0.1)
            self.fail("exact stored snapshot was not confirmed")

        # Simulate process interruption between durable staging and confirmation.
        # A later scrape has a different original timestamp, but recovery must
        # confirm the already-staged exact snapshot rather than conflict forever.
        deadline = time.monotonic() + 12
        with mock.patch.object(
            ledger,
            "confirm_publication",
            side_effect=RuntimeError("fixture interruption"),
        ):
            while not ledger.pending_publication_manifests():
                confirmer.run_once()
                if time.monotonic() >= deadline:
                    self.fail("fixture never staged a stored snapshot")
                time.sleep(0.1)
        time.sleep(1.1)
        confirmer = PublicationConfirmer(
            ledger, client, job="cwo-integration", instance=instance
        )
        confirm_current()
        self.assertFalse(fixture.dispatch_snapshot()["publication"]["pending"])
        endpoint.update(ledger.snapshot())
        with endpoint._lock:
            self.assertNotIn(b"cwo_dispatch_info{", endpoint._payload)

        late = copy.deepcopy(fixture.completion(5))
        late["params"]["responseId"] = "late-response"
        adapter.try_submit(late)
        adapter.drain_once()
        self.assertTrue(fixture.dispatch_snapshot()["publication"]["pending"])
        self.assertEqual(
            fixture.dispatch_snapshot()["aggregate"]["completed_cycles"], 3
        )
        endpoint.update(ledger.snapshot())
        confirm_current()
        dashboard = json.loads(
            (ROOT / "examples/observability/cwo-observed-dispatches.json").read_text()
        )
        query_count = 0
        for panel in walk_panels(dashboard["panels"]):
            for target in panel.get("targets", []):
                expression = (
                    target["expr"]
                    .replace("$project", fixture.project_id)
                    .replace("$dispatch", fixture.dispatch_id)
                    .replace("$__range", "5m")
                )
                client.query(expression, time.time())
                query_count += 1
        self.assertGreater(query_count, 20)

        # Retired terminal history proves zero active agents. An unknown
        # project has no such evidence and must remain unavailable.
        active_expression = next(
            panel["targets"][0]["expr"]
            for panel in walk_panels(dashboard["panels"])
            if panel["title"] == "Active distinct agents"
        ).replace("$__range", "5m")
        active = client.query(
            active_expression.replace("$project", fixture.project_id), time.time()
        )
        self.assertEqual(len(active), 1)
        self.assertEqual(float(active[0]["value"][1]), 0.0)
        self.assertEqual(
            client.query(
                active_expression.replace("$project", "unobserved-project"),
                time.time(),
            ),
            [],
        )

        work = next(p for p in dashboard["panels"] if p["title"] == "Work and results")
        work_queries = {
            target["refId"]: target["expr"].replace("$project", fixture.project_id)
            .replace("$dispatch", fixture.dispatch_id).replace("$__range", "5m")
            for target in work["targets"]
        }
        # Independent expected values: two reported usages of 18 and 6 tokens,
        # one response without usage, and 410 seconds of measured wall time.
        for reference, expected in {"B": 3, "C": 410, "D": 3, "E": 24}.items():
            rows = client.query(work_queries[reference], time.time())
            self.assertEqual(len(rows), 1, reference)
            self.assertEqual(rows[0]["metric"]["dispatch_id"], fixture.dispatch_id)
            self.assertEqual(float(rows[0]["value"][1]), expected, reference)

        def panel_query(title, reference="A", *, project=None):
            panel = next(p for p in dashboard["panels"] if p["title"] == title)
            expr = next(t["expr"] for t in panel["targets"] if t["refId"] == reference)
            return expr.replace("$project", project or fixture.project_id).replace(
                "$dispatch", fixture.dispatch_id).replace("$__range", "5m")

        for title, expected in (("Input tokens reported", 22), ("Output tokens reported", 2),
                                ("Completed model responses", 3), ("Observed agent activity", 0)):
            rows = client.query(panel_query(title), time.time())
            self.assertEqual(len(rows), 1, title)
            self.assertEqual(float(rows[0]["value"][1]), expected, title)
            self.assertEqual(client.query(panel_query(title, project="unobserved-project"), time.time()), [])
        # History must not invent zero activity before this project's first
        # stored observation. All four outcome slices have a known cohort.
        self.assertEqual(client.query(panel_query("Observed agent activity"), time.time() - 600), [])
        self.assertEqual(client.query(panel_query("Observed agent activity"), time.time() + 60), [])
        for reference, expected in {"A": 1, "B": 0, "C": 0, "D": 0}.items():
            rows = client.query(panel_query("Task outcomes", reference), time.time())
            if expected:
                self.assertEqual(float(rows[0]["value"][1]), expected, reference)
            else:
                self.assertEqual(rows, [], reference)
            self.assertEqual(client.query(panel_query("Task outcomes", reference, project="unobserved-project"), time.time()), [])

        # A later contradiction must hide previously stored totals in the new
        # overview too, while keeping the task and measured duration visible.
        adapter.try_submit(fixture.completion(99))
        adapter.drain_once()
        self.assertEqual(fixture.dispatch_snapshot()["aggregate"]["coverage_state"], 3)
        endpoint.update(ledger.snapshot())
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if not client.query(work_queries["E"], time.time()):
                break
            time.sleep(0.1)
        else:
            self.fail("overview retained tokens after an accounting conflict")
        self.assertEqual(client.query(work_queries["D"], time.time()), [])
        self.assertEqual(len(client.query(work_queries["A"], time.time())), 1)
        self.assertEqual(float(client.query(work_queries["C"], time.time())[0]["value"][1]), 410)
        for title in ("Input tokens reported", "Output tokens reported", "Completed model responses"):
            self.assertEqual(client.query(panel_query(title), time.time()), [], title)


if __name__ == "__main__":
    unittest.main()
