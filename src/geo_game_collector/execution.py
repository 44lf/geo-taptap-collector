"""Unified, bounded execution path for Gateway-issued refresh/discovery jobs."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from geo_game_collector.contracts import (
    CanonicalGameResult,
    ErrorCategory,
    EvidenceReference,
    MediaReference,
    ResultStatus,
    SourceName,
)
from geo_game_collector.sources import baidu, ninegame
from geo_game_collector.sources.taptap import (
    BLOCKED_HTTP_STATUSES,
    TapTapAdapter,
    TapTapBlockedError,
)
from geo_game_collector.sources.yingyongbao import (
    LIST_PATHS,
    UrllibPageClient,
    YingYongBaoAdapter,
)


class JobExecutionError(RuntimeError):
    pass


class SourceRuntime(Protocol):
    def refresh(self, source: SourceName, *, name: str) -> CanonicalGameResult: ...

    def discover(
        self,
        source: SourceName,
        *,
        path: str,
        limit: int,
    ) -> tuple[CanonicalGameResult, ...]: ...


@dataclass(frozen=True, slots=True)
class ExecutedItem:
    item_key: str
    target_game_id: int | None
    category_id: int | None
    result: CanonicalGameResult
    attempt_count: int = 1


@dataclass(frozen=True, slots=True)
class JobExecutionSummary:
    items: tuple[ExecutedItem, ...]
    request_count: int
    halted_by_block: bool
    completed_item_keys: tuple[str, ...]


def executed_item_to_payload(item: ExecutedItem) -> dict[str, Any]:
    result = item.result
    return {
        "item_key": item.item_key,
        "target_game_id": item.target_game_id,
        "category_id": item.category_id,
        "attempt_count": item.attempt_count,
        "result": {
            "source": result.source.value,
            "requested_name": result.requested_name,
            "status": result.status.value,
            "source_game_id": result.source_game_id,
            "name": result.name,
            "score": result.score,
            "description": result.description,
            "tags": list(result.tags),
            "platforms": list(result.platforms),
            "comment_count": result.comment_count,
            "icon_url": result.icon_url,
            "screenshot_urls": list(result.screenshot_urls),
            "android_package": result.android_package,
            "raw": result.raw,
            "media": [
                {"role": media.role, "source_url": media.source_url}
                for media in result.media
            ],
            "evidence": [
                {
                    "stage": evidence.stage,
                    "media_type": evidence.media_type,
                    "path": evidence.path,
                }
                for evidence in result.evidence
            ],
            "error_category": (
                result.error_category.value
                if result.error_category is not None
                else None
            ),
        },
    }


def executed_item_from_payload(payload: dict[str, Any]) -> ExecutedItem:
    try:
        result = payload["result"]
        if not isinstance(result, dict):
            raise TypeError
        raw = result.get("raw")
        if raw is not None and not isinstance(raw, dict):
            raise TypeError
        error_category = result.get("error_category")
        return ExecutedItem(
            item_key=str(payload["item_key"]),
            target_game_id=(
                int(payload["target_game_id"])
                if payload.get("target_game_id") is not None
                else None
            ),
            category_id=(
                int(payload["category_id"])
                if payload.get("category_id") is not None
                else None
            ),
            attempt_count=int(payload.get("attempt_count", 1)),
            result=CanonicalGameResult(
                source=SourceName(str(result["source"])),
                requested_name=str(result["requested_name"]),
                status=ResultStatus(str(result["status"])),
                source_game_id=(
                    str(result["source_game_id"])
                    if result.get("source_game_id") is not None
                    else None
                ),
                name=(str(result["name"]) if result.get("name") is not None else None),
                score=(
                    float(result["score"]) if result.get("score") is not None else None
                ),
                description=(
                    str(result["description"])
                    if result.get("description") is not None
                    else None
                ),
                tags=tuple(str(value) for value in result.get("tags", [])),
                platforms=tuple(str(value) for value in result.get("platforms", [])),
                comment_count=(
                    int(result["comment_count"])
                    if result.get("comment_count") is not None
                    else None
                ),
                icon_url=(
                    str(result["icon_url"])
                    if result.get("icon_url") is not None
                    else None
                ),
                screenshot_urls=tuple(
                    str(value) for value in result.get("screenshot_urls", [])
                ),
                android_package=(
                    str(result["android_package"])
                    if result.get("android_package") is not None
                    else None
                ),
                raw=raw,
                media=tuple(
                    MediaReference(
                        role=str(media["role"]),
                        source_url=str(media["source_url"]),
                    )
                    for media in result.get("media", [])
                ),
                evidence=tuple(
                    EvidenceReference(
                        stage=str(evidence["stage"]),
                        media_type=str(evidence["media_type"]),
                        path=str(evidence["path"]),
                    )
                    for evidence in result.get("evidence", [])
                ),
                error_category=(
                    ErrorCategory(str(error_category))
                    if error_category is not None
                    else None
                ),
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise JobExecutionError("persisted item result is invalid") from exc


def _normalized_name(value: str) -> str:
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE).casefold()


def _matches(candidate: str | None, target: str) -> bool:
    return candidate is not None and _normalized_name(candidate) == _normalized_name(
        target
    )


class ProvenSourceRuntime:
    """Runtime bindings around the four migrated, fixture-tested source adapters."""

    def __init__(
        self,
        *,
        taptap: TapTapAdapter | None = None,
        yingyongbao: YingYongBaoAdapter | None = None,
    ) -> None:
        self._taptap = taptap or TapTapAdapter()
        self._yingyongbao = yingyongbao or YingYongBaoAdapter(UrllibPageClient())

    def refresh(self, source: SourceName, *, name: str) -> CanonicalGameResult:
        if source is SourceName.BAIDU:
            return baidu.search_by_name(name, matcher=_matches)
        if source is SourceName.NINEGAME:
            return ninegame.search_by_name(name, matcher=_matches)
        if source is SourceName.TAPTAP:
            return self._taptap.search_by_name(name)
        discovered = self._yingyongbao.discover(
            paths=LIST_PATHS,
            with_detail=True,
            detail_limit=15,
        )
        return next(
            (result for result in discovered.results if _matches(result.name, name)),
            CanonicalGameResult(
                source=SourceName.YINGYONGBAO,
                requested_name=name,
                status=ResultStatus.MISS,
            ),
        )

    def discover(
        self,
        source: SourceName,
        *,
        path: str,
        limit: int,
    ) -> tuple[CanonicalGameResult, ...]:
        if limit <= 0:
            raise ValueError("discovery limit must be positive")
        if source is SourceName.BAIDU:
            return baidu.search(path, page_size=min(limit, 20))
        if source is SourceName.TAPTAP:
            return self._taptap.search_category(path, page_size=min(limit, 20))
        if source is SourceName.YINGYONGBAO:
            return self._yingyongbao.discover(
                paths=(path,),
                with_detail=True,
                detail_limit=min(limit, 15),
            ).results[:limit]
        raise JobExecutionError("NineGame discovery is not an approved execution path")


def _blocked_status(error: BaseException) -> int | None:
    if isinstance(error, TapTapBlockedError):
        return error.status
    status = getattr(error, "status", None)
    if status is None:
        status = getattr(error, "code", None)
    return int(status) if status in BLOCKED_HTTP_STATUSES else None


def _error_result(
    source: SourceName,
    *,
    requested_name: str,
    error: BaseException,
) -> tuple[CanonicalGameResult, bool]:
    status = _blocked_status(error)
    if status is not None:
        return (
            CanonicalGameResult(
                source=source,
                requested_name=requested_name,
                status=ResultStatus.BLOCKED,
                raw={"http_status": status, "error_type": type(error).__name__},
                error_category=ErrorCategory.BLOCKED,
            ),
            True,
        )
    category = (
        ErrorCategory.CONFIGURATION
        if isinstance(error, (ValueError, JobExecutionError))
        else ErrorCategory.RETRYABLE
    )
    return (
        CanonicalGameResult(
            source=source,
            requested_name=requested_name,
            status=ResultStatus.ERROR,
            raw={"error_type": type(error).__name__},
            error_category=category,
        ),
        False,
    )


def _safe_component(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")
    return (normalized or "item")[:80]


class JobExecutor:
    def __init__(self, runtime: SourceRuntime) -> None:
        self._runtime = runtime

    def execute(
        self,
        manifest: dict[str, Any],
        *,
        completed_item_keys: Sequence[str] = (),
        prior_request_count: int = 0,
        on_item: Callable[[ExecutedItem, int], None] | None = None,
    ) -> JobExecutionSummary:
        mode = manifest.get("mode")
        if mode not in {"refresh", "discovery"}:
            raise JobExecutionError("job mode is invalid")
        try:
            sources = tuple(SourceName(value) for value in manifest["source_order"])
            max_requests = int(manifest["limits"]["max_requests"])
        except (KeyError, TypeError, ValueError) as exc:
            raise JobExecutionError("job source order or limits are invalid") from exc
        if not sources or max_requests <= 0 or prior_request_count < 0:
            raise JobExecutionError(
                "job source order and max_requests must be positive"
            )
        if prior_request_count > max_requests:
            raise JobExecutionError("persisted request count exceeds job max_requests")
        completed = set(completed_item_keys)
        items: list[ExecutedItem] = []
        request_count = prior_request_count
        halted = False

        if mode == "refresh":
            targets = manifest.get("targets")
            if not isinstance(targets, list) or not targets:
                raise JobExecutionError("refresh job requires targets")
            for target in targets:
                if not isinstance(target, dict):
                    raise JobExecutionError("refresh target is invalid")
                target_id = target.get("target_game_id")
                name = target.get("name")
                category_id = target.get("category_id")
                if not isinstance(target_id, int) or not isinstance(name, str):
                    raise JobExecutionError("refresh target identity is invalid")
                for source in sources:
                    item_key = f"target-{target_id}-{source.value}"
                    if item_key in completed:
                        continue
                    if request_count >= max_requests:
                        raise JobExecutionError("job max_requests exhausted")
                    request_count += 1
                    try:
                        result = self._runtime.refresh(source, name=name)
                        stop = False
                    except Exception as exc:
                        result, stop = _error_result(
                            source,
                            requested_name=name,
                            error=exc,
                        )
                    item = ExecutedItem(
                        item_key=item_key,
                        target_game_id=target_id,
                        category_id=category_id,
                        result=result,
                    )
                    if on_item is not None:
                        on_item(item, request_count)
                    items.append(item)
                    completed.add(item_key)
                    if stop:
                        halted = True
                        break
                if halted:
                    break
        else:
            paths = manifest.get("discovery_paths")
            if not isinstance(paths, list) or not paths:
                raise JobExecutionError("discovery job requires paths")
            for raw_path in paths:
                if not isinstance(raw_path, dict):
                    raise JobExecutionError("discovery path is invalid")
                source = SourceName(raw_path.get("source"))
                path = raw_path.get("path")
                if not isinstance(path, str) or not path:
                    raise JobExecutionError("discovery path is invalid")
                if request_count >= max_requests:
                    raise JobExecutionError("job max_requests exhausted")
                request_count += 1
                try:
                    results = self._runtime.discover(
                        source,
                        path=path,
                        limit=max_requests - request_count + 1,
                    )
                except Exception as exc:
                    error, stop = _error_result(
                        source,
                        requested_name=path,
                        error=exc,
                    )
                    results = (error,)
                    halted = stop
                for index, result in enumerate(results):
                    identity = result.source_game_id or f"result-{index}"
                    item_key = f"discovery-{source.value}-{_safe_component(identity)}"
                    if item_key in completed:
                        continue
                    item = ExecutedItem(
                        item_key=item_key,
                        target_game_id=None,
                        category_id=None,
                        result=result,
                    )
                    if on_item is not None:
                        on_item(item, request_count)
                    items.append(item)
                    completed.add(item_key)
                if halted:
                    break
        return JobExecutionSummary(
            items=tuple(items),
            request_count=request_count,
            halted_by_block=halted,
            completed_item_keys=tuple(sorted(completed)),
        )
