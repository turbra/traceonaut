"""Evaluate the shipped CLI review table against interval/accounting boundaries."""
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
class ReviewQueryTests(unittest.TestCase):
    def test_source_window_partial_usage_dedup_and_stale_removal(self):
        table = next(p for p in json.loads((ROOT/'examples/observability/cwo-overview.json').read_text())['panels'] if p['id']==401)
        queries = {t['refId']: interpolate(t['expr']) for t in table['targets']}
        def labels(identity):return f'review_id="{identity}",outcome="completed",requested_model="requested",reported_model="reported",effort="high"'
        def series(name, identity, value, extra=''):
            return {'series':name+'{'+labels(identity)+extra+'}', 'values':f'{value}x120'}
        sources=[]
        for identity,stamp in [('old',3599),('start',3600),('middle',5400),('end',7200),('future',7201),('partial',5500)]:
            sources.append(series('cwo_review_started_timestamp_seconds',identity,stamp))
            for kind,val in [('input',2),('cache_creation',10),('cache_read',20),('output',40),('thinking',30)]:
                if identity=='partial' and kind=='cache_read':continue
                sources.append(series('cwo_review_tokens',identity,val,',kind="'+kind+'"'))
            sources.append(series('cwo_review_duration_seconds',identity,1.25))
        sources.append(series('cwo_review_tokens','start',2,',kind="input",instance="copy"'))
        for identity,state in [('start','linked'),('middle','ambiguous'),('end','pending'),('partial','unlinked')]:
            sources.append({'series':f'cwo_review_attribution_state{{review_id="{identity}",state="{state}"}}','values':'1x120'})
        for instance in ('one','copy'):
            sources.append({'series':f'cwo_review_session_info{{review_id="start",project_id="example",session_id="session-one",instance="{instance}"}}','values':'1x120'})
        sources.append({'series':'cwo_review_session_info{review_id="old",project_id="example",session_id="session-one"}','values':'1x120'})
        def expected(values):return [{'labels':'{'+labels(identity)+'}', 'value':value} for identity,value in values.items()]
        checks=[{'expr':queries[key],'eval_time':'2h','exp_samples':expected(vals)} for key,vals in {
            'A':{'start':3600000,'middle':5400000,'end':7200000,'partial':5500000},
            'B':{'start':32,'middle':32,'end':32},
            'C':{k:40 for k in ('start','middle','end','partial')},
            'D':{k:1.25 for k in ('start','middle','end','partial')},
        }.items()]
        checks += [
            {'expr':queries['E'],'eval_time':'2h','exp_samples':[
                {'labels':f'{{review_id="{identity}",state="{state}"}}','value':1}
                for identity,state in [('start','linked'),('middle','ambiguous'),('end','pending'),('partial','unlinked')]]},
            {'expr':queries['F'],'eval_time':'2h','exp_samples':[{'labels':'{review_id="start",session_id="session-one"}','value':1}]},
        ]
        # Removed/conflicting source metrics must not be resurrected from old
        # samples anywhere in the selected range.
        stale=[{**s,'values':s['values'].replace('x120','x118')+' stale'} for s in sources]
        cases=[{'interval':'1m','input_series':sources,'promql_expr_test':checks},
               {'interval':'1m','input_series':stale,'promql_expr_test':[{'expr':q,'eval_time':'2h','exp_samples':[]} for q in queries.values()]},
               {'interval':'1m','input_series':[],'promql_expr_test':[{'expr':q,'eval_time':'2h','exp_samples':[]} for q in queries.values()]}]
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'rules.json';path.write_text(json.dumps({'rule_files':[],'evaluation_interval':'1m','tests':cases}))
            result=subprocess.run([str(Path(BINARY).with_name('promtool')),'test','rules',str(path)],capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_project_and_session_filters_keep_only_attributed_reviews(self):
        dashboard = json.loads((ROOT/'examples/observability/cwo-overview.json').read_text())
        table = next(p for p in dashboard['panels'] if p['id'] == 401)
        reviews = [
            ('first-primary', 'first', 'primary', 'linked'),
            ('first-child', 'first', 'child', 'linked'),
            ('second-other', 'second', 'other', 'linked'),
            ('second-primary', 'second', 'primary', 'linked'),
            ('pending', None, None, 'pending'),
            ('ambiguous', None, None, 'ambiguous'),
            ('unlinked', None, None, 'unlinked'),
        ]
        sources = []
        for identity, project, session, state in reviews:
            for instance in ('one', 'copy'):
                labels = (f'review_id="{identity}",instance="{instance}",outcome="completed",'
                          'requested_model="requested",reported_model="reported",effort="high"')
                def add(metric, value, extra=''):
                    sources.append({'series': metric+'{'+labels+extra+'}', 'values': f'{value}x120'})
                add('cwo_review_started_timestamp_seconds', 5400)
                add('cwo_review_duration_seconds', 1.25)
                for kind, value in [('input', 2), ('cache_creation', 10), ('cache_read', 20), ('output', 40)]:
                    # A selected partial review retains its row and known values.
                    if identity == 'first-child' and kind == 'cache_read':
                        continue
                    add('cwo_review_tokens', value, f',kind="{kind}"')
                sources.append({'series': f'cwo_review_attribution_state{{review_id="{identity}",state="{state}",instance="{instance}"}}',
                                'values': '1x120'})
                if project is not None:
                    sources.append({'series': f'cwo_review_session_info{{review_id="{identity}",project_id="{project}",session_id="{session}",instance="{instance}"}}',
                                    'values': '1x120'})
        # Intentionally supply no cwo_codex_session_* samples: an attributed
        # review remains selectable even if its session has stopped exporting.
        selections = [
            ('.*', '.*', [0, 1, 2, 3, 4, 5, 6]),
            ('first', '.*', [0, 1]),
            ('.*', 'primary', [0, 3]),
            ('second', 'primary', [3]),
            ('first', 'primary', [0]),
            ('first', 'child', [1]),
            ('(first|second)', '(primary|other)', [0, 2, 3]),
            ('first', 'other', []),
            ('missing', '.*', []),
        ]
        cases = []
        for project, session, indexes in selections:
            checks = []
            for target in table['targets']:
                ref = target['refId']
                if ref in {'G','H'}:
                    continue  # Older collectors have no record/evaluation marker.
                selected = [reviews[index] for index in indexes
                            if not (ref == 'B' and reviews[index][0] == 'first-child')
                            and not (ref == 'F' and reviews[index][1] is None)]
                query = interpolate(target['expr'], project, session)
                checks.append({'expr': 'count by (review_id) ('+query+')', 'eval_time': '2h',
                               'exp_samples': [{'labels': f'{{review_id="{row[0]}"}}', 'value': 1}
                                               for row in selected]})
                value = {'A': 5400000, 'B': 32, 'C': 40, 'D': 1.25, 'E': 1, 'F': 1}[ref]
                checks.append({'expr': 'sum('+query+')', 'eval_time': '2h',
                               'exp_samples': [] if not selected else [
                                   {'labels': '{}', 'value': len(selected)*value}]})
            cases.append({'interval': '1m', 'input_series': sources, 'promql_expr_test': checks})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'rules.json'
            path.write_text(json.dumps({'rule_files': [], 'evaluation_interval': '1m', 'tests': cases}))
            result = subprocess.run([str(Path(BINARY).with_name('promtool')), 'test', 'rules', str(path)],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
