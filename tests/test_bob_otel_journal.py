"""Durable accounting, privacy and loss controls for optional generation capture."""
import copy
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from traceonaut.bob_otel_journal import JournalReader, project, CAPTURE_STATUS, MAX_LINE_BYTES
from traceonaut.bob_session_telemetry import BobCollector, identity, render_bob_metrics
from bob_fixtures import database, task, message


def packet(chat='chat-one', *, sequence=1, input_value=100, output=20, total=None,
           version='2.0.5', now=None, extra=None):
    now = time.time() if now is None else now
    attributes = {'bob.event.type': {'stringValue': 'LLM Generation'},
                  'gen_ai.conversation.id': {'stringValue': identity(chat)},
                  'bob.producer.version': {'stringValue': version}}
    for key, value in [('input_tokens', input_value), ('output_tokens', output), ('total_tokens', total)]:
        if value is not None:
            attributes['gen_ai.usage.' + key] = {'intValue': str(value) if type(value) is int else value}
    attributes.update(extra or {})
    span = {'traceId': f'{sequence:032x}', 'spanId': f'{sequence:016x}', 'name': 'LLM Generation',
            'startTimeUnixNano': str(int((now - 1) * 1e9)), 'endTimeUnixNano': str(int(now * 1e9)),
            'attributes': [{'key': key, 'value': value} for key, value in attributes.items()]}
    return {'resourceSpans': [{'resource': {}, 'scopeSpans': [{'scope': {}, 'spans': [span]}]}]}


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.journal = self.root / 'journal'
        self.journal.mkdir(mode=0o700)
        self.path = self.journal / 'bob-usage.json'
        self.state = self.root / 'state'
        self.state.mkdir(mode=0o700)
        self.db = sqlite3.connect(self.state / 'index.sqlite3')
        self.addCleanup(self.db.close)
        self.reader = JournalReader(self.journal, self.db, self.state)
        self.now = time.time()

    def write(self, value, mode='ab'):
        with self.path.open(mode) as stream:
            stream.write(json.dumps(value).encode() + b'\n')

    def rows(self, chats=('chat-one',), responses=1):
        return [{'session_id': identity(chat), 'responses': responses, 'tool_results': 0} for chat in chats]

    def scan(self, chats=('chat-one',), now=None, responses=1):
        rows = self.rows(chats, responses)
        self.reader.scan(rows, now or self.now + 5)
        return rows, self.reader.snapshot(rows)

    def completed_rotations(self):
        paths = []
        for sequence in range(1, 7):
            path = self.journal / f'bob-usage-{sequence}.json'
            path.write_bytes(json.dumps(packet(sequence=sequence, now=self.now)).encode() + b'\n')
            os.utime(path, (self.now - 100 + sequence, self.now - 100 + sequence))
            paths.append(path)
        rows, health = self.scan()
        self.assertEqual(rows[0]['captured_usage']['total'], 720)
        self.assertEqual(health['backlog_bytes'], 0)
        return paths

    def test_completed_rotations_cannot_starve_new_usage_across_restart_and_replay(self):
        paths = self.completed_rotations()
        limit = 2 * paths[0].stat().st_size
        new = packet(sequence=7, input_value=7, output=3, now=self.now)
        self.write(new)
        for _ in range(8):
            self.reader = JournalReader(self.journal, self.db, self.state, max_bytes=limit)
            rows, health = self.scan()
        self.assertEqual(rows[0]['captured_usage']['total'], 730)
        self.assertEqual(health['backlog_bytes'], 0)
        self.assertEqual(health['losses'], 0)
        self.assertTrue(all(path.exists() for path in paths))
        self.write(new)
        for _ in range(8):
            self.reader = JournalReader(self.journal, self.db, self.state, max_bytes=limit)
            rows, health = self.scan()
        self.assertEqual(rows[0]['captured_usage']['total'], 730)
        self.assertEqual(health['backlog_bytes'], 0)
        self.assertEqual(health['losses'], 0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM bob_capture_events').fetchone()[0], 7)
        # A same-inode, same-size rewrite must still be verified after rotating.
        old = paths[0].stat()
        paths[0].write_bytes(json.dumps(packet(sequence=8, now=self.now)).encode() + b'\n')
        os.utime(paths[0], ns=(old.st_atime_ns, old.st_mtime_ns))
        for _ in range(8):
            self.reader = JournalReader(self.journal, self.db, self.state, max_bytes=limit)
            rows, health = self.scan()
        self.assertEqual(rows[0]['captured_usage']['total'], 850)
        self.assertEqual(health['losses'], 1)

    def test_time_budget_resumes_past_completed_rotations_after_restart(self):
        self.completed_rotations()
        self.write(packet(sequence=7, input_value=7, output=3, now=self.now))
        for _ in range(8):
            self.reader = JournalReader(self.journal, self.db, self.state)
            ticks = iter([0, 0, 0])
            with patch('traceonaut.bob_otel_journal.time.monotonic', side_effect=lambda: next(ticks, 1)):
                rows, health = self.scan()
        self.assertEqual(rows[0]['captured_usage']['total'], 730)
        self.assertEqual(health['backlog_bytes'], 0)
        self.assertEqual(health['errors'], 0)
        self.assertEqual(health['losses'], 0)

    def test_unread_record_gets_next_budget_when_head_verification_used_the_remainder(self):
        old = self.journal / 'bob-usage-old.json'
        old.write_bytes(json.dumps(packet(now=self.now)).encode() + b'\n')
        os.utime(old, (self.now - 100, self.now - 100))
        self.scan()
        self.write(packet(sequence=2, now=self.now))
        limit = 2 * old.stat().st_size
        self.reader = JournalReader(self.journal, self.db, self.state, max_bytes=limit)
        self.assertEqual(self.scan()[0][0]['captured_usage']['total'], 120)
        self.reader = JournalReader(self.journal, self.db, self.state, max_bytes=limit)
        rows, health = self.scan()
        self.assertEqual(rows[0]['captured_usage']['total'], 240)
        self.assertEqual(health['backlog_bytes'], 0)

    def test_partial_tail_cannot_starve_another_file(self):
        old = self.journal / 'bob-usage-old.json'
        raw = json.dumps(packet(now=self.now)).encode() + b'\n'
        old.write_bytes(raw[:len(raw) * 4 // 5])
        os.utime(old, (self.now - 100, self.now - 100))
        self.write(packet(sequence=2, now=self.now))
        limit = 2 * len(raw)
        for _ in range(4):
            self.reader = JournalReader(self.journal, self.db, self.state, max_bytes=limit)
            rows, health = self.scan()
        self.assertEqual(rows[0]['captured_usage'].get('total'), 120)
        self.assertEqual(health['backlog_bytes'], old.stat().st_size)
        info = old.stat()
        self.assertEqual(self.db.execute('SELECT offset FROM bob_capture_files WHERE id=?',
                         (f'{info.st_dev}:{info.st_ino}',)).fetchone()[0], 0)
        self.assertEqual(health['losses'], 0)

    def test_row_budget_resumes_and_a_removed_cursor_file_falls_back_safely(self):
        paths = []
        for sequence in range(1, 4):
            path = self.journal / f'bob-usage-{sequence}.json'
            path.write_bytes(json.dumps(packet(sequence=sequence, now=self.now)).encode() + b'\n')
            os.utime(path, (self.now - 100 + sequence, self.now - 100 + sequence))
            paths.append(path)
        for expected in (120, 240):
            self.reader = JournalReader(self.journal, self.db, self.state, max_rows=1)
            rows, health = self.scan()
            self.assertEqual(rows[0]['captured_usage']['total'], expected)
            self.assertGreater(health['backlog_bytes'], 0)
        info = paths[2].stat()
        self.assertEqual(self.db.execute('SELECT next_file FROM bob_capture_scan WHERE id=1').fetchone()[0],
                         f'{info.st_dev}:{info.st_ino}')
        paths[2].unlink()
        self.write(packet(sequence=4, now=self.now))
        self.reader = JournalReader(self.journal, self.db, self.state, max_rows=1)
        rows, health = self.scan()
        self.assertEqual(rows[0]['captured_usage']['total'], 360)
        self.assertEqual(health['backlog_bytes'], 0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM bob_capture_events').fetchone()[0], 3)
        self.reader = JournalReader(self.journal, self.db, self.state, max_rows=1)
        self.assertEqual(self.scan()[0][0]['captured_usage']['total'], 360)

    def test_real_cache_detail_is_not_added_and_replay_restart_is_idempotent(self):
        value = packet(input_value=1864, output=5, total=1869, now=self.now,
                       extra={'gen_ai.usage.cache_creation.input_tokens': {'intValue': '1861'}})
        self.write(value)
        rows, health = self.scan()
        self.assertEqual(rows[0]['captured_usage'], {'input':1864,'output':5,'total':1869,'cache_write_input':1861})
        self.assertEqual(rows[0]['capture_status'], CAPTURE_STATUS['partial'])
        self.write(value)
        self.reader = JournalReader(self.journal, self.db, self.state)
        again, second = self.scan()
        self.assertEqual(again, rows)
        self.assertEqual(second['epoch'], health['epoch'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM bob_capture_events').fetchone()[0], 1)

    def test_zero_missing_and_unsupported_version_are_distinct_from_activity_absence(self):
        for seq, chat, input_value, output, version in [(1,'zero',0,0,'2.0.5'),
            (2,'missing',4,None,'2.0.5'),(3,'unknown',10,2,'9.9')]:
            self.write(packet(chat,sequence=seq,input_value=input_value,output=output,version=version,now=self.now))
        rows, _ = self.scan(('zero','missing','unknown','inactive'), responses=0)
        self.assertEqual([row['capture_status'] for row in rows], [2,3,3,1])
        self.assertEqual(rows[0]['captured_usage']['total'], 0)
        self.assertEqual(rows[1]['captured_usage'], {})
        self.assertEqual(rows[2]['captured_usage'], {})

    def test_saved_message_without_response_is_missing_not_no_activity(self):
        rows=self.rows(responses=0)
        rows[0]['last_event']=self.now
        self.reader.scan(rows,self.now+1)
        self.reader.snapshot(rows)
        self.assertEqual(rows[0]['capture_status'],CAPTURE_STATUS['missing'])
        self.assertEqual(rows[0]['captured_usage'],{})

    def test_conflicting_span_keeps_first_counts_once(self):
        self.write(packet(now=self.now))
        self.scan()
        self.write(packet(input_value=999, now=self.now))
        rows, health = self.scan()
        self.assertEqual(rows[0]['captured_usage']['input'], 100)
        self.assertEqual(health['losses'], 1)
        self.write(packet(input_value=999, now=self.now))
        self.assertEqual(self.scan()[1]['losses'], 1)

    def test_shared_index_capacity_rejects_capture_without_resetting_saved_collection(self):
        self.write(packet(now=self.now))
        with patch('traceonaut.bob_otel_journal.MAX_INDEX_BYTES',1):
            rows,health=self.scan()
        self.assertEqual(rows[0]['captured_usage'],{})
        self.assertEqual(health['losses'],1)
        self.assertEqual(health['source_available'],1)

    def test_pending_join_is_withheld_then_resolves_and_expiry_never_counts_later(self):
        self.write(packet(now=self.now))
        _, health = self.scan(())
        self.assertEqual(health['pending_joins'], 1)
        self.reader.retention = 10
        self.scan((), now=self.now + 20)
        rows, health = self.scan(now=self.now + 21)
        self.assertEqual(rows[0]['captured_usage'], {})
        self.assertEqual(health['pending_joins'], 0)
        self.assertEqual(health['losses'], 1)
        self.write(packet(sequence=2,now=self.now))
        self.scan((), now=self.now + 22)
        rows, health = self.scan(now=self.now + 23)
        self.assertEqual(rows[0]['captured_usage']['total'], 120)

    def test_partial_tail_does_not_advance_and_completion_is_counted_once(self):
        value = json.dumps(packet(now=self.now)).encode()
        self.path.write_bytes(value[:len(value)//2])
        _, health = self.scan()
        self.assertGreater(health['backlog_bytes'], 0)
        self.assertEqual(self.db.execute('SELECT offset FROM bob_capture_files').fetchone()[0], 0)
        with self.path.open('ab') as stream:
            stream.write(value[len(value)//2:] + b'\n')
        self.assertEqual(self.scan()[0][0]['captured_usage']['total'], 120)

    def test_rotation_truncation_and_readable_replay_do_not_raise_totals(self):
        value = packet(now=self.now)
        self.write(value)
        self.scan()
        self.path.rename(self.journal / 'bob-usage-old.json')
        self.write(packet(sequence=2,now=self.now))
        self.assertEqual(self.scan()[0][0]['captured_usage']['total'], 240)
        self.write(value, mode='wb')
        self.assertEqual(self.scan()[0][0]['captured_usage']['total'], 240)
        self.reader.max_events = 2
        self.write(packet(sequence=3,now=self.now))
        rows, health = self.scan()
        self.assertEqual(rows[0]['captured_usage']['total'], 240)
        self.assertGreater(health['losses'], 0)
        self.write(value)
        self.assertEqual(self.scan()[0][0]['captured_usage']['total'], 240)

    def test_state_loss_persists_eof_reset_across_constructor_restart(self):
        self.write(packet(now=self.now))
        self.scan()
        for name in ('bob_capture_meta','bob_capture_files','bob_capture_events','bob_capture_totals'):
            self.db.execute('DELETE FROM ' + name)
        self.db.commit()
        self.reader = JournalReader(self.journal, self.db, self.state)
        self.reader = JournalReader(self.journal, self.db, self.state)
        rows, health = self.scan()
        self.assertEqual(rows[0]['captured_usage'], {})
        self.assertEqual(health['losses'], 1)
        self.write(packet(sequence=2,now=health['epoch']+1))
        self.assertEqual(self.scan()[0][0]['captured_usage']['total'], 120)

    def test_state_loss_rejects_older_replay_after_eof_and_across_restart(self):
        old = packet(now=self.now)
        self.write(old)
        self.scan()
        for name in ('bob_capture_meta','bob_capture_files','bob_capture_events','bob_capture_totals'):
            self.db.execute('DELETE FROM ' + name)
        self.db.commit()
        self.reader = JournalReader(self.journal,self.db,self.state)
        self.scan()
        self.reader = JournalReader(self.journal,self.db,self.state)
        self.write(old)
        rows,health = self.scan()
        self.assertEqual(rows[0]['captured_usage'],{})
        self.assertEqual(health['losses'],2)
        self.write(packet(sequence=2,now=health['epoch']+1))
        self.assertEqual(self.scan()[0][0]['captured_usage']['total'],120)

    def test_malformed_oversized_and_private_fields_never_enter_state(self):
        value = packet(now=self.now, extra={'user.prompt': {'stringValue':'PRIVATE_CANARY'}})
        self.write(value)
        with self.path.open('ab') as stream:
            stream.write(b'x' * (MAX_LINE_BYTES + 20) + b'\n')
        rows, health = self.scan()
        self.assertEqual(rows[0]['captured_usage'], {})
        self.assertEqual(health['losses'], 2)
        self.assertNotIn('PRIVATE_CANARY', ''.join(self.db.iterdump()))

    def test_symlink_hardlink_and_public_directory_fail_closed_without_affecting_database_activity(self):
        self.write(packet(now=self.now))
        self.journal.chmod(0o755)
        self.assertEqual(self.scan()[1]['source_available'], 0)
        self.journal.chmod(0o700)
        target = self.root / 'other.json'
        self.path.rename(target)
        self.path.symlink_to(target)
        self.assertEqual(self.scan()[1]['source_available'], 0)
        self.path.unlink()
        os.link(target,self.path)
        self.assertEqual(self.scan()[1]['source_available'], 0)

    def test_two_chats_and_child_are_exclusive_no_saved_token_subtraction(self):
        for sequence, chat, count in [(1,'parent',30),(2,'child',10),(3,'other',4)]:
            self.write(packet(chat,sequence=sequence,input_value=count,output=1,now=self.now))
        rows, _ = self.scan(('parent','child','other'))
        self.assertEqual([row['captured_usage']['total'] for row in rows],[31,11,5])

    def test_supplied_inconsistent_total_bool_negative_and_optional_absence_are_not_zero(self):
        for seq, chat, input_value, total in [(1,'conflict',10,99),(2,'bool',True,None),(3,'negative',-1,None)]:
            self.write(packet(chat,sequence=seq,input_value=input_value,total=total,now=self.now))
        rows, _ = self.scan(('conflict','bool','negative'))
        self.assertTrue(all(row['captured_usage']=={} for row in rows))

    def test_unknown_resource_metadata_and_nested_payload_are_rejected(self):
        original = packet(now=self.now)
        for location in ('resource','scope','events','status','links','name'):
            value=copy.deepcopy(original)
            resource=value['resourceSpans'][0]; scope=resource['scopeSpans'][0]; span=scope['spans'][0]
            if location=='resource': resource['resource']['attributes']=[{'key':'user','value':{'stringValue':'PRIVATE'}}]
            elif location=='scope': scope['scope']['name']='PRIVATE'
            elif location=='status': span['status']={'message':'PRIVATE'}
            elif location=='name': span['name']='PRIVATE'
            else: span[location]=[{'name':'PRIVATE'}]
            with self.subTest(location=location), self.assertRaises(ValueError):
                project(json.dumps(value),self.now+1)


class CollectorCaptureTests(unittest.TestCase):
    def test_optional_reader_uses_existing_index_and_missing_journal_preserves_saved_metrics(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); now=time.time(); journal=root/'journal'; journal.mkdir(mode=0o700)
            database(root/'bob/db/bob.db',[task(now=now)],[message(now=now,role='user')]).close()
            collector=BobCollector(root/'bob',root/'state',otel_journal_dir=journal)
            try:
                snapshot=collector.scan(now=now+1)
                self.assertEqual(snapshot['source_available'],1)
                self.assertEqual(snapshot['sessions'][0]['responses'],0)
                self.assertEqual(snapshot['sessions'][0]['usage']['total'],120)
                self.assertEqual(snapshot['sessions'][0]['capture_status'],3)
                journal.rename(root/'unavailable')
                failed=collector.scan(now=now+2)
                self.assertEqual(failed['source_available'],1)
                self.assertEqual(failed['capture']['source_available'],0)
                self.assertEqual(failed['sessions'][0]['capture_status'],5)
                payload=render_bob_metrics(failed).decode()
                self.assertIn('traceonaut_bob_capture_enabled{} 1',payload)
                self.assertNotIn('PRIVATE_SENTINEL',payload)
                labels=[line for line in payload.splitlines() if line.startswith('traceonaut_bob_session_capture')]
                self.assertTrue(all('project_id=' in line and 'session_id=' in line for line in labels))
            finally: collector.close()


if __name__ == '__main__': unittest.main()
