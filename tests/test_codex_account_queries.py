"""Verify both dashboards share account panels tested against isolated Prometheus."""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from traceonaut.observability_exporter import MetricsEndpoint, PrometheusQueryClient


class AccountPanelContractTests(unittest.TestCase):
    def test_stable_and_beta_share_account_scope_and_missing_data_handling(self) -> None:
        contracts = []
        for name in ("grafana-sessions-dashboard.json", "grafana-codex-beta-dashboard.json"):
            dashboard = json.loads(
                (ROOT / "examples" / "observability" / name).read_text(encoding="utf-8")
            )
            panels = {
                panel["id"]: {key: value for key, value in panel.items() if key != "gridPos"}
                for panel in dashboard["panels"]
                if panel["id"] in range(90, 95)
            }
            self.assertEqual(set(panels), set(range(90, 95)), name)
            contracts.append(panels)
        # The Prometheus cases below therefore qualify the actual expressions
        # in both dashboards, including zero, missing, stale and failed reads.
        self.assertEqual(contracts[0], contracts[1])


@unittest.skipUnless(
    os.environ.get("CWO_TEST_PROMETHEUS_BINARY"),
    "separately verified Prometheus binary not supplied",
)
class AccountQueryIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        root = Path(cls.tmp.name)
        cls.credential = b"synthetic-account-fixture"
        cls.precollection_time = time.time() - 60
        cls.marker = 0

        cls.primary = MetricsEndpoint("127.0.0.1", 0, cls.credential)
        cls.secondary = MetricsEndpoint("127.0.0.1", 0, cls.credential)
        for endpoint in (cls.primary, cls.secondary):
            endpoint._payload = b"# account fixture intentionally empty\n"
            endpoint.start()
            cls.addClassCleanup(endpoint.close)

        credential_file = root / "scrape.token"
        credential_file.write_bytes(cls.credential)
        credential_file.chmod(0o600)
        targets = [
            f"127.0.0.1:{endpoint.server.server_address[1]}"
            for endpoint in (cls.primary, cls.secondary)
        ]
        config = root / "prometheus.json"
        config.write_text(
            json.dumps(
                {
                    "global": {"scrape_interval": "250ms"},
                    "scrape_configs": [
                        {
                            "job_name": "account-fixture",
                            "scrape_interval": "250ms",
                            "scrape_timeout": "200ms",
                            "authorization": {
                                "type": "Bearer",
                                "credentials_file": str(credential_file),
                            },
                            "static_configs": [{"targets": targets}],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        cls.process = subprocess.Popen(
            [
                os.environ["CWO_TEST_PROMETHEUS_BINARY"],
                "--config.file=" + str(config),
                "--storage.tsdb.path=" + str(root / "tsdb"),
                f"--web.listen-address=127.0.0.1:{port}",
                "--storage.tsdb.retention.time=1h",
                "--log.level=error",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        cls.addClassCleanup(cls._stop_prometheus)
        cls.address = f"http://127.0.0.1:{port}"
        cls.client = PrometheusQueryClient(cls.address)

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if cls.process.poll() is not None:
                raise AssertionError("temporary Prometheus exited")
            try:
                with urlopen(cls.address + "/-/ready", timeout=1) as response:
                    if response.status == 200:
                        rows = cls.client.query(
                            'count(up{job="account-fixture"} == 1)', time.time()
                        )
                        if rows and float(rows[0]["value"][1]) == 2:
                            break
            except (OSError, ValueError):
                pass
            time.sleep(0.05)
        else:
            raise AssertionError("temporary Prometheus did not scrape both fixtures")

        dashboard = json.loads(
            (ROOT / "examples/observability/grafana-codex-beta-dashboard.json").read_text(
                encoding="utf-8"
            )
        )
        panels = {panel["id"]: panel for panel in dashboard["panels"]}
        cls.expressions = {
            (panel_id, target["refId"]): target["expr"]
            for panel_id in range(91, 95)
            for target in panels[panel_id]["targets"]
        }

    @classmethod
    def _stop_prometheus(cls) -> None:
        if cls.process.poll() is None:
            cls.process.terminate()
            try:
                cls.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cls.process.kill()
                cls.process.wait(timeout=5)

    def setUp(self) -> None:
        self._set_payload(self.primary, None)
        self._set_payload(self.secondary, None)
        self._wait_for(
            "cwo_codex_account_available",
            lambda rows: rows == [],
            "account fixtures did not become absent",
        )

    @classmethod
    def _set_payload(cls, endpoint: MetricsEndpoint, payload: bytes | None) -> None:
        with endpoint._lock:
            endpoint._payload = payload or b"# account fixture intentionally empty\n"

    @classmethod
    def _wait_for(cls, expression, predicate, message):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            rows = cls.client.query(expression, time.time())
            if predicate(rows):
                return rows
            if cls.process.poll() is not None:
                raise AssertionError("temporary Prometheus exited")
            time.sleep(0.05)
        raise AssertionError(message)

    @classmethod
    def _payload(
        cls,
        *,
        available: int = 1,
        last_success: float | None = None,
        count: int | None = 2,
        primary: dict[str, int] | None = None,
        secondary: dict[str, int] | None = None,
    ) -> tuple[bytes, int]:
        cls.marker += 1
        lines = [
            f"cwo_codex_account_available {available}",
            f"cwo_codex_account_last_attempt_timestamp_seconds {cls.marker}",
        ]
        if last_success is not None:
            lines.append(
                f"cwo_codex_account_last_success_timestamp_seconds {last_success}"
            )
        if count is not None:
            lines.append(f"cwo_codex_account_reset_credits_available {count}")
        for name, window in (("primary", primary), ("secondary", secondary)):
            if window is None:
                continue
            labels = f'{{window="{name}"}}'
            for field, metric in (
                ("used", "window_used_percent"),
                ("minutes", "window_minutes"),
                ("reset", "window_reset_timestamp_seconds"),
            ):
                if field in window:
                    lines.append(
                        f"cwo_codex_account_{metric}{labels} {window[field]}"
                    )
        return ("\n".join(lines) + "\n").encode("utf-8"), cls.marker

    def _publish(self, endpoint=None, **values) -> float:
        endpoint = endpoint or self.primary
        payload, marker = self._payload(**values)
        self._set_payload(endpoint, payload)
        rows = self._wait_for(
            f"cwo_codex_account_last_attempt_timestamp_seconds == {marker}",
            lambda result: len(result) == 1,
            "Prometheus did not store the account fixture",
        )
        return float(rows[0]["value"][0])

    def _query(self, panel_id: int, ref: str = "A", *, when=None):
        return self.client.query(
            self.expressions[(panel_id, ref)], time.time() if when is None else when
        )

    def _value(self, panel_id: int, ref: str = "A", *, when=None) -> float:
        rows = self._query(panel_id, ref, when=when)
        self.assertEqual(len(rows), 1, (panel_id, ref, rows))
        return float(rows[0]["value"][1])

    def _assert_allowance_empty(self, *, when=None) -> None:
        for panel_id, refs in ((91, "A"), (92, "A"), (93, "AB")):
            for ref in refs:
                self.assertEqual(self._query(panel_id, ref, when=when), [])

    def test_zero_count_is_observed_but_successful_null_marks_old_series_stale(self):
        now = time.time()
        self._publish(last_success=now, count=0)
        self.assertEqual(self._value(91), 0)

        self._publish(last_success=time.time(), count=2)
        self.assertEqual(self._value(91), 2)
        self._publish(last_success=time.time(), count=None)
        self._wait_for(
            "cwo_codex_account_reset_credits_available",
            lambda rows: rows == [],
            "omitted reset count did not become stale after a successful scrape",
        )
        self.assertEqual(self._query(91), [])
        self.assertEqual(self._value(94), 2)

    def test_failed_read_is_unavailable_even_with_a_recent_success_time(self):
        self._publish(available=0, last_success=time.time(), count=None)
        self._assert_allowance_empty()
        self.assertEqual(self._value(94), 0)

    def test_stale_and_future_clock_never_look_current(self):
        self._publish(last_success=time.time() - 150, count=2)
        self._assert_allowance_empty()
        self.assertEqual(self._value(94), 1)

        self._publish(last_success=time.time() + 60, count=2)
        self._assert_allowance_empty()
        self.assertEqual(self._value(94), 1)

    def test_weekly_window_must_be_present_exact_and_unique(self):
        cases = (
            {},
            {"primary": {"used": 4, "minutes": 300, "reset": 2000000000}},
            {
                "primary": {"used": 4, "minutes": 10080, "reset": 2000000000},
                "secondary": {"used": 8, "minutes": 10080, "reset": 2000000100},
            },
        )
        for windows in cases:
            with self.subTest(windows=windows):
                self._publish(last_success=time.time(), **windows)
                self.assertEqual(self._query(92), [])
                self.assertEqual(self._query(93, "A"), [])
                self.assertEqual(self._query(93, "B"), [])

    def test_secondary_weekly_window_supplies_one_pair_and_overrun_clamps(self):
        now = time.time()
        weekly_reset = int(now) + 600
        self._publish(
            last_success=now,
            primary={"used": 10, "minutes": 300, "reset": int(now) + 300},
            secondary={"used": 4, "minutes": 10080, "reset": weekly_reset},
        )
        self.assertEqual(self._value(92), 96)
        self.assertEqual(self._value(93, "A"), weekly_reset * 1000)
        countdown = self._value(93, "B")
        self.assertGreater(countdown, 590)
        self.assertLessEqual(countdown, 600)

        self._publish(
            last_success=time.time(),
            primary={"used": 104, "minutes": 10080, "reset": weekly_reset},
        )
        self.assertEqual(self._value(92), 0)

    def test_duplicate_exporters_suppress_every_account_panel(self):
        now = time.time()
        window = {"used": 4, "minutes": 10080, "reset": int(now) + 600}
        self._publish(last_success=now, primary=window)
        self.assertEqual(self._value(91), 2)
        self._publish(endpoint=self.secondary, last_success=time.time(), primary=window)
        self._wait_for(
            "count(cwo_codex_account_available) == 2",
            lambda rows: len(rows) == 1,
            "Prometheus did not observe both account exporters",
        )
        self._assert_allowance_empty()
        for ref in ("A", "B"):
            self.assertEqual(self._query(94, ref), [])

    def test_reset_countdown_uses_selected_evaluation_time_and_precollection_is_empty(self):
        now = time.time()
        reset = int(now) + 600
        scraped_at = self._publish(
            last_success=now,
            primary={"used": 4, "minutes": 10080, "reset": reset},
        )
        self.assertEqual(self._value(93, "A", when=scraped_at), reset * 1000)
        self.assertAlmostEqual(
            self._value(93, "B", when=scraped_at), reset - scraped_at, places=3
        )
        self.assertAlmostEqual(
            self._value(93, "B", when=scraped_at + 30),
            reset - scraped_at - 30,
            places=3,
        )

        self._assert_allowance_empty(when=self.precollection_time)
        for ref in ("A", "B"):
            self.assertEqual(self._query(94, ref, when=self.precollection_time), [])


if __name__ == "__main__":
    unittest.main()
