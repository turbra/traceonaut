"""Read-only Prometheus presentation and stored-sample publication confirmation.

The HTTP path serves a pre-rendered snapshot. Collection, disk access, and
Prometheus queries run outside the scrape path and outside supervisor control.
"""

from __future__ import annotations

from dataclasses import dataclass
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
import threading
import time
from typing import Any, Callable, Mapping
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .observability_contract import (
    COMPONENT_STATES,
    CoverageState,
    DispatchField,
    FieldState,
    LIFECYCLE_CODES,
    METRIC_FAMILIES,
    SAFE_INTEGER_MAX,
    TERMINAL_LIFECYCLE_STATES,
    TokenKind,
    calculate_declared_allowance,
    numeric_for_export,
)


def _label(value: Any) -> str:
    text = "unknown" if value is None else str(value)
    if len(text) > 128 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:+/-]*", text):
        raise ValueError("invalid metric label")
    return text


@dataclass(frozen=True)
class Sample:
    name: str
    labels: tuple[tuple[str, str], ...]
    value: int | float

    def manifest(self, timestamp: float) -> dict[str, Any]:
        return {
            "name": self.name,
            "labels": dict(self.labels),
            "value": self.value,
            "timestamp_seconds": timestamp,
        }


def _sample(name: str, value: int | float, labels: Mapping[str, Any]) -> Sample:
    family = METRIC_FAMILIES[name]
    if set(labels) != set(family.labels):
        raise ValueError("metric label set mismatch")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid metric value")
    if value < 0 or (isinstance(value, float) and not math.isfinite(value)):
        raise ValueError("invalid metric value")
    if isinstance(value, int) and value > SAFE_INTEGER_MAX:
        raise ValueError("metric integer exceeds exact export range")
    return Sample(
        name, tuple((key, _label(labels[key])) for key in family.labels), value
    )


