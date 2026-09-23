from __future__ import annotations

import hashlib
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import socket
import threading
import time
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs


ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / "scripts"))

from traceonaut.observability_contract import (  # noqa: E402
    CoverageState,
    FieldState,
    LIFECYCLE_CODES,
    SAFE_INTEGER_MAX,
    TokenKind,
    normalize_token_usage,
)
from traceonaut.observability_exporter import (  # noqa: E402
    MetricsEndpoint,
    ObservabilityExportService,
    PrometheusQueryClient,
    PublicationConfirmer,
    Sample,
    build_samples,
    dispatch_samples,
    metrics_bind_address,
    read_credential,
)


PROJECT_ID = "11111111-1111-4111-8111-111111111111"
DISPATCH_ID = "22222222-2222-4222-8222-222222222222"
AGENT_ID = "33333333-3333-4333-8333-333333333333"
CLOCK_EPOCH_ID = "44444444-4444-4444-8444-444444444444"


def uuid_for(number: int) -> str:
    return f"{number:08x}-0000-4000-8000-{number:012x}"


def sample_labels(sample: Sample) -> dict[str, str]:
    return dict(sample.labels)


def selected(samples: list[Sample], name: str, **required_labels: str) -> list[Sample]:
    return [
        sample
        for sample in samples
        if sample.name == name
        and all(
            sample_labels(sample).get(key) == value
            for key, value in required_labels.items()
        )
    ]


def cycle(
    ordinal: int,
    usage: dict[str, dict[str, object]] | None,
) -> dict[str, object]:
    return {
        "cycle_ordinal": ordinal,
        "usage": usage,
        "observed_at_seconds": 1_789_000_000 + ordinal,
    }


def aggregate_for(cycles: list[dict[str, object]]) -> dict[str, object]:
    sums = {kind.value: 0 for kind in TokenKind}
    counts = {kind.value: 0 for kind in TokenKind}
    states: dict[str, int] = {}
    for kind in TokenKind:
        present_states: list[int] = []
        for item in cycles:
            usage = item["usage"]
            if usage is None:
                present_states.append(int(FieldState.UNAVAILABLE))
                continue
            entry = usage[kind.value]
            state = int(entry["state"])
            present_states.append(state)
            if state in (int(FieldState.PRESENT), int(FieldState.RUNTIME_NORMALIZED)):
                sums[kind.value] += int(entry["value"])
                counts[kind.value] += 1
        if not present_states or all(
            state == int(FieldState.UNAVAILABLE) for state in present_states
        ):
            states[kind.value] = int(FieldState.UNAVAILABLE)
        elif all(state == int(FieldState.INVALID) for state in present_states):
            states[kind.value] = int(FieldState.INVALID)
        elif counts[kind.value] != len(cycles):
            states[kind.value] = int(FieldState.PARTIAL)
        elif kind in {TokenKind.INPUT, TokenKind.OUTPUT, TokenKind.TOTAL}:
            states[kind.value] = int(FieldState.PRESENT)
        else:
            states[kind.value] = int(FieldState.RUNTIME_NORMALIZED)
    return {
        "completed_cycles": len(cycles),
        "token_sums": sums,
        "token_cycles": counts,
        "token_states": states,
        "conflict_count": 0,
        "coverage_state": int(CoverageState.OBSERVED_NO_KNOWN_GAP),
    }


