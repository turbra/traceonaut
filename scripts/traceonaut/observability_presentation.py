"""Protected human-readable context for observability presentation.

This registry is deliberately separate from the telemetry ledger and metric
contract.  Callers supply every display string explicitly; this module never
derives presentation text from prompts, logs, or model output.
"""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import stat
from typing import Any, Iterable, Mapping
import unicodedata
from uuid import UUID, uuid4


MAX_PRESENTATION_BYTES = 1024 * 1024
MAX_PRESENTATION_TEXT_CHARACTERS = 256
MAX_PRESENTATION_TEXT_BYTES = 1024
MAX_PROJECTS = 4096
MAX_DISPATCHES = 16384

_UUID_PATTERN = (
    "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    "[0-9a-f]{4}-[0-9a-f]{12}$"
)

PRESENTATION_REGISTRY_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://openai.com/cwo/observability-presentation-v1.schema.json",
    "title": "CWO observability presentation registry",
    "type": "object",
    "additionalProperties": False,
    "required": ["version", "projects", "dispatches"],
    "properties": {
        "version": {"const": 1},
        "projects": {
            "type": "object",
            "maxProperties": MAX_PROJECTS,
            "propertyNames": {"pattern": _UUID_PATTERN},
            "additionalProperties": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name"],
                "properties": {
                    "name": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_PRESENTATION_TEXT_CHARACTERS,
                    }
                },
            },
        },
        "dispatches": {
            "type": "object",
            "maxProperties": MAX_DISPATCHES,
            "propertyNames": {"pattern": _UUID_PATTERN},
            "additionalProperties": {
                "type": "object",
                "additionalProperties": False,
                "required": ["task_name", "agent_name"],
                "properties": {
                    field: {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_PRESENTATION_TEXT_CHARACTERS,
                    }
                    for field in ("task_name", "agent_name", "work_item_title")
                },
            },
        },
    },
}


class PresentationRegistryError(ValueError):
    """A fixed failure for protected presentation registry operations."""


