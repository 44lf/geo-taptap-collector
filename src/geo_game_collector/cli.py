from __future__ import annotations

import argparse
import getpass
import json
import sys
import threading
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from geo_game_collector import __version__
from geo_game_collector.diagnostics import RuntimeDiagnostics
from geo_game_collector.launcher import (
    LocalSettings,
    ScheduledCollector,
    build_application,
    default_data_directory,
    install_signal_handlers,
    save_public_settings,
)
from geo_game_collector.observability import error_fingerprint
from geo_game_collector.secret_store import CollectorSecretStore


def _default_config_path() -> Path:
    return default_data_directory() / "config" / "collector.json"


def _settings(path: str) -> LocalSettings:
    return LocalSettings.load(Path(path))


def _json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _claim_request_id(mode: str) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"manual-{mode}-{timestamp}-{uuid.uuid4().hex[:8]}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="geo-game-collector")
    parser.add_argument(
        "--config",
        default=str(_default_config_path()),
        help="non-secret Collector JSON config path",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    commands = parser.add_subparsers(dest="command")

    configure = commands.add_parser("configure")
    configure.add_argument("--gateway-url", required=True)
    configure.add_argument("--collector-id", required=True)
    configure.add_argument("--data-dir")

    credential = commands.add_parser("credential")
    credential.add_argument("action", choices=("set", "delete", "status"))
    credential.add_argument(
        "--stdin",
        action="store_true",
        help="read the credential from standard input instead of a hidden prompt",
    )

    run_now = commands.add_parser("run-now")
    run_now.add_argument("--mode", choices=("refresh", "discovery"), required=True)
    run_now.add_argument("--claim-request-id")

    commands.add_parser("agent")
    commands.add_parser("drain")
    commands.add_parser("doctor")
    commands.add_parser("health")
    commands.add_parser("readiness")
    commands.add_parser("metrics")

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    try:
        if args.command == "configure":
            data_directory = (
                Path(args.data_dir)
                if args.data_dir is not None
                else default_data_directory()
            )
            settings = LocalSettings(
                gateway_url=args.gateway_url,
                collector_id=args.collector_id,
                data_directory=data_directory,
            )
            settings.ensure_directories()
            save_public_settings(Path(args.config), settings)
            _json(
                {
                    "configured": True,
                    "config_path": str(Path(args.config)),
                    "data_directory": str(settings.data_directory),
                }
            )
            return 0

        settings = _settings(args.config)
        secret_store = CollectorSecretStore(settings.collector_id)
        if args.command == "credential":
            if args.action == "set":
                value = (
                    sys.stdin.readline().rstrip("\r\n")
                    if args.stdin
                    else getpass.getpass("Collector credential: ")
                )
                secret_store.set(value)
                _json({"credential_stored": True})
            elif args.action == "delete":
                secret_store.delete()
                _json({"credential_deleted": True})
            else:
                _json({"credential_available": secret_store.exists()})
            return 0

        diagnostics = RuntimeDiagnostics(
            settings=settings,
            secret_store=secret_store,
        )
        if args.command == "health":
            health_result = diagnostics.liveness()
            _json(health_result.to_dict())
            return 0 if health_result.ok else 1
        if args.command == "readiness":
            readiness_result = diagnostics.readiness()
            _json(readiness_result.to_dict())
            return 0 if readiness_result.ok else 1
        if args.command == "metrics":
            print(diagnostics.prometheus_metrics(), end="")
            return 0

        application = build_application(settings, secret_store=secret_store)
        try:
            if args.command == "run-now":
                run_result = application.run_once(
                    claim_request_id=(
                        args.claim_request_id or _claim_request_id(args.mode)
                    ),
                    mode=args.mode,
                )
                _json(
                    {
                        "job_id": run_result.job_id,
                        "run_id": run_result.run_id,
                        "transport_id": run_result.transport_id,
                        "run_status": run_result.run_status,
                        "transport_state": run_result.transport_state,
                        "drained_backlog": run_result.drained_backlog,
                    }
                )
                return 0
            if args.command == "drain":
                records = application.drain()
                _json(
                    {
                        "drained": len(records),
                        "states": [
                            {
                                "transport_id": (
                                    record.transfer.correlations.transport_id
                                ),
                                "state": record.state.value,
                            }
                            for record in records
                        ],
                    }
                )
                return 0
            if args.command == "doctor":
                application.control.sync_configuration(now=datetime.now(UTC))
                doctor_result = RuntimeDiagnostics(
                    settings=settings,
                    secret_store=secret_store,
                    configuration_cache=application.configuration_cache,
                    spool=application.spool,
                ).readiness()
                _json(doctor_result.to_dict())
                return 0 if doctor_result.ok else 1
            if args.command == "agent":
                stop_event = threading.Event()
                install_signal_handlers(stop_event)

                def report_error(error: BaseException) -> None:
                    _json(
                        {
                            "level": "error",
                            "error_type": type(error).__name__,
                            "error_fingerprint": error_fingerprint(error),
                        }
                    )

                ScheduledCollector(application).run_forever(
                    stop_event,
                    on_error=report_error,
                )
                return 0
            raise AssertionError("unhandled command")
        finally:
            application.close()
    except Exception as exc:
        print(
            json.dumps(
                {
                    "error": type(exc).__name__,
                    "error_fingerprint": error_fingerprint(exc),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