def dispatch(
    *,
    dispatch_id: str = DISPATCH_ID,
    agent_id: str = AGENT_ID,
    lifecycle: str = "completed",
    revision: int = 7,
    latest_confirmed_revision: int | None = None,
    cycles: list[dict[str, object]] | None = None,
    elapsed_seconds: int | float | None = 410,
    elapsed_state: int = int(FieldState.PRESENT),
    coverage: CoverageState = CoverageState.OBSERVED_NO_KNOWN_GAP,
    configured_model: str | None = "gpt-5.6-sol",
    configured_effort: str | None = "xhigh",
) -> dict[str, object]:
    cycle_records = cycles or []
    aggregate = aggregate_for(cycle_records)
    aggregate["coverage_state"] = int(coverage)
    return {
        "observation": {
            "record_type": "dispatch_observation.v1",
            "project_id": PROJECT_ID,
            "dispatch_id": dispatch_id,
            "agent_id": agent_id,
            "packet_ref": f"packet-{dispatch_id[:8]}",
            "packet_sha256": hashlib.sha256(dispatch_id.encode()).hexdigest(),
            "supervisor_submission_ref": f"submission-{dispatch_id[:8]}",
            "requested_executor": "native_worker",
            "requested_model": "gpt-5.6-sol",
            "requested_effort": "xhigh",
            "declared_cycle_allowance": 14,
            "declared_elapsed_allowance_seconds": 400,
            "enforced_tool_call_limit": 40,
            "enforced_runtime_limit_seconds": 900,
            "lifecycle_state": lifecycle,
            "timing": {
                "clock_source": "supervisor-monotonic",
                "clock_epoch_id": CLOCK_EPOCH_ID,
                "provenance": "supervisor-lifecycle-receipt",
                "submitted_seconds": 100,
                "acknowledged_seconds": 101,
                "running_seconds": 102,
                "terminal_seconds": (
                    510
                    if lifecycle not in {"submitted", "acknowledged", "running"}
                    else None
                ),
                "elapsed_seconds": elapsed_seconds,
                "elapsed_state": elapsed_state,
            },
            "coverage_state": int(coverage),
        },
        "snapshot_revision": revision,
        "binding": {
            "configured_model": configured_model,
            "configured_effort": configured_effort,
        },
        "bindings": [],
        "aggregate": aggregate,
        "activity": {
            "retry_notices": 0,
            "turn_outcomes": {"completed": 0, "failed": 0, "interrupted": 0},
            "tool_events": [],
        },
        "cycles": cycle_records,
        "publication": {
            "current_revision": revision,
            "latest_confirmed_revision": latest_confirmed_revision,
            "pending": latest_confirmed_revision != revision,
            "staged_manifest_sha256": None,
            "staged_sample_timestamp_seconds": None,
        },
    }


def snapshot(dispatches: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "snapshot_revision": 20,
        "projects": [
            {
                "registration": {
                    "record_type": "project_registration.v1",
                    "project_id": PROJECT_ID,
                    "state": "enabled",
                },
                "health": {
                    "connection_state": "connected",
                    "schema_state": "compatible",
                    "ledger_state": "healthy",
                    "queue_state": "healthy",
                    "disk_state": "healthy",
                    "publication_state": 1,
                    "last_event_timestamp_seconds": 1_789_000_020,
                    "queue_depth": 0,
                    "ledger_bytes": 4096,
                },
                "event_counts": {
                    "accepted": 0,
                    "duplicate": 0,
                    "unassigned": 0,
                    "conflict": 0,
                    "rejected": 0,
                    "lost": 0,
                },
                "dispatches": dispatches,
            }
        ],
    }


