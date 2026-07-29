"""Native runtime assembly, persistent layout, and bounded background scheduling."""

from __future__ import annotations

import json
import os
import signal
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import FrameType
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from geo_game_collector.agent import AgentRunResult, CollectorAgent
from geo_game_collector.control_client import CollectorControlClient
from geo_game_collector.execution import JobExecutor, ProvenSourceRuntime
from geo_game_collector.local_state import (
    ConfigurationCache,
    JobStore,
)
from geo_game_collector.observability import GatewayTelemetry, JsonlLogBuffer
from geo_game_collector.scheduler import (
    SchedulePolicy,
    ScheduleState,
    ScheduleStateStore,
    parse_hhmm,
    plan_resume,
)
from geo_game_collector.secret_store import CollectorSecretStore
from geo_game_collector.spool import SpoolRecord, SpoolStore
from geo_game_collector.transport import (
    GatewayHttpClient,
    PresignedArchiveUploader,
    TransferDrain,
)

_SENSITIVE_CONFIG_PARTS = ("secret", "password", "credential", "token", "cookie")


def default_data_directory(
    environment: Mapping[str, str] | None = None,
) -> Path:
    env = environment if environment is not None else os.environ
    if os.name == "nt" and env.get("LOCALAPPDATA"):
        return Path(env["LOCALAPPDATA"]) / "GEOGameCollector"
    if env.get("XDG_DATA_HOME"):
        return Path(env["XDG_DATA_HOME"]) / "geo-game-collector"
    return Path.home() / ".local" / "share" / "geo-game-collector"


