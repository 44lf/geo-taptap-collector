"""One-shot Collector orchestration with restart-safe execution and delivery."""

from __future__ import annotations

import hashlib
import platform
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from geo_game_collector import __version__
from geo_game_collector.bundle_build import build_bundle
from geo_game_collector.contracts import CorrelationIds, ResultStatus
from geo_game_collector.control_client import CollectorControlClient
from geo_game_collector.execution import (
    ExecutedItem,
    JobExecutionSummary,
    JobExecutor,
    executed_item_from_payload,
    executed_item_to_payload,
)
from geo_game_collector.local_state import DataDirectoryLock, JobRecord, JobStore
from geo_game_collector.observability import Correlation
from geo_game_collector.packaging import BundlePackageResult
from geo_game_collector.spool import (
    PackagedTransfer,
    SpoolRecord,
    SpoolStore,
)
from geo_game_collector.transport import RemoteRun, TransferDrain


class CollectorAgentError(RuntimeError):
    pass


class RunCompletionGateway(Protocol):
    def complete_run(
        self,
        *,
        run_id: str,
        job_id: str,
        status: str,
        summary: dict[str, Any],
        started_at: datetime,
        finished_at: datetime,
        source: str | None = None,
        error_classification: str | None = None,
        error_summary: str | None = None,
    ) -> RemoteRun: ...

    def send_heartbeat(
        self,
        *,
        platform: str,
        agent_version: str,
        enabled_sources: tuple[str, ...],
        current_run_id: str | None,
        current_stage: str,
        spool_pending_count: int,
    ) -> datetime: ...


class AgentTelemetry(Protocol):
    def emit(
        self,
        *,
        correlation: Correlation,
        stage: str,
        event_type: str,
        level: str,
        message: str,
        payload: dict[str, Any] | None = None,
        error: BaseException | None = None,
        occurred_at: datetime | None = None,
    ) -> bool: ...


class BundleBuilder(Protocol):
    def __call__(
        self,
        *,
        job_manifest: dict[str, Any],
        correlations: CorrelationIds,
        execution: JobExecutionSummary,
        work_root: Path,
        destination: Path,
        created_at: datetime,
    ) -> BundlePackageResult: ...


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    job_id: str | None
    run_id: str | None
    transport_id: str | None
    run_status: str | None
    transport_state: str | None
    drained_backlog: int


def correlations_for(collector_id: str, job_id: str) -> CorrelationIds:
    digest = hashlib.sha256(f"{collector_id}:{job_id}".encode()).hexdigest()[:24]
    return CorrelationIds(
        collector_id=collector_id,
        job_id=job_id,
        run_id=f"run-{digest}",
        bundle_id=f"bundle-{digest}",
        transport_id=f"transport-{digest}",
    )


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CollectorAgentError("persisted run completion timestamp is invalid")
    return parsed.astimezone(UTC)


def _time_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CollectorAgentError("agent clock must return an aware timestamp")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _run_status(items: tuple[ExecutedItem, ...]) -> str:
    normal = sum(
        item.result.status in {ResultStatus.SUCCESS, ResultStatus.MISS}
        for item in items
    )
    failed = len(items) - normal
    if failed == 0:
        return "succeeded"
    return "partial" if normal else "failed"


