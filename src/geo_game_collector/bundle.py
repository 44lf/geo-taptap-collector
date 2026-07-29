from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import PurePosixPath
from typing import Any

from geo_game_collector.contracts import (
    CorrelationIds,
    ErrorCategory,
    JobMode,
    ResultStatus,
    SourceName,
)

_SHA256 = re.compile(r"[0-9a-f]{64}")


def _validate_sha256(value: str) -> None:
    if _SHA256.fullmatch(value) is None:
        raise ValueError("sha256 must be a lowercase SHA-256 hex digest")


def _validate_safe_path(value: str) -> None:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or str(path) != value
        or any(part in ("", ".", "..") for part in path.parts)
    ):
        raise ValueError(f"path is not a safe POSIX relative path: {value}")


def _utc_iso(value: datetime) -> str:
    offset = value.utcoffset()
    if value.tzinfo is None or offset is None:
        raise ValueError("created_at must include a UTC offset")
    if offset.total_seconds() != 0:
        raise ValueError("created_at must be UTC")
    return value.isoformat().replace("+00:00", "Z")


class BundleFileRole(str, Enum):
    NORMALIZED = "normalized"
    RAW_EVIDENCE = "raw_evidence"
    MEDIA = "media"


@dataclass(frozen=True, slots=True)
class ComponentVersion:
    component: str
    version: str

    def __post_init__(self) -> None:
        if not self.component or not self.version:
            raise ValueError("component and version must be non-empty")


@dataclass(frozen=True, slots=True)
class BundleLimits:
    max_items: int
    max_requests: int
    max_files: int
    max_file_bytes: int
    max_total_bytes: int
    max_raw_evidence_bytes: int
    max_media_files_per_item: int

    def __post_init__(self) -> None:
        for field_name in (
            "max_items",
            "max_requests",
            "max_files",
            "max_file_bytes",
            "max_total_bytes",
            "max_raw_evidence_bytes",
        ):
            if getattr(self, field_name) <= 0:
                raise ValueError(f"{field_name} must be positive")
        if self.max_media_files_per_item < 0:
            raise ValueError("max_media_files_per_item must be non-negative")


@dataclass(frozen=True, slots=True)
class BundleFileEntry:
    path: str
    role: BundleFileRole
    media_type: str
    size_bytes: int
    sha256: str
    source: SourceName
    source_game_id: str | None = None
    source_url: str | None = None

    def __post_init__(self) -> None:
        _validate_safe_path(self.path)
        _validate_sha256(self.sha256)
        if not self.media_type:
            raise ValueError("media_type must be non-empty")
        if self.size_bytes < 0:
            raise ValueError("size_bytes must be non-negative")
        if self.role is BundleFileRole.MEDIA and not self.source_url:
            raise ValueError("media inventory entries require source_url")


@dataclass(frozen=True, slots=True)
class BundleItemEntry:
    item_key: str
    source: SourceName
    source_game_id: str | None
    target_game_id: int | None
    status: ResultStatus
    normalized_path: str | None
    raw_evidence_paths: tuple[str, ...]
    media_paths: tuple[str, ...]
    error_category: ErrorCategory | None = None
    http_status: int | None = None
    stage: str | None = None
    attempt_count: int = 1

    def __post_init__(self) -> None:
        if not self.item_key:
            raise ValueError("item_key must be non-empty")
        if self.target_game_id is not None and self.target_game_id <= 0:
            raise ValueError("target_game_id must be positive")
        paths = (
            (() if self.normalized_path is None else (self.normalized_path,))
            + self.raw_evidence_paths
            + self.media_paths
        )
        if len(paths) != len(set(paths)):
            raise ValueError("item paths must be unique")
        for path in paths:
            _validate_safe_path(path)
        if self.status is ResultStatus.SUCCESS and (
            not self.source_game_id or self.normalized_path is None
        ):
            raise ValueError(
                "successful item requires source_game_id and normalized_path"
            )
        if self.status is ResultStatus.BLOCKED:
            if self.error_category is not ErrorCategory.BLOCKED:
                raise ValueError("blocked item requires blocked error_category")
        elif self.status is ResultStatus.ERROR:
            if (
                self.error_category is None
                or self.error_category is ErrorCategory.BLOCKED
            ):
                raise ValueError("error item requires a non-blocked error_category")
        elif self.error_category is not None:
            raise ValueError("success and miss items must not have an error_category")
        if self.http_status is not None and not 100 <= self.http_status <= 599:
            raise ValueError("http_status must be between 100 and 599")
        if self.stage is not None and not self.stage.strip():
            raise ValueError("stage must be non-empty when provided")
        if self.attempt_count <= 0:
            raise ValueError("attempt_count must be positive")


