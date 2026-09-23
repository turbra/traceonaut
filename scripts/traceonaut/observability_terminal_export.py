"""Private, replay-safe export of terminal registered dispatch observations."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Any
from uuid import UUID

from .observability_contract import (
    CoverageGap,
    CoverageState,
    FieldState,
    HealthReason,
    LifecycleState,
    PublicationState,
    QueueState,
    SchemaState,
    TERMINAL_LIFECYCLE_STATES,
    TokenKind,
    ToolCategory,
    ToolLifecycle,
    TurnOutcome,
    calculate_declared_allowance,
)
from .observability_ledger import (
    DATABASE_NAME,
    ObservabilityLedger,
    ObservabilityLedgerError,
)


PROJECTION_VERSION = 1
PROJECTION_RECORD_TYPE = "terminal_dispatch_projection.v1"
CURSOR_RECORD_TYPE = "terminal_dispatch_export_cursor.v1"
OBSERVATION_DIRECTORY_NAME = "observations"
CURSOR_NAME = "cursor.json"
LOCK_NAME = "export.lock"
MAX_SINK_RECORD_BYTES = 1024 * 1024
_PURPOSES = (
    "unknown",
    "model-response",
    "manual-compaction",
    "automatic-compaction",
    "auxiliary",
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/-]{0,127}$")


class TerminalObservationExportError(ValueError):
    """Stable failure raised for an unsafe or inconsistent terminal export."""


def _canonical(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TerminalObservationExportError(
            "terminal-export-value-not-json"
        ) from exc


def _content_revision(domain: str, value: Any) -> str:
    return hashlib.sha256(domain.encode("ascii") + b"\0" + _canonical(value)).hexdigest()


def _revision_content(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return semantic content, excluding observation-time filesystem fields."""

    projected = json.loads(_canonical(value))
    projected["health"].pop("observation_time")
    return projected


def _validate_parent_path(path: Path) -> None:
    if not path.is_absolute():
        raise TerminalObservationExportError("terminal-export-path-not-absolute")
    cursor = Path(path.anchor)
    for part in path.parts[1:-1]:
        cursor /= part
        try:
            metadata = cursor.lstat()
        except FileNotFoundError:
            raise TerminalObservationExportError(
                "terminal-export-parent-missing"
            ) from None
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise TerminalObservationExportError("terminal-export-parent-unsafe")
        writable = metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        sticky = metadata.st_mode & stat.S_ISVTX
        if metadata.st_uid not in {0, os.geteuid()}:
            raise TerminalObservationExportError("terminal-export-parent-unsafe")
        if writable and (not sticky or metadata.st_uid != 0):
            raise TerminalObservationExportError("terminal-export-parent-unsafe")


def _private_directory(path: Path, *, create: bool) -> None:
    _validate_parent_path(path)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if not create:
            raise TerminalObservationExportError(
                "terminal-export-directory-missing"
            ) from None
        try:
            path.mkdir(mode=0o700)
        except OSError as exc:
            raise TerminalObservationExportError(
                "terminal-export-directory-create-failed"
            ) from exc
        metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise TerminalObservationExportError("terminal-export-directory-unsafe")


