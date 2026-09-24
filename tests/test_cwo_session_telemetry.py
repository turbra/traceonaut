"""Synthetic CWO context association; never use user transcripts as fixtures."""
import copy
import json
import os
from pathlib import Path
import sys
import subprocess
import unittest
from uuid import uuid4
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from traceonaut.cwo_session_telemetry import CwoSessionCollector, direct_tool, render_cwo_session_metrics, skill_block
import test_codex_session_telemetry as session_fixture


class DetectionTests(unittest.TestCase):
    def test_direct_tool_not_mentions_wrappers_or_shell_programs(self):
        command=['python3','/opt/complex-work-orchestration/scripts/run_checked_command.py','/private/spec.json']
        self.assertEqual(direct_tool(command),'run_checked_command')
        self.assertEqual(direct_tool(['/bin/bash','-lc',' '.join(command)]),'run_checked_command')
        self.assertEqual(direct_tool(['/bin/bash','-lc',' '.join(command)+'; cat /private/result.json']),'run_checked_command')
        for bad in [command[1:],['cat',command[1]],['echo',*command],['python3','-c','print("'+command[1]+'")'],
                    ['/bin/bash','-lc','echo "'+' '.join(command)+'"'],['bash','-lc','false && '+' '.join(command)],
                    ['bash','-lc',"cat <<EOF\n"+' '.join(command)+'\nEOF'],['bash','-lc',';'.join(['true',' '.join(command)])],
                    ['python3','/opt/other/scripts/run_checked_command.py'],['python3','/opt/complex-work-orchestration/scripts/unknown.py'],
                    ['python3','$CWO/scripts/run_checked_command.py'],None, ' '.join(command),['python3',{}]]:
            self.assertIsNone(direct_tool(bad),bad)

    def test_exact_skill_envelope_not_conversation_or_tool_output(self):
        text='<skill>\n<name>complex-work-orchestration</name>\n<path>/opt/complex-work-orchestration/SKILL.md</path>\nPrivate skill instructions\n</skill>'
        payload={'type':'message','role':'user','content':[{'type':'input_text','text':text}]}
        self.assertTrue(skill_block(payload))
        for changes in [{'role':'assistant'},{'content':[{'type':'output_text','text':text}]},{'content':[{'type':'input_text','text':'Example: '+text}]},{'content':[{'type':'input_text','text':'Use CWO please'}]}]:
            self.assertFalse(skill_block({**payload,**changes}))


