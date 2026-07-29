from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from geo_game_collector.contracts import CorrelationIds, ErrorCategory
from geo_game_collector.spool import (
    PackagedTransfer,
    SpoolState,
    SpoolStore,
)
from geo_game_collector.transport import (
    GatewayHttpError,
    RemoteTransfer,
    RetryDisposition,
    RetryPolicy,
    TransferDrain,
    TransferIdentityError,
    TransferReceipt,
    UploadAuthorization,
    UploadAuthorizationExpired,
    classify_transport_error,
)

NOW = datetime(2026, 7, 29, 7, 0, tzinfo=timezone.utc)


def _store(tmp_path: Path) -> SpoolStore:
    return SpoolStore(tmp_path / "spool", max_entries=10, max_bytes=10_000)


def _packaged(tmp_path: Path, store: SpoolStore) -> PackagedTransfer:
    content = b"immutable transport archive"
    source = tmp_path / "bundle.tar.zst"
    source.write_bytes(content)
    transfer = PackagedTransfer(
        correlations=CorrelationIds(
            collector_id="collector-local-1",
            job_id="job-1",
            run_id="run-1",
            bundle_id="bundle-1",
            transport_id="transport-1",
        ),
        destination="geo-production",
        object_key="incoming/collector-local-1/transport-1.tar.zst",
        archive_size=len(content),
        archive_sha256=hashlib.sha256(content).hexdigest(),
        bundle_schema_version="2",
        created_at=NOW,
    )
    store.create_packaged(transfer, source)
    return transfer


@dataclass
class _Gateway:
    transfer: PackagedTransfer
    create_states: list[str]
    complete_states: list[str]
    status_states: list[RemoteTransfer]

    def __post_init__(self) -> None:
        self.create_calls = 0
        self.renew_calls = 0
        self.complete_calls = 0
        self.status_calls = 0

    def _remote(self, state: str, *, upload: bool = False) -> RemoteTransfer:
        return RemoteTransfer(
            transport_id=self.transfer.correlations.transport_id,
            state=state,
            object_key=self.transfer.object_key,
            archive_sha256=self.transfer.archive_sha256,
            upload=(
                UploadAuthorization(
                    object_key=self.transfer.object_key,
                    upload_url="https://inbox.invalid/signed",
                    required_size=self.transfer.archive_size,
                    required_sha256=self.transfer.archive_sha256,
                    required_headers={
                        "x-amz-meta-sha256": self.transfer.archive_sha256
                    },
                )
                if upload
                else None
            ),
        )

    def create_transfer(self, transfer: PackagedTransfer) -> RemoteTransfer:
        self.create_calls += 1
        state = self.create_states.pop(0) if self.create_states else "created"
        return self._remote(state, upload=state in {"created", "archive_uploaded"})

    def renew_upload_authorization(self, transport_id: str) -> UploadAuthorization:
        self.renew_calls += 1
        return self._remote("created", upload=True).upload  # type: ignore[return-value]

    def complete_transfer(self, transport_id: str) -> RemoteTransfer:
        self.complete_calls += 1
        state = self.complete_states.pop(0) if self.complete_states else "ready"
        return self._remote(state)

    def get_transfer_status(self, transport_id: str) -> RemoteTransfer:
        self.status_calls += 1
        return self.status_states.pop(0)


class _Uploader:
    def __init__(self, actions: list[BaseException | None] | None = None) -> None:
        self.actions = list(actions or [None])
        self.calls: list[tuple[str, Path, dict[str, str]]] = []

    def upload(self, authorization: UploadAuthorization, archive: Path) -> None:
        self.calls.append(
            (
                authorization.upload_url,
                archive,
                dict(authorization.required_headers),
            )
        )
        action = self.actions.pop(0)
        if action is not None:
            raise action


