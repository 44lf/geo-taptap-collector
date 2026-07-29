from __future__ import annotations

from pathlib import Path

from geo_game_collector.contracts import ResultStatus, SourceName
from geo_game_collector.sources import ninegame

CARD = (Path(__file__).parent / "fixtures" / "ninegame" / "search_card.html").read_text(
    encoding="utf-8"
)


def test_parse_card_emits_canonical_result_and_preserves_raw_card() -> None:
    result = ninegame.parse_card(CARD, requested_name="餐厅养成记")

    assert result is not None
    assert result.source is SourceName.NINEGAME
    assert result.status is ResultStatus.SUCCESS
    assert result.source_game_id == "12345"
    assert result.name == "餐厅养成记"
    assert result.score == 8.5
    assert result.tags == ("经营游戏",)
    assert result.description == "开一家餐厅，慢慢经营做大做强。"
    assert result.icon_url == "https://media.9game.cn/icon.png"
    assert result.platforms == ("android",)
    assert result.screenshot_urls == ()
    assert result.raw is not None and result.raw["gameid"] == "12345"
    assert "sr-poker" in result.raw["card_html"]
    assert [(media.role, media.source_url) for media in result.media] == [
        ("icon", "https://media.9game.cn/icon.png"),
    ]


def test_parse_card_strips_highlight_and_only_first_card_is_used() -> None:
    page = (
        '<div class="sr-poker" data-gameid="1"><div class="title"><a href="#">王者'
        '<span class="high-light-f60">荣耀</span></a></div></div>'
        '<div class="sr-poker" data-gameid="2"><div class="title">'
        '<a href="#">第二个</a></div></div>'
    )

    block = ninegame.first_card(page)
    result = ninegame.parse_card(block, requested_name="王者荣耀") if block else None

    assert result is not None
    assert result.name == "王者荣耀"
    assert result.source_game_id == "1"


def test_parse_card_returns_none_without_name_and_guards_score() -> None:
    assert (
        ninegame.parse_card(
            '<div class="sr-poker" data-gameid="7"></div>', requested_name="x"
        )
        is None
    )
    assert ninegame.parse_score(None) is None
    assert ninegame.parse_score("0") is None
    assert ninegame.parse_score("abc") is None
    assert ninegame.parse_score("8.5") == 8.5


def test_search_by_name_uses_strict_matcher_to_reject_unrelated_fallback(
    monkeypatch,
) -> None:
    unrelated = (
        '<div class="sr-poker" data-gameid="999"><div class="title">'
        '<a href="#">妈妈把我的游戏藏起来了3</a></div></div>'
    )
    monkeypatch.setattr(ninegame, "fetch", lambda url, timeout=15: unrelated)

    result = ninegame.search_by_name("不存在的游戏xyz123")

    assert result.status is ResultStatus.MISS
    assert result.source is SourceName.NINEGAME


def test_search_by_name_returns_canonical_result_and_supports_normalized_matcher(
    monkeypatch,
) -> None:
    captured_urls: list[str] = []

    def fake_fetch(url: str, timeout: int = 15) -> str:
        captured_urls.append(url)
        return CARD

    monkeypatch.setattr(ninegame, "fetch", fake_fetch)

    result = ninegame.search_by_name(
        "餐厅养成记（豪华版）",
        matcher=lambda candidate, target: candidate == "餐厅养成记",
    )

    assert result.status is ResultStatus.SUCCESS
    assert result.name == "餐厅养成记"
    assert captured_urls == [
        "https://www.9game.cn/search/?keyword=%E9%A4%90%E5%8E%85%E5%85%BB%E6%88%90%E8%AE%B0%EF%BC%88%E8%B1%AA%E5%8D%8E%E7%89%88%EF%BC%89"
    ]


def test_search_by_name_empty_page_is_miss(monkeypatch) -> None:
    monkeypatch.setattr(ninegame, "fetch", lambda url, timeout=15: None)

    assert ninegame.search_by_name("原神").status is ResultStatus.MISS
