from __future__ import annotations

import html
import http.cookiejar
import json
import logging
import re
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import (
    HTTPCookieProcessor,
    Request,
    build_opener,
)

from geo_game_collector.contracts import (
    CanonicalGameResult,
    MediaReference,
    ResultStatus,
    SourceName,
)

logger = logging.getLogger(__name__)

CATEGORY_API = "https://www.taptap.cn/webapiv2/app-tag/v1/by-tag"
DETAIL_API = "https://www.taptap.cn/webapiv2/app/v6/detail"
NAME_SEARCH_API = "https://www.taptap.cn/webapiv2/search/v6/agg-search"

BLOCKED_HTTP_STATUSES = frozenset({403, 405, 429})
MAX_PAGE_SIZE = 20

ORDER_HOT = "hot"
ORDER_NEW = "new"
_ORDER_MAP = {ORDER_HOT: "hits", ORDER_NEW: "updated"}

PLATFORM_ANDROID = "android"
PLATFORM_IOS = "ios"
PLATFORM_PC = "pc"
_PLATFORM_KEY_MAP = {
    "android": PLATFORM_ANDROID,
    "ios": PLATFORM_IOS,
    "pc": PLATFORM_PC,
}

CATEGORIES = {
    name: name
    for name in (
        "卡牌",
        "射击",
        "二次元",
        "Roguelike",
        "解谜",
        "文字",
        "音游",
        "女性向",
        "养成",
        "沙盒",
        "开放世界",
        "MMORPG",
        "武侠",
        "国风",
        "竞速",
        "益智",
        "Steam移植",
        "UP主推荐",
        "生存",
        "MOBA",
        "放置",
        "塔防",
        "像素",
        "治愈",
        "末日",
        "格斗",
        "魔性",
    )
}

_HEADERS = {
    "Referer": "https://www.taptap.cn/categories/all",
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": "Mozilla/5.0",
    "Accept": "*/*",
}
_HTML_TAG = re.compile(r"<[^>]+>")


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: bytes


class TapTapTransport(Protocol):
    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse: ...

    def cookie(self, name: str) -> str | None: ...


class UrllibTapTapTransport:
    """Cookie-preserving stdlib transport; source policy stays in the adapter."""

    def __init__(self) -> None:
        self._cookies = http.cookiejar.CookieJar()
        self._opener = build_opener(HTTPCookieProcessor(self._cookies))

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse:
        request = Request(
            url,
            data=body,
            headers=dict(headers),
            method=method,
        )
        try:
            with self._opener.open(request, timeout=timeout) as response:
                return HttpResponse(
                    status=int(response.status),
                    body=response.read(),
                )
        except HTTPError as exc:
            return HttpResponse(status=int(exc.code), body=exc.read())

    def cookie(self, name: str) -> str | None:
        return next(
            (cookie.value for cookie in self._cookies if cookie.name == name),
            None,
        )


@dataclass(frozen=True, slots=True)
class RequestAttempt:
    stage: str
    attempt: int
    max_attempts: int
    outcome: str
    http_status: int | None = None
    error_type: str | None = None


AttemptRecorder = Callable[[RequestAttempt], None]
Sleeper = Callable[[float], None]


