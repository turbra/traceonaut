from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from traceonaut.codex_account_telemetry import AccountAllowancePoller, AccountSnapshotMetrics, normalize_allowance, read_account_allowance
from traceonaut.codex_session_telemetry import write_snapshot


class AccountAllowanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.binary = self.home / "codex"
        self.binary.write_text("#!/bin/sh\nexit 1\n")
        self.binary.chmod(0o700)

    def response(self, count=2):
        return {"accountId": "private-account", "rateLimits": {
            "limitId": "codex", "primary": {"usedPercent": 4, "windowDurationMins": 10080, "resetsAt": 2000000000}},
            "rateLimitResetCredits": {"availableCount": count, "credits": [{"id": "private-credit"}]}}

    def test_authoritative_count_not_capped_detail_count(self):
        result = normalize_allowance(self.response(9))
        self.assertEqual(result["reset_credits"], 9)
        self.assertEqual(result["windows"]["primary"]["usedPercent"], 4)
        self.assertNotIn("private", json.dumps(result))

    def test_zero_null_and_malformed_count_are_distinct(self):
        self.assertEqual(normalize_allowance(self.response(0))["reset_credits"], 0)
        for count in (None, -1, True, "2", 2.5, 2**53):
            with self.subTest(count=count):
                self.assertIsNone(normalize_allowance(self.response(count))["reset_credits"])
        source = self.response()
        source["rateLimitResetCredits"] = None
        self.assertIsNone(normalize_allowance(source)["reset_credits"])

    def test_multi_bucket_is_authoritative_and_never_merges_quotas(self):
        source = self.response()
        source["rateLimitsByLimitId"] = {"another_model": source["rateLimits"]}
        self.assertEqual(normalize_allowance(source)["windows"], {})
        source["rateLimitsByLimitId"]["codex"] = {"secondary": {"usedPercent": 25, "windowDurationMins": 10080}}
        self.assertEqual(normalize_allowance(source)["windows"], {"secondary": {"usedPercent": 25, "windowDurationMins": 10080}})

    def test_fields_are_independent_and_invalid_values_stay_missing(self):
        source = self.response()
        source["rateLimits"]["primary"] = {"usedPercent": True, "windowDurationMins": 0, "resetsAt": "tomorrow"}
        self.assertEqual(normalize_allowance(source)["windows"], {})
        source["rateLimits"]["primary"] = {"usedPercent": 104, "windowDurationMins": 10080, "resetsAt": None}
        self.assertEqual(normalize_allowance(source)["windows"]["primary"], {"usedPercent": 104, "windowDurationMins": 10080})
        for result in (None, [], {}, {"rateLimits": None}):
            with self.assertRaises(ValueError):
                normalize_allowance(result)

    def poller(self):
        return AccountAllowancePoller(self.binary, self.home)

    def test_success_failed_read_stale_and_future_clock(self):
        poller = self.poller()
        with patch("traceonaut.codex_account_telemetry.read_account_allowance", return_value=normalize_allowance(self.response())), patch("traceonaut.codex_account_telemetry.time.time", return_value=1000):
            poller.poll_once()
        self.assertIn(b"cwo_codex_account_reset_credits_available 2", poller.render_metrics(1050))
        for now in (999, 1150, 2000):
            self.assertNotIn(b"reset_credits_available", poller.render_metrics(now))
        with patch("traceonaut.codex_account_telemetry.read_account_allowance", side_effect=ValueError("private-service-error")), patch("traceonaut.codex_account_telemetry.time.time", return_value=1060):
            poller.poll_once()
        payload = poller.render_metrics(1060)
        self.assertIn(b"cwo_codex_account_available 0", payload)
        self.assertIn(b"last_success_timestamp_seconds 1000", payload)
        self.assertNotIn(b"reset_credits_available", payload)
        self.assertNotIn(b"private", payload)

    def test_successful_null_replaces_previous_count(self):
        poller = self.poller()
        for count in (2, None):
            with patch("traceonaut.codex_account_telemetry.read_account_allowance", return_value=normalize_allowance(self.response(count))):
                poller.poll_once()
        self.assertIn(b"cwo_codex_account_available 1", poller.render_metrics())
        self.assertNotIn(b"reset_credits_available", poller.render_metrics())

    def test_protected_snapshot_replaces_null_and_rejects_bad_or_missing_data(self):
        path = self.home / "allowance.json"
        reader = AccountSnapshotMetrics(path)
        poller = self.poller()
        for count in (2, None):
            with patch("traceonaut.codex_account_telemetry.read_account_allowance", return_value=normalize_allowance(self.response(count))):
                poller.poll_once()
            write_snapshot(path, poller.snapshot())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(b"reset_credits_available" in reader.render_metrics(), count is not None)
        for body in ('{}', '{"version":1,"version":1}', 'x' * 16385):
            path.write_text(body)
            self.assertEqual(reader.render_metrics(), b"# TYPE cwo_codex_account_available gauge\ncwo_codex_account_available 0\n")
        path.unlink()
        self.assertNotIn(b"reset_credits_available", reader.render_metrics())

    def test_snapshot_reader_rejects_permissions_symlinks_and_unknown_fields(self):
        poller = self.poller()
        with patch("traceonaut.codex_account_telemetry.read_account_allowance", return_value=normalize_allowance(self.response())):
            poller.poll_once()
        path = self.home / "allowance.json"
        write_snapshot(path, poller.snapshot())
        reader = AccountSnapshotMetrics(path)
        path.chmod(0o644)
        self.assertNotIn(b"reset_credits_available", reader.render_metrics())
        path.chmod(0o600)
        unsafe = poller.snapshot()
        unsafe["accountId"] = "private-account"
        write_snapshot(path, unsafe)
        self.assertNotIn(b"private", reader.render_metrics())
        self.assertNotIn(b"reset_credits_available", reader.render_metrics())
        write_snapshot(path, poller.snapshot())
        alias = self.home / "alias.json"
        alias.symlink_to(path)
        self.assertNotIn(b"reset_credits_available", AccountSnapshotMetrics(alias).render_metrics())
        self.home.chmod(0o755)
        self.assertNotIn(b"reset_credits_available", reader.render_metrics())
        self.home.chmod(0o700)

    def test_dead_snapshot_suppresses_numbers_without_advancing_observation_time(self):
        poller = self.poller()
        with patch("traceonaut.codex_account_telemetry.read_account_allowance", return_value=normalize_allowance(self.response())), patch("traceonaut.codex_account_telemetry.time.time", return_value=1000):
            poller.poll_once()
        path = self.home / "allowance.json"
        write_snapshot(path, poller.snapshot())
        reader = AccountSnapshotMetrics(path)
        self.assertIn(b"reset_credits_available 2", reader.render_metrics(1100))
        self.assertNotIn(b"reset_credits_available", reader.render_metrics(1150))
        self.assertIn(b"last_success_timestamp_seconds 1000", reader.render_metrics(1150))

    def test_session_endpoint_default_is_unchanged_and_snapshot_is_additive(self):
        from collect_codex_sessions import SessionMetricsEndpoint
        endpoint = SessionMetricsEndpoint.__new__(SessionMetricsEndpoint)
        endpoint._lock = threading.Lock()
        with patch("collect_codex_sessions.render_session_metrics", return_value=b"session_metric 7\n"):
            endpoint.update_sessions({})
            self.assertEqual(endpoint._payload, b"session_metric 7\n")
            endpoint.update_sessions({}, account=AccountSnapshotMetrics(self.home / "missing.json"))
            self.assertTrue(endpoint._payload.startswith(b"session_metric 7\n"))
            self.assertIn(b"cwo_codex_account_available 0", endpoint._payload)

    def test_one_type_declaration_per_metric_and_no_sensitive_fields(self):
        source = self.response()
        source["rateLimits"]["secondary"] = {"usedPercent": 30, "windowDurationMins": 300, "resetsAt": 2000000001}
        poller = self.poller()
        with patch("traceonaut.codex_account_telemetry.read_account_allowance", return_value=normalize_allowance(source)):
            poller.poll_once()
        payload = poller.render_metrics().decode()
        self.assertEqual(payload.count("# TYPE cwo_codex_account_window_used_percent gauge"), 1)
        self.assertIn('window="secondary"', payload)
        for private in ("private-account", "private-credit", str(self.home), "project_id", "session_id"):
            self.assertNotIn(private, payload)

    def fake_native(self, mode="normal"):
        self.binary.write_text(f'''#!{sys.executable}
import json,os,signal,sys,time
from pathlib import Path
home=Path(os.environ['CODEX_HOME'])
for line in sys.stdin:
 message=json.loads(line)
 with (home/'methods.jsonl').open('a') as f:f.write(json.dumps(message)+'\\n')
 if message['method']=='initialize':
  print(json.dumps({{'id':1,'result':{{'codexHome':str(home) if {mode!r}!='wrong-home' else '/wrong'}}}}),flush=True)
 elif message['method']=='account/rateLimits/read':
  if {mode!r}=='hang':
   signal.signal(signal.SIGTERM,signal.SIG_IGN)
   (home/'pid').write_text(str(os.getpid()))
   time.sleep(60)
  elif {mode!r}=='oversize':print('x'*1100000,flush=True)
  elif {mode!r}=='rpc-error':print(json.dumps({{'id':2,'error':{{'message':'private-error'}}}}),flush=True)
  else:
   (home/'env-keys.json').write_text(json.dumps(sorted(os.environ)))
   print(json.dumps({{'id':2,'result':{self.response()!r}}}),flush=True)
''')
        self.binary.chmod(0o700)

    def test_native_read_uses_only_allowed_methods_and_environment(self):
        self.fake_native()
        with patch.dict(os.environ, {"OPENAI_API_KEY": "private-key", "METRICS_TOKEN": "private-token"}):
            result = read_account_allowance(self.binary, self.home)
        self.assertEqual(result["reset_credits"], 2)
        methods = [json.loads(line)["method"] for line in (self.home / "methods.jsonl").read_text().splitlines()]
        self.assertEqual(methods, ["initialize", "initialized", "account/rateLimits/read"])
        keys = json.loads((self.home / "env-keys.json").read_text())
        self.assertNotIn("OPENAI_API_KEY", keys)
        self.assertNotIn("METRICS_TOKEN", keys)

    def test_profile_mismatch_prevents_account_read(self):
        self.fake_native("wrong-home")
        with self.assertRaisesRegex(ValueError, "profile mismatch"):
            read_account_allowance(self.binary, self.home)
        self.assertEqual(len((self.home / "methods.jsonl").read_text().splitlines()), 1)

    def test_native_errors_are_bounded_and_sanitized(self):
        for mode in ("oversize", "rpc-error"):
            self.fake_native(mode)
            with self.assertRaises(ValueError) as caught:
                read_account_allowance(self.binary, self.home)
            self.assertNotIn("private-error", str(caught.exception))

    def test_timeout_kills_owned_client(self):
        self.fake_native("hang")
        started = time.monotonic()
        with self.assertRaises(ValueError):
            read_account_allowance(self.binary, self.home, timeout=0.2)
        self.assertLess(time.monotonic() - started, 4)
        with self.assertRaises(ProcessLookupError):
            os.kill(int((self.home / "pid").read_text()), 0)

    def test_background_read_does_not_block_scrape_and_can_stop(self):
        poller = self.poller()
        entered = threading.Event()
        def blocked(_binary, _home, stopping):
            entered.set()
            stopping.wait(10)
            raise ValueError("stopped")
        with patch("traceonaut.codex_account_telemetry.read_account_allowance", side_effect=blocked):
            poller.start()
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            self.assertIn(b"cwo_codex_account_available 0", poller.render_metrics())
            self.assertLess(time.monotonic() - started, 0.1)
            self.assertTrue(poller.close())


if __name__ == "__main__":
    unittest.main()