class ExportAccountingTests(unittest.TestCase):
    def test_missing_usage_counts_cycle_and_exports_unknown_not_zero(self) -> None:
        samples = dispatch_samples(dispatch(cycles=[cycle(1, None)]))

        self.assertEqual(
            selected(samples, "cwo_dispatch_completed_cycles_total")[0].value, 1
        )
        self.assertEqual(selected(samples, "cwo_cycle_present")[0].value, 1)
        self.assertEqual(selected(samples, "cwo_cycle_tokens"), [])
        self.assertEqual(selected(samples, "cwo_dispatch_observed_tokens"), [])
        for kind in TokenKind:
            cycle_state = selected(
                samples,
                "cwo_cycle_token_state",
                token_kind=kind.value,
            )[0]
            dispatch_state = selected(
                samples,
                "cwo_dispatch_token_state",
                token_kind=kind.value,
            )[0]
            covered_cycles = selected(
                samples,
                "cwo_dispatch_token_cycles",
                token_kind=kind.value,
            )[0]
            self.assertEqual(cycle_state.value, int(FieldState.UNAVAILABLE))
            self.assertEqual(dispatch_state.value, int(FieldState.UNAVAILABLE))
            self.assertEqual(covered_cycles.value, 0)

    def test_runtime_normalized_zero_is_a_present_zero_with_state_two(self) -> None:
        usage = normalize_token_usage(
            {
                "input": 0,
                "cached_input": 0,
                "cache_write_input": 0,
                "output": 0,
                "reasoning_output": 0,
                "total": 0,
            }
        )
        assert usage is not None
        samples = dispatch_samples(dispatch(cycles=[cycle(1, usage)]))

        cached = selected(
            samples, "cwo_cycle_tokens", token_kind=TokenKind.CACHED_INPUT.value
        )[0]
        cached_state = selected(
            samples,
            "cwo_cycle_token_state",
            token_kind=TokenKind.CACHED_INPUT.value,
        )[0]
        core = selected(samples, "cwo_cycle_tokens", token_kind=TokenKind.INPUT.value)[
            0
        ]
        core_state = selected(
            samples,
            "cwo_cycle_token_state",
            token_kind=TokenKind.INPUT.value,
        )[0]

        self.assertEqual(cached.value, 0)
        self.assertEqual(cached_state.value, int(FieldState.RUNTIME_NORMALIZED))
        self.assertEqual(core.value, 0)
        self.assertEqual(core_state.value, int(FieldState.PRESENT))

    def test_large_exact_usage_is_omitted_only_at_export_with_overflow_state(
        self,
    ) -> None:
        exact = SAFE_INTEGER_MAX + 1
        usage = normalize_token_usage(
            {
                "input": exact,
                "cached_input": 0,
                "cache_write_input": 0,
                "output": 0,
                "reasoning_output": 0,
                "total": exact,
            }
        )
        assert usage is not None
        source = dispatch(cycles=[cycle(1, usage)])
        self.assertEqual(source["cycles"][0]["usage"]["input"]["value"], exact)

        samples = dispatch_samples(source)

        self.assertEqual(selected(samples, "cwo_cycle_tokens", token_kind="input"), [])
        self.assertEqual(
            selected(samples, "cwo_dispatch_observed_tokens", token_kind="input"),
            [],
        )
        self.assertEqual(
            selected(samples, "cwo_cycle_token_state", token_kind="input")[0].value,
            int(FieldState.OVERFLOW),
        )
        self.assertEqual(
            selected(samples, "cwo_dispatch_token_state", token_kind="input")[0].value,
            int(FieldState.OVERFLOW),
        )

    def test_declared_14_cycle_and_400_second_examples_are_exact(self) -> None:
        cycles = [cycle(index, None) for index in range(1, 16)]
        samples = dispatch_samples(dispatch(cycles=cycles))

        expected = {
            "cwo_dispatch_declared_cycle_allowance": 14,
            "cwo_dispatch_declared_cycle_remaining": 0,
            "cwo_dispatch_declared_cycle_overrun": 1,
            "cwo_dispatch_declared_elapsed_allowance_seconds": 400,
            "cwo_dispatch_declared_elapsed_remaining_seconds": 0,
            "cwo_dispatch_declared_elapsed_overrun_seconds": 10,
            "cwo_dispatch_enforced_tool_call_limit": 40,
            "cwo_dispatch_enforced_runtime_limit_seconds": 900,
        }
        for name, value in expected.items():
            with self.subTest(metric=name):
                self.assertEqual(selected(samples, name)[0].value, value)

    def test_latest_unavailable_numeric_state_omits_old_style_measurement(self) -> None:
        source = dispatch(
            cycles=[],
            elapsed_seconds=None,
            elapsed_state=int(FieldState.CLOCK_GAP),
            configured_model="unknown",
            configured_effort=None,
        )
        samples = dispatch_samples(source)

        self.assertEqual(selected(samples, "cwo_dispatch_elapsed_seconds"), [])
        self.assertEqual(
            selected(
                samples,
                "cwo_dispatch_field_state",
                field="dispatch_elapsed",
            )[0].value,
            int(FieldState.CLOCK_GAP),
        )
        self.assertEqual(selected(samples, "cwo_dispatch_configured_model_info"), [])
        self.assertEqual(selected(samples, "cwo_dispatch_configured_effort_info"), [])
        self.assertEqual(
            selected(
                samples,
                "cwo_dispatch_field_state",
                field="configured_model",
            )[0].value,
            int(FieldState.UNQUALIFIED),
        )
        self.assertEqual(
            selected(
                samples,
                "cwo_dispatch_field_state",
                field="configured_effort",
            )[0].value,
            int(FieldState.UNAVAILABLE),
        )


