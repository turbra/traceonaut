"""The public metric inventory matches emitted names, types and label keys."""

from pathlib import Path
import re
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from traceonaut.codex_account_telemetry import render_account_metrics
from traceonaut.codex_session_telemetry import SessionCollector, render_session_metrics
from traceonaut.observability_contract import METRIC_FAMILIES


class MetricsReferenceTests(unittest.TestCase):
    def test_names_types_and_labels_match_emitters(self):
        now = time.time()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "codex/sessions").mkdir(parents=True)
            collector = SessionCollector(root / "codex", root / "state")
            try:
                collector.scan()
                snapshot = collector.snapshot(now=now)
            finally:
                collector.close()
        ids = {"project_id": "example-project", "session_id": "00000000-0000-4000-8000-000000000001"}
        snapshot["sessions"] = [{
            **ids, "kind": "session", "parent_id": "none", "model": "example-model", "effort": "high",
            "last_event": now, "created": now, "state": 1, "usage_state": 1, "reported_tokens": 30,
            "responses": 1, "completed_turns": 1, "failed_turns": 0, "observed_turn_seconds": 2,
            "usage": {"total": 30, "input": 20, "output": 10},
        }]
        snapshot["reliability"]["command_observations"] = [{
            **ids, "observation_id": "example-command", "outcome": "completed",
            "timestamp": now, "duration_seconds": 2,
        }]
        snapshot["reliability"]["compaction_observations"] = [{
            **ids, "observation_id": "example-compaction", "timestamp": now,
        }]
        payload = render_session_metrics(snapshot) + render_account_metrics({
            "available": True, "last_attempt": now, "last_success": now,
            "values": {"reset_credits": 2, "windows": {"primary": {
                "usedPercent": 20, "windowDurationMins": 10080, "resetsAt": now + 100,
            }}},
        }, now=now)
        types = dict(re.findall(r"^# TYPE (\w+) (\w+)$", payload.decode(), re.M))
        actual = {}
        for line in payload.decode().splitlines():
            match = re.match(r"^(cwo_\w+)(?:\{([^}]*)\})? ", line)
            if match:
                name, label_text = match.groups()
                labels = frozenset(re.findall(r'(\w+)=', label_text or ""))
                actual[name] = (types.get(name, "untyped"), labels)
        actual.update({name: (family.metric_type, frozenset(family.labels)) for name, family in METRIC_FAMILIES.items()})
        reference = (ROOT / "references/reference/metrics.md").read_text()
        documented = {}
        for name, kind, labels in re.findall(r"^\| `(cwo_\w+)` \| (\w+) \| ([^|]+) \|", reference, re.M):
            self.assertNotIn(name, documented)
            documented[name] = (kind, frozenset(re.findall(r"`(\w+)`", labels)))
        self.assertEqual(documented, actual)


if __name__ == "__main__":
    unittest.main()
