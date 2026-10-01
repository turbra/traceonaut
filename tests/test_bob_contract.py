import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from traceonaut.bob_session_telemetry import exclusive_usage, project_task, project_message, SAFE_INTEGER
from bob_fixtures import task, message


class BobContractTests(unittest.TestCase):
    now = 1_800_000_000

    def test_cache_is_subset_and_messages_are_not_added(self):
        item = project_task(task(), self.now)
        usage = exclusive_usage([item])[item["session_id"]]
        self.assertEqual(usage["total"], 120)
        self.assertEqual(usage["cached_input"], 80)
        reply = project_message(message(), self.now)
        self.assertEqual(reply["responses"], 1)
        self.assertNotIn("spend", json.dumps(reply))
        self.assertNotIn("PRIVATE_SENTINEL", json.dumps(reply))

    def test_completed_subtask_is_subtracted_but_subagent_is_not(self):
        items = [project_task(row, self.now) for row in (
            task("parent"), task("child", parent="parent", kind="subtask", status="completed",
                                 costs={"input": 30, "output": 5, "cacheRead": 10, "cacheWrite": 0}),
            task("agent", parent="parent", kind="subagent"))]
        usage = exclusive_usage(items)
        self.assertEqual(usage[items[0]["session_id"]]["total"], 85)
        self.assertEqual(sum(v["total"] for v in usage.values()), 240)
        items[1]["completed_subtask"] = False
        self.assertEqual(exclusive_usage(items)[items[0]["session_id"]]["total"], 120)

    def test_missing_zero_overflow_and_inconsistent_parent(self):
        for value, expected in ((None, None), (0, 0), (-1, None), (True, None), (SAFE_INTEGER + 1, None)):
            row = project_task(task(costs={"input": value, "output": 0}), self.now)
            self.assertEqual(exclusive_usage([row])[row["session_id"]]["total"], expected)
        parent = project_task(task("parent", costs={"input": 1}), self.now)
        child = project_task(task("child", parent="parent", kind="subtask", status="completed"), self.now)
        self.assertIsNone(exclusive_usage([parent, child])[parent["session_id"]]["input"])

    def test_child_saved_before_parent_is_not_assumed_merged(self):
        parent = project_task(task('parent'), self.now)
        child = project_task(task('child', parent='parent', kind='subtask', status='completed', now=self.now + 1), self.now + 1)
        self.assertIsNone(exclusive_usage([parent, child])[parent['session_id']]['total'])

    def test_unknown_shapes_and_versions_are_rejected(self):
        for field, value, reason in (("costs", '{"input":1,"input":2}', "invalid_json"),
                                     ("costs", '[]', "invalid_json"),
                                     ("version", '3.0.0', "unsupported_version"),
                                     ("task_type", 'unknown', "unsupported_kind"),
                                     ("created_at", int((self.now + 301) * 1000), "invalid_timestamp")):
            row = task()
            row[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, reason):
                project_task(row, self.now)

    def test_tool_outcomes_do_not_infer_command_success(self):
        for value, errors, unknown in ((False, 0, 0), (True, 1, 0), (None, 0, 1), (0, 0, 1)):
            row = message(role="tool", data={"toolUsage": {"signature": {"isError": value,
                "arguments": {"command": "PRIVATE_SENTINEL"}}}, "_meta": {"durationMs": 250}})
            item = project_message(row, self.now)
            self.assertEqual((item["tools"], item["errors"], item["unknown"], item["duration"]),
                             (1, errors, unknown, .25))
            self.assertNotIn("PRIVATE_SENTINEL", json.dumps(item))

    def test_local_ui_message_is_not_response_and_timestamp_is_milliseconds(self):
        item = project_message(message(data={"_meta": {"notAi": True}}), self.now)
        self.assertEqual(item["responses"], 0)
        self.assertEqual(item["at"], self.now)


if __name__ == "__main__":
    unittest.main()
