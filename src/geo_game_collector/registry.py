from __future__ import annotations

from typing import Any, Protocol

from geo_game_collector.contracts import (
    SUPPORTED_SOURCES,
    CanonicalGameResult,
    ResultStatus,
    SourceName,
)


class UnsupportedSourceError(ValueError):
    pass


class SourceContractError(ValueError):
    pass


class SourceAdapter(Protocol):
    def collect(self, request: Any) -> tuple[CanonicalGameResult, ...]: ...


class SourceRegistry:
    def __init__(self) -> None:
        self._adapters: dict[SourceName, Any] = {}

    @staticmethod
    def _coerce_source(source: SourceName | str) -> SourceName:
        try:
            return SourceName(source)
        except ValueError as exc:
            raise UnsupportedSourceError(f"unknown source: {source}") from exc

    def register(self, source: SourceName | str, adapter: Any) -> None:
        source_name = self._coerce_source(source)
        if source_name not in SUPPORTED_SOURCES:
            raise UnsupportedSourceError(f"unknown source: {source}")
        if source_name in self._adapters:
            raise ValueError(f"source already registered: {source_name.value}")
        self._adapters[source_name] = adapter

    def get(self, source: SourceName | str) -> Any:
        source_name = self._coerce_source(source)
        try:
            return self._adapters[source_name]
        except KeyError as exc:
            raise UnsupportedSourceError(
                f"source is supported but not registered: {source_name.value}"
            ) from exc

    def collect(
        self,
        source: SourceName | str,
        request: Any,
    ) -> tuple[CanonicalGameResult, ...]:
        """Resolve the source before invocation, then enforce canonical output."""

        source_name = self._coerce_source(source)
        adapter: SourceAdapter = self.get(source_name)
        results = adapter.collect(request)
        if not isinstance(results, tuple):
            raise SourceContractError(
                "source adapter results must be an immutable tuple"
            )
        for result in results:
            if not isinstance(result, CanonicalGameResult):
                raise SourceContractError(
                    "source adapter returned a non-canonical result"
                )
            if result.source is not source_name:
                raise SourceContractError("source adapter result identity mismatch")
            if result.status is ResultStatus.SUCCESS and result.raw is None:
                raise SourceContractError(
                    "successful source result must preserve raw evidence"
                )
        return results

    @property
    def registered_sources(self) -> tuple[SourceName, ...]:
        return tuple(source for source in SUPPORTED_SOURCES if source in self._adapters)
