from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.request import Request, build_opener

from geo_game_collector.contracts import (
    CanonicalGameResult,
    EvidenceReference,
    MediaReference,
    ResultStatus,
    SourceName,
)

BASE_URL = "https://sj.qq.com"
SELF_PACKAGE = "com.tencent.android.qqdownloader"
LIST_PATHS = (
    "/hot-game-list",
    "/new-game-list",
    "/reserve-game-list",
    "/recommend-list",
    "/game/rpg",
    "/game/avg",
    "/game/xiuxianyizhi",
    "/game/chuangxinpinlei",
    "/tag/moba",
    "/tag/roguelike",
    "/tag/openworld",
    "/tag/sandbox",
    "/tag/rpg",
    "/tag/racing",
    "/tag/fighting",
    "/tag/chess",
)

_NEXT_DATA_RE = re.compile(
    r'id="__NEXT_DATA__"[^>]*>(.*?)</script>',
    re.DOTALL,
)
_SAFE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9._-]+")


class PageClient(Protocol):
    def get(self, url: str) -> tuple[int, str]: ...


class UrllibPageClient:
    """Small HTTPS client using Python's default certificate verification."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 20.0,
        max_response_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._opener = build_opener()

    def get(self, url: str) -> tuple[int, str]:
        request = Request(  # noqa: S310 - URL is constrained by the adapter.
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "User-Agent": "geo-game-collector/0.1",
            },
            method="GET",
        )
        try:
            with self._opener.open(request, timeout=self._timeout_seconds) as response:
                payload = response.read(self._max_response_bytes + 1)
                status = int(response.status)
        except HTTPError as exc:
            payload = exc.read(self._max_response_bytes + 1)
            status = int(exc.code)
        if len(payload) > self._max_response_bytes:
            raise ValueError("response exceeds max_response_bytes")
        return status, payload.decode("utf-8", errors="replace")


@dataclass(frozen=True, slots=True)
class RawPageEvidence:
    stage: str
    url: str
    http_status: int
    body: str
    path: str
    media_type: str = "text/html"

    @property
    def reference(self) -> EvidenceReference:
        return EvidenceReference(
            stage=self.stage,
            media_type=self.media_type,
            path=self.path,
        )


@dataclass(frozen=True, slots=True)
class ListPageResult:
    results: tuple[CanonicalGameResult, ...]
    evidence: RawPageEvidence

    @property
    def http_status(self) -> int:
        return self.evidence.http_status


@dataclass(frozen=True, slots=True)
class DetailPageResult:
    result: CanonicalGameResult | None
    evidence: RawPageEvidence


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    results: tuple[CanonicalGameResult, ...]
    evidence: tuple[RawPageEvidence, ...]
    request_count: int
    detail_attempts: int


def _page_props(text: str) -> dict[str, Any] | None:
    match = _NEXT_DATA_RE.search(text)
    if match is None:
        return None
    try:
        data = json.loads(match.group(1))
    except (TypeError, ValueError):
        return None
    props = data.get("props") if isinstance(data, dict) else None
    page_props = props.get("pageProps") if isinstance(props, dict) else None
    return page_props if isinstance(page_props, dict) else {}


def _collect_cards(value: object, accumulator: list[dict[str, Any]]) -> None:
    if isinstance(value, dict):
        package = value.get("pkg_name")
        if (
            isinstance(package, str)
            and package
            and (value.get("name") or value.get("app_name"))
        ):
            accumulator.append(value)
        for child in value.values():
            _collect_cards(child, accumulator)
    elif isinstance(value, list):
        for child in value:
            _collect_cards(child, accumulator)


def _positive_number(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _deduplicate(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _tags_from(node: dict[str, Any]) -> tuple[str, ...]:
    structured = node.get("tags_st")
    if structured:
        try:
            tags = json.loads(structured) if isinstance(structured, str) else structured
            if isinstance(tags, list):
                names = [
                    str(tag["tag_name"])
                    for tag in tags
                    if isinstance(tag, dict) and tag.get("tag_name")
                ]
                if names:
                    return _deduplicate(names)
        except (TypeError, ValueError):
            pass
    raw = node.get("tags")
    if isinstance(raw, str) and raw:
        return _deduplicate([tag for tag in raw.split(",") if tag])
    return ()


def _screenshots_from(node: dict[str, Any]) -> tuple[str, ...]:
    snapshots = node.get("audited_snapshots")
    if not isinstance(snapshots, str) or not snapshots:
        return ()
    return _deduplicate([url for url in snapshots.split(",") if url.startswith("http")])


def _platforms_from(node: dict[str, Any]) -> tuple[str, ...]:
    tags = node.get("tags")
    return (
        ("android", "ios") if isinstance(tags, str) and "iOS" in tags else ("android",)
    )


def _media_references(
    icon_url: str | None, screenshot_urls: tuple[str, ...]
) -> tuple[MediaReference, ...]:
    media: list[MediaReference] = []
    if icon_url:
        media.append(MediaReference(role="icon", source_url=icon_url))
    media.extend(
        MediaReference(role="screenshot", source_url=url) for url in screenshot_urls
    )
    return tuple(media)


def _card_to_result(
    node: dict[str, Any],
    evidence: RawPageEvidence,
    *,
    detail_fetched: bool,
    highlight_comments: list[dict[str, str | None]] | None = None,
) -> CanonicalGameResult:
    package = str(node.get("pkg_name") or "")
    name = str(node.get("name") or node.get("app_name") or "")
    icon = node.get("icon")
    icon_url = icon if isinstance(icon, str) and icon else None
    screenshots = _screenshots_from(node)
    description = node.get("description")
    category = node.get("cate_name_new") or node.get("cate_name")
    source_url = f"{BASE_URL}/appdetail/{package}"
    raw = {
        "category": category if isinstance(category, str) else None,
        "detail_fetched": detail_fetched,
        "highlight_comments": highlight_comments or [],
        "source_url": source_url,
    }
    return CanonicalGameResult(
        source=SourceName.YINGYONGBAO,
        requested_name=name,
        status=ResultStatus.SUCCESS,
        source_game_id=package,
        name=name,
        score=_positive_number(node.get("average_rating")),
        description=(
            description if isinstance(description, str) and description else None
        ),
        tags=_tags_from(node),
        platforms=_platforms_from(node),
        icon_url=icon_url,
        screenshot_urls=screenshots,
        android_package=package,
        raw=raw,
        media=_media_references(icon_url, screenshots),
        evidence=(evidence.reference,),
    )


def _find_game_detail_item(
    page_props: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    dynamic = page_props.get("dynamicCardResponse")
    dynamic_data = dynamic.get("data") if isinstance(dynamic, dict) else None
    components = (
        dynamic_data.get("components") if isinstance(dynamic_data, dict) else None
    )
    if not isinstance(components, list):
        return None, None
    for component in components:
        data = component.get("data") if isinstance(component, dict) else None
        if not isinstance(data, dict) or data.get("name") != "GameDetail":
            continue
        items = data.get("itemData")
        if not isinstance(items, list) or not items:
            return None, None
        main = items[0] if isinstance(items[0], dict) else None
        comments_item = next(
            (item for item in items if isinstance(item, dict) and item.get("comments")),
            None,
        )
        return main, comments_item
    return None, None


def _comments_from(
    item: dict[str, Any] | None,
) -> list[dict[str, str | None]]:
    comments = item.get("comments") if item is not None else None
    if not isinstance(comments, list):
        return []
    output: list[dict[str, str | None]] = []
    for entry in comments:
        if not isinstance(entry, dict):
            continue
        comment = entry.get("comment")
        content = comment.get("content") if isinstance(comment, dict) else None
        if not isinstance(content, str) or not content:
            continue
        user = entry.get("user")
        nickname = user.get("nickName") if isinstance(user, dict) else None
        output.append(
            {
                "user": nickname if isinstance(nickname, str) else None,
                "content": content,
            }
        )
    return output[:10]


def _safe_evidence_component(value: str) -> str:
    component = _SAFE_COMPONENT_RE.sub("-", value.strip("/"))
    return component or "root"


def _merge_detail(
    base: CanonicalGameResult, detail: CanonicalGameResult
) -> CanonicalGameResult:
    screenshots = (
        detail.screenshot_urls
        if len(detail.screenshot_urls) > len(base.screenshot_urls)
        else base.screenshot_urls
    )
    tags = detail.tags if len(detail.tags) > len(base.tags) else base.tags
    raw = dict(base.raw or {})
    raw.update(detail.raw or {})
    icon_url = base.icon_url or detail.icon_url
    return replace(
        base,
        score=base.score if base.score is not None else detail.score,
        description=base.description or detail.description,
        tags=tags,
        comment_count=(
            base.comment_count
            if base.comment_count is not None
            else detail.comment_count
        ),
        icon_url=icon_url,
        screenshot_urls=screenshots,
        raw=raw,
        media=_media_references(icon_url, screenshots),
        evidence=base.evidence + detail.evidence,
    )


class YingYongBaoAdapter:
    source = SourceName.YINGYONGBAO

    def __init__(
        self,
        client: PageClient,
        *,
        max_detail_requests: int = 15,
        max_list_paths: int = len(LIST_PATHS),
        max_response_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        if max_detail_requests < 0:
            raise ValueError("max_detail_requests must be non-negative")
        if max_list_paths <= 0:
            raise ValueError("max_list_paths must be positive")
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        self._client = client
        self._max_detail_requests = max_detail_requests
        self._max_list_paths = max_list_paths
        self._max_response_bytes = max_response_bytes

    def _fetch(self, url: str, *, stage: str, path: str) -> RawPageEvidence:
        status, text = self._client.get(url)
        if len(text.encode("utf-8")) > self._max_response_bytes:
            raise ValueError("response exceeds max_response_bytes")
        return RawPageEvidence(
            stage=stage,
            url=url,
            http_status=status,
            body=text,
            path=path,
        )

    def list_page(self, path: str) -> ListPageResult:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("list path must be a root-relative HTTPS path")
        evidence = self._fetch(
            BASE_URL + path,
            stage="list",
            path=(f"evidence/yingyongbao/list/{_safe_evidence_component(path)}.html"),
        )
        if evidence.http_status != 200 or not evidence.body:
            return ListPageResult(results=(), evidence=evidence)
        page_props = _page_props(evidence.body)
        if page_props is None:
            return ListPageResult(results=(), evidence=evidence)
        cards: list[dict[str, Any]] = []
        _collect_cards(page_props, cards)
        results = tuple(
            _card_to_result(card, evidence, detail_fetched=False)
            for card in cards
            if card.get("pkg_name") != SELF_PACKAGE
        )
        return ListPageResult(results=results, evidence=evidence)

    def detail(self, package: str) -> DetailPageResult:
        if (
            not package
            or _SAFE_COMPONENT_RE.search(package) is not None
            or "/" in package
        ):
            raise ValueError("package must be a safe source identity")
        evidence = self._fetch(
            f"{BASE_URL}/appdetail/{package}",
            stage="detail",
            path=(
                f"evidence/yingyongbao/detail/{_safe_evidence_component(package)}.html"
            ),
        )
        if evidence.http_status != 200 or not evidence.body:
            return DetailPageResult(result=None, evidence=evidence)
        page_props = _page_props(evidence.body)
        if page_props is None:
            return DetailPageResult(result=None, evidence=evidence)
        main, comments_item = _find_game_detail_item(page_props)
        if main is None:
            return DetailPageResult(result=None, evidence=evidence)
        result = _card_to_result(
            main,
            evidence,
            detail_fetched=True,
            highlight_comments=_comments_from(comments_item),
        )
        return DetailPageResult(result=result, evidence=evidence)

    def discover(
        self,
        *,
        paths: tuple[str, ...] = LIST_PATHS,
        with_detail: bool = True,
        detail_limit: int = 15,
    ) -> DiscoveryResult:
        if not paths or len(paths) > self._max_list_paths:
            raise ValueError("paths exceed max_list_paths or are empty")
        if detail_limit < 0 or detail_limit > self._max_detail_requests:
            raise ValueError("detail_limit exceeds max_detail_requests")

        by_id: dict[str, CanonicalGameResult] = {}
        evidence: list[RawPageEvidence] = []
        for path in paths:
            page = self.list_page(path)
            evidence.append(page.evidence)
            for game in page.results:
                source_id = game.source_game_id
                if source_id and source_id not in by_id:
                    by_id[source_id] = game

        detail_attempts = 0
        if with_detail:
            need_detail = [game for game in by_id.values() if not game.description][
                :detail_limit
            ]
            for game in need_detail:
                source_id = game.source_game_id
                if source_id is None:
                    continue
                detail_page = self.detail(source_id)
                evidence.append(detail_page.evidence)
                detail_attempts += 1
                if detail_page.result is not None:
                    by_id[source_id] = _merge_detail(game, detail_page.result)

        return DiscoveryResult(
            results=tuple(by_id.values()),
            evidence=tuple(evidence),
            request_count=len(evidence),
            detail_attempts=detail_attempts,
        )
