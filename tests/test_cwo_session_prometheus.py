"""Evaluate shipped CWO session queries with source-time and identity fixtures."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BINARY = os.environ.get('CWO_TEST_PROMETHEUS_BINARY')


@unittest.skipUnless(BINARY, 'separately verified Prometheus binary not supplied')
class CwoSessionQueryTests(unittest.TestCase):
    def test_population_usage_commands_and_unavailable_states(self):
        dashboard=json.loads((ROOT/'examples/observability/cwo-overview.json').read_text())
        queries={p['id']:p['targets'][0]['expr'].replace('$__range','1h').replace('$__from','3600000').replace('$__to','7200000')
                 for p in dashboard['panels']+[c for r in dashboard['panels'] for c in r.get('panels',[])] if p.get('targets') and p['id']>=300}
        coverage = next(p for p in dashboard['panels'] if p['id'] == 309)['targets'][1]['expr']
        queries["coverage"] = coverage
        for panel in dashboard['panels']:
            if panel['id'] >= 300:
                for target in panel.get('targets',[]):
                    self.assertNotIn('[$__range]',target['expr'], 'Session view must not scan every historical sample for a latest-at-end value')
        def series(name,value):return {'series':name,'values':str(value)+'x120'}
        def session(sid,kind='session',associated=3000,last=6000,usage=100,state=1,instance='one'):
            labels=f'project_id="p",session_id="{sid}",instance="{instance}"'
            result=[series('cwo_codex_session_info{'+labels+f',kind="{kind}",model="m",effort="max"}}',1),
                    series('cwo_codex_session_last_event_timestamp_seconds{'+labels+'}',last),
                    series('cwo_codex_session_usage_tokens{'+labels+',token_kind="total"}',usage),
                    series('cwo_codex_session_usage_state{'+labels+'}',state)]
            if associated is not None:result.append(series('cwo_codex_session_cwo_association_timestamp_seconds{'+labels+',source="skill_block"}',associated))
            return result
        def command(identity,at,instance='one'):
            return series(f'cwo_codex_cwo_command_timestamp_seconds{{project_id="p",session_id="a",observation_id="{identity}",tool="run_checked_command",outcome="completed",instance="{instance}"}}',at)
        def checks(values):
            return [{'expr':queries[key],'eval_time':'2h','exp_samples':[] if value is None else [{'labels':'{}','value':value}]} for key,value in values.items()]
        populated=(session('a')+session('a',instance='copy')+session('b',kind='subagent',usage=50)+session('conflict',state=2)
                   +session('ordinary',associated=None)+session('old',last=3599)+session('future',associated=7201)
                   +session('future-event',last=7201))
        # Historical metadata changes cannot double the session count or reintroduce expired sessions.
        populated.append({'series':'cwo_codex_session_info{project_id="p",session_id="a",kind="session",model="old",effort="high"}', 'values':'1x119 stale'})
        expired=session('expired')
        expired[0]['values']='1x119 stale'
        populated+=expired
        commands=[command('old',3599),command('start',3600),command('end',7200),command('future',7201),command('start',3600,'copy')]
        fixtures=[
            {'input_series':populated+commands+[series('cwo_codex_cwo_scan_ready',1)],'promql_expr_test':checks({301:2,302:1,303:150,304:2,"coverage":1})},
            {'input_series':[series('cwo_codex_cwo_scan_ready',1)],'promql_expr_test':checks({301:0,302:0,303:None,304:0})},
            {'input_series':[series('cwo_codex_cwo_scan_ready',0)],'promql_expr_test':checks({301:None,302:None,303:None,304:None,"coverage":0})},
            {'input_series':[],'promql_expr_test':checks({301:None,302:None,303:None,304:None,"coverage":None})},
            {'input_series':session('unknown',state=0)+[series('cwo_codex_cwo_scan_ready',1)],'promql_expr_test':checks({301:1,303:None})},
        ]
        for case in fixtures:case['interval']='1m'
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'rules.json'
            path.write_text(json.dumps({'rule_files':[],'evaluation_interval':'1m','tests':fixtures}))
            result=subprocess.run([str(Path(BINARY).with_name('promtool')),'test','rules',str(path)],capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)


if __name__=='__main__':unittest.main()
