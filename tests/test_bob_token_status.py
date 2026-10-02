"""Exact saved-token provenance, independently of collection completeness."""
from pathlib import Path
import re
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from traceonaut.bob_session_telemetry import (
    BobCollector, SAFE_INTEGER, TOKEN_STATUS, project_task,
    reconcile_usage, render_bob_metrics,
)
from bob_fixtures import database, task, message


class BobTokenStatusTests(unittest.TestCase):
    now = 1_800_000_000

    def collect(self, tasks, messages=()):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        writer = database(root / 'bob/db/bob.db', tasks, messages)
        self.addCleanup(writer.close)
        collector = BobCollector(root / 'bob', root / 'state')
        self.addCleanup(collector.close)
        for _ in range(100):
            snapshot = collector.scan(now=self.now)
            if not snapshot['pending']:
                self.assertEqual(snapshot['source_available'], 1)
                return snapshot
        self.fail('scan did not finish')

    def test_qualified_omission_keeps_activity_and_complete_collection(self):
        messages = [message(str(i)) for i in range(19)] + [
            message('tool-' + str(i), role='tool', data={
                'toolUsage': {'signature': {'isError': i < 3}}}) for i in range(31)]
        snapshot = self.collect([task(costs={'cost': 2, 'contextTokens': 4096})], messages)
        row = snapshot['sessions'][0]
        self.assertEqual((row['responses'], row['tool_results'], row['tool_errors']), (19, 31, 3))
        self.assertEqual(snapshot['collection_complete'], 1)
        self.assertEqual(row['partial'], 1)  # Legacy broad flag is unchanged.
        self.assertTrue(all(value is None for value in row['usage'].values()))
        self.assertEqual(set(row['usage_status'].values()), {'not_recorded'})
        payload = render_bob_metrics(snapshot).decode()
        self.assertNotRegex(payload, r'(?m)^traceonaut_bob_session_usage_tokens\{')
        self.assertEqual(len(re.findall(r'(?m)^traceonaut_bob_session_token_status\{.*\} 1$', payload)), 3)

    def test_each_field_and_zero_are_independent_of_cache(self):
        cases = [({}, (None, None, None), ('not_recorded', 'not_recorded', 'not_recorded')),
                 ({'input': None, 'output': None}, (None, None, None), ('not_recorded',) * 3),
                 ({'input': 0, 'output': 0}, (0, 0, 0), ('recorded',) * 3),
                 ({'input': 17}, (17, None, None), ('recorded', 'not_recorded', 'not_recorded')),
                 ({'output': 3}, (None, 3, None), ('not_recorded', 'recorded', 'not_recorded')),
                 ({'input': 17, 'output': 3}, (17, 3, 20), ('recorded',) * 3)]
        for costs, values, reasons in cases:
            with self.subTest(costs=costs):
                snapshot = self.collect([task(costs=costs)])
                row = snapshot['sessions'][0]
                self.assertEqual(snapshot['collection_complete'], 1)
                self.assertEqual(tuple(row['usage'][k] for k in ('input', 'output', 'total')), values)
                self.assertEqual(tuple(row['usage_status'][k] for k in ('input', 'output', 'total')), reasons)

    def test_invalid_values_and_overflow_remain_collection_problems(self):
        for invalid in (True, False, -1, 1.5, '10', [], {}, SAFE_INTEGER + 1):
            with self.subTest(invalid=invalid):
                snapshot = self.collect([task(costs={'input': invalid, 'output': 3})])
                row = snapshot['sessions'][0]
                self.assertEqual(snapshot['collection_complete'], 0)
                self.assertEqual(row['usage_status']['input'], 'invalid')
                self.assertEqual(row['usage_status']['total'], 'invalid')
                self.assertEqual(row['usage']['output'], 3)
                self.assertIsNone(row['usage']['total'])
        snapshot = self.collect([task(costs={'input': SAFE_INTEGER, 'output': 1})])
        self.assertEqual(snapshot['sessions'][0]['usage_status']['total'], 'invalid')
        self.assertEqual(snapshot['collection_complete'], 0)
        snapshot = self.collect([task(costs={'input': 1, 'output': 2, 'cacheRead': -1})])
        self.assertEqual(snapshot['sessions'][0]['usage']['total'], 3)
        self.assertEqual(snapshot['collection_complete'], 0)

    def test_parent_withholding_is_never_mislabeled_as_source_omission(self):
        for costs, newer in (({}, False), ({'input': -1, 'output': 2}, False),
                             ({'input': 500, 'output': 2}, False),
                             ({'input': 10, 'output': 2}, True)):
            with self.subTest(costs=costs, newer=newer):
                rows = [task('parent'), task('child', parent='parent', kind='subtask',
                    status='completed', costs=costs, now=self.now + int(newer))]
                snapshot = self.collect(rows)
                parent = next(r for r in snapshot['sessions'] if r['parent_id'] is None)
                self.assertEqual(parent['usage_status']['input'], 'reconciliation_unavailable')
                self.assertEqual(parent['usage_status']['total'], 'reconciliation_unavailable')
                self.assertIsNone(parent['usage']['total'])
                self.assertEqual(snapshot['collection_complete'], 0)
        snapshot = self.collect([task('parent'), task('child', parent='parent', kind='subtask',
            status='completed', costs={'input': 10, 'output': 2, 'cacheRead': 0, 'cacheWrite': 0})])
        self.assertEqual(sum(r['usage']['total'] for r in snapshot['sessions']), 120)
        self.assertEqual(snapshot['collection_complete'], 1)

    def test_unknown_outcomes_skips_pending_and_outages_are_not_hidden(self):
        snapshot = self.collect([task(costs={})], [message(role='tool', data={})])
        self.assertEqual(snapshot['collection_complete'], 0)
        self.assertEqual(snapshot['sessions'][0]['tool_unknown'], 1)
        snapshot = self.collect([task(costs={})], [message(data={'role': 'unsupported'})])
        self.assertEqual(snapshot['collection_complete'], 0)
        self.assertEqual(snapshot['skipped_records']['invalid_record'], 1)
        # Pending, missing-source and recovery checks also cover unchanged
        # source scheduling in test_bob_collector and test_bob_incremental.

    def test_empty_healthy_source_is_complete(self):
        snapshot = self.collect([])
        self.assertEqual(snapshot['collection_complete'], 1)
        self.assertEqual(snapshot['sessions'], [])

    def test_legacy_status_is_absent_not_inferred(self):
        snapshot = self.collect([task(costs={})])
        del snapshot['sessions'][0]['usage_status']
        payload = render_bob_metrics(snapshot).decode()
        self.assertNotRegex(payload, r'(?m)^traceonaut_bob_session_token_status\{')
        row = project_task(task(), self.now)
        del row['usage_status']
        usage, status = reconcile_usage([row])
        self.assertEqual(usage[row['session_id']]['total'], 120)
        self.assertNotIn('total', status[row['session_id']])

    def test_metric_bounds_and_privacy(self):
        self.assertEqual(TOKEN_STATUS, {'recorded': 0, 'not_recorded': 1, 'invalid': 2,
                                       'reconciliation_unavailable': 3})
        snapshot = self.collect([task()])
        for reason, code in TOKEN_STATUS.items():
            row = snapshot['sessions'][0]
            row['usage_status'] = dict.fromkeys(('input', 'output', 'total', 'PRIVATE_SENTINEL'), reason)
            payload = render_bob_metrics(snapshot).decode()
            lines = [s for s in payload.splitlines() if s.startswith('traceonaut_bob_session_token_status{')]
            self.assertEqual(len(lines), 3)
            for line in lines:
                self.assertTrue(line.endswith(' ' + str(code)))
                self.assertEqual(set(re.findall(r'(\w+)=', line)), {'project_id', 'session_id', 'token_kind'})
                self.assertNotIn('PRIVATE_SENTINEL', line)
            row['usage_status']['input'] = 'PRIVATE_SENTINEL'
            self.assertNotIn('PRIVATE_SENTINEL', render_bob_metrics(snapshot).decode())


if __name__ == '__main__':
    unittest.main()
