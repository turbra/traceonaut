"""Three supported source configurations and independent scan/publication paths."""
from contextlib import redirect_stderr, redirect_stdout
import fcntl
import io
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import collect_codex_sessions as cli
from build_release import build_release
from traceonaut.bob_session_telemetry import MAX_ROWS_PER_SCAN
from traceonaut.collector_paths import validate_outputs
from bob_fixtures import database, task, message


class SessionSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.release = build_release('sessions', self.root / 'releases')
        self.codex = self.root / 'codex'
        self.bob = self.root / 'bob'
        self.output = self.root / 'output'
        self.output.mkdir(mode=0o700)

    def command(self, sources, *, once=True, legacy=False, state=None, snapshots=None):
        script = 'collect_codex_sessions.py' if legacy else 'collect_sessions.py'
        args = [sys.executable, str(self.release / 'scripts' / script),
                '--session-state-dir', str(state or self.output / 'state')]
        for source in sources:
            args += ['--' + source + '-home', str(getattr(self, source)),
                     '--snapshot-file' if source == 'codex' else '--bob-snapshot-file',
                     str((snapshots or {}).get(source, self.output / (source + '.json')))]
        return args + (['--once'] if once else [])

    def setup_sources(self, sources):
        if 'codex' in sources:
            (self.codex / 'sessions').mkdir(parents=True)
        if 'bob' in sources:
            now = time.time()
            database(self.bob / 'db/bob.db', [task(now=now)], [message(now=now)]).close()

    def run_command(self, args):
        return subprocess.run(args, cwd=self.root, capture_output=True, text=True, timeout=15)

    def test_codex_only_without_bob(self):
        self.setup_sources(['codex'])
        result = self.run_command(self.command(['codex']))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'codex': {'sessions': 0, 'pending_files': 0, 'source_available': 1}})
        self.assertFalse(self.bob.exists())
        self.assertFalse((self.output / 'state/bob.sqlite3').exists())
        self.assertFalse((self.output / 'bob.json').exists())

    def test_bob_only_without_codex(self):
        self.setup_sources(['bob'])
        result = self.run_command(self.command(['bob']))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['bob']['sessions'], 1)
        self.assertEqual(json.loads(result.stdout)['bob']['collection_complete'], 1)
        self.assertFalse(self.codex.exists())
        self.assertFalse((self.output / 'state/sessions.sqlite3').exists())
        self.assertFalse((self.output / 'codex.json').exists())

    def test_optional_capture_requires_bob_and_cannot_alias_state(self):
        self.setup_sources(['codex','bob'])
        result=self.run_command(self.command(['codex'])+['--bob-otel-journal-dir',str(self.root/'journal')])
        self.assertEqual(result.returncode,2)
        self.assertIn('requires --bob-home',result.stderr)
        for path in (self.output/'state',self.bob,self.output/'bob.json'):
            with self.subTest(path=path):
                result=self.run_command(self.command(['bob'])+['--bob-otel-journal-dir',str(path)])
                self.assertEqual(result.returncode,2)
        journal=self.root/'journal';journal.mkdir(mode=0o700)
        for sources in (['bob'],['bob','codex']):
            result=self.run_command(self.command(sources)+['--bob-otel-journal-dir',str(journal)])
            self.assertEqual(result.returncode,0,result.stderr)
            snapshot=json.loads((self.output/'bob.json').read_text())
            self.assertEqual(snapshot['capture']['enabled'],1)
            self.assertEqual(snapshot['sessions'][0]['capture_status'],3)

    def test_both_and_missing_source_do_not_block_each_other(self):
        self.setup_sources(['bob'])
        result = self.run_command(self.command(['codex', 'bob']))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('Collection failed', result.stderr)
        self.assertNotIn(str(self.codex), result.stderr)
        status = json.loads(result.stdout)
        self.assertEqual(status['bob']['source_available'], 1)
        self.assertEqual(status['codex']['source_available'], 0)
        self.setup_sources(['codex'])
        (self.bob / 'db/bob.db').rename(self.root / 'held.db')
        result = self.run_command(self.command(['codex', 'bob']))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('Collection failed', result.stderr)
        self.assertNotIn(str(self.bob), result.stderr)
        status = json.loads(result.stdout)
        self.assertEqual(status['bob']['source_available'], 0)
        self.assertEqual(status['codex']['source_available'], 1)

    def test_once_unavailable_source_is_failure_in_each_entrypoint(self):
        self.codex.mkdir()
        self.bob.mkdir()
        for sources, legacy in ((['codex'], True), (['codex'], False), (['bob'], False)):
            with self.subTest(sources=sources, legacy=legacy):
                result = self.run_command(self.command(sources, legacy=legacy))
                self.assertEqual(result.returncode, 1, result.stderr)
                status = json.loads(result.stdout)
                if not legacy:
                    status = status[sources[0]]
                self.assertEqual(status['source_available'], 0)
                self.assertIn('Collection failed', result.stderr)
                self.assertNotIn('Traceback', result.stderr)
                self.assertNotIn(str(self.codex), result.stderr)
                self.assertNotIn(str(self.bob), result.stderr)
                self.assertNotIn(str(self.codex), result.stdout)
                self.assertNotIn(str(self.bob), result.stdout)

    def test_pending_bob_scan_is_available_and_once_succeeds(self):
        now = time.time()
        tasks = [task(identity=f'chat-{index}', now=now) for index in range(MAX_ROWS_PER_SCAN + 1)]
        database(self.bob / 'db/bob.db', tasks).close()
        result = self.run_command(self.command(['bob']))
        self.assertEqual(result.returncode, 0, result.stderr)
        status = json.loads(result.stdout)['bob']
        self.assertEqual(status['source_available'], 1)
        self.assertEqual(status['pending'], 1)
        self.assertEqual(status['collection_complete'], 0)

    def test_unwritable_state_parent_is_fatal_in_each_entrypoint(self):
        self.setup_sources(['codex', 'bob'])
        for label, sources, legacy in (('legacy', ['codex'], True),
                                       ('neutral-codex', ['codex'], False),
                                       ('neutral-bob', ['bob'], False),
                                       ('neutral-both', ['codex', 'bob'], False)):
            with self.subTest(mode=label):
                parent = self.root / (label + '-unwritable-state')
                parent.mkdir(mode=0o500)
                try:
                    result = self.run_command(self.command(sources, legacy=legacy, state=parent / 'state'))
                finally:
                    parent.chmod(0o700)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn('Session telemetry unavailable', result.stderr)
                self.assertNotIn('Traceback', result.stderr)
                self.assertNotIn(str(parent), result.stderr)

    def test_bad_snapshot_path_is_fatal_in_each_entrypoint(self):
        self.setup_sources(['codex', 'bob'])
        for label, sources, legacy, broken in (
                ('legacy', ['codex'], True, 'codex'),
                ('neutral-codex', ['codex'], False, 'codex'),
                ('neutral-bob', ['bob'], False, 'bob'),
                ('neutral-both', ['codex', 'bob'], False, 'bob')):
            with self.subTest(mode=label):
                bad_snapshot = self.root / (label + '-snapshot-directory')
                bad_snapshot.mkdir(mode=0o700)
                result = self.run_command(self.command(
                    sources, legacy=legacy, state=self.output / (label + '-state'),
                    snapshots={broken: bad_snapshot}))
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn('Session telemetry unavailable', result.stderr)
                self.assertNotIn('Traceback', result.stderr)
                self.assertNotIn(str(bad_snapshot), result.stderr)

    def test_writer_lock_contention_is_fatal_in_each_entrypoint(self):
        self.setup_sources(['codex', 'bob'])
        for label, sources, legacy, lock_name in (
                ('legacy', ['codex'], True, 'writer.lock'),
                ('neutral-codex', ['codex'], False, 'writer.lock'),
                ('neutral-bob', ['bob'], False, 'bob-writer.lock'),
                ('neutral-both-codex', ['codex', 'bob'], False, 'writer.lock'),
                ('neutral-both-bob', ['codex', 'bob'], False, 'bob-writer.lock')):
            with self.subTest(mode=label):
                state = self.output / (label + '-state')
                state.mkdir(mode=0o700)
                descriptor = os.open(state / lock_name, os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    result = self.run_command(self.command(sources, legacy=legacy, state=state))
                finally:
                    os.close(descriptor)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn('Session telemetry unavailable', result.stderr)
                self.assertNotIn('Traceback', result.stderr)
                self.assertNotIn(str(state), result.stderr)

    def test_daemon_exits_promptly_on_storage_failures_with_both_sources(self):
        self.setup_sources(['codex', 'bob'])
        credential = self.output / 'metrics.token'
        created = self.run_command([sys.executable, str(ROOT / 'scripts/create_metrics_token.py'),
                                    '--credential-file', str(credential)])
        self.assertEqual(created.returncode, 0, created.stderr)
        for failure in ('state-parent', 'snapshot', 'writer-lock'):
            with self.subTest(failure=failure):
                state = self.output / (failure + '-state')
                snapshot = self.output / (failure + '-bob-snapshot')
                descriptor = None
                restricted_parent = None
                if failure == 'state-parent':
                    restricted_parent = self.root / 'unwritable-daemon-state'
                    restricted_parent.mkdir(mode=0o500)
                    state = restricted_parent / 'state'
                elif failure == 'snapshot':
                    snapshot.mkdir(mode=0o700)
                else:
                    state.mkdir(mode=0o700)
                    descriptor = os.open(state / 'bob-writer.lock', os.O_CREAT | os.O_RDWR, 0o600)
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    with socket.socket() as reserved:
                        reserved.bind(('127.0.0.1', 0))
                        port = reserved.getsockname()[1]
                    command = self.command(['codex', 'bob'], once=False, state=state,
                                           snapshots={'bob': snapshot})
                    result = self.run_command(command + [
                        '--credential-file', str(credential), '--port', str(port), '--poll-seconds', '1'])
                finally:
                    if descriptor is not None:
                        os.close(descriptor)
                    if restricted_parent is not None:
                        restricted_parent.chmod(0o700)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(result.stdout, '')
                self.assertIn('Session telemetry unavailable', result.stderr)
                self.assertNotIn('Traceback', result.stderr)
                self.assertNotIn(str(self.root), result.stderr)

    def test_invalid_combinations_before_state_creation(self):
        base = self.command([])
        for extra in ([], ['--bob-home', str(self.bob)], ['--snapshot-file', str(self.output / 'c.json')]):
            result = self.run_command(base + extra)
            self.assertEqual(result.returncode, 2)
            self.assertFalse((self.output / 'state').exists())
        for option in ('--cwo-sessions', '--account-snapshot-file', '--state-dir'):
            extra = [option] + ([] if option == '--cwo-sessions' else [str(self.output / 'extra')])
            result = self.run_command(self.command(['bob']) + extra)
            self.assertEqual(result.returncode, 2)
            self.assertIn('require --codex-home', result.stderr)

    def test_disabled_source_emits_no_metrics_and_endpoint_merges_caches(self):
        endpoint = object.__new__(cli.SessionMetricsEndpoint)
        endpoint._lock = threading.Lock()
        endpoint.update_part('bob', b'traceonaut_bob_collector_source_available{} 1\n')
        self.assertNotIn(b'cwo_codex', endpoint._payload)
        endpoint.update_part('codex', b'cwo_codex_collector_source_available{} 1\n')
        self.assertIn(b'traceonaut_bob', endpoint._payload)
        self.assertIn(b'cwo_codex', endpoint._payload)
        endpoint.update_part('bob', b'traceonaut_bob_collector_source_available{} 0\n')
        self.assertIn(b'cwo_codex_collector_source_available{} 1', endpoint._payload)

    def test_worker_failure_preserves_codex_persisted_error_counters(self):
        args = SimpleNamespace(snapshot_file=self.output / 'codex.json', once=True, poll_seconds=1)
        worker = cli.SourceWorker('codex', args, threading.Event())
        worker.snapshot['errors'] = {'read_error': 7}
        with patch.object(cli, 'CodexSource', side_effect=OSError('private source detail')):
            worker.run()
        self.assertEqual(worker.snapshot['source_available'], 0)
        self.assertEqual(worker.snapshot['errors'], {'read_error': 7})
        self.assertNotIn('private source detail', json.dumps(worker.snapshot))

    def test_enabled_health_is_present_before_source_construction(self):
        args = SimpleNamespace(snapshot_file=self.output / 'codex.json', bob_snapshot_file=self.output / 'bob.json')
        endpoint = object.__new__(cli.SessionMetricsEndpoint)
        endpoint._lock = threading.Lock()
        with patch.object(cli, 'CodexSource') as codex, patch.object(cli, 'BobCollector') as bob:
            cli.SourceWorker('codex', args, threading.Event(), endpoint)
            cli.SourceWorker('bob', args, threading.Event(), endpoint)
            codex.assert_not_called()
            bob.assert_not_called()
        self.assertIn(b'cwo_codex_collector_source_available{} 0', endpoint._payload)
        self.assertIn(b'traceonaut_bob_collector_source_available{} 0', endpoint._payload)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_http_200_with_startup_health_zero_is_not_once_success(self):
        credential = b'synthetic-local-credential'
        endpoint = cli.SessionMetricsEndpoint('127.0.0.1', 0, credential)
        args = SimpleNamespace(bob_snapshot_file=self.output / 'bob.json', snapshot_file=None)
        cli.SourceWorker('bob', args, threading.Event(), endpoint)
        endpoint.start()
        try:
            url = f'http://127.0.0.1:{endpoint.server.server_port}/metrics'
            with urlopen(Request(url, headers={'Authorization': 'Bearer ' + credential.decode()}),
                         timeout=2) as response:
                self.assertEqual(response.status, 200)
                payload = response.read()
            self.assertIn(b'traceonaut_bob_collector_source_available{} 0\n', payload)
        finally:
            endpoint.close()
        result = self.run_command(self.command(['bob']))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(result.stdout)['bob']['source_available'], 0)

    def test_private_bob_index_write_failure_is_fatal(self):
        self.setup_sources(['bob'])
        args = SimpleNamespace(bob_home=self.bob, session_state_dir=self.output / 'state',
            bob_snapshot_file=self.output / 'bob.json', snapshot_file=None,
            session_retention_seconds=2592000, session_export_cap=1000, once=True, poll_seconds=1)
        stopping = threading.Event()
        worker = cli.SourceWorker('bob', args, stopping)
        real_collector = cli.BobCollector
        private_detail = 'PRIVATE_INDEX_FAILURE_SENTINEL'

        def failing_collector(*constructor_args, **constructor_kwargs):
            collector = real_collector(*constructor_args, **constructor_kwargs)
            database_connection = collector.db
            def execute(query, *execute_args):
                if query.startswith('INSERT OR REPLACE INTO publication'):
                    raise sqlite3.OperationalError(private_detail)
                return database_connection.execute(query, *execute_args)
            collector.db = MagicMock(wraps=database_connection)
            collector.db.execute.side_effect = execute
            return collector

        with patch.object(cli, 'BobCollector', side_effect=failing_collector):
            worker.run()
        self.assertTrue(worker.failed)
        self.assertTrue(stopping.is_set())
        self.assertEqual(worker.status['source_available'], 0)
        self.assertNotIn(private_detail, json.dumps(worker.snapshot))

    def test_private_codex_index_error_is_fatal(self):
        self.setup_sources(['codex'])
        args = SimpleNamespace(codex_home=self.codex, session_state_dir=self.output / 'state',
            snapshot_file=self.output / 'codex.json', bob_snapshot_file=None,
            session_retention_seconds=2592000, session_export_cap=1000,
            cwo_sessions=False, state_dir=None, account_snapshot_file=None,
            once=True, poll_seconds=1)
        stopping = threading.Event()
        worker = cli.SourceWorker('codex', args, stopping)
        real_source = cli.CodexSource
        private_detail = 'PRIVATE_CODEX_INDEX_FAILURE_SENTINEL'

        def failing_source(*constructor_args, **constructor_kwargs):
            source = real_source(*constructor_args, **constructor_kwargs)
            database_connection = source.collector.db
            def execute(query, *execute_args):
                if query.startswith('SELECT * FROM files WHERE offset<size'):
                    raise sqlite3.OperationalError(private_detail)
                return database_connection.execute(query, *execute_args)
            source.collector.db = MagicMock(wraps=database_connection)
            source.collector.db.execute.side_effect = execute
            return source

        with patch.object(cli, 'CodexSource', side_effect=failing_source):
            worker.run()
        self.assertTrue(worker.failed)
        self.assertTrue(stopping.is_set())
        self.assertEqual(worker.status['source_available'], 0)
        self.assertNotIn(private_detail, json.dumps(worker.snapshot))

    def test_locked_external_codex_database_recovers_with_bob_healthy(self):
        self.setup_sources(['codex', 'bob'])
        private_detail = 'PRIVATE_EXTERNAL_TITLE_SENTINEL'
        external = self.codex / 'state_5.sqlite'
        source_db = sqlite3.connect(external)
        source_db.execute('CREATE TABLE threads(id TEXT, cwd TEXT, name TEXT)')
        source_db.execute('INSERT INTO threads VALUES (?, ?, ?)',
                          ('00000000-0000-4000-8000-000000000001', '/workspace/example', private_detail))
        source_db.commit()
        source_db.execute('BEGIN EXCLUSIVE')
        process = None
        try:
            once = self.run_command(self.command(
                ['codex', 'bob'], state=self.output / 'once-state',
                snapshots={'codex': self.output / 'once-codex.json',
                           'bob': self.output / 'once-bob.json'}))
            self.assertEqual(once.returncode, 1, once.stdout + once.stderr)
            self.assertEqual(json.loads(once.stdout)['codex']['source_available'], 0)
            self.assertEqual(json.loads(once.stdout)['bob']['source_available'], 1)
            self.assertIn('Collection failed', once.stderr)
            self.assertNotIn(str(external), once.stderr)
            self.assertNotIn(private_detail, once.stdout + once.stderr)
            self.assertNotIn('Traceback', once.stderr)

            credential = self.output / 'metrics.token'
            created = self.run_command([sys.executable, str(ROOT / 'scripts/create_metrics_token.py'),
                                        '--credential-file', str(credential)])
            self.assertEqual(created.returncode, 0, created.stderr)
            token = credential.read_text().strip()
            with socket.socket() as reserved:
                reserved.bind(('127.0.0.1', 0))
                port = reserved.getsockname()[1]
            process = subprocess.Popen(self.command(['codex', 'bob'], once=False) + [
                '--credential-file', str(credential), '--port', str(port), '--poll-seconds', '1'],
                cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            url = f'http://127.0.0.1:{port}/metrics'
            def wait_for(expected, *, require_attempt=False):
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    self.assertIsNone(process.poll(), 'collector exited during a source read outage')
                    try:
                        request = Request(url, headers={'Authorization': 'Bearer ' + token})
                        with urlopen(request, timeout=1) as response:
                            payload = response.read().decode()
                        if (all(f'{name}{{}} {value}\n' in payload for name, value in expected.items())
                                and (not require_attempt or (self.output / 'codex.json').is_file())):
                            return payload
                    except (OSError, URLError):
                        pass
                    time.sleep(.05)
                self.fail('collector did not publish the expected source health state')

            metrics = {'codex': 'cwo_codex_collector_source_available',
                       'bob': 'traceonaut_bob_collector_source_available'}
            unavailable = wait_for({metrics['codex']: 0, metrics['bob']: 1}, require_attempt=True)
            self.assertNotIn(private_detail, unavailable)
            source_db.rollback()
            recovered = wait_for({metrics['codex']: 1, metrics['bob']: 1})
            self.assertNotIn(private_detail, recovered)
            self.assertIsNone(process.poll())
        finally:
            source_db.rollback()
            source_db.close()
            if process is not None:
                process.terminate()
                stdout, stderr = process.communicate(timeout=10)
        if process is not None:
            self.assertEqual(process.returncode, 0, stderr.decode())
            self.assertEqual(stdout, b'')
            self.assertEqual(stderr, b'')

    def test_daemon_unexpected_worker_error_stops_and_joins_other_source(self):
        self.setup_sources(['codex', 'bob'])
        argv = self.command(['codex', 'bob'], once=False)[2:] + [
            '--credential-file', str(self.output / 'unused.token'), '--poll-seconds', '1']
        bob_started = threading.Event()
        stopping_events = []
        closed = []
        private_detail = 'PRIVATE_WORKER_FAILURE_SENTINEL'
        real_worker = cli.SourceWorker
        real_bob_scan = cli.BobCollector.scan
        real_codex_close = cli.CodexSource.close
        real_bob_close = cli.BobCollector.close

        class CapturingWorker(real_worker):
            def __init__(self, name, args, stopping, *extra):
                stopping_events.append(stopping)
                super().__init__(name, args, stopping, *extra)

        def failing_codex_scan(_source):
            if not bob_started.wait(4):
                raise AssertionError('Bob worker never started')
            raise RuntimeError(private_detail)

        def bob_scan(source, *args, **kwargs):
            bob_started.set()
            return real_bob_scan(source, *args, **kwargs)

        def codex_close(source):
            closed.append('codex')
            return real_codex_close(source)

        def bob_close(source):
            closed.append('bob')
            return real_bob_close(source)

        result = {}
        stdout, stderr = io.StringIO(), io.StringIO()
        def invoke():
            try:
                result['code'] = cli.main(argv, require_codex=False)
            except BaseException as error:
                result['error'] = error

        with redirect_stdout(stdout), redirect_stderr(stderr), \
                patch.object(cli, 'SourceWorker', CapturingWorker), \
                patch.object(cli.CodexSource, 'scan', failing_codex_scan), \
                patch.object(cli.BobCollector, 'scan', bob_scan), \
                patch.object(cli.CodexSource, 'close', codex_close), \
                patch.object(cli.BobCollector, 'close', bob_close), \
                patch.object(cli, 'SessionMetricsEndpoint') as endpoint, \
                patch.object(cli, 'read_credential', return_value=b'synthetic-credential'), \
                patch.object(cli.signal, 'signal'), patch.object(cli.os, 'umask'):
            thread = threading.Thread(target=invoke, name='test-collector-main')
            thread.start()
            thread.join(4)
            timed_out = thread.is_alive()
            if timed_out and stopping_events:
                stopping_events[0].set()
            thread.join(5)
        self.assertFalse(thread.is_alive(), 'collector main did not join its workers')
        self.assertFalse(timed_out, 'unexpected worker error did not stop the daemon')
        self.assertNotIn('error', result)
        self.assertEqual(result['code'], 1)
        self.assertEqual(sorted(closed), ['bob', 'codex'])
        endpoint.return_value.close.assert_called_once_with()
        self.assertEqual(stdout.getvalue(), '')
        self.assertIn('Session telemetry unavailable', stderr.getvalue())
        self.assertNotIn(private_detail, stderr.getvalue())
        self.assertNotIn('Traceback', stderr.getvalue())
        self.assertEqual(self.run_command(self.command(['codex', 'bob'])).returncode, 0)

    def test_documented_bob_once_modes_and_bob_render(self):
        self.setup_sources(['codex', 'bob'])
        env = {**os.environ, 'TRACEONAUT_BOB_HOME': str(self.bob),
               'TRACEONAUT_CODEX_HOME': str(self.codex), 'TRACEONAUT_DATA_DIR': str(self.output)}
        guide = (ROOT / 'references/bob-collection.md').read_text()
        self.assertNotIn('<!-- codex-only-once -->', guide)
        for marker, sources in (('bob-only-once', {'bob'}), ('both-sources-once', {'codex', 'bob'})):
            block = re.search(r'<!-- ' + marker + r' -->\s*```bash\n(.*?)\n```', guide, re.S)[1]
            result = subprocess.run(['bash', '-eu', '-c', block], cwd=ROOT, env=env,
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(set(json.loads(result.stdout)), sources)
            for status in json.loads(result.stdout).values():
                self.assertEqual(status['source_available'], 1)
        guide = (ROOT / 'references/dashboards/ibm-bob-beta.md').read_text()
        block = re.search(r'<!-- render-bob -->\s*```bash\n(.*?)\n```', guide, re.S)[1]
        # The guide's default storage assignment is already supplied by the fixture.
        block = '\n'.join(line for line in block.splitlines() if not line.startswith('export TRACEONAUT_DATA_DIR='))
        result = subprocess.run(['bash', '-eu', '-c', block], cwd=ROOT, env=env,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        dashboard = json.loads((self.output / 'ibm-bob-beta.json').read_text())
        self.assertEqual(dashboard['uid'], 'traceonaut-ibm-bob-beta')

    def test_documented_bob_serve_modes_and_shared_troubleshooting(self):
        self.setup_sources(['codex', 'bob'])
        data_dir = self.root / '.local/share/traceonaut'
        data_dir.mkdir(parents=True, mode=0o700)
        credential = data_dir / 'metrics.token'
        result = self.run_command([sys.executable, str(ROOT / 'scripts/create_metrics_token.py'),
                                   '--credential-file', str(credential)])
        self.assertEqual(result.returncode, 0, result.stderr)
        token = credential.read_text().strip()
        env = {**os.environ, 'HOME': str(self.root),
               'TRACEONAUT_BOB_HOME': str(self.bob), 'TRACEONAUT_CODEX_HOME': str(self.codex),
               'TRACEONAUT_DATA_DIR': str(data_dir), 'TRACEONAUT_METRICS_CREDENTIAL': str(credential),
               'TRACEONAUT_LISTEN_ADDRESS': '127.0.0.1'}
        guide = (ROOT / 'references/bob-collection.md').read_text()
        health = {'codex': 'cwo_codex_collector_source_available',
                  'bob': 'traceonaut_bob_collector_source_available'}
        for marker, sources in (('bob-only-serve', {'bob'}),
                                ('both-sources-serve', {'bob', 'codex'})):
            with self.subTest(marker=marker):
                block = re.search(r'<!-- ' + marker + r' -->\s*```bash\n(.*?)\n```', guide, re.S)[1]
                self.assertEqual(block.count('--port 9464'), 1)
                with socket.socket() as reserved:
                    reserved.bind(('127.0.0.1', 0))
                    port = reserved.getsockname()[1]
                process = subprocess.Popen(
                    ['bash', '-eu', '-c', 'exec ' + block.replace('--port 9464', f'--port {port}')],
                    cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    deadline = time.monotonic() + 12
                    url = f'http://127.0.0.1:{port}/metrics'
                    while True:
                        self.assertIsNone(process.poll(), 'documented collector exited before readiness')
                        try:
                            request = Request(url, headers={'Authorization': 'Bearer ' + token})
                            with urlopen(request, timeout=1) as response:
                                payload = response.read().decode()
                            if all(f'{health[source]}{{}} 1\n' in payload for source in sources):
                                break
                        except (OSError, URLError):
                            pass
                        self.assertLess(time.monotonic(), deadline, 'documented collector did not become ready')
                        time.sleep(.05)
                    for disabled in set(health) - sources:
                        self.assertNotIn(health[disabled], payload)
                    troubleshooting = (ROOT / 'references/operations/troubleshooting.md').read_text()
                    check = re.search(r'<!-- metrics-check -->\s*```bash\n(.*?)\n```',
                                      troubleshooting, re.S)[1]
                    self.assertEqual(check.count('127.0.0.1:9464'), 1)
                    checked = subprocess.run(
                        ['bash', '-eu', '-c', check.replace('127.0.0.1:9464', f'127.0.0.1:{port}')],
                        cwd=ROOT, env=env, capture_output=True, text=True, timeout=10)
                    self.assertEqual(checked.returncode, 0, checked.stderr)
                    self.assertIn('HTTP 200', checked.stdout)
                    self.assertNotIn(token, checked.stdout + checked.stderr)
                finally:
                    process.terminate()
                    stdout, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, stderr.decode())
                self.assertEqual(stdout, b'')
                self.assertEqual(stderr, b'')

    def test_http_auth_all_modes_and_source_recovery(self):
        self.setup_sources(['codex', 'bob'])
        credential = self.output / 'metrics.token'
        result = self.run_command([sys.executable, str(ROOT / 'scripts/create_metrics_token.py'),
                                   '--credential-file', str(credential)])
        self.assertEqual(result.returncode, 0, result.stderr)
        token = credential.read_text().strip()
        for sources in (['bob'], ['codex'], ['bob', 'codex']):
            with self.subTest(sources=sources):
                with socket.socket() as reserved:
                    reserved.bind(('127.0.0.1', 0))
                    port = reserved.getsockname()[1]
                process = subprocess.Popen(self.command(sources, once=False) + [
                    '--credential-file', str(credential), '--port', str(port), '--poll-seconds', '1'],
                    cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                url = f'http://127.0.0.1:{port}/metrics'
                def wait_for(expected):
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        try:
                            request = Request(url, headers={'Authorization': 'Bearer ' + token})
                            with urlopen(request, timeout=1) as response:
                                payload = response.read().decode()
                            if all(f'{metric}{{}} {value}\n' in payload for metric, value in expected.items()):
                                return payload
                        except (OSError, URLError):
                            pass
                        time.sleep(.05)
                    self.fail('enabled source did not publish the expected HTTP health state')
                try:
                    metrics = {'bob': 'traceonaut_bob_collector_source_available',
                               'codex': 'cwo_codex_collector_source_available'}
                    payload = wait_for({metrics[source]: 1 for source in sources})
                    for disabled in set(metrics) - set(sources):
                        self.assertNotIn(metrics[disabled], payload)
                    with self.assertRaises(HTTPError) as rejected:
                        urlopen(url, timeout=1)
                    self.assertEqual(rejected.exception.code, 401)
                    if len(sources) == 2:
                        source_db = self.bob / 'db/bob.db'
                        held = self.root / 'held.db'
                        source_db.rename(held)
                        try:
                            wait_for({metrics['bob']: 0, metrics['codex']: 1})
                            self.assertIsNone(process.poll())
                        finally:
                            held.rename(source_db)
                        wait_for({metrics['bob']: 1, metrics['codex']: 1})
                        source_sessions = self.codex / 'sessions'
                        held_sessions = self.root / 'held-sessions'
                        source_sessions.rename(held_sessions)
                        try:
                            wait_for({metrics['codex']: 0, metrics['bob']: 1})
                            self.assertIsNone(process.poll())
                        finally:
                            held_sessions.rename(source_sessions)
                        wait_for({metrics['codex']: 1, metrics['bob']: 1})
                finally:
                    process.terminate()
                    stdout, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, stderr.decode())
                self.assertEqual(stdout, b'')
                self.assertEqual(stderr, b'')

    def test_slow_source_does_not_delay_other_source(self):
        self.setup_sources(['codex', 'bob'])
        stopping, blocked, release = threading.Event(), threading.Event(), threading.Event()
        args = SimpleNamespace(codex_home=self.codex, bob_home=self.bob, session_state_dir=self.output / 'state',
            snapshot_file=self.output / 'codex.json', bob_snapshot_file=self.output / 'bob.json',
            session_retention_seconds=2592000, session_export_cap=1000, cwo_sessions=False, state_dir=None,
            account_snapshot_file=None, once=True, poll_seconds=1)
        for slow_name, fast_name in (('bob', 'codex'), ('codex', 'bob')):
            blocked.clear()
            release.clear()
            cls = cli.BobCollector if slow_name == 'bob' else cli.CodexSource
            original = cls.scan
            def slow_scan(source):
                blocked.set()
                release.wait(5)
                return original(source)
            slow = cli.SourceWorker(slow_name, args, stopping)
            fast = cli.SourceWorker(fast_name, args, stopping)
            with patch.object(cls, 'scan', slow_scan):
                thread = threading.Thread(target=slow.run)
                thread.start()
                try:
                    self.assertTrue(blocked.wait(2))
                    fast_thread = threading.Thread(target=fast.run)
                    fast_thread.start()
                    fast_thread.join(2)
                    self.assertFalse(fast_thread.is_alive())
                    self.assertEqual(fast.status['source_available'], 1)
                    self.assertTrue(thread.is_alive())
                finally:
                    release.set()
                    thread.join(5)

    def test_aliasing_and_reserved_outputs_rejected(self):
        self.setup_sources(['bob'])
        alias = self.output / 'alias.json'
        os.link(self.bob / 'db/bob.db', alias)
        with self.assertRaises(ValueError):
            validate_outputs([self.bob], self.output / 'state', [alias])
        for path in (self.bob / 'out.json', self.output / 'state/bob.sqlite3',
                     self.output / 'state/bob.sqlite3-wal', self.output / 'state/bob.sqlite3-journal'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate_outputs([self.bob], self.output / 'state', [path])


if __name__ == '__main__':
    unittest.main()
