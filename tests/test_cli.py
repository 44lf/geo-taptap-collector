from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from geo_game_collector.cli import main
from geo_game_collector.launcher import LocalSettings


def test_configure_writes_only_public_settings(tmp_path: Path, capsys):
    config = tmp_path / "collector.json"

    result = main(
        [
            "--config",
            str(config),
            "configure",
            "--gateway-url",
            "https://gateway.test",
            "--collector-id",
            "collector-1",
            "--data-dir",
            str(tmp_path / "data"),
        ]
    )

    assert result == 0
    assert json.loads(capsys.readouterr().out)["configured"] is True
    assert "secret" not in config.read_text(encoding="utf-8").lower()


def test_health_command_is_available_without_a_credential(tmp_path: Path, capsys):
    config = tmp_path / "collector.json"
    main(
        [
            "--config",
            str(config),
            "configure",
            "--gateway-url",
            "https://gateway.test",
            "--collector-id",
            "collector-1",
            "--data-dir",
            str(tmp_path / "data"),
        ]
    )
    capsys.readouterr()

    result = main(["--config", str(config), "health"])
    payload = json.loads(capsys.readouterr().out)

    assert result == 0
    assert payload["ok"] is True
    assert payload["checks"]["data_directory_writable"] is True


def test_agent_command_closes_application_after_interrupt(monkeypatch, tmp_path: Path):
    settings = LocalSettings(
        gateway_url="https://gateway.test",
        collector_id="collector-1",
        data_directory=tmp_path / "data",
    )
    application = MagicMock()
    application.settings = settings
    monkeypatch.setattr("geo_game_collector.cli._settings", lambda _path: settings)
    monkeypatch.setattr(
        "geo_game_collector.cli.CollectorSecretStore",
        lambda _collector_id: object(),
    )
    monkeypatch.setattr(
        "geo_game_collector.cli.build_application",
        lambda _settings, **_kwargs: application,
    )
    monkeypatch.setattr(
        "geo_game_collector.cli.install_signal_handlers",
        lambda stop_event: stop_event.set(),
    )

    result = main(["--config", str(tmp_path / "collector.json"), "agent"])

    assert result == 0
    application.close.assert_called_once_with()
