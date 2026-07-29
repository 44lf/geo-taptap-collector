"""NineGame search-page-only adapter migrated from GEO's proven parser.

The captcha-blocked detail route is intentionally not implemented.
NineGame returns unrelated fallback cards for unknown terms.
Every search result must pass the caller matcher.
"""

from __future__ import annotations

import gzip
import html
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen

from geo_game_collector.contracts import (
    CanonicalGameResult,
    MediaReference,
    ResultStatus,
    SourceName,
)

_BASE = "https://www.9game.cn"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
}

Matcher = Callable[[str | None, str], bool]


def fetch(url: str, timeout: int = 15) -> str | None:
    """Fetch the approved search page, preserving GEO's gzip handling and timeout."""
    request = Request(url, headers=_HEADERS)
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed approved endpoint
        if response.status != 200:
            return None
        raw: bytes = response.read()
        if "gzip" in (response.headers.get("Content-Encoding") or ""):
            raw = gzip.decompress(raw)
        return raw.decode("utf-8", "replace")


def search_by_name(
    name: str,
    *,
    matcher: Matcher | None = None,
) -> CanonicalGameResult:
    """Search only the first result card and return success or explicit miss."""
    text = fetch(f"{_BASE}/search/?keyword={quote(name)}")
    block = first_card(text) if text else None
    result = parse_card(block, requested_name=name) if block else None
    if result is None or not (matcher or _exact_match)(result.name, name):
        return CanonicalGameResult(
            source=SourceName.NINEGAME,
            requested_name=name,
            status=ResultStatus.MISS,
        )
    return result


def first_card(text: str) -> str | None:
    """Extract the first `sr-poker` block using the known bounded-card parser."""
    start = text.find('class="sr-poker"')
    if start < 0:
        return None
    end = text.find('class="sr-poker"', start + 5)
    return text[start : end if end > 0 else start + 4000]


def parse_card(block: str, *, requested_name: str) -> CanonicalGameResult | None:
    game_id = _one(block, r'data-gameid="(\d+)"')
    title = _one(block, r'<div class="title">\s*<a[^>]*>(.*?)</a>')
    name = (
        strip_tags(title)
        if title
        else (_one(block, r'<img[^>]+alt="([^"]*)"') or "").strip()
    )
    if not name:
        return None
    category = strip_tags(_one(block, r'<p class="des">(.*?)</p>'))
    description = strip_tags(_one(block, r'<p class="text">(.*?)</p>')) or None
    score = parse_score(_one(block, r'<span class="oran">\s*([\d.]+)\s*</span>'))
    icon_url = _one(block, r'<img\s+src="([^"]+)"')
    platforms: list[str] = []
    if re.search(r'class="down[^"]*\bandroid\b', block):
        platforms.append("android")
    if re.search(r'class="down[^"]*\bios\b', block):
        platforms.append("ios")
    raw: dict[str, Any] = {
        "gameid": game_id,
        "category": category,
        "card_html": block,
    }
    media = (MediaReference(role="icon", source_url=icon_url),) if icon_url else ()
    return CanonicalGameResult(
        source=SourceName.NINEGAME,
        requested_name=requested_name,
        status=ResultStatus.SUCCESS,
        source_game_id=game_id or name,
        name=name,
        score=score,
        description=description,
        tags=(category,) if category else (),
        platforms=tuple(platforms),
        icon_url=icon_url,
        raw=raw,
        media=media,
    )


def parse_score(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        score = float(value)
    except ValueError:
        return None
    return score if score > 0 else None


def strip_tags(value: str | None) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", value or "")).strip()


def _one(block: str, pattern: str) -> str | None:
    matched = re.search(pattern, block, re.S)
    return matched.group(1) if matched else None


def _exact_match(candidate: str | None, target: str) -> bool:
    return candidate == target
