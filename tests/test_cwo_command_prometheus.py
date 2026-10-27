"""Exercise the shipped command queries with independently specified histories."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BINARY = os.environ.get('CWO_TEST_PROMETHEUS_BINARY')


def query(identity, *, start=3600, end=7200, project='.*', session='.*'):
    document = json.loads((ROOT/'examples/observability/cwo-overview.json').read_text())
    expr = next(p for p in document['panels'] if p['id'] == identity)['targets'][0]['expr']
    return (expr.replace('$__from', str(start * 1000)).replace('$__to', str(end * 1000))
            .replace('${project:regex}', project).replace('${session:regex}', session))


@unittest.skipUnless(BINARY, 'separately verified Prometheus binary not supplied')
class CommandQueries(unittest.TestCase):
    def run_cases(self, cases):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'queries.json'
            path.write_text(json.dumps({'rule_files': [], 'evaluation_interval': '1m', 'tests': cases}))
            result = subprocess.run([str(Path(BINARY).with_name('promtool')), 'test', 'rules', str(path)],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @staticmethod
    def event(identity, at, command='git', project='p', session='s', instance='one', values=None):
        return {'series': f'cwo_codex_cwo_shell_command_timestamp_seconds{{project_id="{project}",session_id="{session}",observation_id="{identity}",command="{command}",instance="{instance}"}}',
                'values': values or str(at) + 'x120'}

    @staticmethod
    def health(complete=1, stamp=7200, ready=1):
        return [{'series': name, 'values': f'{value}x120'} for name, value in [
            ('cwo_codex_cwo_shell_scan_timestamp_seconds', stamp),
            ('cwo_codex_cwo_shell_scan_ready', ready),
            ('cwo_codex_cwo_shell_limit_reached', 0),
            ('cwo_codex_cwo_shell_history_floor_timestamp_seconds', 0),
            ('cwo_codex_cwo_shell_session_complete{project_id="p",session_id="s"}', complete)]]

    def test_counts_boundaries_history_filters_and_withdrawals(self):
        events = [self.event('start', 3600), self.event('start', 3600, instance='copy'),
                  self.event('end', 7200), self.event('before', 3599), self.event('after', 7201),
                  self.event('mcp', 6000, command='curl', project='other', session='child'),
                  self.event('wrapped', 6000, command='python3'),
                  self.event('evicted', 4000, values='4000x60 stale'),
                  self.event('conflict', 5000, values='5000x60 0x59'),
                  self.event('conflict', 5000, instance='old-copy', values='5000x60 stale')]
        cases = []
        for project, session, expected in [
            ('.*', '.*', {'git': 3, 'python3': 1, 'curl': 1}),
            ('p', 's', {'git': 3, 'python3': 1}),
            ('(p|other)', '(s|child)', {'git': 3, 'python3': 1, 'curl': 1}),
            ('other', 'child', {'curl': 1}),
            ('other', 's', {}),
        ]:
            cases.append({'interval': '1m', 'input_series': events + self.health(complete=0),
                'promql_expr_test': [{'expr': query(411, project=project, session=session), 'eval_time': '2h',
                    'exp_samples': [{'labels': '{command="' + command + '"}', 'value': value} for command, value in expected.items()]}]})
        for seconds in (3600, 86400, 30*86400):
            start = 7200 - seconds
            expected = 3 if seconds == 3600 else 4
            cases.append({'interval': '1m', 'input_series': events + self.health(), 'promql_expr_test': [
                {'expr': query(411, start=start, project='p', session='s'), 'eval_time': '2h',
                 'exp_samples': [{'labels': '{command="git"}', 'value': expected}, {'labels': '{command="python3"}', 'value': 1}]}]})
        # Withdrawal overflow excludes the uncertain older interval, not all newer data.
        health = self.health()
        health[3]['values'] = '4000x120'
        cases.append({'interval': '1m', 'input_series': events + health, 'promql_expr_test': [
            {'expr': query(411, project='p'), 'eval_time': '2h', 'exp_samples': [
                {'labels': '{command="git"}', 'value': 1}, {'labels': '{command="python3"}', 'value': 1}]}]})
        self.run_cases(cases)

    def test_complete_partial_stale_absent_and_empty(self):
        cases = []
        floor = self.health()
        floor[3]['values'] = '4000x120'
        for health, expected in [(self.health(), 1), (self.health(complete=0), 0),
                                 (self.health(ready=0), 0), (floor, 0),
                                 (self.health(stamp=7000), 2), ([], 3)]:
            cases.append({'interval': '1m', 'input_series': health, 'promql_expr_test': [
                {'expr': query(412), 'eval_time': '2h', 'exp_samples': [{'labels': '{}', 'value': expected}]},
                {'expr': query(411), 'eval_time': '2h', 'exp_samples': []}]})
        self.run_cases(cases)


if __name__ == '__main__':
    unittest.main()
