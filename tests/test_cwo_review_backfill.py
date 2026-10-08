"""Recovered source timestamps remain queryable before their first scrape."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
BINARY=os.environ.get('CWO_TEST_PROMETHEUS_BINARY')


@unittest.skipUnless(BINARY,'separately verified Prometheus binary not supplied')
class BackfillQueries(unittest.TestCase):
    def test_historical_end_uses_latest_projection_without_resurrecting_pending(self):
        table=next(p for p in json.loads((ROOT/'examples/observability/cwo-overview.json').read_text())['panels'] if p['id']==401)
        def labels(identity,outcome='completed'):
            return f'review_id="{identity}",outcome="{outcome}",requested_model="model",reported_model="model",effort="high"'
        sources=[{'series':'cwo_review_scan_timestamp_seconds','values':'_ _ 7200 10800 14400'}]
        def add(name,identity,values,extra='',outcome='completed'):
            sources.append({'series':name+'{'+labels(identity,outcome)+extra+'}','values':values})
        # One invocation was initially pending/unlinked; final identity and link
        # arrive after the selected range ended. The old row must disappear.
        marker=',state="unlinked",record_state="missing_result",verdict="unknown",project_id="",session_id="",input_available="0",output_available="0",duration_available="0"'
        add('cwo_review_snapshot_timestamp_seconds','pending','_ _ 7200 stale _',marker)
        add('cwo_review_started_timestamp_seconds','pending','_ _ 5400 stale _')
        marker=',state="linked",record_state="complete",verdict="accept_pending_peer",project_id="project",session_id="session",input_available="1",output_available="1",duration_available="1"'
        add('cwo_review_snapshot_timestamp_seconds','final','_ _ _ 10800 14400',marker)
        add('cwo_review_started_timestamp_seconds','final','_ _ _ 5400 5400')
        for kind,value in [('input',2),('cache_creation',10),('cache_read',20),('output',7)]:
            add('cwo_review_tokens','final',f'_ _ _ {value} {value}',',kind="'+kind+'"')
        add('cwo_review_duration_seconds','final','_ _ _ 3.5 3.5')
        # A later projection removes a previously known output field. The old
        # numeric sample must not be resurrected by forward lookahead.
        marker=',state="linked",record_state="missing_result",verdict="unknown",project_id="project",session_id="session",input_available="0",output_available="0",duration_available="0"'
        add('cwo_review_snapshot_timestamp_seconds','missing','_ _ _ 10800 14400',marker,'failed')
        add('cwo_review_started_timestamp_seconds','missing','_ _ _ 5500 5500',outcome='failed')
        add('cwo_review_tokens','missing','_ _ 99 stale _',',kind="output"','failed')
        checks=[]
        for target in table['targets']:
            expr=target['expr'].replace('${project:regex}','.*').replace('${session:regex}','.*').replace('$__from','3600000').replace('$__to','7200000')
            ref=target['refId']
            expected={'A':{'final':5400000,'missing':5500000},'B':{'final':32},'C':{'final':7},'D':{'final':3.5}}.get(ref)
            if expected is not None:
                samples=[{'labels':'{'+labels(key,'failed' if key=='missing' else 'completed')+'}','value':value} for key,value in expected.items()]
            else:
                dimension={'E':'state','F':'session_id','G':'record_state','H':'verdict'}[ref]
                values={'E':['linked','linked'],'F':['session','session'],'G':['complete','missing_result'],'H':['accept_pending_peer','unknown']}[ref]
                samples=[{'labels':f'{{review_id="{identity}",{dimension}="{value}"}}','value':1} for identity,value in zip(('final','missing'),values)]
            for at in ('2h','4h'):
                checks.append({'expr':expr,'eval_time':at,'exp_samples':samples})
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'rules.json';path.write_text(json.dumps({'rule_files':[],'evaluation_interval':'1h','tests':[{'interval':'1h','input_series':sources,'promql_expr_test':checks}]}))
            result=subprocess.run([str(Path(BINARY).with_name('promtool')),'test','rules',str(path)],capture_output=True,text=True,timeout=45)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
