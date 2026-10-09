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
    def test_review_health_separates_collection_faults_from_saved_gaps(self):
        panel=next(p for p in json.loads((ROOT/'examples/observability/cwo-overview.json').read_text())['panels'] if p['id']==402)
        # Old failed/unsupported runs persist, while actual collection faults
        # appear on the second scan. Each query must also display a real zero.
        sources=[]
        for reason,value in [('missing_result',3),('unsupported_launch',4),('invalid_record',0)]:
            sources.append({'series':f'cwo_review_discovery_gaps{{reason="{reason}"}}','values':f'{value} {value}'})
        for metric,values in [('cwo_review_source_errors','0 2'),('cwo_review_limit_reached','0 1'),
                              ('cwo_review_skipped_records{reason="invalid_record"}','0 1'),
                              ('cwo_review_skipped_records{reason="preparation_record"}','2 2'),('cwo_review_pending_results','3 3')]:
            sources.append({'series':metric,'values':values})
        sources.append({'series':'cwo_review_skipped_records{reason="missing_provenance"}','values':'3 3'})
        checks=[]
        for at,faults in [('0m',0),('1m',4)]:
            for target in panel['targets']:
                checks.append({'expr':target['expr'],'eval_time':at,
                    'exp_samples':[{'labels':'{}','value':faults if target['refId']=='A' else 10}]})
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'rules.json'
            path.write_text(json.dumps({'rule_files':[],'evaluation_interval':'1m','tests':[
                {'interval':'1m','input_series':sources,'promql_expr_test':checks}]}))
            result=subprocess.run([str(Path(BINARY).with_name('promtool')),'test','rules',str(path)],capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_historical_review_survives_normal_expiry_and_export_cap(self):
        table=next(p for p in json.loads((ROOT/'examples/observability/cwo-overview.json').read_text())['panels'] if p['id']==401)
        labels='review_id="expired",outcome="completed",requested_model="model",reported_model="model",effort="high"'
        marker=',history="tracked",state="linked",record_state="complete",verdict="accept",project_id="project",session_id="session",input_available="1",output_available="1",duration_available="1"'
        # Launch at hour 1, retain through hour 721, then expire normally.
        # A historical range ending at hour 2 must still display this review
        # after subsequent scans have advanced beyond its 30-day retention.
        sources=[{'series':'cwo_review_scan_timestamp_seconds','values':'0+3600x744'},
                 {'series':'cwo_review_snapshot_timestamp_seconds{'+labels+marker+'}','values':'_ 3600+3600x720 stale'},
                 {'series':'cwo_review_started_timestamp_seconds{'+labels+'}','values':'_ 3600x720 stale'},
                 {'series':'cwo_review_duration_seconds{'+labels+'}','values':'_ 3x720 stale'}]
        for kind,value in [('input',2),('cache_creation',10),('cache_read',20),('output',7)]:
            sources.append({'series':'cwo_review_tokens{'+labels+',kind="'+kind+'"}','values':f'_ {value}x720 stale'})
        # A still-eligible review falls off the export cap after hour 2.
        # Its last positive sample must survive without a withdrawal marker.
        sources.extend([{'series':s['series'].replace('expired','capped'),
                         'values':s['values'].replace('x720','x1')}
                        for s in sources[1:]])
        # Withdrawal before expiry differs from normal retention expiry.
        withdrawn=labels.replace('expired','withdrawn')
        sources.extend([
            {'series':'cwo_review_snapshot_timestamp_seconds{'+withdrawn+marker+'}','values':'_ 3600 7200 stale'},
            {'series':'cwo_review_snapshot_timestamp_seconds{'+withdrawn+marker.replace('record_state="complete"','record_state="removed"')+'}','values':'_ _ _ 10800+3600x718 stale'},
            {'series':'cwo_review_started_timestamp_seconds{'+withdrawn+'}','values':'_ 3600 3600 stale'},
        ])
        checks=[{'expr':'sum(cwo_review_scan_timestamp_seconds)','eval_time':'744h',
                 'exp_samples':[{'labels':'{}','value':2678400}]}]
        for target in table['targets']:
            expr=target['expr'].replace('${project:regex}','.*').replace('${session:regex}','.*').replace('$__range','2h').replace('$__from','0').replace('$__to','7200000')
            ref=target['refId']
            expected={'A':3600000,'B':32,'C':7,'D':3}.get(ref)
            if expected is not None:
                samples=[{'labels':'{'+labels+'}','value':expected}]
            else:
                dimension,value={'E':('state','linked'),'F':('session_id','session'),'G':('record_state','complete'),'H':('verdict','accept')}[ref]
                samples=[{'labels':f'{{review_id="expired",{dimension}="{value}"}}','value':1}]
            samples += [{'labels':s['labels'].replace('expired','capped'),'value':s['value']} for s in samples[:]]
            checks.append({'expr':expr,'eval_time':'2h','exp_samples':samples})
            wide=target['expr'].replace('${project:regex}','.*').replace('${session:regex}','.*').replace('$__range','744h').replace('$__from','0').replace('$__to','2678400000')
            checks.append({'expr':wide,'eval_time':'744h','exp_samples':samples})
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'rules.json';path.write_text(json.dumps({'rule_files':[],'evaluation_interval':'1h','tests':[{'interval':'1h','input_series':sources,'promql_expr_test':checks}]}))
            result=subprocess.run([str(Path(BINARY).with_name('promtool')),'test','rules',str(path)],capture_output=True,text=True,timeout=45)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_historical_end_uses_latest_projection_without_resurrecting_pending(self):
        table=next(p for p in json.loads((ROOT/'examples/observability/cwo-overview.json').read_text())['panels'] if p['id']==401)
        def labels(identity,outcome='completed'):
            return f'review_id="{identity}",outcome="{outcome}",requested_model="model",reported_model="model",effort="high"'
        sources=[{'series':'cwo_review_scan_timestamp_seconds','values':'_ _ 7200 10800 14400'}]
        def add(name,identity,values,extra='',outcome='completed'):
            sources.append({'series':name+'{'+labels(identity,outcome)+extra+'}','values':values})
        # One invocation was initially pending/unlinked; final identity and link
        # arrive after the selected range ended. The old row must disappear.
        marker=',history="tracked",state="unlinked",record_state="missing_result",verdict="unknown",project_id="",session_id="",input_available="0",output_available="0",duration_available="0"'
        add('cwo_review_snapshot_timestamp_seconds','pending','_ _ 7200 stale _',marker)
        add('cwo_review_started_timestamp_seconds','pending','_ _ 5400 stale _')
        add('cwo_review_snapshot_timestamp_seconds','pending','_ _ _ 10800 14400',marker.replace('record_state="missing_result"','record_state="removed"'))
        marker=',history="tracked",state="linked",record_state="complete",verdict="accept_pending_peer",project_id="project",session_id="session",input_available="1",output_available="1",duration_available="1"'
        add('cwo_review_snapshot_timestamp_seconds','final','_ _ _ 10800 14400',marker)
        add('cwo_review_started_timestamp_seconds','final','_ _ _ 5400 5400')
        for kind,value in [('input',2),('cache_creation',10),('cache_read',20),('output',7)]:
            add('cwo_review_tokens','final',f'_ _ _ {value} {value}',',kind="'+kind+'"')
        add('cwo_review_duration_seconds','final','_ _ _ 3.5 3.5')
        # A later projection removes a previously known output field. The old
        # numeric sample must not be resurrected by forward lookahead.
        marker=',history="tracked",state="linked",record_state="missing_result",verdict="unknown",project_id="project",session_id="session",input_available="0",output_available="0",duration_available="0"'
        add('cwo_review_snapshot_timestamp_seconds','missing','_ _ _ 10800 14400',marker,'failed')
        add('cwo_review_started_timestamp_seconds','missing','_ _ _ 5500 5500',outcome='failed')
        add('cwo_review_tokens','missing','_ _ 99 stale _',',kind="output"','failed')
        checks=[]
        for target in table['targets']:
            expr=target['expr'].replace('${project:regex}','.*').replace('${session:regex}','.*').replace('$__range','1h').replace('$__from','3600000').replace('$__to','7200000')
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
