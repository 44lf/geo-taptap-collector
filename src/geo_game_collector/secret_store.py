"""OS-backed Collector credential storage with container secret-file support."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

SERVICE_NAME = "geo-game-collector"


class SecretStoreError(RuntimeError):
    pass


class KeyringBackend(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...

    def delete_password(self, service: str, username: str) -> None: ...


def _default_keyring() -> KeyringBackend:
    try:
        import keyring
    except ImportError as exc:
        raise SecretStoreError(
            "OS secret storage requires the installed keyring package"
        ) from exc
    return keyring


def _validate(value: str | None) -> str:
    if value is None or not value.strip() or any(char in value for char in "\r\n"):
        raise SecretStoreError("Collector credential is missing or invalid")
    return value


class CollectorSecretStore:
    def __init__(
        self,
        collector_id: str,
        *,
        backend: KeyringBackend | None = None,
    ) -> None:
        if not collector_id.strip():
            raise ValueError("collector_id must be non-empty")
        self.collector_id = collector_id
        self._backend = backend

    @property
    def _keyring(self) -> KeyringBackend:
        if self._backend is None:
            self._backend = _default_keyring()
        return self._backend

    def get(
        self,
        *,
        environment: dict[str, str] | None = None,
    ) -> str:
        env = environment if environment is not None else os.environ
        secret_file = env.get("GEO_COLLECTOR_CREDENTIAL_FILE")
        if secret_file:
            path = Path(secret_file)
            try:
                return _validate(path.read_text(encoding="utf-8").strip())
            except OSError as exc:
                raise SecretStoreError(
                    "Collector credential file cannot be read"
                ) from exc
        if "GEO_COLLECTOR_CREDENTIAL" in env:
            return _validate(env["GEO_COLLECTOR_CREDENTIAL"])
        try:
            return _validate(
                self._keyring.get_password(SERVICE_NAME, self.collector_id)
            )
        except SecretStoreError:
            raise
        except Exception as exc:
            raise SecretStoreError("OS secret storage is unavailable") from exc

    def set(self, value: str) -> None:
        credential = _validate(value)
        try:
            self._keyring.set_password(
                SERVICE_NAME,
                self.collector_id,
                credential,
            )
        except Exception as exc:
            raise SecretStoreError("OS secret storage write failed") from exc

    def delete(self) -> None:
        try:
            self._keyring.delete_password(SERVICE_NAME, self.collector_id)
        except Exception as exc:
            raise SecretStoreError("OS secret storage delete failed") from exc

    def exists(self, *, environment: dict[str, str] | None = None) -> bool:
        try:
            self.get(environment=environment)
        except SecretStoreError:
            return False
        return True
