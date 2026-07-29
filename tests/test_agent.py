from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from geo_game_collector.agent import CollectorAgent, correlations_for
from geo_game_collector.contracts import (
    CanonicalGameResult,
    ErrorCategory,
    ResultStatus,
    SourceName,
)
from geo_game_collector.execution import (
    ExecutedItem,
    JobExecutor,
    executed_item_to_payload,
)
from geo_game_collector.local_state import JobStore
from geo_game_collector.packaging import BundlePackageResult
from geo_game_collector.spool import SpoolStore
from geo_game_collector.transport import RemoteRun

NOW = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)


class _Control:
    def __init__(self, record):
        self.record = record
        self.sync_calls = 0

    def sync_configuration(self, *, now):
        self.sync_calls += 1
        return object()

    def claim_and_persist(self, *, claim_request_id, mode):
        return self.record


class _Gateway:
    def __init__(self, collector_id: str):
        self.collector_id = collector_id
        self.completions = []
        self.heartbeats = []

    def complete_run(self, **payload):
        self.completions.append(payload)
        return RemoteRun(
            created=True,
            collector_id=self.collector_id,
            run_id=payload["run_id"],
            job_id=payload["job_id"],
            status=payload["status"],
        )

    def send_heartbeat(self, **payload):
        self.heartbeats.append(payload)
        return NOW


class _Runtime:
    def __init__(self):
        self.calls = 0

    def refresh(self, source, *, name):
        self.calls += 1
        return CanonicalGameResult(
            source=source,
            requested_name=name,
            status=ResultStatus.SUCCESS,
            source_game_id=f"{source.value}-1",
            name=name,
            raw={"source": source.value},
        )

    def discover(self, source, *, path, limit):
        raise AssertionError("not used")


class _Drain:
    def __init__(self, spool):
        self.spool = spool
        self.calls = []

    def drain_one(self, transport_id, *, now):
        self.calls.append(transport_id)
        return self.spool.load(transport_id)


class _Telemetry:
    def __init__(self):
        self.event_types = []

    def emit(self, **event):
        self.event_types.append(event["event_type"])
        return True


def _manifest():
    return {
        "schema_version": 1,
        "job_id": "job-1",
        "policy_version": "policy-1",
        "mode": "refresh",
        "source_order": ["baidu", "taptap"],
        "targets": [{"target_game_id": 7, "name": "Fixture", "category_id": 3}],
        "discovery_paths": [],
        "limits": {
            "max_targets": 10,
            "max_discovery_paths": 5,
            "max_requests": 10,
            "max_media_per_item": 0,
            "max_media_bytes": 1024,
        },
        "scheduled_for": "2026-07-29T08:00:00Z",
        "destination": "geo-production",
    }


