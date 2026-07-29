from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

from geo_game_collector.contracts import CorrelationIds, ErrorCategory

_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class SpoolError(RuntimeError):
    """Base class for durable spool errors."""


class SpoolIdentityConflict(SpoolError):
    """A transport identity was reused for different immutable content."""


class InvalidSpoolTransition(SpoolError):
    """The requested local delivery state transition is unsafe."""


class ReceiptMismatch(SpoolError):
    """A terminal receipt does not match the retained archive identity."""


class SpoolState(str, Enum):
    PACKAGED = "packaged"
    TRANSFER_CREATED = "transfer_created"
    ARCHIVE_UPLOADED = "archive_uploaded"
    READY = "ready"
    PROCESSED = "processed"
    FAILED = "failed"


_NEXT_STATES: dict[SpoolState, SpoolState] = {
    SpoolState.PACKAGED: SpoolState.TRANSFER_CREATED,
    SpoolState.TRANSFER_CREATED: SpoolState.ARCHIVE_UPLOADED,
    SpoolState.ARCHIVE_UPLOADED: SpoolState.READY,
}


def _require_safe_id(field_name: str, value: str) -> None:
    if _SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"{field_name} is not a safe bounded identifier")


def _require_sha256(value: str) -> None:
    if _SHA256.fullmatch(value) is None:
        raise ValueError("archive_sha256 must be a lowercase SHA-256 digest")


def _require_utc(field_name: str, value: datetime) -> None:
    offset = value.utcoffset()
    if value.tzinfo is None or offset is None or offset.total_seconds() != 0:
        raise ValueError(f"{field_name} must be UTC")


def _utc_text(value: datetime) -> str:
    _require_utc("timestamp", value)
    return value.isoformat().replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    _require_utc("persisted timestamp", parsed)
    return parsed


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


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
class PackagedTransfer:
    correlations: CorrelationIds
    destination: str
    object_key: str
    archive_size: int
    archive_sha256: str
    bundle_schema_version: str
    created_at: datetime

    def __post_init__(self) -> None:
        for field_name in ("destination", "object_key", "bundle_schema_version"):
            value = getattr(self, field_name)
            if not value or value != value.strip():
                raise ValueError(f"{field_name} must be non-empty trimmed text")
        for field_name in (
            "collector_id",
            "job_id",
            "run_id",
            "bundle_id",
            "transport_id",
        ):
            _require_safe_id(field_name, getattr(self.correlations, field_name))
        if self.archive_size <= 0:
            raise ValueError("archive_size must be positive")
        _require_sha256(self.archive_sha256)
        _require_utc("created_at", self.created_at)


@dataclass(frozen=True, slots=True)
class SpoolRecord:
    transfer: PackagedTransfer
    state: SpoolState
    attempt_count: int
    updated_at: datetime
    next_attempt_at: datetime | None = None
    retry_blocked: bool = False
    last_error_category: ErrorCategory | None = None
    last_error_summary: str | None = None
    receipt_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.attempt_count < 0:
            raise ValueError("attempt_count must be non-negative")
        _require_utc("updated_at", self.updated_at)
        if self.next_attempt_at is not None:
            _require_utc("next_attempt_at", self.next_attempt_at)
        if self.receipt_at is not None:
            _require_utc("receipt_at", self.receipt_at)


@dataclass(frozen=True, slots=True)
class TerminalReceipt:
    collector_id: str
    transport_id: str
    archive_sha256: str
    status: SpoolState
    completed_at: datetime
    error_category: ErrorCategory | None = None
    error_summary: str | None = None

    def __post_init__(self) -> None:
        _require_safe_id("collector_id", self.collector_id)
        _require_safe_id("transport_id", self.transport_id)
        _require_sha256(self.archive_sha256)
        if self.status not in (SpoolState.PROCESSED, SpoolState.FAILED):
            raise ValueError("receipt status must be processed or failed")
        if self.status is SpoolState.FAILED and self.error_category is None:
            raise ValueError("failed receipt requires error_category")
        if self.status is SpoolState.PROCESSED and self.error_category is not None:
            raise ValueError("processed receipt cannot carry error_category")
        _require_utc("completed_at", self.completed_at)


