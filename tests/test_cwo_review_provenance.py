"""Synthetic launch evidence; session prose and receipt inspection are not proof."""
import copy
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
from traceonaut import cwo_review_provenance as provenance
from traceonaut.cwo_review_telemetry import project, render_review_metrics
from traceonaut.cwo_session_telemetry import CwoSessionCollector
import test_codex_session_telemetry as sessions
from test_cwo_review_telemetry import fixtures


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.f = sessions.SessionCollectionTests()
        self.f.setUp()
        self.f.now = int(self.f.now)
        self.addCleanup(self.f.doCleanups)
        self.cwo = CwoSessionCollector(self.f.home, self.f.state)
        self.addCleanup(lambda: self.cwo.close())
        self.reader = provenance.ProvenanceIndex(self.cwo)
        self.launch, self.result = fixtures()
        self.launch.update(started_at=self.f.now-10, lane='reviewer', account='reviewer', prompt_sha256='b'*64)
        from datetime import datetime, timezone
        self.launch['started_at'] = datetime.fromtimestamp(self.f.now-10, timezone.utc).isoformat()
        self.review = project(self.launch, self.result, {(self.launch['dispatch_id'],self.launch['packet_sha256']):self.f.now-20}, self.f.now)
        self.command = ['python3','/private/contractor/runner.py','reviewer','/private/contractor']
        self.summary = {**{k:self.result[k] for k in provenance.RESULT_FIELDS if k in self.result},
                        'prompt_sha256':'b'*64, 'requested_model':self.launch['requested_model'],
                        'requested_effort':'high', 'lane':'reviewer', 'account':'reviewer', 'result_excerpt':'PRIVATE_TEXT'}

    def event(self, command=None, output=None, sid=None, identity='run', process='123'):
        return self.f.event('event_msg', {'type':'item_completed','thread_id':sid or self.f.sid,'turn_id':'turn',
            'started_at_ms':(self.f.now-11)*1000, 'completed_at_ms':self.f.now*1000,
            'item':{'type':'CommandExecution','id':identity,'process_id':process,'status':'completed',
                    'command':command or self.command,'stdout':json.dumps(self.summary) if output is None else output}}, at=self.f.now)

    def scan(self, **kwargs):
        self.f.collector.scan()
        snapshot = self.f.collector.snapshot(now=self.f.now)
        reviews = {'reviews':[copy.deepcopy(self.review)]}
        status = self.reader.scan(self.f.collector.db,snapshot,reviews,now=self.f.now,**kwargs)
        return reviews['reviews'][0], status

    def test_direct_runner_replay_restart_and_accounting_unchanged(self):
        self.f.write(self.event(),self.event(),self.f.usage())
        self.f.collector.scan()
        before = list(map(tuple,self.f.collector.db.execute('SELECT path,offset FROM files')))
        row,status = self.scan()
        self.assertEqual(status['ready'],1)
        self.assertEqual(row['attribution'],'linked')
        self.assertEqual(row['source_session']['session_id'],self.f.sid)
        self.assertEqual(self.f.row()['usage']['total'],30)
        self.assertEqual(list(map(tuple,self.f.collector.db.execute('SELECT path,offset FROM files'))),before)
        self.cwo.close()
        self.cwo = CwoSessionCollector(self.f.home,self.f.state)
        self.reader = provenance.ProvenanceIndex(self.cwo)
        self.assertEqual(self.scan()[0],row)
        self.cwo.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        raw=self.cwo.path.read_bytes()
        for private in [b'PRIVATE_TEXT',b'/private/contractor',b'reviewer',b'cache_creation_input_tokens']:
            self.assertNotIn(private,raw)

    def test_interactive_launch_requires_typed_stdin_same_process(self):
        code='text(await tools.write_stdin({session_id:123,chars:"python3 /private/contractor/runner.py reviewer /private/contractor\\n",yield_time_ms:1000}));'
        call=self.f.event('response_item',{'type':'custom_tool_call','name':'exec','input':code},at=self.f.now-10)
        self.f.write(call,self.event(command=['bash','-lc','sudo -n su'],output='terminal prompt\n'+json.dumps(self.summary)+'\nterminal prompt'))
        self.assertEqual(self.scan()[0]['attribution'],'linked')
        for wrong in [code.replace('123','124'), 'if (false) {'+code+'}', 'const example = '+json.dumps(code)+';']:
            self.f.path.write_text(self.f.path.read_text().splitlines()[0]+'\n')
            call['payload']['input']=wrong
            self.f.write(call,self.event(command=['bash','-lc','sudo -n su']))
            self.assertEqual(self.scan()[0]['attribution'],'unlinked')

    def test_receipt_reads_mentions_preparation_and_conditional_launch_are_not_proof(self):
        for cmd in [['cat','/private/receipt.json'], ['echo',*self.command],
                    ['python3','/opt/complex-work-orchestration/scripts/run_checked_command.py','/private/prepare.command.json'],
                    ['bash','-lc','false && '+' '.join(self.command)],['bash','-lc','sudo -n su']]:
            self.f.write(self.event(command=cmd,identity=str(uuid4())))
        self.f.write(self.f.event('response_item',{'type':'message','role':'user','content':[{'type':'input_text','text':json.dumps(self.summary)}]}))
        self.assertEqual(self.scan()[0]['attribution'],'unlinked')

    def test_wrong_hash_result_model_effort_time_thread_do_not_link(self):
        for field,value in [('prompt_sha256','c'*64),('requested_model','other'),('requested_effort','low'),('is_error',True),('lane','other')]:
            wrong={**self.summary,field:value}
            self.f.write(self.event(output=json.dumps(wrong),identity=field))
        self.f.write(self.event(sid=str(uuid4())))
        late=self.event(identity='late');late['payload']['started_at_ms']=self.f.now*1000
        self.f.write(late)
        self.assertEqual(self.scan()[0]['attribution'],'unlinked')

    def test_actual_cli_identity_matches_only_receipt_model_and_effort(self):
        cmd=['claude','--model',self.launch['requested_model'],'--effort','high','--output-format','json','-p']
        self.f.write(self.event(command=cmd,output=json.dumps(self.result)))
        self.assertEqual(self.scan()[0]['attribution'],'linked')

    def test_rewrite_and_source_disappearance_remove_link(self):
        self.f.write(self.event())
        self.assertEqual(self.scan()[0]['attribution'],'linked')
        prior=self.f.path.stat()
        self.f.path.write_bytes(self.f.path.read_bytes().replace(b'bbbbbbbb',b'cccccccc'))
        os.utime(self.f.path,ns=(prior.st_atime_ns,prior.st_mtime_ns))
        self.assertEqual(self.scan()[0]['attribution'],'unlinked')
        self.f.path.unlink()
        row,status=self.scan()
        self.assertNotEqual(row['attribution'],'linked')
        self.assertNotIn('source_session',row)

    def test_incremental_pending_bounds_and_no_double_count(self):
        self.f.write(self.event())
        row,status=self.scan(byte_budget=1)
        self.assertEqual(row['attribution'],'pending')
        self.assertEqual(status['pending_files'],1)
        self.assertEqual(self.scan()[0]['attribution'],'linked')
        with mock.patch.object(provenance,'MAX_FILES',0):
            self.assertEqual(self.scan()[0]['attribution'],'pending')
        self.assertEqual(self.scan()[0]['attribution'],'linked')
        with mock.patch.object(provenance,'MAX_EVIDENCE',0):
            self.assertEqual(self.scan()[0]['attribution'],'pending')

    def test_conflicting_sources_are_ambiguous_and_child_is_not_parent(self):
        child=str(uuid4())
        path=self.f.home/'sessions'/('rollout-'+child+'.jsonl')
        self.f.write(self.f.event('session_meta',{'id':child,'cwd':'/workspace/other','timestamp':self.f.now-50,
            'source':{'subagent':{'thread_spawn':{'parent_thread_id':self.f.sid,'depth':1}}}},at=self.f.now-50),
            self.event(sid=child),path=path)
        row,_=self.scan()
        self.assertEqual(row['source_session']['session_id'],child)
        self.f.write(self.event())
        row,_=self.scan()
        self.assertEqual(row['attribution'],'ambiguous')
        self.assertNotIn('source_session',row)

    def test_identical_results_with_different_review_ids_remain_ambiguous(self):
        self.f.write(self.event());self.f.collector.scan()
        reviews={'reviews':[copy.deepcopy(self.review),{**self.review,'review_id':'c'*64}]}
        self.reader.scan(self.f.collector.db,self.f.collector.snapshot(),reviews,now=self.f.now)
        self.assertEqual([r['attribution'] for r in reviews['reviews']],['ambiguous','ambiguous'])

    def test_partial_record_and_unreadable_source_stay_pending(self):
        raw=json.dumps(self.event())
        with self.f.path.open('a') as stream:stream.write(raw)
        self.assertEqual(self.scan()[0]['attribution'],'pending')
        with self.f.path.open('a') as stream:stream.write('\n')
        self.assertEqual(self.scan()[0]['attribution'],'linked')
        with mock.patch.object(provenance,'_open_source',side_effect=PermissionError):
            row,status=self.scan()
        self.assertEqual(row['attribution'],'pending')
        self.assertEqual(status['source_errors'],1)
        self.assertNotIn('source_session',row)

    def test_unrelated_malformed_records_do_not_hide_proven_launch(self):
        with self.f.path.open('a') as stream:
            stream.write('{"type":"response_item","payload":{"type":"reasoning","text":"broken\n')
        self.f.write(self.event())
        self.assertEqual(self.scan()[0]['attribution'],'linked')

    def test_old_metric_lines_are_identical_after_attribution(self):
        self.f.write(self.event())
        linked,_=self.scan()
        base={'source_available':1,'collection_complete':1,'scan_timestamp_seconds':self.f.now,'source_files':1,
              'source_errors':0,'pending_results':0,'limit_reached':0,'skipped_records':{}}
        before=render_review_metrics({**base,'reviews':[self.review]}).splitlines()
        after=render_review_metrics({**base,'reviews':[linked]}).splitlines()
        unchanged=lambda lines:[l for l in lines if not any(name in l for name in (b'cwo_review_session_info',b'cwo_review_attribution_state',b'cwo_review_snapshot_timestamp_seconds'))]
        self.assertEqual(unchanged(before),unchanged(after))
