"""Paired CLI results: source identity, exact accounting, privacy and failures."""
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
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

    def test_pending_result_missing_audit_unmatched_and_future(self):
        (self.root/'lane-response.raw.json').unlink()
        self.assertEqual(self.scan()['pending_results'], 1)
        self.write();(self.root/'audit.jsonl').unlink()
        self.assertEqual(self.scan()['source_errors'], 1)
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