class SpoolStore:
    """Filesystem-backed source of truth for immutable outbound archives."""

    def __init__(self, root: Path, *, max_entries: int, max_bytes: int) -> None:
        if max_entries <= 0 or max_bytes <= 0:
            raise ValueError("spool limits must be positive")
        self.root = root
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.entries_dir = root / "transfers"

    def _entry_dir(self, transport_id: str) -> Path:
        _require_safe_id("transport_id", transport_id)
        return self.entries_dir / transport_id

    def _metadata_path(self, transport_id: str) -> Path:
        return self._entry_dir(transport_id) / "metadata.json"

    def _state_path(self, transport_id: str) -> Path:
        return self._entry_dir(transport_id) / "state.json"

    def archive_path(self, transport_id: str) -> Path:
        return self._entry_dir(transport_id) / "archive.bin"

    def create_packaged(
        self, transfer: PackagedTransfer, source_archive: Path
    ) -> SpoolRecord:
        existing_path = self._metadata_path(transfer.correlations.transport_id)
        if existing_path.exists():
            existing = self.load(transfer.correlations.transport_id)
            if existing.transfer != transfer:
                raise SpoolIdentityConflict(
                    "transport_id already belongs to different immutable fields"
                )
            self.verify_archive(existing)
            return existing

        entry_dir = self._entry_dir(transfer.correlations.transport_id)
        if entry_dir.exists():
            quarantine = (
                self.root
                / "quarantine"
                / f"{transfer.correlations.transport_id}-{uuid.uuid4().hex}"
            )
            quarantine.parent.mkdir(parents=True, exist_ok=True)
            os.replace(entry_dir, quarantine)

        if not source_archive.is_file():
            raise FileNotFoundError(source_archive)
        if source_archive.stat().st_size != transfer.archive_size:
            raise SpoolIdentityConflict("source archive size differs from declaration")
        if _sha256_file(source_archive) != transfer.archive_sha256:
            raise SpoolIdentityConflict("source archive hash differs from declaration")
        if not self.can_accept(transfer.archive_size):
            raise SpoolError("spool high-water limit reached")

        entry_dir.mkdir(parents=True, exist_ok=False)
        archive_path = self.archive_path(transfer.correlations.transport_id)
        temporary = entry_dir / ".archive.bin.tmp"
        try:
            with source_archive.open("rb") as source, temporary.open("xb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, archive_path)
            self.verify_archive(
                SpoolRecord(
                    transfer=transfer,
                    state=SpoolState.PACKAGED,
                    attempt_count=0,
                    updated_at=transfer.created_at,
                )
            )
            record = SpoolRecord(
                transfer=transfer,
                state=SpoolState.PACKAGED,
                attempt_count=0,
                updated_at=transfer.created_at,
            )
            self._write_state(record)
            _atomic_json(existing_path, self._metadata_payload(transfer))
            return record
        except BaseException:
            temporary.unlink(missing_ok=True)
            if not existing_path.exists():
                shutil.rmtree(entry_dir, ignore_errors=True)
            raise

    def load(self, transport_id: str) -> SpoolRecord:
        metadata = json.loads(self._metadata_path(transport_id).read_text("utf-8"))
        state = json.loads(self._state_path(transport_id).read_text("utf-8"))
        correlations = CorrelationIds(**metadata["correlations"])
        transfer = PackagedTransfer(
            correlations=correlations,
            destination=metadata["destination"],
            object_key=metadata["object_key"],
            archive_size=metadata["archive_size"],
            archive_sha256=metadata["archive_sha256"],
            bundle_schema_version=metadata["bundle_schema_version"],
            created_at=_parse_utc(metadata["created_at"]),
        )
        return SpoolRecord(
            transfer=transfer,
            state=SpoolState(state["state"]),
            attempt_count=state["attempt_count"],
            updated_at=_parse_utc(state["updated_at"]),
            next_attempt_at=(
                _parse_utc(state["next_attempt_at"])
                if state["next_attempt_at"] is not None
                else None
            ),
            retry_blocked=bool(state.get("retry_blocked", False)),
            last_error_category=(
                ErrorCategory(state["last_error_category"])
                if state["last_error_category"] is not None
                else None
            ),
            last_error_summary=state["last_error_summary"],
            receipt_at=(
                _parse_utc(state["receipt_at"])
                if state["receipt_at"] is not None
                else None
            ),
        )

    def verify_archive(self, record: SpoolRecord) -> None:
        archive = self.archive_path(record.transfer.correlations.transport_id)
        if not archive.is_file():
            raise SpoolIdentityConflict("retained archive is missing")
        if archive.stat().st_size != record.transfer.archive_size:
            raise SpoolIdentityConflict("retained archive size changed")
        if _sha256_file(archive) != record.transfer.archive_sha256:
            raise SpoolIdentityConflict("retained archive hash changed")

    def advance(
        self, transport_id: str, target: SpoolState, *, now: datetime
    ) -> SpoolRecord:
        _require_utc("now", now)
        record = self.load(transport_id)
        if target is record.state:
            return record
        if target in (SpoolState.PROCESSED, SpoolState.FAILED):
            raise InvalidSpoolTransition("terminal state requires a matching receipt")
        if _NEXT_STATES.get(record.state) is not target:
            raise InvalidSpoolTransition(
                f"cannot advance {record.state.value} to {target.value}"
            )
        self.verify_archive(record)
        updated = replace(
            record,
            state=target,
            updated_at=now,
            next_attempt_at=None,
            retry_blocked=False,
            last_error_category=None,
            last_error_summary=None,
        )
        self._write_state(updated)
        return updated

    def defer_retry(
        self,
        transport_id: str,
        *,
        now: datetime,
        delay: timedelta,
        summary: str,
    ) -> SpoolRecord:
        _require_utc("now", now)
        if delay.total_seconds() <= 0:
            raise ValueError("retry delay must be positive")
        record = self.load(transport_id)
        if record.state in (SpoolState.PROCESSED, SpoolState.FAILED):
            raise InvalidSpoolTransition("terminal transfer cannot be retried")
        self.verify_archive(record)
        updated = replace(
            record,
            attempt_count=record.attempt_count + 1,
            updated_at=now,
            next_attempt_at=now + delay,
            retry_blocked=False,
            last_error_category=ErrorCategory.RETRYABLE,
            last_error_summary=summary,
        )
        self._write_state(updated)
        return updated

    def stop_retry(
        self,
        transport_id: str,
        *,
        now: datetime,
        category: ErrorCategory,
        summary: str,
    ) -> SpoolRecord:
        _require_utc("now", now)
        if category is ErrorCategory.RETRYABLE:
            raise ValueError("retryable failures must use defer_retry")
        record = self.load(transport_id)
        if record.state in (SpoolState.PROCESSED, SpoolState.FAILED):
            raise InvalidSpoolTransition("terminal transfer cannot be stopped")
        self.verify_archive(record)
        updated = replace(
            record,
            attempt_count=record.attempt_count + 1,
            updated_at=now,
            next_attempt_at=None,
            retry_blocked=True,
            last_error_category=category,
            last_error_summary=summary,
        )
        self._write_state(updated)
        return updated

    def apply_receipt(self, receipt: TerminalReceipt) -> SpoolRecord:
        record = self.load(receipt.transport_id)
        transfer = record.transfer
        if (
            receipt.collector_id != transfer.correlations.collector_id
            or receipt.archive_sha256 != transfer.archive_sha256
        ):
            raise ReceiptMismatch("receipt does not match retained archive identity")
        if record.state in (SpoolState.PROCESSED, SpoolState.FAILED):
            if (
                record.state is receipt.status
                and record.receipt_at == receipt.completed_at
            ):
                return record
            raise ReceiptMismatch(
                "terminal receipt conflicts with existing terminal state"
            )
        self.verify_archive(record)
        updated = replace(
            record,
            state=receipt.status,
            updated_at=receipt.completed_at,
            next_attempt_at=None,
            retry_blocked=False,
            last_error_category=receipt.error_category,
            last_error_summary=receipt.error_summary,
            receipt_at=receipt.completed_at,
        )
        self._write_state(updated)
        return updated

    def list_due(self, *, now: datetime) -> tuple[SpoolRecord, ...]:
        _require_utc("now", now)
        records = (
            self.load(path.name) for path in self.entries_dir.glob("*") if path.is_dir()
        )
        due = [
            record
            for record in records
            if record.state not in (SpoolState.PROCESSED, SpoolState.FAILED)
            and not record.retry_blocked
            and (record.next_attempt_at is None or record.next_attempt_at <= now)
        ]
        return tuple(
            sorted(
                due,
                key=lambda item: (
                    item.transfer.created_at,
                    item.transfer.correlations.transport_id,
                ),
            )
        )

    def pending_count(self) -> int:
        if not self.entries_dir.exists():
            return 0
        return sum(
            self.load(path.name).state not in (SpoolState.PROCESSED, SpoolState.FAILED)
            for path in self.entries_dir.iterdir()
            if path.is_dir()
        )

    def metrics(self) -> dict[str, int]:
        if not self.entries_dir.exists():
            return {
                "entries": 0,
                "pending": 0,
                "terminal": 0,
                "bytes": 0,
            }
        records = tuple(
            self.load(path.name) for path in self.entries_dir.iterdir() if path.is_dir()
        )
        pending = sum(
            record.state not in (SpoolState.PROCESSED, SpoolState.FAILED)
            for record in records
        )
        return {
            "entries": len(records),
            "pending": pending,
            "terminal": len(records) - pending,
            "bytes": sum(record.transfer.archive_size for record in records),
        }

    def cleanup_candidates(
        self, *, now: datetime, processed_grace: timedelta
    ) -> tuple[SpoolRecord, ...]:
        _require_utc("now", now)
        if processed_grace.total_seconds() < 0:
            raise ValueError("processed_grace must be non-negative")
        if not self.entries_dir.exists():
            return ()
        result = []
        for path in self.entries_dir.iterdir():
            if not path.is_dir():
                continue
            record = self.load(path.name)
            if (
                record.state is SpoolState.PROCESSED
                and record.receipt_at is not None
                and record.receipt_at + processed_grace <= now
            ):
                result.append(record)
        return tuple(
            sorted(
                result,
                key=lambda item: (
                    item.receipt_at or item.updated_at,
                    item.transfer.correlations.transport_id,
                ),
            )
        )

    def cleanup_processed(
        self, *, now: datetime, processed_grace: timedelta
    ) -> tuple[str, ...]:
        """Delete only receipt-verified processed archives after their grace period.

        Each directory is first moved out of the live transfer namespace.  A
        crash can therefore leave only an isolated cleanup directory, never a
        half-deleted live transfer that could be retried or counted as pending.
        """

        cleanup_root = self.root / ".cleanup"
        # Entries can only reach this directory after a matching processed
        # receipt and grace-period check. Finish an interrupted prior cleanup
        # before selecting new candidates.
        if cleanup_root.exists():
            for stale in cleanup_root.iterdir():
                if stale.is_dir():
                    shutil.rmtree(stale)
                else:
                    stale.unlink()

        removed: list[str] = []
        for candidate in self.cleanup_candidates(
            now=now,
            processed_grace=processed_grace,
        ):
            transport_id = candidate.transfer.correlations.transport_id
            entry_dir = self._entry_dir(transport_id)
            if not entry_dir.exists():
                continue
            # Re-read immediately before the destructive move so a stale
            # candidate can never delete a non-terminal or mismatched entry.
            current = self.load(transport_id)
            if (
                current.state is not SpoolState.PROCESSED
                or current.receipt_at is None
                or current.receipt_at + processed_grace > now
                or current.transfer.archive_sha256 != candidate.transfer.archive_sha256
            ):
                continue
            cleanup_root.mkdir(parents=True, exist_ok=True)
            isolated = cleanup_root / f"{transport_id}-{uuid.uuid4().hex}"
            os.replace(entry_dir, isolated)
            shutil.rmtree(isolated)
            removed.append(transport_id)
        return tuple(removed)

    def available_disk_bytes(self) -> int:
        """Return physical free bytes for the filesystem containing the spool."""

        self.root.mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(self.root).free

    def can_reserve(self, archive_size: int, *, min_free_bytes: int = 0) -> bool:
        """Check both logical spool limits and physical disk headroom."""

        if min_free_bytes < 0:
            raise ValueError("min_free_bytes must be non-negative")
        return self.can_accept(archive_size) and (
            self.available_disk_bytes() - archive_size >= min_free_bytes
        )

    def can_accept(self, archive_size: int) -> bool:
        if archive_size <= 0:
            raise ValueError("archive_size must be positive")
        if not self.entries_dir.exists():
            return archive_size <= self.max_bytes
        records = [
            self.load(path.name) for path in self.entries_dir.iterdir() if path.is_dir()
        ]
        return (
            len(records) < self.max_entries
            and sum(record.transfer.archive_size for record in records) + archive_size
            <= self.max_bytes
        )

    @staticmethod
    def _metadata_payload(transfer: PackagedTransfer) -> dict[str, Any]:
        return {
            "correlations": {
                field: getattr(transfer.correlations, field)
                for field in (
                    "collector_id",
                    "job_id",
                    "run_id",
                    "bundle_id",
                    "transport_id",
                )
            },
            "destination": transfer.destination,
            "object_key": transfer.object_key,
            "archive_size": transfer.archive_size,
            "archive_sha256": transfer.archive_sha256,
            "bundle_schema_version": transfer.bundle_schema_version,
            "created_at": _utc_text(transfer.created_at),
        }

    def _write_state(self, record: SpoolRecord) -> None:
        _atomic_json(
            self._state_path(record.transfer.correlations.transport_id),
            {
                "state": record.state.value,
                "attempt_count": record.attempt_count,
                "updated_at": _utc_text(record.updated_at),
                "next_attempt_at": (
                    _utc_text(record.next_attempt_at)
                    if record.next_attempt_at is not None
                    else None
                ),
                "retry_blocked": record.retry_blocked,
                "last_error_category": (
                    record.last_error_category.value
                    if record.last_error_category is not None
                    else None
                ),
                "last_error_summary": record.last_error_summary,
                "receipt_at": (
                    _utc_text(record.receipt_at)
                    if record.receipt_at is not None
                    else None
                ),
            },
        )
