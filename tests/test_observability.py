from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from geo_game_collector.observability import (
    Correlation,
    GatewayTelemetry,
    JsonlLogBuffer,
    RetentionCandidate,
    RetentionPolicies,
    eligible_for_cleanup,
    structured_log,
    technical_log_pressure_cleanup,
)

NOW = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)


def _record(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "correlation": Correlation(
            collector_id="collector-local-01",
            run_id="run-1",
            transport_id="transport-1",
            bundle_id="bundle-1",
            source="taptap",
        ),
        "component": "collector-agent",
        "component_version": "0.1.0",
        "stage": "collect",
        "event_type": "request_failed",
        "level": "error",
        "message": "GET https://example.test/path?X-Amz-Signature=secret failed",
        "payload": {"status": 503},
        "error": TimeoutError("Authorization: Bearer secret"),
        "occurred_at": NOW,
    }
    values.update(overrides)
    return structured_log(**values)  # type: ignore[arg-type]


def test_structured_log_is_correlated_utc_and_otel_compatible() -> None:
    record = _record()
    assert record["timestamp"] == "2026-07-29T08:00:00Z"
    assert record["time_unix_nano"] == 1785312000000000000
    assert record["severity_text"] == "ERROR"
    assert record["attributes"]["collector.id"] == "collector-local-01"
    assert record["attributes"]["run.id"] == "run-1"
    assert record["attributes"]["component.version"] == "0.1.0"
    assert len(record["attributes"]["error.fingerprint"]) == 24


def test_structured_log_removes_secrets_queries_and_raw_bodies() -> None:
    record = _record(
        message=(
            "Authorization: Bearer message-secret mysql://user:password@db.internal/geo"
        ),
        payload={
            "authorization": "Bearer secret",
            "Cookie": "sid=secret",
            "xsrf_token": "secret",
            "upload_url": "https://minio.test/a?X-Amz-Credential=secret",
            "response_body": "<secret>",
            "safe_url": "https://source.test/game?id=secret",
            "nested": {"database_url": "mysql://secret"},
        },
    )
    encoded = json.dumps(record)
    assert "Bearer secret" not in encoded
    assert "message-secret" not in encoded
    assert "user:password" not in encoded
    assert "sid=secret" not in encoded
    assert "X-Amz-Credential" not in encoded
    assert "<secret>" not in encoded
    assert "https://source.test/game" in encoded
    assert "?id=secret" not in encoded


def test_naive_timestamp_and_unknown_level_are_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _record(occurred_at=datetime(2026, 7, 29))
    with pytest.raises(ValueError, match="unsupported"):
        _record(level="notice")


class _Exporter:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.records: list[dict[str, object]] = []

    def export(self, records: list[dict[str, object]]) -> None:
        if self.fail:
            raise OSError("backend unavailable with token=secret")
        self.records.extend(records)


def test_jsonl_rotates_and_exports_only_closed_segments(tmp_path: Path) -> None:
    buffer = JsonlLogBuffer(
        tmp_path / "logs",
        max_segment_bytes=4096,
        max_records_per_segment=2,
        clock=lambda: NOW,
    )
    buffer.append(_record(event_type="one"))
    buffer.append(_record(event_type="two"))
    buffer.append(_record(event_type="three"))
    assert len(buffer.pending_segments()) == 1

    exporter = _Exporter()
    result = buffer.try_export(exporter)

    assert result.accepted is True
    assert result.exported_records == 2
    assert len(exporter.records) == 2
    assert not buffer.pending_segments()
    assert len(list((tmp_path / "logs").glob("*.acked"))) == 1
    assert (tmp_path / "logs" / "active.jsonl").exists()


def test_export_failure_preserves_segment_and_is_non_throwing(tmp_path: Path) -> None:
    bundle = tmp_path / "spool" / "pending.tar.zst"
    bundle.parent.mkdir()
    bundle.write_bytes(b"bundle-is-independent")
    buffer = JsonlLogBuffer(
        tmp_path / "logs",
        max_segment_bytes=4096,
        max_records_per_segment=1,
        clock=lambda: NOW,
    )
    buffer.append(_record())
    buffer.rotate()
    before = buffer.pending_segments()[0].read_bytes()

    result = buffer.try_export(_Exporter(fail=True))

    assert result.accepted is False
    assert result.error_classification == "telemetry_backend_unavailable"
    assert buffer.pending_segments()[0].read_bytes() == before
    assert bundle.read_bytes() == b"bundle-is-independent"


