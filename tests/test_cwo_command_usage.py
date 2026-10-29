"""Source-derived synthetic requests; no private arguments or transcripts."""
import copy
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from traceonaut.cwo_command_usage import (
    CommandCollector, FUTURE_RETRY_SECONDS, RETENTION_SECONDS,
    render_command_usage_metrics, request,
)
from traceonaut.cwo_session_telemetry import CwoSessionCollector
import test_codex_session_telemetry as fixtures


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.SessionCollectionTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        # CommandExecution timestamps are integer milliseconds. Avoid synthetic
        # sub-microsecond future events caused by a float ms/seconds round trip.
        self.f.now = int(self.f.now * 1000) / 1000
        self.cwo = CwoSessionCollector(self.f.home, self.f.state)
        self.addCleanup(lambda: self.cwo.close())
        self.commands = CommandCollector(self.cwo)
        self.boundary = self.f.now - 50

    def event(self, identity='call-one', name='git', at=None, command=None, **extra):
        at = self.f.now if at is None else at
        return self.f.event('event_msg', {'type': 'item_completed', 'thread_id': self.f.sid,
            'turn_id': 'turn-one', 'completed_at_ms': at * 1000, 'item': {
                'type': 'CommandExecution', 'id': identity, 'status': 'completed',
                'command': command if command is not None else [name, 'SECRET_ARG'], **extra}}, at=at)

    def snapshot(self):
        self.f.collector.scan()
        snap = self.f.collector.snapshot(now=self.f.now)
        snap['cwo_sessions'] = {'scan_ready': 1, 'associations': [
            {'session_id': self.f.sid, 'project_id': self.f.row()['project_id'], 'timestamp': self.boundary}]}
        return snap

    def scan(self, snapshot=None, **kw):
        return self.commands.scan(self.f.collector.db, snapshot or self.snapshot(), now=self.f.now, **kw)

    def test_independent_inventory_repeated_names_wrappers_and_raw_calls(self):
        events = [self.event(), self.event(), self.event('curl', 'curl'),
                  self.event('sequence', command=['bash', '-lc', 'git status; git diff | head']),
                  self.event('wrapped', command=['env', 'SECRET=value', 'sudo', '-n', 'curl', '-I', 'https://example.com']),
                  self.event('script', command=['python3', '-c', 'SECRET_PROGRAM'])]
        events += [self.f.event('response_item', {'type': 'function_call_output', 'call_id': 'call-one', 'output': 'SECRET_RESULT'}),
                   self.f.event('response_item', {'type': 'function_call', 'name': 'exec_command', 'call_id': 'call-one', 'arguments': '{}'})]
        self.f.write(*events)
        result = self.scan()
        # Independent expected counts, not calculated through the product parser.
        expected = {'git': 3, 'curl': 2, 'head': 1, 'python3': 1}
        actual = {name: sum(r['command'] == name for r in result['calls']) for name in expected}
        self.assertEqual(actual, expected)
        self.assertEqual(len(result['calls']), 7)
        self.assertEqual(result['source_gaps'], 0)
        self.assertEqual(result['sessions'][0]['complete'], 1)
        self.assertEqual(result['scan_ready'], 1)
        payload = render_command_usage_metrics(result).decode()
        for forbidden in ('SECRET', 'tools.fake', '/private', 'arguments', 'output='):
            self.assertNotIn(forbidden, payload)
        self.cwo.db.execute('PRAGMA wal_checkpoint(FULL)')
        self.assertNotIn(b'SECRET', self.cwo.path.read_bytes())

    def test_restart_replay_copy_late_output_and_append(self):
        self.f.write(self.event())
        first = self.scan()
        offsets = [tuple(r) for r in self.f.collector.db.execute('SELECT path,offset FROM files')]
        self.cwo.close(); self.cwo = CwoSessionCollector(self.f.home, self.f.state)
        self.commands = CommandCollector(self.cwo)
        self.assertEqual(self.scan()['calls'], first['calls'])
        self.assertEqual([tuple(r) for r in self.f.collector.db.execute('SELECT path,offset FROM files')], offsets)
        copied = self.f.path.with_name('rollout-copy-' + self.f.sid + '.jsonl')
        copied.write_bytes(self.f.path.read_bytes())
        self.f.write(self.f.event('response_item', {'type': 'function_call_output', 'call_id': 'call-one', 'output': 'ok'}), self.event('two'))
        self.assertEqual(len(self.scan()['calls']), 2)
        self.assertEqual(len(self.scan()['calls']), 2)

    def test_boundary_rewind_and_pre_creation_fork_history(self):
        self.f.write(self.event('old', at=self.boundary - 10), self.event('current'))
        self.assertEqual(len(self.scan()['calls']), 1)
        self.boundary -= 20
        self.assertEqual(len(self.scan()['calls']), 2)
        self.f.write(self.event('fork', at=self.f.now - 500))
        self.assertEqual(len(self.scan()['calls']), 2)
        snap = self.snapshot(); snap['sessions'][0]['created'] = 0
        broken = self.scan(snap)
        self.assertEqual(broken['scan_ready'], 0)
        self.assertGreater(broken['source_errors'], 0)

    def test_conflict_withdraws_original_name_and_time(self):
        self.f.write(self.event())
        first = self.scan()['calls'][0]
        self.f.write(self.event(name='different'))
        result = self.scan()
        self.assertEqual(result['calls'][0]['id'], first['id'])
        self.assertEqual(result['calls'][0]['command'], 'git')
        self.assertEqual(result['calls'][0]['timestamp'], 0)
        self.assertEqual(result['source_gaps'], 1)
        self.assertEqual(result['sessions'][0]['complete'], 0)

    def test_changed_command_set_withdraws_every_original_occurrence(self):
        self.f.write(self.event(command=['bash', '-lc', 'git status; curl x; git diff']))
        first = self.scan()
        self.assertEqual(len(first['calls']), 3)
        self.f.write(self.event(command=['git', 'status']))
        result = self.scan()
        self.assertEqual(len(result['calls']), 3)
        self.assertTrue(all(r['timestamp'] == 0 for r in result['calls']))
        self.assertEqual({r['id'] for r in first['calls']}, {r['id'] for r in result['calls']})

    def test_owner_timestamp_and_missing_source_coverage(self):
        foreign = self.event(); foreign['payload']['thread_id'] = str(uuid4())
        missing = self.event('missing'); del missing['payload']['thread_id']
        invalid = self.event('invalid'); invalid['payload']['completed_at_ms'] = float('nan')
        self.f.write(foreign, missing, invalid, self.event('valid'))
        result = self.scan()
        self.assertEqual(len(result['calls']), 1)
        self.assertEqual(result['source_gaps'], 2)
        snapshot = self.snapshot()
        self.f.collector.db.execute('DELETE FROM files')
        self.assertEqual(self.scan(snapshot)['scan_ready'], 0)

    def test_cap_retention_and_unassociated_calls(self):
        self.f.write(*(self.event(str(i), at=self.f.now - i) for i in range(5)))
        result = self.scan(cap=3)
        self.assertEqual(len(result['calls']), 3)
        self.assertEqual(result['limit_reached'], 1)
        self.assertTrue(all(r['timestamp'] > 0 for r in result['calls']))
        self.assertEqual(self.scan(cap=3)['limit_reached'], 1)
        self.f.now += RETENTION_SECONDS + 1
        self.assertEqual(self.scan()['calls'], [])

    def test_conflict_survives_cap_and_overflow_has_history_floor(self):
        self.f.write(self.event('a', at=self.f.now-2))
        self.scan(cap=1)
        self.f.write(self.event('a', name='changed', at=self.f.now-2), self.event('b', at=self.f.now-1))
        result = self.scan(cap=1)
        self.assertEqual(len(result['calls']), 1)
        self.assertEqual(result['calls'][0]['timestamp'], 0)
        self.assertEqual(result['limit_reached'], 1)
        self.f.write(self.event('c'), self.event('c', name='changed'))
        result = self.scan(cap=1)
        self.assertAlmostEqual(result['history_floor_timestamp_seconds'], self.f.now-2, places=5)
        self.assertEqual(result['calls'][0]['timestamp'], 0)

    def test_conflict_after_export_eviction_and_index_bound(self):
        self.f.write(self.event('a', at=self.f.now-2))
        self.scan(cap=1)
        self.f.write(self.event('b', at=self.f.now-1))
        self.scan(cap=1)
        self.f.write(self.event('a', name='changed'))
        result = self.scan(cap=1)
        self.assertEqual(result['calls'][0]['timestamp'], 0)
        with mock.patch('traceonaut.cwo_command_usage.INDEX_CAP', 1):
            result = self.scan(cap=1)
        self.assertEqual(self.cwo.db.execute('SELECT count(*) FROM shell_calls').fetchone()[0], 1)
        self.assertGreater(result['history_floor_timestamp_seconds'], 0)

    def test_reordered_keys_and_blank_identity(self):
        event = self.event()
        event['payload']['item'] = {'aggregated_output': 'x'*2000, 'command': ['git'], 'id': 'reordered', 'type': 'CommandExecution'}
        self.f.write(event, self.event('   '))
        result = self.scan()
        self.assertEqual(len(result['calls']), 1)
        self.assertEqual(result['source_gaps'], 1)

    def test_conflict_replay_after_index_eviction_keeps_run_identity(self):
        self.f.write(self.event('old', at=self.f.now-2),
                     self.event('old', name='changed', at=self.f.now-2),
                     self.event('new', at=self.f.now-1))
        with mock.patch('traceonaut.cwo_command_usage.INDEX_CAP', 1):
            self.scan(cap=1)
            self.f.write(self.event('old', at=self.f.now-2))
            result = self.scan(cap=1)
        self.assertEqual(len(result['calls']), 1)
        self.assertEqual(result['calls'][0]['timestamp'], 0)
        self.assertEqual(self.cwo.db.execute('SELECT count(*) FROM shell_runs').fetchone()[0], 1)
        self.assertEqual(self.cwo.db.execute('SELECT count(*) FROM shell_calls').fetchone()[0], 1)

    def test_oversized_reordered_command_marks_partial(self):
        event = self.event()
        event['payload']['item'] = {'aggregated_output': 'x'*2048,
            'command': ['git'], 'id': 'large', 'type': 'CommandExecution'}
        self.f.write(event, self.event('after'))
        with mock.patch('traceonaut.cwo_command_usage.MAX_LINE', 1024):
            result = self.scan()
        self.assertEqual(result['source_gaps'], 1)
        self.assertEqual(result['sessions'][0]['complete'], 0)
        self.assertEqual(len(result['calls']), 1)

    def test_invalid_unsupported_future_and_names_are_visible(self):
        invalid = self.event('missing-time'); invalid['payload']['completed_at_ms'] = None
        events = [self.event('x', name='$DYNAMIC'), self.event('', name='git'),
                  self.event('future', at=self.f.now + FUTURE_RETRY_SECONDS + 1),
                  self.event('loop', command=['bash', '-lc', 'for x in a; do curl x; done']), invalid]
        self.f.write(*events)
        result = self.scan()
        self.assertEqual(result['calls'], [])
        self.assertEqual(result['source_gaps'], 5)
        self.assertEqual(result['sessions'][0]['complete'], 0)
        self.assertIsNone(request({'type': 'response_item', 'payload': {'type': 'function_call'}}, self.f.sid))

    def test_near_future_completion_is_retried_without_duplicate(self):
        self.commands = CommandCollector(self.cwo, history=True)
        self.f.write(self.event('future', at=self.f.now + 2), self.event('after', at=self.f.now + 2))
        early = self.scan()
        self.assertEqual(early['calls'], [])
        self.assertEqual(early['source_gaps'], 0)
        self.assertEqual(early['pending_files'], 1)
        self.cwo.close()
        self.cwo = CwoSessionCollector(self.f.home, self.f.state)
        self.commands = CommandCollector(self.cwo, history=True)
        self.f.now += 2
        current = self.scan()
        self.assertEqual(len(current['calls']), 2)
        self.assertEqual(len(current['history']['rows']), 2)
        self.assertEqual(current['source_gaps'], 0)
        self.assertEqual(current['pending_files'], 0)
        self.assertEqual(current['sessions'][0]['complete'], 1)
        self.assertEqual(self.scan()['calls'], current['calls'])

    def test_far_future_completion_does_not_block_later_records(self):
        self.f.write(self.event('far-future', at=self.f.now + FUTURE_RETRY_SECONDS + 1), self.event('current'))
        result = self.scan()
        self.assertEqual(len(result['calls']), 1)
        self.assertEqual(result['source_gaps'], 1)
        self.assertEqual(result['pending_files'], 0)

    def test_partial_append_and_bounded_reads(self):
        line = json.dumps(self.event())
        with self.f.path.open('a') as stream: stream.write(line)
        self.assertEqual(self.scan()['pending_files'], 1)
        with self.f.path.open('a') as stream: stream.write('\n')
        result = self.scan()
        self.assertEqual(len(result['calls']), 1)
        self.assertEqual(result['pending_files'], 0)
        self.f.write(*(self.event(str(i)) for i in range(20)))
        for _ in range(30):
            result = self.scan(byte_budget=1024, per_file_budget=1024)
            if not result['pending_files']: break
        self.assertEqual(len(result['calls']), 21)
        self.assertEqual(result['pending_files'], 0)

    def test_source_replacement_oversized_and_recovery(self):
        self.f.write(self.event())
        self.scan()
        before = self.f.path.stat()
        content = self.f.path.read_bytes().replace(b'call-one', b'call-two')
        self.f.path.write_bytes(content); os.utime(self.f.path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(len(self.scan()['calls']), 2)
        self.f.write(self.event('large', aggregated_output='x' * 2048), self.event('after'))
        with mock.patch('traceonaut.cwo_command_usage.MAX_LINE', 1024):
            result = self.scan()
        self.assertEqual(result['source_gaps'], 1)
        self.assertEqual(len(result['calls']), 3)
        self.assertNotIn('large', render_command_usage_metrics(result).decode())

    def test_oversized_outputs_do_not_create_false_tool_gaps(self):
        self.f.write(self.f.event('response_item', {'type': 'function_call_output', 'call_id': 'one', 'output': 'x'*2048}),
                     self.event('after'))
        with mock.patch('traceonaut.cwo_command_usage.MAX_LINE', 1024):
            result = self.scan()
        self.assertEqual(result['source_gaps'], 0)
        self.assertEqual(len(result['calls']), 1)

    def test_empty_unavailable_old_snapshot_and_unassociated(self):
        result = self.scan()
        self.assertEqual(result['calls'], [])
        self.assertEqual(result['sessions'][0]['complete'], 1)
        self.f.write(self.event())
        snap = self.snapshot(); snap['cwo_sessions']['associations'] = []
        self.assertEqual(self.scan(snap)['calls'], [])
        snap = self.snapshot(); snap['source_available'] = 0
        self.assertEqual(self.scan(snap)['scan_ready'], 0)
        from collect_codex_sessions import codex_payload
        old = self.f.collector.snapshot(now=self.f.now)
        self.assertNotIn(b'cwo_codex_cwo_shell_', codex_payload(old))


if __name__ == '__main__':
    unittest.main()
