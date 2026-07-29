"""Build immutable Bundle v2 artifacts from completed source execution."""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from geo_game_collector.bundle import (
    BundleFileEntry,
    BundleFileRole,
    BundleItemEntry,
    BundleLimits,
    BundleManifestV2,
    ComponentVersion,
)
from geo_game_collector.contracts import (
    CanonicalGameResult,
    CorrelationIds,
    JobMode,
    ResultStatus,
    SourceName,
)
from geo_game_collector.execution import JobExecutionSummary
from geo_game_collector.packaging import BundlePackageResult, package_bundle

MediaFetcher = Callable[[str, int], tuple[bytes, str] | None]


@dataclass(frozen=True, slots=True)
class BundleBuildLimits:
    max_files: int = 500
    max_file_bytes: int = 20 * 1024 * 1024
    max_total_bytes: int = 512 * 1024 * 1024
    max_raw_evidence_bytes: int = 64 * 1024 * 1024


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalized(result: CanonicalGameResult) -> dict[str, Any]:
    return {
        "source": result.source.value,
        "source_game_id": result.source_game_id,
        "requested_name": result.requested_name,
        "status": result.status.value,
        "name": result.name,
        "score": result.score,
        "description": result.description,
        "tags": list(result.tags),
        "platforms": list(result.platforms),
        "comment_count": result.comment_count,
        "icon_url": result.icon_url,
        "screenshot_urls": list(result.screenshot_urls),
        "android_package": result.android_package,
        "raw": result.raw or {},
    }


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()


def _media_extension(media_type: str) -> str:
    return {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }.get(media_type, ".bin")


def default_media_fetcher(url: str, max_bytes: int) -> tuple[bytes, str] | None:
    request = Request(
        url,
        headers={"User-Agent": "geo-game-collector/0.1"},
        method="GET",
    )
    with urlopen(request, timeout=20) as response:  # noqa: S310 - URLs come from approved sources
        data = response.read(max_bytes + 1)
        media_type = str(response.headers.get_content_type()).lower()
    if len(data) > max_bytes or media_type not in {
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/gif",
    }:
        return None
    return data, media_type


def build_bundle(
    *,
    job_manifest: dict[str, Any],
    correlations: CorrelationIds,
    execution: JobExecutionSummary,
    work_root: Path,
    destination: Path,
    created_at: datetime,
    build_limits: BundleBuildLimits | None = None,
    media_fetcher: MediaFetcher | None = default_media_fetcher,
) -> BundlePackageResult:
    """Materialize normalized/raw/media evidence, then package deterministic tar.zst."""

    limits = build_limits or BundleBuildLimits()
    if work_root.exists():
        raise FileExistsError(work_root)
    work_root.mkdir(parents=True)
    job_limits = job_manifest["limits"]
    max_media_per_item = int(job_limits["max_media_per_item"])
    max_media_bytes = int(job_limits["max_media_bytes"])
    bundle_limits = BundleLimits(
        max_items=max(1, len(execution.items)),
        max_requests=int(job_limits["max_requests"]),
        max_files=limits.max_files,
        max_file_bytes=limits.max_file_bytes,
        max_total_bytes=limits.max_total_bytes,
        max_raw_evidence_bytes=limits.max_raw_evidence_bytes,
        max_media_files_per_item=max_media_per_item,
    )
    entries: list[BundleFileEntry] = []
    items: list[BundleItemEntry] = []
    paths: dict[str, Path] = {}
    media_bytes = 0
    try:
        for executed in execution.items:
            result = executed.result
            base = f"items/{executed.item_key}"
            normalized_path: str | None = None
            raw_paths: list[str] = []
            media_paths: list[str] = []
            if result.status is ResultStatus.SUCCESS:
                normalized_path = f"{base}/normalized.json"
                data = _json_bytes(_normalized(result))
                _add_file(
                    work_root,
                    entries,
                    paths,
                    path=normalized_path,
                    data=data,
                    role=BundleFileRole.NORMALIZED,
                    media_type="application/json",
                    source=result.source,
                    source_game_id=result.source_game_id,
                )
            if result.raw is not None:
                raw_path = f"{base}/raw.json"
                data = _json_bytes(result.raw)
                _add_file(
                    work_root,
                    entries,
                    paths,
                    path=raw_path,
                    data=data,
                    role=BundleFileRole.RAW_EVIDENCE,
                    media_type="application/json",
                    source=result.source,
                    source_game_id=result.source_game_id,
                )
                raw_paths.append(raw_path)
            if result.status is ResultStatus.SUCCESS and media_fetcher is not None:
                for index, media in enumerate(
                    result.media[:max_media_per_item], start=1
                ):
                    remaining = min(
                        limits.max_file_bytes, max_media_bytes - media_bytes
                    )
                    if remaining <= 0:
                        break
                    try:
                        fetched = media_fetcher(media.source_url, remaining)
                    except Exception:
                        fetched = None
                    if fetched is None:
                        continue
                    data, media_type = fetched
                    if len(data) > remaining:
                        continue
                    media_bytes += len(data)
                    media_path = (
                        f"{base}/media/{index:02d}-{media.role}"
                        f"{_media_extension(media_type)}"
                    )
                    _add_file(
                        work_root,
                        entries,
                        paths,
                        path=media_path,
                        data=data,
                        role=BundleFileRole.MEDIA,
                        media_type=media_type,
                        source=result.source,
                        source_game_id=result.source_game_id,
                        source_url=media.source_url,
                    )
                    media_paths.append(media_path)
            items.append(
                BundleItemEntry(
                    item_key=executed.item_key,
                    source=result.source,
                    source_game_id=result.source_game_id,
                    target_game_id=executed.target_game_id,
                    status=result.status,
                    normalized_path=normalized_path,
                    raw_evidence_paths=tuple(raw_paths),
                    media_paths=tuple(media_paths),
                    error_category=result.error_category,
                    attempt_count=executed.attempt_count,
                )
            )
        manifest = BundleManifestV2(
            schema_version=2,
            correlations=correlations,
            job_mode=JobMode(job_manifest["mode"]),
            destination=str(job_manifest["destination"]),
            policy_version=str(job_manifest["policy_version"]),
            schedule_occurrence=datetime.fromisoformat(
                str(job_manifest["scheduled_for"]).replace("Z", "+00:00")
            ),
            source_order=tuple(
                SourceName(value) for value in job_manifest["source_order"]
            ),
            created_at=created_at,
            component_versions=(
                ComponentVersion(component="collector-agent", version="0.1.0"),
            ),
            limits=bundle_limits,
            items=tuple(items),
            files=tuple(entries),
        )
        return package_bundle(manifest, files=paths, destination=destination)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(work_root, ignore_errors=True)


def _add_file(
    root: Path,
    entries: list[BundleFileEntry],
    paths: dict[str, Path],
    *,
    path: str,
    data: bytes,
    role: BundleFileRole,
    media_type: str,
    source: SourceName,
    source_game_id: str | None,
    source_url: str | None = None,
) -> None:
    target = root / Path(*path.split("/"))
    _write(target, data)
    entries.append(
        BundleFileEntry(
            path=path,
            role=role,
            media_type=media_type,
            size_bytes=len(data),
            sha256=_sha(data),
            source=source,
            source_game_id=source_game_id,
            source_url=source_url,
        )
    )
    paths[path] = target
