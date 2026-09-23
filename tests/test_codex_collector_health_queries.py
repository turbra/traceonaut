"""Execute the actual Beta health and retention semantics in isolated Prometheus."""
from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from traceonaut.observability_exporter import MetricsEndpoint, PrometheusQueryClient


@unittest.skipUnless(os.environ.get("CWO_TEST_PROMETHEUS_BINARY"), "separately verified Prometheus binary not supplied")
class CollectorHealthQueryTests(unittest.TestCase):
    def test_health_zero_stale_missing_and_expired_session_history(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
            root = Path(directory)
            now = int(time.time())
            endpoint = MetricsEndpoint("127.0.0.1", 0, b"synthetic-health-fixture")
            labels = 'project_id="fixture",session_id="work"'
            session = [
                f'cwo_codex_session_info{{{labels},model="before",effort="unknown"}} 1',
                f'cwo_codex_session_last_event_timestamp_seconds{{{labels}}} {now - 10}',
                f'cwo_codex_session_usage_state{{{labels}}} 1',
                f'cwo_codex_session_usage_tokens{{{labels},token_kind="total"}} 30',
            ]
            # Hand-written independent fixtures, not the production renderer.
            def publish(generation, *, scan=now, errors=0, sessions=True, health=True):
                lines = [f'fixture_generation {generation}']
                if health:
                    lines.extend([
                        'cwo_codex_collector_source_available 1',
                        f'cwo_codex_collector_scan_timestamp_seconds {scan}',
                        f'cwo_codex_collector_errors_total{{reason="invalid_record"}} {errors}',
                        'cwo_codex_collector_errors_total{reason="source_read"} 0',
                        'cwo_codex_collector_skipped_records_total{reason="untracked_prefix"} 7',
                        'cwo_codex_collector_skipped_records_total{reason="invalid_payload"} 2',
                        'cwo_codex_collector_skipped_records_total{reason="invalid_timestamp"} 0',
                        'cwo_codex_collector_session_export_retention_seconds 2592000',
                        'cwo_codex_collector_session_export_cap 1000',
                        f'cwo_codex_collector_session_export_exported_sessions {int(sessions)}',
                        f'cwo_codex_collector_session_export_expired_sessions {int(not sessions)}',
                        'cwo_codex_collector_session_export_cap_omitted_sessions 0',
                    ])
                if sessions:
                    lines.extend(session)
                with endpoint._lock:
                    endpoint._payload = ('\n'.join(lines) + '\n').encode()

            publish(1)
            endpoint.start()
            cleanup.callback(endpoint.close)
            credential = root / 'fixture.token'
            credential.write_bytes(b'synthetic-health-fixture')
            credential.chmod(0o600)
            with socket.socket() as reserved:
                reserved.bind(('127.0.0.1', 0))
                port = reserved.getsockname()[1]
            config = root / 'prometheus.json'
            config.write_text(json.dumps({
                'global': {'scrape_interval': '1s'},
                'scrape_configs': [{'job_name': 'fixture', 'scrape_timeout': '1s',
                    'authorization': {'type': 'Bearer', 'credentials_file': str(credential)},
                    'static_configs': [{'targets': [f'127.0.0.1:{endpoint.server.server_address[1]}']}]}],
            }))
            process = subprocess.Popen([os.environ['CWO_TEST_PROMETHEUS_BINARY'],
                '--config.file=' + str(config), '--storage.tsdb.path=' + str(root / 'tsdb'),
                f'--web.listen-address=127.0.0.1:{port}', '--log.level=error'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            def stop():
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
            cleanup.callback(stop)
            client = PrometheusQueryClient(f'http://127.0.0.1:{port}')
            dashboard = json.loads((ROOT / 'examples/observability/grafana-codex-beta-dashboard.json').read_text())
            panels = {p['id']: p for row in dashboard['panels'] for p in [row, *row.get('panels', [])]}
            def query(expression, at=None):
                return client.query(expression, time.time() if at is None else at)
            def until(expression):
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    try:
                        if query(expression):
                            return
                    except (OSError, ValueError):
                        pass
                    if process.poll() is not None:
                        self.fail('isolated Prometheus exited')
                    time.sleep(0.1)
                self.fail('fixture was not observed: ' + expression)
            def panel(identity, *, at=None, ref='A'):
                at = time.time() if at is None else at
                expression = next(t['expr'] for t in panels[identity]['targets'] if t['refId'] == ref)
                expression = expression.replace('$project', 'fixture').replace('$session', 'work')
                expression = expression.replace('$__from', str((now - 100) * 1000)).replace('$__to', str(at * 1000))
                return query(expression, at)
            def value(identity, **kwargs):
                rows = panel(identity, **kwargs)
                self.assertEqual(len(rows), 1)
                return float(rows[0]['value'][1])

            until('min(count_over_time(cwo_codex_collector_errors_total[1h])) >= 2')
            with self.subTest('known zero errors, bounded reasons and real scan age'):
                self.assertEqual(value(102), 0)
                self.assertLess(value(101), 20)
                self.assertGreaterEqual(value(101), 0)
                self.assertEqual({r['metric']['reason']: float(r['value'][1]) for r in panel(103)},
                                 dict(untracked_prefix=7, invalid_payload=2, invalid_timestamp=0))
                self.assertEqual(value(104, ref='A'), 30)
                self.assertEqual(value(104, ref='C'), 1)
                self.assertEqual(value(32), 30)
            historical_at = time.time()
            with self.subTest('a growing error counter and stale scan remain visible'):
                publish(2, scan=now - 3600, errors=5)
                until('fixture_generation == 2')
                self.assertGreater(value(102), 0)
                self.assertGreaterEqual(value(101), 3600)
            with self.subTest('never-completed scan is unavailable, not epoch age'):
                publish(3, scan=0, errors=5)
                until('fixture_generation == 3')
                self.assertEqual(panel(101), [])
            with self.subTest('retirement removes current series, not past samples or labels'):
                publish(4, errors=5, sessions=False)
                until('fixture_generation == 4')
                self.assertEqual(panel(32), [])
                self.assertEqual(value(32, at=historical_at), 30)
                old = query('cwo_codex_session_info', historical_at)
                self.assertEqual(old[0]['metric']['model'], 'before')
                self.assertEqual(query('cwo_codex_session_info'), [])
                self.assertEqual(value(104, ref='C'), 0)
                self.assertEqual(value(104, ref='D'), 1)
            with self.subTest('missing health has no synthetic zero fallback'):
                publish(5, sessions=False, health=False)
                until('fixture_generation == 5')
                self.assertEqual(panel(101), [])
                self.assertEqual(panel(103), [])
                # rate retains historical samples, as explicitly requested;
                # at a point without samples it is unavailable, not zero.
                self.assertEqual(panel(102, at=now - 7200), [])


if __name__ == '__main__':
    unittest.main()
