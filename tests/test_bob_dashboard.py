import json
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from render_bob_dashboard import render_dashboard, validate_snapshot
from build_release import build_release
from traceonaut.bob_session_telemetry import project_task, HEALTH, SESSION_METRICS
from bob_fixtures import task


class BobDashboardTests(unittest.TestCase):
    def setUp(self):
        self.template = json.loads((ROOT / 'examples/observability/ibm-bob-beta.json').read_text())
        self.snapshot = {'version': 1, 'source': 'ibm-bob', 'sessions': [project_task(task(), 1_800_000_000)]}

    def test_identity_layout_units_and_independent_queries(self):
        self.assertEqual(self.template['title'], 'IBM Bob · Beta')
        self.assertEqual(self.template['uid'], 'traceonaut-ibm-bob-beta')
        self.assertEqual(self.template['refresh'], '30s')
        self.assertEqual(len(self.template['panels']), 7)
        self.assertEqual({v['name'] for v in self.template['templating']['list']}, {'project', 'session'})
        text = json.dumps(self.template)
        self.assertNotIn('cwo_codex_', text)
        for panel in self.template['panels']:
            self.assertNotEqual(panel['type'], 'text')
            for target in panel['targets']:
                expr = target['expr']
                if 'traceonaut_bob_session_' in expr:
                    self.assertIn('project_id=~"$project"', expr)
                    self.assertIn('session_id=~"$session"', expr)
        self.assertEqual(self.template['panels'][-1]['transformations'][0]['id'], 'merge')

    def test_metric_reference_covers_every_bob_family(self):
        reference = (ROOT / 'references/reference/metrics.md').read_text()
        actual = set(re.findall(r'^\| `(traceonaut_bob_\w+)` \| gauge \|', reference, re.M))
        expected = {'traceonaut_bob_collector_' + k for k in HEALTH}
        expected.update('traceonaut_bob_session_' + k for k in SESSION_METRICS)
        expected.update(('traceonaut_bob_collector_skipped_records', 'traceonaut_bob_session_info', 'traceonaut_bob_session_usage_tokens'))
        self.assertEqual(actual, expected)

    def test_names_only_map_exported_rows_and_do_not_change_queries(self):
        rendered = render_dashboard(self.template, self.snapshot, 'demo')
        self.assertNotIn('__inputs', rendered)
        self.assertIn('Synthetic chat', json.dumps(rendered))
        for before, after in zip(self.template['panels'], rendered['panels']):
            self.assertEqual([t['expr'] for t in before['targets']], [t['expr'] for t in after['targets']])
        self.assertEqual(len(rendered['templating']['list'][1]['options']), 2)
        empty = render_dashboard(self.template, {**self.snapshot, 'sessions': []})
        self.assertEqual(len(empty['templating']['list'][1]['options']), 1)

    def test_duplicate_wrong_source_and_untrusted_names(self):
        with self.assertRaises(ValueError):
            validate_snapshot({**self.snapshot, 'source': 'codex'})
        with self.assertRaises(ValueError):
            validate_snapshot({**self.snapshot, 'sessions': self.snapshot['sessions'] * 2})
        self.snapshot['sessions'][0]['title'] = '$project'
        self.assertEqual(validate_snapshot(self.snapshot)[0]['title'], 'Name unavailable')

    def test_standalone_bob_bundle_and_missing_snapshot_message(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = build_release('bob-beta', root / 'releases')
            snapshot = root / 'bob.json'
            snapshot.write_text(json.dumps(self.snapshot))
            snapshot.chmod(0o600)
            output = root / 'dashboard.json'
            command = [sys.executable, str(release / 'scripts/render_bob_dashboard.py'),
                       '--template', str(release / 'examples/observability/ibm-bob-beta.json'),
                       '--snapshot-file', str(snapshot), '--output', str(output)]
            result = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(output.read_text())['uid'], self.template['uid'])
            snapshot.unlink()
            result = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 1)
            self.assertIn(str(snapshot), result.stderr)
            self.assertEqual(result.stdout, '')


if __name__ == '__main__':
    unittest.main()
