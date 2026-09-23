"""Optional, read-only Codex account allowance polling outside the scrape path.

Only numeric allowance fields leave the native client. No account identities,
credit IDs, credentials, backend messages, or session contents are retained.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import select
import signal
import stat
import subprocess
import threading
import time

from .observability_host import _private_file_bytes


POLL_SECONDS = 60
FRESH_SECONDS = 150
MAX_BYTES = 1024 * 1024
SAFE_INTEGER = (1 << 53) - 1


def _integer(value, minimum=0):
    return type(value) is int and minimum <= value <= SAFE_INTEGER


def normalize_allowance(result):
    """Use the authoritative reset count and the shared Codex quota only."""
    if not isinstance(result, dict) or not isinstance(result.get("rateLimits"), dict):
        raise ValueError("account allowance response unavailable")
    values = {"reset_credits": None, "windows": {}}
    credits = result.get("rateLimitResetCredits")
    if isinstance(credits, dict) and _integer(credits.get("availableCount")):
        values["reset_credits"] = credits["availableCount"]
    buckets = result.get("rateLimitsByLimitId")
    bucket = buckets.get("codex") if isinstance(buckets, dict) else result["rateLimits"]
    if not isinstance(bucket, dict) or bucket.get("limitId") not in (None, "codex"):
        return values
    for name in ("primary", "secondary"):
        source = bucket.get(name)
        if not isinstance(source, dict):
            continue
        window = {}
        for field, minimum in (("usedPercent", 0), ("windowDurationMins", 1), ("resetsAt", 1)):
            if _integer(source.get(field), minimum):
                window[field] = source[field]
        if window:
            values["windows"][name] = window
    return values


def read_account_allowance(codex_bin, codex_home, stopping=None, timeout=20):
    """Own one bounded stdio client; send only initialization and a limits read."""
    stopping = stopping or threading.Event()
    allowed_environment = ("HOME", "PATH", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
                           "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR",
                           "DBUS_SESSION_BUS_ADDRESS", "SSL_CERT_FILE", "SSL_CERT_DIR",
                           "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY")
    environment = {key: os.environ[key] for key in allowed_environment if key in os.environ}
    environment["CODEX_HOME"] = str(codex_home)
    process = subprocess.Popen(
        [str(codex_bin), "app-server", "--stdio"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=environment, bufsize=0, start_new_session=True,
    )
    deadline = time.monotonic() + timeout
    buffer = b""
    received = 0

    def send(message):
        process.stdin.write((json.dumps(message) + "\n").encode())
        process.stdin.flush()

    def receive(identity):
        nonlocal buffer, received
        while not stopping.is_set() and time.monotonic() < deadline:
            if b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                message = json.loads(line)
                if isinstance(message, dict) and message.get("id") == identity:
                    if "error" in message or not isinstance(message.get("result"), dict):
                        raise ValueError("account allowance RPC unavailable")
                    return message["result"]
                continue
            ready, _, _ = select.select([process.stdout], [], [], min(0.2, max(0, deadline - time.monotonic())))
            if ready:
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                received += len(chunk)
                if received > MAX_BYTES:
                    raise ValueError("account allowance response too large")
                buffer += chunk
        raise ValueError("account allowance read interrupted or timed out")

    try:
        send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "cwo_account_metrics", "version": "1.0.0"}}})
        initialized = receive(1)
        if initialized.get("codexHome") != str(codex_home):
            raise ValueError("account allowance profile mismatch")
        send({"method": "initialized"})
        send({"id": 2, "method": "account/rateLimits/read"})
        return normalize_allowance(receive(2))
    finally:
        # The stdio client and any helpers have their own process group.
        # Never signal an existing Codex session or a shared daemon.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2)
        process.stdin.close()
        process.stdout.close()


class AccountAllowancePoller:
    """One account snapshot, never summed per project, session, or agent."""

    def __init__(self, codex_bin: Path, codex_home: Path):
        if not codex_bin.is_absolute() or not codex_bin.is_file() or not os.access(codex_bin, os.X_OK):
            raise ValueError("Codex executable must be an absolute executable file")
        if not codex_home.is_absolute() or not codex_home.is_dir():
            raise ValueError("Codex account source must be an absolute directory")
        self.codex_bin, self.codex_home = codex_bin, codex_home
        self.stopping = threading.Event()
        self.lock = threading.Lock()
        self.last_attempt = self.last_success = None
        self.available = False
        self.values = {"reset_credits": None, "windows": {}}
        self.thread = None

    def poll_once(self):
        attempted = time.time()
        try:
            values = read_account_allowance(self.codex_bin, self.codex_home, self.stopping)
        except (OSError, ValueError, subprocess.SubprocessError, RecursionError):
            values = None
        with self.lock:
            self.last_attempt = attempted
            self.available = values is not None
            self.values = values or {"reset_credits": None, "windows": {}}
            if self.available:
                self.last_success = time.time()

    def start(self):
        def run():
            while not self.stopping.is_set():
                self.poll_once()
                if self.stopping.wait(POLL_SECONDS):
                    break
        self.thread = threading.Thread(target=run, name="codex-account-allowance", daemon=True)
        self.thread.start()

    def close(self):
        self.stopping.set()
        if self.thread:
            self.thread.join(timeout=5)
        return self.thread is None or not self.thread.is_alive()

    def snapshot(self):
        with self.lock:
            return {"version": 1, "available": self.available, "last_attempt": self.last_attempt,
                    "last_success": self.last_success, "values": self.values}

    def render_metrics(self, now=None):
        return render_account_metrics(self.snapshot(), now)


def render_account_metrics(snapshot, now=None):
    now = time.time() if now is None else now
    available, attempted, observed, values = (snapshot[key] for key in ("available", "last_attempt", "last_success", "values"))
    lines = []

    def emit(suffix, value, window=None):
        if value is None or not math.isfinite(value):
            return
        name = "cwo_codex_account_" + suffix
        labels = f'{{window="{window}"}}' if window else ""
        lines.append(f"# TYPE {name} gauge\n{name}{labels} {value}")

    emit("available", int(available))
    emit("last_attempt_timestamp_seconds", attempted)
    emit("last_success_timestamp_seconds", observed)
    if available and observed is not None and 0 <= now - observed < FRESH_SECONDS:
        emit("reset_credits_available", values["reset_credits"])
        for name, window in values["windows"].items():
            for field, suffix in (("usedPercent", "window_used_percent"),
                                  ("windowDurationMins", "window_minutes"),
                                  ("resetsAt", "window_reset_timestamp_seconds")):
                emit(suffix, window.get(field), name)
    # One TYPE declaration per family, even when both windows are present.
    seen, output = set(), []
    for line in "\n".join(lines).splitlines():
        if not line.startswith("# TYPE") or line not in seen:
            output.append(line)
        seen.add(line)
    return ("\n".join(output) + "\n").encode()


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate account snapshot field")
        result[key] = value
    return result


def validate_snapshot(value):
    if not isinstance(value, dict) or set(value) != {"version", "available", "last_attempt", "last_success", "values"}:
        raise ValueError("invalid account snapshot fields")
    if type(value["version"]) is not int or value["version"] != 1 or type(value["available"]) is not bool:
        raise ValueError("invalid account snapshot version or availability")
    for field in ("last_attempt", "last_success"):
        number = value[field]
        if number is not None and (type(number) not in (int, float) or not math.isfinite(number) or not 0 <= number <= SAFE_INTEGER):
            raise ValueError("invalid account snapshot timestamp")
    if value["available"] and value["last_success"] is None:
        raise ValueError("missing account observation time")
    fields = value["values"]
    if not isinstance(fields, dict) or set(fields) != {"reset_credits", "windows"}:
        raise ValueError("invalid account snapshot values")
    if fields["reset_credits"] is not None and not _integer(fields["reset_credits"]):
        raise ValueError("invalid account reset count")
    windows = fields["windows"]
    if not isinstance(windows, dict) or set(windows) - {"primary", "secondary"}:
        raise ValueError("invalid account snapshot windows")
    for window in windows.values():
        if not isinstance(window, dict) or set(window) - {"usedPercent", "windowDurationMins", "resetsAt"}:
            raise ValueError("invalid account window fields")
        for field, number in window.items():
            if not _integer(number, 0 if field == "usedPercent" else 1):
                raise ValueError("invalid account window value")
    return value


class AccountSnapshotMetrics:
    """Read only a protected, numeric host snapshot; never open a native client."""

    def __init__(self, path):
        self.path = Path(path)
        if not self.path.is_absolute():
            raise ValueError("account snapshot path must be absolute")

    def render_metrics(self, now=None):
        try:
            parent = self.path.parent.lstat()
            if parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) != 0o700:
                raise ValueError("account snapshot directory must be private")
            value = json.loads(_private_file_bytes(self.path, maximum=16384), object_pairs_hook=_unique_keys)
            snapshot = validate_snapshot(value)
        except (OSError, ValueError, RecursionError):
            snapshot = {"version": 1, "available": False, "last_attempt": None, "last_success": None,
                        "values": {"reset_credits": None, "windows": {}}}
        return render_account_metrics(snapshot, now)
