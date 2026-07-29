from __future__ import annotations

from pathlib import Path

import pytest

from geo_game_collector.secret_store import CollectorSecretStore, SecretStoreError


class _Keyring:
    def __init__(self):
        self.values = {}

    def get_password(self, service, username):
        return self.values.get((service, username))

    def set_password(self, service, username, password):
        self.values[(service, username)] = password

    def delete_password(self, service, username):
        del self.values[(service, username)]


def test_os_secret_store_round_trip_never_returns_metadata():
    backend = _Keyring()
    store = CollectorSecretStore("collector-1", backend=backend)

    store.set("credential-id:secret-value")

    assert store.exists(environment={})
    assert store.get(environment={}) == "credential-id:secret-value"
    store.delete()
    assert not store.exists(environment={})


def test_container_secret_file_precedes_environment_and_keyring(tmp_path: Path):
    secret_file = tmp_path / "credential"
    secret_file.write_text("file-secret\n", encoding="utf-8")
    store = CollectorSecretStore("collector-1", backend=_Keyring())

    assert (
        store.get(
            environment={
                "GEO_COLLECTOR_CREDENTIAL_FILE": str(secret_file),
                "GEO_COLLECTOR_CREDENTIAL": "environment-secret",
            }
        )
        == "file-secret"
    )


def test_missing_or_multiline_credentials_fail_closed():
    store = CollectorSecretStore("collector-1", backend=_Keyring())

    with pytest.raises(SecretStoreError, match="missing"):
        store.get(environment={})
    with pytest.raises(SecretStoreError, match="invalid"):
        store.set("secret\ninjected")
