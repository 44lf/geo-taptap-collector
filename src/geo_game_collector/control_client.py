"""Collector control-plane orchestration.

Configuration is cached and every immutable job is persisted before acknowledgement.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from geo_game_collector.local_state import (
    CachedConfiguration,
    ConfigurationCache,
    JobRecord,
    JobStore,
)
from geo_game_collector.transport import RemoteConfiguration, RemoteJob


class GatewayControlApi(Protocol):
    def get_configuration(self, current_version: int | None) -> RemoteConfiguration: ...

    def claim_job(self, *, claim_request_id: str, mode: str) -> RemoteJob | None: ...

    def acknowledge_job(self, *, job_id: str, manifest_sha256: str) -> RemoteJob: ...


class CollectorControlClient:
    def __init__(
        self,
        *,
        gateway: GatewayControlApi,
        configuration_cache: ConfigurationCache,
        job_store: JobStore,
    ) -> None:
        self._gateway = gateway
        self._configuration_cache = configuration_cache
        self._job_store = job_store

    def sync_configuration(self, *, now: datetime) -> CachedConfiguration:
        cached = self._configuration_cache.load_optional()
        remote = self._gateway.get_configuration(
            cached.version if cached is not None else None
        )
        if remote.unchanged:
            if (
                cached is None
                or remote.version != cached.version
                or remote.snapshot_sha256 != cached.snapshot_sha256
            ):
                raise ValueError(
                    "Gateway unchanged response does not match local cache"
                )
            snapshot = cached.snapshot
        else:
            if remote.snapshot is None:
                raise ValueError(
                    "Gateway changed response lacks a configuration snapshot"
                )
            snapshot = remote.snapshot
        return self._configuration_cache.save(
            version=remote.version,
            snapshot_sha256=remote.snapshot_sha256,
            max_staleness_seconds=remote.max_staleness_seconds,
            snapshot=snapshot,
            retrieved_at=now,
        )

    def claim_and_persist(
        self,
        *,
        claim_request_id: str,
        mode: str,
    ) -> JobRecord | None:
        remote = self._gateway.claim_job(
            claim_request_id=claim_request_id,
            mode=mode,
        )
        if remote is None:
            return None
        record = self._job_store.persist_claimed(
            job_id=remote.job_id,
            claim_request_id=remote.claim_request_id,
            manifest=remote.manifest,
            manifest_sha256=remote.manifest_sha256,
            created_at=remote.claimed_at,
        )
        self._gateway.acknowledge_job(
            job_id=record.job_id,
            manifest_sha256=record.manifest_sha256,
        )
        return self._job_store.mark_acknowledged(
            record.job_id,
            now=remote.claimed_at,
        )
