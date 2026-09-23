"""Render-result reuse must not bypass input or output protection."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import render_codex_beta_dashboard as beta
from test_codex_beta_renderer import snapshot, template


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


class RenderCacheTests(unittest.TestCase):
    def test_complete_real_dashboard_matches_uncached_and_hit_skips_render(self):
        draft = json.loads((ROOT / "examples/observability/codex-work-overview-beta.json").read_text())
        source = snapshot()
        aliases = {"version": 1, "sessions": {"parent": "Named work"}}
        expected = encoded(beta.render_dashboard(draft, source, "prometheus", aliases))
        cache = beta._RenderCache()
        with mock.patch.object(beta, "render_dashboard", wraps=beta.render_dashboard) as render:
            for _ in range(3):
                self.assertEqual(encoded(cache.render(copy.deepcopy(draft), copy.deepcopy(source), "prometheus", copy.deepcopy(aliases))), expected)
            self.assertEqual(render.call_count, 1)

    def test_each_projected_field_invalidates_and_ignored_metadata_does_not(self):
        cache = beta._RenderCache()
        source = snapshot()
        original = beta.render_dashboard
        changes = {
            "session_id": "replacement", "project_id": "new-project",
            "title": "New title", "project_name": "New project",
            "agent_name": "New agent", "kind": "subagent", "parent_id": "other",
        }
        with mock.patch.object(beta, "render_dashboard", wraps=original) as render:
            for field, value in changes.items():
                with self.subTest(field=field):
                    cache.render(template(), source)
                    changed = copy.deepcopy(source)
                    # The fixture shares a project; a name change must remain
                    # valid for every row in that project.
                    if field == "project_name":
                        for row in changed["sessions"]:
                            if row["project_id"] == "source":
                                row[field] = value
                    else:
                        changed["sessions"][0][field] = value
                    before = render.call_count
                    self.assertEqual(encoded(cache.render(template(), changed)), encoded(original(template(), changed)))
                    self.assertEqual(render.call_count, before + 1)
            cache.render(template(), source)
            before = render.call_count
            source["collected_at"] = 42
            source["sessions"][0].update(tokens=900, prompt="PRIVATE NOT RENDERED")
            result = cache.render(template(), source)
            self.assertEqual(render.call_count, before)
            self.assertNotIn("PRIVATE NOT RENDERED", encoded(result).decode())

    def test_order_template_labels_datasource_and_json_types_are_in_key(self):
        original = beta.render_dashboard
        variants = []
        source = snapshot()
        variants.append((template(), source, None, None))
        variants.append((template(), {**source, "sessions": source["sessions"][::-1]}, None, None))
        variants.append(({**template(), "title": "Updated"}, source, None, None))
        variants.append((template(), source, None, {"version": 1, "sessions": {"parent": "Alias"}}))
        variants.append((template(), source, "prometheus", None))
        variants.append((template(), source, None, None))
        variants.append(({**template(), "probe": True}, source, None, None))
        variants.append(({**template(), "probe": 1}, source, None, None))
        variants.append(({**template(), "probe": 1.0}, source, None, None))
        variants.append((dict(reversed(list(variants[-1][0].items()))), source, None, None))
        cache = beta._RenderCache()
        with mock.patch.object(beta, "render_dashboard", wraps=original) as render:
            for index, args in enumerate(variants):
                self.assertEqual(encoded(cache.render(*args)), encoded(original(*args)))
                self.assertEqual(render.call_count, index + 1)

    def test_equivalent_validated_metadata_does_not_invalidate(self):
        cache = beta._RenderCache()
        source = snapshot()
        with mock.patch.object(beta, "render_dashboard", wraps=beta.render_dashboard) as render:
            expected = encoded(cache.render(template(), source))
            source["sessions"][0]["title"] = "  Build   dashboard  "
            self.assertEqual(encoded(cache.render(template(), source)), expected)
            self.assertEqual(render.call_count, 1)

    def test_invalid_inputs_cannot_reuse_previous_result(self):
        cases = [
            ({**template(), "uid": "foreign"}, snapshot(), None, None),
            (template(), {"version": 2, "sessions": []}, None, None),
            (template(), snapshot(), "bad uid", None),
            (template(), snapshot(), None, {"version": 1, "sessions": {"parent": "$project"}}),
        ]
        bad_selector = template()
        bad_selector["templating"]["list"][1]["label"] = "Wrong"
        cases.append((bad_selector, snapshot(), None, None))
        for args in cases:
            with self.subTest(args=args):
                cache = beta._RenderCache()
                expected = encoded(cache.render(template(), snapshot()))
                with self.assertRaises(ValueError):
                    cache.render(*args)
                self.assertEqual(encoded(cache.render(template(), snapshot())), expected)

    def test_hit_still_repairs_deleted_or_valid_same_uid_output(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "beta.json"
            cache = beta._RenderCache()
            original = beta.render_dashboard
            with mock.patch.object(beta, "render_dashboard", wraps=original) as render:
                for tamper in (None, "delete", "replace"):
                    if tamper == "delete":
                        path.unlink()
                    elif tamper == "replace":
                        path.write_text(json.dumps({"uid": beta.BETA_UID, "title": "stale"}))
                    self.assertTrue(beta.write_beta_dashboard(path, cache.render(template(), snapshot())))
                    self.assertEqual(path.read_bytes(), encoded(original(template(), snapshot())))
                self.assertEqual(render.call_count, 1)

    def test_hit_preserves_corrupt_foreign_symlink_size_and_fifo_refusal(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "beta.json"
            cache = beta._RenderCache()
            cache.render(template(), snapshot())
            for payload in (b"not JSON", b'{"uid":"foreign"}', b"[]"):
                path.write_bytes(payload)
                with self.assertRaises(ValueError):
                    beta.write_beta_dashboard(path, cache.render(template(), snapshot()))
                self.assertEqual(path.read_bytes(), payload)
            path.write_text(json.dumps({"uid": beta.BETA_UID}))
            with mock.patch.object(beta, "MAX_DASHBOARD_BYTES", 1), self.assertRaises(ValueError):
                beta.write_beta_dashboard(path, cache.render(template(), snapshot()))
            path.unlink()
            target = root / "target.json"
            target.write_text(json.dumps({"uid": beta.BETA_UID}))
            path.symlink_to(target)
            with self.assertRaises(ValueError):
                beta.write_beta_dashboard(path, cache.render(template(), snapshot()))
            path.unlink()
            os.mkfifo(path)
            with self.assertRaises(ValueError):
                beta.write_beta_dashboard(path, cache.render(template(), snapshot()))

    def run_loop(self, root, transition, *, watch=True):
        source, draft, labels, output = [root / name for name in ("snapshot.json", "template.json", "labels.json", "beta.json")]
        source.write_bytes(encoded(snapshot()))
        source.chmod(0o600)
        draft.write_bytes(encoded(template()))
        labels.write_bytes(encoded({"version": 1}))
        labels.chmod(0o600)
        calls = []

        class Stop:
            def set(self):
                pass

            def wait(self, seconds):
                calls.append(seconds)
                return transition(len(calls), source, draft, labels, output)

        args = ["--template", str(draft), "--snapshot-file", str(source), "--labels-file", str(labels), "--output", str(output)]
        if watch:
            args += ["--watch-seconds", "2"]
        with mock.patch.object(beta.threading, "Event", return_value=Stop()), mock.patch.object(beta.signal, "signal"), mock.patch("builtins.print"):
            result = beta.main(args)
        return result, calls, output

    def test_watch_retains_validation_writer_and_input_change_order(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            def transition(cycle, source, draft, labels, output):
                self.assertTrue(output.exists())
                if cycle == 1:
                    output.unlink()
                if cycle == 2:
                    changed = snapshot()
                    changed["sessions"][0]["title"] = "Changed while watching"
                    source.write_bytes(encoded(changed))
                return cycle == 3
            with mock.patch.object(beta, "render_dashboard", wraps=beta.render_dashboard) as render, mock.patch.object(beta, "write_beta_dashboard", wraps=beta.write_beta_dashboard) as write:
                result, waits, output = self.run_loop(root, transition)
                self.assertEqual(result, 0)
                self.assertEqual(waits, [2, 2, 2])
                self.assertEqual(render.call_count, 2)
                self.assertEqual(write.call_count, 3)
                self.assertIn("Changed while watching", output.read_text())

    def test_warm_watch_exits_before_write_on_malformed_or_protected_input(self):
        for failure in ("malformed", "snapshot-mode", "labels-mode", "labels-symlink"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as folder:
                saved = []
                def transition(cycle, source, draft, labels, output):
                    saved.append(output.read_bytes())
                    if failure == "malformed":
                        source.write_text("not JSON")
                    elif failure == "snapshot-mode":
                        source.chmod(0o644)
                    elif failure == "labels-mode":
                        labels.chmod(0o644)
                    else:
                        target = labels.with_name("target.json")
                        labels.rename(target)
                        labels.symlink_to(target)
                    return False
                with mock.patch.object(beta, "write_beta_dashboard", wraps=beta.write_beta_dashboard) as write:
                    result, waits, output = self.run_loop(Path(folder), transition)
                    self.assertEqual(result, 1)
                    self.assertEqual(waits, [2])
                    self.assertEqual(write.call_count, 1)
                    self.assertEqual(output.read_bytes(), saved[0])

    def test_restart_is_cold_and_one_shot_does_not_use_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            with mock.patch.object(beta, "render_dashboard", wraps=beta.render_dashboard) as render:
                for _ in range(2):
                    result, waits, _ = self.run_loop(Path(folder), lambda *args: True)
                    self.assertEqual(result, 0)
                    self.assertEqual(waits, [2])
                self.assertEqual(render.call_count, 2)
            with mock.patch.object(beta._RenderCache, "render", side_effect=AssertionError("one-shot must bypass cache")):
                result, waits, _ = self.run_loop(Path(folder), lambda *args: True, watch=False)
                self.assertEqual(result, 0)
                self.assertEqual(waits, [])

    def test_failed_watch_recovers_only_in_a_new_cold_run(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            def invalidate(cycle, source, *unused):
                source.write_text("invalid")
                return False
            with mock.patch.object(beta, "render_dashboard", wraps=beta.render_dashboard) as render:
                failed, _, _ = self.run_loop(root, invalidate)
                self.assertEqual(failed, 1)
                self.assertEqual(render.call_count, 1)
                recovered, _, _ = self.run_loop(root, lambda *args: True)
                self.assertEqual(recovered, 0)
                self.assertEqual(render.call_count, 2)

    def test_nullable_parent_and_empty_title_remain_distinct_inputs(self):
        cache = beta._RenderCache()
        source = snapshot()
        original = beta.render_dashboard
        with mock.patch.object(beta, "render_dashboard", wraps=original) as render:
            cache.render(template(), source)
            source["sessions"][0]["parent_id"] = "none"
            cache.render(template(), source)
            source["sessions"][0]["title"] = ""
            result = cache.render(template(), source)
            self.assertEqual(render.call_count, 3)
            self.assertEqual(encoded(result), encoded(original(template(), source)))


if __name__ == "__main__":
    unittest.main()
