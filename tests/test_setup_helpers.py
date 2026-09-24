"""Credential creation and health checks preserve the documented privacy boundary."""

from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import check_metrics
import create_metrics_token
from traceonaut.observability_exporter import read_credential


class SetupHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.token = self.root / "metrics.token"

    def invoke(self, main, args, expected=0):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            if expected:
                with self.assertRaises(SystemExit) as failure:
                    main(args)
                self.assertEqual(failure.exception.code, expected)
            else:
                self.assertEqual(main(args), 0)
        return out.getvalue() + err.getvalue()

    def create(self):
        self.assertEqual(self.invoke(create_metrics_token.main, ["--credential-file", str(self.token)]), "")
        return read_credential(self.token).decode()

    def test_create_private_token_without_output_or_replacement(self):
        token = self.create()
        self.assertEqual(self.token.stat().st_mode & 0o777, 0o600)
        output = self.invoke(create_metrics_token.main, ["--credential-file", str(self.token)], 1)
        self.assertNotIn(token, output)
        self.assertEqual(read_credential(self.token).decode(), token)

    def test_creation_uses_exact_private_mode_with_restrictive_umask(self):
        previous = os.umask(0o777)
        try:
            self.create()
        finally:
            os.umask(previous)
        self.assertEqual(self.token.stat().st_mode & 0o777, 0o600)

    def test_creation_rejects_symlink_target_and_unsafe_parent(self):
        original = self.root / "original"
        original.write_text("preserve")
        self.token.symlink_to(original)
        self.invoke(create_metrics_token.main, ["--credential-file", str(self.token)], 1)
        self.assertEqual(original.read_text(), "preserve")
        self.token.unlink()
        self.root.chmod(0o770)
        try:
            self.invoke(create_metrics_token.main, ["--credential-file", str(self.token)], 1)
            self.assertFalse(self.token.exists())
        finally:
            self.root.chmod(0o700)

    def test_creation_rejects_symlink_parent(self):
        link = self.root / "link"
        directory = self.root / "directory"
        directory.mkdir(mode=0o700)
        link.symlink_to(directory)
        self.invoke(create_metrics_token.main, ["--credential-file", str(link / "token")], 1)
        self.assertEqual(list(directory.iterdir()), [])

    def endpoint(self):
        token = self.create()
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                calls.append(self.path)
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/metrics")
                    self.end_headers()
                    return
                self.send_response(401 if self.path == "/denied" else 200)
                self.end_headers()
                if self.path == "/metrics":
                    self.wfile.write(b"cwo_codex_collector_scan_timestamp_seconds{} 123\n")
                else:
                    self.wfile.write(token.encode())
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def stop():
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.addCleanup(stop)
        return f"http://127.0.0.1:{server.server_port}", token, calls

    def test_health_check_ignores_proxies_and_hides_payload(self):
        url, token, calls = self.endpoint()
        with patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:1", "HTTP_PROXY": "http://127.0.0.1:1", "no_proxy": "", "NO_PROXY": ""}):
            output = self.invoke(check_metrics.main, ["--url", url + "/metrics", "--credential-file", str(self.token)])
        self.assertEqual(calls, ["/metrics"])
        self.assertIn("HTTP 200", output)
        self.assertNotIn(token, output)
        self.assertNotIn("123", output)

    def test_redirect_errors_and_missing_health_never_echo_token(self):
        url, token, calls = self.endpoint()
        for path in ("/redirect", "/denied", "/missing"):
            with self.subTest(path=path):
                output = self.invoke(check_metrics.main, ["--url", url + path, "--credential-file", str(self.token)], 1)
                self.assertNotIn(token, output)
        self.assertEqual(calls, ["/redirect", "/denied", "/missing"])

    def test_rejects_bad_url_and_credential_before_request(self):
        self.create()
        for url in ("file:///etc/passwd", "http://user:password@example.org/metrics", "http://example.org:invalid", "http://example.org/#fragment"):
            with self.subTest(url=url), patch.object(check_metrics, "build_opener") as opener:
                output = self.invoke(check_metrics.main, ["--url", url, "--credential-file", str(self.token)], 1)
                opener.assert_not_called()
                self.assertNotIn(url, output)
        self.token.chmod(0o644)
        with patch.object(check_metrics, "build_opener") as opener:
            self.invoke(check_metrics.main, ["--credential-file", str(self.token)], 1)
            opener.assert_not_called()


if __name__ == "__main__":
    unittest.main()