def _sha(payload):
    import json

    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def test_agent_checkpoints_results_before_idempotent_run_and_spool(tmp_path: Path):
    collector_id = "collector-local-1"
    manifest = _manifest()
    jobs = JobStore(tmp_path / "jobs")
    record = jobs.persist_claimed(
        job_id="job-1",
        claim_request_id="claim-1",
        manifest=manifest,
        manifest_sha256=_sha(manifest),
        created_at=NOW,
    )
    control = _Control(record)
    gateway = _Gateway(collector_id)
    runtime = _Runtime()
    spool = SpoolStore(tmp_path / "spool", max_entries=10, max_bytes=1024 * 1024)
    drain = _Drain(spool)
    telemetry = _Telemetry()
    build_calls = []

    def build(**kwargs):
        build_calls.append(kwargs)
        destination = kwargs["destination"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        archive = b"fixture-archive"
        destination.write_bytes(archive)
        correlations = kwargs["correlations"]
        return BundlePackageResult(
            path=destination,
            object_key=(f"incoming/{collector_id}/{correlations.transport_id}.tar.zst"),
            archive_bytes=len(archive),
            archive_sha256=hashlib.sha256(archive).hexdigest(),
            file_count=3,
        )

    agent = CollectorAgent(
        collector_id=collector_id,
        data_directory=tmp_path,
        control=control,
        gateway=gateway,
        job_store=jobs,
        executor=JobExecutor(runtime),
        spool=spool,
        drain=drain,
        clock=lambda: NOW,
        bundle_builder=build,
        telemetry=telemetry,
    )

    result = agent.run_once(claim_request_id="claim-1", mode="refresh")
    persisted = jobs.load("job-1")

    assert result.run_status == "succeeded"
    assert result.transport_state == "packaged"
    assert runtime.calls == 2
    assert persisted.request_count == 2
    assert len(persisted.completed_item_payloads) == 2
    assert persisted.run_completion is not None
    assert len(gateway.completions) == 1
    assert gateway.heartbeats[-1]["current_stage"] == "idle"
    assert gateway.heartbeats[-1]["spool_pending_count"] == 1
    assert len(build_calls) == 1
    assert spool.load(result.transport_id).transfer.archive_sha256
    assert {
        "job_claimed",
        "item_completed",
        "run_completed",
        "bundle_packaged",
        "transport_advanced",
    }.issubset(set(telemetry.event_types))

    control.record = persisted
    replay = agent.run_once(claim_request_id="claim-1", mode="refresh")

    assert replay.transport_id == result.transport_id
    assert runtime.calls == 2
    assert len(gateway.completions) == 1
    assert len(build_calls) == 1
    assert correlations_for(collector_id, "job-1").transport_id == result.transport_id


def test_restart_after_durable_blocked_checkpoint_never_requests_another_source(
    tmp_path: Path,
) -> None:
    collector_id = "collector-local-1"
    manifest = _manifest()
    jobs = JobStore(tmp_path / "jobs")
    jobs.persist_claimed(
        job_id="job-1",
        claim_request_id="claim-1",
        manifest=manifest,
        manifest_sha256=_sha(manifest),
        created_at=NOW,
    )
    blocked = ExecutedItem(
        item_key="target-7-taptap",
        target_game_id=7,
        category_id=3,
        result=CanonicalGameResult(
            source=SourceName.TAPTAP,
            requested_name="Fixture",
            status=ResultStatus.BLOCKED,
            raw={"status": 403},
            error_category=ErrorCategory.BLOCKED,
        ),
    )
    jobs.checkpoint_item(
        "job-1",
        item_key=blocked.item_key,
        payload=executed_item_to_payload(blocked),
        request_count=1,
        now=NOW,
    )
    runtime = _Runtime()
    spool = SpoolStore(tmp_path / "spool", max_entries=10, max_bytes=1024 * 1024)
    agent = CollectorAgent(
        collector_id=collector_id,
        data_directory=tmp_path,
        control=_Control(jobs.load("job-1")),
        gateway=_Gateway(collector_id),
        job_store=jobs,
        executor=JobExecutor(runtime),
        spool=spool,
        drain=_Drain(spool),
        clock=lambda: NOW,
    )

    _, summary = agent._execute(jobs.load("job-1"))

    assert runtime.calls == 0
    assert summary.halted_by_block is True
    assert summary.items == (blocked,)


def test_spool_backpressure_is_checked_before_configuration_or_claim(
    tmp_path: Path,
) -> None:
    collector_id = "collector-local-1"
    control = _Control(None)
    runtime = _Runtime()
    spool = SpoolStore(tmp_path / "spool", max_entries=10, max_bytes=1024 * 1024)
    cleanup_calls = []
    spool.cleanup_processed = lambda **kwargs: cleanup_calls.append(kwargs) or ()  # type: ignore[method-assign]
    spool.can_reserve = lambda *_args, **_kwargs: False  # type: ignore[method-assign]
    gateway = _Gateway(collector_id)
    agent = CollectorAgent(
        collector_id=collector_id,
        data_directory=tmp_path,
        control=control,
        gateway=gateway,
        job_store=JobStore(tmp_path / "jobs"),
        executor=JobExecutor(runtime),
        spool=spool,
        drain=_Drain(spool),
        clock=lambda: NOW,
        spool_min_free_bytes=1024,
    )

    result = agent.run_once(claim_request_id="claim-1", mode="refresh")

    assert result.job_id is None
    assert control.sync_calls == 0
    assert runtime.calls == 0
    assert cleanup_calls == [{"now": NOW, "processed_grace": timedelta(hours=24)}]
    assert gateway.heartbeats[-1]["current_stage"] == "backpressure"