def dispatch_samples(
    dispatch: Mapping[str, Any],
    *,
    clock_epoch_id: str | None = None,
    monotonic_seconds: float | None = None,
) -> list[Sample]:
    """Render one committed dispatch; omit unavailable numeric observations."""
    observation = dispatch["observation"]
    aggregate = dispatch["aggregate"]
    labels = {key: observation[key] for key in ("project_id", "dispatch_id")}
    samples: list[Sample] = []

    def emit(name: str, value: Any, **extra: Any) -> None:
        if value is not None:
            samples.append(_sample(name, value, {**labels, **extra}))

    emit(
        "cwo_dispatch_info",
        1,
        **{
            key: observation[key]
            for key in ("agent_id", "packet_ref", "requested_model", "requested_effort")
        },
    )
    emit("cwo_dispatch_snapshot_revision", dispatch["snapshot_revision"])
    emit("cwo_dispatch_state", LIFECYCLE_CODES[observation["lifecycle_state"]])
    emit("cwo_dispatch_completed_cycles_total", aggregate["completed_cycles"])
    coverage = aggregate["coverage_state"]
    emit("cwo_dispatch_coverage_state", coverage)
    binding = dispatch.get("binding") or {}
    fields = {field.value: int(FieldState.UNAVAILABLE) for field in DispatchField}
    for field in ("model", "effort"):
        value = binding.get("configured_" + field)
        if value not in (None, "unknown"):
            emit(
                "cwo_dispatch_configured_" + field + "_info",
                1,
                **{"configured_" + field: value},
            )
            fields["configured_" + field] = int(FieldState.PRESENT)
        elif value == "unknown":
            fields["configured_" + field] = int(FieldState.UNQUALIFIED)

    for kind in TokenKind:
        key = kind.value
        value = aggregate["token_sums"].get(key)
        state = aggregate["token_states"].get(key, int(FieldState.UNAVAILABLE))
        value, state = numeric_for_export(value, state=state)
        emit("cwo_dispatch_observed_tokens", value, token_kind=key)
        emit("cwo_dispatch_token_state", int(state), token_kind=key)
        emit(
            "cwo_dispatch_token_cycles",
            aggregate["token_cycles"].get(key, 0),
            token_kind=key,
        )

    for cycle in dispatch["cycles"]:
        ordinal = cycle["cycle_ordinal"]
        emit("cwo_cycle_present", 1, cycle_ordinal=ordinal)
        emit(
            "cwo_cycle_observed_timestamp_seconds",
            cycle["observed_at_seconds"],
            cycle_ordinal=ordinal,
        )
        for kind in TokenKind:
            entry = (cycle["usage"] or {}).get(
                kind.value, {"value": None, "state": int(FieldState.UNAVAILABLE)}
            )
            value, state = numeric_for_export(entry["value"], state=entry["state"])
            emit(
                "cwo_cycle_tokens", value, cycle_ordinal=ordinal, token_kind=kind.value
            )
            emit(
                "cwo_cycle_token_state",
                int(state),
                cycle_ordinal=ordinal,
                token_kind=kind.value,
            )

    timing = observation["timing"]
    elapsed, elapsed_state = timing["elapsed_seconds"], timing["elapsed_state"]
    if observation["lifecycle_state"] not in TERMINAL_LIFECYCLE_STATES:
        submitted = timing["submitted_seconds"]
        if (
            clock_epoch_id is not None
            and timing["clock_epoch_id"] == clock_epoch_id
            and submitted is not None
            and monotonic_seconds is not None
            and monotonic_seconds >= submitted
        ):
            elapsed, elapsed_state = monotonic_seconds - submitted, int(
                FieldState.PRESENT
            )
        else:
            elapsed = None
            elapsed_state = int(
                FieldState.CLOCK_GAP
                if submitted is not None
                else FieldState.UNAVAILABLE
            )
    if isinstance(elapsed, int) and elapsed > SAFE_INTEGER_MAX:
        elapsed, elapsed_state = None, int(FieldState.OVERFLOW)
    fields["dispatch_elapsed"] = elapsed_state
    if elapsed_state == int(FieldState.PRESENT):
        emit("cwo_dispatch_elapsed_seconds", elapsed)
    else:
        elapsed = None

    for field, unit, observed, allowance_coverage in (
        ("cycle", "", aggregate["completed_cycles"], coverage),
        ("elapsed", "_seconds", elapsed, int(CoverageState.OBSERVED_NO_KNOWN_GAP)),
    ):
        key = "declared_" + field + "_allowance" + unit
        allowance = observation[key]
        field_key = "declared_" + field + "_allowance"
        if allowance is None:
            fields[field_key] = int(FieldState.UNAVAILABLE)
            continue
        if isinstance(allowance, int) and allowance > SAFE_INTEGER_MAX:
            fields[field_key] = int(FieldState.OVERFLOW)
            continue
        fields[field_key] = int(FieldState.PRESENT)
        emit("cwo_dispatch_" + key, allowance)
        calculation = calculate_declared_allowance(
            allowance, observed, coverage=allowance_coverage
        )
        # There is no lower-bound metric in the frozen v1 inventory. Do not
        # export a lower bound under the exact-overrun family name.
        if not calculation.lower_bound_overrun:
            emit(
                "cwo_dispatch_declared_" + field + "_remaining" + unit,
                calculation.remaining,
            )
            emit(
                "cwo_dispatch_declared_" + field + "_overrun" + unit,
                calculation.overrun,
            )
    for field in ("enforced_tool_call_limit", "enforced_runtime_limit_seconds"):
        value = observation[field]
        if not (isinstance(value, int) and value > SAFE_INTEGER_MAX):
            emit("cwo_dispatch_" + field, value)
    for field, state in fields.items():
        emit("cwo_dispatch_field_state", state, field=field)

    activity = dispatch.get("activity") or {}
    emit("cwo_dispatch_retry_notices_total", activity.get("retry_notices"))
    for outcome, count in (activity.get("turn_outcomes") or {}).items():
        if outcome not in ("completed", "failed", "interrupted"):
            raise ValueError("invalid turn outcome")
        emit("cwo_dispatch_turn_outcomes_total", count, outcome=outcome)
    for event in activity.get("tool_events") or []:
        if event["category"] not in (
            "command",
            "file_change",
            "dynamic",
            "mcp",
            "web",
            "other",
        ):
            raise ValueError("invalid tool category")
        if event["lifecycle"] not in ("started", "completed", "failed"):
            raise ValueError("invalid tool lifecycle")
        emit(
            "cwo_dispatch_tool_events_total",
            event["count"],
            category=event["category"],
            lifecycle=event["lifecycle"],
        )
    return samples