class CollectorAgent:
    """Run claimed work once, then advance all durable outbound transfers."""

    def __init__(
        self,
        *,
        collector_id: str,
        data_directory: Path,
        control: CollectorControlClient,
        gateway: RunCompletionGateway,
        job_store: JobStore,
        executor: JobExecutor,
        spool: SpoolStore,
        drain: TransferDrain,
        clock: Callable[[], datetime],
        bundle_builder: BundleBuilder = build_bundle,
        telemetry: AgentTelemetry | None = None,
        processed_grace: timedelta = timedelta(hours=24),
        spool_min_free_bytes: int = 0,
    ) -> None:
        if processed_grace < timedelta(0):
            raise ValueError("processed_grace cannot be negative")
        if spool_min_free_bytes < 0:
            raise ValueError("spool_min_free_bytes cannot be negative")
        self._collector_id = collector_id
        self._data_directory = data_directory
        self._control = control
        self._gateway = gateway
        self._job_store = job_store
        self._executor = executor
        self._spool = spool
        self._drain = drain
        self._clock = clock
        self._bundle_builder = bundle_builder
        self._telemetry = telemetry
        self._processed_grace = processed_grace
        self._spool_min_free_bytes = spool_min_free_bytes

    def _event(
        self,
        correlations: CorrelationIds,
        *,
        stage: str,
        event_type: str,
        level: str = "info",
        payload: dict[str, Any] | None = None,
        error: BaseException | None = None,
    ) -> None:
        if self._telemetry is None:
            return
        try:
            self._telemetry.emit(
                correlation=Correlation(
                    collector_id=correlations.collector_id,
                    run_id=correlations.run_id,
                    transport_id=correlations.transport_id,
                    bundle_id=correlations.bundle_id,
                    job_id=correlations.job_id,
                ),
                stage=stage,
                event_type=event_type,
                level=level,
                message=event_type.replace("_", " "),
                payload=payload,
                error=error,
                occurred_at=self._now(),
            )
        except Exception:
            pass

    def _heartbeat(
        self,
        *,
        correlations: CorrelationIds | None,
        stage: str,
        enabled_sources: tuple[str, ...],
    ) -> None:
        try:
            self._gateway.send_heartbeat(
                platform=platform.system() or "unknown",
                agent_version=__version__,
                enabled_sources=enabled_sources,
                current_run_id=(
                    correlations.run_id if correlations is not None else None
                ),
                current_stage=stage,
                spool_pending_count=self._spool.pending_count(),
            )
        except Exception:
            pass

    def _drain_due(self, *, now: datetime) -> tuple[SpoolRecord, ...]:
        return tuple(
            self._drain.drain_one(
                record.transfer.correlations.transport_id,
                now=now,
            )
            for record in self._spool.list_due(now=now)
        )

    def service_backlog(self) -> tuple[SpoolRecord, ...]:
        with DataDirectoryLock(self._data_directory):
            now = self._now()
            self._spool.cleanup_processed(
                now=now,
                processed_grace=self._processed_grace,
            )
            drained = self._drain_due(now=now)
            self._heartbeat(
                correlations=None,
                stage="idle",
                enabled_sources=(),
            )
            return drained

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise CollectorAgentError("agent clock must return an aware timestamp")
        return value.astimezone(UTC)

    def _execute(self, record: JobRecord) -> tuple[JobRecord, JobExecutionSummary]:
        saved_items = tuple(
            executed_item_from_payload(payload)
            for _key, payload in sorted(record.completed_item_payloads.items())
        )
        if any(item.result.status is ResultStatus.BLOCKED for item in saved_items):
            return record, JobExecutionSummary(
                items=saved_items,
                request_count=record.request_count,
                halted_by_block=True,
                completed_item_keys=record.completed_item_keys,
            )

        def checkpoint(item: ExecutedItem, request_count: int) -> None:
            self._job_store.checkpoint_item(
                record.job_id,
                item_key=item.item_key,
                payload=executed_item_to_payload(item),
                request_count=request_count,
                now=self._now(),
            )
            self._event(
                correlations_for(self._collector_id, record.job_id),
                stage="collect",
                event_type="item_completed",
                payload={
                    "item_key": item.item_key,
                    "source": item.result.source.value,
                    "status": item.result.status.value,
                    "request_count": request_count,
                },
            )

        incremental = self._executor.execute(
            record.manifest,
            completed_item_keys=record.completed_item_keys,
            prior_request_count=record.request_count,
            on_item=checkpoint,
        )
        updated = self._job_store.load(record.job_id)
        all_items = tuple(
            executed_item_from_payload(payload)
            for _key, payload in sorted(updated.completed_item_payloads.items())
        )
        if not all_items:
            raise CollectorAgentError("job execution produced no durable item results")
        return updated, JobExecutionSummary(
            items=all_items,
            request_count=updated.request_count,
            halted_by_block=incremental.halted_by_block
            or any(item.result.status is ResultStatus.BLOCKED for item in saved_items),
            completed_item_keys=updated.completed_item_keys,
        )

    def _persist_completion(
        self,
        record: JobRecord,
        execution: JobExecutionSummary,
        correlations: CorrelationIds,
    ) -> tuple[JobRecord, dict[str, Any]]:
        if record.run_completion is not None:
            return record, record.run_completion
        counts = Counter(item.result.status.value for item in execution.items)
        status = _run_status(execution.items)
        finished_at = self._now()
        completion: dict[str, Any] = {
            "job_id": record.job_id,
            "run_id": correlations.run_id,
            "status": status,
            "summary": {
                "item_count": len(execution.items),
                "request_count": execution.request_count,
                "status_counts": dict(sorted(counts.items())),
                "halted_by_block": execution.halted_by_block,
            },
            "started_at": _time_text(record.created_at),
            "finished_at": _time_text(finished_at),
            "error_classification": ("source_failures" if status == "failed" else None),
            "error_summary": (
                "all collected items ended in blocked or error state"
                if status == "failed"
                else None
            ),
        }
        updated = self._job_store.persist_run_completion(
            record.job_id,
            completion=completion,
            now=finished_at,
        )
        return updated, completion

    def _complete_remote_run(
        self,
        completion: dict[str, Any],
        correlations: CorrelationIds,
    ) -> None:
        remote = self._gateway.complete_run(
            run_id=correlations.run_id,
            job_id=correlations.job_id,
            status=str(completion["status"]),
            summary=dict(completion["summary"]),
            started_at=_parse_utc(str(completion["started_at"])),
            finished_at=_parse_utc(str(completion["finished_at"])),
            error_classification=completion.get("error_classification"),
            error_summary=completion.get("error_summary"),
        )
        if (
            remote.collector_id != correlations.collector_id
            or remote.run_id != correlations.run_id
            or remote.job_id != correlations.job_id
            or remote.status != completion["status"]
        ):
            raise CollectorAgentError("Gateway run completion identity mismatch")

    def run_once(self, *, claim_request_id: str, mode: str) -> AgentRunResult:
        with DataDirectoryLock(self._data_directory):
            started = self._now()
            self._spool.cleanup_processed(
                now=started,
                processed_grace=self._processed_grace,
            )
            drained = self._drain_due(now=started)
            if not self._spool.can_reserve(
                1,
                min_free_bytes=self._spool_min_free_bytes,
            ):
                self._heartbeat(
                    correlations=None,
                    stage="backpressure",
                    enabled_sources=(),
                )
                return AgentRunResult(
                    None,
                    None,
                    None,
                    None,
                    None,
                    len(drained),
                )
            self._control.sync_configuration(now=started)
            record = self._control.claim_and_persist(
                claim_request_id=claim_request_id,
                mode=mode,
            )
            if record is None:
                self._heartbeat(
                    correlations=None,
                    stage="idle",
                    enabled_sources=(),
                )
                return AgentRunResult(None, None, None, None, None, len(drained))

            correlations = correlations_for(self._collector_id, record.job_id)
            enabled_sources = tuple(
                str(source) for source in record.manifest.get("source_order", [])
            )
            self._heartbeat(
                correlations=correlations,
                stage="claim",
                enabled_sources=enabled_sources,
            )
            self._event(
                correlations,
                stage="claim",
                event_type="job_claimed",
                payload={"mode": record.manifest.get("mode")},
            )
            try:
                existing = self._spool.load(correlations.transport_id)
            except FileNotFoundError:
                existing = None
            if existing is not None:
                if record.run_completion is None:
                    raise CollectorAgentError(
                        "packaged transfer exists without durable run completion"
                    )
                self._job_store.mark_terminal(
                    record.job_id,
                    succeeded=record.run_completion["status"] != "failed",
                    now=self._now(),
                )
                delivered = self._drain.drain_one(
                    correlations.transport_id,
                    now=self._now(),
                )
                self._event(
                    correlations,
                    stage="transfer",
                    event_type="transport_resumed",
                    payload={"state": delivered.state.value},
                )
                self._heartbeat(
                    correlations=None,
                    stage="idle",
                    enabled_sources=enabled_sources,
                )
                return AgentRunResult(
                    record.job_id,
                    correlations.run_id,
                    correlations.transport_id,
                    str(record.run_completion["status"]),
                    delivered.state.value,
                    len(drained),
                )

            if record.run_completion is None:
                self._heartbeat(
                    correlations=correlations,
                    stage="collect",
                    enabled_sources=enabled_sources,
                )
                record, execution = self._execute(record)
                record, completion = self._persist_completion(
                    record,
                    execution,
                    correlations,
                )
                self._event(
                    correlations,
                    stage="collect",
                    event_type="run_completed",
                    payload={
                        "status": completion["status"],
                        **dict(completion["summary"]),
                    },
                )
            else:
                completion = record.run_completion
                items = tuple(
                    executed_item_from_payload(payload)
                    for _key, payload in sorted(record.completed_item_payloads.items())
                )
                execution = JobExecutionSummary(
                    items=items,
                    request_count=record.request_count,
                    halted_by_block=bool(
                        completion.get("summary", {}).get("halted_by_block", False)
                    ),
                    completed_item_keys=record.completed_item_keys,
                )

            staging = self._data_directory / "staging"
            package = self._bundle_builder(
                job_manifest=record.manifest,
                correlations=correlations,
                execution=execution,
                work_root=staging / f"{correlations.transport_id}.work",
                destination=staging / f"{correlations.transport_id}.tar.zst",
                created_at=_parse_utc(str(completion["finished_at"])),
            )
            self._heartbeat(
                correlations=correlations,
                stage="package",
                enabled_sources=enabled_sources,
            )
            self._event(
                correlations,
                stage="package",
                event_type="bundle_packaged",
                payload={
                    "archive_size": package.archive_bytes,
                    "archive_sha256": package.archive_sha256,
                },
            )
            self._complete_remote_run(completion, correlations)
            transfer = PackagedTransfer(
                correlations=correlations,
                destination=str(record.manifest["destination"]),
                object_key=package.object_key,
                archive_size=package.archive_bytes,
                archive_sha256=package.archive_sha256,
                bundle_schema_version="2",
                created_at=_parse_utc(str(completion["finished_at"])),
            )
            self._spool.create_packaged(transfer, package.path)
            package.path.unlink(missing_ok=True)
            self._job_store.mark_terminal(
                record.job_id,
                succeeded=completion["status"] != "failed",
                now=self._now(),
            )
            delivered = self._drain.drain_one(
                correlations.transport_id,
                now=self._now(),
            )
            self._event(
                correlations,
                stage="transfer",
                event_type="transport_advanced",
                payload={"state": delivered.state.value},
            )
            self._heartbeat(
                correlations=None,
                stage="idle",
                enabled_sources=enabled_sources,
            )
            return AgentRunResult(
                record.job_id,
                correlations.run_id,
                correlations.transport_id,
                str(completion["status"]),
                delivered.state.value,
                len(drained),
            )
