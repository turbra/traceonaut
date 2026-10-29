"""Exercise the shipped command queries with independently specified histories."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BINARY = os.environ.get('CWO_TEST_PROMETHEUS_BINARY')


def query(identity, *, ref='A', start=3600, end=7200, project='.*', session='.*'):
    document = json.loads((ROOT/'examples/observability/cwo-overview.json').read_text())
    panel = next(p for p in document['panels'] if p['id'] == identity)
    expr = next(target['expr'] for target in panel['targets'] if target['refId'] == ref)
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
    def health(complete=1, stamp=7200, ready=1):
        return [{'series': name, 'values': f'{value}x120'} for name, value in [
            ('cwo_codex_cwo_shell_scan_timestamp_seconds', stamp),
            ('cwo_codex_cwo_shell_scan_ready', ready),
            ('cwo_codex_cwo_shell_limit_reached', 0),
            ('cwo_codex_cwo_shell_history_floor_timestamp_seconds', 0),
            ('cwo_codex_cwo_shell_session_complete{project_id="p",session_id="s"}', complete)]]

    def test_complete_partial_stale_absent_and_empty(self):
        cases = []
        floor = self.health()
        floor[3]['values'] = '4000x120'
        for health, expected in [(self.health(), 1), (self.health(complete=0), 0),
                                 (self.health(ready=0), 0), (floor, 0),
                                 (self.health(stamp=7000), 2), ([], 3)]:
            cases.append({'interval': '1m', 'input_series': health, 'promql_expr_test': [
                {'expr': query(412), 'eval_time': '2h', 'exp_samples': [{'labels': '{}', 'value': expected}]}]})
        self.run_cases(cases)

    @staticmethod
    def history_series():
        prefix = 'cwo_codex_cwo_shell_'
        labels = 'project_id="p",session_id="s",execution_id="run"'
        metadata = ',session_name="Example chat",project_name="Example project",command_description="curl -I [arguments omitted]"'
        records = [
            {'series': prefix + name + '{' + labels + extra + '}', 'values': str(value) + 'x120'}
            for name, extra, value in (
                ('invocation_timestamp_seconds', '', 7100),
                ('invocation_exit_code', '', 0),
                ('invocation_duration_seconds', '', 0.25),
                ('invocation_outcome', '', 1),
                ('invocation_info', metadata + ',recorded_directory="/example/app"', 1),
                ('network_info', metadata + ',network_index="0",destination="https://one.example",source_ip="192.0.2.4",source_ip_evidence="curl report",http_method="POST",payload_kind="File",payload_files="/example/report.txt",upload_options="--data-binary"', 1),
                ('network_info', metadata + ',network_index="1",destination="https://two.example",source_ip="",source_ip_evidence="not recorded"', 1),
            )
        ]
        return records + [
            {'series':'cwo_codex_collector_source_available','values':'1x120'},
            {'series':'cwo_codex_cwo_shell_scan_timestamp_seconds','values':'7200x120'},
        ]

    def test_command_history_joins_current_text_and_numbers(self):
        expected = '{execution_id="run",session_name="Example chat",project_name="Example project",command_description="curl -I [arguments omitted]",recorded_directory="/example/app"}'
        tests = [{'expr': query(413), 'eval_time': '2h',
                  'exp_samples': [{'labels': expected, 'value': 7100000}]}]
        for ref, value in (('B', 0), ('C', .25), ('D', 1)):
            tests.append({'expr': query(413, ref=ref), 'eval_time': '2h',
                          'exp_samples': [{'labels': '{execution_id="run"}', 'value': value}]})
        for options in ({'start':7150}, {'end':7000}, {'project':'other'}, {'session':'other'}):
            tests.append({'expr': query(413, **options), 'eval_time':'2h', 'exp_samples':[]})
        self.run_cases([{'interval':'1m','input_series':self.history_series(),'promql_expr_test':tests}])

    def test_network_history_keeps_multiple_destinations_and_unknown_ip(self):
        base = 'execution_id="run",session_name="Example chat",project_name="Example project",command_description="curl -I [arguments omitted]"'
        expected = [
            {'labels':'{' + base + ',network_index="0",network_row="run/0",destination="https://one.example",source_ip="192.0.2.4",http_method="POST",payload_kind="File",payload_files="/example/report.txt",upload_options="--data-binary"}', 'value':7100000},
            {'labels':'{' + base + ',network_index="1",network_row="run/1",destination="https://two.example",source_ip="Not recorded",http_method="Unknown",payload_kind="Unknown",payload_files="Not recorded",upload_options="Not recorded"}', 'value':7100000},
        ]
        tests = [{'expr':query(414),'eval_time':'2h','exp_samples':expected},
                 {'expr':query(414,ref='B'),'eval_time':'2h','exp_samples':[
                     {'labels':'{network_row="run/0"}','value':1}, {'labels':'{network_row="run/1"}','value':1}]}]
        for options in ({'start':7150}, {'end':7000}, {'project':'other'}, {'session':'other'}):
            tests.append({'expr':query(414,**options),'eval_time':'2h','exp_samples':[]})
        self.run_cases([{'interval':'1m','input_series':self.history_series(),'promql_expr_test':tests}])

    def test_withdrawn_invocations_and_stale_metadata_do_not_leave_rows(self):
        withdrawn = self.history_series()
        withdrawn[0]['values'] = '0x120'
        stale = self.history_series()
        for row in stale[4:]:
            row['values'] = '1x119 stale'
        self.run_cases([{'interval':'1m','input_series':series,'promql_expr_test':[
            {'expr':query(identity),'eval_time':'2h','exp_samples':[]}
            for identity in (413,414)]} for series in (withdrawn,stale)])

    def test_unavailable_or_stale_collectors_do_not_show_retained_commands(self):
        cases = []
        for available, stamp in ((0,7200),(1,7000)):
            series = self.history_series()
            series[-2]['values'] = str(available)+'x120'
            series[-1]['values'] = str(stamp)+'x120'
            # A second healthy source cannot authorize the unavailable source.
            series += [
                {'series':'cwo_codex_collector_source_available{job="other",instance="other"}','values':'1x120'},
                {'series':'cwo_codex_cwo_shell_scan_timestamp_seconds{job="other",instance="other"}','values':'7200x120'},
            ]
            cases.append({'interval':'1m','input_series':series,'promql_expr_test':[
                {'expr':query(identity,ref=ref),'eval_time':'2h','exp_samples':[]}
                for identity, refs in ((413,'ABCD'),(414,'AB')) for ref in refs]})
        self.run_cases(cases)


if __name__ == '__main__':
    unittest.main()
