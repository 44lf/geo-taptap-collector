"""Baidu Lewan direct-HTTP adapter migrated from GEO's proven source behavior.

This module only performs source acquisition and structural canonicalization.
It does not normalize identities, select GEO targets, or persist business data.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from geo_game_collector.contracts import (
    CanonicalGameResult,
    MediaReference,
    ResultStatus,
    SourceName,
)

_API = "https://lewan.baidu.com/lewanapi"
_TYPE_ID = "3"
_HEADERS = {
    "Referer": "https://lewan.baidu.com/lewanhome?idfrom=",
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": "Mozilla/5.0",
}

# typeId=3 (mobile games) category name -> tag ID, from the proven GEO source.
CATEGORIES = {
    "全部类型": "0",
    "策略": "16290",
    "休闲": "16292",
    "策略经营": "56632",
    "其它": "56636",
    "模拟": "70472",
    "经营": "70490",
    "卡通": "88187",
    "动作": "16328",
    "冒险": "16201",
    "RPG": "88083",
    "角色扮演": "5914",
    "都市": "71516",
    "益智": "16204",
    "养成": "33889",
    "射击": "16323",
    "战争": "33272",
    "回合制": "70475",
    "战略": "136251",
    "枪械": "136249",
    "魔幻": "5920",
    "像素": "33222",
    "二次元": "65701",
    "MMORPG": "136250",
    "卡牌": "70481",
    "脑力": "136270",
    "日系": "104836",
    "竞速": "94968",
    "消除": "136267",
    "跑酷": "33867",
    "赛车": "33202",
    "观察": "94969",
    "解谜": "33226",
    "放置": "136253",
    "三国": "5923",
    "玄幻": "74476",
    "棋牌": "16334",
    "生存": "70473",
    "格斗": "33179",
    "怀旧": "94974",
}

_ORDER_MAP = {"hot": 3, "new": 2}
_REQUEST_PLATFORM_TO_PLATID = {None: 0, "android": 3, "ios": 4}
_RESULT_PLATFORM_NAME_MAP = {"Android": "android", "IOS": "ios"}

Matcher = Callable[[str | None, str], bool]


def search(
    category: str,
    *,
    platform: str | None = None,
    order: str = "hot",
    page: int = 1,
    page_size: int = 20,
) -> tuple[CanonicalGameResult, ...]:
    """Collect one Baidu category page using the directly verified API."""
    params: dict[str, str | int] = {
        "action": "game_filter",
        "typeId": _TYPE_ID,
        "tagId": CATEGORIES.get(category, category),
        "platId": _REQUEST_PLATFORM_TO_PLATID.get(platform, 0),
        "order": _ORDER_MAP.get(order, _ORDER_MAP["hot"]),
        "psize": page_size,
        "pnum": page,
    }
    payload = _get_json(params)
    game_list = ((payload.get("result") or {}).get("data") or {}).get("gameList") or []
    return tuple(_from_filter(item) for item in game_list if isinstance(item, dict))


def search_by_name(
    name: str,
    *,
    matcher: Matcher | None = None,
) -> CanonicalGameResult:
    """Search by name and return only an exact (or caller-normalized) match.

    A miss is explicit in the canonical result so runtime code can distinguish it from a
    transient source exception without importing GEO business behavior.
    """
    match = matcher or _exact_match
    payload = _get_json({"action": "game_query", "gameName": name})
    items = ((payload.get("result") or {}).get("data")) or []
    for item in items:
        if isinstance(item, dict) and match(
            _as_optional_text(item.get("gameName")), name
        ):
            return _from_query(item, requested_name=name)
    return CanonicalGameResult(
        source=SourceName.BAIDU,
        requested_name=name,
        status=ResultStatus.MISS,
    )


def _get_json(params: dict[str, str | int]) -> dict[str, Any]:
    request = Request(f"{_API}?{urlencode(params)}", headers=_HEADERS)
    with urlopen(request, timeout=10) as response:  # noqa: S310 - fixed approved endpoint
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Baidu response must be a JSON object")
    return payload


def _exact_match(candidate: str | None, target: str) -> bool:
    return candidate == target


def _from_query(item: dict[str, Any], *, requested_name: str) -> CanonicalGameResult:
    return _result(
        item,
        requested_name=requested_name,
        tags=_strings(item.get("gameTypes")),
        platforms=(),
        screenshots=(),
        score=None,
        comment_count=None,
        description=None,
    )


def _from_filter(item: dict[str, Any]) -> CanonicalGameResult:
    name = _required_text(item.get("gameName"), "Baidu filter gameName")
    score = _score(item.get("gameScore"))
    comment_count = _non_negative_int(item.get("commentCount"))
    return _result(
        item,
        requested_name=name,
        tags=_strings(item.get("gameTags")),
        platforms=tuple(
            _RESULT_PLATFORM_NAME_MAP.get(platform, platform)
            for platform in _strings(item.get("gamePlatform"))
        ),
        screenshots=_strings(item.get("gameOfficialPic")),
        score=score,
        comment_count=comment_count,
        description=_as_optional_text(item.get("gameDesc")),
    )


def _result(
    item: dict[str, Any],
    *,
    requested_name: str,
    tags: tuple[str, ...],
    platforms: tuple[str, ...],
    screenshots: tuple[str, ...],
    score: float | None,
    comment_count: int | None,
    description: str | None,
) -> CanonicalGameResult:
    source_game_id = _required_identifier(item.get("gameId"), "Baidu gameId")
    name = _required_text(item.get("gameName"), "Baidu gameName")
    icon_url = _as_optional_text(item.get("gameIcon"))
    media = _media(icon_url, screenshots)
    return CanonicalGameResult(
        source=SourceName.BAIDU,
        requested_name=requested_name,
        status=ResultStatus.SUCCESS,
        source_game_id=source_game_id,
        name=name,
        score=score,
        description=description,
        tags=tags,
        platforms=platforms,
        comment_count=comment_count,
        icon_url=icon_url,
        screenshot_urls=screenshots,
        raw=item,
        media=media,
    )


def _media(
    icon_url: str | None, screenshots: tuple[str, ...]
) -> tuple[MediaReference, ...]:
    references: list[MediaReference] = []
    if icon_url:
        references.append(MediaReference(role="icon", source_url=icon_url))
    references.extend(
        MediaReference(role="screenshot", source_url=url) for url in screenshots
    )
    return tuple(references)


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def _as_optional_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _required_text(value: object, field_name: str) -> str:
    text = _as_optional_text(value)
    if text is None:
        raise ValueError(f"{field_name} is required")
    return text


def _required_identifier(value: object, field_name: str) -> str:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field_name} is required")
    text = str(value).strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


def _score(value: object) -> float | None:
    text = _as_optional_text(value)
    if text is None:
        return None
    try:
        return float(text)
    except ValueError as exc:
        raise ValueError("Baidu gameScore must be numeric") from exc


def _non_negative_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("Baidu commentCount must be an integer")
    try:
        integer = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("Baidu commentCount must be an integer") from exc
    if integer < 0:
        raise ValueError("Baidu commentCount must be non-negative")
    return integer
