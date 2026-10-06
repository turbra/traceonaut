import json
import copy
import contextlib
import io
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from render_bob_dashboard import main, render_dashboard, validate_snapshot
from build_release import build_release
from traceonaut.bob_session_telemetry import project_task, HEALTH, SESSION_METRICS, CAPTURE_HEALTH
from bob_fixtures import task


class BobDashboardTests(unittest.TestCase):
    def setUp(self):
        self.template = json.loads((ROOT / 'examples/observability/ibm-bob-beta.json').read_text())
        self.snapshot = {'version': 1, 'source': 'ibm-bob', 'sessions': [project_task(task(), 1_800_000_000)]}

    def test_identity_layout_units_and_independent_queries(self):
        self.assertEqual(self.template['title'], 'IBM Bob · Beta')
        self.assertEqual(self.template['uid'], 'traceonaut-ibm-bob-beta')
        self.assertEqual(self.template['refresh'], '30s')
        self.assertEqual(len(self.template['panels']), 8)
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
        self.assertEqual(next(p for p in self.template['panels'] if p['id']==6)['transformations'][0]['id'], 'merge')

    def test_metric_reference_covers_every_bob_family(self):
        reference = (ROOT / 'references/reference/metrics.md').read_text()
        actual = set(re.findall(r'^\| `(traceonaut_bob_\w+)` \| gauge \|', reference, re.M))
        expected = {'traceonaut_bob_collector_' + k for k in HEALTH}
        expected.update('traceonaut_bob_session_' + k for k in SESSION_METRICS)
        expected.update(('traceonaut_bob_collector_skipped_records', 'traceonaut_bob_session_info',
                         'traceonaut_bob_session_usage_tokens', 'traceonaut_bob_session_token_status',
                         'traceonaut_bob_session_capture_status'))
        expected.update('traceonaut_bob_capture_'+key for key in CAPTURE_HEALTH if key!='loss_total')
        self.assertEqual(actual, expected)

    def test_token_status_wording_and_native_adjacent_counts(self):
        overview = next(p for p in self.template['panels'] if p['id'] == 1)
        names = [t['legendFormat'] for t in overview['targets']]
        self.assertEqual(names[names.index('Chats') + 1], 'Chats with token counts')
        self.assertEqual(overview['options']['orientation'], 'vertical')
        table = next(p for p in self.template['panels'] if p['id'] == 6)
        status = next(o for o in table['fieldConfig']['overrides'] if o['matcher']['options'] == 'Token counts')
        mappings = next(p['value'][0]['options'] for p in status['properties'] if p['id'] == 'mappings')
        self.assertEqual(mappings['1']['text'], 'Not recorded by Bob')
        for code in ('2', '3', '4'):
            self.assertEqual(mappings[code]['text'], 'Unavailable')
        self.assertEqual(mappings['5']['text'], 'Unavailable / stale')
        self.assertEqual(table['fieldConfig']['defaults']['noValue'], 'Not recorded')
        self.assertLessEqual(sum(p['value'] for o in table['fieldConfig']['overrides']
                                for p in o['properties'] if p['id'] == 'custom.width'), 980)

    def test_capture_is_explicitly_partial_separate_from_saved_and_has_no_dash(self):
        capture = next(panel for panel in self.template['panels'] if panel['id']==8)
        self.assertIn('partial',capture['title'])
        self.assertNotIn('traceonaut_bob_session_usage_tokens',json.dumps(capture))
        self.assertIn('increase(',capture['targets'][2]['expr'])
        mappings=capture['fieldConfig']['overrides'][0]['properties'][0]['value'][0]['options']
        self.assertEqual(set(mappings),set('012345'))
        self.assertNotIn('Complete',json.dumps(mappings))
        table=next(panel for panel in self.template['panels'] if panel['id']==6)
        names=table['transformations'][1]['options']['renameByName']
        self.assertEqual((names['Value #B'],names['Value #F'],names['Value #G']),
                         ('Saved tokens','Captured tokens','Capture'))
        self.assertNotIn('—',json.dumps(capture))

    def test_names_only_map_exported_rows_and_do_not_change_queries(self):
        rendered = render_dashboard(self.template, self.snapshot, 'demo')
        self.assertNotIn('__inputs', rendered)
        self.assertIn('Synthetic chat', json.dumps(rendered))
        for before, after in zip(self.template['panels'], rendered['panels']):
            self.assertEqual([t['expr'] for t in before['targets']], [t['expr'] for t in after['targets']])
        self.assertEqual(len(rendered['templating']['list'][1]['options']), 2)
        empty = render_dashboard(self.template, {**self.snapshot, 'sessions': []})
        self.assertEqual(len(empty['templating']['list'][1]['options']), 1)

    def test_input_output_charts_show_separate_saved_and_partial_capture(self):
        for panel_id, kind in ((2, 'input'), (7, 'output')):
            with self.subTest(kind=kind):
                panel = next(p for p in self.template['panels'] if p['id'] == panel_id)
                self.assertEqual(panel['title'], kind.capitalize() + ' tokens')
                saved, captured = panel['targets']
                self.assertEqual(saved['legendFormat'], 'Saved history')
                self.assertEqual(captured['legendFormat'], 'Captured generation · partial')
                self.assertIn('traceonaut_bob_session_usage_tokens', saved['expr'])
                self.assertNotIn('captured_tokens_total', saved['expr'])
                self.assertIn('traceonaut_bob_session_captured_tokens_total', captured['expr'])
                self.assertIn('token_kind="' + kind + '"', captured['expr'])
                self.assertNotIn('traceonaut_bob_session_usage_tokens', captured['expr'])
                self.assertTrue(panel['options']['legend']['showLegend'])
                self.assertEqual(panel['fieldConfig']['defaults']['noValue'], 'Token counts unavailable')
                self.assertTrue(captured['range'])
                self.assertFalse(captured['instant'])

    def test_duplicate_wrong_source_and_untrusted_names(self):
        with self.assertRaises(ValueError):
            validate_snapshot({**self.snapshot, 'source': 'codex'})
        with self.assertRaises(ValueError):
            validate_snapshot({**self.snapshot, 'sessions': self.snapshot['sessions'] * 2})
        self.snapshot['sessions'][0]['title'] = '$project'
        self.assertEqual(validate_snapshot(self.snapshot)[0]['title'], 'Name unavailable')

    def test_mapping_bytes_are_stable_under_row_reordering(self):
        rows = [project_task(task(identity), 1_800_000_000) for identity in ('first', 'second', 'third')]
        # Shared project names also resolve deterministically if records differ.
        rows[0]['project_name'] = 'Project A'
        rows[1]['project_name'] = 'Project B'
        rows[1]['title'] = rows[0]['title']
        first = render_dashboard(self.template, {**self.snapshot, 'sessions': rows})
        second = render_dashboard(self.template, {**self.snapshot, 'sessions': list(reversed(rows))})
        self.assertEqual(json.dumps(first), json.dumps(second))

    def test_only_name_variables_change_and_identity_survives_rename(self):
        extra = {'name': 'source', 'type': 'datasource', 'query': 'prometheus'}
        self.template['templating']['list'].append(extra)
        original = render_dashboard(self.template, self.snapshot)
        self.snapshot['sessions'][0]['title'] = 'Renamed chat'
        duplicate = copy.deepcopy(self.snapshot['sessions'][0])
        duplicate['session_id'] = 'a' * 64
        self.snapshot['sessions'].append(duplicate)
        renamed = render_dashboard(self.template, self.snapshot)
        self.assertEqual(renamed['templating']['list'][-1], extra)
        identity = original['templating']['list'][1]['options'][1]['value']
        option = next(o for o in renamed['templating']['list'][1]['options'] if o['value'] == identity)
        self.assertEqual(option['text'], 'Renamed chat · ' + identity[:8])
        self.assertNotIn('Renamed chat', json.dumps(self.template))

    def test_watch_recovers_repeatedly_and_retains_last_valid_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot, output = root / 'bob.json', root / 'dashboard.json'
            command = ['--template', str(ROOT / 'examples/observability/ibm-bob-beta.json'),
                       '--snapshot-file', str(snapshot), '--output', str(output), '--watch-seconds', '1']
            first = copy.deepcopy(self.snapshot)
            second = copy.deepcopy(first)
            second['sessions'][0]['title'] = 'Renamed chat'
            second['sessions'].append(project_task(task('new-chat'), 1_800_000_000))
            empty = {**self.snapshot, 'sessions': []}
            steps = [first, '{broken', '{broken', second, None, b'\xff', {'version': 9}, first, empty]
            last_good = None
            iteration = 0
            def advance(seconds):
                nonlocal iteration, last_good
                self.assertEqual(seconds, 1)
                if output.exists():
                    body = output.read_bytes()
                    if iteration in (2, 3, 5, 6, 7):
                        self.assertEqual(body, last_good)
                    last_good = body
                    self.assertEqual(output.stat().st_mode & 0o777, 0o644)
                if iteration == len(steps):
                    return True
                value = steps[iteration]
                if value is None:
                    snapshot.unlink()
                else:
                    snapshot.write_bytes(value if isinstance(value, bytes) else
                                         (value if isinstance(value, str) else json.dumps(value)).encode())
                    snapshot.chmod(0o600)
                iteration += 1
                return False
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch('render_bob_dashboard.threading.Event') as event, patch('render_bob_dashboard.signal.signal'), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                event.return_value.wait.side_effect = advance
                self.assertEqual(main(command), 0)
            self.assertEqual(len(json.loads(output.read_text())['templating']['list'][1]['options']), 1)
            self.assertEqual(stderr.getvalue().count('snapshot missing'), 2)
            self.assertEqual(stderr.getvalue().count('snapshot invalid'), 2)
            self.assertNotIn('Renamed chat', stderr.getvalue())
            self.assertEqual([json.loads(line)['named_chats'] for line in stdout.getvalue().splitlines()], [1, 2, 1, 0])
            self.assertFalse(list(root.glob('*.tmp')))

    def test_watch_does_not_retry_unsafe_paths_or_broken_template(self):
        for failure in ('snapshot-mode', 'snapshot-symlink', 'output-symlink', 'template'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                snapshot, output, template = root / 'bob.json', root / 'dashboard.json', root / 'template.json'
                snapshot.write_text(json.dumps(self.snapshot))
                snapshot.chmod(0o600)
                template.write_text(json.dumps(self.template))
                output.write_text('last valid output')
                if failure == 'snapshot-mode':
                    snapshot.chmod(0o644)
                elif failure == 'snapshot-symlink':
                    actual = root / 'actual.json'
                    snapshot.rename(actual)
                    snapshot.symlink_to(actual)
                elif failure == 'output-symlink':
                    actual = root / 'last.json'
                    output.rename(actual)
                    output.symlink_to(actual)
                else:
                    template.write_text('{broken')
                with patch('render_bob_dashboard.threading.Event') as event, patch('render_bob_dashboard.signal.signal'), \
                        contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(main(['--template', str(template), '--snapshot-file', str(snapshot),
                                           '--output', str(output), '--watch-seconds', '1']), 1)
                    event.return_value.wait.assert_not_called()
                self.assertEqual(output.read_text(), 'last valid output')

    def test_invalid_snapshot_is_still_a_one_shot_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot, output = root / 'bob.json', root / 'dashboard.json'
            snapshot.write_text('{broken')
            snapshot.chmod(0o600)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(['--template', str(ROOT / 'examples/observability/ibm-bob-beta.json'),
                                       '--snapshot-file', str(snapshot), '--output', str(output)]), 1)
            self.assertFalse(output.exists())

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