class SnapshotAndRetirementTests(unittest.TestCase):
    def test_agent_state_is_one_series_per_project_agent_not_per_dispatch(self) -> None:
        shared_agent = uuid_for(10)
        other_agent = uuid_for(11)
        dispatches = [
            dispatch(
                dispatch_id=uuid_for(101),
                agent_id=shared_agent,
                lifecycle="submitted",
                elapsed_seconds=None,
                elapsed_state=int(FieldState.UNAVAILABLE),
            ),
            dispatch(
                dispatch_id=uuid_for(102),
                agent_id=shared_agent,
                lifecycle="running",
                elapsed_seconds=12,
            ),
            dispatch(
                dispatch_id=uuid_for(103),
                agent_id=other_agent,
                lifecycle="failed",
            ),
        ]

        samples, _manifests = build_samples(
            snapshot(dispatches),
            clock_epoch_id=CLOCK_EPOCH_ID,
            monotonic_seconds=120,
        )
        agents = selected(samples, "cwo_agent_state")

        self.assertEqual(len(agents), 2)
        values = {sample_labels(item)["agent_id"]: item.value for item in agents}
        self.assertEqual(values[shared_agent], LIFECYCLE_CODES["running"])
        self.assertEqual(values[other_agent], LIFECYCLE_CODES["failed"])

    def test_terminal_series_retire_only_after_exact_current_revision(self) -> None:
        current_revision = 9
        exact = dispatch(
            dispatch_id=uuid_for(201),
            agent_id=uuid_for(21),
            revision=current_revision,
            latest_confirmed_revision=current_revision,
        )
        stale = dispatch(
            dispatch_id=uuid_for(202),
            agent_id=uuid_for(22),
            revision=current_revision,
            latest_confirmed_revision=current_revision - 1,
        )
        active = dispatch(
            dispatch_id=uuid_for(203),
            agent_id=uuid_for(23),
            lifecycle="running",
            revision=current_revision,
            latest_confirmed_revision=current_revision,
            elapsed_seconds=20,
        )

        samples, manifests = build_samples(
            snapshot([exact, stale, active]),
            clock_epoch_id=CLOCK_EPOCH_ID,
            monotonic_seconds=125,
        )

        exported_dispatches = {
            sample_labels(item)["dispatch_id"]
            for item in selected(samples, "cwo_dispatch_snapshot_revision")
        }
        self.assertNotIn(uuid_for(201), exported_dispatches)
        self.assertIn(uuid_for(202), exported_dispatches)
        self.assertIn(uuid_for(203), exported_dispatches)
        self.assertEqual(set(manifests), {uuid_for(202)})
        self.assertEqual(
            selected(samples, "cwo_telemetry_publication_pending_dispatches")[0].value,
            1,
        )


