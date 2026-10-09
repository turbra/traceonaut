"""Recorded launches, safe output discovery and independent reporting checks."""
import copy
from datetime import datetime, timezone
import json
import hashlib
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from traceonaut import cwo_review_discovery as discovery
from traceonaut.cwo_review_telemetry import render_review_metrics

NOW=1800000000


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.db=sqlite3.connect(':memory:');self.db.row_factory=sqlite3.Row
        self.addCleanup(self.db.close)
        self.reader=discovery.LaunchDiscovery(self.db)
        self.sid=str(uuid4())
        self.snapshot={'sessions':[{'session_id':self.sid,'project_id':'project'}],
                       'cwo_sessions':{'associations':[{'session_id':self.sid}]}}
        self.result={'type':'result','is_error':False,'uuid':str(uuid4()),'session_id':str(uuid4()),
                     'usage':{'input_tokens':2,'cache_creation_input_tokens':10,'cache_read_input_tokens':20,'output_tokens':7},
                     'modelUsage':{'review-model':{}},'duration_ms':3500}

    def event(self,command,output='',identity='execution',sid=None,start=NOW-10):
        return {'type':'event_msg','timestamp':datetime.fromtimestamp(NOW,timezone.utc).isoformat(),
                'payload':{'type':'item_completed','thread_id':sid or self.sid,'turn_id':'turn',
                           'started_at_ms':start*1000,'completed_at_ms':NOW*1000,
                           'item':{'type':'CommandExecution','id':identity,'status':'completed','exit_code':0,
                                   'command':['bash','-lc',command],'stdout':output}}}

    def consume(self,event):
        self.reader.consume(event,'sessions/example.jsonl',event['payload']['thread_id'])

    def scan(self,base=None):
        result=base or {'reviews':[],'source_available':0,'collection_complete':1,'source_files':0,
                       'source_errors':0,'pending_results':0,'limit_reached':0,'skipped_records':{},'scan_timestamp_seconds':NOW}
        return self.reader.merge(result,self.snapshot,NOW)

    def command(self,output=None):
        command='claude --model review-model --effort high --output-format stream-json -p'
        if output:command+=' > '+str(output)
        return command

    def test_unconfigured_stream_exact_usage_and_replay(self):
        path=self.root/'review.stream.jsonl';path.write_text(json.dumps(self.result)+'\n')
        event=self.event(self.command(path));self.consume(event)
        first=self.scan();self.assertEqual(len(first['reviews']),1)
        row=first['reviews'][0]
        self.assertEqual(row['tokens'],{'input':2,'cache_creation':10,'cache_read':20,'output':7})
        self.assertEqual(row['source_session']['session_id'],self.sid)
        self.assertEqual(row['timestamp'],NOW-10)
        self.consume(event);self.assertEqual(self.scan()['reviews'],first['reviews'])
        path.unlink();self.assertEqual(self.scan()['reviews'],first['reviews'])
        payload=render_review_metrics(first)
        self.assertNotIn(str(self.root).encode(),payload)

    def test_late_final_retries_without_new_rollout_bytes(self):
        path=self.root/'review-stream.jsonl';path.write_text('{}\n')
        self.consume(self.event(self.command(path)))
        before=self.scan();self.assertEqual(before['discovery']['gaps']['missing_result'],1)
        self.assertEqual(before['reviews'][0]['tokens'],{})
        path.write_text(json.dumps(self.result)+'\n')
        self.assertEqual(self.scan()['reviews'][0]['tokens']['output'],7)

    def test_claude_inline_and_codex_inline(self):
        self.consume(self.event(self.command(),json.dumps(self.result)))
        thread=str(uuid4())
        raw='\n'.join(map(json.dumps,[{'type':'thread.started','thread_id':thread},
            {'type':'turn.completed','usage':{'input_tokens':100,'cached_input_tokens':30,'output_tokens':20,'reasoning_output_tokens':5}}]))
        self.consume(self.event('codex exec -m sample -c \'model_reasoning_effort="high"\' --json -',raw,'codex'))
        rows=self.scan()['reviews'];self.assertEqual(len(rows),2)
        row=next(r for r in rows if r.get('provider')=='codex')
        self.assertEqual(row['tokens'],{'input':70,'cache_creation':0,'cache_read':30,'output':20,'thinking':5})
        self.assertEqual(row['reported_model'],'unknown')
        self.assertIsNone(row['provider_result_id'])
        self.consume(self.event('codex exec resume -m sample --json -',raw,'resume',start=NOW-5))
        self.assertEqual(len(self.scan()['reviews']),3)

    def test_stderr_redirect_does_not_replace_inline_result(self):
        error = self.root/'err.log'
        error.write_text('a diagnostic, not a result')
        self.consume(self.event(self.command()+' 2>'+str(error),json.dumps(self.result)))
        snapshot = self.scan()
        self.assertEqual(snapshot['reviews'][0]['tokens']['output'],7)
        self.assertEqual(snapshot['discovery']['gaps'],{})

    def test_separate_stdout_stderr_and_descriptor_copy(self):
        output = self.root/'result.stream.jsonl'
        output.write_text(json.dumps(self.result)+'\n')
        for suffix in (' 2>/tmp/err.log', ' 2>>relative.log', ' 2>&1'):
            with self.subTest(suffix=suffix):
                command = self.command(output)+suffix
                self.assertEqual(discovery.launch_metadata(['sh','-c',command])['output'],str(output))
                self.consume(self.event(command,identity='redirected'))
        self.assertEqual(self.scan()['reviews'][0]['tokens']['output'],7)
        self.assertEqual(self.scan()['discovery']['gaps'],{})

    def test_numeric_arguments_quotes_and_direct_argv_are_preserved(self):
        for prompt in ('2', "'2'", '"2"', "'>'"):
            command = self.command()+' '+prompt+' >/tmp/result'
            self.assertEqual(discovery.launch_metadata(['sh','-c',command])['output'],'/tmp/result')
        self.assertEqual(discovery.launch_metadata(['sh','-c',self.command()+' 1>/tmp/result'])['output'],'/tmp/result')
        self.assertIsNone(discovery.launch_metadata(['sh','-c',self.command()+' 1>&2']))
        self.assertIsNone(discovery.launch_metadata(['sh','-c',self.command()+' &>/tmp/result']))
        self.assertIsNone(discovery.launch_metadata(['claude','-p','2>literal'])['output'])
        self.assertEqual(discovery.launch_metadata(['sh','-c','cat /tmp/prompt | '+self.command('/tmp/result')])['output'],'/tmp/result')
        relative=discovery.launch_metadata(['sh','-c',self.command()+' < reviews/brief.md >/tmp/result 2>/tmp/err'])
        self.assertEqual(relative['output'],'/tmp/result')
        self.assertIsNone(relative['prompt'])

    def test_discovery_export_cap_preserves_eligible_history(self):
        from traceonaut.cwo_review_telemetry import preserve_review_markers
        self.consume(self.event(self.command(),json.dumps(self.result),'old',start=NOW-20))
        first = preserve_review_markers(self.db,self.scan())
        old_id = first['reviews'][0]['review_id']
        self.result['uuid'] = str(uuid4())
        self.consume(self.event(self.command(),json.dumps(self.result),'new'))
        with mock.patch.object(discovery,'EXPORT_CAP',1):
            snapshot = preserve_review_markers(self.db,self.scan())
        self.assertEqual(len(snapshot['reviews']),1)
        self.assertIn(old_id,snapshot['eligible_review_ids'])
        self.assertEqual(snapshot['retired_reviews'],[])
        self.assertEqual(snapshot['limit_reached'],1)
        self.assertEqual(snapshot['collection_complete'],0)

    def test_result_gaps_and_unprojected_launches_have_distinct_membership(self):
        from traceonaut.cwo_review_telemetry import preserve_review_markers
        self.consume(self.event(self.command(),json.dumps(self.result),'known'))
        first=preserve_review_markers(self.db,self.scan())
        identity=first['reviews'][0]['review_id']
        # An unsupported launch cannot prove a previously indexed row vanished.
        self.db.execute('DELETE FROM review_launches')
        self.consume(self.event(self.command()+'; echo done',identity='unsupported'))
        unknown=self.scan()
        self.assertFalse(unknown['membership_complete'])
        self.assertEqual(preserve_review_markers(self.db,unknown)['retired_reviews'],[])
        # A known launch with no result is still an enumerated member. It does
        # not mask the independent disappearance of the original source row.
        self.db.execute('DELETE FROM review_discovery_errors')
        self.consume(self.event(self.command(),identity='pending'))
        pending=self.scan()
        self.assertTrue(pending['membership_complete'])
        self.assertEqual([r['review_id'] for r in preserve_review_markers(self.db,pending)['retired_reviews']],[identity])

    def test_heredoc_probe_is_separate_from_review(self):
        self.consume(self.event(self.command()+" <<'INPUT'\nReview this; explain A && B.\nINPUT",json.dumps(self.result)))
        probe=copy.deepcopy(self.result);probe['uuid']=str(uuid4())
        self.consume(self.event(self.command()+" <<'INPUT'\nReply with exactly STDIN_OK.\nINPUT",json.dumps(probe),'probe'))
        result=self.scan();self.assertEqual(len(result['reviews']),1)
        self.assertEqual(result['discovery']['model_checks'],1)

    def test_shell_suffix_cannot_bind_unrelated_output(self):
        for suffix in ('; cat anything > /tmp/result',' && cat anything > /tmp/result',' || claude --model other'):
            self.assertIsNone(discovery.launch_metadata(['bash','-lc',self.command()+suffix]))

    def test_duplicate_provider_identity_is_not_relinked(self):
        self.consume(self.event(self.command(),json.dumps(self.result)))
        other=str(uuid4());self.snapshot['sessions'].append({'session_id':other,'project_id':'other'})
        self.snapshot['cwo_sessions']['associations'].append({'session_id':other})
        self.consume(self.event(self.command(),json.dumps(self.result),'copy',other))
        row=self.scan()['reviews'][0]
        self.assertEqual(row['attribution'],'ambiguous');self.assertNotIn('source_session',row)

    def test_conflict_cannot_be_restored_by_third_copy(self):
        self.consume(self.event(self.command(),json.dumps(self.result),'a'))
        wrong=copy.deepcopy(self.result);wrong['usage']['output_tokens']=9
        self.consume(self.event(self.command(),json.dumps(wrong),'b'))
        self.consume(self.event(self.command(),json.dumps(self.result),'c'))
        result=self.scan();self.assertEqual(result['reviews'],[])
        self.assertEqual(result['discovery']['gaps']['conflicting_result'],1)

    def test_zero_is_retained_and_missing_is_absent(self):
        self.result['usage']={'input_tokens':0,'output_tokens':0}
        self.consume(self.event(self.command(),json.dumps(self.result)))
        row=self.scan()['reviews'][0]
        self.assertEqual(row['tokens'],{'input':0,'output':0})

    def test_malformed_final_and_wrong_clock_do_not_invent_usage(self):
        self.result['usage']['input_tokens']=-1
        self.consume(self.event(self.command(),json.dumps(self.result)))
        self.assertEqual(self.scan()['reviews'][0]['tokens'],{})
        bad=self.event(self.command(),json.dumps(self.result),'clock')
        bad['timestamp']='2020-01-01T00:00:00Z';self.consume(bad)
        self.assertEqual(self.db.execute('SELECT count(*) FROM review_launches').fetchone()[0],1)

    def test_symlinks_are_reported_not_followed(self):
        path=self.root/'stream.jsonl';real=self.root/'real.jsonl'
        real.write_text(json.dumps(self.result)+'\n');path.symlink_to(real)
        self.consume(self.event(self.command(path)))
        self.assertEqual(self.scan()['discovery']['gaps']['unreadable_output'],1)

    def test_scan_limit_and_retention_are_visible(self):
        path=self.root/'stream.jsonl';path.write_text(json.dumps(self.result)+'\n')
        self.reader.remaining=0;self.consume(self.event(self.command(path)))
        self.assertEqual(self.scan()['limit_reached'],1)
        self.reader.merge(self.scan(),self.snapshot,NOW+31*86400)
        self.assertEqual(self.db.execute('SELECT count(*) FROM review_launches').fetchone()[0],0)

    def test_partial_final_recovery_then_restart(self):
        path=self.root/'stream.jsonl';path.write_text('{"type":"result"')
        self.consume(self.event(self.command(path)))
        self.assertEqual(self.scan()['reviews'][0]['tokens'],{})
        path.write_text(json.dumps(self.result)+'\n')
        self.reader=discovery.LaunchDiscovery(self.db)
        self.assertEqual(self.scan()['reviews'][0]['tokens']['output'],7)

    def test_control_commands_and_unsupported_shell_are_accounted_separately(self):
        for command in ('claude auth status --json','claude --version','codex login status','codex exec --help | sed -n 1,100p'):
            self.consume(self.event(command,identity=command))
        self.assertEqual(self.scan()['discovery']['launches'],0)
        self.assertEqual(self.scan()['discovery']['gaps'],{})
        self.consume(self.event(self.command()+'; echo done',identity='unsupported'))
        self.assertEqual(self.scan()['discovery']['gaps'],{'unsupported_launch':1})

    def test_checked_wrapper_exact_binding_replay_and_cleanup(self):
        from test_cwo_audit_telemetry import record
        prompt=b'Dispatch ID: dispatch\nPacket SHA-256: '+b'a'*64+b'\nPrivate review prompt\n'
        (self.root/'review-prompt.txt').write_bytes(prompt)
        result={k:v for k,v in self.result.items() if k not in ('uuid','session_id')}
        provenance={'started_at':datetime.fromtimestamp(NOW-9,timezone.utc).isoformat(),
                    'finished_at':datetime.fromtimestamp(NOW-1,timezone.utc).isoformat(),
                    'requested_model':'review-model','effort':'high','events':[result],
                    'prompt_sha256':hashlib.sha256(prompt).hexdigest()}
        artifact=self.root/'review-provenance.json';artifact.write_text(json.dumps(provenance))
        audit=self.root/'contract-audit.jsonl'
        audit.write_bytes(record('dispatch_prepared',timestamp=NOW-20,dispatch_id='dispatch',packet_sha256='a'*64)+
                          record('return_evaluated',timestamp=NOW-1,dispatch_id='dispatch',packet_sha256='a'*64,
                                 verdict='accept',peer_review_status='pending',hold_classification='peer-review-pending'))
        script=self.root/'run_review.py'
        script.write_text("from pathlib import Path\nART = Path(__file__).parent\nprompt = (ART / 'review-prompt.txt').read_text()\n(ART / 'review-provenance.json').write_text('saved')\n")
        spec=self.root/'review-spec.json';spec.write_text(json.dumps({'mode':'argv','argv':['python3','-B',str(script)]}))
        os.utime(script,(NOW-20,NOW-20));os.utime(spec,(NOW-20,NOW-20))
        event=self.event('python3 /example/complex-work-orchestration/scripts/run_checked_command.py review-spec.json')
        event['payload']['item']['cwd']=self.root.as_uri()
        self.consume(event);first=self.scan();row=first['reviews'][0]
        self.assertEqual(row['source_session']['session_id'],self.sid)
        self.assertEqual(row['timestamp'],NOW-9)
        self.assertEqual(row['finished_timestamp'],NOW-1)
        self.assertEqual(row['evaluation'],'accept_pending_peer')
        self.assertEqual(row['tokens']['output'],7)
        audit_snapshot=self.reader.merge_audit(None,NOW,True)
        self.assertEqual(len(audit_snapshot['events']),2)
        self.assertEqual(self.reader.merge_audit(audit_snapshot,NOW,True)['events'],audit_snapshot['events'])
        self.consume(event);self.assertEqual(len(self.scan()['reviews']),1)
        for path in (artifact,script,spec,audit,self.root/'review-prompt.txt'):path.unlink()
        self.reader=discovery.LaunchDiscovery(self.db)
        self.assertEqual(self.scan()['reviews'],first['reviews'])
        self.assertEqual(self.reader.merge_audit(None,NOW,True)['events'],audit_snapshot['events'])

    def test_reused_output_preserves_two_failed_executions(self):
        thread=str(uuid4());path=self.root/'attempt.stream.jsonl'
        path.write_text(json.dumps({'type':'thread.started','thread_id':thread})+'\n')
        for identity,start in [('first',NOW-20),('second',NOW-10)]:
            event=self.event('codex exec -m model --json - > '+str(path),identity=identity,start=start)
            event['payload']['item'].update(status='failed',exit_code=1)
            self.consume(event)
        rows=self.scan()['reviews'];self.assertEqual(len(rows),2)
        self.assertTrue(all(row['tokens']=={} and row['outcome']=='failed' for row in rows))
        self.assertTrue(all(row['finished_timestamp']==NOW for row in rows))

    def test_source_to_metrics_backfill_then_new_launch_and_restart(self):
        from test_cwo_session_telemetry import CwoCollectionTests
        from traceonaut.cwo_review_provenance import ProvenanceIndex
        from traceonaut.cwo_review_telemetry import ReviewCollector
        from traceonaut.cwo_session_telemetry import CwoSessionCollector
        fixture=CwoCollectionTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        f=fixture.f;f.now=int(f.now)
        output=self.root/'outside-configured-roots.stream.jsonl'
        output.write_text(json.dumps(self.result)+'\n')
        def event(identity):
            return f.event('event_msg',{'type':'item_completed','thread_id':f.sid,'turn_id':'turn',
                'started_at_ms':(f.now-1)*1000,'completed_at_ms':f.now*1000,
                'item':{'type':'CommandExecution','id':identity,'status':'completed','exit_code':0,
                        'command':['bash','-lc',self.command(output)]}},at=f.now)
        fixture.f.write(fixture.skill(at=f.now-2),event('old'),f.usage())
        reader=ProvenanceIndex(fixture.cwo,discover=True)
        def scan():
            f.collector.scan();snapshot=f.collector.snapshot(now=f.now)
            snapshot['cwo_sessions']=fixture.cwo.scan(f.collector.db,snapshot,now=f.now)
            reviews=ReviewCollector(directories=[]).scan(now=f.now)
            status=reader.scan(f.collector.db,snapshot,reviews,now=f.now)
            self.assertEqual(status['ready'],1)
            return reviews
        first=scan();self.assertEqual(len(first['reviews']),1)
        self.assertEqual(first['reviews'][0]['source_session']['session_id'],f.sid)
        self.assertEqual(f.row()['usage']['total'],30)
        self.assertEqual(scan()['reviews'],first['reviews'])
        self.result['uuid']=str(uuid4());output=self.root/'another-new-directory.stream.jsonl'
        output.write_text(json.dumps(self.result)+'\n');f.write(event('new'))
        current=scan();self.assertEqual(len(current['reviews']),2)
        metrics=render_review_metrics(current).decode()
        lines=[line for line in metrics.splitlines() if line.startswith('cwo_review_started_timestamp_seconds{')]
        self.assertEqual(len(lines),2);self.assertEqual(len(set(lines)),2)
        fixture.cwo.close();fixture.cwo=CwoSessionCollector(f.home,f.state)
        reader=ProvenanceIndex(fixture.cwo,discover=True)
        self.assertEqual(scan()['reviews'],current['reviews'])

    def test_parser_upgrade_replays_redirects_without_losing_saved_results(self):
        from test_cwo_session_telemetry import CwoCollectionTests
        from traceonaut.cwo_review_provenance import ProvenanceIndex
        from traceonaut.cwo_review_telemetry import ReviewCollector
        fixture=CwoCollectionTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        f=fixture.f;f.now=int(f.now)
        output=self.root/'retained.stream.jsonl'
        output.write_text(json.dumps(self.result)+'\n')
        inline=copy.deepcopy(self.result);inline['uuid']=str(uuid4())
        def event(identity,command,stdout=''):
            return f.event('event_msg',{'type':'item_completed','thread_id':f.sid,'turn_id':'turn',
                'started_at_ms':(f.now-1)*1000,'completed_at_ms':f.now*1000,
                'item':{'type':'CommandExecution','id':identity,'status':'completed','exit_code':0,
                        'command':['bash','-lc',command],'stdout':stdout}},at=f.now)
        f.write(fixture.skill(at=f.now-2),f.usage(),
                event('retained',self.command(output)),
                event('stderr',self.command()+' 2>/tmp/old-error.log',json.dumps(inline)))
        reader=ProvenanceIndex(fixture.cwo,discover=True)
        def scan():
            f.collector.scan();snapshot=f.collector.snapshot(now=f.now)
            snapshot['cwo_sessions']=fixture.cwo.scan(f.collector.db,snapshot,now=f.now)
            reviews=ReviewCollector(directories=[]).scan(now=f.now)
            self.assertEqual(reader.scan(f.collector.db,snapshot,reviews,now=f.now)['ready'],1)
            return reviews
        first=scan();self.assertEqual(len(first['reviews']),2)
        # Simulate the old reader having consumed all bytes and misread stderr.
        db=fixture.cwo.db
        row=db.execute("SELECT id,metadata FROM review_launches WHERE json_extract(metadata,'$.output') IS NULL").fetchone()
        metadata=json.loads(row['metadata']);metadata['output']='/tmp/old-error.log'
        db.execute("UPDATE review_launches SET metadata=?,projection=NULL,state='unreadable_output' WHERE id=?",
                   (json.dumps(metadata),row['id']))
        db.execute('DELETE FROM review_parser_version')
        output.unlink()
        reader=ProvenanceIndex(fixture.cwo,discover=True)
        replayed=scan()
        self.assertEqual(replayed['reviews'],first['reviews'])
        offsets=[tuple(row) for row in db.execute('SELECT path,offset FROM review_files')]
        reader=ProvenanceIndex(fixture.cwo,discover=True)
        self.assertEqual([tuple(row) for row in db.execute('SELECT path,offset FROM review_files')],offsets)
        self.assertEqual(scan()['reviews'],first['reviews'])


if __name__=='__main__':unittest.main()
