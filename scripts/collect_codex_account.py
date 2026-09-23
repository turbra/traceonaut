#!/usr/bin/env python3
"""Poll the selected Codex profile's account allowance into a private snapshot."""

from __future__ import annotations

import argparse
import fcntl
import os
from pathlib import Path
import signal
import stat
import sys

from traceonaut.codex_account_telemetry import AccountAllowancePoller, POLL_SECONDS
from traceonaut.codex_session_telemetry import _safe_path, write_snapshot


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-bin", required=True, type=Path)
    parser.add_argument("--codex-home", required=True, type=Path)
    parser.add_argument("--snapshot-file", required=True, type=Path)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if any(not path.is_absolute() for path in (args.codex_bin, args.codex_home, args.snapshot_file)):
        parser.error("all paths must be absolute")
    if Path(os.path.abspath(args.snapshot_file)).is_relative_to(args.codex_home.resolve()):
        parser.error("account snapshot must be outside the Codex source home")
    os.umask(0o077)
    descriptor = None
    poller = None
    try:
        _safe_path(args.snapshot_file.parent, owner=True)
        if stat.S_IMODE(args.snapshot_file.parent.stat().st_mode) != 0o700:
            raise ValueError("account snapshot directory must be private")
        lock = args.snapshot_file.with_name("." + args.snapshot_file.name + ".lock")
        descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ValueError("account writer lock must be private")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        poller = AccountAllowancePoller(args.codex_bin, args.codex_home)
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: poller.stopping.set())
        while not poller.stopping.is_set():
            poller.poll_once()
            write_snapshot(args.snapshot_file, poller.snapshot())
            if args.once or poller.stopping.wait(POLL_SECONDS):
                break
        return 0
    except Exception:
        print("Account allowance collection unavailable; inspect profile access and private output.", file=sys.stderr)
        return 1
    finally:
        if poller:
            poller.close()
        if descriptor is not None:
            os.close(descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
