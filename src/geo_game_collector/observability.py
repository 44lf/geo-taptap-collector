"""Secret-safe structured logs and durable local telemetry buffering."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

_SENSITIVE_KEY = re.compile(
    r"(authorization|credential|secret|password|token|cookie|xsrf|presigned|"
    r"upload_url|database_url|minio|raw_body|response_body)",
    re.IGNORECASE,
)
_URL = re.compile(r"https?://[^\s\"']+", re.IGNORECASE)
_BEARER = re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s,;]+")
_URI_USERINFO = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)([^/@\s]+@)")
_SAFE_LEVELS = {"debug", "info", "warning", "error", "critical"}


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return current.astimezone(UTC)


def _strip_url_query(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        parsed = urlsplit(match.group(0))
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))

    return _URL.sub(replace, value)


def _sanitize_text(value: str) -> str:
    clean = _strip_url_query(value)
    clean = _BEARER.sub(r"\1[REDACTED]", clean)
    return _URI_USERINFO.sub(r"\1[REDACTED]@", clean)


def sanitize(value: Any, *, key: str = "") -> Any:
    """Remove credentials, request bodies, and URL queries before persistence."""

    if _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {
            str(item_key): sanitize(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        return _sanitize_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _sanitize_text(str(value))


@dataclass(frozen=True, slots=True)
class Correlation:
    collector_id: str
    run_id: str | None = None
    transport_id: str | None = None
    bundle_id: str | None = None
    job_id: str | None = None
    source: str | None = None

    def attributes(self) -> dict[str, str]:
        values = {
            "collector.id": self.collector_id,
            "run.id": self.run_id,
            "transport.id": self.transport_id,
            "bundle.id": self.bundle_id,
            "job.id": self.job_id,
            "source": self.source,
        }
        if not self.collector_id.strip():
            raise ValueError("collector_id must be non-empty")
        return {key: value for key, value in values.items() if value is not None}


def error_fingerprint(error: BaseException) -> str:
    """Create a stable non-secret fingerprint without retaining exception text."""

    category = f"{type(error).__module__}.{type(error).__qualname__}"
    return hashlib.sha256(category.encode("utf-8")).hexdigest()[:24]


def structured_log(
    *,
    correlation: Correlation,
    component: str,
    component_version: str,
    stage: str,
    event_type: str,
    level: str,
    message: str,
    payload: Mapping[str, Any] | None = None,
    error: BaseException | None = None,
    occurred_at: datetime | None = None,
) -> dict[str, Any]:
    """Build one OpenTelemetry-compatible JSON log record."""

    normalized_level = level.lower()
    required = (component, component_version, stage, event_type)
    if normalized_level not in _SAFE_LEVELS:
        raise ValueError("unsupported log level")
    if any(not value.strip() for value in required):
        raise ValueError(
            "component, component_version, stage and event_type are required"
        )
    when = _utc(occurred_at)
    attributes: dict[str, Any] = {
        **correlation.attributes(),
        "component": component,
        "component.version": component_version,
        "stage": stage,
        "event.type": event_type,
        **dict(payload or {}),
    }
    if error is not None:
        attributes["error.type"] = type(error).__name__
        attributes["error.fingerprint"] = error_fingerprint(error)
    return {
        "timestamp": when.isoformat().replace("+00:00", "Z"),
        "time_unix_nano": int(when.timestamp() * 1_000_000_000),
        "severity_text": normalized_level.upper(),
        "body": sanitize(message),
        "attributes": sanitize(attributes),
    }


@dataclass(frozen=True, slots=True)
class ExportResult:
    accepted: bool
    exported_records: int = 0
    error_classification: str | None = None


class LogExporter(Protocol):
    def export(self, records: Sequence[Mapping[str, Any]]) -> None: ...


class EventBatchGateway(Protocol):
    def send_events(self, events: Sequence[Mapping[str, Any]]) -> object: ...


class JsonlLogBuffer:
    """Append-only rotated JSONL whose closed segments survive exporter outages."""

    def __init__(
        self,
        root: Path,
        *,
        max_segment_bytes: int,
        max_records_per_segment: int,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if max_segment_bytes < 256 or max_records_per_segment < 1:
            raise ValueError("invalid JSONL segment limits")
        self.root = root
        self.max_segment_bytes = max_segment_bytes
        self.max_records_per_segment = max_records_per_segment
        self._clock = clock or (lambda: datetime.now(UTC))
        self.root.mkdir(parents=True, exist_ok=True)
        self._active = self.root / "active.jsonl"

    def _active_records(self) -> int:
        if not self._active.exists():
            return 0
        with self._active.open("rb") as stream:
            return sum(1 for _ in stream)

    def append(self, record: Mapping[str, Any]) -> None:
        encoded = (
            json.dumps(
                sanitize(dict(record)),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        if len(encoded) > self.max_segment_bytes:
            raise ValueError("single log record exceeds segment byte limit")
        current_bytes = self._active.stat().st_size if self._active.exists() else 0
        current_records = self._active_records()
        if current_records and (
            current_bytes + len(encoded) > self.max_segment_bytes
            or current_records >= self.max_records_per_segment
        ):
            self.rotate()
        with self._active.open("ab") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())

    def rotate(self) -> Path | None:
        if not self._active.exists() or self._active.stat().st_size == 0:
            return None
        now = _utc(self._clock())
        prefix = now.strftime("%Y%m%dT%H%M%S.%fZ")
        sequence = 0
        while True:
            candidate = self.root / f"segment-{prefix}-{sequence:04d}.jsonl"
            if not candidate.exists():
                break
            sequence += 1
        os.replace(self._active, candidate)
        return candidate

    def pending_segments(self) -> tuple[Path, ...]:
        return tuple(sorted(self.root.glob("segment-*.jsonl")))

    @staticmethod
    def read_segment(path: Path) -> list[dict[str, Any]]:
        with path.open("r", encoding="utf-8") as stream:
            return [json.loads(line) for line in stream if line.strip()]

    def try_export(self, exporter: LogExporter) -> ExportResult:
        """Export closed segments; leave every byte in place when export fails."""

        exported = 0
        for segment in self.pending_segments():
            records = self.read_segment(segment)
            try:
                exporter.export(records)
            except Exception:
                return ExportResult(
                    accepted=False,
                    exported_records=exported,
                    error_classification="telemetry_backend_unavailable",
                )
            acknowledged = segment.with_suffix(".acked")
            os.replace(segment, acknowledged)
            exported += len(records)
        return ExportResult(accepted=True, exported_records=exported)


class GatewayTelemetry:
    """Write local JSONL first and best-effort mirror operator events to GEO."""

    def __init__(
        self,
        *,
        buffer: JsonlLogBuffer,
        gateway: EventBatchGateway,
        event_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._buffer = buffer
        self._gateway = gateway
        self._event_id_factory = event_id_factory or (lambda: uuid.uuid4().hex)

    @staticmethod
    def _event(record: Mapping[str, Any]) -> dict[str, Any]:
        attributes = record.get("attributes")
        if not isinstance(attributes, Mapping):
            raise ValueError("structured record attributes are invalid")
        payload = attributes.get("event.payload")
        return {
            "event_id": str(attributes["event.id"]),
            "run_id": attributes.get("run.id"),
            "job_id": attributes.get("job.id"),
            "transport_id": attributes.get("transport.id"),
            "bundle_id": attributes.get("bundle.id"),
            "source": attributes.get("source"),
            "component": str(attributes["component"]),
            "component_version": str(attributes["component.version"]),
            "stage": str(attributes["stage"]),
            "event_type": str(attributes["event.type"]),
            "level": str(record["severity_text"]).lower(),
            "payload": dict(payload) if isinstance(payload, Mapping) else None,
            "occurred_at": str(record["timestamp"]),
        }

    def emit(
        self,
        *,
        correlation: Correlation,
        stage: str,
        event_type: str,
        level: str,
        message: str,
        payload: Mapping[str, Any] | None = None,
        error: BaseException | None = None,
        occurred_at: datetime | None = None,
    ) -> bool:
        event_id = self._event_id_factory()
        record = structured_log(
            correlation=correlation,
            component="collector-agent",
            component_version="0.1.0",
            stage=stage,
            event_type=event_type,
            level=level,
            message=message,
            payload={
                "event.id": event_id,
                "event.payload": dict(payload or {}),
            },
            error=error,
            occurred_at=occurred_at,
        )
        try:
            self._buffer.append(record)
        except Exception:
            pass
        try:
            self._gateway.send_events((self._event(record),))
        except Exception:
            return False
        return True

    def export(self, records: Sequence[Mapping[str, Any]]) -> None:
        for start in range(0, len(records), 100):
            self._gateway.send_events(
                tuple(self._event(record) for record in records[start : start + 100])
            )

    def flush(self) -> ExportResult:
        self._buffer.rotate()
        return self._buffer.try_export(self)


@dataclass(frozen=True, slots=True)
class RetentionPolicies:
    operator_events: timedelta
    technical_logs: timedelta
    processed_bundles: timedelta
    raw_evidence: timedelta
    dead_letter_evidence: timedelta
    technical_log_max_bytes: int

    def __post_init__(self) -> None:
        durations = (
            self.operator_events,
            self.technical_logs,
            self.processed_bundles,
            self.raw_evidence,
            self.dead_letter_evidence,
        )
        if any(value < timedelta(0) for value in durations):
            raise ValueError("retention durations cannot be negative")
        if self.technical_log_max_bytes <= 0:
            raise ValueError("technical_log_max_bytes must be positive")


@dataclass(frozen=True, slots=True)
class RetentionCandidate:
    path: Path
    category: str
    created_at: datetime
    acknowledged: bool
    size_bytes: int = 0


def eligible_for_cleanup(
    candidates: Iterable[RetentionCandidate],
    *,
    policies: RetentionPolicies,
    now: datetime | None = None,
) -> tuple[Path, ...]:
    """Return cleanup candidates without ever selecting unprocessed Bundle evidence."""

    current = _utc(now)
    duration_by_category = {
        "operator_event": policies.operator_events,
        "technical_log": policies.technical_logs,
        "processed_bundle": policies.processed_bundles,
        "raw_evidence": policies.raw_evidence,
        "dead_letter_evidence": policies.dead_letter_evidence,
    }
    eligible: list[RetentionCandidate] = []
    for candidate in candidates:
        if candidate.category not in duration_by_category:
            raise ValueError(f"unknown retention category: {candidate.category}")
        if (
            candidate.category in {"technical_log", "processed_bundle"}
            and not candidate.acknowledged
        ):
            continue
        if candidate.category == "raw_evidence" and not candidate.acknowledged:
            continue
        if (
            current - _utc(candidate.created_at)
            >= duration_by_category[candidate.category]
        ):
            eligible.append(candidate)
    priority = {
        "technical_log": 0,
        "operator_event": 1,
        "processed_bundle": 2,
        "raw_evidence": 3,
        "dead_letter_evidence": 4,
    }
    eligible.sort(
        key=lambda item: (
            priority[item.category],
            _utc(item.created_at),
            str(item.path),
        )
    )
    return tuple(item.path for item in eligible)


def technical_log_pressure_cleanup(
    candidates: Iterable[RetentionCandidate],
    *,
    policies: RetentionPolicies,
    current_log_bytes: int,
) -> tuple[Path, ...]:
    """Select oldest acknowledged technical logs until the log quota is met."""

    if current_log_bytes < 0:
        raise ValueError("current_log_bytes cannot be negative")
    excess = max(0, current_log_bytes - policies.technical_log_max_bytes)
    if excess == 0:
        return ()
    logs = sorted(
        (
            candidate
            for candidate in candidates
            if candidate.category == "technical_log" and candidate.acknowledged
        ),
        key=lambda item: (_utc(item.created_at), str(item.path)),
    )
    selected: list[Path] = []
    reclaimed = 0
    for log in logs:
        if log.size_bytes < 0:
            raise ValueError("candidate size_bytes cannot be negative")
        selected.append(log.path)
        reclaimed += log.size_bytes
        if reclaimed >= excess:
            break
    return tuple(selected)
