#!/usr/bin/env python3
"""Render readable work names into Grafana without changing metric labels."""

from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import re
import signal
import stat
import tempfile
import threading
from typing import Any, Iterator

from traceonaut.observability_presentation import safe_load_presentation_registry
from render_codex_sessions_dashboard import load_snapshot, render_dashboard as render_session_names


DATASOURCE_PLACEHOLDER = "${DS_PROMETHEUS}"
UUID_PATTERN = r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$"


def walk_panels(panels: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for panel in panels:
        yield panel
        yield from walk_panels(panel.get("panels", []))


def _mapping(names: dict[str, str], fallback: str) -> list[dict[str, Any]]:
    return [
        {"type": "value", "options": {key: {"text": name} for key, name in names.items()}},
        {"type": "regex", "options": {"pattern": UUID_PATTERN, "result": {"text": fallback}}},
    ]


def _custom_escape(value: str) -> str:
    # Grafana custom-variable separators; the selected values remain UUIDs.
    return value.replace("\\", "\\\\").replace(",", "\\,").replace(":", "\\:")


def _display_name(value: str) -> str:
    # Grafana expands displayName templates after rowsToFields. Never let a
    # supplied label expand __field.name into an otherwise hidden identity.
    if re.search(r"\$(?:\w|\{)|\[\[", value):
        return "Task name contains unsupported template syntax"
    return value


def _named_variable(variable: dict[str, Any], names: dict[str, str]) -> None:
    variable.update(
        type="custom", query=", ".join(
            _custom_escape(name) + " : " + identity
            for identity, name in names.items()
        ), hide=0 if names else 2,
        options=[{"text": "All", "value": "$__all", "selected": True}]
        + [{"text": name, "value": identity, "selected": False} for identity, name in names.items()],
        current={"text": "All", "value": "$__all", "selected": True},
    )
    for field in ("datasource", "definition", "refresh", "regex", "sort"):
        variable.pop(field, None)


def render_dashboard(
    template: dict[str, Any], registry: dict[str, Any], *, datasource_uid: str | None = None,
    session_snapshot: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Apply explicit labels only to presentation; all metric queries stay intact."""
    dashboard = copy.deepcopy(template)
    projects = {key: value["name"] for key, value in registry["projects"].items()}
    tasks = {key: value["task_name"] for key, value in registry["dispatches"].items()}
    workers = {key: value["agent_name"] for key, value in registry["dispatches"].items()}
    task_mapping = _mapping(tasks, "Task name not provided")
    worker_mapping = _mapping(workers, "Worker name not provided")
    for variable in dashboard.get("templating", {}).get("list", []):
        if variable["name"] == "project":
            _named_variable(variable, projects)
            variable["label"] = "Observed project"
        elif variable["name"] == "dispatch":
            _named_variable(variable, tasks)
            variable["label"] = "Observed task"
    for panel in walk_panels(dashboard["panels"]):
        # Technical diagnostics retain original identities for investigation.
        if panel.get("id", 0) < 100:
            continue
        if panel.get("type") == "bargauge" and any(
            item.get("id") == "rowsToFields"
            for item in panel.get("transformations", [])
        ):
            panel["fieldConfig"]["overrides"].extend(
                {"matcher": {"id": "byName", "options": identity},
                 "properties": [{"id": "displayName", "value": _display_name(name)}]}
                for identity, name in tasks.items()
            )
        for override in panel.get("fieldConfig", {}).get("overrides", []):
            name = override.get("matcher", {}).get("options")
            for prop in override.get("properties", []):
                if prop.get("id") == "mappings":
                    if name == "Task":
                        prop["value"] = copy.deepcopy(task_mapping)
                    elif name == "Worker":
                        prop["value"] = copy.deepcopy(worker_mapping)
    if session_snapshot is not None:
        # Reuse protected session-name validation/mapping without changing the
        # observed-dispatch variables or importing host names into metric labels.
        native = [p for p in dashboard["panels"] if p.get("id") == 305]
        named = render_session_names({"panels": native}, session_snapshot)["panels"]
        by_id = {p["id"]: p for p in named}
        dashboard["panels"] = [by_id.get(p["id"], p) for p in dashboard["panels"]]
    if datasource_uid is not None:
        if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", datasource_uid) is None:
            raise ValueError("invalid datasource UID")

        def bind(value: Any) -> Any:
            if isinstance(value, dict):
                return {key: bind(item) for key, item in value.items()}
            if isinstance(value, list):
                return [bind(item) for item in value]
            return datasource_uid if value == DATASOURCE_PLACEHOLDER else value

        dashboard = bind(dashboard)
        dashboard.pop("__inputs", None)
    return dashboard


def _validated_output_path(path: Path) -> Path:
    normalized = Path(os.path.abspath(path))
    for parent in normalized.parents:
        try:
            metadata = parent.lstat()
        except OSError:
            raise ValueError("dashboard output path invalid") from None
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("dashboard output path invalid")
    try:
        metadata = normalized.lstat()
    except FileNotFoundError:
        return normalized
    except OSError:
        raise ValueError("dashboard output path invalid") from None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("dashboard output path invalid")
    return normalized


def _matches_existing_output(path: Path, payload: bytes) -> bool:
    flags = os.O_RDONLY | os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return False
    except OSError:
        raise ValueError("dashboard output path invalid") from None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("dashboard output path invalid")
        content = bytearray()
        maximum = len(payload) + 1
        while len(content) < maximum:
            chunk = os.read(descriptor, maximum - len(content))
            if not chunk:
                break
            content.extend(chunk)
        return bytes(content) == payload
    except OSError:
        raise ValueError("dashboard output path invalid") from None
    finally:
        os.close(descriptor)


def write_dashboard(path: Path, dashboard: dict[str, Any]) -> bool:
    """Replace only changed content so file provisioning does not churn."""
    payload = (json.dumps(dashboard, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path = _validated_output_path(path)
    if _matches_existing_output(path, payload):
        return False
    descriptor, temporary = tempfile.mkstemp(prefix=".cwo-dashboard-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
            os.fchmod(target.fileno(), 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--presentation-file", type=Path)
    parser.add_argument("--session-snapshot-file", type=Path, help="optional existing session snapshot for CWO-associated session names")
    parser.add_argument("--datasource-uid")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--watch-seconds", type=float, help="refresh local name mappings every 1 to 60 seconds")
    args = parser.parse_args(argv)
    if args.presentation_file is None and args.session_snapshot_file is None:
        parser.error("provide --presentation-file or --session-snapshot-file")
    if args.watch_seconds is not None and not 1 <= args.watch_seconds <= 60:
        parser.error("--watch-seconds must be between 1 and 60")
    stop = threading.Event()
    if args.watch_seconds is not None:
        signal.signal(signal.SIGTERM, lambda _signum, _frame: stop.set())
        signal.signal(signal.SIGINT, lambda _signum, _frame: stop.set())
    try:
        while True:
            template = json.loads(args.template.read_text(encoding="utf-8"))
            registry = (safe_load_presentation_registry(args.presentation_file) if args.presentation_file
                        else {"version": 1, "projects": {}, "dispatches": {}})
            session_snapshot = load_snapshot(args.session_snapshot_file) if args.session_snapshot_file else None
            dashboard = render_dashboard(template, registry, datasource_uid=args.datasource_uid, session_snapshot=session_snapshot)
            changed = write_dashboard(args.output, dashboard)
            if changed or args.watch_seconds is None:
                print(json.dumps({"status": "rendered", "changed": changed,
                                  "named_projects": len(registry["projects"]),
                                  "named_tasks": len(registry["dispatches"])}), flush=True)
            if args.watch_seconds is None or stop.wait(args.watch_seconds):
                break
    except (OSError, ValueError, KeyError, TypeError):
        print("Dashboard render unavailable: check template, private labels, and output path.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
