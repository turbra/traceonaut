"""Semantic contracts for additive supervisor dispatch observability.

The helpers in this module normalize already-authorized controller records. They
validate shape, bounded vocabularies, numeric semantics, and relationships, but
they do not authenticate controller receipts, prove that UUIDs were randomly
generated, or establish dispatch ownership. Those are adapter and ledger duties.

Raw source identifiers are not accepted. Callers must replace thread, turn, and
response identifiers with their protected domain-separated fingerprints before
calling these helpers.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from enum import Enum, IntEnum
from math import isfinite
import re
from types import MappingProxyType
from typing import Any
from uuid import UUID


SAFE_INTEGER_MAX = (1 << 53) - 1
UNKNOWN_LABEL = "unknown"

MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_JSON_DEPTH = 32
MAX_NORMALIZED_RECORD_BYTES = 16 * 1024
MAX_PENDING_RECORDS = 1_024
MAX_QUEUE_RECORDS = 4_096
MAX_PENDING_BINDING_SECONDS = 60

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_OPAQUE_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_BOUNDED_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/-]{0,127}$")


class StringEnum(str, Enum):
    """String enum whose serialized representation is its value."""

    def __str__(self) -> str:
        return self.value


class RegistrationState(StringEnum):
    ENABLED = "enabled"
    REVOKED = "revoked"


class BindingState(StringEnum):
    ACTIVE = "active"
    CLOSED = "closed"
    REVOKED = "revoked"


class LifecycleState(StringEnum):
    SUBMITTED = "submitted"
    ACKNOWLEDGED = "acknowledged"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CONTROL_LOST = "control-lost"
    UNKNOWN = "unknown"


class FieldState(IntEnum):
    UNAVAILABLE = 0
    PRESENT = 1
    RUNTIME_NORMALIZED = 2
    INVALID = 3
    PARTIAL = 4
    UNQUALIFIED = 5
    CLOCK_GAP = 6
    OVERFLOW = 7


class CoverageState(IntEnum):
    UNKNOWN = 0
    OBSERVED_NO_KNOWN_GAP = 1
    KNOWN_GAP = 2
    ACCOUNTING_CONFLICT = 3


class DispatchField(StringEnum):
    DISPATCH_ELAPSED = "dispatch_elapsed"
    DECLARED_CYCLE_ALLOWANCE = "declared_cycle_allowance"
    DECLARED_ELAPSED_ALLOWANCE = "declared_elapsed_allowance"
    CONFIGURED_MODEL = "configured_model"
    CONFIGURED_EFFORT = "configured_effort"
    ACTUAL_MODEL = "actual_model"
    ACTUAL_EFFORT = "actual_effort"
    REQUEST_ATTEMPTS = "request_attempts"
    PER_RESPONSE_DURATION = "per_response_duration"
    ORCHESTRATION_OVERHEAD = "orchestration_overhead"
    TOOL_RESPONSE_LINK = "tool_response_link"


class Component(StringEnum):
    CONNECTION = "connection"
    SCHEMA = "schema"
    LEDGER = "ledger"
    QUEUE = "queue"


class ConnectionState(StringEnum):
    DISABLED = "disabled"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"


class SchemaState(StringEnum):
    UNQUALIFIED = "unqualified"
    COMPATIBLE = "compatible"
    INCOMPATIBLE = "incompatible"


class LedgerState(StringEnum):
    HEALTHY = "healthy"
    FAULT = "fault"
    RECOVERY = "recovery"


class QueueState(StringEnum):
    HEALTHY = "healthy"
    OVERLOADED = "overloaded"


class DiskState(StringEnum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    PRESSURE = "pressure"
    FULL = "full"


class PublicationState(IntEnum):
    DISABLED = 0
    AVAILABLE = 1
    QUERY_UNAVAILABLE = 2
    CONFIRMATION_MISMATCH = 3
    CAPACITY_BLOCKED = 4


class TokenKind(StringEnum):
    INPUT = "input"
    CACHED_INPUT = "cached_input"
    CACHE_WRITE_INPUT = "cache_write_input"
    OUTPUT = "output"
    REASONING_OUTPUT = "reasoning_output"
    TOTAL = "total"


class TokenProvenance(StringEnum):
    UNAVAILABLE = "unavailable"
    RUNTIME_REPORTED = "runtime-reported"
    RUNTIME_NORMALIZED_UPSTREAM_PRESENCE_UNKNOWN = (
        "runtime-normalized-upstream-presence-unknown"
    )
    INVALID = "invalid"


class CompletionPurpose(StringEnum):
    UNKNOWN = "unknown"
    MODEL_RESPONSE = "model-response"
    MANUAL_COMPACTION = "manual-compaction"
    AUTOMATIC_COMPACTION = "automatic-compaction"
    AUXILIARY = "auxiliary"


class ClockSource(StringEnum):
    SUPERVISOR_MONOTONIC = "supervisor-monotonic"


class TimingProvenance(StringEnum):
    SUPERVISOR_LIFECYCLE_RECEIPT = "supervisor-lifecycle-receipt"


class AcknowledgementProvenance(StringEnum):
    TRUSTED_CONTROLLER_RECEIPT = "trusted-controller-receipt"


class ConfigurationProvenance(StringEnum):
    ACKNOWLEDGED_THREAD_CONFIGURATION = "acknowledged-thread-configuration"


class HealthReason(StringEnum):
    INVALID_IDENTITY = "invalid-identity"
    INVALID_USAGE = "invalid-usage"
    SCHEMA_INCOMPATIBLE = "schema-incompatible"
    BINDING_AMBIGUOUS = "binding-ambiguous"
    BINDING_CONFLICT = "binding-conflict"
    ACCOUNTING_CONFLICT = "accounting-conflict"
    PENDING_EXPIRED = "pending-expired"
    QUEUE_OVERFLOW = "queue-overflow"
    DISK_FULL = "disk-full"
    KEY_UNAVAILABLE = "key-unavailable"
    SOURCE_DISCONNECTED = "source-disconnected"
    UNSUPPORTED_STATE_VERSION = "unsupported-state-version"
    COLLECTOR_WRITE_FAILURE = "collector-write-failure"
    COLLECTOR_HOOK_FAILURE = "collector-hook-failure"
    SHUTDOWN_PENDING = "shutdown-with-pending-events"


class CoverageGap(StringEnum):
    CRASH_BEFORE_COMMIT = "crash-before-commit"
    SOURCE_DISCONNECT = "source-disconnect"
    QUEUE_OVERFLOW = "queue-overflow"
    UNSUPPORTED_SOURCE = "unsupported-source"
    KEY_LOSS = "key-loss"
    UNASSIGNED_BINDING = "unassigned-binding"
    TIMING_EPOCH_GAP = "timing-epoch-gap"
    AUTOMATIC_COMPACTION_UNQUALIFIED = "automatic-compaction-unqualified"
    CONCURRENT_BINDING_UNQUALIFIED = "concurrent-binding-unqualified"
    PUBLICATION_UNCONFIRMED = "publication-unconfirmed"
    COLLECTOR_WRITE_FAILURE = "collector-write-failure"
    COLLECTOR_HOOK_FAILURE = "collector-hook-failure"
    SHUTDOWN_PENDING = "shutdown-with-pending-events"


class TurnOutcome(StringEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class ToolCategory(StringEnum):
    COMMAND = "command"
    FILE_CHANGE = "file_change"
    DYNAMIC = "dynamic"
    MCP = "mcp"
    WEB = "web"
    OTHER = "other"


class ToolLifecycle(StringEnum):
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"


class TelemetryDisposition(StringEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    UNASSIGNED = "unassigned"
    CONFLICT = "conflict"
    REJECTED = "rejected"
    LOST = "lost"


LIFECYCLE_CODES = MappingProxyType(
    {
        LifecycleState.SUBMITTED: 0,
        LifecycleState.ACKNOWLEDGED: 1,
        LifecycleState.RUNNING: 2,
        LifecycleState.COMPLETED: 3,
        LifecycleState.FAILED: 4,
        LifecycleState.INTERRUPTED: 5,
        LifecycleState.CONTROL_LOST: 6,
        LifecycleState.UNKNOWN: 7,
    }
)

TERMINAL_LIFECYCLE_STATES = frozenset(
    {
        LifecycleState.COMPLETED,
        LifecycleState.FAILED,
        LifecycleState.INTERRUPTED,
        LifecycleState.CONTROL_LOST,
        LifecycleState.UNKNOWN,
    }
)

TOKEN_KIND_PROVENANCE = MappingProxyType(
    {
        TokenKind.INPUT: TokenProvenance.RUNTIME_REPORTED,
        TokenKind.CACHED_INPUT: (
            TokenProvenance.RUNTIME_NORMALIZED_UPSTREAM_PRESENCE_UNKNOWN
        ),
        TokenKind.CACHE_WRITE_INPUT: (
            TokenProvenance.RUNTIME_NORMALIZED_UPSTREAM_PRESENCE_UNKNOWN
        ),
        TokenKind.OUTPUT: TokenProvenance.RUNTIME_REPORTED,
        TokenKind.REASONING_OUTPUT: (
            TokenProvenance.RUNTIME_NORMALIZED_UPSTREAM_PRESENCE_UNKNOWN
        ),
        TokenKind.TOTAL: TokenProvenance.RUNTIME_REPORTED,
    }
)

COMPONENT_STATES = MappingProxyType(
    {
        Component.CONNECTION: tuple(state.value for state in ConnectionState),
        Component.SCHEMA: tuple(state.value for state in SchemaState),
        Component.LEDGER: tuple(state.value for state in LedgerState),
        Component.QUEUE: tuple(state.value for state in QueueState),
    }
)


@dataclass(frozen=True, slots=True)
class AllowanceCalculation:
    remaining: int | float | None
    overrun: int | float | None
    lower_bound_overrun: bool


@dataclass(frozen=True, slots=True)
class MetricFamily:
    metric_type: str
    labels: tuple[str, ...]


_D = ("project_id", "dispatch_id")
_C = _D + ("cycle_ordinal",)

METRIC_FAMILIES = MappingProxyType(
    {
        "cwo_dispatch_info": MetricFamily(
            "gauge",
            _D
            + (
                "agent_id",
                "packet_ref",
                "requested_model",
                "requested_effort",
            ),
        ),
        "cwo_dispatch_configured_model_info": MetricFamily(
            "gauge", _D + ("configured_model",)
        ),
        "cwo_dispatch_configured_effort_info": MetricFamily(
            "gauge", _D + ("configured_effort",)
        ),
        "cwo_dispatch_snapshot_revision": MetricFamily("gauge", _D),
        "cwo_dispatch_state": MetricFamily("gauge", _D),
        "cwo_agent_state": MetricFamily("gauge", ("project_id", "agent_id")),
        "cwo_dispatch_completed_cycles_total": MetricFamily("counter", _D),
        "cwo_cycle_present": MetricFamily("gauge", _C),
        "cwo_cycle_observed_timestamp_seconds": MetricFamily("gauge", _C),
        "cwo_cycle_tokens": MetricFamily("gauge", _C + ("token_kind",)),
        "cwo_dispatch_observed_tokens": MetricFamily("gauge", _D + ("token_kind",)),
        "cwo_cycle_token_state": MetricFamily("gauge", _C + ("token_kind",)),
        "cwo_dispatch_token_state": MetricFamily("gauge", _D + ("token_kind",)),
        "cwo_dispatch_token_cycles": MetricFamily("gauge", _D + ("token_kind",)),
        "cwo_dispatch_elapsed_seconds": MetricFamily("gauge", _D),
        "cwo_dispatch_declared_cycle_allowance": MetricFamily("gauge", _D),
        "cwo_dispatch_declared_cycle_remaining": MetricFamily("gauge", _D),
        "cwo_dispatch_declared_cycle_overrun": MetricFamily("gauge", _D),
        "cwo_dispatch_declared_elapsed_allowance_seconds": MetricFamily("gauge", _D),
        "cwo_dispatch_declared_elapsed_remaining_seconds": MetricFamily("gauge", _D),
        "cwo_dispatch_declared_elapsed_overrun_seconds": MetricFamily("gauge", _D),
        "cwo_dispatch_enforced_tool_call_limit": MetricFamily("gauge", _D),
        "cwo_dispatch_enforced_runtime_limit_seconds": MetricFamily("gauge", _D),
        "cwo_dispatch_field_state": MetricFamily("gauge", _D + ("field",)),
        "cwo_dispatch_retry_notices_total": MetricFamily("counter", _D),
        "cwo_dispatch_turn_outcomes_total": MetricFamily("counter", _D + ("outcome",)),
        "cwo_dispatch_tool_events_total": MetricFamily(
            "counter", _D + ("category", "lifecycle")
        ),
        "cwo_telemetry_events_total": MetricFamily(
            "counter", ("project_id", "disposition")
        ),
        "cwo_telemetry_component_state": MetricFamily(
            "gauge", ("project_id", "component", "state")
        ),
        "cwo_telemetry_last_event_timestamp_seconds": MetricFamily(
            "gauge", ("project_id",)
        ),
        "cwo_telemetry_queue_depth": MetricFamily("gauge", ("project_id",)),
        "cwo_telemetry_ledger_bytes": MetricFamily("gauge", ("project_id",)),
        "cwo_telemetry_publication_state": MetricFamily("gauge", ("project_id",)),
        "cwo_telemetry_publication_pending_dispatches": MetricFamily(
            "gauge", ("project_id",)
        ),
        "cwo_dispatch_coverage_state": MetricFamily("gauge", _D),
    }
)


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be a mapping")
    return value


def _required(value: Mapping[str, Any], fields: Collection[str], record: str) -> None:
    missing = sorted(set(fields).difference(value))
    if missing:
        raise ValueError(f"{record} is missing required fields: {missing}")


def normalize_nonnegative_integer(value: Any, field: str) -> int:
    """Return an exact nonnegative integer without applying export limits."""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a nonnegative integer")
    return value


def normalize_positive_integer(value: Any, field: str) -> int:
    result = normalize_nonnegative_integer(value, field)
    if result == 0:
        raise ValueError(f"{field} must be greater than zero")
    return result


def normalize_nonnegative_number(value: Any, field: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError(f"{field} must be a nonnegative finite number")
    if isinstance(value, float) and not isfinite(value):
        raise ValueError(f"{field} must be a nonnegative finite number")
    return value


def _nullable_integer(value: Any, field: str) -> int | None:
    if value is None:
        return None
    return normalize_nonnegative_integer(value, field)


def _nullable_number(value: Any, field: str) -> int | float | None:
    if value is None:
        return None
    return normalize_nonnegative_number(value, field)


def numeric_for_export(
    value: Any,
    *,
    state: FieldState | int = FieldState.PRESENT,
) -> tuple[int | None, FieldState]:
    """Apply Prometheus' exact-integer boundary without changing ledger data."""

    normalized_state = _int_enum(state, FieldState, "state")
    if value is None or normalized_state in {
        FieldState.UNAVAILABLE,
        FieldState.INVALID,
        FieldState.UNQUALIFIED,
        FieldState.CLOCK_GAP,
        FieldState.OVERFLOW,
    }:
        return None, normalized_state
    integer = normalize_nonnegative_integer(value, "value")
    if integer > SAFE_INTEGER_MAX:
        return None, FieldState.OVERFLOW
    return integer, normalized_state