def _agent_states(dispatches: list[Mapping[str, Any]]) -> dict[tuple[str, str], Sample]:
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for dispatch in dispatches:
        observation = dispatch["observation"]
        groups.setdefault(
            (observation["project_id"], observation["agent_id"]), []
        ).append(observation)
    result = {}
    for (project, agent), observations in groups.items():
        states = {row["lifecycle_state"] for row in observations}
        active = states.intersection({"submitted", "acknowledged", "running"})
        if active:
            state = max(active, key=lambda value: LIFECYCLE_CODES[value])
        elif len(states) == 1:
            state = next(iter(states))
        else:
            # Terminal receipts from unrelated clock epochs do not establish
            # an ordering. The agent is terminal, with unknown final state.
            state = "unknown"
        result[(project, agent)] = _sample(
            "cwo_agent_state",
            LIFECYCLE_CODES[state],
            {"project_id": project, "agent_id": agent},
        )
    return result


def build_samples(
    snapshot: Mapping[str, Any], **clock: Any
) -> tuple[list[Sample], dict[str, list[Sample]]]:
    samples: list[Sample] = []
    manifests: dict[str, list[Sample]] = {}
    for project in snapshot["projects"]:
        registration, health = project["registration"], project["health"]
        if registration["state"] != "enabled":
            continue
        project_id = registration["project_id"]
        labels = {"project_id": project_id}
        dispatches = project["dispatches"]
        agents = _agent_states(dispatches)
        visible_agents = set()
        pending = 0
        for dispatch in dispatches:
            observation = dispatch["observation"]
            terminal = observation["lifecycle_state"] in TERMINAL_LIFECYCLE_STATES
            publication = dispatch["publication"]
            if (
                terminal
                and publication["latest_confirmed_revision"]
                == dispatch["snapshot_revision"]
            ):
                continue
            emitted = dispatch_samples(dispatch, **clock)
            samples.extend(emitted)
            agent_key = (project_id, observation["agent_id"])
            visible_agents.add(agent_key)
            if terminal:
                pending += 1
                manifests[observation["dispatch_id"]] = emitted + [agents[agent_key]]
        samples.extend(agents[key] for key in sorted(visible_agents))
        for component, states in COMPONENT_STATES.items():
            for state in states:
                samples.append(
                    _sample(
                        "cwo_telemetry_component_state",
                        int(health[component.value + "_state"] == state),
                        {**labels, "component": component.value, "state": state},
                    )
                )
        for field in (
            "last_event_timestamp_seconds",
            "queue_depth",
            "ledger_bytes",
            "publication_state",
        ):
            if health.get(field) is not None:
                samples.append(_sample("cwo_telemetry_" + field, health[field], labels))
        samples.append(
            _sample("cwo_telemetry_publication_pending_dispatches", pending, labels)
        )
        for disposition, count in project.get("event_counts", {}).items():
            if disposition not in (
                "accepted",
                "duplicate",
                "unassigned",
                "conflict",
                "rejected",
                "lost",
            ):
                raise ValueError("invalid telemetry disposition")
            samples.append(
                _sample(
                    "cwo_telemetry_events_total",
                    count,
                    {**labels, "disposition": disposition},
                )
            )
    return samples, manifests


def render_prometheus(samples: list[Sample]) -> bytes:
    lines: list[str] = []
    seen: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
    for name in sorted({sample.name for sample in samples}):
        lines.append(f"# TYPE {name} {METRIC_FAMILIES[name].metric_type}")
        for sample in sorted(
            (s for s in samples if s.name == name), key=lambda s: s.labels
        ):
            identity = (sample.name, sample.labels)
            if identity in seen:
                raise ValueError("duplicate exported series")
            seen.add(identity)
            labels = ",".join(f'{key}="{value}"' for key, value in sample.labels)
            lines.append(f"{name}{{{labels}}} {sample.value}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def validate_credential_parent(path: Path) -> Path:
    """Validate the trusted directory chain before reading or creating a token."""
    path = path.absolute()
    for parent in reversed(path.parents):
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("unsafe credential ancestor")
        if info.st_uid not in (0, os.geteuid()):
            raise ValueError("untrusted credential ancestor")
        writable = info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        if writable and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX):
            raise ValueError("writable credential ancestor")
    return path