class TapTapRequestError(RuntimeError):
    retryable = False
    stop_run = False

    def __init__(
        self,
        message: str,
        *,
        stage: str,
        attempts: int,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.attempts = attempts
        self.status = status


class TapTapBlockedError(TapTapRequestError):
    """A policy stop: callers must halt the remaining TapTap run."""

    retryable = False
    stop_run = True


class TapTapRetryExhaustedError(TapTapRequestError):
    retryable = True


class TapTapResponseError(TapTapRequestError):
    pass


def _default_attempt_recorder(event: RequestAttempt) -> None:
    level = (
        logging.WARNING
        if event.outcome in {"blocked", "retryable", "retry_exhausted"}
        else logging.DEBUG
    )
    logger.log(
        level,
        (
            "taptap request attempt "
            "stage=%s attempt=%s/%s outcome=%s http_status=%s error_type=%s"
        ),
        event.stage,
        event.attempt,
        event.max_attempts,
        event.outcome,
        event.http_status,
        event.error_type,
    )


def _x_ua() -> str:
    return (
        "V=1&PN=WebApp&LANG=zh_CN&VN_CODE=102&LOC=CN&PLT=PC&DS=Android"
        f"&UID={uuid.uuid4()}&OS=Windows&OSV=10&DT=PC"
    )


def _clean_description(description: Any) -> str | None:
    if not isinstance(description, dict):
        return None
    text = description.get("text")
    if not isinstance(text, str) or not text:
        return None
    cleaned = html.unescape(_HTML_TAG.sub("", text)).strip()
    return cleaned or None


def _unique_text(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _score(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _count(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _icon_url(app: dict[str, Any]) -> str | None:
    icon = app.get("icon")
    if not isinstance(icon, dict):
        return None
    value = icon.get("original_url") or icon.get("url")
    return value if isinstance(value, str) and value else None


def _media(
    icon_url: str | None,
    screenshot_urls: tuple[str, ...],
) -> tuple[MediaReference, ...]:
    refs: list[MediaReference] = []
    if icon_url:
        refs.append(MediaReference(role="icon", source_url=icon_url))
    refs.extend(
        MediaReference(role="screenshot", source_url=url) for url in screenshot_urls
    )
    return tuple(refs)


def _multipart_encode(fields: Mapping[str, str]) -> tuple[str, bytes]:
    boundary = f"----WebKitFormBoundary{uuid.uuid4().hex}"
    parts = [
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
            f"\r\n\r\n{value}\r\n"
        )
        for name, value in fields.items()
    ]
    parts.append(f"--{boundary}--\r\n")
    return boundary, "".join(parts).encode("utf-8")


class TapTapAdapter:
    """Authorized TapTap category/detail/name-search adapter.

    HTTP 403/405/429 is a non-retryable policy stop at every request stage.
    Only connection failures, timeouts, and HTTP 5xx receive bounded retries.
    """

    def __init__(
        self,
        *,
        transport: TapTapTransport | None = None,
        max_attempts: int = 3,
        timeout_seconds: float = 10.0,
        backoff_seconds: float = 0.5,
        sleeper: Sleeper = time.sleep,
        attempt_recorder: AttemptRecorder = _default_attempt_recorder,
    ) -> None:
        if not 1 <= max_attempts <= 5:
            raise ValueError("max_attempts must be between 1 and 5")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if backoff_seconds < 0:
            raise ValueError("backoff_seconds must be non-negative")
        self._transport = transport or UrllibTapTapTransport()
        self._max_attempts = max_attempts
        self._timeout_seconds = timeout_seconds
        self._backoff_seconds = backoff_seconds
        self._sleeper = sleeper
        self._attempt_recorder = attempt_recorder

    def _record(
        self,
        *,
        stage: str,
        attempt: int,
        outcome: str,
        status: int | None = None,
        error: BaseException | None = None,
    ) -> None:
        self._attempt_recorder(
            RequestAttempt(
                stage=stage,
                attempt=attempt,
                max_attempts=self._max_attempts,
                outcome=outcome,
                http_status=status,
                error_type=type(error).__name__ if error is not None else None,
            )
        )

    def _backoff(self, attempt: int) -> None:
        self._sleeper(self._backoff_seconds * attempt)

    def _request_json(
        self,
        *,
        stage: str,
        method: str,
        url: str,
        headers: Mapping[str, str] = _HEADERS,
        body: bytes | None = None,
    ) -> dict[str, Any]:
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._transport.request(
                    method=method,
                    url=url,
                    headers=headers,
                    body=body,
                    timeout=self._timeout_seconds,
                )
            except (TimeoutError, ConnectionError, URLError, OSError) as exc:
                if attempt == self._max_attempts:
                    self._record(
                        stage=stage,
                        attempt=attempt,
                        outcome="retry_exhausted",
                        error=exc,
                    )
                    raise TapTapRetryExhaustedError(
                        f"TapTap {stage} network retries exhausted",
                        stage=stage,
                        attempts=attempt,
                    ) from exc
                self._record(
                    stage=stage,
                    attempt=attempt,
                    outcome="retryable",
                    error=exc,
                )
                self._backoff(attempt)
                continue

            status = response.status
            if status in BLOCKED_HTTP_STATUSES:
                self._record(
                    stage=stage,
                    attempt=attempt,
                    outcome="blocked",
                    status=status,
                )
                raise TapTapBlockedError(
                    f"TapTap {stage} blocked with HTTP {status}",
                    stage=stage,
                    attempts=attempt,
                    status=status,
                )
            if 500 <= status <= 599:
                if attempt == self._max_attempts:
                    self._record(
                        stage=stage,
                        attempt=attempt,
                        outcome="retry_exhausted",
                        status=status,
                    )
                    raise TapTapRetryExhaustedError(
                        f"TapTap {stage} HTTP {status} retries exhausted",
                        stage=stage,
                        attempts=attempt,
                        status=status,
                    )
                self._record(
                    stage=stage,
                    attempt=attempt,
                    outcome="retryable",
                    status=status,
                )
                self._backoff(attempt)
                continue
            if not 200 <= status <= 299:
                self._record(
                    stage=stage,
                    attempt=attempt,
                    outcome="permanent",
                    status=status,
                )
                raise TapTapRequestError(
                    f"TapTap {stage} returned HTTP {status}",
                    stage=stage,
                    attempts=attempt,
                    status=status,
                )

            try:
                decoded = json.loads(response.body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                self._record(
                    stage=stage,
                    attempt=attempt,
                    outcome="invalid_json",
                    status=status,
                    error=exc,
                )
                raise TapTapResponseError(
                    f"TapTap {stage} returned invalid JSON",
                    stage=stage,
                    attempts=attempt,
                    status=status,
                ) from exc
            if not isinstance(decoded, dict):
                self._record(
                    stage=stage,
                    attempt=attempt,
                    outcome="invalid_json",
                    status=status,
                )
                raise TapTapResponseError(
                    f"TapTap {stage} JSON root must be an object",
                    stage=stage,
                    attempts=attempt,
                    status=status,
                )
            self._record(
                stage=stage,
                attempt=attempt,
                outcome="success",
                status=status,
            )
            return decoded
        raise AssertionError("bounded request loop exited unexpectedly")

    def search_category(
        self,
        category: str,
        *,
        platform: str | None = None,
        order: str = ORDER_HOT,
        page: int = 1,
        page_size: int = MAX_PAGE_SIZE,
    ) -> tuple[CanonicalGameResult, ...]:
        if not category or category != category.strip():
            raise ValueError("category must be non-empty trimmed text")
        if not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ValueError("TapTap page_size must be between 1 and 20")
        if page <= 0:
            raise ValueError("page must be positive")
        if platform not in {None, PLATFORM_ANDROID, PLATFORM_IOS, PLATFORM_PC}:
            raise ValueError(f"unsupported platform: {platform}")

        tag = CATEGORIES.get(category, category)
        params = {
            "X-UA": _x_ua(),
            "tag": tag,
            "sort": _ORDER_MAP.get(order, "hits"),
            "from": (page - 1) * page_size,
            "limit": page_size,
        }
        payload = self._request_json(
            stage="category",
            method="GET",
            url=f"{CATEGORY_API}?{urlencode(params)}",
        )
        data = payload.get("data")
        raw_items = data.get("list") if isinstance(data, dict) else None
        items = raw_items if isinstance(raw_items, list) else []
        results = tuple(
            self._from_category_item(
                item,
                requested_name=category,
                tag=tag,
                raw_response=payload,
            )
            for item in items
            if isinstance(item, dict)
        )
        if platform is None:
            return results
        return tuple(result for result in results if platform in result.platforms)

    def get_detail(
        self,
        source_game_id: str,
        *,
        requested_name: str | None = None,
    ) -> CanonicalGameResult:
        if not source_game_id or source_game_id != source_game_id.strip():
            raise ValueError("source_game_id must be non-empty trimmed text")
        params = {"X-UA": _x_ua(), "id": source_game_id}
        payload = self._request_json(
            stage="detail",
            method="GET",
            url=f"{DETAIL_API}?{urlencode(params)}",
        )
        data = payload.get("data")
        app = data.get("app") if isinstance(data, dict) else None
        if not isinstance(app, dict) or not app:
            return CanonicalGameResult(
                source=SourceName.TAPTAP,
                requested_name=requested_name or source_game_id,
                status=ResultStatus.MISS,
                raw={"stage": "detail", "response": payload},
            )
        return self._from_detail_app(
            app,
            requested_name=requested_name or source_game_id,
            raw_response=payload,
        )

    def search_by_name(self, name: str) -> CanonicalGameResult:
        if not name or name != name.strip():
            raise ValueError("name must be non-empty trimmed text")
        bootstrap_params = {
            "X-UA": _x_ua(),
            "tag": "卡牌",
            "sort": "hits",
            "from": 0,
            "limit": 1,
        }
        self._request_json(
            stage="name_search_xsrf",
            method="GET",
            url=f"{CATEGORY_API}?{urlencode(bootstrap_params)}",
        )
        xsrf = self._transport.cookie("XSRF-TOKEN")
        if not xsrf:
            raise TapTapResponseError(
                "TapTap name search did not receive XSRF-TOKEN",
                stage="name_search_xsrf",
                attempts=1,
            )

        boundary, body = _multipart_encode({"kw": name, "types": "mix"})
        headers = {
            **_HEADERS,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Referer": f"https://www.taptap.cn/search/{quote(name)}",
            "Origin": "https://www.taptap.cn",
            "X-XSRF-TOKEN": xsrf,
        }
        payload = self._request_json(
            stage="name_search",
            method="POST",
            url=f"{NAME_SEARCH_API}?{urlencode({'X-UA': _x_ua()})}",
            headers=headers,
            body=body,
        )
        for group in _groups(payload):
            raw_items = group.get("list")
            items = raw_items if isinstance(raw_items, list) else []
            for item in items:
                if not isinstance(item, dict) or item.get("type") != "brand":
                    continue
                brand = item.get("brand")
                app = brand.get("app") if isinstance(brand, dict) else None
                if isinstance(app, dict) and app.get("title") == name:
                    return self._from_name_app(
                        app,
                        requested_name=name,
                        raw_response=payload,
                    )
        return CanonicalGameResult(
            source=SourceName.TAPTAP,
            requested_name=name,
            status=ResultStatus.MISS,
            raw={"stage": "name_search", "response": payload},
        )

    @staticmethod
    def _from_category_item(
        app: dict[str, Any],
        *,
        requested_name: str,
        tag: str,
        raw_response: dict[str, Any],
    ) -> CanonicalGameResult:
        platforms = []
        if app.get("identifier"):
            platforms.append(PLATFORM_ANDROID)
        if app.get("itunes_id"):
            platforms.append(PLATFORM_IOS)
        stat = app.get("stat")
        stat_data = stat if isinstance(stat, dict) else {}
        rating = stat_data.get("rating")
        rating_data = rating if isinstance(rating, dict) else {}
        icon_url = _icon_url(app)
        return CanonicalGameResult(
            source=SourceName.TAPTAP,
            requested_name=requested_name,
            status=ResultStatus.SUCCESS,
            source_game_id=str(app.get("id")),
            name=str(app.get("title")),
            score=_score(rating_data.get("score")),
            description=_clean_description(app.get("description")),
            tags=(tag,),
            platforms=_unique_text(platforms),
            comment_count=_count(stat_data.get("review_count")),
            icon_url=icon_url,
            screenshot_urls=(),
            android_package=(str(app["identifier"]) if app.get("identifier") else None),
            raw={"stage": "category", "response": raw_response},
            media=_media(icon_url, ()),
        )

    @staticmethod
    def _from_detail_app(
        app: dict[str, Any],
        *,
        requested_name: str,
        raw_response: dict[str, Any],
    ) -> CanonicalGameResult:
        raw_tags = app.get("tags")
        tag_items = raw_tags if isinstance(raw_tags, list) else []
        tags = _unique_text(
            [
                str(tag["value"])
                for tag in tag_items
                if isinstance(tag, dict) and tag.get("value")
            ]
        )
        raw_screenshots = app.get("screenshots")
        screenshot_items = raw_screenshots if isinstance(raw_screenshots, list) else []
        screenshots = _unique_text(
            [
                str(shot.get("original_url") or shot.get("url"))
                for shot in screenshot_items
                if isinstance(shot, dict)
                and (shot.get("original_url") or shot.get("url"))
            ]
        )
        platform_info = app.get("platform_info")
        platform_data = platform_info if isinstance(platform_info, dict) else {}
        raw_supported = platform_data.get("supported_platforms")
        supported = raw_supported if isinstance(raw_supported, list) else []
        platforms = _unique_text(
            [
                _PLATFORM_KEY_MAP.get(str(item["key"]), str(item["key"]))
                for item in supported
                if isinstance(item, dict) and item.get("key")
            ]
        )
        stat = app.get("stat")
        stat_data = stat if isinstance(stat, dict) else {}
        rating = stat_data.get("rating")
        rating_data = rating if isinstance(rating, dict) else {}
        icon_url = _icon_url(app)
        return CanonicalGameResult(
            source=SourceName.TAPTAP,
            requested_name=requested_name,
            status=ResultStatus.SUCCESS,
            source_game_id=str(app.get("id")),
            name=str(app.get("title")),
            score=_score(rating_data.get("score")),
            description=_clean_description(app.get("description")),
            tags=tags,
            platforms=platforms,
            comment_count=_count(stat_data.get("review_count")),
            icon_url=icon_url,
            screenshot_urls=screenshots,
            android_package=(str(app["identifier"]) if app.get("identifier") else None),
            raw={"stage": "detail", "response": raw_response},
            media=_media(icon_url, screenshots),
        )

    @staticmethod
    def _from_name_app(
        app: dict[str, Any],
        *,
        requested_name: str,
        raw_response: dict[str, Any],
    ) -> CanonicalGameResult:
        raw_tags = app.get("tags")
        tag_items = raw_tags if isinstance(raw_tags, list) else []
        tags = _unique_text(
            [
                str(tag["value"])
                for tag in tag_items
                if isinstance(tag, dict) and tag.get("value")
            ]
        )
        raw_supported = app.get("supported_platforms")
        supported = raw_supported if isinstance(raw_supported, list) else []
        platforms = _unique_text(
            [
                _PLATFORM_KEY_MAP.get(str(item["key"]), str(item["key"]))
                for item in supported
                if isinstance(item, dict) and item.get("key")
            ]
        )
        icon_url = _icon_url(app)
        return CanonicalGameResult(
            source=SourceName.TAPTAP,
            requested_name=requested_name,
            status=ResultStatus.SUCCESS,
            source_game_id=str(app.get("id")),
            name=str(app.get("title")),
            score=_score(app.get("score")),
            description=_clean_description(app.get("description")),
            tags=tags,
            platforms=platforms,
            comment_count=None,
            icon_url=icon_url,
            screenshot_urls=(),
            android_package=(str(app["identifier"]) if app.get("identifier") else None),
            raw={"stage": "name_search", "response": raw_response},
            media=_media(icon_url, ()),
        )


def _groups(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data")
    raw_groups = data.get("list") if isinstance(data, dict) else None
    groups = raw_groups if isinstance(raw_groups, list) else []
    return [group for group in groups if isinstance(group, dict)]