def calculate_declared_allowance(
    allowance: Any,
    observed: Any,
    *,
    coverage: CoverageState | int = CoverageState.OBSERVED_NO_KNOWN_GAP,
) -> AllowanceCalculation:
    """Calculate display-only remaining/overrun values.

    A known coverage gap suppresses exact results. If captured use already
    exceeds the allowance, the visible excess remains a lower bound.
    """

    if allowance is None or observed is None:
        return AllowanceCalculation(None, None, False)
    normalized_allowance = normalize_nonnegative_number(allowance, "allowance")
    normalized_observed = normalize_nonnegative_number(observed, "observed")
    normalized_coverage = _int_enum(coverage, CoverageState, "coverage")
    if normalized_coverage is CoverageState.OBSERVED_NO_KNOWN_GAP:
        return AllowanceCalculation(
            max(normalized_allowance - normalized_observed, 0),
            max(normalized_observed - normalized_allowance, 0),
            False,
        )
    if (
        normalized_coverage is CoverageState.KNOWN_GAP
        and normalized_observed > normalized_allowance
    ):
        return AllowanceCalculation(
            None,
            normalized_observed - normalized_allowance,
            True,
        )
    return AllowanceCalculation(None, None, False)


def normalize_bounded_label(
    value: Any,
    vocabulary: Collection[str],
    *,
    nullable: bool = False,
    field: str = "label",
) -> str | None:
    """Map unknown values to a bounded category without retaining raw text."""

    if value is None and nullable:
        return None
    allowed = frozenset(vocabulary)
    if not allowed or UNKNOWN_LABEL in allowed:
        raise ValueError(f"{field} vocabulary must be nonempty and reserve 'unknown'")
    if any(
        not isinstance(item, str) or not _BOUNDED_LABEL_RE.fullmatch(item)
        for item in allowed
    ):
        raise ValueError(f"{field} vocabulary contains an invalid label")
    if isinstance(value, str) and value in allowed:
        return value
    return UNKNOWN_LABEL