class FakeQueryClient:
    def __init__(
        self,
        expected: list[Sample],
        *,
        job: str,
        instance: str,
        original_timestamp: float,
    ) -> None:
        self.expected = expected
        self.job = job
        self.instance = instance
        self.original_timestamp = original_timestamp
        self.calls: list[tuple[str, float]] = []
        self.timestamp_offsets: dict[int, float] = {}
        self.override_job: str | None = None

    def query(self, expression: str, at_seconds: float) -> list[dict[str, object]]:
        self.calls.append((expression, at_seconds))
        timestamp_query = expression.startswith("timestamp(label_replace(")
        rows = []
        for index, sample in enumerate(self.expected):
            labels = sample_labels(sample)
            labels["job"] = self.override_job or self.job
            labels["instance"] = self.instance
            if timestamp_query:
                labels["cwo_metric_name"] = sample.name
                value = self.original_timestamp + self.timestamp_offsets.get(index, 0)
            else:
                labels["__name__"] = sample.name
                value = sample.value
            rows.append(
                {
                    "metric": labels,
                    "value": [at_seconds, str(value)],
                }
            )
        return rows


class FakePublicationLedger:
    def __init__(self, source: dict[str, object]) -> None:
        self.source = source
        self.pending: list[dict[str, object]] = []
        self.staged: list[dict[str, object]] = []
        self.confirmed: list[dict[str, object]] = []
        self.publication_states: list[tuple[str, int]] = []

    def snapshot(self) -> dict[str, object]:
        return self.source

    def pending_publication_manifests(self) -> list[dict[str, object]]:
        return self.pending

    def stage_publication_manifest(self, dispatch_id: str, **kwargs: object) -> str:
        self.staged.append({"dispatch_id": dispatch_id, **kwargs})
        return hashlib.sha256(b"manifest").hexdigest()

    def confirm_publication(self, dispatch_id: str, **kwargs: object) -> None:
        self.confirmed.append({"dispatch_id": dispatch_id, **kwargs})

    def set_publication_state(self, project_id: str, state: int) -> None:
        self.publication_states.append((project_id, state))


