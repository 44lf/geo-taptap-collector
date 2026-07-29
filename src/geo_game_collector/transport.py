"""Restart-safe Collector transfer orchestration and scoped HTTP clients."""

from __future__ import annotations

import hashlib
import json
import random
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from geo_game_collector.contracts import ErrorCategory
from geo_game_collector.spool import (
    PackagedTransfer,
    SpoolRecord,
    SpoolState,
    SpoolStore,
    TerminalReceipt,
)


class GatewayHttpError(RuntimeError):
    def __init__(self, status: int, detail: str = "") -> None:
        super().__init__(f"gateway HTTP {status}")
        self.status = status
        self.detail = detail


class UploadAuthorizationExpired(RuntimeError):
    pass


class TransferIdentityError(RuntimeError):
    pass


class _RejectGatewayRedirects(HTTPRedirectHandler):
    def redirect_request(
        self,
        request: Request,
        response: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> Request:
        del response, new_url
        raise HTTPError(
            request.full_url,
            code,
            f"Collector Gateway redirect rejected: {message}",
            headers,
            None,
        )


_GATEWAY_OPENER = build_opener(_RejectGatewayRedirects()).open
_REMOTE_ERROR_CLASSIFICATION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


class RetryDisposition(str, Enum):
    RETRY = "retry"
    STOP = "stop"


@dataclass(frozen=True, slots=True)
class ClassifiedTransportError:
    disposition: RetryDisposition
    category: ErrorCategory
    summary: str


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    base_delay: timedelta = timedelta(seconds=2)
    max_delay: timedelta = timedelta(minutes=5)
    jitter_ratio: float = 0.2
    jitter: Callable[[], float] = random.random

    def __post_init__(self) -> None:
        if self.base_delay.total_seconds() <= 0:
            raise ValueError("base_delay must be positive")
        if self.max_delay < self.base_delay:
            raise ValueError("max_delay must not be smaller than base_delay")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("jitter_ratio must be between 0 and 1")

    def delay_for(self, attempt: int) -> timedelta:
        if attempt <= 0:
            raise ValueError("attempt must be positive")
        base_seconds = min(
            self.max_delay.total_seconds(),
            self.base_delay.total_seconds() * (2 ** min(attempt - 1, 30)),
        )
        random_value = min(1.0, max(0.0, self.jitter()))
        factor = 1 - self.jitter_ratio + 2 * self.jitter_ratio * random_value
        return timedelta(seconds=base_seconds * factor)


@dataclass(frozen=True, slots=True)
class UploadAuthorization:
    object_key: str
    upload_url: str
    required_size: int
    required_sha256: str
    required_headers: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class TransferReceipt:
    status: str
    archive_sha256: str
    completed_at: datetime
    error_classification: str | None = None
    error_summary: str | None = None


@dataclass(frozen=True, slots=True)
class RemoteTransfer:
    transport_id: str
    state: str
    object_key: str
    archive_sha256: str
    upload: UploadAuthorization | None = None
    receipt: TransferReceipt | None = None


@dataclass(frozen=True, slots=True)
class RemoteConfiguration:
    version: int
    unchanged: bool
    snapshot_sha256: str
    max_staleness_seconds: int
    snapshot: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class RemoteJob:
    job_id: str
    claim_request_id: str
    mode: str
    destination: str
    manifest_sha256: str
    manifest: dict[str, Any]
    status: str
    claimed_at: datetime


@dataclass(frozen=True, slots=True)
class RemoteRun:
    created: bool
    collector_id: str
    run_id: str
    job_id: str
    status: str


@dataclass(frozen=True, slots=True)
class EventBatchOutcome:
    accepted_event_ids: tuple[str, ...]
    duplicate_event_ids: tuple[str, ...]


class GatewayTransferApi(Protocol):
    def create_transfer(self, transfer: PackagedTransfer) -> RemoteTransfer: ...

    def renew_upload_authorization(self, transport_id: str) -> UploadAuthorization: ...

    def complete_transfer(self, transport_id: str) -> RemoteTransfer: ...

    def get_transfer_status(self, transport_id: str) -> RemoteTransfer: ...


class ArchiveUploader(Protocol):
    def upload(self, authorization: UploadAuthorization, archive: Path) -> None: ...


def classify_transport_error(error: BaseException) -> ClassifiedTransportError:
    if isinstance(
        error,
        (TimeoutError, ConnectionError, URLError, UploadAuthorizationExpired),
    ):
        return ClassifiedTransportError(
            RetryDisposition.RETRY,
            ErrorCategory.RETRYABLE,
            "transport connection or authorization expired",
        )
    if isinstance(error, GatewayHttpError):
        if error.status in {408, 425, 429, 500, 502, 503, 504}:
            return ClassifiedTransportError(
                RetryDisposition.RETRY,
                ErrorCategory.RETRYABLE,
                f"gateway transient HTTP {error.status}",
            )
        if error.status in {401, 403}:
            return ClassifiedTransportError(
                RetryDisposition.STOP,
                ErrorCategory.SECURITY,
                f"gateway authorization HTTP {error.status}",
            )
        return ClassifiedTransportError(
            RetryDisposition.STOP,
            ErrorCategory.PERMANENT,
            f"gateway permanent HTTP {error.status}",
        )
    if isinstance(error, TransferIdentityError):
        return ClassifiedTransportError(
            RetryDisposition.STOP,
            ErrorCategory.SECURITY,
            "remote transfer identity mismatch",
        )
    if isinstance(error, (ValueError, json.JSONDecodeError)):
        return ClassifiedTransportError(
            RetryDisposition.STOP,
            ErrorCategory.PERMANENT,
            "transfer contract validation failed",
        )
    return ClassifiedTransportError(
        RetryDisposition.STOP,
        ErrorCategory.PERMANENT,
        "unexpected permanent transport failure",
    )


def _validate_upload(
    authorization: UploadAuthorization,
    transfer: PackagedTransfer,
) -> None:
    if (
        authorization.object_key != transfer.object_key
        or authorization.required_size != transfer.archive_size
        or authorization.required_sha256 != transfer.archive_sha256
        or not authorization.upload_url
    ):
        raise TransferIdentityError("upload authorization differs from local archive")


def _validate_remote(remote: RemoteTransfer, transfer: PackagedTransfer) -> None:
    if (
        remote.transport_id != transfer.correlations.transport_id
        or remote.object_key != transfer.object_key
        or remote.archive_sha256 != transfer.archive_sha256
    ):
        raise TransferIdentityError("remote transfer differs from local archive")
    if remote.upload is not None:
        _validate_upload(remote.upload, transfer)


def _spool_receipt(
    remote: RemoteTransfer, transfer: PackagedTransfer
) -> TerminalReceipt:
    receipt = remote.receipt
    if receipt is None or receipt.archive_sha256 != transfer.archive_sha256:
        raise TransferIdentityError("terminal remote transfer lacks a matching receipt")
    if receipt.status == "processed":
        state = SpoolState.PROCESSED
        category = None
        summary = None
    elif receipt.status in {"failed", "dead_letter"}:
        state = SpoolState.FAILED
        try:
            category = ErrorCategory(receipt.error_classification or "")
        except ValueError:
            category = ErrorCategory.PERMANENT
        summary = receipt.error_summary
        if (
            receipt.error_classification is not None
            and receipt.error_classification
            not in {item.value for item in ErrorCategory}
        ):
            classification = f"remote classification={receipt.error_classification}"
            summary = f"{classification}; {summary}" if summary else classification
    else:
        raise TransferIdentityError("remote receipt has an unsupported terminal state")
    return TerminalReceipt(
        collector_id=transfer.correlations.collector_id,
        transport_id=transfer.correlations.transport_id,
        archive_sha256=transfer.archive_sha256,
        status=state,
        completed_at=receipt.completed_at,
        error_category=category,
        error_summary=summary,
    )


class TransferDrain:
    def __init__(
        self,
        *,
        store: SpoolStore,
        gateway: GatewayTransferApi,
        uploader: ArchiveUploader,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self._store = store
        self._gateway = gateway
        self._uploader = uploader
        self._retry_policy = retry_policy or RetryPolicy()

    def _advance_to_ready(
        self,
        transport_id: str,
        *,
        now: datetime,
    ) -> SpoolRecord:
        record = self._store.load(transport_id)
        next_state = {
            SpoolState.PACKAGED: SpoolState.TRANSFER_CREATED,
            SpoolState.TRANSFER_CREATED: SpoolState.ARCHIVE_UPLOADED,
            SpoolState.ARCHIVE_UPLOADED: SpoolState.READY,
        }
        while record.state is not SpoolState.READY:
            target = next_state.get(record.state)
            if target is None:
                raise TransferIdentityError("local transfer cannot reconcile to READY")
            record = self._store.advance(transport_id, target, now=now)
        return record

    def _reconcile_remote(
        self,
        remote: RemoteTransfer,
        *,
        transfer: PackagedTransfer,
        now: datetime,
    ) -> SpoolRecord | None:
        _validate_remote(remote, transfer)
        transport_id = transfer.correlations.transport_id
        if remote.state in {
            "ready",
            "processing",
            "processed",
            "failed",
            "dead_letter",
        }:
            record = self._advance_to_ready(transport_id, now=now)
            if remote.state in {"processed", "failed", "dead_letter"}:
                return self._store.apply_receipt(_spool_receipt(remote, transfer))
            return record
        if remote.state not in {"created", "archive_uploaded", "retry_wait"}:
            raise TransferIdentityError("remote transfer state is unsupported")
        return None

    def drain_one(
        self,
        transport_id: str,
        *,
        now: datetime,
        poll_terminal: bool = True,
    ) -> SpoolRecord:
        record = self._store.load(transport_id)
        if (
            record.state in {SpoolState.PROCESSED, SpoolState.FAILED}
            or record.retry_blocked
        ):
            return record
        transfer = record.transfer
        upload: UploadAuthorization | None = None
        try:
            if record.state is SpoolState.PACKAGED:
                remote = self._gateway.create_transfer(transfer)
                reconciled = self._reconcile_remote(
                    remote,
                    transfer=transfer,
                    now=now,
                )
                if reconciled is not None:
                    record = reconciled
                else:
                    upload = remote.upload
                    record = self._store.advance(
                        transport_id,
                        SpoolState.TRANSFER_CREATED,
                        now=now,
                    )

            if record.state is SpoolState.TRANSFER_CREATED:
                if upload is None:
                    upload = self._gateway.renew_upload_authorization(transport_id)
                _validate_upload(upload, transfer)
                try:
                    self._uploader.upload(
                        upload,
                        self._store.archive_path(transport_id),
                    )
                except UploadAuthorizationExpired:
                    upload = self._gateway.renew_upload_authorization(transport_id)
                    _validate_upload(upload, transfer)
                    self._uploader.upload(
                        upload,
                        self._store.archive_path(transport_id),
                    )
                record = self._store.advance(
                    transport_id,
                    SpoolState.ARCHIVE_UPLOADED,
                    now=now,
                )

            if record.state is SpoolState.ARCHIVE_UPLOADED:
                remote = self._gateway.complete_transfer(transport_id)
                reconciled = self._reconcile_remote(
                    remote,
                    transfer=transfer,
                    now=now,
                )
                if reconciled is None:
                    raise TransferIdentityError("completion did not reach READY")
                record = reconciled

            if record.state is SpoolState.READY and poll_terminal:
                remote = self._gateway.get_transfer_status(transport_id)
                reconciled = self._reconcile_remote(
                    remote,
                    transfer=transfer,
                    now=now,
                )
                if reconciled is not None:
                    record = reconciled
            return record
        except Exception as exc:
            classified = classify_transport_error(exc)
            if classified.disposition is RetryDisposition.RETRY:
                delay = self._retry_policy.delay_for(record.attempt_count + 1)
                return self._store.defer_retry(
                    transport_id,
                    now=now,
                    delay=delay,
                    summary=classified.summary,
                )
            return self._store.stop_retry(
                transport_id,
                now=now,
                category=classified.category,
                summary=classified.summary,
            )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class PresignedArchiveUploader:
    """Stream a retained archive to one pre-signed URL without logging the URL."""

    def __init__(
        self,
        *,
        timeout: float = 120.0,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        self._timeout = timeout
        self._opener = opener

    def upload(self, authorization: UploadAuthorization, archive: Path) -> None:
        if (
            archive.stat().st_size != authorization.required_size
            or _sha256_file(archive) != authorization.required_sha256
        ):
            raise TransferIdentityError(
                "local archive differs from upload authorization"
            )
        headers = {
            **dict(authorization.required_headers),
            "Content-Type": "application/zstd",
            "Content-Length": str(authorization.required_size),
        }
        try:
            with archive.open("rb") as body:
                request = Request(
                    authorization.upload_url,
                    data=body,
                    headers=headers,
                    method="PUT",
                )
                with self._opener(request, timeout=self._timeout) as response:
                    status = int(response.status)
        except HTTPError as exc:
            if exc.code == 403:
                raise UploadAuthorizationExpired() from exc
            raise GatewayHttpError(exc.code) from exc
        except (TimeoutError, URLError, OSError) as exc:
            raise ConnectionError("archive upload failed") from exc
        if status not in {200, 201, 204}:
            raise GatewayHttpError(status)


class GatewayHttpClient:
    """Small scoped JSON client for the Collector transfer endpoints."""

    def __init__(
        self,
        *,
        base_url: str,
        bearer_credential: str,
        timeout: float = 15.0,
        opener: Callable[..., Any] = _GATEWAY_OPENER,
    ) -> None:
        if not base_url.startswith("https://"):
            raise ValueError("Collector Gateway base_url must use HTTPS")
        if not bearer_credential or any(
            char in bearer_credential for char in ("\r", "\n")
        ):
            raise ValueError("bearer_credential is invalid")
        self._base_url = base_url.rstrip("/") + "/"
        self._bearer_credential = bearer_credential
        self._timeout = timeout
        self._opener = opener

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = (
            json.dumps(payload, separators=(",", ":")).encode()
            if payload is not None
            else None
        )
        request = Request(
            urljoin(self._base_url, path.lstrip("/")),
            data=data,
            headers={
                "Authorization": f"Bearer {self._bearer_credential}",
                "Accept": "application/json",
                **({"Content-Type": "application/json"} if data is not None else {}),
            },
            method=method,
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                body = response.read()
                status = int(response.status)
        except HTTPError as exc:
            raise GatewayHttpError(exc.code) from exc
        except (TimeoutError, URLError, OSError) as exc:
            raise ConnectionError("Collector Gateway request failed") from exc
        if not 200 <= status < 300:
            raise GatewayHttpError(status)
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Collector Gateway returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise ValueError("Collector Gateway response must be an object")
        return decoded

    @staticmethod
    def _upload(payload: object) -> UploadAuthorization | None:
        if payload is None:
            return None
        value = payload if isinstance(payload, dict) else {}
        headers = value.get("required_headers")
        if not isinstance(headers, dict):
            raise ValueError("upload required_headers must be an object")
        return UploadAuthorization(
            object_key=str(value.get("object_key", "")),
            upload_url=str(value.get("upload_url", "")),
            required_size=int(value.get("required_size", 0)),
            required_sha256=str(value.get("required_sha256", "")),
            required_headers={str(key): str(item) for key, item in headers.items()},
        )

    @classmethod
    def _remote(
        cls,
        payload: dict[str, Any],
        *,
        transport_id: str,
        object_key: str,
    ) -> RemoteTransfer:
        receipt_status = payload.get("receipt_status")
        receipt = None
        if receipt_status:
            completed_at = datetime.fromisoformat(
                str(payload["receipt_completed_at"]).replace("Z", "+00:00")
            )
            category = payload.get("error_classification")
            classification = str(category) if category is not None else None
            if (
                classification is not None
                and _REMOTE_ERROR_CLASSIFICATION.fullmatch(classification) is None
            ):
                raise ValueError("receipt error classification is invalid")
            receipt = TransferReceipt(
                status=str(receipt_status),
                archive_sha256=str(payload.get("archive_sha256", "")),
                completed_at=completed_at,
                error_classification=classification,
                error_summary=(
                    str(payload["error_summary"])
                    if payload.get("error_summary") is not None
                    else None
                ),
            )
        return RemoteTransfer(
            transport_id=transport_id,
            state=str(payload.get("state", "")),
            object_key=str(payload.get("object_key", object_key)),
            archive_sha256=str(payload.get("archive_sha256", "")),
            upload=cls._upload(payload.get("upload")),
            receipt=receipt,
        )

    def create_transfer(self, transfer: PackagedTransfer) -> RemoteTransfer:
        ids = transfer.correlations
        payload = self._request(
            "POST",
            "api/collector/transfers",
            {
                "transport_id": ids.transport_id,
                "job_id": ids.job_id,
                "run_id": ids.run_id,
                "bundle_id": ids.bundle_id,
                "destination": transfer.destination,
                "archive_size": transfer.archive_size,
                "archive_sha256": transfer.archive_sha256,
                "bundle_schema_version": transfer.bundle_schema_version,
            },
        )
        return self._remote(
            payload,
            transport_id=ids.transport_id,
            object_key=transfer.object_key,
        )

    def get_configuration(
        self,
        current_version: int | None,
    ) -> RemoteConfiguration:
        suffix = (
            f"?current_version={current_version}" if current_version is not None else ""
        )
        payload = self._request(
            "GET",
            f"api/collector/configuration{suffix}",
        )
        snapshot = payload.get("snapshot")
        if snapshot is not None and not isinstance(snapshot, dict):
            raise ValueError("configuration snapshot must be an object")
        return RemoteConfiguration(
            version=int(payload.get("version", 0)),
            unchanged=bool(payload.get("unchanged", False)),
            snapshot_sha256=str(payload.get("snapshot_sha256", "")),
            max_staleness_seconds=int(payload.get("max_staleness_seconds", 0)),
            snapshot=snapshot,
        )

    @staticmethod
    def _job(payload: object) -> RemoteJob | None:
        if payload is None:
            return None
        if not isinstance(payload, dict):
            raise ValueError("job response must be an object")
        manifest = payload.get("manifest")
        if not isinstance(manifest, dict):
            raise ValueError("job manifest must be an object")
        scheduled_for = manifest.get("scheduled_for")
        if not isinstance(scheduled_for, str):
            raise ValueError("job scheduled_for is missing")
        return RemoteJob(
            job_id=str(payload.get("job_id", "")),
            claim_request_id=str(payload.get("claim_request_id", "")),
            mode=str(payload.get("mode", "")),
            destination=str(payload.get("destination", "")),
            manifest_sha256=str(payload.get("manifest_sha256", "")),
            manifest=manifest,
            status=str(payload.get("status", "")),
            claimed_at=datetime.fromisoformat(scheduled_for.replace("Z", "+00:00")),
        )

    def claim_job(
        self,
        *,
        claim_request_id: str,
        mode: str,
    ) -> RemoteJob | None:
        payload = self._request(
            "POST",
            "api/collector/jobs/claim",
            {
                "claim_request_id": claim_request_id,
                "mode": mode,
            },
        )
        return self._job(payload.get("job"))

    def acknowledge_job(
        self,
        *,
        job_id: str,
        manifest_sha256: str,
    ) -> RemoteJob:
        payload = self._request(
            "POST",
            f"api/collector/jobs/{quote(job_id, safe='')}/ack",
            {"manifest_sha256": manifest_sha256},
        )
        job = self._job(payload)
        if job is None:
            raise ValueError("Gateway did not return acknowledged job")
        return job

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
    ) -> RemoteRun:
        def timestamp(value: datetime) -> str:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("run timestamps must include a UTC offset")
            return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

        payload = self._request(
            "POST",
            "api/collector/runs/complete",
            {
                "run_id": run_id,
                "job_id": job_id,
                "source": source,
                "status": status,
                "summary": summary,
                "error_classification": error_classification,
                "error_summary": error_summary,
                "started_at": timestamp(started_at),
                "finished_at": timestamp(finished_at),
            },
        )
        return RemoteRun(
            created=bool(payload.get("created")),
            collector_id=str(payload.get("collector_id", "")),
            run_id=str(payload.get("run_id", "")),
            job_id=str(payload.get("job_id", "")),
            status=str(payload.get("status", "")),
        )

    def send_heartbeat(
        self,
        *,
        platform: str,
        agent_version: str,
        enabled_sources: Sequence[str],
        current_run_id: str | None,
        current_stage: str,
        spool_pending_count: int,
    ) -> datetime:
        payload = self._request(
            "POST",
            "api/collector/heartbeat",
            {
                "platform": platform,
                "agent_version": agent_version,
                "enabled_sources": list(enabled_sources),
                "current_run_id": current_run_id,
                "current_stage": current_stage,
                "spool_pending_count": spool_pending_count,
            },
        )
        received_at = payload.get("received_at")
        if not isinstance(received_at, str):
            raise ValueError("Gateway heartbeat response lacks received_at")
        parsed = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("Gateway heartbeat timestamp is not timezone-aware")
        return parsed.astimezone(UTC)

    def send_events(
        self,
        events: Sequence[Mapping[str, Any]],
    ) -> EventBatchOutcome:
        payload = self._request(
            "POST",
            "api/collector/events",
            {"events": [dict(event) for event in events]},
        )
        accepted = payload.get("accepted_event_ids")
        duplicates = payload.get("duplicate_event_ids")
        if not isinstance(accepted, list) or not isinstance(duplicates, list):
            raise ValueError("Gateway event response is invalid")
        return EventBatchOutcome(
            accepted_event_ids=tuple(str(value) for value in accepted),
            duplicate_event_ids=tuple(str(value) for value in duplicates),
        )

    def renew_upload_authorization(self, transport_id: str) -> UploadAuthorization:
        payload = self._request(
            "POST",
            (
                "api/collector/transfers/"
                f"{quote(transport_id, safe='')}/upload-authorization"
            ),
        )
        upload = self._upload(payload)
        if upload is None:
            raise ValueError("Gateway did not return upload authorization")
        return upload

    def complete_transfer(self, transport_id: str) -> RemoteTransfer:
        payload = self._request(
            "POST",
            f"api/collector/transfers/{quote(transport_id, safe='')}/complete",
        )
        return self._remote(
            payload,
            transport_id=transport_id,
            object_key=str(payload.get("object_key", "")),
        )

    def get_transfer_status(self, transport_id: str) -> RemoteTransfer:
        payload = self._request(
            "GET",
            f"api/collector/transfers/{quote(transport_id, safe='')}",
        )
        return self._remote(
            payload,
            transport_id=transport_id,
            object_key=str(payload.get("object_key", "")),
        )
