"""Opt-in ordinary Codex commands and backwards-compatible network enrichment."""
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from collect_codex_sessions import CodexSource
from traceonaut.cwo_command_usage import CommandCollector, render_command_usage_metrics
from traceonaut.command_network import WRITE_OUT
import test_cwo_command_usage as fixtures


class ScopeTests(unittest.TestCase):
    setUp=fixtures.CommandTests.setUp
    event=fixtures.CommandTests.event
    snapshot=fixtures.CommandTests.snapshot
    scan=fixtures.CommandTests.scan

    def test_all_scope_collects_without_cwo_association_and_returns_to_default(self):
        self.f.write(self.event(command=['curl','--data-binary','@/example/report.zip','https://example.test']))
        snapshot=self.snapshot(); snapshot.pop('cwo_sessions')
        self.assertEqual(self.scan(snapshot)['history']['rows'],[])
        self.commands=CommandCollector(self.cwo,history=True,scope='all')
        first=self.scan(snapshot)
        self.assertEqual(first['scan_ready'],1)
        self.assertEqual(first['scope'],'all')
        self.assertEqual(len(first['history']['rows']),1)
        self.assertEqual(len(self.scan(snapshot)['history']['rows']),1)
        self.assertIn('cwo_codex_cwo_shell_scope{scope="all"} 1',render_command_usage_metrics(first).decode())
        self.commands=CommandCollector(self.cwo,history=True)
        default=self.scan(snapshot)
        self.assertEqual(default['history']['rows'],[])
        self.assertEqual(default['calls'],[])
        self.assertNotIn('network_info{',render_command_usage_metrics(default).decode())
        self.commands=CommandCollector(self.cwo,history=True,scope='all')
        restored=self.scan(snapshot)
        self.assertEqual(restored['history']['rows'][0]['id'],first['history']['rows'][0]['id'])

    def test_all_scope_uses_creation_not_later_cwo_association_boundary(self):
        self.f.write(self.event('earlier',at=self.boundary-1),self.event('later'))
        self.commands=CommandCollector(self.cwo,history=True)
        self.assertEqual(len(self.scan()['history']['rows']),1)
        self.commands=CommandCollector(self.cwo,history=True,scope='all')
        self.assertEqual(len(self.scan()['history']['rows']),2)
        self.commands=CommandCollector(self.cwo,history=True)
        self.assertEqual(len(self.scan()['history']['rows']),1)

    def test_old_network_projection_upgrades_without_false_conflict(self):
        self.commands=CommandCollector(self.cwo,history=True)
        self.f.write(self.event(command=['curl','-o','/dev/null','-w',WRITE_OUT,'https://example.test'],
            stdout='TRACEONAUT_CONNECTION {"local_ip":"192.0.2.10"}\n'))
        first=self.scan()['history']['rows'][0]
        legacy=[{k:r[k] for k in ('command','destination','source_ip','source_ip_evidence','destination_kind')}
                for r in first['network']]
        digest=hashlib.sha256(json.dumps(legacy,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        with self.cwo.db:
            self.cwo.db.execute('UPDATE shell_runs SET network_projection=?,network_projection_version=NULL',(digest,))
            self.cwo.db.execute("UPDATE shell_watermark SET at=3 WHERE key='projection'")
        self.commands=CommandCollector(self.cwo,history=True)
        enriched=self.scan()['history']['rows'][0]
        self.assertEqual(enriched['id'],first['id'])
        self.assertEqual(enriched['timestamp'],first['timestamp'])
        self.assertEqual(enriched['conflict'],0)
        self.assertEqual(enriched['network'][0]['http_method'],'GET')
        self.assertEqual(self.scan()['history']['rows'][0],enriched)
        self.f.write(self.event(command=['curl','-o','/dev/null','-w',WRITE_OUT,'https://example.test'],
            stdout='TRACEONAUT_CONNECTION {"local_ip":"192.0.2.11"}\n'))
        self.assertEqual(self.scan()['history']['rows'][0]['timestamp'],0)

    def test_all_scope_requires_explicit_history(self):
        with self.assertRaises(ValueError):CommandCollector(self.cwo,scope='all')
        with self.assertRaises(ValueError):CommandCollector(self.cwo,history=True,scope='other')

    def test_all_scope_obeys_the_session_export_window_and_cap(self):
        self.f.write(self.event(command=['curl','https://example.test']))
        self.commands=CommandCollector(self.cwo,history=True,scope='all')
        snapshot=self.snapshot()
        original=snapshot['sessions'][0]
        now=self.f.now+1
        snapshot['session_export']={'evaluated_at':now,'retention_seconds':30*86400,'cap':1}
        self.assertEqual(len(self.scan(snapshot)['history']['rows']),1)
        # A newer metadata-only session occupies the current export slot. The
        # retained command from the older session must leave current export.
        snapshot['sessions'].append({**original,'session_id':'00000000-0000-0000-0000-000000000002',
            'created':now,'last_event':now})
        capped=self.scan(snapshot)
        self.assertEqual(capped['history']['rows'],[])
        self.assertEqual(capped['calls'],[])
        self.assertEqual([r['session_id'] for r in capped['sessions']],
                         ['00000000-0000-0000-0000-000000000002'])
        # Re-admission replays the retained source; identity stays deduplicated.
        snapshot['session_export']['cap']=2
        restored=self.scan(snapshot)
        self.assertEqual(len(restored['history']['rows']),1)
        self.assertEqual(len(self.scan(snapshot)['history']['rows']),1)
        snapshot['session_export']['evaluated_at']=now+31*86400
        expired=self.scan(snapshot)
        self.assertEqual(expired['history']['rows'],[])
        self.assertEqual(expired['calls'],[])
        self.assertEqual(expired['sessions'],[])

    def test_source_collects_ordinary_commands_without_scanning_cwo(self):
        self.f.write(self.event(command=['curl','-X','POST','--data-binary','@/example/report.zip','https://example.test']))
        args=SimpleNamespace(codex_home=self.f.home,session_state_dir=self.f.root/'source-state',
            session_retention_seconds=30*86400,session_export_cap=1000,cwo_sessions=False,
            cwo_command_history=True,command_history_scope='all',state_dir=None,account_snapshot_file=None)
        source=CodexSource(args,None,None)
        try:
            snapshot,payload,status=source.scan()
            self.assertNotIn('cwo_sessions',snapshot)
            self.assertEqual(status['cwo_shell_commands']['scope'],'all')
            self.assertEqual(len(snapshot['cwo_shell_commands']['history']['rows']),1)
            text=payload.decode()
            self.assertIn('http_method="POST"',text)
            self.assertIn('payload_files="/example/report.zip"',text)
            self.assertNotIn('cwo_codex_session_cwo_association_timestamp_seconds',text)
        finally:source.close()


if __name__=='__main__':unittest.main()
