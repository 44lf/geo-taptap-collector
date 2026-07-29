from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any


class SourceName(str, Enum):
    BAIDU = "baidu"
    NINEGAME = "ninegame"
    YINGYONGBAO = "yingyongbao"
    TAPTAP = "taptap"


SUPPORTED_SOURCES = (
    SourceName.BAIDU,
    SourceName.NINEGAME,
    SourceName.YINGYONGBAO,
    SourceName.TAPTAP,
)


class ResultStatus(str, Enum):
    SUCCESS = "success"
    MISS = "miss"
    BLOCKED = "blocked"
    ERROR = "error"


class ErrorCategory(str, Enum):
    BLOCKED = "blocked"
    CONFIGURATION = "configuration"
    PERMANENT = "permanent"
    RETRYABLE = "retryable"
    SECURITY = "security"


class JobMode(str, Enum):
    REFRESH = "refresh"
    DISCOVERY = "discovery"


def _require_text(field_name: str, value: str) -> None:
    if not value or value != value.strip():
        raise ValueError(f"{field_name} must be non-empty trimmed text")


def _require_utc(field_name: str, value: datetime) -> None:
    offset = value.utcoffset()
    if value.tzinfo is None or offset is None:
        raise ValueError(f"{field_name} must include a UTC offset")
    if offset.total_seconds() != 0:
        raise ValueError(f"{field_name} must be UTC")


@dataclass(frozen=True, slots=True)
class MediaReference:
    role: str
    source_url: str

    def __post_init__(self) -> None:
        _require_text("role", self.role)
        _require_text("source_url", self.source_url)


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    stage: str
    media_type: str
    path: str

    def __post_init__(self) -> None:
        _require_text("stage", self.stage)
        _require_text("media_type", self.media_type)
        _require_text("path", self.path)


@dataclass(frozen=True, slots=True)
class CanonicalGameResult:
    source: SourceName
    requested_name: str
    status: ResultStatus
    source_game_id: str | None = None
    name: str | None = None
    score: float | None = None
    description: str | None = None
    tags: tuple[str, ...] = ()
    platforms: tuple[str, ...] = ()
    comment_count: int | None = None
    icon_url: str | None = None
    screenshot_urls: tuple[str, ...] = ()
    android_package: str | None = None
    raw: dict[str, Any] | None = None
    media: tuple[MediaReference, ...] = ()
    evidence: tuple[EvidenceReference, ...] = ()
    error_category: ErrorCategory | None = None

    def __post_init__(self) -> None:
        _require_text("requested_name", self.requested_name)
        if self.status is ResultStatus.SUCCESS and (
            not self.source_game_id or not self.name
        ):
            raise ValueError("successful result requires source_game_id and name")
        if self.status is ResultStatus.BLOCKED:
            if self.error_category is not ErrorCategory.BLOCKED:
                raise ValueError("blocked result requires blocked error_category")
        elif self.status is ResultStatus.ERROR:
            if (
                self.error_category is None
                or self.error_category is ErrorCategory.BLOCKED
            ):
                raise ValueError("error result requires a non-blocked error_category")
        elif self.error_category is not None:
            raise ValueError("success and miss results must not have an error_category")
        if len(set(self.tags)) != len(self.tags):
            raise ValueError("tags must be unique")
        if len(set(self.platforms)) != len(self.platforms):
            raise ValueError("platforms must be unique")
        if len(set(self.screenshot_urls)) != len(self.screenshot_urls):
            raise ValueError("screenshot_urls must be unique")
        if self.comment_count is not None and self.comment_count < 0:
            raise ValueError("comment_count must be non-negative")


@dataclass(frozen=True, slots=True)
class RefreshTarget:
    target_game_id: int
    name: str
    category_id: int

    def __post_init__(self) -> None:
        if self.target_game_id <= 0:
            raise ValueError("target_game_id must be positive")
        if self.category_id <= 0:
            raise ValueError("category_id must be positive")
        _require_text("name", self.name)


@dataclass(frozen=True, slots=True)
class DiscoveryPath:
    source: SourceName
    path: str

    def __post_init__(self) -> None:
        _require_text("path", self.path)


@dataclass(frozen=True, slots=True)
class JobLimits:
    max_targets: int
    max_discovery_paths: int
    max_requests: int
    max_media_per_item: int
    max_media_bytes: int

    def __post_init__(self) -> None:
        for field_name in (
            "max_targets",
            "max_discovery_paths",
            "max_requests",
            "max_media_bytes",
        ):
            if getattr(self, field_name) <= 0:
                raise ValueError(f"{field_name} must be positive")
        if self.max_media_per_item < 0:
            raise ValueError("max_media_per_item must be non-negative")


@dataclass(frozen=True, slots=True)
class JobManifest:
    schema_version: int
    job_id: str
    policy_version: str
    mode: JobMode
    source_order: tuple[SourceName, ...]
    targets: tuple[RefreshTarget, ...]
    discovery_paths: tuple[DiscoveryPath, ...]
    limits: JobLimits
    scheduled_for: datetime
    destination: str

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("job manifest schema_version must be 1")
        for field_name in ("job_id", "policy_version", "destination"):
            _require_text(field_name, getattr(self, field_name))
        _require_utc("scheduled_for", self.scheduled_for)
        if not self.source_order or len(set(self.source_order)) != len(
            self.source_order
        ):
            raise ValueError("source_order must be non-empty and unique")
        if len(self.targets) > self.limits.max_targets:
            raise ValueError("targets exceed max_targets")
        if len(self.discovery_paths) > self.limits.max_discovery_paths:
            raise ValueError("discovery_paths exceed max_discovery_paths")
        if self.mode is JobMode.REFRESH:
            if not self.targets or self.discovery_paths:
                raise ValueError("refresh job requires targets and no discovery_paths")
        elif not self.discovery_paths or self.targets:
            raise ValueError("discovery job requires discovery_paths and no targets")
        referenced_sources = {path.source for path in self.discovery_paths}
        if not referenced_sources.issubset(self.source_order):
            raise ValueError("discovery path source must appear in source_order")


@dataclass(frozen=True, slots=True)
class CorrelationIds:
    collector_id: str
    job_id: str
    run_id: str
    bundle_id: str
    transport_id: str

    def __post_init__(self) -> None:
        for field_name in (
            "collector_id",
            "job_id",
            "run_id",
            "bundle_id",
            "transport_id",
        ):
            _require_text(field_name, getattr(self, field_name))
