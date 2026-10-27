"""Opt-in command history, action backfill and presentation boundaries."""
import json
import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from traceonaut.cwo_command_usage import CommandCollector, render_command_usage_metrics
from render_codex_sessions_dashboard import validate_snapshot, load_snapshot
from render_observability_dashboard import render_dashboard, write_dashboard
import test_cwo_command_usage as fixtures

ROOT = Path(__file__).resolve().parents[1]


class HistoryTests(unittest.TestCase):
    setUp = fixtures.CommandTests.setUp
    event = fixtures.CommandTests.event
    snapshot = fixtures.CommandTests.snapshot
    scan = fixtures.CommandTests.scan

    def enable(self):
        self.commands = CommandCollector(self.cwo, history=True)

    def test_cli_requires_cwo_and_codex_sources(self):
        from collect_codex_sessions import main
        for flags, message in (
            (['--cwo-command-history'], '--cwo-command-history requires --cwo-sessions'),
            (['--cwo-command-history', '--cwo-sessions'], 'command history requires --codex-home'),
            (['--command-history-scope', 'all'], '--command-history-scope all requires --command-history'),
            (['--command-history', '--command-history-scope', 'all'], 'command history requires --codex-home'),
        ):
            with self.subTest(flags=flags), contextlib.redirect_stderr(io.StringIO()) as output:
                with self.assertRaises(SystemExit) as error:
                    main(['--bob-home', '/unused/bob', '--bob-snapshot-file', '/unused/snapshot',
                          '--session-state-dir', '/unused/state', '--once', *flags], require_codex=False)
                self.assertEqual(error.exception.code, 2)
                self.assertIn(message, output.getvalue())

    def test_opt_in_backfills_without_duplicate_counts_and_disable_clears_details(self):
        self.f.write(self.event(command=['git','commit','-m','SECRET'], cwd='/workspace/example',
                               status='completed',exit_code=0,duration={'secs':1,'nanos':0}))
        first = self.scan()
        self.assertEqual(first['calls'][0]['action'],'git commit')
        self.assertFalse(first['history']['enabled'])
        self.assertEqual(self.cwo.db.execute('SELECT count(*) FROM shell_history').fetchone()[0],0)
        self.enable(); enabled = self.scan()
        self.assertEqual(len(enabled['calls']),1); self.assertEqual(len(enabled['history']['rows']),1)
        self.assertEqual(enabled['history']['rows'][0]['summary'],'git commit [arguments omitted]')
        payload = render_command_usage_metrics(enabled).decode()
        self.assertIn('action="git commit"',payload)
        self.assertNotIn('SECRET',payload)
        self.assertIn('recorded_directory="/workspace/example"',payload)
        self.assertIn('command_description="git commit [arguments omitted]"',payload)
        self.assertIn('invocation_exit_code',payload)
        self.assertEqual(self.scan()['history'],enabled['history'])
        self.commands = CommandCollector(self.cwo, history=False)
        off = self.scan()
        self.assertFalse(off['history']['enabled']); self.assertEqual(off['history']['rows'],[])
        self.assertNotIn('cwo_codex_cwo_shell_invocation_info{',render_command_usage_metrics(off).decode())
        self.assertNotIn('cwo_codex_cwo_shell_network_info{',render_command_usage_metrics(off).decode())
        self.assertEqual(self.cwo.db.execute('SELECT count(*) FROM shell_history').fetchone()[0],0)
        self.cwo.db.execute('PRAGMA wal_checkpoint(FULL)')
        self.assertNotIn(b'SECRET',self.cwo.path.read_bytes())

    def test_old_projection_migration_populates_actions_and_history(self):
        self.f.write(self.event(command=['git','push']))
        first = self.scan()
        with self.cwo.db:
            self.cwo.db.execute('ALTER TABLE shell_calls DROP COLUMN action')
            self.cwo.db.execute('ALTER TABLE shell_runs DROP COLUMN projection')
            self.cwo.db.execute("DELETE FROM shell_watermark WHERE key='projection'")
        self.enable(); result = self.scan()
        self.assertEqual(len(result['calls']),1)
        self.assertEqual(result['calls'][0]['id'],first['calls'][0]['id'])
        self.assertEqual(result['calls'][0]['action'],'git push')
        self.assertEqual(result['history']['rows'][0]['summary'],'git push')

    def test_changed_arguments_or_metadata_withdraw_original_action_and_detail(self):
        self.enable()
        self.f.write(self.event(command=['git','status']))
        first = self.scan()
        self.f.write(self.event(command=['git','push','SECRET']))
        result = self.scan()
        self.assertEqual(result['calls'][0]['action'],'git status')
        self.assertEqual(result['calls'][0]['timestamp'],0)
        self.assertEqual(result['history']['rows'][0]['timestamp'],0)
        self.assertIsNone(result['history']['rows'][0]['summary'])
        self.assertNotIn('SECRET',json.dumps(result))
        self.assertNotIn('cwo_codex_cwo_shell_invocation_info{',render_command_usage_metrics(result).decode())

    def test_history_is_one_row_per_invocation_and_bounded(self):
        self.enable()
        self.f.write(self.event(command=['bash','-lc','false && git push; tail -n 4 app.log'],
                               status='completed',exit_code=0,duration={'secs':2,'nanos':0}))
        result = self.scan()
        self.assertEqual(len(result['calls']),3); self.assertEqual(len(result['history']['rows']),1)
        self.assertIn('&& git push',result['history']['rows'][0]['summary'])
        self.f.write(self.event('new',command=['cat','file.txt']))
        with mock.patch('traceonaut.cwo_command_usage.HISTORY_CAP',1):
            result = self.scan()
        self.assertEqual(len(result['history']['rows']),1)

    def test_renderer_opt_in_prometheus_without_embedded_details(self):
        self.enable(); self.f.write(self.event(command=['tail','-n','10','app.log'],cwd='/workspace/example'))
        data = self.scan(); snapshot = self.snapshot(); snapshot['cwo_shell_commands'] = data
        self.assertNotIn('command_history', validate_snapshot(snapshot))
        projected = validate_snapshot(snapshot,include_command_history=True)
        template = json.loads((ROOT/'examples/observability/cwo-overview.json').read_text())
        registry = {'version':1,'projects':{},'dispatches':{}}
        rendered = render_dashboard(template,registry,session_snapshot=projected)
        panel = next(p for p in rendered['panels'] if p['id']==413)
        text = json.dumps(panel)
        self.assertNotIn('tail -n 10 app.log',text); self.assertNotIn('/workspace/example',text)
        self.assertIn('Not recorded',text); self.assertNotIn('__TRACEONAUT_COMMAND_IDS__',text)
        self.assertNotIn('tail -n 10 app.log',json.dumps(panel['targets']))
        bound = render_dashboard(template,registry,session_snapshot=projected,
            datasource_uid='prometheus')
        self.assertEqual(next(p for p in bound['panels'] if p['id']==414)['datasource']['uid'],'prometheus')
        self.assertNotIn('infinity',json.dumps(bound))
        self.assertNotIn('DS_COMMAND_HISTORY',json.dumps(bound))
        other = json.loads(json.dumps(projected))
        other['command_history']['rows'] = []
        empty = render_dashboard(template,registry,session_snapshot=other,datasource_uid='prometheus')
        for identity in (413,414):
            self.assertEqual(next(p for p in bound['panels'] if p['id']==identity),
                             next(p for p in empty['panels'] if p['id']==identity))
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            path = Path(folder)/'dashboard.json'
            write_dashboard(path,rendered)
            self.assertEqual(path.stat().st_mode & 0o777,0o640)
            os.chmod(path,0o644)
            self.assertTrue(write_dashboard(path,rendered))
            self.assertEqual(path.stat().st_mode & 0o777,0o640)
            source = Path(folder)/'snapshot.json'; source.write_text(json.dumps(snapshot)); source.chmod(0o600)
            self.assertEqual(load_snapshot(source,include_command_history=True)['command_history'],projected['command_history'])
        off = render_dashboard(template,registry,session_snapshot=validate_snapshot(snapshot))
        self.assertFalse(any(p['id']==413 for p in off['panels']))
        self.assertFalse(any(p['id']==414 for p in off['panels']))
        self.assertNotIn('yesoreyeram-infinity-datasource',json.dumps(off))
        self.assertNotIn('/workspace/example',json.dumps(off))

    def test_network_backfill_privacy_replay_and_conflicting_report(self):
        from traceonaut.command_network import WRITE_OUT
        command = ['curl','-o','/dev/null','-w',WRITE_OUT,'https://user:SECRET@example.test/private?key=SECRET']
        self.enable()
        self.f.write(self.event(command=command,stdout='TRACEONAUT_CONNECTION {"local_ip":"192.0.2.10"}\n'))
        first=self.scan()
        network=first['history']['rows'][0]['network']
        self.assertEqual(network[0]['destination'],'https://example.test')
        self.assertEqual(network[0]['source_ip'],'192.0.2.10')
        self.assertEqual(self.scan()['history'],first['history'])
        self.assertNotIn('SECRET',json.dumps(first))
        payload=render_command_usage_metrics(first).decode()
        self.assertIn('destination="https://example.test"',payload)
        self.assertIn('source_ip="192.0.2.10"',payload)
        self.assertNotIn('SECRET',payload)
        self.f.write(self.event(command=command,stdout='TRACEONAUT_CONNECTION {"local_ip":"192.0.2.11"}\n'))
        changed=self.scan()['history']['rows'][0]
        self.assertEqual(changed['timestamp'],0)
        self.assertEqual(changed['network'],[])

    def test_new_source_record_reaches_metrics_without_rerender(self):
        from traceonaut.command_network import WRITE_OUT
        self.enable()
        self.f.write(self.event(command=['git','status']))
        first=render_command_usage_metrics(self.scan()).decode()
        self.assertIn('command_description="git status"',first)
        self.assertNotIn('cwo_codex_cwo_shell_network_info{',first)
        self.f.write(self.event('new-network',command=['curl','-o','/dev/null','-w',WRITE_OUT,'https://example.test'],
            stdout='TRACEONAUT_CONNECTION {"local_ip":"192.0.2.10"}\n',exit_code=0))
        second=render_command_usage_metrics(self.scan()).decode()
        self.assertEqual(second.count('\ncwo_codex_cwo_shell_invocation_info{'),2)
        self.assertIn('destination="https://example.test"',second)
        self.assertIn('source_ip="192.0.2.10"',second)
        self.assertEqual(render_command_usage_metrics(self.scan()).decode(),second)

    def test_metadata_unicode_names_unknown_ip_and_network_cap(self):
        self.enable()
        self.f.write(self.event(command=['bash','-lc','curl https://example.test/a; curl https://other.test/b']))
        snapshot=self.snapshot()
        snapshot['sessions'][0]['title']='Release café "preview"'
        snapshot['sessions'][0]['project_name']='Example'
        result=self.scan(snapshot)
        with mock.patch('traceonaut.cwo_command_usage.NETWORK_CAP',1):
            capped=self.scan(snapshot)
            payload=render_command_usage_metrics(capped).decode()
        self.assertEqual(capped['limit_reached'],1)
        self.assertEqual(payload.count('\ncwo_codex_cwo_shell_network_info{'),1)
        self.assertIn('session_name="Release café \\"preview\\""',payload)
        self.assertNotIn('\\u00e9',payload)
        self.assertIn('source_ip="Not recorded"',payload)
        self.assertEqual(len(result['history']['rows'][0]['network']),2)


if __name__ == '__main__':
    unittest.main()
