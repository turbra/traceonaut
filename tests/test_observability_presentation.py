from __future__ import annotations

import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from traceonaut.observability_presentation import (
    PRESENTATION_REGISTRY_SCHEMA,
    PresentationRegistryError,
    empty_presentation_registry,
    load_presentation_registry,
    record_presentation_submission,
    safe_load_presentation_registry,
    write_presentation_registry,
)


class ObservabilityPresentationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.path = self.root / "presentation.json"
        self.project_id = str(uuid.uuid4())
        self.dispatch_id = str(uuid.uuid4())

    def test_schema_and_safe_missing_fallback_are_machine_readable(self) -> None:
        json.dumps(PRESENTATION_REGISTRY_SCHEMA, allow_nan=False)
        self.assertEqual(PRESENTATION_REGISTRY_SCHEMA["properties"]["version"], {"const": 1})
        self.assertEqual(
            safe_load_presentation_registry(self.path),
            empty_presentation_registry(),
        )
        with self.assertRaisesRegex(PresentationRegistryError, "missing"):
            load_presentation_registry(self.path)

    def test_record_submission_atomically_preserves_explicit_context(self) -> None:
        result = record_presentation_submission(
            self.path,
            project_id=self.project_id,
            project_name="CWO Observability",
            dispatch_id=self.dispatch_id,
            task_name="Qualification pass 1",
            agent_name="Standard Codex Sol xhigh",
            work_item_title="Dashboard context qualification",
        )

        self.assertEqual(load_presentation_registry(self.path), result)
        self.assertEqual(
            result["projects"][self.project_id], {"name": "CWO Observability"}
        )
        self.assertEqual(
            result["dispatches"][self.dispatch_id],
            {
                "task_name": "Qualification pass 1",
                "agent_name": "Standard Codex Sol xhigh",
                "work_item_title": "Dashboard context qualification",
            },
        )
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertFalse(any(path.suffix == ".tmp" for path in self.root.iterdir()))

        second_dispatch = str(uuid.uuid4())
        updated = record_presentation_submission(
            self.path,
            project_id=self.project_id,
            project_name="CWO Observability",
            dispatch_id=second_dispatch,
            task_name="Qualification pass 2",
            agent_name="Standard Codex Sol xhigh",
        )
        self.assertEqual(len(updated["projects"]), 1)
        self.assertEqual(len(updated["dispatches"]), 2)

    def test_old_job_context_records_project_without_inventing_dispatch(self) -> None:
        result = record_presentation_submission(
            self.path,
            project_id=self.project_id,
            project_name="CWO Observability",
            dispatch_id=self.dispatch_id,
        )

        self.assertEqual(result["projects"][self.project_id]["name"], "CWO Observability")
        self.assertEqual(result["dispatches"], {})

    def test_controls_partial_context_and_excluded_paths_are_rejected(self) -> None:
        cases = (
            {"project_name": "bad\nname"},
            {"project_name": "bad\ud800name"},
            {"task_name": "task", "agent_name": None},
            {
                "task_name": "task",
                "agent_name": "agent",
                "work_item_title": "bad\u0000title",
            },
        )
        for overrides in cases:
            values = {
                "project_name": "Project",
                "task_name": "Task",
                "agent_name": "Agent",
            }
            values.update(overrides)
            with self.subTest(overrides=overrides), self.assertRaises(PresentationRegistryError):
                record_presentation_submission(
                    self.path,
                    project_id=self.project_id,
                    dispatch_id=self.dispatch_id,
                    **values,
                )

        excluded = self.root / "receipts"
        excluded.mkdir(mode=0o700)
        with self.assertRaisesRegex(PresentationRegistryError, "excluded"):
            record_presentation_submission(
                excluded / "presentation.json",
                project_id=self.project_id,
                project_name="Project",
                dispatch_id=self.dispatch_id,
                task_name="Task",
                agent_name="Agent",
                excluded_roots=(excluded,),
            )

    def test_owner_only_paths_and_corrupt_registry_fail_safely(self) -> None:
        public = self.root / "public"
        public.mkdir(mode=0o755)
        with self.assertRaisesRegex(PresentationRegistryError, "owner-only"):
            write_presentation_registry(
                public / "presentation.json", empty_presentation_registry()
            )

        self.path.write_text("{broken", encoding="utf-8")
        self.path.chmod(0o600)
        self.assertEqual(
            safe_load_presentation_registry(self.path),
            empty_presentation_registry(),
        )
        with self.assertRaisesRegex(PresentationRegistryError, "JSON invalid"):
            load_presentation_registry(self.path)

        self.path.write_text(json.dumps(empty_presentation_registry()), encoding="utf-8")
        self.path.chmod(0o644)
        self.assertEqual(
            safe_load_presentation_registry(self.path),
            empty_presentation_registry(),
        )
        with self.assertRaisesRegex(PresentationRegistryError, "owner-only"):
            load_presentation_registry(self.path)


if __name__ == "__main__":
    unittest.main()