def _enum_value(value: Any, enum_type: type[StringEnum], field: str) -> str:
    try:
        return enum_type(value).value
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} is invalid") from exc


def _int_enum(
    value: Any,
    enum_type: type[IntEnum],
    field: str,
) -> IntEnum:
    if isinstance(value, bool):
        raise ValueError(f"{field} is invalid")
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} is invalid") from exc


def _uuid(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a canonical UUID")
    try:
        parsed = UUID(value)
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{field} must be a canonical UUID") from exc
    if str(parsed) != value or parsed.int == 0:
        raise ValueError(f"{field} must be a nonzero canonical UUID")
    return value


def _sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase SHA-256 value")
    return value


def _opaque_alias(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _OPAQUE_ALIAS_RE.fullmatch(value):
        raise ValueError(f"{field} must be a bounded opaque CWO alias")
    return value


def _optional_enum_provenance(
    value: Any,
    configured_value: str | None,
    field: str,
) -> str | None:
    if configured_value is None:
        if value is not None:
            raise ValueError(f"{field} must be null when its value is null")
        return None
    return _enum_value(value, ConfigurationProvenance, field)


def _normalize_resource_limits(value: Any) -> dict[str, int]:
    limits = _mapping(value, "resource_limits")
    fields = {
        "max_frame_bytes",
        "max_json_depth",
        "max_normalized_record_bytes",
        "max_pending_records",
        "max_queue_records",
        "pending_binding_deadline_seconds",
        "max_registrations",
        "max_disk_bytes",
    }
    _required(limits, fields, "resource_limits")
    result = {
        field: normalize_positive_integer(limits[field], f"resource_limits.{field}")
        for field in fields
    }
    maxima = {
        "max_frame_bytes": MAX_FRAME_BYTES,
        "max_json_depth": MAX_JSON_DEPTH,
        "max_normalized_record_bytes": MAX_NORMALIZED_RECORD_BYTES,
        "max_pending_records": MAX_PENDING_RECORDS,
        "max_queue_records": MAX_QUEUE_RECORDS,
        "pending_binding_deadline_seconds": MAX_PENDING_BINDING_SECONDS,
    }
    for field, maximum in maxima.items():
        if result[field] > maximum:
            raise ValueError(f"resource_limits.{field} exceeds the v1 safety envelope")
    return {field: result[field] for field in sorted(fields)}


def normalize_project_registration(value: Any) -> dict[str, Any]:
    record = _mapping(value, "project_registration")
    fields = {
        "project_id",
        "owning_principal_id",
        "runtime_owner_id",
        "state",
        "generation",
        "resource_limits",
    }
    _required(record, fields, "project_registration")
    return {
        "record_type": "project_registration.v1",
        "project_id": _uuid(record["project_id"], "project_id"),
        "owning_principal_id": normalize_nonnegative_integer(
            record["owning_principal_id"], "owning_principal_id"
        ),
        "runtime_owner_id": _uuid(record["runtime_owner_id"], "runtime_owner_id"),
        "state": _enum_value(record["state"], RegistrationState, "state"),
        "generation": normalize_positive_integer(record["generation"], "generation"),
        "resource_limits": _normalize_resource_limits(record["resource_limits"]),
    }


def _normalize_timing(value: Any) -> dict[str, Any]:
    timing = _mapping(value, "timing")
    fields = {
        "clock_source",
        "clock_epoch_id",
        "provenance",
        "submitted_seconds",
        "acknowledged_seconds",
        "running_seconds",
        "terminal_seconds",
        "elapsed_seconds",
        "elapsed_state",
    }
    _required(timing, fields, "timing")
    epoch = (
        None
        if timing["clock_epoch_id"] is None
        else _uuid(timing["clock_epoch_id"], "timing.clock_epoch_id")
    )
    points = {
        field: _nullable_number(timing[field], f"timing.{field}")
        for field in (
            "submitted_seconds",
            "acknowledged_seconds",
            "running_seconds",
            "terminal_seconds",
        )
    }
    elapsed = _nullable_number(timing["elapsed_seconds"], "timing.elapsed_seconds")
    elapsed_state = _int_enum(
        timing["elapsed_state"], FieldState, "timing.elapsed_state"
    )
    if elapsed is None and elapsed_state in {
        FieldState.PRESENT,
        FieldState.RUNTIME_NORMALIZED,
        FieldState.PARTIAL,
    }:
        raise ValueError("timing.elapsed_state claims a missing elapsed value")
    if elapsed is not None and elapsed_state is not FieldState.PRESENT:
        raise ValueError("timing.elapsed_seconds requires PRESENT field state")
    if epoch is None and any(point is not None for point in points.values()):
        raise ValueError("timing points require a common clock epoch")
    previous: int | float | None = None
    for field in (
        "submitted_seconds",
        "acknowledged_seconds",
        "running_seconds",
        "terminal_seconds",
    ):
        point = points[field]
        if point is not None and previous is not None and point < previous:
            raise ValueError("timing points must be monotonic")
        if point is not None:
            previous = point
    return {
        "clock_source": _enum_value(
            timing["clock_source"], ClockSource, "timing.clock_source"
        ),
        "clock_epoch_id": epoch,
        "provenance": _enum_value(
            timing["provenance"], TimingProvenance, "timing.provenance"
        ),
        **points,
        "elapsed_seconds": elapsed,
        "elapsed_state": int(elapsed_state),
    }


def normalize_dispatch_observation(
    value: Any,
    *,
    executor_vocabulary: Collection[str],
    model_vocabulary: Collection[str],
    effort_vocabulary: Collection[str],
) -> dict[str, Any]:
    record = _mapping(value, "dispatch_observation")
    fields = {
        "project_id",
        "dispatch_id",
        "agent_id",
        "packet_ref",
        "packet_sha256",
        "supervisor_submission_ref",
        "requested_executor",
        "requested_model",
        "requested_effort",
        "declared_cycle_allowance",
        "declared_elapsed_allowance_seconds",
        "enforced_tool_call_limit",
        "enforced_runtime_limit_seconds",
        "lifecycle_state",
        "timing",
        "coverage_state",
    }
    _required(record, fields, "dispatch_observation")
    return {
        "record_type": "dispatch_observation.v1",
        "project_id": _uuid(record["project_id"], "project_id"),
        "dispatch_id": _uuid(record["dispatch_id"], "dispatch_id"),
        "agent_id": _uuid(record["agent_id"], "agent_id"),
        "packet_ref": _opaque_alias(record["packet_ref"], "packet_ref"),
        "packet_sha256": _sha256(record["packet_sha256"], "packet_sha256"),
        "supervisor_submission_ref": _opaque_alias(
            record["supervisor_submission_ref"], "supervisor_submission_ref"
        ),
        "requested_executor": normalize_bounded_label(
            record["requested_executor"],
            executor_vocabulary,
            field="requested_executor",
        ),
        "requested_model": normalize_bounded_label(
            record["requested_model"],
            model_vocabulary,
            nullable=True,
            field="requested_model",
        ),
        "requested_effort": normalize_bounded_label(
            record["requested_effort"],
            effort_vocabulary,
            nullable=True,
            field="requested_effort",
        ),
        "declared_cycle_allowance": _nullable_integer(
            record["declared_cycle_allowance"], "declared_cycle_allowance"
        ),
        "declared_elapsed_allowance_seconds": _nullable_number(
            record["declared_elapsed_allowance_seconds"],
            "declared_elapsed_allowance_seconds",
        ),
        "enforced_tool_call_limit": _nullable_integer(
            record["enforced_tool_call_limit"], "enforced_tool_call_limit"
        ),
        "enforced_runtime_limit_seconds": _nullable_number(
            record["enforced_runtime_limit_seconds"],
            "enforced_runtime_limit_seconds",
        ),
        "lifecycle_state": _enum_value(
            record["lifecycle_state"], LifecycleState, "lifecycle_state"
        ),
        "timing": _normalize_timing(record["timing"]),
        "coverage_state": int(
            _int_enum(record["coverage_state"], CoverageState, "coverage_state")
        ),
    }


def validate_lifecycle_transition(previous: Any, current: Any) -> None:
    """Validate an additive transition; receipts remain the authority for it."""

    prior = LifecycleState(_enum_value(previous, LifecycleState, "previous"))
    next_state = LifecycleState(_enum_value(current, LifecycleState, "current"))
    allowed = {
        LifecycleState.SUBMITTED: {
            LifecycleState.SUBMITTED,
            LifecycleState.ACKNOWLEDGED,
            LifecycleState.RUNNING,
            *TERMINAL_LIFECYCLE_STATES,
        },
        LifecycleState.ACKNOWLEDGED: {
            LifecycleState.ACKNOWLEDGED,
            LifecycleState.RUNNING,
            *TERMINAL_LIFECYCLE_STATES,
        },
        LifecycleState.RUNNING: {LifecycleState.RUNNING, *TERMINAL_LIFECYCLE_STATES},
    }
    if prior in TERMINAL_LIFECYCLE_STATES:
        valid = next_state is prior
    else:
        valid = next_state in allowed[prior]
    if not valid:
        raise ValueError(
            f"invalid lifecycle transition: {prior.value} -> {next_state.value}"
        )


def normalize_runtime_binding(
    value: Any,
    *,
    model_vocabulary: Collection[str],
    effort_vocabulary: Collection[str],
) -> dict[str, Any]:
    record = _mapping(value, "runtime_binding")
    fields = {
        "binding_id",
        "project_id",
        "registration_generation",
        "connection_id",
        "dispatch_id",
        "agent_id",
        "packet_ref",
        "packet_sha256",
        "supervisor_submission_ref",
        "thread_fingerprint",
        "turn_fingerprint",
        "acknowledgement_receipt_sha256",
        "acknowledgement_provenance",
        "state",
        "configured_model",
        "configured_model_provenance",
        "configured_effort",
        "configured_effort_provenance",
    }
    _required(record, fields, "runtime_binding")
    configured_model = normalize_bounded_label(
        record["configured_model"],
        model_vocabulary,
        nullable=True,
        field="configured_model",
    )
    configured_effort = normalize_bounded_label(
        record["configured_effort"],
        effort_vocabulary,
        nullable=True,
        field="configured_effort",
    )
    return {
        "record_type": "runtime_binding.v1",
        "binding_id": _uuid(record["binding_id"], "binding_id"),
        "project_id": _uuid(record["project_id"], "project_id"),
        "registration_generation": normalize_positive_integer(
            record["registration_generation"], "registration_generation"
        ),
        "connection_id": _uuid(record["connection_id"], "connection_id"),
        "dispatch_id": _uuid(record["dispatch_id"], "dispatch_id"),
        "agent_id": _uuid(record["agent_id"], "agent_id"),
        "packet_ref": _opaque_alias(record["packet_ref"], "packet_ref"),
        "packet_sha256": _sha256(record["packet_sha256"], "packet_sha256"),
        "supervisor_submission_ref": _opaque_alias(
            record["supervisor_submission_ref"], "supervisor_submission_ref"
        ),
        "thread_fingerprint": _sha256(
            record["thread_fingerprint"], "thread_fingerprint"
        ),
        "turn_fingerprint": _sha256(record["turn_fingerprint"], "turn_fingerprint"),
        "acknowledgement_receipt_sha256": _sha256(
            record["acknowledgement_receipt_sha256"],
            "acknowledgement_receipt_sha256",
        ),
        "acknowledgement_provenance": _enum_value(
            record["acknowledgement_provenance"],
            AcknowledgementProvenance,
            "acknowledgement_provenance",
        ),
        "state": _enum_value(record["state"], BindingState, "state"),
        "configured_model": configured_model,
        "configured_model_provenance": _optional_enum_provenance(
            record["configured_model_provenance"],
            configured_model,
            "configured_model_provenance",
        ),
        "configured_effort": configured_effort,
        "configured_effort_provenance": _optional_enum_provenance(
            record["configured_effort_provenance"],
            configured_effort,
            "configured_effort_provenance",
        ),
    }


def _token_entry(kind: TokenKind, value: Any) -> dict[str, Any]:
    if value is None:
        return {
            "value": None,
            "state": int(FieldState.UNAVAILABLE),
            "provenance": TokenProvenance.UNAVAILABLE.value,
        }
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return {
            "value": None,
            "state": int(FieldState.INVALID),
            "provenance": TokenProvenance.INVALID.value,
        }
    provenance = TOKEN_KIND_PROVENANCE[kind]
    state = (
        FieldState.PRESENT
        if provenance is TokenProvenance.RUNTIME_REPORTED
        else FieldState.RUNTIME_NORMALIZED
    )
    return {
        "value": value,
        "state": int(state),
        "provenance": provenance.value,
    }


def _invalidate_relationship(entry: dict[str, Any]) -> None:
    entry["state"] = int(FieldState.INVALID)
    entry["provenance"] = TokenProvenance.INVALID.value


def normalize_token_usage(value: Any) -> dict[str, dict[str, Any]] | None:
    """Normalize nullable usage while preserving invalid-versus-missing state."""

    if value is None:
        return None
    if not isinstance(value, Mapping):
        return {
            kind.value: {
                "value": None,
                "state": int(FieldState.INVALID),
                "provenance": TokenProvenance.INVALID.value,
            }
            for kind in TokenKind
        }
    result = {
        kind.value: _token_entry(kind, value.get(kind.value)) for kind in TokenKind
    }

    def valid(kind: TokenKind) -> bool:
        return result[kind.value]["state"] in {
            int(FieldState.PRESENT),
            int(FieldState.RUNTIME_NORMALIZED),
        }

    if valid(TokenKind.CACHED_INPUT) and valid(TokenKind.INPUT):
        if (
            result[TokenKind.CACHED_INPUT.value]["value"]
            > result[TokenKind.INPUT.value]["value"]
        ):
            _invalidate_relationship(result[TokenKind.CACHED_INPUT.value])
    if valid(TokenKind.REASONING_OUTPUT) and valid(TokenKind.OUTPUT):
        if (
            result[TokenKind.REASONING_OUTPUT.value]["value"]
            > result[TokenKind.OUTPUT.value]["value"]
        ):
            _invalidate_relationship(result[TokenKind.REASONING_OUTPUT.value])
    if all(
        valid(kind) for kind in (TokenKind.INPUT, TokenKind.OUTPUT, TokenKind.TOTAL)
    ):
        if (
            result[TokenKind.TOTAL.value]["value"]
            != result[TokenKind.INPUT.value]["value"]
            + result[TokenKind.OUTPUT.value]["value"]
        ):
            _invalidate_relationship(result[TokenKind.TOTAL.value])
    return result


def token_for_export(entry: Any) -> tuple[int | None, FieldState]:
    normalized = _mapping(entry, "token_entry")
    _required(normalized, {"value", "state", "provenance"}, "token_entry")
    state = _int_enum(normalized["state"], FieldState, "token_entry.state")
    return numeric_for_export(normalized["value"], state=state)


def aggregate_token_state(
    kind: TokenKind | str,
    *,
    contributing_cycles: Any,
    observed_cycles: Any,
) -> FieldState:
    """Return the dispatch token state without inventing missing contributions."""

    try:
        normalized_kind = TokenKind(kind)
    except (TypeError, ValueError) as exc:
        raise ValueError("kind is invalid") from exc
    contributing = normalize_nonnegative_integer(
        contributing_cycles, "contributing_cycles"
    )
    observed = normalize_nonnegative_integer(observed_cycles, "observed_cycles")
    if contributing > observed:
        raise ValueError("contributing_cycles cannot exceed observed_cycles")
    if contributing == 0:
        return FieldState.UNAVAILABLE
    if contributing < observed:
        return FieldState.PARTIAL
    if (
        TOKEN_KIND_PROVENANCE[normalized_kind]
        is TokenProvenance.RUNTIME_NORMALIZED_UPSTREAM_PRESENCE_UNKNOWN
    ):
        return FieldState.RUNTIME_NORMALIZED
    return FieldState.PRESENT


def normalize_model_completion(value: Any) -> dict[str, Any]:
    record = _mapping(value, "model_completion")
    fields = {
        "binding_id",
        "project_id",
        "dispatch_id",
        "thread_fingerprint",
        "turn_fingerprint",
        "response_fingerprint",
        "cycle_ordinal",
        "usage",
        "purpose",
        "source_compatibility_sha256",
        "observed_at_seconds",
        "runtime_emitted_at_seconds",
    }
    _required(record, fields, "model_completion")
    try:
        purpose = CompletionPurpose(record["purpose"]).value
    except (TypeError, ValueError):
        purpose = CompletionPurpose.UNKNOWN.value
    return {
        "record_type": "model_completion.v1",
        "binding_id": _uuid(record["binding_id"], "binding_id"),
        "project_id": _uuid(record["project_id"], "project_id"),
        "dispatch_id": _uuid(record["dispatch_id"], "dispatch_id"),
        "thread_fingerprint": _sha256(
            record["thread_fingerprint"], "thread_fingerprint"
        ),
        "turn_fingerprint": _sha256(record["turn_fingerprint"], "turn_fingerprint"),
        "response_fingerprint": _sha256(
            record["response_fingerprint"], "response_fingerprint"
        ),
        "cycle_ordinal": normalize_positive_integer(
            record["cycle_ordinal"], "cycle_ordinal"
        ),
        "usage": normalize_token_usage(record["usage"]),
        "purpose": purpose,
        "source_compatibility_sha256": _sha256(
            record["source_compatibility_sha256"], "source_compatibility_sha256"
        ),
        "observed_at_seconds": normalize_nonnegative_number(
            record["observed_at_seconds"], "observed_at_seconds"
        ),
        "runtime_emitted_at_seconds": _nullable_number(
            record["runtime_emitted_at_seconds"], "runtime_emitted_at_seconds"
        ),
    }


def normalize_telemetry_health(value: Any) -> dict[str, Any]:
    record = _mapping(value, "telemetry_health")
    fields = {
        "project_id",
        "connection_state",
        "schema_state",
        "ledger_state",
        "queue_state",
        "disk_state",
        "publication_state",
        "reason_counts",
        "unassigned_count",
        "conflict_count",
        "rejected_count",
        "queue_depth",
        "ledger_bytes",
        "last_event_timestamp_seconds",
        "known_coverage_gaps",
    }
    _required(record, fields, "telemetry_health")
    counts = _mapping(record["reason_counts"], "reason_counts")
    unexpected_reasons = sorted(
        set(counts).difference(reason.value for reason in HealthReason)
    )
    if unexpected_reasons:
        raise ValueError(f"reason_counts has unsupported reasons: {unexpected_reasons}")
    reason_counts = {
        reason.value: normalize_nonnegative_integer(
            counts.get(reason.value, 0), f"reason_counts.{reason.value}"
        )
        for reason in HealthReason
    }
    gaps = record["known_coverage_gaps"]
    if not isinstance(gaps, list):
        raise ValueError("known_coverage_gaps must be a list")
    normalized_gaps = []
    for index, gap in enumerate(gaps):
        normalized_gaps.append(
            _enum_value(gap, CoverageGap, f"known_coverage_gaps[{index}]")
        )
    if len(normalized_gaps) != len(set(normalized_gaps)):
        raise ValueError("known_coverage_gaps must not contain duplicates")
    return {
        "record_type": "telemetry_health.v1",
        "project_id": _uuid(record["project_id"], "project_id"),
        "connection_state": _enum_value(
            record["connection_state"], ConnectionState, "connection_state"
        ),
        "schema_state": _enum_value(
            record["schema_state"], SchemaState, "schema_state"
        ),
        "ledger_state": _enum_value(
            record["ledger_state"], LedgerState, "ledger_state"
        ),
        "queue_state": _enum_value(record["queue_state"], QueueState, "queue_state"),
        "disk_state": _enum_value(record["disk_state"], DiskState, "disk_state"),
        "publication_state": int(
            _int_enum(
                record["publication_state"], PublicationState, "publication_state"
            )
        ),
        "reason_counts": reason_counts,
        "unassigned_count": normalize_nonnegative_integer(
            record["unassigned_count"], "unassigned_count"
        ),
        "conflict_count": normalize_nonnegative_integer(
            record["conflict_count"], "conflict_count"
        ),
        "rejected_count": normalize_nonnegative_integer(
            record["rejected_count"], "rejected_count"
        ),
        "queue_depth": normalize_nonnegative_integer(
            record["queue_depth"], "queue_depth"
        ),
        "ledger_bytes": normalize_nonnegative_integer(
            record["ledger_bytes"], "ledger_bytes"
        ),
        "last_event_timestamp_seconds": _nullable_number(
            record["last_event_timestamp_seconds"], "last_event_timestamp_seconds"
        ),
        "known_coverage_gaps": sorted(normalized_gaps),
    }
