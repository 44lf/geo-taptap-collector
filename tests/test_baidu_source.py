from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from geo_game_collector.contracts import ResultStatus, SourceName
from geo_game_collector.sources import baidu

FIXTURES = Path(__file__).parent / "fixtures" / "baidu"


class _FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def _fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_category_search_preserves_baidu_request_and_canonical_fields(
    monkeypatch,
) -> None:
    requests = []

    def fake_urlopen(request: object, timeout: int = 10) -> _FakeResponse:
        requests.append((request, timeout))
        return _FakeResponse(_fixture("game_filter.json"))

    monkeypatch.setattr(baidu, "urlopen", fake_urlopen)

    results = baidu.search(
        "养成", platform="android", order="new", page=2, page_size=50
    )

    assert len(results) == 1
    result = results[0]
    assert result.source is SourceName.BAIDU
    assert result.status is ResultStatus.SUCCESS
    assert result.requested_name == "餐厅养成记"
    assert result.source_game_id == "123"
    assert result.score == 8.7
    assert result.tags == ("经营", "养成")
    assert result.platforms == ("android", "ios")
    assert result.comment_count == 42
    assert result.screenshot_urls == (
        "https://example.invalid/1.jpg",
        "https://example.invalid/2.jpg",
    )
    assert result.description == "开一家餐厅"
    assert result.raw is not None and result.raw["gameId"] == 123
    assert [(media.role, media.source_url) for media in result.media] == [
        ("icon", "https://example.invalid/icon.png"),
        ("screenshot", "https://example.invalid/1.jpg"),
        ("screenshot", "https://example.invalid/2.jpg"),
    ]

    request, timeout = requests[0]
    assert timeout == 10
    query = parse_qs(urlparse(request.full_url).query)
    assert query == {
        "action": ["game_filter"],
        "typeId": ["3"],
        "tagId": [baidu.CATEGORIES["养成"]],
        "platId": ["3"],
        "order": ["2"],
        "psize": ["50"],
        "pnum": ["2"],
    }
    assert request.headers["Referer"] == "https://lewan.baidu.com/lewanhome?idfrom="
    assert request.headers["X-requested-with"] == "XMLHttpRequest"


def test_name_search_returns_exact_canonical_result_and_preserves_sparse_fields(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        baidu,
        "urlopen",
        lambda request, timeout=10: _FakeResponse(_fixture("game_query.json")),
    )

    result = baidu.search_by_name("餐厅养成记")

    assert result.status is ResultStatus.SUCCESS
    assert result.source is SourceName.BAIDU
    assert result.source_game_id == "1"
    assert result.name == "餐厅养成记"
    assert result.tags == ("手游", "策略经营")
    assert result.icon_url == "https://example.invalid/icon.png"
    assert result.score is None
    assert result.platforms == ()
    assert result.comment_count is None
    assert result.screenshot_urls == ()
    assert result.description is None


def test_name_search_rejects_prefix_match_as_miss(monkeypatch) -> None:
    monkeypatch.setattr(
        baidu,
        "urlopen",
        lambda request, timeout=10: _FakeResponse(
            {"result": {"data": [{"gameId": 9, "gameName": "餐厅养成记·豪华版"}]}}
        ),
    )

    result = baidu.search_by_name("餐厅养成记")

    assert result.status is ResultStatus.MISS
    assert result.requested_name == "餐厅养成记"


def test_name_search_honors_injected_matcher(monkeypatch) -> None:
    monkeypatch.setattr(
        baidu,
        "urlopen",
        lambda request, timeout=10: _FakeResponse(_fixture("game_query.json")),
    )

    result = baidu.search_by_name(
        "别名", matcher=lambda candidate, target: candidate == "餐厅养成记"
    )

    assert result.status is ResultStatus.SUCCESS
    assert result.name == "餐厅养成记"