class PublicationConfirmationTests(unittest.TestCase):
    def terminal_manifest(self) -> tuple[dict[str, object], str, list[Sample]]:
        dispatch_id = uuid_for(301)
        source = snapshot(
            [
                dispatch(
                    dispatch_id=dispatch_id,
                    agent_id=uuid_for(31),
                    revision=12,
                    latest_confirmed_revision=11,
                )
            ]
        )
        _samples, manifests = build_samples(source)
        return source, dispatch_id, manifests[dispatch_id]

    def test_query_uses_exact_job_instance_and_original_common_timestamp(self) -> None:
        _source, _dispatch_id, manifest = self.terminal_manifest()
        client = FakeQueryClient(
            manifest,
            job="cwo-job",
            instance="127.0.0.1:9468",
            original_timestamp=995,
        )
        confirmer = PublicationConfirmer(
            object(),
            client,
            job="cwo-job",
            instance="127.0.0.1:9468",
            freshness_seconds=30,
        )

        observed = confirmer._read(manifest, 1000)

        self.assertEqual(observed, 995)
        self.assertEqual(len(client.calls), 2)
        value_query, timestamp_query = (call[0] for call in client.calls)
        for expression in (value_query, timestamp_query):
            self.assertIn('job="cwo-job"', expression)
            self.assertIn('instance="127.0.0.1:9468"', expression)
        self.assertTrue(timestamp_query.startswith("timestamp(label_replace("))
        self.assertIn('"cwo_metric_name", "$1", "__name__", "(.+)"', timestamp_query)

    def test_query_rejects_mixed_original_timestamps_and_wrong_scrape_identity(
        self,
    ) -> None:
        _source, _dispatch_id, manifest = self.terminal_manifest()
        client = FakeQueryClient(
            manifest,
            job="cwo-job",
            instance="127.0.0.1:9468",
            original_timestamp=995,
        )
        confirmer = PublicationConfirmer(
            object(),
            client,
            job="cwo-job",
            instance="127.0.0.1:9468",
            freshness_seconds=30,
        )

        client.timestamp_offsets[0] = -1
        self.assertIsNone(confirmer._read(manifest, 1000))
        client.timestamp_offsets.clear()
        client.override_job = "other-job"
        with self.assertRaisesRegex(ValueError, "unbounded query result"):
            confirmer._read(manifest, 1000)

    def test_run_once_stages_sha_string_and_confirms_exact_revision(self) -> None:
        source, dispatch_id, manifest = self.terminal_manifest()
        ledger = FakePublicationLedger(source)
        client = FakeQueryClient(
            manifest,
            job="cwo-job",
            instance="127.0.0.1:9468",
            original_timestamp=995,
        )
        confirmer = PublicationConfirmer(
            ledger,
            client,
            job="cwo-job",
            instance="127.0.0.1:9468",
            freshness_seconds=30,
        )

        result = confirmer.run_once(now_seconds=1000)

        self.assertEqual(result, {"confirmed": 1, "mismatch": 0, "unavailable": 0})
        self.assertEqual(ledger.staged[0]["dispatch_id"], dispatch_id)
        self.assertEqual(ledger.staged[0]["revision"], 12)
        self.assertEqual(ledger.staged[0]["sample_timestamp_seconds"], 995)
        self.assertEqual(ledger.confirmed[0]["dispatch_id"], dispatch_id)
        self.assertEqual(ledger.confirmed[0]["revision"], 12)
        self.assertEqual(
            ledger.confirmed[0]["manifest_sha256"],
            hashlib.sha256(b"manifest").hexdigest(),
        )
        self.assertEqual(ledger.publication_states, [(PROJECT_ID, 1)])

    def test_run_once_resumes_staged_manifest_at_original_timestamp(self) -> None:
        source, dispatch_id, manifest = self.terminal_manifest()
        original_timestamp = 995.0
        manifest_sha256 = hashlib.sha256(b"persisted-manifest").hexdigest()
        ledger = FakePublicationLedger(source)
        ledger.pending.append(
            {
                "project_id": PROJECT_ID,
                "dispatch_id": dispatch_id,
                "revision": 12,
                "manifest_sha256": manifest_sha256,
                "sample_timestamp_seconds": original_timestamp,
                "samples": [sample.manifest(original_timestamp) for sample in manifest],
            }
        )
        client = FakeQueryClient(
            manifest,
            job="cwo-job",
            instance="127.0.0.1:9468",
            original_timestamp=original_timestamp,
        )
        confirmer = PublicationConfirmer(
            ledger,
            client,
            job="cwo-job",
            instance="127.0.0.1:9468",
            freshness_seconds=30,
        )

        result = confirmer.run_once(now_seconds=1100)

        self.assertEqual(result, {"confirmed": 1, "mismatch": 0, "unavailable": 0})
        self.assertEqual(ledger.staged, [])
        self.assertEqual(
            {at_seconds for _expression, at_seconds in client.calls},
            {original_timestamp},
        )
        self.assertEqual(
            ledger.confirmed,
            [
                {
                    "dispatch_id": dispatch_id,
                    "revision": 12,
                    "manifest_sha256": manifest_sha256,
                    "sample_timestamp_seconds": original_timestamp,
                    "confirmed_at_seconds": 1100,
                }
            ],
        )
        self.assertEqual(len(ledger.pending), 1)
        self.assertEqual(ledger.pending[0]["revision"], 12)

    def test_project_mismatch_is_not_downgraded_by_later_query_failure(self) -> None:
        mismatch_id = uuid_for(401)
        unavailable_id = uuid_for(402)
        ledger = FakePublicationLedger(
            snapshot(
                [
                    dispatch(dispatch_id=mismatch_id),
                    dispatch(dispatch_id=unavailable_id),
                ]
            )
        )

        class MixedResultConfirmer(PublicationConfirmer):
            def _read(self, expected: list[Sample], at: float) -> float | None:
                dispatch_id = dict(expected[0].labels)["dispatch_id"]
                if dispatch_id == mismatch_id:
                    return None
                raise OSError("query unavailable")

        confirmer = MixedResultConfirmer(
            ledger,
            object(),
            job="cwo-job",
            instance="127.0.0.1:9468",
        )

        self.assertEqual(
            confirmer.run_once(now_seconds=1000),
            {"confirmed": 0, "mismatch": 1, "unavailable": 1},
        )
        self.assertEqual(ledger.publication_states, [(PROJECT_ID, 3)])


