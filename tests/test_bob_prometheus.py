"""Evaluate the actual Bob dashboard expressions with independent metric fixtures."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BINARY = os.environ.get('CWO_TEST_PROMETHEUS_BINARY')


@unittest.skipUnless(BINARY, 'separately verified Prometheus binary not supplied')
class BobQueryTests(unittest.TestCase):
    def test_populated_partial_empty_stale_filters_and_time_boundaries(self):
        dashboard = json.loads((ROOT / 'examples/observability/ibm-bob-beta.json').read_text())
        expressions = {(p['id'], t['refId']): t['expr'] for p in dashboard['panels'] for t in p['targets']}
        def query(key, project='.*', chat='.*'):
            return expressions[key].replace('$project', project).replace('$session', chat).replace('$__from', '3600000').replace('$__to', '7200000')
        def series(name, value):
            return {'series': name, 'values': f'{value}x120'}
        base = []
        for project, sid, at, tokens, responses in (('one', 'a', 7100, 100, 3), ('one', 'b', 3600, 200, 5),
                ('two', 'c', 7200, 400, 7), ('one', 'old', 3599, 999, 9), ('one', 'future', 7201, 999, 9)):
            labels = f'project_id="{project}",session_id="{sid}"'
            for suffix, value in (('last_event_timestamp_seconds', at), ('response_count', responses),
                    ('tool_result_count', responses), ('tool_error_count', 0), ('tool_unknown_count', 0), ('partial', 0)):
                base.append(series(f'traceonaut_bob_session_{suffix}{{{labels}}}', value))
            for kind, value in (('input', tokens), ('output', tokens // 10), ('total', tokens + tokens // 10)):
                base.append(series(f'traceonaut_bob_session_usage_tokens{{{labels},token_kind="{kind}"}}', value))
        def health(available=1, complete=1, success=7200):
            return [series('traceonaut_bob_collector_source_available', available),
                    series('traceonaut_bob_collector_collection_complete', complete),
                    series('traceonaut_bob_collector_last_success_timestamp_seconds', success)]
        def check(key, values, project='.*', chat='.*'):
            return {'expr': query(key, project, chat), 'eval_time': '2h', 'exp_samples':
                [{'labels': labels, 'value': value} for labels, value in values]}
        fixtures = []
        for complete in (0, 1):
            tests = [check((1, 'C'), [('{}', 3)]), check((1, 'D'), [('{}', 770)]),
                     check((1, 'E'), [('{}', complete)]), check((2, 'A'), [('{}', 700)]),
                     check((7, 'A'), [('{}', 70)]), check((4, 'A'), [('{}', 15)]),
                     check((4, 'B'), [('{}', 0)]), check((4, 'C'), [('{}', 0)]),
                     check((5, 'A'), [('{}', 2)]), check((1, 'D'), [('{}', 330)], project='one'),
                     check((1, 'D'), [('{}', 110)], project='one', chat='a')]
            for panel, ref, value in ((3, 'A', 3), (6, 'A', 7100000), (6, 'B', 110),
                    (6, 'C', 3), (6, 'D', 0), (6, 'E', 0)):
                tests.append(check((panel, ref), [('{project_id="one",session_id="a"}', value)], 'one', 'a'))
            fixtures.append({'input_series': base + health(complete=complete), 'promql_expr_test': tests})
        for available, success in ((0, 7200), (1, 7000)):
            fixtures.append({'input_series': base + health(available=available, success=success),
                'promql_expr_test': [check((1, 'A'), [('{}', 0)]), check((1, 'D'), [('{}', 770)]),
                    check((2, 'A'), []), check((7, 'A'), []), check((5, 'A'), [])]})
        fixtures.append({'input_series': health(), 'promql_expr_test': [check((1, 'C'), [('{}', 0)]),
            check((1, 'D'), []), check((4, 'A'), []), check((6, 'B'), [])]})
        for item in fixtures:
            item['interval'] = '1m'
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'bob-rules.json'
            path.write_text(json.dumps({'rule_files': [], 'evaluation_interval': '1m', 'tests': fixtures}))
            result = subprocess.run([str(Path(BINARY).with_name('promtool')), 'test', 'rules', str(path)],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
