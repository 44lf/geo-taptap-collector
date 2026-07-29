from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import geo_game_collector.local_state as local_state_module
from geo_game_collector.control_client import CollectorControlClient
from geo_game_collector.local_state import (
    ConfigurationCache,
    DataDirectoryLock,
    DataDirectoryLocked,
    JobIdentityConflict,
    JobState,
    JobStore,
)
from geo_game_collector.transport import RemoteConfiguration, RemoteJob

NOW = datetime(2026, 7, 29, 10, 0, tzinfo=UTC)


def _sha(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _manifest() -> dict:
    return {
        "schema_version": 1,
        "job_id": "job-1",
        "policy_version": "collector-config-3",
        "mode": "refresh",
        "source_order": ["baidu", "taptap"],
        "targets": [
            {"target_game_id": 101, "name": "Game A", "category_id": 9},
            {"target_game_id": 102, "name": "Game B", "category_id": 10},
        ],
        "discovery_paths": [],
        "limits": {
            "max_targets": 2,
            "max_discovery_paths": 1,
            "max_requests": 16,
            "max_media_per_item": 6,
            "max_media_bytes": 1024,
        },
        "scheduled_for": "2026-07-29T10:00:00Z",
        "destination": "geo-production",
    }


class _Gateway:
    def __init__(self, *, configuration, job):
        self.configuration = configuration
        self.job = job
        self.current_versions = []
        self.ack_calls = []
        self.on_ack = None

    def get_configuration(self, current_version):
        self.current_versions.append(current_version)
        return self.configuration

    def claim_job(self, *, claim_request_id, mode):
        assert claim_request_id == "claim-1"
        assert mode == "refresh"
        return self.job

    def acknowledge_job(self, *, job_id, manifest_sha256):
        if self.on_ack is not None:
            self.on_ack()
        self.ack_calls.append((job_id, manifest_sha256))
        return self.job


def test_configuration_cache_is_hash_verified_versioned_and_staleness_bounded(
    tmp_path: Path,
) -> None:
    snapshot = {"schema_version": 1, "refresh": {"enabled": True}}
    remote = RemoteConfiguration(
        version=3,
        unchanged=False,
        snapshot_sha256=_sha(snapshot),
        max_staleness_seconds=3600,
        snapshot=snapshot,
    )
    gateway = _Gateway(configuration=remote, job=None)
    cache = ConfigurationCache(tmp_path / "state" / "configuration.json")
    client = CollectorControlClient(
        gateway=gateway,
        configuration_cache=cache,
        job_store=JobStore(tmp_path / "jobs"),
    )

    first = client.sync_configuration(now=NOW)
    gateway.configuration = RemoteConfiguration(
        version=3,
        unchanged=True,
        snapshot_sha256=_sha(snapshot),
        max_staleness_seconds=3600,
        snapshot=None,
    )
    second = client.sync_configuration(now=NOW + timedelta(minutes=1))

    assert first.snapshot == snapshot
    assert second.snapshot == snapshot
    assert gateway.current_versions == [None, 3]
    assert cache.load().version == 3
    assert cache.is_stale(now=NOW + timedelta(seconds=3659)) is False
    assert cache.is_stale(now=NOW + timedelta(seconds=3660)) is True


def test_claimed_job_is_persisted_before_ack_and_replay_is_idempotent(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    remote = RemoteJob(
        job_id="job-1",
        claim_request_id="claim-1",
        mode="refresh",
        destination="geo-production",
        manifest_sha256=_sha(manifest),
        manifest=manifest,
        status="claimed",
        claimed_at=NOW,
    )
    gateway = _Gateway(configuration=None, job=remote)
    store = JobStore(tmp_path / "jobs")
    gateway.on_ack = lambda: store.load("job-1")
    client = CollectorControlClient(
        gateway=gateway,
        configuration_cache=ConfigurationCache(tmp_path / "config.json"),
        job_store=store,
    )

    first = client.claim_and_persist(claim_request_id="claim-1", mode="refresh")
    repeated = client.claim_and_persist(claim_request_id="claim-1", mode="refresh")

    assert first == repeated
    assert first.state is JobState.ACKNOWLEDGED
    assert gateway.ack_calls == [
        ("job-1", _sha(manifest)),
        ("job-1", _sha(manifest)),
    ]
    assert store.load("job-1").manifest == manifest


def test_partial_final_job_directory_is_quarantined_before_recovery(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    partial = tmp_path / "jobs" / "job-1"
    partial.mkdir(parents=True)
    (partial / "manifest.json").write_text(
        json.dumps(
            {
                "job_id": "job-1",
                "claim_request_id": "claim-1",
                "manifest_sha256": _sha(manifest),
                "manifest": manifest,
                "created_at": "2026-07-29T10:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    recovered = JobStore(tmp_path / "jobs").persist_claimed(
        job_id="job-1",
        claim_request_id="claim-1",
        manifest=manifest,
        manifest_sha256=_sha(manifest),
        created_at=NOW,
    )

    quarantine = tuple((tmp_path / "jobs" / "quarantine").iterdir())
    assert recovered.state is JobState.CLAIMED
    assert len(quarantine) == 1
    assert (quarantine[0] / "manifest.json").is_file()
    assert JobStore(tmp_path / "jobs").load("job-1") == recovered


def test_failed_atomic_claim_never_publishes_a_partial_final_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest()
    original = local_state_module._atomic_json
    calls = 0

    def fail_second_write(path: Path, payload: dict) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated state write failure")
        original(path, payload)

    monkeypatch.setattr(local_state_module, "_atomic_json", fail_second_write)
    store = JobStore(tmp_path / "jobs")
    with pytest.raises(OSError, match="simulated"):
        store.persist_claimed(
            job_id="job-1",
            claim_request_id="claim-1",
            manifest=manifest,
            manifest_sha256=_sha(manifest),
            created_at=NOW,
        )

    assert not (tmp_path / "jobs" / "job-1").exists()
    assert len(tuple((tmp_path / "jobs" / "quarantine").iterdir())) == 1

    monkeypatch.setattr(local_state_module, "_atomic_json", original)
    recovered = store.persist_claimed(
        job_id="job-1",
        claim_request_id="claim-1",
        manifest=manifest,
        manifest_sha256=_sha(manifest),
        created_at=NOW,
    )
    assert store.load("job-1") == recovered


def test_job_checkpoints_and_atomic_state_survive_restart(tmp_path: Path) -> None:
    manifest = _manifest()
    store = JobStore(tmp_path / "jobs")
    store.persist_claimed(
        job_id="job-1",
        claim_request_id="claim-1",
        manifest=manifest,
        manifest_sha256=_sha(manifest),
        created_at=NOW,
    )
    store.mark_acknowledged("job-1", now=NOW)
    store.checkpoint_item(
        "job-1",
        item_key="target-101",
        payload={"item_key": "target-101", "result": {"status": "success"}},
        request_count=1,
        now=NOW,
    )

    restarted = JobStore(tmp_path / "jobs")
    record = restarted.load("job-1")

    assert record.state is JobState.RUNNING
    assert record.completed_item_keys == ("target-101",)
    assert record.completed_item_payloads["target-101"]["result"] == {
        "status": "success"
    }
    assert record.request_count == 1
    assert restarted.incomplete_item_keys("job-1") == ("target-102",)


def test_job_identity_reuse_with_different_manifest_is_rejected(tmp_path: Path) -> None:
    manifest = _manifest()
    store = JobStore(tmp_path / "jobs")
    store.persist_claimed(
        job_id="job-1",
        claim_request_id="claim-1",
        manifest=manifest,
        manifest_sha256=_sha(manifest),
        created_at=NOW,
    )
    changed = {**manifest, "destination": "geo-dev"}

    with pytest.raises(JobIdentityConflict):
        store.persist_claimed(
            job_id="job-1",
            claim_request_id="claim-1",
            manifest=changed,
            manifest_sha256=_sha(changed),
            created_at=NOW,
        )


def test_run_completion_identity_is_persisted_before_terminal_state(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    store = JobStore(tmp_path / "jobs")
    store.persist_claimed(
        job_id="job-1",
        claim_request_id="claim-1",
        manifest=manifest,
        manifest_sha256=_sha(manifest),
        created_at=NOW,
    )
    completion = {
        "job_id": "job-1",
        "run_id": "run-1",
        "status": "succeeded",
        "finished_at": "2026-07-29T08:00:00Z",
    }

    stored = store.persist_run_completion(
        "job-1",
        completion=completion,
        now=NOW,
    )
    replay = JobStore(tmp_path / "jobs").persist_run_completion(
        "job-1",
        completion=completion,
        now=NOW,
    )
    terminal = store.mark_terminal("job-1", succeeded=True, now=NOW)

    assert stored.run_completion == completion
    assert replay.run_completion == completion
    assert terminal.state is JobState.COMPLETED
    with pytest.raises(JobIdentityConflict, match="conflict"):
        store.persist_run_completion(
            "job-1",
            completion={**completion, "status": "failed"},
            now=NOW,
        )


def test_data_directory_global_lock_is_cross_platform_and_single_owner(
    tmp_path: Path,
) -> None:
    first = DataDirectoryLock(tmp_path)
    second = DataDirectoryLock(tmp_path)

    with first:
        with pytest.raises(DataDirectoryLocked):
            second.acquire()
    with second:
        assert second.acquired is True
    assert second.acquired is False