class EndpointAndClientTests(unittest.TestCase):
    def test_remote_bind_is_explicit_and_keeps_the_selected_address_family(self):
        credential = b"test-observability-token"
        for host, family in (("192.0.2.10", socket.AF_INET),
                             ("2001:db8::10", socket.AF_INET6),
                             ("::ffff:192.0.2.10", socket.AF_INET6)):
            with self.subTest(host=host):
                with self.assertRaisesRegex(ValueError, "must bind loopback"):
                    MetricsEndpoint(host, 0, credential)
                # Exercise construction without binding a real LAN interface.
                with patch.object(HTTPServer, "server_bind"), patch.object(HTTPServer, "server_activate"):
                    endpoint = MetricsEndpoint(host, 0, credential, allow_remote=True)
                    try:
                        self.assertEqual(endpoint.server.server_address, (host, 0))
                        self.assertEqual(endpoint.server.address_family, family)
                    finally:
                        endpoint.close()
        for host in ("127.0.0.1", "::1"):
            self.assertTrue(metrics_bind_address(host).is_loopback)

    def test_unsafe_remote_bind_is_rejected_before_opening_a_socket(self):
        hosts = ("", "localhost", "workstation.example", "not-an-ip", "0.0.0.0", "::",
                 "224.0.0.1", "ff02::1", "255.255.255.255", "::ffff:0.0.0.0",
                 "::ffff:224.0.0.1", "::ffff:255.255.255.255")
        for host in hosts:
            with self.subTest(host=host), patch.object(HTTPServer, "__init__") as server_init:
                with self.assertRaisesRegex(ValueError, "metrics host"):
                    MetricsEndpoint(host, 0, b"test-observability-token", allow_remote=True)
                server_init.assert_not_called()

    def test_remote_permission_does_not_bypass_authentication(self):
        credential = b"test-observability-token"
        endpoint = MetricsEndpoint("127.0.0.1", 0, credential, allow_remote=True)
        self.addCleanup(endpoint.close)
        endpoint.update(snapshot([dispatch()]))
        endpoint.start()
        for supplied in (None, b"incorrect-token-value"):
            status, body = self.request(endpoint, "GET", "/metrics", supplied)
            self.assertEqual(status, 401)
            self.assertNotIn(credential, body)
        self.assertEqual(self.request(endpoint, "GET", "/metrics", credential)[0], 200)

    @staticmethod
    def request(
        endpoint: MetricsEndpoint,
        method: str,
        path: str,
        credential: bytes | None,
    ) -> tuple[int, bytes]:
        host, port = endpoint.server.server_address[:2]
        connection = HTTPConnection(host, port, timeout=2)
        headers = {}
        if credential is not None:
            headers["Authorization"] = "Bearer " + credential.decode("ascii")
        connection.request(method, path, headers=headers)
        response = connection.getresponse()
        body = response.read()
        status = response.status
        connection.close()
        return status, body

    def test_metrics_endpoint_is_loopback_authenticated_read_only_and_redacted(
        self,
    ) -> None:
        credential = b"test-observability-token"
        endpoint = MetricsEndpoint("127.0.0.1", 0, credential)
        endpoint.start()
        try:
            status, body = self.request(endpoint, "GET", "/metrics", None)
            self.assertEqual(status, 401)
            self.assertNotIn(credential, body)

            status, body = self.request(endpoint, "GET", "/metrics", credential)
            self.assertEqual(status, 503)
            self.assertNotIn(credential, body)

            endpoint.update(snapshot([dispatch()]))
            status, body = self.request(endpoint, "GET", "/metrics", credential)
            self.assertEqual(status, 200)
            self.assertIn(b"cwo_dispatch_completed_cycles_total", body)
            self.assertNotIn(b"packet_sha256", body)
            self.assertNotIn(b"submission-", body)

            status, body = self.request(endpoint, "GET", "/other", credential)
            self.assertEqual(status, 404)
            self.assertNotIn(credential, body)

            status, body = self.request(endpoint, "POST", "/metrics", credential)
            self.assertEqual(status, 501)
            self.assertNotIn(credential, body)
            self.assertEqual(
                self.request(endpoint, "GET", "/metrics", credential)[0], 200
            )
        finally:
            endpoint.close()

        with self.assertRaisesRegex(ValueError, "must bind loopback"):
            MetricsEndpoint("192.0.2.1", 0, credential)

    def test_scrape_path_serves_last_snapshot_while_refresh_source_is_blocked(
        self,
    ) -> None:
        credential = b"test-observability-token"
        endpoint = MetricsEndpoint("127.0.0.1", 0, credential)
        entered = threading.Event()
        release = threading.Event()
        calls = 0

        def snapshot_source() -> dict[str, object]:
            nonlocal calls
            calls += 1
            if calls > 1:
                entered.set()
                release.wait(timeout=2)
            return snapshot([dispatch()])

        service = ObservabilityExportService(snapshot_source, endpoint)
        service.start()
        try:
            self.assertTrue(entered.wait(timeout=2))
            started = time.monotonic()
            status, body = self.request(endpoint, "GET", "/metrics", credential)
            elapsed = time.monotonic() - started
            self.assertEqual(status, 200)
            self.assertIn(b"cwo_dispatch_info", body)
            self.assertLess(elapsed, 1)
        finally:
            release.set()
            service.close()

    def test_prometheus_client_uses_bounded_authenticated_loopback_post(self) -> None:
        credential = b"prometheus-query-token"
        seen: list[dict[str, object]] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                form = parse_qs(self.rfile.read(length).decode("ascii"))
                seen.append(
                    {
                        "path": self.path,
                        "authorization": self.headers.get("Authorization"),
                        "form": form,
                    }
                )
                body = json.dumps(
                    {
                        "status": "success",
                        "data": {
                            "resultType": "vector",
                            "result": [
                                {
                                    "metric": {"__name__": "up"},
                                    "value": [1000, "1"],
                                }
                            ],
                        },
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            client = PrometheusQueryClient(
                f"http://{host}:{port}", credential=credential
            )
            rows = client.query("up", 1000)
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.assertEqual(rows[0]["value"][1], "1")
        self.assertEqual(seen[0]["path"], "/api/v1/query")
        self.assertEqual(
            seen[0]["authorization"], "Bearer " + credential.decode("ascii")
        )
        self.assertEqual(seen[0]["form"], {"query": ["up"], "time": ["1000"]})

        with self.assertRaisesRegex(ValueError, "reviewed local endpoint"):
            PrometheusQueryClient("http://192.0.2.1:9090")

    def test_credential_reader_requires_owner_only_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            credential = root / "scrape.token"
            credential.write_bytes(b"test-observability-token\n")
            credential.chmod(0o600)
            self.assertEqual(read_credential(credential), b"test-observability-token")

            credential.chmod(0o640)
            with self.assertRaisesRegex(ValueError, "owner-only regular file"):
                read_credential(credential)


if __name__ == "__main__":
    unittest.main()
