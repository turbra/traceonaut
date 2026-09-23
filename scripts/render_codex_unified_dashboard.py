#!/usr/bin/env python3
"""Render Codex Unified overview from canonical collected session metadata.

Native collected titles are authoritative. Beta-only display aliases are never
loaded. Names decorate stable identities without entering metric labels.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import re
import signal
import stat
import threading
from urllib.parse import quote

from render_codex_beta_dashboard import (
    _beta_named_variable as _named_custom_variable,
    presentation_names,
)
from render_codex_sessions_dashboard import (
    DATASOURCE_PLACEHOLDER, _mapping, _validated_output_path,
    load_snapshot, validate_snapshot, walk_panels, write_dashboard,
)

UNIFIED_UID = "cwo-codex-unified"
UNIFIED_PATH = "/d/" + UNIFIED_UID + "/codex-c2b7-unified-overview"
MAX_DASHBOARD_BYTES = 32 * 1024 * 1024


def unified_names(snapshot: dict) -> dict:
    """Use one canonical title; range-end roles are supplied by metric labels."""
    names = presentation_names(snapshot, {"version": 1, "projects": {}, "sessions": {}})
    names["Work"] = names["Session"]
    return names


def render_dashboard(template: dict, snapshot: dict, datasource_uid=None) -> dict:
    if template.get("uid") != UNIFIED_UID:
        raise ValueError("unified renderer requires its separate dashboard UID")
    snapshot = validate_snapshot(snapshot)
    names = unified_names(snapshot)
    dashboard = copy.deepcopy(template)
    variables = {item["name"]: item for item in dashboard.get("templating", {}).get("list", [])}
    if "project" not in variables or "session" not in variables:
        raise ValueError("unified project and session variables are required")
    session_variable = variables["session"]
    if (
        session_variable.get("type") != "custom"
        or session_variable.get("label") != "Work"
        or session_variable.get("hide") != 0
        or session_variable.get("multi") is not True
        or session_variable.get("includeAll") is not True
        or session_variable.get("allValue") != ".+"
        or "datasource" in session_variable
        or "definition" in session_variable
    ):
        raise ValueError("unified Work selection must be a visible custom variable")
    _named_custom_variable(variables["project"], names["Project"])
    _named_custom_variable(session_variable, names["Session"])
    for panel in walk_panels(dashboard.get("panels", [])):
        if panel.get("type") == "bargauge" and any(
            item.get("id") == "rowsToFields"
            for item in panel.get("transformations", [])
        ):
            panel.setdefault("fieldConfig", {}).setdefault("overrides", []).extend(
                {
                    "matcher": {"id": "byName", "options": identity},
                    "properties": [
                        {"id": "displayName", "value": name},
                        {
                            "id": "links",
                            "value": [{
                                "title": "Focus this work",
                                "url": UNIFIED_PATH
                                + "?${project:queryparam}&var-session="
                                + quote(identity, safe="")
                                + "&${__url_time_range}",
                            }],
                        },
                    ],
                }
                for identity, name in names["Session"].items()
            )
        for override in panel.get("fieldConfig", {}).get("overrides", []):
            field = override.get("matcher", {}).get("options")
            if field not in names:
                continue
            for prop in override.get("properties", []):
                if prop.get("id") == "mappings":
                    prop["value"] = _mapping(names[field], field + " name unavailable")
    if datasource_uid is not None:
        if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", datasource_uid) is None:
            raise ValueError("invalid datasource UID")
        def bind(value):
            if isinstance(value, dict):
                return {key: bind(item) for key, item in value.items()}
            if isinstance(value, list):
                return [bind(item) for item in value]
            return datasource_uid if value == DATASOURCE_PLACEHOLDER else value
        dashboard = bind(dashboard)
        dashboard.pop("__inputs", None)
    return dashboard


def write_unified_dashboard(path: Path, dashboard: dict) -> bool:
    """Refuse to replace any dashboard other than this unified."""
    if dashboard.get("uid") != UNIFIED_UID:
        raise ValueError("unexpected unified dashboard UID")
    payload = (json.dumps(dashboard, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if len(payload) > MAX_DASHBOARD_BYTES:
        raise ValueError("rendered unified dashboard exceeds output size limit")
    path = _validated_output_path(path)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return write_dashboard(path, dashboard)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_DASHBOARD_BYTES:
            raise ValueError("existing dashboard output is invalid")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            data = source.read(MAX_DASHBOARD_BYTES + 1)
        if len(data) > MAX_DASHBOARD_BYTES or json.loads(data).get("uid") != UNIFIED_UID:
            raise ValueError("refusing to replace another dashboard")
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        raise ValueError("existing output is not a unified dashboard") from None
    finally:
        os.close(descriptor)
    return write_dashboard(path, dashboard)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--snapshot-file", type=Path, required=True)
    parser.add_argument("--datasource-uid")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--watch-seconds", type=float)
    args = parser.parse_args(argv)
    if args.watch_seconds is not None and not 1 <= args.watch_seconds <= 60:
        parser.error("--watch-seconds must be between 1 and 60")
    inputs = [args.template, args.snapshot_file]
    if args.output.resolve() in {path.resolve() for path in inputs}:
        parser.error("unified output must be separate from input files")
    stop = threading.Event()
    if args.watch_seconds is not None:
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        while True:
            template = json.loads(args.template.read_text(encoding="utf-8"))
            snapshot = load_snapshot(args.snapshot_file)
            rendered = render_dashboard(template, snapshot, args.datasource_uid)
            changed = write_unified_dashboard(args.output, rendered)
            if changed or args.watch_seconds is None:
                print(json.dumps({"status": "rendered", "dashboard": UNIFIED_UID, "changed": changed}), flush=True)
            if args.watch_seconds is None or stop.wait(args.watch_seconds):
                break
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        print("Unified dashboard render unavailable: check template, protected metadata and unified output.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
