"""Bob generations continue in bounded slices without waiting between slices."""

import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import collect_codex_sessions as cli
from traceonaut import bob_session_telemetry as bob
from bob_fixtures import database, message, task


class Clock:
    def __init__(self):
        self.elapsed = 0.0

    def monotonic(self):
        return self.elapsed

    def time(self):
        return 1_800_000_000 + self.elapsed

    def advance(self, seconds):
        self.elapsed += seconds


class Stop:
    """An Event whose timed waits consume simulated time instead of wall time."""

    def __init__(self, clock, on_wait):
        self.clock, self.on_wait = clock, on_wait
        self.stopped = False
        self.waits = []

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, seconds):
        self.waits.append(seconds)
        self.clock.advance(seconds)
        self.on_wait()
        return self.stopped


class BobSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.args = SimpleNamespace(
            bob_home=self.root / 'bob', codex_home=self.root / 'codex',
            session_state_dir=self.root / 'state',
            bob_snapshot_file=self.root / 'bob.json', snapshot_file=self.root / 'codex.json',
            session_retention_seconds=2592000, session_export_cap=1000,
            cwo_sessions=False, state_dir=None, account_snapshot_file=None,
            once=False, poll_seconds=5)

    def fixture(self, count, *, content='Synthetic message'):
        rows = (message(str(index), data={'role': 'assistant', 'content': content})
                for index in range(count))
        writer = database(self.args.bob_home / 'db/bob.db', [task()], rows)
        self.addCleanup(writer.close)
        return writer

    def run_worker(self, *, row_seconds=.0001, after_scan=None, on_wait=None):
        self.clock = Clock()
        self.scans, self.rows_per_scan, self.scan_seconds, self.waited_snapshots = [], [], [], []

        def waited():
            self.waited_snapshots.append(self.worker.snapshot)
            if on_wait:
                on_wait(self.worker.snapshot)
            elif not self.worker.snapshot['pending']:
                self.stopping.set()

        self.stopping = Stop(self.clock, waited)
        self.worker = cli.SourceWorker('bob', self.args, self.stopping)
        collector = cli.BobCollector

        def create(*args, **kwargs):
            self.reader = collector(*args, **kwargs)
            scan, read_row = self.reader.scan, self.reader._read_row
            rows_read = 0

            def timed_row():
                nonlocal rows_read
                row = read_row()
                if row is not None:
                    rows_read += 1
                    self.clock.advance(row_seconds)
                return row

            def scan_slice():
                nonlocal rows_read
                rows_read = 0
                started = self.clock.monotonic()
                value = scan()
                self.scans.append(value)
                self.rows_per_scan.append(rows_read)
                self.scan_seconds.append(self.clock.monotonic() - started)
                if after_scan:
                    after_scan(value)
                if len(self.scans) >= 100:
                    self.stopping.set()
                return value

            self.reader._read_row = timed_row
            self.reader.scan = scan_slice
            return self.reader

        with patch.object(cli, 'BobCollector', create), \
                patch.object(cli, 'time', self.clock), patch.object(bob, 'time', self.clock):
            self.worker.run()
        self.assertFalse(self.worker.failed)
        self.assertIsNone(self.reader.source)
        self.assertIsNone(self.reader.db)
        self.assertIsNone(self.reader.lock)

    def assert_publication(self, responses):
        value = self.worker.snapshot
        self.assertEqual((value['source_available'], value['collection_complete'],
                          value['pending'], value['limit_reached']), (1, 1, 0, 0))
        self.assertEqual(value['indexed_sessions'], 1)
        self.assertEqual(value['sessions'][0]['responses'], responses)
        self.assertEqual(value['sessions'][0]['usage']['total'], 120)
        self.assertEqual(json.loads(self.args.bob_snapshot_file.read_text()), value)
        self.assertEqual(len(self.stopping.waits), 1)
        self.assertGreater(self.stopping.waits[0], 4)
        self.assertTrue(all(not value['pending'] for value in self.waited_snapshots))
        self.assertTrue(all(not value['sessions'] for value in self.scans[:-1]))

    def test_thirty_thousand_messages_publish_without_polling_between_row_slices(self):
        self.fixture(30_000)
        self.run_worker()
        self.assert_publication(30_000)
        self.assertEqual(len(self.scans), 16)
        self.assertEqual(max(self.rows_per_scan), bob.MAX_ROWS_PER_SCAN)
        self.assertGreater(sum(self.scan_seconds), 3)
        self.assertLess(sum(self.scan_seconds), bob.MAX_TRANSACTION_SECONDS)

    def test_large_messages_publish_without_polling_between_byte_slices(self):
        self.fixture(60, content='x' * (900 * 1024))
        self.run_worker(row_seconds=.01)
        self.assert_publication(60)
        self.assertGreater(len(self.scans), 13)
        self.assertLess(max(self.rows_per_scan), bob.MAX_ROWS_PER_SCAN)
        self.assertLess(max(self.scan_seconds), bob.MAX_SCAN_SECONDS)

    def test_elapsed_work_budget_yields_slices_without_waiting_for_poll(self):
        self.fixture(160)
        self.run_worker(row_seconds=.05)
        self.assert_publication(160)
        self.assertGreater(len(self.scans), 13)
        self.assertGreater(sum(self.scan_seconds), 8)
        self.assertLessEqual(max(self.scan_seconds), bob.MAX_SCAN_SECONDS + .050001)

    def test_completed_partial_generation_returns_to_normal_polling(self):
        writer = self.fixture(1)
        writer.execute("UPDATE messages SET data='invalid JSON'")
        writer.commit()
        self.run_worker()
        self.assertEqual(len(self.scans), 1)
        self.assertEqual((self.worker.snapshot['pending'],
                          self.worker.snapshot['collection_complete']), (0, 0))
        self.assertEqual(self.worker.snapshot['skipped_records']['invalid_json'], 1)
        self.assertEqual(len(self.stopping.waits), 1)
        self.assertGreater(self.stopping.waits[0], 4)

    def test_transaction_timeout_retains_publication_then_recovers_with_sticky_limit(self):
        writer = self.fixture(1)

        def scanned(value):
            if len(self.scans) == 1:
                rows = (message(str(index)) for index in range(1, 3001))
                writer.executemany('INSERT INTO messages VALUES (?,?,?,?,?)',
                                   (tuple(row.values()) for row in rows))
                writer.commit()
            elif len(self.scans) == 2:
                self.clock.advance(bob.MAX_TRANSACTION_SECONDS + 1)

        def waited(value):
            if len(self.scans) > 3 and value['collection_complete']:
                self.stopping.set()

        self.run_worker(after_scan=scanned, on_wait=waited)
        first, pending, failed, retry, recovered = self.scans
        self.assertEqual(pending['pending'], 1)
        self.assertEqual((failed['source_available'], failed['pending'], failed['limit_reached']),
                         (0, 0, 1))
        self.assertEqual(failed['sessions'], first['sessions'])
        self.assertEqual(failed['last_success'], first['last_success'])
        self.assertEqual((retry['source_available'], retry['pending'], retry['limit_reached']),
                         (1, 1, 1))
        self.assertEqual(recovered['sessions'][0]['responses'], 3001)
        self.assertEqual((recovered['collection_complete'], recovered['limit_reached']), (1, 0))
        self.assertEqual(self.waited_snapshots, [first, failed, recovered])
        self.assertEqual(self.stopping.waits[1], self.args.poll_seconds)

    def test_stop_between_pending_slices_closes_database_and_writer_lock(self):
        self.fixture(6000)

        def scanned(value):
            if len(self.scans) == 2:
                self.stopping.set()

        self.run_worker(after_scan=scanned)
        self.assertEqual(len(self.scans), 2)
        self.assertEqual(self.worker.snapshot['pending'], 1)
        self.assertEqual(self.stopping.waits, [])
        reader = bob.BobCollector(self.args.bob_home, self.args.session_state_dir)
        reader.close()

    def test_once_still_returns_after_one_pending_slice(self):
        self.fixture(3000)
        self.args.once = True
        self.run_worker()
        self.assertEqual(len(self.scans), 1)
        self.assertEqual(self.worker.snapshot['pending'], 1)
        self.assertEqual(self.stopping.waits, [])

    def test_unavailable_source_with_retained_pending_work_waits_for_poll(self):
        clock = Clock()
        stopping = Stop(clock, lambda: stopping.set())
        worker = cli.SourceWorker('bob', self.args, stopping)
        worker.snapshot.update(pending=1, source_available=1)
        with patch.object(cli, 'BobCollector') as factory, patch.object(cli, 'time', clock):
            factory.return_value.scan.side_effect = cli.SessionSourceUnavailable()
            worker.run()
            factory.return_value.scan.assert_called_once()
            factory.return_value.close.assert_called_once()
        self.assertFalse(worker.failed)
        self.assertEqual((worker.snapshot['source_available'], worker.snapshot['pending']), (0, 1))
        self.assertEqual(stopping.waits, [self.args.poll_seconds])

    def test_codex_pending_files_keep_normal_poll_interval(self):
        clock = Clock()
        stopping = Stop(clock, lambda: stopping.set())
        worker = cli.SourceWorker('codex', self.args, stopping)
        snapshot = {**worker.snapshot, 'source_available': 1, 'pending_files': 2}
        status = {'source_available': 1, 'pending_files': 2, 'sessions': 0}
        with patch.object(cli, 'CodexSource') as factory, patch.object(cli, 'time', clock):
            factory.return_value.scan.return_value = snapshot, b'', status
            worker.run()
            factory.return_value.scan.assert_called_once()
            factory.return_value.close.assert_called_once()
        self.assertFalse(worker.failed)
        self.assertEqual(worker.snapshot['pending_files'], 2)
        self.assertEqual(stopping.waits, [self.args.poll_seconds])

    def test_codex_and_cached_http_endpoint_respond_during_bob_continuation(self):
        self.fixture(8000)
        (self.args.codex_home / 'sessions').mkdir(parents=True)
        stopping, continued, release = threading.Event(), threading.Event(), threading.Event()
        credential = b'synthetic-scheduling-credential'
        endpoint = cli.SessionMetricsEndpoint('127.0.0.1', 0, credential)
        endpoint.start()
        self.addCleanup(endpoint.close)
        worker = cli.SourceWorker('bob', self.args, stopping, endpoint)
        original = bob.BobCollector.scan
        scans = []

        def scan(source):
            if len(scans) == 2:
                continued.set()
                if not release.wait(5):
                    raise RuntimeError('test continuation was not released')
            value = original(source)
            scans.append(value)
            return value

        with patch.object(bob.BobCollector, 'scan', scan):
            thread = threading.Thread(target=worker.run)
            thread.start()
            try:
                self.assertTrue(continued.wait(2), 'Bob did not continue its pending slices')
                self.assertEqual(worker.snapshot['pending'], 1)
                codex_args = SimpleNamespace(**{**vars(self.args), 'once': True})
                codex = cli.SourceWorker('codex', codex_args, stopping, endpoint)
                codex_thread = threading.Thread(target=codex.run)
                codex_thread.start()
                codex_thread.join(2)
                self.assertFalse(codex_thread.is_alive())
                self.assertFalse(codex.failed)
                self.assertEqual(codex.status['source_available'], 1)
                request = Request(f'http://127.0.0.1:{endpoint.server.server_port}/metrics',
                                  headers={'Authorization': 'Bearer ' + credential.decode()})
                with urlopen(request, timeout=2) as response:
                    self.assertEqual(response.status, 200)
                    payload = response.read()
                self.assertIn(b'cwo_codex_collector_source_available{} 1\n', payload)
                self.assertIn(b'traceonaut_bob_collector_pending{} 1\n', payload)
                self.assertTrue(thread.is_alive())
            finally:
                stopping.set()
                release.set()
                thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertFalse(worker.failed)


if __name__ == '__main__':
    unittest.main()
