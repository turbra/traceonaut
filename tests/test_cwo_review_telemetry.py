"""Paired CLI results: source identity, exact accounting, privacy and failures."""
import copy
import hashlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from traceonaut import cwo_review_telemetry as review
from collect_codex_sessions import SessionMetricsEndpoint
from test_cwo_audit_telemetry import record, NOW


def fixtures():
    launch = {'dispatch_id': 'dispatch-example', 'packet_sha256': 'a' * 64,
              'started_at': datetime.fromtimestamp(NOW - 100, timezone.utc).isoformat(),
              'requested_model': 'example-requested', 'effort': 'high', 'argv': ['PRIVATE_PROMPT']}
    result = {'type': 'result', 'is_error': False, 'subtype': 'success',
              'uuid': '00000000-0000-4000-8000-000000000001', 'session_id': '00000000-0000-4000-8000-000000000002',
              'duration_ms': 1250, 'usage': {'input_tokens': 2, 'cache_creation_input_tokens': 100,
              'cache_read_input_tokens': 20, 'output_tokens': 50, 'output_tokens_details': {'thinking_tokens': 30},
              'iterations': [{'input_tokens': 2, 'output_tokens': 50}]},
              'modelUsage': {'example-reported': {'inputTokens': 999}}, 'result': 'PRIVATE_RESULT'}
    return launch, result


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.launch, self.result = fixtures()
        self.write()
        self.collector = review.ReviewCollector(directories=[self.root])

    def write(self, prefix='lane', directory=None):
        root = directory or self.root
        root.mkdir(exist_ok=True)
        (root / (prefix + '-launch-receipt.json')).write_text(json.dumps(self.launch))
        (root / (prefix + '-response.raw.json')).write_text(json.dumps(self.result))
        (root / 'audit.jsonl').write_bytes(record('dispatch_prepared', timestamp=NOW - 110,
            dispatch_id=self.launch['dispatch_id'], packet_sha256=self.launch['packet_sha256']))

    def scan(self, **kwargs):return self.collector.scan(now=kwargs.get('now', NOW))

    def write_provenance(self, directory=None, *, final=True):
        root = directory or self.root
        root.mkdir(exist_ok=True)
        prompt = ('Dispatch ID: ' + self.launch['dispatch_id'] + '\nPacket SHA-256: '
                  + self.launch['packet_sha256'] + '\nPRIVATE_PROMPT\n').encode()
        result = {key: value for key, value in self.result.items() if key not in ('uuid', 'session_id')}
        provenance = {'started_at': self.launch['started_at'],
                      'requested_model': self.launch['requested_model'], 'executed_effort': 'high',
                      'prompt_sha256': hashlib.sha256(prompt).hexdigest(),
                      'events': ([{'type': 'assistant', 'usage': {'input_tokens': 999}},
                                  {'type': 'assistant', 'usage': {'input_tokens': 999}}, result] if final else [])}
        (root/'review-prompt.txt').write_bytes(prompt)
        (root/'review-provenance.json').write_text(json.dumps(provenance))
        (root/'contract-audit.jsonl').write_bytes(record('dispatch_prepared', timestamp=NOW - 110,
            dispatch_id=self.launch['dispatch_id'], packet_sha256=self.launch['packet_sha256']))
        return provenance

    def test_new_launch_pair_normalizes_effort_and_preserves_identity(self):
        expected = self.scan()['reviews'][0]
        launch = {**self.launch, 'requested_effort': self.launch['effort']}
        launch.pop('effort')
        (self.root/'claude-launch.json').write_text(json.dumps(launch))
        (self.root/'claude-response.json').write_text(json.dumps(self.result))
        result = self.scan()
        self.assertEqual(result['collection_complete'], 1)
        self.assertEqual(result['reviews'], [expected])
        (self.root/'lane-launch-receipt.json').unlink()
        self.assertEqual(self.scan()['reviews'], [expected])

    def test_snapshot_removal_restart_and_reappearance(self):
        snapshot = self.scan()
        db_path = self.root/'history.sqlite3'
        with sqlite3.connect(db_path) as db:
            review.preserve_review_markers(db, snapshot)
            changes = db.total_changes
            review.preserve_review_markers(db, {**snapshot, 'scan_timestamp_seconds': NOW+1})
            self.assertEqual(db.total_changes, changes)
            removed = review.preserve_review_markers(db, {**snapshot, 'reviews': [], 'eligible_review_ids': [], 'scan_timestamp_seconds': NOW+2})
            self.assertEqual(len(removed['retired_reviews']), 1)
            payload = review.render_review_metrics(removed).decode()
            samples = [line for line in payload.splitlines() if 'review_id=' in line]
            self.assertEqual(len(samples), 1)
            self.assertTrue(samples[0].startswith('cwo_review_snapshot_timestamp_seconds{'))
            self.assertIn('record_state="removed"', samples[0])
            self.assertIn('history="tracked"', samples[0])
            self.assertNotIn('PRIVATE', payload)
        with sqlite3.connect(db_path) as db:
            restarted = review.preserve_review_markers(db, {**snapshot, 'reviews': [], 'eligible_review_ids': [], 'scan_timestamp_seconds': NOW+3})
            self.assertEqual(restarted['retired_reviews'], removed['retired_reviews'])
            returned = review.preserve_review_markers(db, {**snapshot, 'scan_timestamp_seconds': NOW+4})
            self.assertEqual(returned['retired_reviews'], [])

    def test_snapshot_pending_replaced_and_normal_expiry(self):
        snapshot = self.scan()
        final = snapshot['reviews'][0]
        pending = {**final, 'review_id': 'pending-result', 'outcome': 'unknown',
                   'tokens': {}, 'duration': None, 'record_state': 'missing_result'}
        with sqlite3.connect(':memory:') as db:
            review.preserve_review_markers(db, {**snapshot, 'reviews': [pending]})
            changed = review.preserve_review_markers(db, {**snapshot, 'scan_timestamp_seconds': NOW+1})
            self.assertEqual([row['review_id'] for row in changed['retired_reviews']], ['pending-result'])
            self.assertEqual(changed['reviews'], [final])
            expired = review.preserve_review_markers(db, {**snapshot, 'reviews': [],
                'scan_timestamp_seconds': final['timestamp']+review.RETENTION_SECONDS})
            self.assertEqual(expired['retired_reviews'], [])
            self.assertEqual(db.execute('SELECT count(*) FROM review_snapshot_history').fetchone()[0], 0)

    def test_snapshot_history_cap_remains_visible_until_expiry(self):
        snapshot = self.scan()
        first = snapshot['reviews'][0]
        second = {**first, 'review_id': 'second', 'timestamp': first['timestamp']+1}
        with sqlite3.connect(':memory:') as db, mock.patch.object(review, 'SNAPSHOT_HISTORY_CAP', 1):
            limited = review.preserve_review_markers(db, {**snapshot, 'reviews': [first, second]})
            self.assertEqual(limited['limit_reached'], 1)
            self.assertEqual(limited['collection_complete'], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM review_snapshot_history').fetchone()[0], 1)
            later = review.preserve_review_markers(db, {**snapshot, 'reviews': [second], 'scan_timestamp_seconds': NOW+1})
            self.assertEqual(later['limit_reached'], 1)
            self.assertIn('history="legacy"', review.render_review_metrics(self.scan()).decode())

    def test_export_cap_and_actual_deletion_in_same_scan_survive_restart(self):
        def add(prefix, number):
            self.launch['started_at'] = datetime.fromtimestamp(NOW-100+number,timezone.utc).isoformat()
            self.result['uuid'] = '00000000-0000-4000-8000-'+str(number).zfill(12)
            self.write(prefix)
        add('second',2)
        before = self.scan()
        by_time = sorted(before['reviews'],key=lambda row:row['timestamp'])
        removed_id, capped_id = [row['review_id'] for row in by_time]
        db_path = self.root/'history.sqlite3'
        with sqlite3.connect(db_path) as db:
            review.preserve_review_markers(db,before)
            add('third',3);add('fourth',4)
            (self.root/'lane-launch-receipt.json').unlink()
            with mock.patch.object(review,'EXPORT_CAP',1):
                current = self.scan()
            self.assertEqual(len(current['eligible_review_ids']),3)
            self.assertIn(capped_id,current['eligible_review_ids'])
            changed = review.preserve_review_markers(db,current)
            self.assertEqual([row['review_id'] for row in changed['retired_reviews']],[removed_id])
        with sqlite3.connect(db_path) as db:
            restarted = review.preserve_review_markers(db,current)
            self.assertEqual(restarted['retired_reviews'],changed['retired_reviews'])
            changes = db.total_changes
            review.preserve_review_markers(db,current)
            self.assertEqual(db.total_changes,changes)

    def test_unreadable_scan_preserves_history_but_confirmed_conflict_withdraws(self):
        with sqlite3.connect(':memory:') as db:
            first = review.preserve_review_markers(db,self.scan())
            identity = first['reviews'][0]['review_id']
            self.write('copy')
            wrong = copy.deepcopy(self.result);wrong['usage']['output_tokens'] += 1
            (self.root/'copy-response.raw.json').write_text(json.dumps(wrong))
            def read(path):
                if path.name == 'unreadable-launch.json':raise OSError('unreadable')
                return path.read_bytes()
            absent = self.collector.scan(now=NOW,files=[self.root/'unreadable-launch.json'],reader=read)
            self.assertFalse(absent['membership_complete'])
            self.assertEqual(review.preserve_review_markers(db,absent)['retired_reviews'],[])
            files = list(self.root.glob('*-launch-receipt.json'))+[self.root/'unreadable-launch.json']
            conflict = self.collector.scan(now=NOW,files=files,reader=read)
            self.assertFalse(conflict['membership_complete'])
            self.assertIn(identity,conflict['withdrawn_review_ids'])
            self.assertEqual([r['review_id'] for r in review.preserve_review_markers(db,conflict)['retired_reviews']],[identity])

    def test_missing_pair_is_visible_and_final_replaces_it_during_partial_scan(self):
        response = self.root/'lane-response.raw.json'
        response.unlink()
        with sqlite3.connect(':memory:') as db:
            pending = review.preserve_review_markers(db,self.scan())
            row = pending['reviews'][0]
            self.assertEqual(row['record_state'],'missing_result')
            self.assertEqual(row['tokens'],{})
            self.assertEqual(row['requested_model'],self.launch['requested_model'])
            response.write_text(json.dumps(self.result))
            (self.root/'bad-launch.json').write_text('broken')
            final = review.preserve_review_markers(db,self.scan())
            self.assertFalse(final['membership_complete'])
            self.assertEqual(len(final['reviews']),1)
            self.assertEqual(final['reviews'][0]['tokens']['output'],50)
            self.assertEqual([r['review_id'] for r in final['retired_reviews']],[row['review_id']])

    def test_conflicting_effort_aliases_are_rejected(self):
        for field in ('requested_effort', 'executed_effort'):
            for value in ('low', None, 1, []):
                with self.subTest(field=field, value=value):
                    launch = {**self.launch, field: value}
                    with self.assertRaises(ValueError):
                        review.project(launch, self.result,
                                       {(self.launch['dispatch_id'], self.launch['packet_sha256']): NOW-110}, NOW)

    def test_preparation_sidecar_does_not_add_pending_result(self):
        preparation = {key: value for key, value in self.launch.items() if key != 'started_at'}
        (self.root/'lane-launch.json').write_text(json.dumps(preparation))
        self.assertEqual(self.scan()['collection_complete'], 1)
        self.assertEqual(self.scan()['source_files'], 1)

    def test_complete_new_pair_survives_bad_or_pending_legacy_receipt(self):
        (self.root/'lane-launch.json').write_text(json.dumps(self.launch))
        (self.root/'lane-response.json').write_text(json.dumps(self.result))
        (self.root/'lane-launch-receipt.json').write_text('{}')
        snapshot = self.scan()
        self.assertEqual(len(snapshot['reviews']), 1)
        self.assertEqual(snapshot['skipped_records']['invalid_record'], 1)
        self.write()
        (self.root/'lane-response.raw.json').unlink()
        snapshot = self.scan()
        self.assertEqual(len(snapshot['reviews']), 1)
        self.assertEqual(snapshot['pending_results'], 0)

    def test_valid_audit_survives_bad_alternate_with_visible_health(self):
        expected = self.scan()['reviews']
        (self.root/'contract-audit.jsonl').write_text('broken\n')
        snapshot = self.scan()
        self.assertEqual(snapshot['reviews'], expected)
        self.assertEqual(snapshot['skipped_records']['invalid_record'], 1)
        self.assertEqual(snapshot['collection_complete'], 0)

    def test_epoch_provenance_has_identical_identity_and_usage(self):
        (self.root/'lane-launch-receipt.json').unlink()
        value=self.write_provenance()
        original=self.scan()['reviews']
        value['start_epoch']=review._timestamp(value.pop('started_at'))
        value['end_epoch']=NOW-1
        (self.root/'review-provenance.json').write_text(json.dumps(value))
        row=self.scan()['reviews'][0]
        self.assertEqual(row['review_id'],original[0]['review_id'])
        self.assertEqual(row['tokens'],original[0]['tokens'])
        self.assertEqual(row['timestamp'],NOW-100)
        self.assertEqual(row['finished_timestamp'],NOW-1)
        for bad in (True, '1790000000', -1, 1e100):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                review.normalize_launch({'start_epoch':bad})
        with self.assertRaises(ValueError):
            review.normalize_launch({'start_epoch':NOW, 'started_at':self.launch['started_at']})

    def test_preparation_configs_are_visible_exclusions(self):
        (self.root/'plan-launch.json').write_text(json.dumps({'model':'model','effort_requested':'high',
            'entry':'review','argv':['claude','-p'],'packet_sha256':'a'*64}))
        snapshot=self.scan()
        self.assertEqual(len(snapshot['reviews']),1)
        self.assertEqual(snapshot['skipped_records']['preparation_record'],1)
        self.assertEqual(snapshot['collection_complete'],1)
        self.assertTrue(snapshot['membership_complete'])

    def test_provenance_final_usage_and_attempt_identity(self):
        (self.root/'lane-launch-receipt.json').unlink()
        self.write_provenance()
        first = self.scan()
        self.assertEqual(first['collection_complete'], 1)
        row = first['reviews'][0]
        self.assertEqual(row['tokens']['input'], 2)
        self.assertEqual(row['tokens']['output'], 50)
        self.assertEqual(row['effort'], 'high')
        self.assertFalse(row['cli_identity'])
        self.assertEqual(row['provenance_keys'], [])
        self.write_provenance(self.root/'copy')
        self.assertEqual(self.scan()['reviews'], [row])
        self.assertEqual(review.ReviewCollector(directories=[self.root]).scan(now=NOW)['reviews'], [row])
        payload = review.render_review_metrics(first).decode()
        self.assertNotIn('PRIVATE', payload)
        self.assertNotIn('dispatch-example', payload)
        self.assertNotIn('cwo_review_session_info{', payload)

    def test_mixed_formats_count_once_preserving_cli_identity(self):
        expected = self.scan()['reviews'][0]
        self.write_provenance()
        result = self.scan()
        self.assertEqual(result['collection_complete'], 1)
        self.assertEqual(result['reviews'], [expected])
        self.result['usage']['output_tokens'] = 60
        self.write_provenance()
        result = self.scan()
        self.assertEqual(result['reviews'], [])
        self.assertGreater(result['skipped_records']['conflicting_result'], 0)

    def test_provenance_requires_prompt_hash_headers_and_audited_launch(self):
        (self.root/'lane-launch-receipt.json').unlink()
        provenance = self.write_provenance()
        prompt_path = self.root/'review-prompt.txt'
        prompt_path.write_bytes(prompt_path.read_bytes()+b'changed')
        self.assertEqual(self.scan()['skipped_records']['invalid_record'], 1)
        self.write_provenance()
        (self.root/'contract-audit.jsonl').write_bytes(record('dispatch_prepared', timestamp=NOW-90,
            dispatch_id=self.launch['dispatch_id'], packet_sha256=self.launch['packet_sha256']))
        # Remove the older matching audit so the future prepare cannot qualify.
        (self.root/'audit.jsonl').unlink()
        self.assertEqual(self.scan()['skipped_records']['unmatched_dispatch'], 1)
        self.write_provenance()
        prompt_path.write_bytes(b'PRIVATE_PROMPT\n')
        provenance['prompt_sha256'] = hashlib.sha256(prompt_path.read_bytes()).hexdigest()
        (self.root/'review-provenance.json').write_text(json.dumps(provenance))
        self.assertEqual(self.scan()['skipped_records']['invalid_record'], 1)

    def test_provenance_cannot_resurrect_conflicting_cli_copies(self):
        self.write_provenance()
        self.result['usage']['output_tokens'] = 60
        self.write(prefix='conflict')
        snapshot = self.scan()
        self.assertEqual(snapshot['reviews'], [])
        self.assertGreater(snapshot['skipped_records']['conflicting_result'], 0)
        self.assertEqual(snapshot['collection_complete'], 0)

    def test_missing_provenance_prompt_is_a_saved_evidence_gap(self):
        (self.root/'lane-launch-receipt.json').unlink()
        self.write_provenance()
        (self.root/'review-prompt.txt').unlink()
        snapshot = self.scan()
        self.assertEqual(snapshot['source_errors'], 0)
        self.assertEqual(snapshot['skipped_records']['missing_provenance'], 1)
        self.assertEqual(snapshot['pending_results'], 0)

    def test_provenance_pending_and_multiple_final_results(self):
        (self.root/'lane-launch-receipt.json').unlink()
        self.write_provenance(final=False)
        self.assertEqual(self.scan()['pending_results'], 1)
        provenance = self.write_provenance()
        provenance['events'].append(provenance['events'][-1])
        (self.root/'review-provenance.json').write_text(json.dumps(provenance))
        self.assertEqual(self.scan()['reviews'], [])
        self.assertEqual(self.scan()['skipped_records']['invalid_record'], 1)

    def test_provenance_retries_have_separate_recorded_launch_times(self):
        (self.root/'lane-launch-receipt.json').unlink()
        self.write_provenance()
        self.launch['started_at'] = datetime.fromtimestamp(NOW-50, timezone.utc).isoformat()
        self.write_provenance(self.root/'retry')
        self.assertEqual(len(self.scan()['reviews']), 2)

    def test_provenance_prompt_symlink_is_rejected(self):
        (self.root/'lane-launch-receipt.json').unlink()
        self.write_provenance()
        prompt = self.root/'review-prompt.txt'
        prompt.rename(self.root/'real-prompt.txt')
        prompt.symlink_to(self.root/'real-prompt.txt')
        self.assertEqual(self.scan()['source_errors'], 1)

    def test_exact_top_level_usage_models_duration_and_privacy(self):
        snap = self.scan();self.assertEqual(snap['collection_complete'], 1)
        row = snap['reviews'][0]
        self.assertEqual(row['tokens'], {'input': 2, 'cache_creation': 100, 'cache_read': 20, 'output': 50, 'thinking': 30})
        self.assertEqual(row['duration'], 1.25)
        self.assertEqual((row['requested_model'], row['reported_model']), ('example-requested', 'example-reported'))
        payload = review.render_review_metrics(snap).decode()
        for private in ['PRIVATE', str(self.root), 'dispatch-example', self.result['uuid'], 'iterations']:
            self.assertNotIn(private, payload)
        self.assertEqual(len(self.scan()['reviews']), 1)
        self.assertEqual(review.ReviewCollector(directories=[self.root]).scan(now=NOW), snap)

    def test_dedup_copies_and_conflicting_bindings_are_omitted(self):
        self.write(prefix='copy')
        self.assertEqual(len(self.scan()['reviews']), 1)
        self.launch['dispatch_id'] = 'other-dispatch'
        self.write(directory=self.root/'second')
        result = self.scan();self.assertEqual(result['reviews'], [])
        self.assertEqual(result['skipped_records']['conflicting_result'], 1)
        self.assertEqual(result['collection_complete'], 0)

    def test_failures_zero_missing_and_thinking_subset(self):
        self.result.update(is_error=True, duration_ms=0)
        self.result['usage'] = {'input_tokens': 0};self.write()
        row = self.scan()['reviews'][0]
        self.assertEqual((row['outcome'], row['duration'], row['tokens']), ('failed', 0, {'input': 0}))
        self.result.pop('usage');self.result.pop('duration_ms');self.write()
        row = self.scan()['reviews'][0];self.assertEqual(row['tokens'], {});self.assertIsNone(row['duration'])
        self.result['usage'] = {'output_tokens': 1, 'output_tokens_details': {'thinking_tokens': 2}};self.write()
        self.assertEqual(self.scan()['skipped_records']['invalid_record'], 1)

    def test_optional_provenance_conflict_does_not_change_review_accounting(self):
        self.launch['prompt_sha256'] = 'c'*64
        self.write(prefix='copy')
        rows=self.scan()['reviews']
        self.assertEqual(len(rows),1)
        self.assertTrue(rows[0]['provenance_conflict'])
        self.assertEqual(rows[0]['tokens']['output'],50)

    def test_pending_result_missing_audit_unmatched_and_future(self):
        (self.root/'lane-response.raw.json').unlink()
        self.assertEqual(self.scan()['pending_results'], 1)
        self.write();(self.root/'audit.jsonl').unlink()
        missing = self.scan()
        self.assertEqual(missing['source_errors'], 0)
        self.assertEqual(missing['skipped_records']['missing_provenance'], 1)
        self.assertEqual(missing['collection_complete'], 0)
        self.write();(self.root/'audit.jsonl').write_bytes(record('dispatch_prepared', dispatch_id='other', packet_sha256='a'*64))
        self.assertEqual(self.scan()['skipped_records']['unmatched_dispatch'], 1)
        self.write();self.assertEqual(self.scan(now=NOW-101)['skipped_records']['future_timestamp'], 1)
        self.assertEqual(self.scan(now=NOW+review.RETENTION_SECONDS)['reviews'], [])

    def test_invalid_values_hashes_and_unqualified_times(self):
        for value in [True, -1, 1.5, 2**53, '3']:
            with self.subTest(value=value):
                self.result['usage']['input_tokens'] = value;self.write()
                self.assertEqual(self.scan()['reviews'], [])
        self.launch,self.result = fixtures();self.write()
        path = self.root/'audit.jsonl';path.write_bytes(path.read_bytes().replace(b'dispatch_prepared', b'packet_built'))
        self.assertEqual(self.scan()['skipped_records']['invalid_record'], 1)
        self.launch['started_at'] = '2027-01-15T08:00:00';self.write()
        self.assertEqual(self.scan()['reviews'], [])

    def test_symlinks_fifo_oversize_rewrite_and_discovery(self):
        raw = self.root/'lane-response.raw.json';raw.unlink();os.mkfifo(raw)
        self.assertEqual(self.scan()['source_errors'], 1)
        raw.unlink();raw.symlink_to(self.root/'lane-launch-receipt.json')
        self.assertEqual(self.scan()['source_errors'], 1)
        raw.unlink();self.write()
        with mock.patch.object(review, 'MAX_FILE_BYTES', 1):
            self.assertEqual(self.scan()['limit_reached'], 1)
        before = raw.stat();self.result['usage']['input_tokens'] = 3;self.write()
        os.utime(raw, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(self.scan()['reviews'][0]['tokens']['input'], 3)
        self.result['uuid'] = '00000000-0000-4000-8000-000000000003';self.write(directory=self.root/'future-run')
        self.assertEqual(len(self.scan()['reviews']), 2)
        with mock.patch.object(review, 'EXPORT_CAP', 1):
            self.assertEqual(self.scan()['limit_reached'], 1)

    def test_optional_cli_and_protected_outputs(self):
        home = self.root/'codex';(home/'sessions').mkdir(parents=True)
        args = [sys.executable, str(ROOT/'scripts/collect_codex_sessions.py'), '--codex-home', str(home),
                '--session-state-dir', str(self.root/'state'), '--snapshot-file', str(self.root/'snapshot.json'), '--once']
        with tempfile.TemporaryDirectory() as separate:
            target = Path(separate);self.write(directory=target)
            result = subprocess.run(args+['--cwo-review-dir', str(target)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('cwo_reviews', json.loads(result.stdout))
        endpoint = SessionMetricsEndpoint('127.0.0.1', 0, b'synthetic-test-token')
        try:
            snapshot = json.loads((self.root/'snapshot.json').read_text())
            endpoint.update_sessions(snapshot, reviews=self.scan())
            self.assertIn(b'cwo_codex_collector_scan_timestamp_seconds', endpoint._payload)
            self.assertIn(b'cwo_review_started_timestamp_seconds{', endpoint._payload)
            endpoint.update_sessions(snapshot)
            self.assertNotIn(b'cwo_review_', endpoint._payload)
        finally:
            endpoint.close()
        for path in ['relative', str(self.root), str(self.root/'state')]:
            self.assertEqual(subprocess.run(args+['--cwo-review-dir', path], capture_output=True).returncode, 2)
