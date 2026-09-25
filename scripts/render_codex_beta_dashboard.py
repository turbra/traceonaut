#!/usr/bin/env python3
"""Render a separate Codex beta dashboard using protected local display labels."""

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

from render_codex_sessions_dashboard import (
    DATASOURCE_PLACEHOLDER,
    IDENTITY_PATTERN,
    _agent_name,
    _mapping,
    _named_variable,
    _object_without_duplicates,
    _project_name,
    _read_snapshot_bytes,
    _session_name,
    _uuid7_started,
    _validated_output_path,
    load_snapshot,
    validate_snapshot,
    walk_panels,
    write_dashboard,
)


BETA_UID = "cwo-codex-beta"
# Use the canonical path to avoid a second dashboard load during self-navigation.
BETA_PATH = "/d/" + BETA_UID + "/work-overview"
MAX_DASHBOARD_BYTES = 32 * 1024 * 1024


def _beta_named_variable(variable: dict, names: dict[str, str]) -> None:
    """Encode custom options exactly as Grafana 11.5 parses them."""
    _named_variable(variable, names)
    variable["query"] = ", ".join(
        option["text"].replace(",", r"\,") + " : " + option["value"]
        for option in variable["options"][1:]
    )


def validate_labels(value: dict) -> dict:
    """Accept display aliases only, without metric or filesystem configuration."""
    if (
        not isinstance(value, dict)
        or value.get("version") != 1
        or set(value) - {"version", "projects", "sessions"}
    ):
        raise ValueError("unsupported beta labels")
    result = {"version": 1}
    for group in ("projects", "sessions"):
        labels = value.get(group, {})
        if not isinstance(labels, dict) or len(labels) > 100000:
            raise ValueError("invalid beta label group")
        result[group] = {}
        for identity, label in labels.items():
            if not isinstance(identity, str) or not IDENTITY_PATTERN.fullmatch(identity):
                raise ValueError("invalid beta label identity")
            if (
                not isinstance(label, str)
                or not label.strip()
                or len(label) > 256
                or any(ord(character) < 32 for character in label)
                or re.search(r"\$(?:\w|\{)|\[\[", label)
                or re.search(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", label, re.I)
            ):
                raise ValueError("invalid beta display label")
            result[group][identity] = " ".join(label.split())
    explicit = list(result["projects"].values())
    if len({name.casefold() for name in explicit}) != len(explicit):
        raise ValueError("project aliases must be distinct")
    return result


def load_labels(path: Path | None) -> dict:
    if path is None:
        return {"version": 1, "projects": {}, "sessions": {}}
    try:
        value = json.loads(
            _read_snapshot_bytes(path), object_pairs_hook=_object_without_duplicates
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("beta labels JSON invalid") from None
    return validate_labels(value)


def presentation_names(snapshot: dict, labels: dict) -> dict:
    projects, sessions, agents = {}, {}, {}
    first_seen = {}
    for row in snapshot["sessions"]:
        project = row["project_id"]
        name = labels["projects"].get(project, _project_name(row))
        if project in projects and projects[project] != name:
            raise ValueError("project display metadata conflicts")
        projects[project] = name
        started = _uuid7_started(row["session_id"])
        first_seen[project] = min(first_seen.get(project, started), started)

    # New colliding projects remain distinguishable before an explicit alias is
    # supplied. The first-seen timestamp is presentation context, never an ID.
    counts = {}
    for name in projects.values():
        counts[name.casefold()] = counts.get(name.casefold(), 0) + 1
    used = set()
    for project in sorted(projects):
        name = projects[project]
        if counts[name.casefold()] > 1:
            name += " · first seen " + first_seen[project]
        candidate, index = name, 1
        while candidate.casefold() in used:
            index += 1
            candidate = name + f" · unnamed workspace {index}"
        projects[project] = candidate
        used.add(candidate.casefold())

    for row in snapshot["sessions"]:
        identity = row["session_id"]
        sessions[identity] = labels["sessions"].get(
            identity, _session_name(row, projects[row["project_id"]])
        )
        agents[identity] = _agent_name(row)
    repeated = {}
    for name in sessions.values():
        repeated[name.casefold()] = repeated.get(name.casefold(), 0) + 1
    used = set()
    for identity in sorted(sessions):
        name = sessions[identity]
        if repeated[name.casefold()] > 1:
            name += " · " + _uuid7_started(identity)
        candidate, index = name, 1
        while candidate.casefold() in used:
            index += 1
            candidate = name + f" · instance {index}"
        sessions[identity] = candidate
        used.add(candidate.casefold())

    parents = {**sessions, "none": "", "": "Parent not recorded"}
    work_context = {}
    for row in snapshot["sessions"]:
        identity = row["session_id"]
        name = sessions[identity]
        if row["kind"] == "subagent" and row["agent_name"] and agents[identity].casefold() not in name.casefold():
            name += " · " + agents[identity]
        parent = row["parent_id"]
        if parent and parent != "none":
            name += "\n↳ " + sessions.get(parent, "Parent not indexed")
        work_context[identity] = name
    return {
        "Work": work_context,
        "Session": sessions,
        "Work context": work_context,
        "Project": projects,
        "Agent": agents,
        "Parent": parents,
    }


def render_dashboard(template: dict, snapshot: dict, datasource_uid=None, labels=None) -> dict:
    if template.get("uid") != BETA_UID:
        raise ValueError("beta renderer requires its separate dashboard UID")
    snapshot = validate_snapshot(snapshot)
    labels = validate_labels(labels or {"version": 1})
    names = presentation_names(snapshot, labels)
    dashboard = copy.deepcopy(template)
    variables = {item["name"]: item for item in dashboard.get("templating", {}).get("list", [])}
    if "project" not in variables or "session" not in variables:
        raise ValueError("beta project and session variables are required")
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
        raise ValueError("beta Work selection must be a visible custom variable")
    _beta_named_variable(variables["project"], names["Project"])
    _beta_named_variable(session_variable, names["Session"])
    for panel in walk_panels(dashboard.get("panels", [])):
        if panel.get("type") == "bargauge" and any(
            item.get("id") == "rowsToFields"
            for item in panel.get("transformations", [])
        ):
            # One field-name link works for every historical identity. Repeating
            # the same URL in each display-name override bloats large snapshots.
            panel.setdefault("fieldConfig", {}).setdefault("defaults", {})["links"] = [{
                "title": "Focus this work",
                "url": BETA_PATH + "?${project:queryparam}"
                + "&var-session=${__field.name:percentencode}&${__url_time_range}",
            }]
            panel.setdefault("fieldConfig", {}).setdefault("overrides", []).extend(
                {
                    "matcher": {"id": "byName", "options": identity},
                    "properties": [
                        {"id": "displayName", "value": name},
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
                    # The primary inventory has its own Parent column. Keeping
                    # the Work title single-line also keeps linked rows compact.
                    values = names["Session"] if panel.get("id") == 11 and field == "Work" else names[field]
                    prop["value"] = _mapping(values, field + " name unavailable")
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


def write_beta_dashboard(path: Path, dashboard: dict) -> bool:
    """Refuse to replace any dashboard other than this beta."""
    if dashboard.get("uid") != BETA_UID:
        raise ValueError("unexpected beta dashboard UID")
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
        if len(data) > MAX_DASHBOARD_BYTES or json.loads(data).get("uid") != BETA_UID:
            raise ValueError("refusing to replace another dashboard")
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        raise ValueError("existing output is not a beta dashboard") from None
    finally:
        os.close(descriptor)
    return write_dashboard(path, dashboard)


class _RenderCache:
    """One watch loop's last successful render, never an output-file cache."""

    def __init__(self):
        self._key = None
        self._rendered = None

    def render(self, template, snapshot, datasource_uid=None, labels=None):
        # Validate metadata on every cycle, including hits. Changed templates
        # and configuration still pass through the original renderer's checks.
        snapshot = validate_snapshot(snapshot)
        labels = validate_labels(labels or {"version": 1})
        # Encode the complete inputs, preserving ordering and JSON value types.
        # A field allowlist or dict equality could miss future fields or changes
        # such as true -> 1 that remain distinct in the rendered JSON.
        key = json.dumps(
            [template, snapshot, datasource_uid, labels],
            ensure_ascii=False, separators=(",", ":"),
        )
        if key != self._key:
            rendered = render_dashboard(template, snapshot, datasource_uid, labels)
            self._key, self._rendered = key, rendered
        return self._rendered


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--snapshot-file", type=Path, required=True)
    parser.add_argument("--labels-file", type=Path)
    parser.add_argument("--datasource-uid")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--watch-seconds", type=float)
    args = parser.parse_args(argv)
    if args.watch_seconds is not None and not 1 <= args.watch_seconds <= 60:
        parser.error("--watch-seconds must be between 1 and 60")
    inputs = [args.template, args.snapshot_file] + ([args.labels_file] if args.labels_file else [])
    if args.output.resolve() in {path.resolve() for path in inputs}:
        parser.error("beta output must be separate from input files")
    stop = threading.Event()
    if args.watch_seconds is not None:
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
    renderer = _RenderCache().render if args.watch_seconds is not None else render_dashboard
    try:
        while True:
            template = json.loads(args.template.read_text(encoding="utf-8"))
            snapshot = load_snapshot(args.snapshot_file)
            rendered = renderer(template, snapshot, args.datasource_uid, load_labels(args.labels_file))
            changed = write_beta_dashboard(args.output, rendered)
            if changed or args.watch_seconds is None:
                print(json.dumps({"status": "rendered", "dashboard": BETA_UID, "changed": changed}), flush=True)
            if args.watch_seconds is None or stop.wait(args.watch_seconds):
                break
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        print("Beta dashboard render unavailable: check template, protected metadata and beta output.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