def _private_file(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise TerminalObservationExportError("terminal-export-file-unsafe")


def _read_json(path: Path) -> dict[str, Any] | None:
    _private_file(path)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise TerminalObservationExportError("terminal-export-read-failed") from exc
    try:
        metadata = os.fstat(descriptor)
        if metadata.st_size > MAX_SINK_RECORD_BYTES:
            raise TerminalObservationExportError("terminal-export-record-too-large")
        payload = b""
        while len(payload) <= MAX_SINK_RECORD_BYTES:
            block = os.read(descriptor, min(65536, MAX_SINK_RECORD_BYTES + 1 - len(payload)))
            if not block:
                break
            payload += block
    finally:
        os.close(descriptor)
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TerminalObservationExportError("terminal-export-json-invalid") from exc
    if not isinstance(value, dict):
        raise TerminalObservationExportError("terminal-export-json-invalid")
    return value


def _source_identity(state_dir: Path) -> str:
    metadata = (state_dir / DATABASE_NAME).stat(follow_symlinks=False)
    material = f"{metadata.st_dev}:{metadata.st_ino}".encode("ascii")
    return hashlib.sha256(b"cwo-terminal-ledger-source-v1\0" + material).hexdigest()


def _allowance(value: Any) -> dict[str, Any]:
    return {
        "remaining": value.remaining,
        "overrun": value.overrun,
        "lower_bound_overrun": value.lower_bound_overrun,
    }


def _exact_mapping(value: Any, keys: set[str], field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise TerminalObservationExportError(
            f"terminal-export-projection-{field}-invalid"
        )
    return value


def _nonnegative_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TerminalObservationExportError(
            f"terminal-export-projection-{field}-invalid"
        )
    return value


def _number_or_none(value: Any, field: str) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise TerminalObservationExportError(
            f"terminal-export-projection-{field}-invalid"
        )
    return value


def _bounded_label_or_none(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _LABEL_RE.fullmatch(value) is None:
        raise TerminalObservationExportError(
            f"terminal-export-projection-{field}-invalid"
        )
    return value


def _validate_health_snapshot(value: Any) -> None:
    snapshot = _exact_mapping(
        value,
        {
            "record_type",
            "project_id",
            "connection_state",
            "schema_state",
            "ledger_state",
            "queue_state",
            "publication_state",
            "reason_counts",
            "unassigned_count",
            "conflict_count",
            "rejected_count",
            "queue_depth",
            "last_event_timestamp_seconds",
            "known_coverage_gaps",
        },
        "health-snapshot",
    )
    if snapshot["record_type"] != "telemetry_health.v1":
        raise TerminalObservationExportError(
            "terminal-export-projection-health-snapshot-invalid"
        )
    try:
        project_uuid = UUID(snapshot["project_id"])
        SchemaState(snapshot["schema_state"])
        QueueState(snapshot["queue_state"])
        PublicationState(snapshot["publication_state"])
    except (TypeError, ValueError) as exc:
        raise TerminalObservationExportError(
            "terminal-export-projection-health-snapshot-invalid"
        ) from exc
    if str(project_uuid) != snapshot["project_id"]:
        raise TerminalObservationExportError(
            "terminal-export-projection-health-snapshot-invalid"
        )
    if snapshot["ledger_state"] not in {"healthy", "fault", "recovery"}:
        raise TerminalObservationExportError(
            "terminal-export-projection-health-snapshot-invalid"
        )
    reasons = _exact_mapping(
        snapshot["reason_counts"],
        {reason.value for reason in HealthReason},
        "health-reasons",
    )
    for key, count in reasons.items():
        _nonnegative_integer(count, f"health-reason-{key}")
    for key in ("unassigned_count", "conflict_count", "rejected_count", "queue_depth"):
        _nonnegative_integer(snapshot[key], f"health-{key}")
    _number_or_none(
        snapshot["last_event_timestamp_seconds"], "health-last-event"
    )
    gaps = snapshot["known_coverage_gaps"]
    if not isinstance(gaps, list) or len(gaps) != len(set(gaps)):
        raise TerminalObservationExportError(
            "terminal-export-projection-health-gaps-invalid"
        )
    try:
        for gap in gaps:
            CoverageGap(gap)
    except (TypeError, ValueError) as exc:
        raise TerminalObservationExportError(
            "terminal-export-projection-health-gaps-invalid"
        ) from exc


def _health_snapshot_clean(snapshot: Mapping[str, Any]) -> bool:
    relevant_gaps = {
        gap
        for gap in snapshot["known_coverage_gaps"]
        if gap != CoverageGap.PUBLICATION_UNCONFIRMED.value
    }
    return bool(
        snapshot["schema_state"] == SchemaState.COMPATIBLE.value
        and snapshot["ledger_state"] == "healthy"
        and snapshot["queue_state"] == QueueState.HEALTHY.value
        and snapshot["queue_depth"] == 0
        and snapshot["unassigned_count"] == 0
        and snapshot["conflict_count"] == 0
        and snapshot["rejected_count"] == 0
        and not relevant_gaps
        and not any(snapshot["reason_counts"].values())
    )


def _validate_captured_health(value: Any, field: str) -> None:
    captured = _exact_mapping(
        value,
        {
            "source_revision",
            "accounting_revision",
            "content_revision",
            "clean",
            "snapshot",
        },
        field,
    )
    _nonnegative_integer(captured["source_revision"], field + "-source-revision")
    _nonnegative_integer(
        captured["accounting_revision"], field + "-accounting-revision"
    )
    if (
        not isinstance(captured["content_revision"], str)
        or _SHA256_RE.fullmatch(captured["content_revision"]) is None
        or not isinstance(captured["clean"], bool)
    ):
        raise TerminalObservationExportError(
            f"terminal-export-projection-{field}-invalid"
        )
    _validate_health_snapshot(captured["snapshot"])
    expected = _content_revision(
        "cwo-terminal-health-content-v1", captured["snapshot"]
    )
    if captured["content_revision"] != expected:
        raise TerminalObservationExportError(
            f"terminal-export-projection-{field}-revision-invalid"
        )


def _validate_projection(value: Any) -> dict[str, Any]:
    """Fail closed on the exact recursive v1 projection allowlist."""

    record = _exact_mapping(
        value,
        {
            "record_type",
            "projection_version",
            "identity",
            "revisions",
            "configuration",
            "lifecycle",
            "allowances",
            "accounting",
            "health",
            "publication",
            "clean_summary_eligible",
        },
        "record",
    )
    if (
        record["record_type"] != PROJECTION_RECORD_TYPE
        or record["projection_version"] != PROJECTION_VERSION
        or not isinstance(record["clean_summary_eligible"], bool)
    ):
        raise TerminalObservationExportError("terminal-export-projection-record-invalid")
    identity = _exact_mapping(
        record["identity"], {"project_id", "dispatch_id"}, "identity"
    )
    try:
        project_uuid = UUID(identity["project_id"])
        dispatch_uuid = UUID(identity["dispatch_id"])
    except (TypeError, ValueError) as exc:
        raise TerminalObservationExportError(
            "terminal-export-projection-identity-invalid"
        ) from exc
    if (
        str(project_uuid) != identity["project_id"]
        or str(dispatch_uuid) != identity["dispatch_id"]
    ):
        raise TerminalObservationExportError(
            "terminal-export-projection-identity-invalid"
        )
    revisions = _exact_mapping(
        record["revisions"],
        {
            "accounting_revision",
            "source_revision",
            "attributable_health_content_revision",
            "current_project_health_content_revision",
        },
        "revisions",
    )
    _nonnegative_integer(revisions["accounting_revision"], "accounting-revision")
    _nonnegative_integer(revisions["source_revision"], "source-revision")
    for key in (
        "attributable_health_content_revision",
        "current_project_health_content_revision",
    ):
        item = revisions[key]
        if item is not None and (
            not isinstance(item, str) or _SHA256_RE.fullmatch(item) is None
        ):
            raise TerminalObservationExportError(
                "terminal-export-projection-health-revision-invalid"
            )

    configuration = _exact_mapping(
        record["configuration"], {"requested", "acknowledged", "actual"}, "configuration"
    )
    requested = _exact_mapping(
        configuration["requested"], {"executor", "model", "effort"}, "requested"
    )
    for key in requested:
        _bounded_label_or_none(requested[key], "requested-" + key)
    acknowledged = configuration["acknowledged"]
    if not isinstance(acknowledged, list) or not acknowledged:
        raise TerminalObservationExportError(
            "terminal-export-projection-acknowledged-invalid"
        )
    ack_keys = {
        "acknowledgement_provenance",
        "configured_model",
        "configured_model_provenance",
        "configured_effort",
        "configured_effort_provenance",
    }
    for index, item in enumerate(acknowledged):
        ack = _exact_mapping(item, ack_keys, f"acknowledged-{index}")
        for key in ack:
            _bounded_label_or_none(ack[key], f"acknowledged-{index}-{key}")
    actual = _exact_mapping(
        configuration["actual"], {"model", "effort"}, "actual"
    )
    for key in actual:
        field = _exact_mapping(actual[key], {"value", "state"}, "actual-" + key)
        if field["value"] is not None or field["state"] != int(FieldState.UNAVAILABLE):
            raise TerminalObservationExportError(
                "terminal-export-projection-actual-invalid"
            )

    lifecycle = _exact_mapping(
        record["lifecycle"],
        {"state", "elapsed_seconds", "timing_provenance"},
        "lifecycle",
    )
    try:
        state = LifecycleState(lifecycle["state"])
    except (TypeError, ValueError) as exc:
        raise TerminalObservationExportError(
            "terminal-export-projection-lifecycle-invalid"
        ) from exc
    if state not in TERMINAL_LIFECYCLE_STATES:
        raise TerminalObservationExportError(
            "terminal-export-projection-lifecycle-invalid"
        )
    elapsed = _exact_mapping(
        lifecycle["elapsed_seconds"], {"value", "state"}, "elapsed"
    )
    _number_or_none(elapsed["value"], "elapsed-value")
    try:
        FieldState(elapsed["state"])
    except (TypeError, ValueError) as exc:
        raise TerminalObservationExportError(
            "terminal-export-projection-elapsed-state-invalid"
        ) from exc
    _bounded_label_or_none(lifecycle["timing_provenance"], "timing-provenance")

    allowances = _exact_mapping(
        record["allowances"],
        {
            "declared_cycle_allowance",
            "declared_elapsed_allowance_seconds",
            "enforced_tool_call_limit",
            "enforced_runtime_limit_seconds",
            "cycle",
            "elapsed",
        },
        "allowances",
    )
    for key in (
        "declared_cycle_allowance",
        "declared_elapsed_allowance_seconds",
        "enforced_tool_call_limit",
        "enforced_runtime_limit_seconds",
    ):
        _number_or_none(allowances[key], "allowance-" + key)
    for key in ("cycle", "elapsed"):
        calculation = _exact_mapping(
            allowances[key],
            {"remaining", "overrun", "lower_bound_overrun"},
            "allowance-" + key,
        )
        _number_or_none(calculation["remaining"], "allowance-remaining")
        _number_or_none(calculation["overrun"], "allowance-overrun")
        if not isinstance(calculation["lower_bound_overrun"], bool):
            raise TerminalObservationExportError(
                "terminal-export-projection-allowance-bound-invalid"
            )

    accounting = _exact_mapping(
        record["accounting"],
        {
            "completed_cycles",
            "coverage_state",
            "conflict_count",
            "tokens",
            "completion_purpose_counts",
            "activity",
        },
        "accounting",
    )
    completed = _nonnegative_integer(accounting["completed_cycles"], "completed-cycles")
    try:
        CoverageState(accounting["coverage_state"])
    except (TypeError, ValueError) as exc:
        raise TerminalObservationExportError(
            "terminal-export-projection-coverage-invalid"
        ) from exc
    _nonnegative_integer(accounting["conflict_count"], "conflict-count")
    tokens = _exact_mapping(
        accounting["tokens"], {kind.value for kind in TokenKind}, "tokens"
    )
    for key, value_entry in tokens.items():
        entry = _exact_mapping(
            value_entry,
            {"value", "contributor_count", "completed_cycles", "state"},
            "token-" + key,
        )
        contributors = _nonnegative_integer(
            entry["contributor_count"], "token-contributors"
        )
        if entry["completed_cycles"] != completed or contributors > completed:
            raise TerminalObservationExportError(
                "terminal-export-projection-token-count-invalid"
            )
        _number_or_none(entry["value"], "token-value")
        if (contributors == 0) != (entry["value"] is None):
            raise TerminalObservationExportError(
                "terminal-export-projection-token-value-invalid"
            )
        try:
            FieldState(entry["state"])
        except (TypeError, ValueError) as exc:
            raise TerminalObservationExportError(
                "terminal-export-projection-token-state-invalid"
            ) from exc
    purpose_counts = _exact_mapping(
        accounting["completion_purpose_counts"], set(_PURPOSES), "purpose-counts"
    )
    for key, count in purpose_counts.items():
        _nonnegative_integer(count, "purpose-" + key)
    if sum(purpose_counts.values()) != completed:
        raise TerminalObservationExportError(
            "terminal-export-projection-purpose-count-invalid"
        )
    activity = _exact_mapping(
        accounting["activity"],
        {"retry_notices", "turn_outcomes", "tool_events"},
        "activity",
    )
    if activity["retry_notices"] is None:
        if activity["turn_outcomes"] is not None or activity["tool_events"] is not None:
            raise TerminalObservationExportError(
                "terminal-export-projection-activity-invalid"
            )
    else:
        _nonnegative_integer(activity["retry_notices"], "activity-retries")
        outcomes = _exact_mapping(
            activity["turn_outcomes"],
            {item.value for item in TurnOutcome},
            "activity-outcomes",
        )
        for key, count in outcomes.items():
            _nonnegative_integer(count, "activity-outcome-" + key)
        if not isinstance(activity["tool_events"], list):
            raise TerminalObservationExportError(
                "terminal-export-projection-activity-tools-invalid"
            )
        for index, item in enumerate(activity["tool_events"]):
            tool = _exact_mapping(
                item, {"category", "lifecycle", "count"}, f"activity-tool-{index}"
            )
            try:
                ToolCategory(tool["category"])
                ToolLifecycle(tool["lifecycle"])
            except (TypeError, ValueError) as exc:
                raise TerminalObservationExportError(
                    "terminal-export-projection-activity-tool-invalid"
                ) from exc
            _nonnegative_integer(tool["count"], "activity-tool-count")

    health = _exact_mapping(
        record["health"],
        {"current_attribution", "attributable", "latest_unattributed", "observation_time"},
        "health",
    )
    attribution = _exact_mapping(
        health["current_attribution"],
        {"state", "project_dispatch_count", "project_connection_count"},
        "health-attribution",
    )
    if attribution["state"] not in {
        "isolated-project-connection",
        "unavailable-shared-or-unproven-scope",
    }:
        raise TerminalObservationExportError(
            "terminal-export-projection-health-attribution-invalid"
        )
    _nonnegative_integer(attribution["project_dispatch_count"], "project-count")
    _nonnegative_integer(attribution["project_connection_count"], "connection-count")
    if health["attributable"] is not None:
        _validate_captured_health(health["attributable"], "attributable-health")
    if health["latest_unattributed"] is not None:
        _validate_captured_health(
            health["latest_unattributed"], "unattributed-health"
        )
    observation_time = _exact_mapping(
        health["observation_time"],
        {
            "source_revision",
            "disk_state",
            "ledger_bytes",
            "excluded_from_revision_and_clean_eligibility",
        },
        "health-observation-time",
    )
    _nonnegative_integer(observation_time["source_revision"], "observed-source-revision")
    _nonnegative_integer(observation_time["ledger_bytes"], "ledger-bytes")
    if observation_time["disk_state"] not in {"unknown", "healthy", "pressure", "full"}:
        raise TerminalObservationExportError(
            "terminal-export-projection-disk-state-invalid"
        )
    if observation_time["excluded_from_revision_and_clean_eligibility"] is not True:
        raise TerminalObservationExportError(
            "terminal-export-projection-observation-eligibility-invalid"
        )

    attributable = health["attributable"]
    unattributed = health["latest_unattributed"]
    source_revision = revisions["source_revision"]
    accounting_revision = revisions["accounting_revision"]
    if observation_time["source_revision"] != source_revision:
        raise TerminalObservationExportError(
            "terminal-export-projection-observation-revision-invalid"
        )
    if attribution["project_dispatch_count"] < 1 or attribution[
        "project_connection_count"
    ] < 1:
        raise TerminalObservationExportError(
            "terminal-export-projection-health-attribution-invalid"
        )
    if attribution["state"] == "isolated-project-connection":
        if (
            attribution["project_dispatch_count"] != 1
            or attribution["project_connection_count"] != 1
            or attributable is None
            or unattributed is not None
        ):
            raise TerminalObservationExportError(
                "terminal-export-projection-health-attribution-invalid"
            )
        current_health = attributable
    else:
        if (
            (
                attribution["project_dispatch_count"] == 1
                and attribution["project_connection_count"] == 1
            )
            or unattributed is None
        ):
            raise TerminalObservationExportError(
                "terminal-export-projection-health-attribution-invalid"
            )
        current_health = unattributed
    if attributable is not None and attributable["clean"] != _health_snapshot_clean(
        attributable["snapshot"]
    ):
        raise TerminalObservationExportError(
            "terminal-export-projection-attributable-health-clean-invalid"
        )
    if unattributed is not None and unattributed["clean"] is not False:
        raise TerminalObservationExportError(
            "terminal-export-projection-unattributed-health-clean-invalid"
        )
    if (
        current_health["source_revision"] != source_revision
        or current_health["accounting_revision"] != accounting_revision
        or current_health["snapshot"]["project_id"] != identity["project_id"]
        or revisions["current_project_health_content_revision"]
        != current_health["content_revision"]
    ):
        raise TerminalObservationExportError(
            "terminal-export-projection-current-health-link-invalid"
        )
    if attributable is None:
        if revisions["attributable_health_content_revision"] is not None:
            raise TerminalObservationExportError(
                "terminal-export-projection-attributable-health-link-invalid"
            )
    elif (
        attributable["snapshot"]["project_id"] != identity["project_id"]
        or attributable["source_revision"] > source_revision
        or attributable["accounting_revision"] > accounting_revision
        or revisions["attributable_health_content_revision"]
        != attributable["content_revision"]
    ):
        raise TerminalObservationExportError(
            "terminal-export-projection-attributable-health-link-invalid"
        )

    publication = _exact_mapping(
        record["publication"],
        {
            "required_for_offline_export",
            "state",
            "state_name",
            "current_accounting_revision_confirmed",
            "publication_gap_visible",
        },
        "publication",
    )
    try:
        publication_state = PublicationState(publication["state"])
    except (TypeError, ValueError) as exc:
        raise TerminalObservationExportError(
            "terminal-export-projection-publication-invalid"
        ) from exc
    if (
        publication["required_for_offline_export"] is not False
        or publication["state_name"] != publication_state.name.lower()
        or not isinstance(publication["current_accounting_revision_confirmed"], bool)
        or not isinstance(publication["publication_gap_visible"], bool)
    ):
        raise TerminalObservationExportError(
            "terminal-export-projection-publication-invalid"
        )
    if publication["state"] != current_health["snapshot"]["publication_state"]:
        raise TerminalObservationExportError(
            "terminal-export-projection-publication-health-mismatch"
        )
    expected_clean = bool(
        attributable
        and attributable["clean"]
        and attributable["accounting_revision"] == accounting_revision
        and accounting["coverage_state"]
        == int(CoverageState.OBSERVED_NO_KNOWN_GAP)
        and accounting["conflict_count"] == 0
    )
    if record["clean_summary_eligible"] != expected_clean:
        raise TerminalObservationExportError(
            "terminal-export-projection-clean-eligibility-invalid"
        )
    return json.loads(_canonical(record))


def _health_projection(
    health: Mapping[str, Any],
    *,
    source_revision: int,
    accounting_revision: int,
    isolated: bool,
    scope: Mapping[str, Any],
) -> dict[str, Any]:
    revision_exact = {
        key: health[key]
        for key in (
            "record_type",
            "project_id",
            "connection_state",
            "schema_state",
            "ledger_state",
            "queue_state",
            "publication_state",
            "reason_counts",
            "unassigned_count",
            "conflict_count",
            "rejected_count",
            "queue_depth",
            "last_event_timestamp_seconds",
            "known_coverage_gaps",
        )
    }
    clean = bool(isolated and _health_snapshot_clean(revision_exact))
    captured = {
        "source_revision": source_revision,
        "accounting_revision": accounting_revision,
        "content_revision": _content_revision(
            "cwo-terminal-health-content-v1", revision_exact
        ),
        "clean": clean,
        "snapshot": revision_exact,
    }
    return {
        "current_attribution": {
            "state": (
                "isolated-project-connection"
                if isolated
                else "unavailable-shared-or-unproven-scope"
            ),
            "project_dispatch_count": scope["dispatch_count"],
            "project_connection_count": scope["connection_count"],
        },
        "attributable": captured if isolated else None,
        "latest_unattributed": None if isolated else captured,
        "observation_time": {
            "source_revision": source_revision,
            "disk_state": health["disk_state"],
            "ledger_bytes": health["ledger_bytes"],
            "excluded_from_revision_and_clean_eligibility": True,
        },
    }


def _terminal_candidate(
    project: Mapping[str, Any], dispatch: Mapping[str, Any], source_revision: int
) -> dict[str, Any] | None:
    observation = dispatch["observation"]
    if LifecycleState(observation["lifecycle_state"]) not in TERMINAL_LIFECYCLE_STATES:
        return None
    bindings = dispatch["bindings"]
    scope = dispatch["terminal_projection_scope"]
    if not bindings or any(binding["state"] != "closed" for binding in bindings):
        return None
    binding_count = scope["binding_count"]
    if binding_count < 1 or scope["closed_binding_count"] != binding_count:
        return None
    if (
        scope["source_connection_count"] < 1
        or scope["disconnected_source_connection_count"]
        != scope["source_connection_count"]
        or scope["pending_source_records"] != 0
        or scope["pending_binding_records"] != 0
    ):
        return None
    health = project["health"]
    if health["queue_depth"] != 0:
        return None

    configured = sorted(
        (
            {
                "acknowledgement_provenance": binding[
                    "acknowledgement_provenance"
                ],
                "configured_model": binding["configured_model"],
                "configured_model_provenance": binding[
                    "configured_model_provenance"
                ],
                "configured_effort": binding["configured_effort"],
                "configured_effort_provenance": binding[
                    "configured_effort_provenance"
                ],
            }
            for binding in bindings
        ),
        key=lambda item: _canonical(item),
    )
    aggregate = dispatch["aggregate"]
    tokens = {}
    for kind in TokenKind:
        key = kind.value
        contributors = aggregate["token_cycles"][key]
        tokens[key] = {
            "value": aggregate["token_sums"][key] if contributors else None,
            "contributor_count": contributors,
            "completed_cycles": aggregate["completed_cycles"],
            "state": aggregate["token_states"][key],
        }

    elapsed = observation["timing"]["elapsed_seconds"]
    elapsed_state = observation["timing"]["elapsed_state"]
    elapsed_observed = elapsed if elapsed_state == int(FieldState.PRESENT) else None
    cycle_allowance = calculate_declared_allowance(
        observation["declared_cycle_allowance"],
        aggregate["completed_cycles"],
        coverage=aggregate["coverage_state"],
    )
    elapsed_allowance = calculate_declared_allowance(
        observation["declared_elapsed_allowance_seconds"], elapsed_observed
    )
    purposes = {purpose: 0 for purpose in _PURPOSES}
    for cycle in dispatch["cycles"]:
        purposes[cycle["purpose"]] += 1

    project_scope = project["terminal_projection_scope"]
    isolated = bool(
        project_scope["dispatch_count"] == 1
        and project_scope["connection_count"] == 1
        and scope["source_connection_count"] == 1
    )
    health_projection = _health_projection(
        health,
        source_revision=source_revision,
        accounting_revision=dispatch["snapshot_revision"],
        isolated=isolated,
        scope=project_scope,
    )
    current_health = (
        health_projection["attributable"]
        or health_projection["latest_unattributed"]
    )
    publication = dispatch["publication"]
    record = {
        "record_type": PROJECTION_RECORD_TYPE,
        "projection_version": PROJECTION_VERSION,
        "identity": {
            "project_id": observation["project_id"],
            "dispatch_id": observation["dispatch_id"],
        },
        "revisions": {
            "accounting_revision": dispatch["snapshot_revision"],
            "source_revision": source_revision,
            "attributable_health_content_revision": (
                health_projection["attributable"]["content_revision"]
                if health_projection["attributable"]
                else None
            ),
            "current_project_health_content_revision": current_health[
                "content_revision"
            ],
        },
        "configuration": {
            "requested": {
                "executor": observation["requested_executor"],
                "model": observation["requested_model"],
                "effort": observation["requested_effort"],
            },
            "acknowledged": configured,
            "actual": {
                "model": {"value": None, "state": int(FieldState.UNAVAILABLE)},
                "effort": {"value": None, "state": int(FieldState.UNAVAILABLE)},
            },
        },
        "lifecycle": {
            "state": observation["lifecycle_state"],
            "elapsed_seconds": {"value": elapsed, "state": elapsed_state},
            "timing_provenance": observation["timing"]["provenance"],
        },
        "allowances": {
            "declared_cycle_allowance": observation["declared_cycle_allowance"],
            "declared_elapsed_allowance_seconds": observation[
                "declared_elapsed_allowance_seconds"
            ],
            "enforced_tool_call_limit": observation["enforced_tool_call_limit"],
            "enforced_runtime_limit_seconds": observation[
                "enforced_runtime_limit_seconds"
            ],
            "cycle": _allowance(cycle_allowance),
            "elapsed": _allowance(elapsed_allowance),
        },
        "accounting": {
            "completed_cycles": aggregate["completed_cycles"],
            "coverage_state": aggregate["coverage_state"],
            "conflict_count": aggregate["conflict_count"],
            "tokens": tokens,
            "completion_purpose_counts": purposes,
            "activity": dispatch["activity"],
        },
        "health": health_projection,
        "publication": {
            "required_for_offline_export": False,
            "state": health["publication_state"],
            "state_name": PublicationState(health["publication_state"]).name.lower(),
            "current_accounting_revision_confirmed": not publication["pending"],
            "publication_gap_visible": bool(
                publication["pending"]
                or CoverageGap.PUBLICATION_UNCONFIRMED.value
                in health["known_coverage_gaps"]
            ),
        },
    }
    record["clean_summary_eligible"] = bool(
        health_projection["attributable"]
        and health_projection["attributable"]["clean"]
        and aggregate["coverage_state"]
        == int(CoverageState.OBSERVED_NO_KNOWN_GAP)
        and aggregate["conflict_count"] == 0
    )
    return record


def project_terminal_observations(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Build the allowlisted terminal projection from a coherent ledger view."""

    source_revision = snapshot["snapshot_revision"]
    projected = []
    for project in snapshot["projects"]:
        for dispatch in project["dispatches"]:
            candidate = _terminal_candidate(project, dispatch, source_revision)
            if candidate is not None:
                projected.append(candidate)
    projected.sort(
        key=lambda item: (
            item["identity"]["project_id"],
            item["identity"]["dispatch_id"],
        )
    )
    return projected


class TerminalObservationSink:
    """Owner-private atomic JSON upserts plus a replay cursor."""

    def __init__(self, output_dir: str | os.PathLike[str]) -> None:
        self.output_dir = Path(output_dir).expanduser()
        _private_directory(self.output_dir, create=True)
        self.observation_dir = self.output_dir / OBSERVATION_DIRECTORY_NAME
        _private_directory(self.observation_dir, create=True)

    @contextmanager
    def locked(self):
        path = self.output_dir / LOCK_NAME
        _private_file(path)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags, 0o600)
        except OSError as exc:
            raise TerminalObservationExportError(
                "terminal-export-lock-open-failed"
            ) from exc
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise TerminalObservationExportError("terminal-export-file-unsafe")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _atomic_write(self, path: Path, value: Mapping[str, Any]) -> bool:
        payload = _canonical(value)
        if len(payload) > MAX_SINK_RECORD_BYTES:
            raise TerminalObservationExportError("terminal-export-record-too-large")
        _private_file(path)
        if path.exists() and path.read_bytes() == payload:
            return False
        temporary = path.parent / f".{path.name}.{secrets.token_hex(12)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = -1
        try:
            descriptor = os.open(temporary, flags, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            _private_file(path)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return True
        except OSError as exc:
            raise TerminalObservationExportError(
                "terminal-export-write-failed"
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def cursor(self) -> dict[str, Any] | None:
        value = _read_json(self.output_dir / CURSOR_NAME)
        if value is None:
            return None
        if set(value) != {"record_type", "source_identity", "source_revision"}:
            raise TerminalObservationExportError("terminal-export-cursor-invalid")
        if (
            value["record_type"] != CURSOR_RECORD_TYPE
            or not isinstance(value["source_identity"], str)
            or isinstance(value["source_revision"], bool)
            or not isinstance(value["source_revision"], int)
            or value["source_revision"] < 0
        ):
            raise TerminalObservationExportError("terminal-export-cursor-invalid")
        return value

    def observation_path(self, project_id: str, dispatch_id: str) -> Path:
        name = hashlib.sha256(
            b"cwo-terminal-observation-v1\0"
            + project_id.encode("ascii")
            + b"\0"
            + dispatch_id.encode("ascii")
        ).hexdigest()
        return self.observation_dir / f"{name}.json"

    def _merged_record(
        self, path: Path, candidate: dict[str, Any]
    ) -> dict[str, Any]:
        candidate = _validate_projection(candidate)
        existing = _read_json(path)
        if existing is None:
            return candidate
        existing = _validate_projection(existing)
        if (
            existing.get("record_type") != PROJECTION_RECORD_TYPE
            or existing.get("projection_version") != PROJECTION_VERSION
            or existing.get("identity") != candidate["identity"]
        ):
            raise TerminalObservationExportError(
                "terminal-export-observation-conflict"
            )
        existing_source = existing["revisions"]["source_revision"]
        candidate_source = candidate["revisions"]["source_revision"]
        if candidate_source < existing_source:
            raise TerminalObservationExportError(
                "terminal-export-source-revision-regressed"
            )
        if (
            candidate["revisions"]["accounting_revision"]
            < existing["revisions"]["accounting_revision"]
        ):
            raise TerminalObservationExportError(
                "terminal-export-accounting-revision-regressed"
            )
        old_attributable = existing.get("health", {}).get("attributable")
        new_attributable = candidate["health"]["attributable"]
        preserving_historical_attribution = (
            old_attributable is not None and new_attributable is None
        )
        if preserving_historical_attribution:
            candidate["health"]["attributable"] = old_attributable
        attributable = candidate["health"]["attributable"]
        candidate["revisions"]["attributable_health_content_revision"] = (
            attributable["content_revision"] if attributable else None
        )
        current_attribution_qualified = (
            candidate["health"]["current_attribution"]["state"]
            == "isolated-project-connection"
        )
        candidate["clean_summary_eligible"] = bool(
            attributable
            and attributable["clean"]
            and attributable["accounting_revision"]
            == candidate["revisions"]["accounting_revision"]
            and candidate["accounting"]["coverage_state"]
            == int(CoverageState.OBSERVED_NO_KNOWN_GAP)
            and candidate["accounting"]["conflict_count"] == 0
        )
        merged = _validate_projection(candidate)
        if (
            candidate_source == existing_source
            and _revision_content(merged) != _revision_content(existing)
        ):
            raise TerminalObservationExportError(
                "terminal-export-equal-revision-conflict"
            )
        return merged

    def upsert(self, candidate: dict[str, Any]) -> bool:
        validated = _validate_projection(candidate)
        identity = validated["identity"]
        path = self.observation_path(
            identity["project_id"], identity["dispatch_id"]
        )
        return self._atomic_write(path, self._merged_record(path, validated))

    def advance_cursor(self, source_identity: str, source_revision: int) -> None:
        self._atomic_write(
            self.output_dir / CURSOR_NAME,
            {
                "record_type": CURSOR_RECORD_TYPE,
                "source_identity": source_identity,
                "source_revision": source_revision,
            },
        )


def export_terminal_observations(
    state_dir: str | os.PathLike[str], output_dir: str | os.PathLike[str]
) -> dict[str, int]:
    """Rescan one committed source revision and durably upsert terminal runs."""

    state = Path(os.path.abspath(Path(state_dir).expanduser()))
    output = Path(os.path.abspath(Path(output_dir).expanduser()))
    if state == output or state in output.parents or output in state.parents:
        raise TerminalObservationExportError("terminal-export-source-sink-overlap")
    sink = TerminalObservationSink(output)
    with sink.locked():
        try:
            with ObservabilityLedger(state, readonly=True) as ledger:
                source_identity = _source_identity(state)
                snapshot = ledger.terminal_projection_snapshot()
        except ObservabilityLedgerError as exc:
            raise TerminalObservationExportError(str(exc)) from exc
        source_revision = snapshot["snapshot_revision"]
        cursor = sink.cursor()
        if cursor is not None:
            if cursor["source_identity"] != source_identity:
                raise TerminalObservationExportError(
                    "terminal-export-source-identity-mismatch"
                )
            if source_revision < cursor["source_revision"]:
                raise TerminalObservationExportError(
                    "terminal-export-source-revision-regressed"
                )
            if source_revision == cursor["source_revision"]:
                return {
                    "source_revision": source_revision,
                    "eligible_runs": 0,
                    "upserted_runs": 0,
                }
        candidates = project_terminal_observations(snapshot)
        upserted = sum(sink.upsert(candidate) for candidate in candidates)
        # This is intentionally the final durable write. If it fails, restart
        # replays the idempotent run upserts from the previous source revision.
        sink.advance_cursor(source_identity, source_revision)
        return {
            "source_revision": source_revision,
            "eligible_runs": len(candidates),
            "upserted_runs": upserted,
        }


__all__ = [
    "PROJECTION_RECORD_TYPE",
    "PROJECTION_VERSION",
    "TerminalObservationExportError",
    "TerminalObservationSink",
    "export_terminal_observations",
    "project_terminal_observations",
]