def empty_presentation_registry() -> dict[str, Any]:
    """Return a new empty version 1 registry."""

    return {"version": 1, "projects": {}, "dispatches": {}}


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _canonical_uuid(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise PresentationRegistryError(f"{field} invalid")
    try:
        parsed = UUID(value)
    except ValueError:
        raise PresentationRegistryError(f"{field} invalid") from None
    if str(parsed) != value:
        raise PresentationRegistryError(f"{field} invalid")
    return value


def _presentation_text(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > MAX_PRESENTATION_TEXT_CHARACTERS
        or any(unicodedata.category(character).startswith("C") for character in value)
        or len(value.encode("utf-8")) > MAX_PRESENTATION_TEXT_BYTES
    ):
        raise PresentationRegistryError(f"{field} invalid")
    return value


def _validate_registry(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "version",
        "projects",
        "dispatches",
    }:
        raise PresentationRegistryError("registry fields invalid")
    if type(value["version"]) is not int or value["version"] != 1:
        raise PresentationRegistryError("registry version invalid")
    projects = value["projects"]
    dispatches = value["dispatches"]
    if not isinstance(projects, Mapping) or len(projects) > MAX_PROJECTS:
        raise PresentationRegistryError("projects invalid")
    if not isinstance(dispatches, Mapping) or len(dispatches) > MAX_DISPATCHES:
        raise PresentationRegistryError("dispatches invalid")

    normalized_projects: dict[str, dict[str, str]] = {}
    for project_id, context in projects.items():
        project_id = _canonical_uuid(project_id, "project_id")
        if not isinstance(context, Mapping) or set(context) != {"name"}:
            raise PresentationRegistryError("project context invalid")
        normalized_projects[project_id] = {
            "name": _presentation_text(context["name"], "project name")
        }

    normalized_dispatches: dict[str, dict[str, str]] = {}
    required = {"task_name", "agent_name"}
    for dispatch_id, context in dispatches.items():
        dispatch_id = _canonical_uuid(dispatch_id, "dispatch_id")
        if (
            not isinstance(context, Mapping)
            or not required.issubset(context)
            or set(context).difference(required | {"work_item_title"})
        ):
            raise PresentationRegistryError("dispatch context invalid")
        normalized = {
            field: _presentation_text(context[field], field)
            for field in ("task_name", "agent_name")
        }
        if "work_item_title" in context:
            normalized["work_item_title"] = _presentation_text(
                context["work_item_title"], "work_item_title"
            )
        normalized_dispatches[dispatch_id] = normalized
    return {
        "version": 1,
        "projects": normalized_projects,
        "dispatches": normalized_dispatches,
    }


def _private_ancestors(path: Path) -> None:
    for parent in reversed(path.parents):
        try:
            metadata = parent.lstat()
        except OSError:
            raise PresentationRegistryError("presentation path invalid") from None
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise PresentationRegistryError("presentation path invalid")
        if metadata.st_uid not in (0, os.geteuid()):
            raise PresentationRegistryError("presentation path invalid")
        writable = metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        if writable and not (
            metadata.st_uid == 0 and metadata.st_mode & stat.S_ISVTX
        ):
            raise PresentationRegistryError("presentation path invalid")


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _validate_target(path: Path, *, excluded_roots: Iterable[Path] = ()) -> None:
    if not isinstance(path, Path) or not path.is_absolute() or path.name in {"", ".", ".."}:
        raise PresentationRegistryError("presentation path invalid")
    _private_ancestors(path)
    try:
        parent_metadata = path.parent.lstat()
    except OSError:
        raise PresentationRegistryError("presentation path invalid") from None
    if (
        stat.S_ISLNK(parent_metadata.st_mode)
        or not stat.S_ISDIR(parent_metadata.st_mode)
        or parent_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(parent_metadata.st_mode) != 0o700
    ):
        raise PresentationRegistryError("presentation parent must be owner-only")
    for root in excluded_roots:
        if not isinstance(root, Path) or not root.is_absolute():
            raise PresentationRegistryError("excluded presentation root invalid")
        if _is_within(path, root):
            raise PresentationRegistryError("presentation path excluded")


def _read_registry_bytes(path: Path, *, missing_ok: bool) -> bytes | None:
    _validate_target(path)
    flags = os.O_RDONLY | os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise PresentationRegistryError("presentation registry missing") from None
    except OSError:
        raise PresentationRegistryError("presentation registry unreadable") from None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise PresentationRegistryError(
                "presentation registry must be owner-only"
            )
        content = os.read(descriptor, MAX_PRESENTATION_BYTES + 1)
        if len(content) > MAX_PRESENTATION_BYTES:
            raise PresentationRegistryError("presentation registry too large")
        return content
    finally:
        os.close(descriptor)


def _decode_registry(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw, object_pairs_hook=_object_without_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise PresentationRegistryError("presentation registry JSON invalid") from None
    return _validate_registry(value)


def load_presentation_registry(path: Path) -> dict[str, Any]:
    """Strictly load an existing protected registry."""

    raw = _read_registry_bytes(path, missing_ok=False)
    assert raw is not None
    return _decode_registry(raw)


def safe_load_presentation_registry(path: Path) -> dict[str, Any]:
    """Load presentation context or return an empty registry for UI fallback."""

    try:
        raw = _read_registry_bytes(path, missing_ok=True)
        return empty_presentation_registry() if raw is None else _decode_registry(raw)
    except (OSError, PresentationRegistryError):
        return empty_presentation_registry()


def _lock_descriptor(path: Path) -> int:
    lock_path = path.parent / f".{path.name}.lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open(lock_path, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise OSError("unsafe presentation lock")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return descriptor
    except OSError:
        try:
            if descriptor >= 0:
                os.close(descriptor)
        except OSError:
            pass
        raise PresentationRegistryError("presentation registry lock failed") from None


def _write_unlocked(path: Path, registry: Mapping[str, Any]) -> None:
    data = json.dumps(
        registry,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8") + b"\n"
    if len(data) > MAX_PRESENTATION_BYTES:
        raise PresentationRegistryError("presentation registry too large")
    temporary = path.parent / f".{path.name}.{uuid4()}.tmp"
    descriptor = -1
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short presentation write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except OSError:
            pass
        raise PresentationRegistryError("presentation registry write failed") from None


def write_presentation_registry(path: Path, value: Mapping[str, Any]) -> None:
    """Validate and atomically replace a protected registry."""

    registry = _validate_registry(value)
    _validate_target(path)
    lock = _lock_descriptor(path)
    try:
        _write_unlocked(path, registry)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)


def record_presentation_submission(
    path: Path,
    *,
    project_id: str,
    project_name: str,
    dispatch_id: str,
    task_name: str | None = None,
    agent_name: str | None = None,
    work_item_title: str | None = None,
    excluded_roots: Iterable[Path] = (),
) -> dict[str, Any]:
    """Atomically add explicit context for one authoritative submission.

    Supplying no task and agent names records only the project.  This keeps old
    manifests compatible without inventing a dispatch presentation record.
    """

    project_id = _canonical_uuid(project_id, "project_id")
    dispatch_id = _canonical_uuid(dispatch_id, "dispatch_id")
    project_name = _presentation_text(project_name, "project name")
    if (task_name is None) != (agent_name is None):
        raise PresentationRegistryError("task and agent names must be paired")
    if work_item_title is not None and task_name is None:
        raise PresentationRegistryError("work item title requires task context")
    dispatch_context: dict[str, str] | None = None
    if task_name is not None and agent_name is not None:
        dispatch_context = {
            "task_name": _presentation_text(task_name, "task_name"),
            "agent_name": _presentation_text(agent_name, "agent_name"),
        }
        if work_item_title is not None:
            dispatch_context["work_item_title"] = _presentation_text(
                work_item_title, "work_item_title"
            )

    _validate_target(path, excluded_roots=excluded_roots)
    lock = _lock_descriptor(path)
    try:
        raw = _read_registry_bytes(path, missing_ok=True)
        registry = empty_presentation_registry() if raw is None else _decode_registry(raw)
        registry["projects"][project_id] = {"name": project_name}
        if dispatch_context is not None:
            registry["dispatches"][dispatch_id] = dispatch_context
        registry = _validate_registry(registry)
        _write_unlocked(path, registry)
        return registry
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)
