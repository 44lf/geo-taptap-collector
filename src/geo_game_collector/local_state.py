"""Atomic local configuration, immutable jobs, checkpoints, and data-dir locking."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, BinaryIO


class JobIdentityConflict(RuntimeError):
    pass


class DataDirectoryLocked(RuntimeError):
    pass


def _utc(value: datetime) -> None:
    offset = value.utcoffset()
    if value.tzinfo is None or offset is None or offset.total_seconds() != 0:
        raise ValueError("timestamp must be UTC")


def _time_text(value: datetime) -> str:
    _utc(value)
    return value.isoformat().replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    _utc(parsed)
    return parsed


def _sha(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


@dataclass(frozen=True, slots=True)
class CachedConfiguration:
    version: int
    snapshot_sha256: str
    max_staleness_seconds: int
    snapshot: dict[str, Any]
    retrieved_at: datetime


class ConfigurationCache:
    def __init__(self, path: Path) -> None:
        self.path = path

    def save(
        self,
        *,
        version: int,
        snapshot_sha256: str,
        max_staleness_seconds: int,
        snapshot: dict[str, Any],
        retrieved_at: datetime,
    ) -> CachedConfiguration:
        _utc(retrieved_at)
        if version <= 0 or max_staleness_seconds <= 0:
            raise ValueError("configuration version and staleness must be positive")
        if _sha(snapshot) != snapshot_sha256:
            raise JobIdentityConflict("configuration snapshot hash mismatch")
        record = CachedConfiguration(
            version=version,
            snapshot_sha256=snapshot_sha256,
            max_staleness_seconds=max_staleness_seconds,
            snapshot=snapshot,
            retrieved_at=retrieved_at,
        )
        _atomic_json(
            self.path,
            {
                "version": record.version,
                "snapshot_sha256": record.snapshot_sha256,
                "max_staleness_seconds": record.max_staleness_seconds,
                "snapshot": record.snapshot,
                "retrieved_at": _time_text(record.retrieved_at),
            },
        )
        return record

    def load(self) -> CachedConfiguration:
        payload = json.loads(self.path.read_text("utf-8"))
        record = CachedConfiguration(
            version=int(payload["version"]),
            snapshot_sha256=str(payload["snapshot_sha256"]),
            max_staleness_seconds=int(payload["max_staleness_seconds"]),
            snapshot=dict(payload["snapshot"]),
            retrieved_at=_parse_time(payload["retrieved_at"]),
        )
        if _sha(record.snapshot) != record.snapshot_sha256:
            raise JobIdentityConflict("cached configuration hash mismatch")
        return record

    def load_optional(self) -> CachedConfiguration | None:
        return self.load() if self.path.is_file() else None

    def is_stale(self, *, now: datetime) -> bool:
        _utc(now)
        record = self.load()
        return (
            record.retrieved_at + timedelta(seconds=record.max_staleness_seconds) <= now
        )


class JobState(str, Enum):
    CLAIMED = "claimed"
    ACKNOWLEDGED = "acknowledged"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class JobRecord:
    job_id: str
    claim_request_id: str
    manifest: dict[str, Any]
    manifest_sha256: str
    state: JobState
    completed_item_keys: tuple[str, ...]
    completed_item_payloads: dict[str, dict[str, Any]]
    request_count: int
    run_completion: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


class JobStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _directory(self, job_id: str) -> Path:
        if not job_id or any(char in job_id for char in ("/", "\\", "..")):
            raise ValueError("job_id is unsafe")
        return self.root / job_id

    def _manifest_path(self, job_id: str) -> Path:
        return self._directory(job_id) / "manifest.json"

    def _state_path(self, job_id: str) -> Path:
        return self._directory(job_id) / "state.json"

    def _quarantine_incomplete(self, job_id: str) -> Path:
        directory = self._directory(job_id)
        quarantine = self.root / "quarantine" / f"{job_id}-{uuid.uuid4().hex}"
        quarantine.parent.mkdir(parents=True, exist_ok=True)
        os.replace(directory, quarantine)
        return quarantine

    def persist_claimed(
        self,
        *,
        job_id: str,
        claim_request_id: str,
        manifest: dict[str, Any],
        manifest_sha256: str,
        created_at: datetime,
    ) -> JobRecord:
        _utc(created_at)
        if manifest.get("job_id") != job_id or _sha(manifest) != manifest_sha256:
            raise JobIdentityConflict("job manifest identity or hash mismatch")
        directory = self._directory(job_id)
        if directory.exists():
            try:
                existing = self.load(job_id)
            except (
                FileNotFoundError,
                json.JSONDecodeError,
                KeyError,
                TypeError,
                ValueError,
                JobIdentityConflict,
            ):
                self._quarantine_incomplete(job_id)
            else:
                if (
                    existing.claim_request_id != claim_request_id
                    or existing.manifest != manifest
                    or existing.manifest_sha256 != manifest_sha256
                ):
                    raise JobIdentityConflict(
                        "job_id already belongs to another manifest"
                    )
                return existing

        record = JobRecord(
            job_id=job_id,
            claim_request_id=claim_request_id,
            manifest=manifest,
            manifest_sha256=manifest_sha256,
            state=JobState.CLAIMED,
            completed_item_keys=(),
            completed_item_payloads={},
            request_count=0,
            run_completion=None,
            created_at=created_at,
            updated_at=created_at,
        )
        self.root.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(
                prefix=f".{job_id}.",
                suffix=".incomplete",
                dir=self.root,
            )
        )
        try:
            _atomic_json(
                staging / "manifest.json",
                {
                    "job_id": job_id,
                    "claim_request_id": claim_request_id,
                    "manifest_sha256": manifest_sha256,
                    "manifest": manifest,
                    "created_at": _time_text(created_at),
                },
            )
            _atomic_json(staging / "state.json", self._state_payload(record))
            os.replace(staging, directory)
        except BaseException:
            if staging.exists():
                quarantine = (
                    self.root / "quarantine" / f"{job_id}-staging-{uuid.uuid4().hex}"
                )
                quarantine.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staging, quarantine)
            raise
        return record

    @staticmethod
    def _state_payload(record: JobRecord) -> dict[str, Any]:
        return {
            "state": record.state.value,
            "completed_item_keys": list(record.completed_item_keys),
            "completed_item_payloads": record.completed_item_payloads,
            "request_count": record.request_count,
            "run_completion": record.run_completion,
            "updated_at": _time_text(record.updated_at),
        }

    def load(self, job_id: str) -> JobRecord:
        metadata = json.loads(self._manifest_path(job_id).read_text("utf-8"))
        state = json.loads(self._state_path(job_id).read_text("utf-8"))
        manifest = dict(metadata["manifest"])
        digest = str(metadata["manifest_sha256"])
        if manifest.get("job_id") != job_id or _sha(manifest) != digest:
            raise JobIdentityConflict("persisted job manifest identity or hash changed")
        completed_item_keys = tuple(state["completed_item_keys"])
        raw_payloads = state.get("completed_item_payloads", {})
        if not isinstance(raw_payloads, dict):
            raise JobIdentityConflict("persisted item payloads are invalid")
        completed_item_payloads = {
            str(item_key): dict(payload)
            for item_key, payload in raw_payloads.items()
            if isinstance(payload, dict)
        }
        if len(completed_item_payloads) != len(raw_payloads) or set(
            completed_item_payloads
        ) != set(completed_item_keys):
            raise JobIdentityConflict(
                "persisted item checkpoints and payloads do not match"
            )
        raw_completion = state.get("run_completion")
        if raw_completion is not None and not isinstance(raw_completion, dict):
            raise JobIdentityConflict("persisted run completion is invalid")
        request_count = int(state.get("request_count", 0))
        if request_count < 0:
            raise JobIdentityConflict("persisted request count is invalid")
        return JobRecord(
            job_id=job_id,
            claim_request_id=str(metadata["claim_request_id"]),
            manifest=manifest,
            manifest_sha256=digest,
            state=JobState(state["state"]),
            completed_item_keys=completed_item_keys,
            completed_item_payloads=completed_item_payloads,
            request_count=request_count,
            run_completion=(
                dict(raw_completion) if raw_completion is not None else None
            ),
            created_at=_parse_time(metadata["created_at"]),
            updated_at=_parse_time(state["updated_at"]),
        )

    def mark_acknowledged(self, job_id: str, *, now: datetime) -> JobRecord:
        record = self.load(job_id)
        if record.state not in {JobState.CLAIMED, JobState.ACKNOWLEDGED}:
            return record
        updated = replace(record, state=JobState.ACKNOWLEDGED, updated_at=now)
        self._write_state(updated)
        return updated

    def persist_run_completion(
        self,
        job_id: str,
        *,
        completion: dict[str, Any],
        now: datetime,
    ) -> JobRecord:
        record = self.load(job_id)
        if completion.get("job_id") != job_id:
            raise JobIdentityConflict("run completion job identity mismatch")
        try:
            json.dumps(completion, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise JobIdentityConflict("run completion is not JSON-safe") from exc
        if record.run_completion is not None:
            if record.run_completion != completion:
                raise JobIdentityConflict("run completion identity conflict")
            return record
        updated = replace(
            record,
            run_completion=completion,
            updated_at=now,
        )
        self._write_state(updated)
        return updated

    def mark_terminal(
        self,
        job_id: str,
        *,
        succeeded: bool,
        now: datetime,
    ) -> JobRecord:
        record = self.load(job_id)
        if record.run_completion is None:
            raise JobIdentityConflict(
                "job cannot become terminal before run completion"
            )
        target = JobState.COMPLETED if succeeded else JobState.FAILED
        if record.state in {JobState.COMPLETED, JobState.FAILED}:
            if record.state is not target:
                raise JobIdentityConflict("job terminal state conflict")
            return record
        updated = replace(record, state=target, updated_at=now)
        self._write_state(updated)
        return updated

    def checkpoint_item(
        self,
        job_id: str,
        *,
        item_key: str,
        payload: dict[str, Any],
        request_count: int,
        now: datetime,
    ) -> JobRecord:
        record = self.load(job_id)
        if payload.get("item_key") != item_key:
            raise JobIdentityConflict("item checkpoint identity mismatch")
        try:
            json.dumps(payload, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise JobIdentityConflict("item checkpoint is not JSON-safe") from exc
        existing = record.completed_item_payloads.get(item_key)
        if existing is not None and existing != payload:
            raise JobIdentityConflict("item checkpoint identity conflict")
        max_requests = record.manifest.get("limits", {}).get("max_requests")
        if (
            not isinstance(max_requests, int)
            or request_count < record.request_count
            or request_count > max_requests
        ):
            raise JobIdentityConflict("item checkpoint request count is invalid")
        completed = tuple(dict.fromkeys((*record.completed_item_keys, item_key)))
        payloads = {**record.completed_item_payloads, item_key: payload}
        updated = replace(
            record,
            state=JobState.RUNNING,
            completed_item_keys=completed,
            completed_item_payloads=payloads,
            request_count=request_count,
            updated_at=now,
        )
        self._write_state(updated)
        return updated

    def incomplete_item_keys(self, job_id: str) -> tuple[str, ...]:
        record = self.load(job_id)
        targets = record.manifest.get("targets")
        if not isinstance(targets, list):
            return ()
        expected = tuple(
            f"target-{target['target_game_id']}"
            for target in targets
            if isinstance(target, dict)
            and isinstance(target.get("target_game_id"), int)
        )
        completed = set(record.completed_item_keys)
        return tuple(item for item in expected if item not in completed)

    def _write_state(self, record: JobRecord) -> None:
        _utc(record.updated_at)
        _atomic_json(
            self._state_path(record.job_id),
            self._state_payload(record),
        )


class DataDirectoryLock:
    def __init__(self, data_directory: Path) -> None:
        self.path = data_directory / ".collector.lock"
        self._handle: BinaryIO | None = None
        self.acquired = False

    def acquire(self) -> None:
        if self.acquired:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                unix_lock: Any = fcntl
                unix_lock.flock(
                    handle.fileno(),
                    unix_lock.LOCK_EX | unix_lock.LOCK_NB,
                )
        except OSError as exc:
            handle.close()
            raise DataDirectoryLocked(
                "Collector data directory is already in use"
            ) from exc
        self._handle = handle
        self.acquired = True

    def release(self) -> None:
        if self._handle is None:
            self.acquired = False
            return
        handle = self._handle
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            unix_lock: Any = fcntl
            unix_lock.flock(handle.fileno(), unix_lock.LOCK_UN)
        handle.close()
        self._handle = None
        self.acquired = False

    def __enter__(self) -> DataDirectoryLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()