def test_full_transfer_state_machine_ends_only_on_matching_processed_receipt(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    transfer = _packaged(tmp_path, store)
    receipt = TransferReceipt(
        status="processed",
        archive_sha256=transfer.archive_sha256,
        completed_at=NOW + timedelta(minutes=1),
    )
    gateway = _Gateway(
        transfer=transfer,
        create_states=["created"],
        complete_states=["ready"],
        status_states=[
            RemoteTransfer(
                transport_id="transport-1",
                state="processed",
                object_key=transfer.object_key,
                archive_sha256=transfer.archive_sha256,
                receipt=receipt,
            )
        ],
    )
    uploader = _Uploader()
    drain = TransferDrain(store=store, gateway=gateway, uploader=uploader)

    result = drain.drain_one("transport-1", now=NOW)

    assert result.state is SpoolState.PROCESSED
    assert gateway.create_calls == 1
    assert gateway.complete_calls == 1
    assert gateway.status_calls == 1
    assert len(uploader.calls) == 1
    assert store.archive_path("transport-1").is_file()


def test_unknown_remote_dead_letter_classification_becomes_terminal_failure(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    transfer = _packaged(tmp_path, store)
    receipt = TransferReceipt(
        status="dead_letter",
        archive_sha256=transfer.archive_sha256,
        completed_at=NOW + timedelta(minutes=1),
        error_classification="identity",
        error_summary="remote identity conflict",
    )
    gateway = _Gateway(
        transfer=transfer,
        create_states=["created"],
        complete_states=["ready"],
        status_states=[
            RemoteTransfer(
                transport_id="transport-1",
                state="dead_letter",
                object_key=transfer.object_key,
                archive_sha256=transfer.archive_sha256,
                receipt=receipt,
            )
        ],
    )

    result = TransferDrain(
        store=store,
        gateway=gateway,
        uploader=_Uploader(),
    ).drain_one("transport-1", now=NOW)

    assert result.state is SpoolState.FAILED
    assert result.retry_blocked is False
    assert result.last_error_category is ErrorCategory.PERMANENT
    assert result.last_error_summary == (
        "remote classification=identity; remote identity conflict"
    )


def test_interrupted_upload_resumes_same_identity_without_recollecting(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    transfer = _packaged(tmp_path, store)
    archive = store.archive_path("transport-1")
    original_mtime = archive.stat().st_mtime_ns
    gateway = _Gateway(
        transfer=transfer,
        create_states=["created"],
        complete_states=["ready"],
        status_states=[],
    )
    first = TransferDrain(
        store=store,
        gateway=gateway,
        uploader=_Uploader([ConnectionError("network interrupted")]),
        retry_policy=RetryPolicy(jitter_ratio=0.0, jitter=lambda: 0.0),
    )

    deferred = first.drain_one("transport-1", now=NOW)

    assert deferred.state is SpoolState.TRANSFER_CREATED
    assert deferred.next_attempt_at == NOW + timedelta(seconds=2)
    assert archive.stat().st_mtime_ns == original_mtime

    restarted = TransferDrain(
        store=_store(tmp_path),
        gateway=gateway,
        uploader=_Uploader([None]),
        retry_policy=RetryPolicy(jitter=lambda: 0.0),
    )
    resumed = restarted.drain_one(
        "transport-1",
        now=NOW + timedelta(seconds=2),
        poll_terminal=False,
    )

    assert resumed.state is SpoolState.READY
    assert gateway.create_calls == 1
    assert gateway.renew_calls == 1
    assert archive.stat().st_mtime_ns == original_mtime


def test_expired_upload_authorization_is_renewed_once_without_recreating_transfer(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    transfer = _packaged(tmp_path, store)
    gateway = _Gateway(
        transfer=transfer,
        create_states=["created"],
        complete_states=["ready"],
        status_states=[],
    )
    uploader = _Uploader([UploadAuthorizationExpired(), None])
    drain = TransferDrain(store=store, gateway=gateway, uploader=uploader)

    result = drain.drain_one("transport-1", now=NOW, poll_terminal=False)

    assert result.state is SpoolState.READY
    assert gateway.create_calls == 1
    assert gateway.renew_calls == 1
    assert len(uploader.calls) == 2


def test_remote_ready_reconciles_local_restart_without_upload_or_duplicate_complete(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    transfer = _packaged(tmp_path, store)
    gateway = _Gateway(
        transfer=transfer,
        create_states=["ready"],
        complete_states=[],
        status_states=[],
    )
    uploader = _Uploader()
    drain = TransferDrain(store=store, gateway=gateway, uploader=uploader)

    result = drain.drain_one("transport-1", now=NOW, poll_terminal=False)

    assert result.state is SpoolState.READY
    assert uploader.calls == []
    assert gateway.complete_calls == 0


def test_remote_identity_mismatch_stops_automatic_retry_and_preserves_archive(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    transfer = _packaged(tmp_path, store)
    gateway = _Gateway(
        transfer=transfer,
        create_states=["created"],
        complete_states=[],
        status_states=[],
    )
    original = gateway.create_transfer

    def wrong_identity(packaged: PackagedTransfer) -> RemoteTransfer:
        remote = original(packaged)
        return RemoteTransfer(
            transport_id=remote.transport_id,
            state=remote.state,
            object_key=remote.object_key,
            archive_sha256="f" * 64,
            upload=remote.upload,
        )

    gateway.create_transfer = wrong_identity  # type: ignore[method-assign]
    drain = TransferDrain(store=store, gateway=gateway, uploader=_Uploader())

    result = drain.drain_one("transport-1", now=NOW)

    assert result.retry_blocked is True
    assert result.last_error_category is ErrorCategory.SECURITY
    assert store.archive_path("transport-1").is_file()


@pytest.mark.parametrize(
    ("error", "disposition", "category"),
    [
        (TimeoutError(), RetryDisposition.RETRY, ErrorCategory.RETRYABLE),
        (ConnectionError(), RetryDisposition.RETRY, ErrorCategory.RETRYABLE),
        (
            GatewayHttpError(503, "server unavailable"),
            RetryDisposition.RETRY,
            ErrorCategory.RETRYABLE,
        ),
        (
            GatewayHttpError(401, "unauthorized"),
            RetryDisposition.STOP,
            ErrorCategory.SECURITY,
        ),
        (
            GatewayHttpError(409, "identity conflict"),
            RetryDisposition.STOP,
            ErrorCategory.PERMANENT,
        ),
        (
            TransferIdentityError("hash mismatch"),
            RetryDisposition.STOP,
            ErrorCategory.SECURITY,
        ),
    ],
)
def test_retry_classification_is_finite_and_security_failures_stop(
    error, disposition, category
) -> None:
    classified = classify_transport_error(error)
    assert classified.disposition is disposition
    assert classified.category is category


def test_exponential_backoff_is_capped_and_jitter_is_bounded() -> None:
    low = RetryPolicy(
        base_delay=timedelta(seconds=2),
        max_delay=timedelta(seconds=10),
        jitter_ratio=0.25,
        jitter=lambda: 0.0,
    )
    high = RetryPolicy(
        base_delay=timedelta(seconds=2),
        max_delay=timedelta(seconds=10),
        jitter_ratio=0.25,
        jitter=lambda: 1.0,
    )

    assert low.delay_for(1) == timedelta(seconds=1.5)
    assert high.delay_for(1) == timedelta(seconds=2.5)
    assert low.delay_for(99) == timedelta(seconds=7.5)
    assert high.delay_for(99) == timedelta(seconds=12.5)
