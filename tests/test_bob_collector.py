import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from traceonaut.bob_session_telemetry import BobCollector, render_bob_metrics, MAX_JSON_BYTES
from bob_fixtures import database, task, message


class BobCollectorTests(unittest.TestCase):
    now = 1_800_000_000

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'bob'
        self.path = self.home / 'db/bob.db'
        self.writer = database(self.path, [task()], [message()])
        self.addCleanup(self.writer.close)
        self.state = self.root / 'state'

    def reader(self, **kwargs):
        reader = BobCollector(self.home, self.state, **kwargs)
        self.addCleanup(reader.close)
        return reader

    def complete(self, reader):
        for _ in range(20):
            value = reader.scan(now=self.now)
            if not value['pending']:
                return value
        self.fail('bounded scan did not finish')

    def test_basic_accounting_and_source_immutability(self):
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        reader = self.reader()
        value = self.complete(reader)
        self.assertEqual(value['source_available'], 1)
        self.assertEqual(value['collection_complete'], 1)
        self.assertEqual(value['sessions'][0]['usage']['total'], 120)
        self.assertEqual(value['sessions'][0]['responses'], 1)
        payload = render_bob_metrics(value)
        for private in (b'PRIVATE_SENTINEL', b'/workspace', b'Synthetic chat', b'project-one', b'chat-one'):
            self.assertNotIn(private, payload)
        self.assertNotIn(b'PRIVATE_SENTINEL', (self.state / 'bob.sqlite3').read_bytes())
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), ['bob.db'])

    def test_slices_are_atomic_and_restarts_do_not_inflate(self):
        reader = self.reader(max_rows=1)
        first = reader.scan(now=self.now)
        self.assertEqual(first['pending'], 1)
        self.assertEqual(first['sessions'], [])
        first = self.complete(reader)
        self.assertEqual(first['sessions'][0]['responses'], 1)
        second = self.complete(reader)
        self.assertEqual(first['sessions'], second['sessions'])
        reader.close()
        reader = self.reader(max_rows=1)
        self.assertEqual(self.complete(reader)['sessions'], first['sessions'])

    def test_private_sidecar_stays_present_for_codex_state_checks(self):
        reader = self.reader()
        self.complete(reader)
        journal = self.state / 'bob.sqlite3-journal'
        identity = journal.stat().st_ino
        self.writer.execute("UPDATE tasks SET title='Renamed synthetic chat'")
        self.writer.commit()
        self.complete(reader)
        reader.close()
        self.assertEqual(journal.stat().st_ino, identity)
        self.assertEqual(journal.stat().st_mode & 0o777, 0o600)
        self.assertLessEqual(journal.stat().st_size, 1048576)

    def test_wal_updates_equal_timestamps_backdated_edits_and_deletes(self):
        self.writer.execute('PRAGMA journal_mode=WAL')
        reader = self.reader()
        self.complete(reader)
        for mid, when in (('same-time', self.now), ('backdated', self.now - 500)):
            row = message(mid, now=when)
            self.writer.execute('INSERT INTO messages VALUES (?,?,?,?,?)',
                (row['id'], row['task_id'], row['role'], row['created_at'], row['data']))
        self.writer.execute('UPDATE tasks SET costs=?', (json.dumps({'input': 200, 'output': 30, 'cacheRead': 0, 'cacheWrite': 0}),))
        self.writer.commit()
        value = self.complete(reader)
        self.assertEqual(value['sessions'][0]['responses'], 3)
        self.assertEqual(value['sessions'][0]['usage']['total'], 230)
        self.writer.execute("DELETE FROM messages WHERE id='same-time'")
        self.writer.execute("UPDATE messages SET role='user',data='{}' WHERE id='backdated'")
        self.writer.commit()
        self.assertEqual(self.complete(reader)['sessions'][0]['responses'], 1)
        self.writer.execute('DELETE FROM tasks')
        self.writer.commit()
        self.assertEqual(self.complete(reader)['sessions'], [])

    def test_database_and_wal_unchanged_only_shm_reader_marks_can_change(self):
        self.writer.execute('PRAGMA journal_mode=WAL')
        self.writer.execute('UPDATE tasks SET title=?', ('Explicit synthetic title',))
        self.writer.commit()
        def contents():
            return {p.name: p.read_bytes() for p in self.path.parent.iterdir()}
        before = contents()
        self.assertEqual(set(before), {'bob.db', 'bob.db-wal', 'bob.db-shm'})
        reader = self.reader()
        self.assertEqual(self.complete(reader)['source_available'], 1)
        reader.close()
        after = contents()
        self.assertEqual(set(after), set(before))
        for name in ('bob.db', 'bob.db-wal'):
            self.assertEqual(after[name], before[name])
        # SQLite read-only WAL clients still register reader marks in the
        # wal-index header (five uint32 slots at bytes 100..119). These are
        # coordination state, not database records or WAL frames.
        self.assertEqual(len(after['bob.db-shm']), len(before['bob.db-shm']))
        changed = {index for index, (a, b) in enumerate(zip(before['bob.db-shm'], after['bob.db-shm'])) if a != b}
        self.assertTrue(changed <= set(range(100, 120)), changed)

    def test_corrupt_database_retains_cache_and_recovers(self):
        reader = self.reader()
        good = self.complete(reader)
        corrupt = self.root / 'corrupt.db'
        corrupt.write_bytes(b'not a SQLite database')
        healthy = self.root / 'healthy.db'
        self.path.rename(healthy)
        corrupt.rename(self.path)
        failed = reader.scan(now=self.now + 5)
        self.assertEqual(failed['source_available'], 0)
        self.assertEqual(failed['sessions'], good['sessions'])
        healthy.replace(self.path)
        self.assertEqual(self.complete(reader)['source_available'], 1)

    def test_writes_during_transaction_are_seen_next_generation(self):
        self.writer.execute('PRAGMA journal_mode=WAL')
        reader = self.reader(max_rows=1)
        reader.scan(now=self.now)
        self.writer.execute('UPDATE tasks SET costs=?', (json.dumps({'input': 999, 'output': 0}),))
        self.writer.commit()
        self.assertEqual(self.complete(reader)['sessions'][0]['usage']['total'], 120)
        self.assertEqual(self.complete(reader)['sessions'][0]['usage']['total'], 999)

    def test_lock_corruption_missing_schema_and_recovery_are_visible(self):
        reader = self.reader()
        good = self.complete(reader)
        self.writer.execute('BEGIN EXCLUSIVE')
        value = reader.scan(now=self.now + 5)
        self.assertEqual(value['source_available'], 0)
        self.assertEqual(value['last_success'], good['last_success'])
        self.assertEqual(value['sessions'], good['sessions'])
        self.writer.rollback()
        self.assertEqual(self.complete(reader)['source_available'], 1)
        self.writer.execute('ALTER TABLE messages RENAME TO not_messages')
        self.writer.commit()
        value = self.complete(reader)
        self.assertEqual(value['source_available'], 0)
        self.assertEqual(value['collection_complete'], 0)

    def test_database_replacement_and_missing_source_recovery(self):
        reader = self.reader()
        self.complete(reader)
        replacement = self.root / 'replacement.db'
        other = database(replacement, [task('replacement')], [message(chat='replacement')])
        other.close()
        os.replace(replacement, self.path)
        value = self.complete(reader)
        self.assertIn('replacement', value['sessions'][0]['title'])
        self.path.rename(self.root / 'held.db')
        self.assertEqual(reader.scan(now=self.now)['source_available'], 0)
        (self.root / 'held.db').rename(self.path)
        self.assertEqual(self.complete(reader)['source_available'], 1)

    def test_retention_cap_does_not_delete_source_or_index(self):
        for i, age in enumerate((10, 20, 100)):
            row = task('extra-' + str(i), now=self.now - age)
            self.writer.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?)', tuple(row.values()))
        self.writer.commit()
        reader = self.reader(session_retention_seconds=30, session_export_cap=2)
        value = self.complete(reader)
        self.assertEqual((value['indexed_sessions'], len(value['sessions']), value['expired_sessions'], value['cap_omitted_sessions']), (4, 2, 1, 1))
        self.assertEqual(self.writer.execute('SELECT count(*) FROM tasks').fetchone()[0], 4)

    def test_malformed_and_oversized_records_are_partial(self):
        self.writer.execute("UPDATE messages SET data=?", ('x' * (MAX_JSON_BYTES + 1),))
        self.writer.commit()
        value = self.complete(self.reader())
        self.assertEqual(value['skipped_records']['oversized_record'], 1)
        self.assertEqual(value['collection_complete'], 0)
        self.assertEqual(value['sessions'][0]['usage']['total'], 120)
        self.assertEqual(value['sessions'][0]['responses'], 0)

    def test_scan_record_time_and_index_limits_are_visible(self):
        reader = self.reader()
        for constant in ('MAX_TASKS', 'MAX_MESSAGES', 'MAX_INDEX_BYTES', 'MAX_TRANSACTION_SECONDS'):
            reader.data_version = None
            with self.subTest(constant=constant), patch('traceonaut.bob_session_telemetry.' + constant, 0):
                value = reader.scan(now=self.now)
                self.assertEqual(value['limit_reached'], 1)
                self.assertEqual(value['collection_complete'], 0)
        self.assertEqual(self.complete(reader)['source_available'], 1)

    def test_source_and_state_aliases_are_rejected(self):
        moved = self.root / 'real.db'
        self.path.rename(moved)
        self.path.symlink_to(moved)
        self.assertEqual(self.complete(self.reader())['source_available'], 0)
        with self.assertRaises(ValueError):
            BobCollector(self.home, self.home / 'state')

    def test_second_writer_is_refused_and_unknown_numbers_are_absent(self):
        reader = self.reader()
        with self.assertRaises(BlockingIOError):
            BobCollector(self.home, self.state)
        self.writer.execute('UPDATE tasks SET costs=?', ('{"input":true,"output":0}',))
        self.writer.commit()
        value = self.complete(reader)
        self.assertIsNone(value['sessions'][0]['usage']['total'])
        payload = render_bob_metrics(value).decode()
        self.assertNotIn('token_kind="input"', payload)
        self.assertIn('token_kind="output"} 0', payload)


if __name__ == '__main__':
    unittest.main()