class CwoCollectionTests(unittest.TestCase):
    def setUp(self):
        self.f=session_fixture.SessionCollectionTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.cwo=CwoSessionCollector(self.f.home,self.f.state);self.addCleanup(lambda:self.cwo.close())

    def skill(self,at=None):
        return self.f.event('response_item',{'type':'message','role':'user','content':[{'type':'input_text','text':'<skill>\n<name>complex-work-orchestration</name>\n<path>/opt/complex-work-orchestration/SKILL.md</path>\nSECRET_INSTRUCTIONS\n</skill>'}]},at=at)

    def command(self,identity='one',sid=None,at=None,exit_code=0):
        at=self.f.now if at is None else at
        return self.f.event('event_msg',{'type':'item_completed','thread_id':sid or self.f.sid,'turn_id':'turn-1','completed_at_ms':at*1000,
            'item':{'type':'CommandExecution','id':identity,'status':'completed' if exit_code==0 else 'failed','exit_code':exit_code,
                    'command':['/bin/bash','-lc','python3 /opt/complex-work-orchestration/scripts/run_checked_command.py /private/spec.json'],
                    'duration':{'secs':2,'nanos':500000000},'aggregated_output':'SECRET_OUTPUT'}},at=at)

    def scan(self,**kwargs):
        self.f.collector.scan();snap=self.f.collector.snapshot()
        return self.cwo.scan(self.f.collector.db,snap,**kwargs)

    def test_owned_skill_and_completed_commands_replay_restart_privacy(self):
        event=self.command();self.f.write(self.skill(),event,event,self.f.usage())
        result=self.scan()
        self.assertEqual(len(result['associations']),1);self.assertEqual(len(result['commands']),1)
        self.assertEqual(result['commands'][0]['duration'],2.5);self.assertEqual(result['scan_ready'],1)
        payload=render_cwo_session_metrics(result).decode()
        self.assertNotIn('SECRET',payload);self.assertNotIn('/private',payload);self.assertNotIn('/opt',payload)
        self.cwo.close();self.cwo=CwoSessionCollector(self.f.home,self.f.state)
        self.assertEqual(len(self.scan()['commands']),1)
        raw=self.cwo.path.read_bytes()
        self.assertNotIn(b'SECRET',raw);self.assertNotIn(b'/private/spec',raw)
        self.assertEqual(self.f.row()['usage']['total'],30)

    def test_backfill_never_resets_usage_or_turn_cursors(self):
        self.f.write(self.skill(),self.command(),self.f.usage());self.f.collector.scan()
        old=[tuple(r) for r in self.f.collector.db.execute('select path,offset from files')]
        for _ in range(10):
            result=self.cwo.scan(self.f.collector.db,self.f.collector.snapshot(),byte_budget=1024,per_file_budget=1024)
            if not result['pending_files']:break
        self.assertEqual(result['pending_files'],0)
        self.assertEqual([tuple(r) for r in self.f.collector.db.execute('select path,offset from files')],old)
        self.assertEqual(self.f.row()['usage']['total'],30)

    def test_partial_append_retried_and_single_writer(self):
        with self.assertRaisesRegex(ValueError,'already running'):CwoSessionCollector(self.f.home,self.f.state)
        raw=json.dumps(self.skill())
        with self.f.path.open('a') as stream:stream.write(raw)
        result=self.scan();self.assertEqual(result['pending_files'],1);self.assertEqual(result['associations'],[])
        with self.f.path.open('a') as stream:stream.write('\n')
        self.assertEqual(len(self.scan()['associations']),1)

    def test_foreign_and_pre_creation_records_cannot_classify_child(self):
        self.f.write(self.skill(at=self.f.now-200),self.command(sid=str(uuid4())))
        self.assertEqual(self.scan()['associations'],[])

    def test_parent_propagation_creation_boundary_and_cycle(self):
        self.f.write(self.skill(at=self.f.now-50))
        before,after,grandchild=[str(uuid4()) for _ in range(3)]
        for sid,parent,at in [(before,self.f.sid,self.f.now-60),(after,self.f.sid,self.f.now-40),(grandchild,after,self.f.now-30)]:
            self.f.write(self.f.event('session_meta',{'id':sid,'cwd':'/workspace/another','timestamp':at,
                'source':{'subagent':{'thread_spawn':{'parent_thread_id':parent,'depth':1}}}},at=at),path=self.f.home/'sessions'/('rollout-'+sid+'.jsonl'))
        result=self.scan();associated={r['session_id']:r for r in result['associations']}
        self.assertNotIn(before,associated)
        self.assertEqual(associated[after]['source'],'parent_session');self.assertIn(grandchild,associated)
        self.f.collector.db.execute('update sessions set kind=? where id=?',('internal',grandchild));self.f.collector.db.commit()
        self.assertNotIn(grandchild,{r['session_id'] for r in self.scan()['associations']})
        self.f.collector.db.execute('update sessions set kind=? where id=?',('subagent',grandchild));self.f.collector.db.commit()
        self.f.collector.db.execute('update sessions set parent_id=? where id=?',(grandchild,self.f.sid));self.f.collector.db.commit()
        self.assertEqual(len(self.scan()['associations']),3)

    def test_new_source_rotation_and_same_mtime_rewrite_are_seen(self):
        self.f.write(self.skill());self.scan()
        self.f.path.rename(self.f.path.with_name('rollout-copy-'+self.f.sid+'.jsonl'))
        self.f.path=self.f.path.with_name('rollout-copy-'+self.f.sid+'.jsonl')
        self.assertEqual(len(self.scan()['associations']),1)
        event=self.command();self.f.write(event);self.scan()
        before=self.f.path.stat();content=self.f.path.read_bytes()
        replaced=content.replace(b'"id": "one"',b'"id": "two"')
        self.assertEqual(len(content),len(replaced));self.f.path.write_bytes(replaced);os.utime(self.f.path,ns=(before.st_atime_ns,before.st_mtime_ns))
        self.assertEqual(len(self.scan()['commands']),2)

    def test_conflicts_and_bounds_are_visible(self):
        self.f.write(self.command(),self.command(exit_code=1));result=self.scan()
        self.assertEqual(result['commands'],[]);self.assertEqual(result['source_gaps'],1);self.assertEqual(result['scan_ready'],0)
        self.f.write(self.command('two',at=self.f.now+1),self.command('three',at=self.f.now+2))
        with mock.patch('traceonaut.cwo_session_telemetry.COMMAND_CAP',1):
            result=self.scan();self.assertEqual(result['limit_reached'],1);self.assertEqual(len(result['commands']),1)
        with mock.patch('traceonaut.cwo_session_telemetry.MAX_FILES',0):
            self.assertEqual(self.scan()['limit_reached'],1)

    def test_no_cwo_signal_and_source_disappearance(self):
        self.f.write(self.f.usage());result=self.scan()
        self.assertEqual(result['associations'],[]);self.assertEqual(result['scan_ready'],1)
        self.f.path.unlink();result=self.cwo.scan(self.f.collector.db,self.f.collector.snapshot())
        self.assertEqual(result['source_errors'],1);self.assertEqual(result['scan_ready'],0)

    def test_cli_feature_is_optional_and_state_paths_are_protected(self):
        self.f.write(self.skill(),self.command())
        self.cwo.close();self.f.collector.close()
        args=[sys.executable,str(ROOT/'scripts/collect_codex_sessions.py'),'--codex-home',str(self.f.home),
              '--session-state-dir',str(self.f.state),'--snapshot-file',str(self.f.root/'snapshot.json'),'--once','--cwo-sessions']
        try:
            result=subprocess.run(args,capture_output=True,text=True,check=True)
            self.assertEqual(json.loads(result.stdout)['cwo_sessions']['associated_sessions'],1)
            bad=args.copy();bad[bad.index('--snapshot-file')+1]=str(self.f.state/'cwo-sessions.sqlite3')
            self.assertEqual(subprocess.run(bad,capture_output=True).returncode,2)
        finally:
            self.f.collector=session_fixture.SessionCollector(self.f.home,self.f.state)
            self.cwo=CwoSessionCollector(self.f.home,self.f.state)

    def test_future_signal_waits_until_its_source_time(self):
        self.f.write(self.skill(at=self.f.now+100))
        result=self.scan(now=self.f.now)
        self.assertEqual(result['associations'],[]);self.assertEqual(result['scan_ready'],0)
        result=self.scan(now=self.f.now+200)
        self.assertEqual(len(result['associations']),1)

    def test_malformed_candidates_and_oversized_commands_report_gaps(self):
        bad=self.skill();bad['timestamp']='invalid'
        command=self.command();command['payload']['item']['status']='unrecognized'
        self.f.write(bad,command)
        result=self.scan()
        self.assertEqual(result['source_gaps'],2)
        self.assertEqual(result['associations'],[])
        huge=self.command('huge');huge['payload']['item']['aggregated_output']='x'*20000
        ordinary=copy.deepcopy(huge);ordinary['payload']['item']['command']=['echo','ordinary']
        self.f.write(ordinary,huge)
        with mock.patch('traceonaut.cwo_session_telemetry.MAX_LINE',8192):
            result=self.scan()
        self.assertEqual(result['source_gaps'],3)
        self.assertEqual(result['pending_files'],0)

    def test_source_symlink_and_nonprivate_state_are_rejected(self):
        self.f.write(self.skill());self.f.collector.scan()
        target=self.f.path.with_name('saved.jsonl');self.f.path.rename(target)
        self.f.path.symlink_to(target)
        result=self.cwo.scan(self.f.collector.db,self.f.collector.snapshot())
        self.assertEqual(result['source_errors'],1)
        self.assertEqual(result['associations'],[])
        self.cwo.close()
        try:
            self.cwo.path.chmod(0o644)
            with self.assertRaisesRegex(ValueError,'private'):
                CwoSessionCollector(self.f.home,self.f.state)
        finally:
            self.cwo.path.chmod(0o600)
            self.cwo=CwoSessionCollector(self.f.home,self.f.state)


if __name__=='__main__':unittest.main()
