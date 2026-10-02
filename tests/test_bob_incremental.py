"""Incremental Bob scans must agree with a fresh, complete source read."""

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from traceonaut.bob_session_telemetry import BobCollector, render_bob_metrics
from bob_fixtures import database, message, task


class BobIncrementalTests(unittest.TestCase):
    now = 1_800_000_000

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'bob'
        self.path = self.home / 'db/bob.db'
        self.writer = database(self.path, [task()], [message()])
        self.addCleanup(self.writer.close)
        self.writer.execute('PRAGMA journal_mode=WAL')
        self.state = self.root / 'state'
        self.clock = 1000.0
        self.clock_patch = patch('traceonaut.bob_session_telemetry.time.monotonic',
                                 side_effect=lambda: self.clock)
        self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)
        self.comparisons = 0

    def reader(self, state=None, **kwargs):
        reader = BobCollector(self.home, state or self.state, **kwargs)
        self.addCleanup(reader.close)
        return reader

    def insert(self, table, row, *, rowid=None):
        if rowid is not None:
            row = {'rowid': rowid, **row}
        self.writer.execute(
            f'INSERT INTO {table} ({",".join(row)}) VALUES ({",".join("?" for _ in row)})',
            tuple(row.values()))

    def append(self, identity, **kwargs):
        self.insert('messages', message(identity, **kwargs))

    def complete(self, reader):
        for _ in range(100):
            value = reader.scan(now=self.now)
            if not value['pending']:
                return value
        self.fail('bounded scan did not finish')

    @contextmanager
    def reads(self, reader):
        counts = {'tasks': 0, 'messages': 0, 'identities': 0}
        read_row, read_identity = reader._read_row, reader._read_identity

        def body(*args, **kwargs):
            table = reader.stage
            row = read_row(*args, **kwargs)
            if row is not None:
                self.assertIn(table, ('tasks', 'messages'))
                counts[table] += 1
            return row

        def identity(*args, **kwargs):
            row = read_identity(*args, **kwargs)
            if row is not None:
                counts['identities'] += 1
                self.assertNotIn('data', row.keys())
                self.assertNotIn('costs', row.keys())
            return row

        with patch.object(reader, '_read_row', side_effect=body), \
                patch.object(reader, '_read_identity', side_effect=identity):
            yield counts

    def assert_fresh(self, value, **kwargs):
        self.comparisons += 1
        fresh = self.complete(self.reader(self.root / f'fresh-{self.comparisons}', **kwargs))
        for key in ('sessions', 'skipped_records', 'indexed_sessions', 'expired_sessions',
                    'cap_omitted_sessions', 'collection_complete', 'source_available',
                    'pending', 'limit_reached'):
            with self.subTest(field=key):
                self.assertEqual(value[key], fresh[key])

    def seed_messages(self):
        self.append('message-two')
        self.append('message-three')
        self.writer.commit()

    def test_append_reads_one_body_and_checks_every_identity(self):
        for i in range(12):
            self.append(f'older-{i}', now=self.now - i)
        self.writer.commit()
        reader = self.reader()
        with self.reads(reader) as first:
            self.complete(reader)
        self.assertEqual((first['tasks'], first['messages']), (1, 13))

        self.append('appended', now=self.now - 500)
        self.writer.commit()
        with self.reads(reader) as incremental:
            value = self.complete(reader)
        self.assertEqual(incremental, {'tasks': 1, 'messages': 1, 'identities': 14})
        self.assertEqual(value['sessions'][0]['responses'], 14)
        self.assert_fresh(value)

    def test_cost_and_parent_changes_refresh_all_tasks_without_message_bodies(self):
        self.insert('tasks', task('parent-two', costs={
            'input': 200, 'output': 40, 'cacheRead': 80, 'cacheWrite': 5}))
        self.insert('tasks', task('child', parent='chat-one', kind='subtask',
            status='completed', costs={'input': 10, 'output': 2, 'cacheRead': 2, 'cacheWrite': 0}))
        self.writer.commit()
        reader = self.reader()
        self.complete(reader)

        self.writer.execute('UPDATE tasks SET parent_id=?, costs=? WHERE id=?',
            ('parent-two', json.dumps({'input': 20, 'output': 4, 'cacheRead': 4, 'cacheWrite': 0}), 'child'))
        self.writer.commit()
        with self.reads(reader) as counts:
            value = self.complete(reader)
        self.assertEqual(counts, {'tasks': 3, 'messages': 0, 'identities': 1})
        by_title = {row['title']: row for row in value['sessions']}
        self.assertEqual(by_title['Synthetic chat chat-one']['usage']['total'], 120)
        self.assertEqual(by_title['Synthetic chat parent-two']['usage']['total'], 216)
        self.assertEqual(by_title['Synthetic chat child']['usage']['total'], 24)
        self.assert_fresh(value)

    def test_task_skips_refresh_and_skipped_message_identity_remains_incremental(self):
        bad_task = task('bad-task')
        bad_task['costs'] = '{'
        self.insert('tasks', bad_task)
        bad_message = message('bad-json')
        bad_message['data'] = '{'
        self.insert('messages', bad_message)
        self.append('unknown-tool', role='tool')
        self.writer.commit()
        reader = self.reader()
        initial = self.complete(reader)
        self.assertEqual(initial['skipped_records']['invalid_json'], 2)
        self.assertEqual(initial['skipped_records']['unknown_tool_outcome'], 1)

        self.writer.execute('UPDATE tasks SET costs=? WHERE id=?',
                            (task()['costs'], 'bad-task'))
        self.append('new-response')
        self.writer.commit()
        with self.reads(reader) as counts:
            value = self.complete(reader)
        self.assertEqual(counts, {'tasks': 2, 'messages': 1, 'identities': 4})
        self.assertEqual(value['skipped_records']['invalid_json'], 1)
        self.assertEqual(value['skipped_records']['unknown_tool_outcome'], 1)
        self.assertEqual(value['collection_complete'], 0)
        self.assert_fresh(value)

        self.writer.execute('DELETE FROM messages WHERE id=?', ('bad-json',))
        self.writer.commit()
        with self.reads(reader) as counts:
            value = self.complete(reader)
        self.assertEqual(counts['messages'], 3)
        self.assertEqual(value['skipped_records']['invalid_json'], 0)
        self.assert_fresh(value)

    def test_count_neutral_delete_and_append_force_full_read(self):
        self.seed_messages()
        reader = self.reader()
        self.complete(reader)
        self.writer.execute('DELETE FROM messages WHERE id=?', ('message-two',))
        self.append('replacement', role='user')
        self.writer.commit()
        with self.reads(reader) as counts:
            value = self.complete(reader)
        self.assertEqual(counts['messages'], 3)
        self.assertEqual(value['sessions'][0]['responses'], 2)
        self.assert_fresh(value)

    def test_reused_highest_rowid_with_new_identity_forces_full_read(self):
        self.seed_messages()
        reader = self.reader()
        self.complete(reader)
        before = self.writer.execute('SELECT max(rowid) FROM messages').fetchone()[0]
        self.writer.execute('DELETE FROM messages WHERE rowid=?', (before,))
        self.append('replacement', role='user')
        self.writer.commit()
        self.assertEqual(self.writer.execute('SELECT max(rowid) FROM messages').fetchone()[0], before)
        with self.reads(reader) as counts:
            value = self.complete(reader)
        self.assertEqual(counts['messages'], 3)
        self.assertEqual(value['sessions'][0]['responses'], 2)
        self.assert_fresh(value)

    def test_explicit_old_rowid_replacement_forces_full_read(self):
        self.seed_messages()
        reader = self.reader()
        self.complete(reader)
        self.writer.execute('DELETE FROM messages WHERE rowid=1')
        self.insert('messages', message('replacement', role='user'), rowid=1)
        self.writer.commit()
        with self.reads(reader) as counts:
            value = self.complete(reader)
        self.assertEqual(counts['messages'], 3)
        self.assertEqual(value['sessions'][0]['responses'], 2)
        self.assert_fresh(value)

    def test_changed_identity_in_place_forces_full_read(self):
        self.seed_messages()
        reader = self.reader()
        self.complete(reader)
        self.writer.execute('UPDATE messages SET id=?, role=?, data=? WHERE rowid=1',
                            ('renamed-message', 'user', '{}'))
        self.writer.commit()
        with self.reads(reader) as counts:
            value = self.complete(reader)
        self.assertEqual(counts['messages'], 3)
        self.assertEqual(value['sessions'][0]['responses'], 2)
        self.assert_fresh(value)

    def test_invalid_identities_force_safe_full_reads(self):
        for index, invalid in enumerate((None, '', 'x' * 513)):
            with self.subTest(identity=type(invalid).__name__, length=len(invalid or '')):
                self.writer.execute('DELETE FROM messages WHERE rowid=2')
                self.insert('messages', message(invalid), rowid=2)
                self.writer.commit()
                reader = self.reader(self.root / f'invalid-{index}')
                self.complete(reader)
                self.append(f'append-{index}')
                self.writer.commit()
                expected = self.writer.execute('SELECT count(*) FROM messages').fetchone()[0]
                with self.reads(reader) as counts:
                    value = self.complete(reader)
                self.assertEqual(counts['messages'], expected)
                self.assertEqual(value['skipped_records']['invalid_record'], 1)
                self.assert_fresh(value)

    def test_same_id_payload_edit_reconciles_at_300_seconds_without_another_write(self):
        reader = self.reader()
        initial = self.complete(reader)
        self.clock = 1001
        self.writer.execute('UPDATE messages SET data=?',
                            (json.dumps({'role': 'assistant', '_meta': {'notAi': True}}),))
        self.writer.commit()
        with self.reads(reader) as counts:
            edited = self.complete(reader)
        self.assertEqual(counts, {'tasks': 1, 'messages': 0, 'identities': 1})
        self.assertEqual(edited['sessions'], initial['sessions'])

        self.clock = 1299.999
        with self.reads(reader) as counts:
            unchanged = self.complete(reader)
        self.assertEqual(counts, {'tasks': 0, 'messages': 0, 'identities': 0})
        self.assertEqual(unchanged['sessions'], initial['sessions'])

        self.clock = 1300
        with self.reads(reader) as counts:
            reconciled = self.complete(reader)
        self.assertEqual((counts['tasks'], counts['messages']), (1, 1))
        self.assertEqual(reconciled['sessions'][0]['responses'], 0)
        self.assert_fresh(reconciled)

    def test_further_writes_do_not_postpone_full_reconciliation(self):
        reader = self.reader()
        self.complete(reader)
        self.clock = 1100
        self.writer.execute('UPDATE messages SET role=?, data=?', ('user', '{}'))
        self.append('new-response')
        self.writer.commit()
        self.assertEqual(self.complete(reader)['sessions'][0]['responses'], 2)

        for instant, amount in ((1200, 200), (1299, 300)):
            self.clock = instant
            self.writer.execute('UPDATE tasks SET costs=?',
                (json.dumps({'input': amount, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0}),))
            self.writer.commit()
            with self.reads(reader) as counts:
                value = self.complete(reader)
            self.assertEqual(counts, {'tasks': 1, 'messages': 0, 'identities': 2})
            self.assertEqual(value['sessions'][0]['usage']['total'], amount)
            self.assertEqual(value['sessions'][0]['responses'], 2)

        self.clock = 1300
        with self.reads(reader) as counts:
            value = self.complete(reader)
        self.assertEqual(counts['messages'], 2)
        self.assertEqual(value['sessions'][0]['responses'], 1)
        self.assert_fresh(value)

    def test_restart_reads_older_edits_immediately(self):
        reader = self.reader()
        self.complete(reader)
        reader.close()
        self.writer.execute('UPDATE messages SET role=?, data=?', ('user', '{}'))
        self.writer.commit()
        self.clock += 1
        restarted = self.reader()
        with self.reads(restarted) as counts:
            value = self.complete(restarted)
        self.assertEqual((counts['tasks'], counts['messages']), (1, 1))
        self.assertEqual(value['sessions'][0]['responses'], 0)
        self.assert_fresh(value)

    def test_reconnect_reads_older_edits_immediately(self):
        reader = self.reader()
        initial = self.complete(reader)
        with patch.object(reader, '_open', side_effect=sqlite3.OperationalError('synthetic outage')):
            failed = reader.scan(now=self.now)
        self.assertEqual(failed['source_available'], 0)
        self.assertEqual(failed['sessions'], initial['sessions'])
        self.writer.execute('UPDATE messages SET role=?, data=?', ('user', '{}'))
        self.writer.commit()
        with self.reads(reader) as counts:
            value = self.complete(reader)
        self.assertEqual((counts['tasks'], counts['messages']), (1, 1))
        self.assertEqual(value['sessions'][0]['responses'], 0)
        self.assert_fresh(value)

    def test_interrupted_incremental_append_retries_without_double_counting(self):
        reader = self.reader(max_rows=1)
        initial = self.complete(reader)
        self.append('new-one')
        self.append('new-two')
        self.writer.commit()
        read_row = reader._read_row
        appended = []

        def interrupt(*args, **kwargs):
            if reader.stage == 'messages' and appended:
                raise sqlite3.OperationalError('synthetic interrupted read')
            row = read_row(*args, **kwargs)
            if reader.stage == 'messages' and row is not None:
                appended.append(row['id'])
            return row

        with patch.object(reader, '_read_row', side_effect=interrupt):
            failed = self.complete(reader)
        self.assertEqual(appended, ['new-one'])
        self.assertEqual(failed['source_available'], 0)
        self.assertEqual(failed['sessions'], initial['sessions'])
        with self.reads(reader) as counts:
            recovered = self.complete(reader)
        self.assertEqual(counts['messages'], 3)
        self.assertEqual(recovered['sessions'][0]['responses'], 3)
        self.assertEqual(self.complete(reader)['sessions'], recovered['sessions'])
        self.assert_fresh(recovered)

    def test_identity_reads_obey_slice_row_limit_and_publication_stays_atomic(self):
        for i in range(9):
            self.append(f'older-{i}')
        self.writer.commit()
        reader = self.reader(max_rows=2)
        initial = self.complete(reader)
        self.append('new-response')
        self.writer.commit()
        for _ in range(30):
            with self.reads(reader) as counts:
                value = reader.scan(now=self.now)
            self.assertLessEqual(sum(counts.values()), 2)
            if not value['pending']:
                break
            self.assertEqual(value['sessions'], initial['sessions'])
        else:
            self.fail('bounded incremental scan did not finish')
        self.assertEqual(value['sessions'][0]['responses'], 11)
        self.assert_fresh(value)

    def test_wal_writes_during_identity_pass_wait_for_next_generation(self):
        self.seed_messages()
        reader = self.reader(max_rows=2)
        initial = self.complete(reader)
        self.append('before-snapshot')
        self.writer.execute('UPDATE tasks SET costs=?',
            (json.dumps({'input': 200, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0}),))
        self.writer.commit()

        with self.reads(reader) as counts:
            pending = reader.scan(now=self.now)
            self.assertEqual(counts, {'tasks': 1, 'messages': 0, 'identities': 1})
            self.assertEqual(pending['pending'], 1)
            self.assertEqual(pending['sessions'], initial['sessions'])
            self.append('during-snapshot')
            self.writer.execute('UPDATE tasks SET costs=?',
                (json.dumps({'input': 300, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0}),))
            self.writer.commit()
            first = self.complete(reader)
        self.assertEqual(counts, {'tasks': 1, 'messages': 1, 'identities': 4})
        self.assertEqual(first['sessions'][0]['responses'], 4)
        self.assertEqual(first['sessions'][0]['usage']['total'], 200)

        with self.reads(reader) as counts:
            following = self.complete(reader)
        self.assertEqual(counts, {'tasks': 1, 'messages': 1, 'identities': 5})
        self.assertEqual(following['sessions'][0]['responses'], 5)
        self.assertEqual(following['sessions'][0]['usage']['total'], 300)
        self.assert_fresh(following)

    def test_identity_only_work_obeys_scan_time_limit(self):
        for i in range(9):
            self.append(f'older-{i}')
        self.writer.commit()
        reader = self.reader(scan_seconds=.05)
        initial = self.complete(reader)
        self.append('new-response')
        self.writer.commit()
        read_identity = reader._read_identity

        def slow_identity(*args, **kwargs):
            row = read_identity(*args, **kwargs)
            if row is not None:
                self.clock += .03
            return row

        with patch.object(reader, '_read_identity', side_effect=slow_identity):
            with self.reads(reader) as counts:
                pending = reader.scan(now=self.now)
            self.assertEqual(counts, {'tasks': 1, 'messages': 0, 'identities': 2})
            self.assertEqual(pending['pending'], 1)
            self.assertEqual(pending['sessions'], initial['sessions'])
            for _ in range(10):
                with self.reads(reader) as counts:
                    value = reader.scan(now=self.now)
                self.assertLessEqual(counts['identities'], 2)
                if not value['pending']:
                    break
                self.assertEqual(value['sessions'], initial['sessions'])
            else:
                self.fail('identity time slices did not finish')
        self.assertEqual(value['sessions'][0]['responses'], 11)
        self.assert_fresh(value)

    def test_interrupted_identity_pass_recovers_with_full_read(self):
        self.seed_messages()
        reader = self.reader(max_rows=1)
        initial = self.complete(reader)
        self.append('new-response')
        self.writer.commit()
        read_identity = reader._read_identity
        checked = []

        def interrupt(*args, **kwargs):
            if len(checked) == 2:
                raise sqlite3.OperationalError('synthetic interrupted identity read')
            row = read_identity(*args, **kwargs)
            if row is not None:
                checked.append(row['id'])
            return row

        with patch.object(reader, '_read_identity', side_effect=interrupt):
            with self.reads(reader) as counts:
                failed = self.complete(reader)
        self.assertEqual(counts, {'tasks': 1, 'messages': 0, 'identities': 2})
        self.assertEqual(failed['source_available'], 0)
        self.assertEqual(failed['sessions'], initial['sessions'])
        with self.reads(reader) as counts:
            recovered = self.complete(reader)
        self.assertEqual((counts['tasks'], counts['messages']), (1, 4))
        self.assertEqual(recovered['sessions'][0]['responses'], 4)
        self.assert_fresh(recovered)

    def test_identity_byte_boundary_retries_without_losing_or_double_counting_rows(self):
        identities = [prefix + '-' + 'x' * 208 for prefix in 'abcde']
        self.writer.execute('UPDATE messages SET id=?', (identities[0],))
        for identity in identities[1:4]:
            self.append(identity)
        self.writer.commit()
        reader = self.reader(max_bytes=600)
        initial = self.complete(reader)
        self.append(identities[4])
        self.writer.commit()
        slices = 0
        read_identity = reader._read_identity
        returned_ids = []

        def record_identity(*args, **kwargs):
            row = read_identity(*args, **kwargs)
            if row is not None:
                returned_ids.append(row['id'])
            return row

        with patch.object(reader, '_read_identity', side_effect=record_identity):
            with self.reads(reader) as counts:
                for _ in range(10):
                    slices += 1
                    value = reader.scan(now=self.now)
                    if not value['pending']:
                        break
                    self.assertEqual(value['sessions'], initial['sessions'])
                else:
                    self.fail('identity byte slices did not finish')
        # Each slice can consume at most two 210-byte identities. The third
        # identity fetched at a byte boundary must be kept for the next slice.
        self.assertEqual(slices, 3)
        self.assertEqual((counts['tasks'], counts['messages']), (1, 1))
        self.assertEqual(set(returned_ids), set(identities))
        self.assertGreater(len(returned_ids), len(identities))
        self.assertEqual(value['sessions'][0]['responses'], 5)
        self.assert_fresh(value)

    def test_replacement_with_same_rowid_and_id_waits_for_reconciliation(self):
        self.seed_messages()
        reader = self.reader()
        initial = self.complete(reader)
        identities = self.writer.execute('SELECT rowid, id FROM messages ORDER BY rowid').fetchall()
        self.writer.execute('DELETE FROM messages WHERE rowid=1')
        self.insert('messages', message('message-one', role='user'), rowid=1)
        self.writer.commit()
        self.assertEqual(self.writer.execute(
            'SELECT rowid, id FROM messages ORDER BY rowid').fetchall(), identities)
        self.clock = 1001
        with self.reads(reader) as counts:
            cached = self.complete(reader)
        self.assertEqual(counts, {'tasks': 1, 'messages': 0, 'identities': 3})
        self.assertEqual(cached['sessions'], initial['sessions'])
        self.clock = 1300
        with self.reads(reader) as counts:
            reconciled = self.complete(reader)
        self.assertEqual((counts['tasks'], counts['messages']), (1, 3))
        self.assertEqual(reconciled['sessions'][0]['responses'], 2)
        self.assert_fresh(reconciled)

    def test_total_message_limit_includes_cached_rows(self):
        self.seed_messages()
        reader = self.reader()
        initial = self.complete(reader)
        self.append('over-limit')
        self.writer.commit()
        with patch('traceonaut.bob_session_telemetry.MAX_MESSAGES', 3):
            failed = self.complete(reader)
        self.assertEqual(failed['limit_reached'], 1)
        self.assertEqual(failed['collection_complete'], 0)
        self.assertEqual(failed['sessions'], initial['sessions'])
        value = self.complete(reader)
        self.assertEqual(value['sessions'][0]['responses'], 4)
        self.assert_fresh(value)

    def test_incremental_retention_and_cap_match_full_scan(self):
        for i, age in enumerate((10, 20, 100)):
            self.insert('tasks', task(f'other-{i}', now=self.now - age))
        self.writer.commit()
        options = {'session_retention_seconds': 30, 'session_export_cap': 2}
        reader = self.reader(**options)
        self.complete(reader)
        self.append('revive-old', chat='other-2')
        self.writer.commit()
        with self.reads(reader) as counts:
            value = self.complete(reader)
        self.assertEqual(counts, {'tasks': 4, 'messages': 1, 'identities': 2})
        self.assertEqual((value['indexed_sessions'], len(value['sessions']),
                          value['expired_sessions'], value['cap_omitted_sessions']), (4, 2, 0, 2))
        self.assert_fresh(value, **options)

    def test_incremental_scan_keeps_source_immutable_and_raw_ids_private(self):
        reader = self.reader()
        self.complete(reader)
        self.append('RAW_MESSAGE_ID_SENTINEL')
        self.writer.commit()
        source_before = {p.name: hashlib.sha256(p.read_bytes()).digest()
                         for p in self.path.parent.iterdir() if not p.name.endswith('-shm')}
        value = self.complete(reader)
        source_after = {p.name: hashlib.sha256(p.read_bytes()).digest()
                        for p in self.path.parent.iterdir() if not p.name.endswith('-shm')}
        self.assertEqual(source_after, source_before)
        metrics = render_bob_metrics(value)
        for private in (b'RAW_MESSAGE_ID_SENTINEL', b'PRIVATE_SENTINEL', b'chat-one', b'project-one'):
            self.assertNotIn(private, metrics)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        for path in self.state.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            for private in (b'RAW_MESSAGE_ID_SENTINEL', b'PRIVATE_SENTINEL', b'/workspace'):
                self.assertNotIn(private, path.read_bytes())


if __name__ == '__main__':
    unittest.main()
