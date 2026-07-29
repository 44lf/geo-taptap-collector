from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone

import pytest

from geo_game_collector.bundle import (
    ArchiveIntegrity,
    BundleDescriptorV2,
    BundleFileEntry,
    BundleFileRole,
    BundleItemEntry,
    BundleLimits,
    BundleManifestV2,
    ComponentVersion,
)
from geo_game_collector.contracts import (
    CorrelationIds,
    ErrorCategory,
    JobMode,
    ResultStatus,
    SourceName,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def _manifest() -> BundleManifestV2:
    files = (
        BundleFileEntry(
            path="items/target-101/normalized.json",
            role=BundleFileRole.NORMALIZED,
            media_type="application/json",
            size_bytes=240,
            sha256=SHA_A,
            source=SourceName.BAIDU,
            source_game_id="baidu-101",
        ),
        BundleFileEntry(
            path="items/target-101/raw/search.json",
            role=BundleFileRole.RAW_EVIDENCE,
            media_type="application/json",
            size_bytes=500,
            sha256=SHA_B,
            source=SourceName.BAIDU,
            source_game_id="baidu-101",
        ),
        BundleFileEntry(
            path="items/discovery-1/normalized.json",
            role=BundleFileRole.NORMALIZED,
            media_type="application/json",
            size_bytes=260,
            sha256=SHA_C,
            source=SourceName.TAPTAP,
            source_game_id="168332",
        ),
    )
    return BundleManifestV2(
        schema_version=2,
        correlations=CorrelationIds(
            collector_id="collector-local-01",
            job_id="job-mixed-fixture",
            run_id="run-01",
            bundle_id="bundle-01",
            transport_id="transport-01",
        ),
        job_mode=JobMode.DISCOVERY,
        destination="dev",
        policy_version="policy-2026-07-29",
        schedule_occurrence=datetime(2026, 7, 29, 1, 30, tzinfo=timezone.utc),
        source_order=(SourceName.BAIDU, SourceName.TAPTAP),
        created_at=datetime(2026, 7, 29, 2, 0, tzinfo=timezone.utc),
        component_versions=(
            ComponentVersion(component="collector", version="0.1.0"),
            ComponentVersion(component="normalizer", version="geo-contract-v1"),
        ),
        limits=BundleLimits(
            max_items=20,
            max_requests=100,
            max_files=100,
            max_file_bytes=1024,
            max_total_bytes=4096,
            max_raw_evidence_bytes=2048,
            max_media_files_per_item=7,
        ),
        items=(
            BundleItemEntry(
                item_key="target-101",
                source=SourceName.BAIDU,
                source_game_id="baidu-101",
                target_game_id=101,
                status=ResultStatus.SUCCESS,
                normalized_path=files[0].path,
                raw_evidence_paths=(files[1].path,),
                media_paths=(),
            ),
            BundleItemEntry(
                item_key="discovery-1",
                source=SourceName.TAPTAP,
                source_game_id="168332",
                target_game_id=None,
                status=ResultStatus.SUCCESS,
                normalized_path=files[2].path,
                raw_evidence_paths=(),
                media_paths=(),
            ),
        ),
        files=files,
    )


def test_schema_v2_describes_multi_source_evidence_and_archive_integrity() -> None:
    manifest = _manifest()
    descriptor = BundleDescriptorV2(
        manifest=manifest,
        archive=ArchiveIntegrity(
            object_key="incoming/collector-local-01/transport-01.tar.zst",
            size_bytes=2048,
            sha256="d" * 64,
        ),
    )

    payload = descriptor.to_dict()

    assert payload["manifest"]["schema_version"] == 2
    assert payload["manifest"]["job"]["mode"] == "discovery"
    assert payload["manifest"]["job"]["policy_version"] == "policy-2026-07-29"
    assert payload["manifest"]["job"]["schedule_occurrence"].endswith("Z")
    assert payload["manifest"]["job"]["source_order"] == ["baidu", "taptap"]
    assert {entry["role"] for entry in payload["manifest"]["files"]} == {
        "normalized",
        "raw_evidence",
    }
    assert payload["archive"]["sha256"] == "d" * 64
    assert payload["manifest"]["component_versions"]["collector"] == "0.1.0"
    with pytest.raises(FrozenInstanceError):
        descriptor.archive = replace(  # type: ignore[misc]
            descriptor.archive, size_bytes=1
        )


def test_schema_v2_requires_a_full_unique_safe_file_inventory() -> None:
    manifest = _manifest()

    with pytest.raises(ValueError, match="unreferenced inventory"):
        replace(
            manifest,
            files=manifest.files
            + (
                BundleFileEntry(
                    path="items/orphan/raw.json",
                    role=BundleFileRole.RAW_EVIDENCE,
                    media_type="application/json",
                    size_bytes=1,
                    sha256="e" * 64,
                    source=SourceName.BAIDU,
                    source_game_id="orphan",
                ),
            ),
        )

    with pytest.raises(ValueError, match="safe POSIX relative path"):
        replace(
            manifest,
            files=(
                replace(manifest.files[0], path="../escape.json"),
                *manifest.files[1:],
            ),
        )


def test_schema_v2_enforces_bounded_item_file_and_byte_limits() -> None:
    manifest = _manifest()

    with pytest.raises(ValueError, match="max_files"):
        replace(manifest, limits=replace(manifest.limits, max_files=2))

    with pytest.raises(ValueError, match="max_file_bytes"):
        replace(manifest, limits=replace(manifest.limits, max_file_bytes=499))

    with pytest.raises(ValueError, match="max_raw_evidence_bytes"):
        replace(
            manifest,
            limits=replace(manifest.limits, max_raw_evidence_bytes=499),
        )


def test_schema_v2_keeps_refresh_and_discovery_item_identity_distinct() -> None:
    manifest = _manifest()

    with pytest.raises(ValueError, match="refresh item"):
        replace(
            manifest,
            job_mode=JobMode.REFRESH,
            items=(replace(manifest.items[1], target_game_id=None),),
            files=(manifest.files[2],),
        )


def test_schema_v2_rejects_unknown_sources_and_invalid_hashes() -> None:
    manifest = _manifest()

    with pytest.raises(ValueError, match="source_order"):
        replace(manifest, source_order=(SourceName.BAIDU,))

    with pytest.raises(ValueError, match="lowercase SHA-256"):
        replace(
            manifest,
            files=(replace(manifest.files[0], sha256="NOT-A-HASH"),)
            + manifest.files[1:],
        )


def test_schema_v2_records_and_validates_failure_classification() -> None:
    manifest = _manifest()
    blocked = replace(
        manifest.items[1],
        status=ResultStatus.BLOCKED,
        source_game_id=None,
        normalized_path=None,
        error_category=ErrorCategory.BLOCKED,
        http_status=429,
        stage="name_search",
        attempt_count=1,
    )
    blocked_manifest = replace(
        manifest,
        items=(manifest.items[0], blocked),
        files=manifest.files[:2],
    )

    payload = blocked_manifest.to_dict()
    assert payload["items"][1]["error_category"] == "blocked"
    assert payload["items"][1]["http_status"] == 429
    assert payload["items"][1]["stage"] == "name_search"
    assert payload["items"][1]["attempt_count"] == 1

    with pytest.raises(ValueError, match="blocked error_category"):
        replace(blocked, error_category=ErrorCategory.RETRYABLE)
    with pytest.raises(ValueError, match="attempt_count"):
        replace(blocked, attempt_count=0)
