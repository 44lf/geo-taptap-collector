from __future__ import annotations

import json
import signal
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from geo_game_collector.launcher import (
    CollectorApplication,
    LocalSettings,
    ScheduledCollector,
    install_signal_handlers,
    save_public_settings,
)
from geo_game_collector.local_state import CachedConfiguration

NOW = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)


def test_settings_create_stable_non_secret_data_layout(tmp_path: Path):
    settings = LocalSettings(
        gateway_url="https://gateway.test",
        collector_id="collector-1",
        data_directory=tmp_path / "data",
    )
    settings.ensure_directories()
    config = tmp_path / "collector.json"
    save_public_settings(config, settings)

    assert settings.jobs_directory.is_dir()
    assert settings.spool_directory.is_dir()
    assert settings.logs_directory.is_dir()
    payload = json.loads(config.read_text(encoding="utf-8"))
    assert payload["collector_id"] == "collector-1"
    assert payload["processed_grace_hours"] == 24
    assert payload["spool_min_free_bytes"] == 1024 * 1024 * 1024
    assert "credential" not in config.read_text(encoding="utf-8").lower()
    assert LocalSettings.load(config) == settings


def test_settings_reject_credentials_in_public_config(tmp_path: Path):
    path = tmp_path / "collector.json"
    path.write_text(
        json.dumps(
            {
                "gateway_url": "https://gateway.test",
                "collector_id": "collector-1",
                "credential": "must-not-be-here",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="must not contain"):
        LocalSettings.load(path)


class _Control:
    def sync_configuration(self, *, now):
        return CachedConfiguration(
            version=3,
            snapshot_sha256="a" * 64,
            max_staleness_seconds=3600,
            snapshot={
                "refresh": {
                    "enabled": True,
                    "window_start": "15:00",
                    "window_end": "18:00",
                },
                "discovery": {"enabled": False},
            },
            retrieved_at=NOW,
        )


class _Agent:
    def __init__(self):
        self.runs = []
        self.drains = 0

    def run_once(self, *, claim_request_id, mode):
        self.runs.append((claim_request_id, mode))
        return SimpleNamespace(job_id="job-1")

    def service_backlog(self):
        self.drains += 1
        return ()


def test_scheduled_cycle_uses_persisted_occurrence_as_idempotency_key(
    tmp_path: Path,
):
    settings = LocalSettings(
        gateway_url="https://gateway.test",
        collector_id="collector-1",
        data_directory=tmp_path,
    )
    agent = _Agent()
    application = SimpleNamespace(
        settings=settings,
        control=_Control(),
        configuration_cache=object(),
        agent=agent,
    )
    scheduler = ScheduledCollector(application, clock=lambda: NOW)

    first = scheduler.run_cycle()
    second = scheduler.run_cycle()

    assert len(first) == 1
    assert second == ()
    assert agent.runs == [("refresh-20260729T070000Z", "refresh")]
    assert agent.drains == 1


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_signal_handlers_request_clean_shutdown(signum):
    event = threading.Event()
    handlers = {}

    def registrar(signum, handler):
        handlers[signum] = handler
        return None

    install_signal_handlers(event, registrar=registrar)
    handlers[signum](signum, None)

    assert event.is_set()
    assert signal.SIGINT in handlers
    assert signal.SIGTERM in handlers


def test_scheduled_loop_exits_after_interrupt_event(monkeypatch, tmp_path: Path):
    settings = LocalSettings(
        gateway_url="https://gateway.test",
        collector_id="collector-1",
        data_directory=tmp_path,
    )
    application = SimpleNamespace(
        settings=settings,
        control=object(),
        configuration_cache=object(),
        agent=object(),
    )
    scheduler = ScheduledCollector(application)
    stop_event = threading.Event()
    cycles = []

    def run_cycle():
        cycles.append("cycle")
        stop_event.set()
        return ()

    monkeypatch.setattr(scheduler, "run_cycle", run_cycle)

    scheduler.run_forever(stop_event)

    assert cycles == ["cycle"]
    assert stop_event.is_set()


def test_application_close_flushes_buffered_telemetry(tmp_path: Path):
    settings = LocalSettings(
        gateway_url="https://gateway.test",
        collector_id="collector-1",
        data_directory=tmp_path,
    )
    telemetry = MagicMock()
    application = CollectorApplication(
        settings=settings,
        gateway=MagicMock(),
        configuration_cache=MagicMock(),
        control=MagicMock(),
        spool=MagicMock(),
        agent=MagicMock(),
        telemetry=telemetry,
    )

    application.close()

    telemetry.flush.assert_called_once_with()