class _EventGateway:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.events = []

    def send_events(self, events):
        if self.fail:
            raise ConnectionError("Gateway unavailable")
        self.events.extend(events)
        return object()


def test_gateway_telemetry_replays_same_event_id_after_outage(tmp_path: Path) -> None:
    gateway = _EventGateway(fail=True)
    buffer = JsonlLogBuffer(
        tmp_path / "logs",
        max_segment_bytes=4096,
        max_records_per_segment=10,
        clock=lambda: NOW,
    )
    telemetry = GatewayTelemetry(
        buffer=buffer,
        gateway=gateway,
        event_id_factory=lambda: "event-1",
    )

    sent = telemetry.emit(
        correlation=Correlation(
            collector_id="collector-1",
            run_id="run-1",
            job_id="job-1",
        ),
        stage="collect",
        event_type="item_completed",
        level="info",
        message="source https://source.test/a?token=secret completed",
        payload={"status": "success", "authorization": "Bearer secret"},
        occurred_at=NOW,
    )

    assert sent is False
    gateway.fail = False
    flushed = telemetry.flush()
    assert flushed.accepted is True
    assert gateway.events[0]["event_id"] == "event-1"
    assert gateway.events[0]["payload"]["authorization"] == "[REDACTED]"
    assert not buffer.pending_segments()


def test_retention_never_selects_unacknowledged_bundle_evidence() -> None:
    policies = RetentionPolicies(
        operator_events=timedelta(days=7),
        technical_logs=timedelta(days=2),
        processed_bundles=timedelta(days=3),
        raw_evidence=timedelta(days=10),
        dead_letter_evidence=timedelta(days=30),
        technical_log_max_bytes=10_000,
    )
    old = NOW - timedelta(days=40)
    candidates = [
        RetentionCandidate(Path("old-log"), "technical_log", old, True),
        RetentionCandidate(Path("pending-log"), "technical_log", old, False),
        RetentionCandidate(Path("processed"), "processed_bundle", old, True),
        RetentionCandidate(Path("unprocessed"), "processed_bundle", old, False),
        RetentionCandidate(Path("raw-pending"), "raw_evidence", old, False),
        RetentionCandidate(Path("dead-letter"), "dead_letter_evidence", old, True),
    ]

    assert eligible_for_cleanup(candidates, policies=policies, now=NOW) == (
        Path("old-log"),
        Path("processed"),
        Path("dead-letter"),
    )


def test_retention_policies_are_separate_and_validate_categories() -> None:
    policies = RetentionPolicies(
        operator_events=timedelta(days=7),
        technical_logs=timedelta(days=2),
        processed_bundles=timedelta(days=3),
        raw_evidence=timedelta(days=10),
        dead_letter_evidence=timedelta(days=30),
        technical_log_max_bytes=10_000,
    )
    recent = RetentionCandidate(
        Path("recent-log"),
        "technical_log",
        NOW - timedelta(days=1),
        True,
    )
    assert eligible_for_cleanup([recent], policies=policies, now=NOW) == ()
    with pytest.raises(ValueError, match="unknown retention category"):
        eligible_for_cleanup(
            [RetentionCandidate(Path("x"), "unknown", NOW, True)],
            policies=policies,
            now=NOW,
        )


def test_log_pressure_only_selects_acknowledged_technical_logs() -> None:
    policies = RetentionPolicies(
        operator_events=timedelta(days=7),
        technical_logs=timedelta(days=2),
        processed_bundles=timedelta(days=3),
        raw_evidence=timedelta(days=10),
        dead_letter_evidence=timedelta(days=30),
        technical_log_max_bytes=1_000,
    )
    candidates = [
        RetentionCandidate(
            Path("old-log"),
            "technical_log",
            NOW - timedelta(days=3),
            True,
            600,
        ),
        RetentionCandidate(
            Path("new-log"),
            "technical_log",
            NOW - timedelta(days=2),
            True,
            600,
        ),
        RetentionCandidate(
            Path("pending-log"),
            "technical_log",
            NOW - timedelta(days=9),
            False,
            9_000,
        ),
        RetentionCandidate(
            Path("unprocessed-bundle"),
            "processed_bundle",
            NOW - timedelta(days=9),
            False,
            9_000,
        ),
    ]

    assert technical_log_pressure_cleanup(
        candidates,
        policies=policies,
        current_log_bytes=2_100,
    ) == (Path("old-log"), Path("new-log"))
