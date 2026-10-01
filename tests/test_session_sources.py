"""Three supported source configurations and independent scan/publication paths."""
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import collect_codex_sessions as cli
from build_release import build_release
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

    def command(self, sources, *, once=True):
        args = [sys.executable, str(self.release / 'scripts/collect_sessions.py'),
                '--session-state-dir', str(self.output / 'state')]
        for source in sources:
            args += ['--' + source + '-home', str(getattr(self, source)),
                     '--snapshot-file' if source == 'codex' else '--bob-snapshot-file',
                     str(self.output / (source + '.json'))]
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

    def test_both_and_missing_source_do_not_block_each_other(self):
        self.setup_sources(['bob'])
        result = self.run_command(self.command(['codex', 'bob']))
        self.assertEqual(result.returncode, 0, result.stderr)
        status = json.loads(result.stdout)
        self.assertEqual(status['bob']['source_available'], 1)
        self.assertEqual(status['codex']['source_available'], 0)
        self.setup_sources(['codex'])
        (self.bob / 'db/bob.db').rename(self.root / 'held.db')
        result = self.run_command(self.command(['codex', 'bob']))
        status = json.loads(result.stdout)
        self.assertEqual(status['bob']['source_available'], 0)
        self.assertEqual(status['codex']['source_available'], 1)

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

    def test_documented_three_modes_and_bob_render(self):
        self.setup_sources(['codex', 'bob'])
        env = {**os.environ, 'TRACEONAUT_BOB_HOME': str(self.bob),
               'TRACEONAUT_CODEX_HOME': str(self.codex), 'TRACEONAUT_DATA_DIR': str(self.output)}
        guide = (ROOT / 'references/bob-collection.md').read_text()
        for marker, sources in (('bob-only-once', {'bob'}), ('codex-only-once', {'codex'}),
                                ('both-sources-once', {'codex', 'bob'})):
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
                        finally:
                            held.rename(source_db)
                        wait_for({metrics['bob']: 1, metrics['codex']: 1})
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