def read_credential(path: Path) -> bytes:
    """Read one owner-only regular file, rejecting symlinks and unsafe parents."""
    path = validate_credential_parent(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ValueError("credential must be owner-only regular file")
        value = os.read(fd, 4097).strip()
        if not 16 <= len(value) <= 4096 or any(
            byte < 33 or byte > 126 for byte in value
        ):
            raise ValueError("invalid credential encoding or length")
        return value
    finally:
        os.close(fd)


def metrics_bind_address(
    host: str, *, allow_remote: bool = False
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Validate an explicit bind address without resolving names or opening a socket."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError("metrics host must be a numeric IP address") from None
    if not address.is_loopback and not allow_remote:
        raise ValueError("metrics endpoint must bind loopback")
    effective = address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) else None
    effective = effective if effective is not None else address
    if effective.is_unspecified or effective.is_multicast or str(effective) == "255.255.255.255":
        raise ValueError("metrics host must be a specific unicast address, not a wildcard or broadcast")
    return address


class MetricsEndpoint:
    """Authenticated endpoint, loopback by default; updates stay off the scrape path."""

    def __init__(
        self, host: str, port: int, credential: bytes, *, allow_remote: bool = False
    ) -> None:
        address = metrics_bind_address(host, allow_remote=allow_remote)
        if not 16 <= len(credential) <= 4096:
            raise ValueError("invalid credential length")
        self._payload: bytes | None = None
        self._lock = threading.Lock()
        endpoint = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self) -> None:
                super().setup()
                self.connection.settimeout(2)

            def log_message(self, *_args: Any) -> None:
                pass

            def do_GET(self) -> None:
                supplied = self.headers.get("Authorization", "").encode("utf-8")
                if not hmac.compare_digest(supplied, b"Bearer " + credential):
                    self.send_error(401, "Unauthorized")
                    return
                if self.path != "/metrics":
                    self.send_error(404, "Not Found")
                    return
                with endpoint._lock:
                    payload = endpoint._payload
                if payload is None:
                    self.send_error(503, "Telemetry unavailable")
                    return
                self.send_response(200)
                self.send_header(
                    "Content-Type", "text/plain; version=0.0.4; charset=utf-8"
                )
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)

        class Server(HTTPServer):
            address_family = socket.AF_INET6 if address.version == 6 else socket.AF_INET

            def handle_error(self, _request: Any, _client_address: Any) -> None:
                # Never print request data, credentials, paths, or exception text.
                pass

        self.server = Server((host, port), Handler)
        self._thread: threading.Thread | None = None

    def update(self, snapshot: Mapping[str, Any], **clock: Any) -> None:
        try:
            payload = render_prometheus(build_samples(snapshot, **clock)[0])
        except (KeyError, TypeError, ValueError, OverflowError):
            with self._lock:
                self._payload = None
            raise ValueError("invalid telemetry snapshot") from None
        with self._lock:
            self._payload = payload

    def invalidate(self) -> None:
        with self._lock:
            self._payload = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        if self._thread is not None:
            self.server.shutdown()
            self._thread.join(timeout=3)
        self.server.server_close()


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class PrometheusQueryClient:
    """Bounded read-only local query client; it never inherits proxy settings."""

    def __init__(
        self,
        base_url: str,
        *,
        credential: bytes | None = None,
        timeout_seconds: float = 3,
        max_response_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        url = urlsplit(base_url)
        if (
            url.scheme not in ("http", "https")
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in ("", "/")
        ):
            raise ValueError("invalid Prometheus base URL")
        try:
            is_local = ipaddress.ip_address(url.hostname or "").is_loopback
        except ValueError:
            is_local = url.hostname == "localhost"
        if not is_local:
            raise ValueError("v1 query client requires a reviewed local endpoint")
        if (
            not 0 < timeout_seconds <= 5
            or not 0 < max_response_bytes <= 16 * 1024 * 1024
        ):
            raise ValueError("query resource bound invalid")
        self.base_url = base_url.rstrip("/")
        self.credential = credential
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.opener = build_opener(ProxyHandler({}), _NoRedirect())

    def query(self, expression: str, at_seconds: float) -> list[dict[str, Any]]:
        data = urlencode({"query": expression, "time": repr(at_seconds)}).encode(
            "ascii"
        )
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if self.credential:
            headers["Authorization"] = "Bearer " + self.credential.decode("ascii")
        request = Request(self.base_url + "/api/v1/query", data=data, headers=headers)
        try:
            with self.opener.open(request, timeout=self.timeout_seconds) as response:
                raw = response.read(self.max_response_bytes + 1)
            if len(raw) > self.max_response_bytes:
                raise ValueError("query response too large")
            result = json.loads(raw)
            if (
                result.get("status") != "success"
                or result.get("warnings")
                or result.get("data", {}).get("resultType") != "vector"
            ):
                raise ValueError("query response incompatible")
            vector = result["data"]["result"]
            if not isinstance(vector, list):
                raise ValueError("query vector invalid")
            return vector
        except Exception:
            raise ValueError("Prometheus query unavailable") from None


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


class PublicationConfirmer:
    """Confirm a complete exact terminal revision from stored Prometheus samples."""

    def __init__(
        self,
        ledger: Any,
        client: Any,
        *,
        job: str,
        instance: str,
        batch_size: int = 4,
        freshness_seconds: float = 30,
    ) -> None:
        if not job or not instance or len(job) > 128 or len(instance) > 256:
            raise ValueError("bounded scrape identity required")
        if not 1 <= batch_size <= 32 or not 5 <= freshness_seconds <= 300:
            raise ValueError("publication bound invalid")
        self.ledger, self.client = ledger, client
        self.job, self.instance = job, instance
        self.batch_size, self.freshness_seconds = batch_size, freshness_seconds
        self._cursor = 0

    def _read(self, expected: list[Sample], at: float) -> float | None:
        label_map = dict(
            next(
                sample.labels
                for sample in expected
                if sample.name == "cwo_dispatch_snapshot_revision"
            )
        )
        agent = next(s for s in expected if s.name == "cwo_agent_state")
        names = "|".join(
            sorted({s.name for s in expected if s.name != "cwo_agent_state"})
        )
        bounds = f"job={_quote(self.job)},instance={_quote(self.instance)}"
        selector = (
            "{__name__=~"
            + _quote(names)
            + ",project_id="
            + _quote(label_map["project_id"])
            + ",dispatch_id="
            + _quote(label_map["dispatch_id"])
            + ","
            + bounds
            + "} or "
            + "cwo_agent_state{project_id="
            + _quote(label_map["project_id"])
            + ",agent_id="
            + _quote(dict(agent.labels)["agent_id"])
            + ","
            + bounds
            + "}"
        )
        values = self.client.query(selector, at)
        # Prometheus instant vector result timestamps are evaluation times.
        # timestamp() values carry the original stored sample timestamps.
        # Preserve the metric name before timestamp() removes __name__.
        times = self.client.query(
            "timestamp(label_replace(("
            + selector
            + '), "cwo_metric_name", "$1", "__name__", "(.+)"))',
            at,
        )
        wanted = {(s.name, tuple(sorted(s.labels))): s.value for s in expected}

        def parse(rows: list[dict[str, Any]], timestamp: bool) -> dict[Any, float]:
            parsed: dict[Any, float] = {}
            for row in rows:
                labels = dict(row["metric"])
                if (
                    labels.pop("job", None) != self.job
                    or labels.pop("instance", None) != self.instance
                ):
                    raise ValueError("unbounded query result")
                name = labels.pop("cwo_metric_name" if timestamp else "__name__", None)
                if timestamp:
                    labels.pop("__name__", None)
                key = (name, tuple(sorted(labels.items())))
                value = float(row["value"][1])
                if key in parsed or not math.isfinite(value):
                    raise ValueError("ambiguous query result")
                parsed[key] = value
            return parsed

        observed, original_times = parse(values, False), parse(times, True)
        if set(observed) != set(wanted) or set(original_times) != set(wanted):
            return None
        if any(observed[key] != value for key, value in wanted.items()):
            return None
        unique_times = set(original_times.values())
        if len(unique_times) != 1:
            return None
        original = unique_times.pop()
        if not at - self.freshness_seconds <= original <= at:
            return None
        return original

    def run_once(self, *, now_seconds: float | None = None) -> dict[str, int]:
        at = time.time() if now_seconds is None else now_seconds
        snapshot = self.ledger.snapshot()
        _, manifests = build_samples(snapshot)
        pending = {
            record["dispatch_id"]: record
            for record in self.ledger.pending_publication_manifests()
        }
        project_states = {
            p["registration"]["project_id"]: 1
            for p in snapshot["projects"]
            if p["registration"]["state"] == "enabled"
        }
        identifiers = sorted(manifests)
        counts = {"confirmed": 0, "mismatch": 0, "unavailable": 0}
        if not identifiers:
            for project_id in project_states:
                self.ledger.set_publication_state(project_id, 1)
            return counts
        start = self._cursor % len(identifiers)
        selected = (identifiers[start:] + identifiers[:start])[: self.batch_size]
        self._cursor += len(selected)
        for dispatch_id in selected:
            samples = manifests[dispatch_id]
            project_id = dict(samples[0].labels)["project_id"]
            revision = next(
                s.value for s in samples if s.name == "cwo_dispatch_snapshot_revision"
            )
            try:
                staged = pending.get(dispatch_id)
                if staged is not None and staged["revision"] == revision:
                    samples = [
                        _sample(row["name"], row["value"], row["labels"])
                        for row in staged["samples"]
                    ]
                    original = self._read(samples, staged["sample_timestamp_seconds"])
                else:
                    staged = None
                    original = self._read(samples, at)
                if original is None:
                    counts["mismatch"] += 1
                    project_states[project_id] = max(project_states[project_id], 3)
                    continue
                if staged is not None:
                    if original != staged["sample_timestamp_seconds"]:
                        counts["mismatch"] += 1
                        project_states[project_id] = max(project_states[project_id], 3)
                        continue
                    manifest = staged["manifest_sha256"]
                else:
                    manifest = self.ledger.stage_publication_manifest(
                        dispatch_id,
                        revision=revision,
                        sample_timestamp_seconds=original,
                        samples=[sample.manifest(original) for sample in samples],
                    )
                accepted = self.ledger.confirm_publication(
                    dispatch_id,
                    revision=revision,
                    manifest_sha256=(
                        manifest
                        if isinstance(manifest, str)
                        else manifest["manifest_sha256"]
                    ),
                    sample_timestamp_seconds=original,
                    confirmed_at_seconds=at,
                )
                counts["confirmed" if accepted is not False else "mismatch"] += 1
                if accepted is False:
                    project_states[project_id] = max(project_states[project_id], 3)
            except Exception:
                # No transport response, exception text, or credential is logged.
                # The unconfirmed revision remains exposed for a later attempt.
                counts["unavailable"] += 1
                project_states[project_id] = max(project_states[project_id], 2)
        for project_id, state in project_states.items():
            self.ledger.set_publication_state(project_id, state)
        return counts


class ObservabilityExportService:
    """Embed alongside the owned adapter, sharing its serialized ledger writer."""

    def __init__(
        self,
        snapshot: Callable[[], Mapping[str, Any]],
        endpoint: MetricsEndpoint,
        *,
        confirmer: PublicationConfirmer | None = None,
        clock_epoch_id: str | None = None,
    ) -> None:
        self.snapshot, self.endpoint = snapshot, endpoint
        self.confirmer, self.clock_epoch_id = confirmer, clock_epoch_id
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def _refresh(self) -> None:
        while not self._stop.is_set():
            try:
                self.endpoint.update(
                    self.snapshot(),
                    clock_epoch_id=self.clock_epoch_id,
                    monotonic_seconds=time.monotonic(),
                )
            except Exception:
                self.endpoint.invalidate()
            self._stop.wait(1)

    def _confirm(self) -> None:
        while not self._stop.wait(5):
            try:
                self.confirmer.run_once()
            except Exception:
                pass

    def start(self) -> None:
        self.endpoint.update(
            self.snapshot(),
            clock_epoch_id=self.clock_epoch_id,
            monotonic_seconds=time.monotonic(),
        )
        self.endpoint.start()
        for target in (
            (self._refresh, self._confirm) if self.confirmer else (self._refresh,)
        ):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self._threads.append(thread)

    def close(self) -> None:
        self._stop.set()
        self.endpoint.close()
        for thread in self._threads:
            thread.join(timeout=0.1)
