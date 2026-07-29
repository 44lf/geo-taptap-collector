from __future__ import annotations

from dataclasses import dataclass

import pytest

from geo_game_collector.contracts import (
    SUPPORTED_SOURCES,
    CanonicalGameResult,
    ResultStatus,
    SourceName,
)
from geo_game_collector.registry import (
    SourceContractError,
    SourceRegistry,
    UnsupportedSourceError,
)


@dataclass
class _Adapter:
    source: SourceName
    calls: int = 0

    def collect(self, request: object) -> tuple[CanonicalGameResult, ...]:
        self.calls += 1
        return (
            CanonicalGameResult(
                source=self.source,
                requested_name="Fixture Game",
                status=ResultStatus.SUCCESS,
                source_game_id=f"{self.source.value}-1",
                name="Fixture Game",
                raw={"source": self.source.value, "request": str(request)},
            ),
        )


def test_all_four_registered_adapters_emit_canonical_results_with_raw_evidence() -> (
    None
):
    registry = SourceRegistry()
    adapters = {source: _Adapter(source) for source in SUPPORTED_SOURCES}
    for source, adapter in adapters.items():
        registry.register(source, adapter)

    results = {
        source: registry.collect(source, {"name": "Fixture Game"})[0]
        for source in SUPPORTED_SOURCES
    }

    assert registry.registered_sources == SUPPORTED_SOURCES
    assert set(results) == set(SUPPORTED_SOURCES)
    assert all(isinstance(result, CanonicalGameResult) for result in results.values())
    assert all(result.raw is not None for result in results.values())
    assert all(adapter.calls == 1 for adapter in adapters.values())


def test_unknown_source_is_rejected_before_any_adapter_or_network_access() -> None:
    registry = SourceRegistry()
    adapter = _Adapter(SourceName.BAIDU)
    registry.register(SourceName.BAIDU, adapter)

    with pytest.raises(UnsupportedSourceError, match="unknown"):
        registry.collect("unknown", {"name": "never requested"})

    assert adapter.calls == 0


@pytest.mark.parametrize(
    "result",
    [
        ["not immutable"],
        ("not canonical",),
        (
            CanonicalGameResult(
                source=SourceName.NINEGAME,
                requested_name="Fixture Game",
                status=ResultStatus.SUCCESS,
                source_game_id="wrong-source",
                name="Fixture Game",
                raw={"ok": True},
            ),
        ),
        (
            CanonicalGameResult(
                source=SourceName.BAIDU,
                requested_name="Fixture Game",
                status=ResultStatus.SUCCESS,
                source_game_id="missing-raw",
                name="Fixture Game",
            ),
        ),
    ],
)
def test_registry_rejects_invalid_adapter_contract(result) -> None:
    class _Invalid:
        def collect(self, request):
            return result

    registry = SourceRegistry()
    registry.register(SourceName.BAIDU, _Invalid())

    with pytest.raises(SourceContractError):
        registry.collect(SourceName.BAIDU, {})