@dataclass(frozen=True, slots=True)
class LocalSettings:
    gateway_url: str
    collector_id: str
    data_directory: Path
    poll_seconds: float = 60
    request_timeout_seconds: float = 30
    spool_max_entries: int = 200
    spool_max_bytes: int = 10 * 1024 * 1024 * 1024
    spool_min_free_bytes: int = 1024 * 1024 * 1024
    processed_grace_hours: float = 24
    catch_up_max_age_seconds: int = 2 * 24 * 60 * 60
    max_runs_per_start: int = 2
    timezone_name: str = "Asia/Shanghai"

    def __post_init__(self) -> None:
        if not self.gateway_url.startswith("https://"):
            raise ValueError("gateway_url must use HTTPS")
        if not self.collector_id.strip():
            raise ValueError("collector_id must be non-empty")
        if self.poll_seconds <= 0 or self.request_timeout_seconds <= 0:
            raise ValueError("runtime intervals must be positive")
        if (
            self.spool_max_entries <= 0
            or self.spool_max_bytes <= 0
            or self.spool_min_free_bytes < 0
            or self.processed_grace_hours < 0
            or self.catch_up_max_age_seconds <= 0
            or self.max_runs_per_start <= 0
        ):
            raise ValueError("runtime limits must be positive")
        ZoneInfo(self.timezone_name)

    @property
    def config_directory(self) -> Path:
        return self.data_directory / "config"

    @property
    def jobs_directory(self) -> Path:
        return self.data_directory / "jobs"

    @property
    def spool_directory(self) -> Path:
        return self.data_directory / "spool"

    @property
    def state_directory(self) -> Path:
        return self.data_directory / "state"

    @property
    def logs_directory(self) -> Path:
        return self.data_directory / "logs"

    def ensure_directories(self) -> None:
        for path in (
            self.config_directory,
            self.jobs_directory,
            self.spool_directory,
            self.state_directory,
            self.logs_directory,
            self.data_directory / "staging",
        ):
            path.mkdir(parents=True, exist_ok=True)

    def public_dict(self) -> dict[str, Any]:
        return {
            "gateway_url": self.gateway_url,
            "collector_id": self.collector_id,
            "data_directory": str(self.data_directory),
            "poll_seconds": self.poll_seconds,
            "request_timeout_seconds": self.request_timeout_seconds,
            "spool_max_entries": self.spool_max_entries,
            "spool_max_bytes": self.spool_max_bytes,
            "spool_min_free_bytes": self.spool_min_free_bytes,
            "processed_grace_hours": self.processed_grace_hours,
            "catch_up_max_age_seconds": self.catch_up_max_age_seconds,
            "max_runs_per_start": self.max_runs_per_start,
            "timezone_name": self.timezone_name,
        }

    @classmethod
    def load(
        cls,
        path: Path | None,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> LocalSettings:
        env = environment if environment is not None else os.environ
        payload: dict[str, Any] = {}
        if path is not None and path.is_file():
            decoded = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(decoded, dict):
                raise ValueError("Collector config must be a JSON object")
            payload = decoded
        if any(
            part in str(key).lower()
            for key in payload
            for part in _SENSITIVE_CONFIG_PARTS
        ):
            raise ValueError("Collector config must not contain credentials")

        def value(name: str, env_name: str, default: Any = None) -> Any:
            return env.get(env_name, payload.get(name, default))

        gateway_url = value("gateway_url", "GEO_COLLECTOR_GATEWAY_URL")
        collector_id = value("collector_id", "GEO_COLLECTOR_ID")
        if not isinstance(gateway_url, str) or not isinstance(collector_id, str):
            raise ValueError("gateway_url and collector_id are required")
        data_value = value(
            "data_directory",
            "GEO_COLLECTOR_DATA_DIR",
            str(default_data_directory(env)),
        )
        return cls(
            gateway_url=gateway_url,
            collector_id=collector_id,
            data_directory=Path(str(data_value)),
            poll_seconds=float(value("poll_seconds", "GEO_COLLECTOR_POLL_SECONDS", 60)),
            request_timeout_seconds=float(
                value(
                    "request_timeout_seconds",
                    "GEO_COLLECTOR_REQUEST_TIMEOUT_SECONDS",
                    30,
                )
            ),
            spool_max_entries=int(
                value(
                    "spool_max_entries",
                    "GEO_COLLECTOR_SPOOL_MAX_ENTRIES",
                    200,
                )
            ),
            spool_max_bytes=int(
                value(
                    "spool_max_bytes",
                    "GEO_COLLECTOR_SPOOL_MAX_BYTES",
                    10 * 1024 * 1024 * 1024,
                )
            ),
            spool_min_free_bytes=int(
                value(
                    "spool_min_free_bytes",
                    "GEO_COLLECTOR_SPOOL_MIN_FREE_BYTES",
                    1024 * 1024 * 1024,
                )
            ),
            processed_grace_hours=float(
                value(
                    "processed_grace_hours",
                    "GEO_COLLECTOR_PROCESSED_GRACE_HOURS",
                    24,
                )
            ),
            catch_up_max_age_seconds=int(
                value(
                    "catch_up_max_age_seconds",
                    "GEO_COLLECTOR_CATCH_UP_MAX_AGE_SECONDS",
                    2 * 24 * 60 * 60,
                )
            ),
            max_runs_per_start=int(
                value(
                    "max_runs_per_start",
                    "GEO_COLLECTOR_MAX_RUNS_PER_START",
                    2,
                )
            ),
            timezone_name=str(
                value(
                    "timezone_name",
                    "GEO_COLLECTOR_TIMEZONE",
                    "Asia/Shanghai",
                )
            ),
        )


def save_public_settings(path: Path, settings: LocalSettings) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            settings.public_dict(),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


@dataclass(slots=True)
class CollectorApplication:
    settings: LocalSettings
    gateway: GatewayHttpClient
    configuration_cache: ConfigurationCache
    control: CollectorControlClient
    spool: SpoolStore
    agent: CollectorAgent
    telemetry: GatewayTelemetry

    def run_once(self, *, claim_request_id: str, mode: str) -> AgentRunResult:
        return self.agent.run_once(
            claim_request_id=claim_request_id,
            mode=mode,
        )

    def drain(self) -> tuple[SpoolRecord, ...]:
        return self.agent.service_backlog()

    def close(self) -> None:
        self.telemetry.flush()


def build_application(
    settings: LocalSettings,
    *,
    secret_store: CollectorSecretStore | None = None,
    clock: Callable[[], datetime] | None = None,
) -> CollectorApplication:
    settings.ensure_directories()
    secret = secret_store or CollectorSecretStore(settings.collector_id)
    gateway = GatewayHttpClient(
        base_url=settings.gateway_url,
        bearer_credential=secret.get(),
        timeout=settings.request_timeout_seconds,
    )
    configuration_cache = ConfigurationCache(
        settings.config_directory / "configuration.json"
    )
    jobs = JobStore(settings.jobs_directory)
    spool = SpoolStore(
        settings.spool_directory,
        max_entries=settings.spool_max_entries,
        max_bytes=settings.spool_max_bytes,
    )
    control = CollectorControlClient(
        gateway=gateway,
        configuration_cache=configuration_cache,
        job_store=jobs,
    )
    telemetry = GatewayTelemetry(
        buffer=JsonlLogBuffer(
            settings.logs_directory,
            max_segment_bytes=2 * 1024 * 1024,
            max_records_per_segment=500,
            clock=clock,
        ),
        gateway=gateway,
    )
    drain = TransferDrain(
        store=spool,
        gateway=gateway,
        uploader=PresignedArchiveUploader(
            timeout=max(120, settings.request_timeout_seconds)
        ),
    )
    agent = CollectorAgent(
        collector_id=settings.collector_id,
        data_directory=settings.data_directory,
        control=control,
        gateway=gateway,
        job_store=jobs,
        executor=JobExecutor(ProvenSourceRuntime()),
        spool=spool,
        drain=drain,
        clock=clock or (lambda: datetime.now(UTC)),
        telemetry=telemetry,
        processed_grace=timedelta(hours=settings.processed_grace_hours),
        spool_min_free_bytes=settings.spool_min_free_bytes,
    )
    return CollectorApplication(
        settings=settings,
        gateway=gateway,
        configuration_cache=configuration_cache,
        control=control,
        spool=spool,
        agent=agent,
        telemetry=telemetry,
    )


class ScheduledApplication(Protocol):
    settings: LocalSettings
    control: CollectorControlClient
    configuration_cache: ConfigurationCache
    agent: CollectorAgent


def _initial_schedule_state(
    *,
    schedule_id: str,
    config_version: int,
    window_start: str,
    timezone_name: str,
    now: datetime,
) -> ScheduleState:
    local = now.astimezone(ZoneInfo(timezone_name))
    start = parse_hhmm(window_start)
    candidate = local.replace(
        hour=start.hour,
        minute=start.minute,
        second=0,
        microsecond=0,
    )
    return ScheduleState(
        schedule_id=schedule_id,
        config_version=config_version,
        next_due_at=candidate.astimezone(UTC),
        updated_at=now,
    )


class ScheduledCollector:
    def __init__(
        self,
        application: ScheduledApplication,
        *,
        state_store: ScheduleStateStore | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._application = application
        self._state_store = state_store or ScheduleStateStore(
            application.settings.state_directory / "schedules"
        )
        self._clock = clock or (lambda: datetime.now(UTC))

    def run_cycle(self) -> tuple[AgentRunResult, ...]:
        now = self._clock().astimezone(UTC)
        cached = self._application.control.sync_configuration(now=now)
        results: list[AgentRunResult] = []
        for mode in ("refresh", "discovery"):
            raw = cached.snapshot.get(mode)
            if not isinstance(raw, dict) or not raw.get("enabled"):
                continue
            schedule_id = f"{mode}-daily"
            try:
                state = self._state_store.load(schedule_id)
            except FileNotFoundError:
                state = _initial_schedule_state(
                    schedule_id=schedule_id,
                    config_version=cached.version,
                    window_start=str(raw["window_start"]),
                    timezone_name=self._application.settings.timezone_name,
                    now=now,
                )
            policy = SchedulePolicy(
                schedule_id=schedule_id,
                config_version=cached.version,
                mode=mode,
                window_start=parse_hhmm(str(raw["window_start"])),
                window_end=parse_hhmm(str(raw["window_end"])),
                timezone_name=self._application.settings.timezone_name,
                catch_up_max_age=timedelta(
                    seconds=self._application.settings.catch_up_max_age_seconds
                ),
                max_runs_per_start=self._application.settings.max_runs_per_start,
                config_retrieved_at=cached.retrieved_at,
                config_max_staleness=timedelta(seconds=cached.max_staleness_seconds),
            )
            plan = plan_resume(policy, state, now=now)
            if plan.safe_stop_reason is not None:
                continue
            for occurrence in plan.run_occurrences:
                claim_request_id = f"{mode}-{occurrence.strftime('%Y%m%dT%H%M%SZ')}"
                results.append(
                    self._application.agent.run_once(
                        claim_request_id=claim_request_id,
                        mode=mode,
                    )
                )
            self._state_store.save(plan.next_state)
        if not results:
            self._application.agent.service_backlog()
        return tuple(results)

    def run_forever(
        self,
        stop_event: threading.Event,
        *,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> None:
        while not stop_event.is_set():
            try:
                self.run_cycle()
            except Exception as exc:
                if on_error is not None:
                    on_error(exc)
            stop_event.wait(self._application.settings.poll_seconds)


SignalRegistrar = Callable[
    [int, Callable[[int, FrameType | None], None]],
    object,
]


def install_signal_handlers(
    stop_event: threading.Event,
    *,
    registrar: SignalRegistrar = signal.signal,
) -> None:
    def request_stop(_signum: int, _frame: FrameType | None) -> None:
        stop_event.set()

    registrar(signal.SIGINT, request_stop)
    registrar(signal.SIGTERM, request_stop)
