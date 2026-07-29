from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from geo_game_collector.contracts import (
    SUPPORTED_SOURCES,
    CanonicalGameResult,
    CorrelationIds,
    DiscoveryPath,
    ErrorCategory,
    EvidenceReference,
    JobLimits,
    JobManifest,
    JobMode,
    MediaReference,
    RefreshTarget,
    ResultStatus,
    SourceName,
)
from geo_game_collector.registry import SourceRegistry, UnsupportedSourceError


def test_canonical_game_result_is_source_neutral_and_immutable() -> None:
    result = CanonicalGameResult(
        source=SourceName.TAPTAP,
        requested_name="原神",
        status=ResultStatus.SUCCESS,
        source_game_id="168332",
        name="原神",
        description="fixture description",
        score=7.9,
        tags=("开放世界", "二次元"),
        platforms=("android", "ios", "pc"),
        comment_count=100,
        icon_url="https://img.example/icon.png",
        screenshot_urls=("https://img.example/shot.jpg",),
        android_package="com.example.game",
        raw={"id": 168332},
        media=(
            MediaReference(
                role="icon",
                source_url="https://img.example/icon.png",
            ),
        ),
        evidence=(
            EvidenceReference(
                stage="detail",
                media_type="application/json",
                path="evidence/taptap/168332/detail.json",
            ),
        ),
    )

    assert result.source_game_id == "168332"
    assert result.score == 7.9
    assert result.comment_count == 100
    assert result.screenshot_urls == ("https://img.example/shot.jpg",)
    assert result.android_package == "com.example.game"
    assert result.raw == {"id": 168332}
    assert result.tags == ("开放世界", "二次元")
    with pytest.raises(FrozenInstanceError):
        result.name = "mutated"  # type: ignore[misc]


def test_success_result_requires_canonical_identity() -> None:
    with pytest.raises(ValueError, match="source_game_id and name"):
        CanonicalGameResult(
            source=SourceName.BAIDU,
            requested_name="missing",
            status=ResultStatus.SUCCESS,
        )


def test_result_status_and_error_category_must_agree() -> None:
    with pytest.raises(ValueError, match="blocked error_category"):
        CanonicalGameResult(
            source=SourceName.TAPTAP,
            requested_name="blocked",
            status=ResultStatus.BLOCKED,
        )
    with pytest.raises(ValueError, match="non-blocked error_category"):
        CanonicalGameResult(
            source=SourceName.BAIDU,
            requested_name="error",
            status=ResultStatus.ERROR,
            error_category=ErrorCategory.BLOCKED,
        )
    with pytest.raises(ValueError, match="must not have"):
        CanonicalGameResult(
            source=SourceName.BAIDU,
            requested_name="miss",
            status=ResultStatus.MISS,
            error_category=ErrorCategory.RETRYABLE,
        )


def test_job_manifest_is_recursively_immutable_and_bounded() -> None:
    manifest = JobManifest(
        schema_version=1,
        job_id="job-01",
        policy_version="policy-2026-07-29",
        mode=JobMode.REFRESH,
        source_order=(SourceName.BAIDU, SourceName.TAPTAP),
        targets=(
            RefreshTarget(
                target_game_id=101,
                name="原神",
                category_id=501,
            ),
        ),
        discovery_paths=(),
        limits=JobLimits(
            max_targets=20,
            max_discovery_paths=10,
            max_requests=100,
            max_media_per_item=7,
            max_media_bytes=512 * 1024 * 1024,
        ),
        scheduled_for=datetime(2026, 7, 29, 1, 2, 3, tzinfo=timezone.utc),
        destination="dev",
    )

    assert manifest.targets[0].target_game_id == 101
    with pytest.raises(FrozenInstanceError):
        manifest.destination = "production"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        manifest.targets.append(  # type: ignore[attr-defined]
            RefreshTarget(target_game_id=102, name="later", category_id=502)
        )


def test_job_manifest_rejects_mode_shape_and_limits() -> None:
    limits = JobLimits(
        max_targets=1,
        max_discovery_paths=1,
        max_requests=10,
        max_media_per_item=2,
        max_media_bytes=1024,
    )
    with pytest.raises(ValueError, match="refresh job"):
        JobManifest(
            schema_version=1,
            job_id="job-discovery-shaped",
            policy_version="policy-1",
            mode=JobMode.REFRESH,
            source_order=(SourceName.NINEGAME,),
            targets=(),
            discovery_paths=(
                DiscoveryPath(source=SourceName.NINEGAME, path="category/9"),
            ),
            limits=limits,
            scheduled_for=datetime.now(timezone.utc),
            destination="dev",
        )

    with pytest.raises(ValueError, match="max_targets"):
        JobManifest(
            schema_version=1,
            job_id="job-too-large",
            policy_version="policy-1",
            mode=JobMode.REFRESH,
            source_order=(SourceName.BAIDU,),
            targets=(
                RefreshTarget(target_game_id=1, name="one", category_id=9),
                RefreshTarget(target_game_id=2, name="two", category_id=9),
            ),
            discovery_paths=(),
            limits=limits,
            scheduled_for=datetime.now(timezone.utc),
            destination="dev",
        )


def test_correlation_ids_and_error_categories_are_explicit() -> None:
    ids = CorrelationIds(
        collector_id="collector-local-01",
        job_id="job-01",
        run_id="run-01",
        bundle_id="bundle-01",
        transport_id="transport-01",
    )

    assert ids.transport_id == "transport-01"
    assert {category.value for category in ErrorCategory} == {
        "blocked",
        "configuration",
        "permanent",
        "retryable",
        "security",
    }

    with pytest.raises(ValueError, match="collector_id"):
        CorrelationIds(
            collector_id="",
            job_id="job-01",
            run_id="run-01",
            bundle_id="bundle-01",
            transport_id="transport-01",
        )


class _StubAdapter:
    pass


def test_source_registry_accepts_only_the_four_declared_sources() -> None:
    assert SUPPORTED_SOURCES == (
        SourceName.BAIDU,
        SourceName.NINEGAME,
        SourceName.YINGYONGBAO,
        SourceName.TAPTAP,
    )
    registry = SourceRegistry()
    adapters = {source: _StubAdapter() for source in SUPPORTED_SOURCES}

    for source, adapter in adapters.items():
        registry.register(source, adapter)

    assert registry.registered_sources == SUPPORTED_SOURCES
    assert registry.get(SourceName.TAPTAP) is adapters[SourceName.TAPTAP]

    with pytest.raises(UnsupportedSourceError, match="unknown"):
        registry.get("unknown")
    with pytest.raises(UnsupportedSourceError, match="unknown"):
        registry.register("unknown", _StubAdapter())
