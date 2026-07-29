from pathlib import Path

import pytest

from geo_game_collector.contracts import ResultStatus, SourceName
from geo_game_collector.sources.yingyongbao import (
    BASE_URL,
    YingYongBaoAdapter,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "yingyongbao"
LIST_HTML = (FIXTURE_ROOT / "list.html").read_text(encoding="utf-8")
DETAIL_HTML = (FIXTURE_ROOT / "detail.html").read_text(encoding="utf-8")


class _FixtureClient:
    def __init__(self, responses: dict[str, tuple[int, str]]) -> None:
        self.responses = responses
        self.requested_urls: list[str] = []

    def get(self, url: str) -> tuple[int, str]:
        self.requested_urls.append(url)
        return self.responses.get(url, (404, ""))


def test_list_page_emits_canonical_results_and_preserves_raw_evidence() -> None:
    url = f"{BASE_URL}/hot-game-list"
    client = _FixtureClient({url: (200, LIST_HTML)})
    adapter = YingYongBaoAdapter(client)

    page = adapter.list_page("/hot-game-list")

    assert page.http_status == 200
    assert page.evidence.body == LIST_HTML
    assert page.evidence.stage == "list"
    assert [result.source_game_id for result in page.results] == [
        "com.example.alpha",
        "com.example.beta",
    ]
    alpha, beta = page.results
    assert alpha.source is SourceName.YINGYONGBAO
    assert alpha.status is ResultStatus.SUCCESS
    assert alpha.name == "星海远征"
    assert alpha.score == 4.8
    assert alpha.tags == ("开放世界", "角色扮演")
    assert alpha.platforms == ("android", "ios")
    assert alpha.icon_url == "https://img.example/alpha-icon.png"
    assert alpha.screenshot_urls == (
        "https://img.example/alpha-1.jpg",
        "https://img.example/alpha-2.jpg",
    )
    assert alpha.android_package == "com.example.alpha"
    assert alpha.raw is not None
    assert alpha.raw["category"] == "角色扮演"
    assert alpha.raw["detail_fetched"] is False
    assert alpha.evidence == (page.evidence.reference,)
    assert beta.score is None
    assert beta.screenshot_urls == ("https://img.example/beta-1.jpg",)


def test_detail_anchors_game_detail_and_ignores_yyb_self_context() -> None:
    url = f"{BASE_URL}/appdetail/com.example.alpha"
    client = _FixtureClient({url: (200, DETAIL_HTML)})
    adapter = YingYongBaoAdapter(client)

    page = adapter.detail("com.example.alpha")

    assert page.result is not None
    assert page.result.source_game_id == "com.example.alpha"
    assert page.result.name == "星海远征"
    assert page.result.description == "详情页完整简介"
    assert page.result.tags == ("开放世界", "角色扮演", "冒险")
    assert len(page.result.screenshot_urls) == 3
    assert page.result.raw is not None
    assert page.result.raw["detail_fetched"] is True
    assert page.result.raw["highlight_comments"] == [
        {"user": "玩家甲", "content": "很有探索感"},
        {"user": "玩家乙", "content": "战斗节奏不错"},
    ]
    assert page.evidence.body == DETAIL_HTML


def test_discovery_deduplicates_source_ids_and_bounds_detail_requests() -> None:
    list_a = f"{BASE_URL}/hot-game-list"
    list_b = f"{BASE_URL}/new-game-list"
    detail = f"{BASE_URL}/appdetail/com.example.alpha"
    client = _FixtureClient(
        {
            list_a: (200, LIST_HTML),
            list_b: (200, LIST_HTML),
            detail: (200, DETAIL_HTML),
        }
    )
    adapter = YingYongBaoAdapter(client, max_detail_requests=1)

    discovery = adapter.discover(
        paths=("/hot-game-list", "/new-game-list"),
        with_detail=True,
        detail_limit=1,
    )

    assert [result.source_game_id for result in discovery.results] == [
        "com.example.alpha",
        "com.example.beta",
    ]
    alpha = discovery.results[0]
    assert alpha.description == "详情页完整简介"
    assert len(alpha.screenshot_urls) == 3
    assert alpha.tags == ("开放世界", "角色扮演", "冒险")
    assert discovery.detail_attempts == 1
    assert discovery.request_count == 3
    assert client.requested_urls == [list_a, list_b, detail]
    assert len(discovery.evidence) == 3


def test_discovery_rejects_unbounded_detail_limit_before_requests() -> None:
    client = _FixtureClient({})
    adapter = YingYongBaoAdapter(client, max_detail_requests=1)

    with pytest.raises(ValueError, match="detail_limit"):
        adapter.discover(paths=("/hot-game-list",), detail_limit=2)

    assert client.requested_urls == []


def test_response_size_is_bounded_before_parsing() -> None:
    url = f"{BASE_URL}/hot-game-list"
    client = _FixtureClient({url: (200, LIST_HTML)})
    adapter = YingYongBaoAdapter(client, max_response_bytes=20)

    with pytest.raises(ValueError, match="max_response_bytes"):
        adapter.list_page("/hot-game-list")
