"""Local liveness, readiness, and metrics contracts for operators and containers."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from geo_game_collector.launcher import LocalSettings
from geo_game_collector.local_state import ConfigurationCache
from geo_game_collector.secret_store import CollectorSecretStore
from geo_game_collector.spool import SpoolStore


@dataclass(frozen=True, slots=True)
class ProbeResult:
    ok: bool
    checks: dict[str, bool]
    metrics: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checks": self.checks,
            "metrics": self.metrics,
        }


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(dir=path, delete=False)
        temporary = Path(handle.name)
        handle.close()
        temporary.unlink()
    except OSError:
        return False
    return True


class RuntimeDiagnostics:
    def __init__(
        self,
        *,
        settings: LocalSettings,
        secret_store: CollectorSecretStore,
        configuration_cache: ConfigurationCache | None = None,
        spool: SpoolStore | None = None,
    ) -> None:
        self.settings = settings
        self.secret_store = secret_store
        self.configuration_cache = configuration_cache or ConfigurationCache(
            settings.config_directory / "configuration.json"
        )
        self.spool = spool or SpoolStore(
            settings.spool_directory,
            max_entries=settings.spool_max_entries,
            max_bytes=settings.spool_max_bytes,
        )

    def liveness(self) -> ProbeResult:
        writable = _writable(self.settings.state_directory)
        metrics = self.spool.metrics()
        return ProbeResult(
            ok=writable,
            checks={"data_directory_writable": writable},
            metrics=metrics,
        )

    def readiness(
        self,
        *,
        now: datetime | None = None,
        environment: dict[str, str] | None = None,
    ) -> ProbeResult:
        current = now or datetime.now(UTC)
        live = self.liveness()
        credential_available = self.secret_store.exists(environment=environment)
        cached = self.configuration_cache.load_optional()
        configuration_current = (
            cached is not None
            and cached.retrieved_at.timestamp() + cached.max_staleness_seconds
            > current.astimezone(UTC).timestamp()
        )
        below_high_water = (
            live.metrics["entries"] < self.settings.spool_max_entries
            and live.metrics["bytes"] < self.settings.spool_max_bytes
        )
        checks = {
            **live.checks,
            "credential_available": credential_available,
            "configuration_current": configuration_current,
            "spool_below_high_water": below_high_water,
        }
        return ProbeResult(
            ok=all(checks.values()),
            checks=checks,
            metrics=live.metrics,
        )

    def prometheus_metrics(self) -> str:
        values = self.spool.metrics()
        lines = [
            "# TYPE geo_collector_spool_entries gauge",
            f"geo_collector_spool_entries {values['entries']}",
            "# TYPE geo_collector_spool_pending gauge",
            f"geo_collector_spool_pending {values['pending']}",
            "# TYPE geo_collector_spool_terminal gauge",
            f"geo_collector_spool_terminal {values['terminal']}",
            "# TYPE geo_collector_spool_bytes gauge",
            f"geo_collector_spool_bytes {values['bytes']}",
        ]
        return "\n".join(lines) + "\n"
