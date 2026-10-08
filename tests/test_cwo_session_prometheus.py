"""Evaluate shipped CWO session queries with source-time and identity fixtures."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BINARY = os.environ.get('CWO_TEST_PROMETHEUS_BINARY')


def interpolate(expr, project='.*', session='.*'):
    return (expr.replace('$__range', '1h').replace('$__from', '3600000')
            .replace('$__to', '7200000').replace('${project:regex}', project)
            .replace('${session:regex}', session))


@unittest.skipUnless(BINARY, 'separately verified Prometheus binary not supplied')
class CwoSessionQueryTests(unittest.TestCase):
    def test_population_usage_commands_and_unavailable_states(self):
        dashboard=json.loads((ROOT/'examples/observability/cwo-overview.json').read_text())
        queries={p['id']:interpolate(p['targets'][0]['expr'])
                 for p in dashboard['panels']+[c for r in dashboard['panels'] for c in r.get('panels',[])] if p.get('targets') and p['id']>=300}
        coverage = next(p for p in dashboard['panels'] if p['id'] == 309)['targets'][1]['expr']
        queries["coverage"] = coverage
        table = next(p for p in dashboard['panels'] if p['id'] == 305)
        detail_queries = {t['refId']: interpolate(t['expr'])
                          for t in table['targets'] if t['refId'] in ('E', 'F', 'G')}
        for panel in dashboard['panels']:
            # Session inventory is latest-at-end. Contractor review history
            # (400-series panels) separately supports expired saved records.
            if 300 <= panel['id'] < 400:
                for target in panel.get('targets',[]):
                    self.assertNotIn('[$__range]',target['expr'], 'Session view must not scan every historical sample for a latest-at-end value')
        def series(name,value):return {'series':name,'values':str(value)+'x120'}
        def session(sid,kind='session',associated=3000,last=6000,usage=100,state=1,instance='one'):
            labels=f'project_id="p",session_id="{sid}",instance="{instance}"'
            result=[series('cwo_codex_session_info{'+labels+f',kind="{kind}",model="m",effort="max"}}',1),
                    series('cwo_codex_session_last_event_timestamp_seconds{'+labels+'}',last),
                    series('cwo_codex_session_usage_tokens{'+labels+',token_kind="total"}',usage),
                    series('cwo_codex_session_usage_state{'+labels+'}',state)]
            result += [series('cwo_codex_session_completed_turns{'+labels+'}',12),
                       series('cwo_codex_session_failed_turns{'+labels+'}',1),
                       series('cwo_codex_session_observed_turn_seconds{'+labels+'}',20)]
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
        # The main view and helper chart work with no observed-dispatch metrics.
        # Old/future records and duplicate exporter copies must not change counts.
        fixtures[0]['promql_expr_test'].append({'expr':queries[310],'eval_time':'2h',
            'exp_samples':[{'labels':'{tool="run_checked_command"}','value':2}]})
        for ref, value in [('E', 36), ('F', 3), ('G', 60)]:
            fixtures[0]['promql_expr_test'].append({'expr': 'sum(' + detail_queries[ref] + ')',
                'eval_time': '2h', 'exp_samples': [{'labels': '{}', 'value': value}]})
            fixtures[3]['promql_expr_test'].append({'expr': detail_queries[ref], 'eval_time': '2h', 'exp_samples': []})
        for case in fixtures[1:]:
            case['promql_expr_test'].append({'expr':queries[310],'eval_time':'2h','exp_samples':[]})
        for case in fixtures:case['interval']='1m'
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'rules.json'
            path.write_text(json.dumps({'rule_files':[],'evaluation_interval':'1m','tests':fixtures}))
            result=subprocess.run([str(Path(BINARY).with_name('promtool')),'test','rules',str(path)],capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_project_and_session_filters_apply_to_every_session_value(self):
        dashboard = json.loads((ROOT/'examples/observability/cwo-overview.json').read_text())
        panels = {p['id']: p for p in dashboard['panels']}
        sessions = [
            ('first', 'primary', 'session', 100, 2, 1, 20),
            ('first', 'child', 'subagent', 200, 3, 2, 30),
            ('second', 'other', 'session', 300, 5, 3, 50),
            # A shared session label in another project must not leak through.
            ('second', 'primary', 'session', 500, 7, 4, 70),
        ]
        sources = [{'series': 'cwo_codex_cwo_scan_ready', 'values': '1x120'}]

        def add(name, labels, value):
            sources.append({'series': name+'{'+labels+'}', 'values': f'{value}x120'})

        for index, (project, session, kind, tokens, done, failed, seconds) in enumerate(sessions):
            for instance in ('one', 'copy'):
                labels = f'project_id="{project}",session_id="{session}",instance="{instance}"'
                add('cwo_codex_session_info', labels+f',kind="{kind}",model="m",effort="max"', 1)
                for metric, value in [
                    ('last_event_timestamp_seconds', 6000),
                    ('cwo_association_timestamp_seconds', 3000),
                    ('usage_state', 1), ('state', 2),
                    ('completed_turns', done), ('failed_turns', failed),
                    ('observed_turn_seconds', seconds),
                ]:
                    add('cwo_codex_session_'+metric, labels, value)
                add('cwo_codex_session_usage_tokens', labels+',token_kind="total"', tokens)
                add('cwo_codex_cwo_command_timestamp_seconds',
                    labels+f',observation_id="command-{index}",tool="run_checked_command",outcome="completed"',
                    3600 if index == 0 else 7200)
                for identity, stamp in [('old', 3599), ('future', 7201)]:
                    add('cwo_codex_cwo_command_timestamp_seconds',
                        labels+f',observation_id="{identity}-{index}",tool="run_checked_command",outcome="completed"', stamp)

        cases = []
        selections = [
            ('.*', '.*', [0, 1, 2, 3]),
            ('first', '.*', [0, 1]),
            ('.*', 'primary', [0, 3]),
            ('second', 'primary', [3]),
            ('first', 'primary', [0]),  # Selecting a parent does not silently include its agents.
            ('first', 'child', [1]),
            ('(first|second)', '(primary|other)', [0, 2, 3]),
            ('first', 'other', []),
            ('missing', '.*', []),
        ]
        for project, session, indexes in selections:
            selected = [sessions[index] for index in indexes]
            checks = []

            def check(expr, samples):
                checks.append({'expr': interpolate(expr, project, session),
                               'eval_time': '2h', 'exp_samples': samples})

            counts = {
                301: sum(row[2] == 'session' for row in selected),
                302: sum(row[2] == 'subagent' for row in selected),
                303: sum(row[3] for row in selected) if selected else None,
                304: len(selected),
            }
            for panel_id, value in counts.items():
                check(panels[panel_id]['targets'][0]['expr'],
                      [] if value is None else [{'labels': '{}', 'value': value}])
            # Check all seven table queries independently, including metadata,
            # state, usage, timestamp, turn outcomes, and observed turn time.
            for target in panels[305]['targets']:
                check('count by (project_id, session_id) ('+target['expr']+')', [
                    {'labels': f'{{project_id="{row[0]}",session_id="{row[1]}"}}', 'value': 1}
                    for row in selected])
            for ref, column in [('E', 4), ('F', 5), ('G', 6)]:
                target = next(t for t in panels[305]['targets'] if t['refId'] == ref)
                check('sum('+target['expr']+')', [] if not selected else [
                    {'labels': '{}', 'value': sum(row[column] for row in selected)}])
            check(panels[310]['targets'][0]['expr'], [] if not selected else [
                {'labels': '{tool="run_checked_command"}', 'value': len(selected)}])
            # Source readiness is collector-wide, even when the selection is empty.
            check(panels[309]['targets'][1]['expr'], [{'labels': '{}', 'value': 1}])
            cases.append({'interval': '1m', 'input_series': sources, 'promql_expr_test': checks})

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'rules.json'
            path.write_text(json.dumps({'rule_files': [], 'evaluation_interval': '1m', 'tests': cases}))
            result = subprocess.run([str(Path(BINARY).with_name('promtool')), 'test', 'rules', str(path)],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)


if __name__=='__main__':unittest.main()
