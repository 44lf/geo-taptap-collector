from __future__ import annotations

import hashlib
import io
import json
import tarfile
from datetime import UTC, datetime
from pathlib import Path

import zstandard

from geo_game_collector.bundle_build import build_bundle
from geo_game_collector.contracts import (
    CanonicalGameResult,
    CorrelationIds,
    ErrorCategory,
    MediaReference,
    ResultStatus,
    SourceName,
)
from geo_game_collector.execution import ExecutedItem, JobExecutionSummary

NOW = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)


def _job() -> dict:
    return {
        "job_id": "job-1",
        "policy_version": "policy-1",
        "mode": "refresh",
        "source_order": ["baidu"],
        "targets": [{"target_game_id": 7, "name": "Fixture", "category_id": 3}],
        "discovery_paths": [],
        "limits": {
            "max_targets": 1,
            "max_discovery_paths": 1,
            "max_requests": 4,
            "max_media_per_item": 2,
            "max_media_bytes": 1024,
        },
        "scheduled_for": "2026-07-29T08:00:00Z",
        "destination": "geo-dev",
    }


def _correlations() -> CorrelationIds:
    return CorrelationIds(
        collector_id="collector-1",
        job_id="job-1",
        run_id="run-1",
        bundle_id="bundle-1",
        transport_id="transport-1",
    )


def _success() -> CanonicalGameResult:
    return CanonicalGameResult(
        source=SourceName.BAIDU,
        requested_name="Fixture",
        status=ResultStatus.SUCCESS,
        source_game_id="baidu-1",
        name="Fixture",
        tags=("RPG",),
        platforms=("android",),
        icon_url="https://img.test/icon.png",
        screenshot_urls=("https://img.test/shot.png",),
        raw={"id": "baidu-1", "response": {"safe": True}},
        media=(
            MediaReference("icon", "https://img.test/icon.png"),
            MediaReference("screenshot", "https://img.test/shot.png"),
        ),
    )


def _execution(result: CanonicalGameResult) -> JobExecutionSummary:
    return JobExecutionSummary(
        items=(
            ExecutedItem(
                item_key="target-7-baidu",
                target_game_id=7,
                category_id=3,
                result=result,
            ),
        ),
        request_count=1,
        halted_by_block=False,
        completed_item_keys=("target-7-baidu",),
    )


def _members(archive: Path) -> dict[str, bytes]:
    with archive.open("rb") as source:
        with zstandard.ZstdDecompressor().stream_reader(source) as reader:
            raw = reader.read()
    result = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as bundle:
        for member in bundle.getmembers():
            stream = bundle.extractfile(member)
            if stream is not None:
                result[member.name] = stream.read()
    return result


def test_build_bundle_materializes_normalized_raw_media_and_inventory(tmp_path: Path):
    png = b"\x89PNG\r\n\x1a\nfixture"

    result = build_bundle(
        job_manifest=_job(),
        correlations=_correlations(),
        execution=_execution(_success()),
        work_root=tmp_path / "work",
        destination=tmp_path / "bundle.tar.zst",
        created_at=NOW,
        media_fetcher=lambda _url, _limit: (png, "image/png"),
    )

    members = _members(result.path)
    manifest = json.loads(members["manifest.json"])
    assert manifest["schema_version"] == 2
    assert manifest["items"][0]["target_game_id"] == 7
    assert manifest["items"][0]["normalized_path"].endswith("normalized.json")
    assert len(manifest["items"][0]["media_paths"]) == 2
    normalized = json.loads(members[manifest["items"][0]["normalized_path"]])
    assert normalized["source_game_id"] == "baidu-1"
    assert normalized["raw"]["response"] == {"safe": True}
    assert result.archive_sha256 == hashlib.sha256(result.path.read_bytes()).hexdigest()
    assert not (tmp_path / "work").exists()


def test_blocked_result_preserves_bounded_raw_evidence_without_media(tmp_path: Path):
    blocked = CanonicalGameResult(
        source=SourceName.BAIDU,
        requested_name="Fixture",
        status=ResultStatus.BLOCKED,
        raw={"http_status": 429, "error_type": "HTTPError"},
        error_category=ErrorCategory.BLOCKED,
    )

    result = build_bundle(
        job_manifest=_job(),
        correlations=_correlations(),
        execution=_execution(blocked),
        work_root=tmp_path / "work",
        destination=tmp_path / "bundle.tar.zst",
        created_at=NOW,
        media_fetcher=None,
    )

    members = _members(result.path)
    manifest = json.loads(members["manifest.json"])
    item = manifest["items"][0]
    assert item["status"] == "blocked"
    assert item["normalized_path"] is None
    assert item["raw_evidence_paths"]
    assert item["media_paths"] == []
