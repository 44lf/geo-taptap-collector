from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from geo_game_collector.bundle import (
    BundleFileEntry,
    BundleFileRole,
    BundleItemEntry,
    BundleLimits,
    BundleManifestV2,
    ComponentVersion,
)
from geo_game_collector.contracts import (
    CorrelationIds,
    JobMode,
    ResultStatus,
    SourceName,
)
from geo_game_collector.packaging import BundlePackagingError, package_bundle

NORMALIZED = json.dumps(
    {
        "source": "baidu",
        "source_game_id": "baidu-101",
        "requested_name": "Game A",
        "status": "success",
        "name": "Game A",
        "raw": {"id": 101},
    },
    separators=(",", ":"),
).encode()


def _manifest(*, media_type: str = "application/json") -> BundleManifestV2:
    path = "items/target-101/normalized.json"
    return BundleManifestV2(
        schema_version=2,
        correlations=CorrelationIds(
            collector_id="collector-local-1",
            job_id="job-1",
            run_id="run-1",
            bundle_id="bundle-1",
            transport_id="transport-1",
        ),
        job_mode=JobMode.REFRESH,
        destination="geo-production",
        policy_version="collector-config-3",
        schedule_occurrence=datetime(2026, 7, 29, 10, 0, tzinfo=UTC),
        source_order=(SourceName.BAIDU,),
        created_at=datetime(2026, 7, 29, 10, 1, tzinfo=UTC),
        component_versions=(ComponentVersion("collector", "0.1.0"),),
        limits=BundleLimits(
            max_items=10,
            max_requests=20,
            max_files=20,
            max_file_bytes=1024 * 1024,
            max_total_bytes=2 * 1024 * 1024,
            max_raw_evidence_bytes=1024 * 1024,
            max_media_files_per_item=6,
        ),
        items=(
            BundleItemEntry(
                item_key="target-101",
                source=SourceName.BAIDU,
                source_game_id="baidu-101",
                target_game_id=101,
                status=ResultStatus.SUCCESS,
                normalized_path=path,
                raw_evidence_paths=(),
                media_paths=(),
            ),
        ),
        files=(
            BundleFileEntry(
                path=path,
                role=BundleFileRole.NORMALIZED,
                media_type=media_type,
                size_bytes=len(NORMALIZED),
                sha256=hashlib.sha256(NORMALIZED).hexdigest(),
                source=SourceName.BAIDU,
                source_game_id="baidu-101",
            ),
        ),
    )


def test_packaging_is_deterministic_and_returns_outer_archive_integrity(
    tmp_path: Path,
) -> None:
    source = tmp_path / "normalized.json"
    source.write_bytes(NORMALIZED)
    manifest = _manifest()

    first = package_bundle(
        manifest,
        files={manifest.files[0].path: source},
        destination=tmp_path / "first.tar.zst",
    )
    second = package_bundle(
        manifest,
        files={manifest.files[0].path: source},
        destination=tmp_path / "second.tar.zst",
    )

    assert first.archive_bytes == second.archive_bytes
    assert first.archive_sha256 == second.archive_sha256
    assert (
        first.archive_sha256
        == hashlib.sha256((tmp_path / "first.tar.zst").read_bytes()).hexdigest()
    )
    assert first.object_key == "incoming/collector-local-1/transport-1.tar.zst"
    assert first.file_count == 2


@pytest.mark.parametrize(
    ("manifest", "content", "message"),
    [
        (
            replace(
                _manifest(),
                files=(replace(_manifest().files[0], sha256="f" * 64),),
            ),
            NORMALIZED,
            "SHA-256",
        ),
        (_manifest(media_type="image/png"), NORMALIZED, "MIME"),
        (_manifest(), b"changed", "size"),
    ],
)
def test_packaging_rejects_hash_mime_and_size_mismatch_before_publication(
    tmp_path: Path,
    manifest: BundleManifestV2,
    content: bytes,
    message: str,
) -> None:
    source = tmp_path / "input"
    source.write_bytes(content)
    destination = tmp_path / "bundle.tar.zst"

    with pytest.raises(BundlePackagingError, match=message):
        package_bundle(
            manifest,
            files={manifest.files[0].path: source},
            destination=destination,
        )
    assert not destination.exists()


def test_packaging_requires_exact_full_inventory(tmp_path: Path) -> None:
    manifest = _manifest()
    with pytest.raises(BundlePackagingError, match="inventory"):
        package_bundle(
            manifest,
            files={},
            destination=tmp_path / "bundle.tar.zst",
        )
