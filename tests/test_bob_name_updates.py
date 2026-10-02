"""Exercise the documented Bob name watcher and user-service command."""
import configparser
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from build_release import build_release
from traceonaut.bob_session_telemetry import project_task
from bob_fixtures import task

CHECKOUT = '/absolute/path/to/traceonaut'
PUBLICATION = '/var/lib/traceonaut-dashboards/bob'


def example(marker, language):
    guide = (ROOT / 'references/operations/automatic-name-updates.md').read_text()
    match = re.search(r'<!-- ' + marker + r' -->\s*```' + language + r'\n(.*?)\n```', guide, re.S)
    if not match:
        raise AssertionError('Missing name-update example: ' + marker)
    return match[1]


class BobNameUpdateExamples(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.release = build_release('bob-beta', self.root / 'releases')
        self.private = self.root / '.local/share/traceonaut'
        self.private.mkdir(parents=True, mode=0o700)
        self.public = self.root / 'public/bob'
        self.public.mkdir(parents=True, mode=0o2750)
        self.public.chmod(0o2750)

    def unit(self):
        return (example('bob-name-service', 'ini').replace(CHECKOUT, str(self.release))
                .replace('%h', str(self.root)).replace(PUBLICATION, str(self.public))
                .replace('replace-with-existing-prometheus-uid', 'synthetic'))

    def test_provider_is_single_owner_with_reversible_defaults(self):
        provider = example('bob-name-provider', 'yaml')
        self.assertEqual(provider.count('  - name:'), 1)
        for line in ('disableDeletion: true', 'allowUiUpdates: false',
                     'updateIntervalSeconds: 15', 'path: ' + PUBLICATION):
            self.assertIn(line, provider)
        self.assertNotIn('.local/share', provider)
        self.assertIn('-m 2750', example('bob-publication-directory', 'bash'))
        self.assertIn('-g grafana', example('bob-publication-directory', 'bash'))

    @unittest.skipUnless(shutil.which('systemd-analyze'), 'optional systemd-analyze is unavailable')
    def test_documented_unit_passes_systemd_verification(self):
        unit = self.root / 'traceonaut-bob-dashboard.service'
        unit.write_text(self.unit())
        result = subprocess.run(['systemd-analyze', 'verify', '--man=no', str(unit)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_foreground_and_service_examples_publish_repeatedly(self):
        service = configparser.ConfigParser(interpolation=None)
        service.optionxform = str
        service.read_string(self.unit())
        self.assertNotIn('$', service['Service']['ExecStart'])
        self.assertEqual(service['Service']['Restart'], 'on-failure')
        foreground = example('bob-name-watcher', 'bash').split('\n', 1)[1].replace('\\\n', '')
        foreground = (foreground.replace('$TRACEONAUT_DATA_DIR', str(self.private))
                      .replace(PUBLICATION, str(self.public))
                      .replace('replace-with-existing-prometheus-uid', 'synthetic'))
        for label, argv in [('foreground', shlex.split(foreground)),
                            ('service', shlex.split(service['Service']['ExecStart']))]:
            with self.subTest(label=label):
                snapshot = self.private / 'bob.json'
                output = self.public / 'ibm-bob-beta.json'
                snapshot.unlink(missing_ok=True)
                output.unlink(missing_ok=True)
                log = self.root / (label + '.log')
                with log.open('w+') as stream:
                    process = subprocess.Popen(argv, cwd=self.release, stdout=stream, stderr=stream)
                    def wait_for(check):
                        deadline = time.monotonic() + 12
                        while True:
                            self.assertIsNone(process.poll(), log.read_text())
                            try:
                                if check():
                                    return
                            except (FileNotFoundError, json.JSONDecodeError):
                                pass
                            self.assertLess(time.monotonic(), deadline, log.read_text())
                            time.sleep(.05)
                    def save(rows):
                        snapshot.write_text(json.dumps({'version': 1, 'source': 'ibm-bob', 'sessions': rows}))
                        snapshot.chmod(0o600)
                    try:
                        wait_for(lambda: 'snapshot missing' in log.read_text())
                        first = project_task(task('one'), 1_800_000_000)
                        save([first])
                        wait_for(lambda: 'Synthetic chat one' in output.read_text())
                        group = output.stat().st_gid
                        first['title'] = 'Renamed chat'
                        second = project_task(task('two'), 1_800_000_000)
                        save([first, second])
                        wait_for(lambda: 'Renamed chat' in output.read_text() and 'Synthetic chat two' in output.read_text())
                        self.assertEqual(output.stat().st_gid, group)
                        self.assertEqual(group, self.public.stat().st_gid)
                        self.assertEqual(output.stat().st_mode & 0o777, 0o644)
                        save([])
                        wait_for(lambda: len(json.loads(output.read_text())['templating']['list'][1]['options']) == 1)
                        self.assertFalse(list(self.public.glob('*.tmp')))
                    finally:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
                            raise
                    self.assertEqual(process.returncode, 0, log.read_text())
                # These are presentation-only examples: neither profile is needed.
                self.assertFalse((self.root / '.bob').exists())
                self.assertFalse((self.root / '.codex').exists())


if __name__ == '__main__':
    unittest.main()
