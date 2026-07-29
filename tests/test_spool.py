from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from geo_game_collector.contracts import CorrelationIds, ErrorCategory
from geo_game_collector.spool import (
    InvalidSpoolTransition,
    PackagedTransfer,
    ReceiptMismatch,
    SpoolError,
    SpoolIdentityConflict,
    SpoolState,
    SpoolStore,
    TerminalReceipt,
)

NOW = datetime(2026, 7, 29, 2, 0, tzinfo=timezone.utc)


def _archive(tmp_path: Path, name: str, content: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(content)
    return path


def _transfer(content: bytes, *, transport_id: str = "transport-1") -> PackagedTransfer:
    return PackagedTransfer(
        correlations=CorrelationIds(
            collector_id="collector-local-1",
            job_id="job-1",
            run_id="run-1",
            bundle_id="bundle-1",
            transport_id=transport_id,
        ),
        destination="geo-production",
        object_key=f"incoming/collector-local-1/{transport_id}.tar.zst",
        archive_size=len(content),
        archive_sha256=hashlib.sha256(content).hexdigest(),
        bundle_schema_version="2",
        created_at=NOW,
    )


def _store(
    tmp_path: Path, *, max_entries: int = 10, max_bytes: int = 10_000
) -> SpoolStore:
    return SpoolStore(
        tmp_path / "spool",
        max_entries=max_entries,
        max_bytes=max_bytes,
    )


def test_packaged_archive_is_copied_verified_and_survives_restart(
    tmp_path: Path,
) -> None:
    content = b"immutable bundle bytes"
    source = _archive(tmp_path, "source.tar.zst", content)
    transfer = _transfer(content)
    store = _store(tmp_path)

    created = store.create_packaged(transfer, source)
    source.unlink()

    restarted = _store(tmp_path)
    loaded = restarted.load("transport-1")
    restarted.verify_archive(loaded)
    assert created == loaded
    assert restarted.pending_count() == 1
    assert restarted.metrics() == {
        "entries": 1,
        "pending": 1,
        "terminal": 0,
        "bytes": len(content),
    }
    assert restarted.archive_path("transport-1").read_bytes() == content


def test_partial_uncommitted_entry_is_quarantined_before_restart_recovery(
    tmp_path: Path,
) -> None:
    content = b"recovered archive"
    source = _archive(tmp_path, "source.tar.zst", content)
    store = _store(tmp_path)
    partial = store.entries_dir / "transport-1"
    partial.mkdir(parents=True)
    (partial / "archive.bin").write_bytes(b"partial")

    created = store.create_packaged(_transfer(content), source)

    assert created.state is SpoolState.PACKAGED
    assert store.archive_path("transport-1").read_bytes() == content
    quarantined = tuple((store.root / "quarantine").glob("transport-1-*"))
    assert len(quarantined) == 1
    assert (quarantined[0] / "archive.bin").read_bytes() == b"partial"


def test_repeated_create_is_idempotent_but_identity_reuse_is_rejected(
    tmp_path: Path,
) -> None:
    content = b"same bytes"
    source = _archive(tmp_path, "source.tar.zst", content)
    store = _store(tmp_path)
    transfer = _transfer(content)

    first = store.create_packaged(transfer, source)
    second = store.create_packaged(transfer, source)
    assert first == second

    different = _transfer(b"different bytes")
    different_source = _archive(tmp_path, "different.tar.zst", b"different bytes")
    with pytest.raises(SpoolIdentityConflict, match="immutable"):
        store.create_packaged(different, different_source)


def test_state_progress_is_durable_ordered_and_never_repackages(
    tmp_path: Path,
) -> None:
    content = b"archive"
    store = _store(tmp_path)
    record = store.create_packaged(
        _transfer(content), _archive(tmp_path, "source.tar.zst", content)
    )
    archive_mtime = store.archive_path("transport-1").stat().st_mtime_ns

    for target in (
        SpoolState.TRANSFER_CREATED,
        SpoolState.ARCHIVE_UPLOADED,
        SpoolState.READY,
    ):
        record = store.advance(
            "transport-1", target, now=record.updated_at + timedelta(seconds=1)
        )
        assert record.state is target

    assert store.advance("transport-1", SpoolState.READY, now=NOW) == record
    assert store.archive_path("transport-1").stat().st_mtime_ns == archive_mtime
    with pytest.raises(InvalidSpoolTransition):
        store.advance("transport-1", SpoolState.TRANSFER_CREATED, now=NOW)
    with pytest.raises(InvalidSpoolTransition, match="receipt"):
        store.advance("transport-1", SpoolState.PROCESSED, now=NOW)


def test_retry_keeps_stage_and_archive_then_drains_oldest_due_first(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    for index, created_at in ((1, NOW), (2, NOW + timedelta(seconds=1))):
        content = f"archive-{index}".encode()
        source = _archive(tmp_path, f"{index}.tar.zst", content)
        transfer = _transfer(content, transport_id=f"transport-{index}")
        transfer = PackagedTransfer(
            correlations=transfer.correlations,
            destination=transfer.destination,
            object_key=transfer.object_key,
            archive_size=transfer.archive_size,
            archive_sha256=transfer.archive_sha256,
            bundle_schema_version=transfer.bundle_schema_version,
            created_at=created_at,
        )
        store.create_packaged(transfer, source)

    deferred = store.defer_retry(
        "transport-1",
        now=NOW,
        delay=timedelta(minutes=5),
        summary="gateway timeout",
    )
    assert deferred.state is SpoolState.PACKAGED
    assert deferred.attempt_count == 1
    assert deferred.last_error_category is ErrorCategory.RETRYABLE
    assert [
        item.transfer.correlations.transport_id for item in store.list_due(now=NOW)
    ] == ["transport-2"]
    assert [
        item.transfer.correlations.transport_id
        for item in store.list_due(now=NOW + timedelta(minutes=5))
    ] == ["transport-1", "transport-2"]


def test_permanent_transport_error_stops_automatic_drain_without_deleting_archive(
    tmp_path: Path,
) -> None:
    content = b"archive"
    store = _store(tmp_path)
    store.create_packaged(
        _transfer(content), _archive(tmp_path, "source.tar.zst", content)
    )

    stopped = store.stop_retry(
        "transport-1",
        now=NOW,
        category=ErrorCategory.SECURITY,
        summary="authorization rejected",
    )

    assert stopped.retry_blocked is True
    assert stopped.state is SpoolState.PACKAGED
    assert stopped.last_error_category is ErrorCategory.SECURITY
    assert store.list_due(now=NOW + timedelta(days=365)) == ()
    assert store.archive_path("transport-1").read_bytes() == content


def test_only_matching_processed_receipt_allows_cleanup(tmp_path: Path) -> None:
    content = b"archive"
    transfer = _transfer(content)
    store = _store(tmp_path)
    store.create_packaged(transfer, _archive(tmp_path, "source.tar.zst", content))

    with pytest.raises(ReceiptMismatch):
        store.apply_receipt(
            TerminalReceipt(
                collector_id="collector-local-1",
                transport_id="transport-1",
                archive_sha256="0" * 64,
                status=SpoolState.PROCESSED,
                completed_at=NOW + timedelta(minutes=1),
            )
        )
    assert (
        store.cleanup_candidates(
            now=NOW + timedelta(days=1), processed_grace=timedelta()
        )
        == ()
    )

    receipt = TerminalReceipt(
        collector_id="collector-local-1",
        transport_id="transport-1",
        archive_sha256=transfer.archive_sha256,
        status=SpoolState.PROCESSED,
        completed_at=NOW + timedelta(minutes=1),
    )
    processed = store.apply_receipt(receipt)
    assert store.apply_receipt(receipt) == processed
    assert (
        store.cleanup_candidates(
            now=receipt.completed_at + timedelta(hours=1),
            processed_grace=timedelta(hours=2),
        )
        == ()
    )
    assert store.cleanup_candidates(
        now=receipt.completed_at + timedelta(hours=2),
        processed_grace=timedelta(hours=2),
    ) == (processed,)


def test_failed_receipt_and_unacknowledged_archives_are_never_cleanup_candidates(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    for index in (1, 2):
        content = f"archive-{index}".encode()
        store.create_packaged(
            _transfer(content, transport_id=f"transport-{index}"),
            _archive(tmp_path, f"{index}.tar.zst", content),
        )
    transfer = store.load("transport-2").transfer
    failed = store.apply_receipt(
        TerminalReceipt(
            collector_id=transfer.correlations.collector_id,
            transport_id=transfer.correlations.transport_id,
            archive_sha256=transfer.archive_sha256,
            status=SpoolState.FAILED,
            completed_at=NOW + timedelta(minutes=1),
            error_category=ErrorCategory.PERMANENT,
            error_summary="schema rejected",
        )
    )
    assert failed.state is SpoolState.FAILED
    assert (
        store.cleanup_candidates(
            now=NOW + timedelta(days=30), processed_grace=timedelta()
        )
        == ()
    )


def test_high_water_blocks_new_collection_without_discarding_backlog(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path, max_entries=1, max_bytes=20)
    content = b"first"
    store.create_packaged(
        _transfer(content), _archive(tmp_path, "first.tar.zst", content)
    )
    second = b"second"
    assert not store.can_accept(len(second))
    with pytest.raises(SpoolError, match="high-water"):
        store.create_packaged(
            _transfer(second, transport_id="transport-2"),
            _archive(tmp_path, "second.tar.zst", second),
        )
    assert store.archive_path("transport-1").read_bytes() == content


def test_receipt_gated_cleanup_frees_capacity_after_grace(tmp_path: Path) -> None:
    store = _store(tmp_path, max_entries=1, max_bytes=20)
    content = b"first"
    transfer = _transfer(content)
    store.create_packaged(transfer, _archive(tmp_path, "first.tar.zst", content))
    receipt = TerminalReceipt(
        collector_id=transfer.correlations.collector_id,
        transport_id=transfer.correlations.transport_id,
        archive_sha256=transfer.archive_sha256,
        status=SpoolState.PROCESSED,
        completed_at=NOW,
    )
    store.apply_receipt(receipt)

    assert (
        store.cleanup_processed(
            now=NOW + timedelta(minutes=59),
            processed_grace=timedelta(hours=1),
        )
        == ()
    )
    assert not store.can_accept(len(b"second"))
    assert store.cleanup_processed(
        now=NOW + timedelta(hours=1),
        processed_grace=timedelta(hours=1),
    ) == ("transport-1",)
    assert store.metrics() == {
        "entries": 0,
        "pending": 0,
        "terminal": 0,
        "bytes": 0,
    }
    assert store.can_reserve(len(b"second"))


def test_failed_and_unacknowledged_entries_are_never_deleted_by_cleanup(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    for index in (1, 2):
        content = f"archive-{index}".encode()
        store.create_packaged(
            _transfer(content, transport_id=f"transport-{index}"),
            _archive(tmp_path, f"{index}.tar.zst", content),
        )
    failed_transfer = store.load("transport-2").transfer
    store.apply_receipt(
        TerminalReceipt(
            collector_id=failed_transfer.correlations.collector_id,
            transport_id=failed_transfer.correlations.transport_id,
            archive_sha256=failed_transfer.archive_sha256,
            status=SpoolState.FAILED,
            completed_at=NOW,
            error_category=ErrorCategory.PERMANENT,
            error_summary="schema rejected",
        )
    )

    assert (
        store.cleanup_processed(
            now=NOW + timedelta(days=365),
            processed_grace=timedelta(),
        )
        == ()
    )
    assert store.archive_path("transport-1").is_file()
    assert store.archive_path("transport-2").is_file()


def test_disk_reservation_respects_configured_headroom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    monkeypatch.setattr(store, "available_disk_bytes", lambda: 100)

    assert store.can_reserve(40, min_free_bytes=60)
    assert not store.can_reserve(41, min_free_bytes=60)
    with pytest.raises(ValueError, match="min_free_bytes"):
        store.can_reserve(1, min_free_bytes=-1)
