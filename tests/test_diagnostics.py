from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from geo_game_collector.diagnostics import RuntimeDiagnostics
from geo_game_collector.launcher import LocalSettings
from geo_game_collector.local_state import ConfigurationCache
from geo_game_collector.secret_store import CollectorSecretStore

NOW = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)


class _Keyring:
    def __init__(self, value=None):
        self.value = value

    def get_password(self, service, username):
        return self.value

    def set_password(self, service, username, password):
        self.value = password

    def delete_password(self, service, username):
        self.value = None


def _settings(tmp_path: Path) -> LocalSettings:
    return LocalSettings(
        gateway_url="https://gateway.test",
        collector_id="collector-1",
        data_directory=tmp_path,
        spool_max_entries=10,
        spool_max_bytes=1000,
    )


def test_readiness_requires_secret_and_fresh_configuration(tmp_path: Path):
    settings = _settings(tmp_path)
    cache = ConfigurationCache(settings.config_directory / "configuration.json")
    snapshot = {"schema_version": 1}
    digest = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    cache.save(
        version=1,
        snapshot_sha256=digest,
        max_staleness_seconds=3600,
        snapshot=snapshot,
        retrieved_at=NOW,
    )
    diagnostics = RuntimeDiagnostics(
        settings=settings,
        secret_store=CollectorSecretStore(
            "collector-1",
            backend=_Keyring("credential-id:secret"),
        ),
        configuration_cache=cache,
    )

    ready = diagnostics.readiness(now=NOW, environment={})

    assert ready.ok
    assert ready.checks["configuration_current"]
    assert "geo_collector_spool_pending 0" in diagnostics.prometheus_metrics()


def test_liveness_does_not_claim_readiness_without_credential(tmp_path: Path):
    settings = _settings(tmp_path)
    diagnostics = RuntimeDiagnostics(
        settings=settings,
        secret_store=CollectorSecretStore("collector-1", backend=_Keyring()),
    )

    assert diagnostics.liveness().ok
    ready = diagnostics.readiness(now=NOW, environment={})
    assert not ready.ok
    assert not ready.checks["credential_available"]