@dataclass(frozen=True, slots=True)
class BundleManifestV2:
    schema_version: int
    correlations: CorrelationIds
    job_mode: JobMode
    destination: str
    policy_version: str
    schedule_occurrence: datetime
    source_order: tuple[SourceName, ...]
    created_at: datetime
    component_versions: tuple[ComponentVersion, ...]
    limits: BundleLimits
    items: tuple[BundleItemEntry, ...]
    files: tuple[BundleFileEntry, ...]

    def __post_init__(self) -> None:
        if self.schema_version != 2:
            raise ValueError("Bundle schema_version must be 2")
        if not self.destination:
            raise ValueError("destination must be non-empty")
        if not self.policy_version:
            raise ValueError("policy_version must be non-empty")
        _utc_iso(self.created_at)
        _utc_iso(self.schedule_occurrence)
        if not self.source_order or len(self.source_order) != len(
            set(self.source_order)
        ):
            raise ValueError("source_order must be non-empty and unique")
        component_names = tuple(item.component for item in self.component_versions)
        if not component_names or len(component_names) != len(set(component_names)):
            raise ValueError("component_versions must be non-empty and unique")
        if not self.items or len(self.items) > self.limits.max_items:
            raise ValueError("items exceed max_items or are empty")
        if len(self.files) > self.limits.max_files:
            raise ValueError("files exceed max_files")
        item_keys = tuple(item.item_key for item in self.items)
        if len(item_keys) != len(set(item_keys)):
            raise ValueError("item_key values must be unique")
        file_by_path = {entry.path: entry for entry in self.files}
        if len(file_by_path) != len(self.files):
            raise ValueError("file inventory paths must be unique")
        if any(item.source not in self.source_order for item in self.items):
            raise ValueError("item source must appear in source_order")
        if self.job_mode is JobMode.REFRESH and any(
            item.target_game_id is None for item in self.items
        ):
            raise ValueError("refresh item requires target_game_id")

        referenced: set[str] = set()
        for item in self.items:
            expected = (
                (
                    ()
                    if item.normalized_path is None
                    else ((item.normalized_path, BundleFileRole.NORMALIZED),)
                )
                + tuple(
                    (path, BundleFileRole.RAW_EVIDENCE)
                    for path in item.raw_evidence_paths
                )
                + tuple((path, BundleFileRole.MEDIA) for path in item.media_paths)
            )
            for path, role in expected:
                if path in referenced:
                    raise ValueError("file path referenced by multiple items")
                referenced.add(path)
                entry = file_by_path.get(path)
                if entry is None:
                    raise ValueError(
                        f"referenced file is absent from inventory: {path}"
                    )
                if entry.role is not role:
                    raise ValueError(f"inventory role mismatch for {path}")
                if entry.source is not item.source:
                    raise ValueError(f"inventory source mismatch for {path}")
        unreferenced = set(file_by_path) - referenced
        if unreferenced:
            raise ValueError(f"unreferenced inventory files: {sorted(unreferenced)}")

        if any(entry.size_bytes > self.limits.max_file_bytes for entry in self.files):
            raise ValueError("file exceeds max_file_bytes")
        if sum(entry.size_bytes for entry in self.files) > self.limits.max_total_bytes:
            raise ValueError("inventory exceeds max_total_bytes")
        raw_bytes = sum(
            entry.size_bytes
            for entry in self.files
            if entry.role is BundleFileRole.RAW_EVIDENCE
        )
        if raw_bytes > self.limits.max_raw_evidence_bytes:
            raise ValueError("raw evidence exceeds max_raw_evidence_bytes")
        if any(
            len(item.media_paths) > self.limits.max_media_files_per_item
            for item in self.items
        ):
            raise ValueError("item exceeds max_media_files_per_item")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "correlations": {
                field: getattr(self.correlations, field)
                for field in (
                    "collector_id",
                    "job_id",
                    "run_id",
                    "bundle_id",
                    "transport_id",
                )
            },
            "job": {
                "mode": self.job_mode.value,
                "destination": self.destination,
                "policy_version": self.policy_version,
                "schedule_occurrence": _utc_iso(self.schedule_occurrence),
                "source_order": [source.value for source in self.source_order],
            },
            "created_at": _utc_iso(self.created_at),
            "component_versions": {
                item.component: item.version for item in self.component_versions
            },
            "limits": {
                field: getattr(self.limits, field)
                for field in (
                    "max_items",
                    "max_requests",
                    "max_files",
                    "max_file_bytes",
                    "max_total_bytes",
                    "max_raw_evidence_bytes",
                    "max_media_files_per_item",
                )
            },
            "items": [
                {
                    "item_key": item.item_key,
                    "source": item.source.value,
                    "source_game_id": item.source_game_id,
                    "target_game_id": item.target_game_id,
                    "status": item.status.value,
                    "normalized_path": item.normalized_path,
                    "raw_evidence_paths": list(item.raw_evidence_paths),
                    "media_paths": list(item.media_paths),
                    "error_category": (
                        item.error_category.value
                        if item.error_category is not None
                        else None
                    ),
                    "http_status": item.http_status,
                    "stage": item.stage,
                    "attempt_count": item.attempt_count,
                }
                for item in self.items
            ],
            "files": [
                {
                    "path": entry.path,
                    "role": entry.role.value,
                    "media_type": entry.media_type,
                    "size_bytes": entry.size_bytes,
                    "sha256": entry.sha256,
                    "source": entry.source.value,
                    "source_game_id": entry.source_game_id,
                    "source_url": entry.source_url,
                }
                for entry in self.files
            ],
        }


@dataclass(frozen=True, slots=True)
class ArchiveIntegrity:
    object_key: str
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        if not self.object_key:
            raise ValueError("object_key must be non-empty")
        if self.size_bytes <= 0:
            raise ValueError("archive size_bytes must be positive")
        _validate_sha256(self.sha256)


@dataclass(frozen=True, slots=True)
class BundleDescriptorV2:
    manifest: BundleManifestV2
    archive: ArchiveIntegrity

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest": self.manifest.to_dict(),
            "archive": {
                "object_key": self.archive.object_key,
                "size_bytes": self.archive.size_bytes,
                "sha256": self.archive.sha256,
            },
        }
