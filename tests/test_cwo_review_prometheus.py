"""Evaluate the shipped CLI review table against interval/accounting boundaries."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BINARY = os.environ.get('CWO_TEST_PROMETHEUS_BINARY')


@unittest.skipUnless(BINARY, 'separately verified Prometheus binary not supplied')
class ReviewQueryTests(unittest.TestCase):
    def test_source_window_partial_usage_dedup_and_stale_removal(self):
        table = next(p for p in json.loads((ROOT/'examples/observability/cwo-overview.json').read_text())['panels'] if p['id']==401)
        queries = {t['refId']: t['expr'].replace('$__range','1h').replace('$__from','3600000').replace('$__to','7200000') for t in table['targets']}
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
        def expected(values):return [{'labels':'{'+labels(identity)+'}', 'value':value} for identity,value in values.items()]
        checks=[{'expr':queries[key],'eval_time':'2h','exp_samples':expected(vals)} for key,vals in {
            'A':{'start':3600000,'middle':5400000,'end':7200000,'partial':5500000},
            'B':{'start':32,'middle':32,'end':32},
            'C':{k:40 for k in ('start','middle','end','partial')},
            'D':{k:1.25 for k in ('start','middle','end','partial')},
        }.items()]
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
