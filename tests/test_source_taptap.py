from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from geo_game_collector.contracts import ResultStatus, SourceName
from geo_game_collector.sources.taptap import (
    HttpResponse,
    RequestAttempt,
    TapTapAdapter,
    TapTapBlockedError,
    TapTapRetryExhaustedError,
)

FIXTURES = Path(__file__).parent / "fixtures" / "taptap"


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class _FakeTransport:
    def __init__(
        self,
        actions: list[HttpResponse | BaseException],
        *,
        xsrf: str | None = None,
    ) -> None:
        self.actions = list(actions)
        self.xsrf = xsrf
        self.calls: list[dict[str, object]] = []

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout": timeout,
            }
        )
        action = self.actions.pop(0)
        if isinstance(action, BaseException):
            raise action
        return action

    def cookie(self, name: str) -> str | None:
        return self.xsrf if name == "XSRF-TOKEN" else None


def _ok(fixture_name: str) -> HttpResponse:
    return HttpResponse(status=200, body=_fixture(fixture_name))


def test_category_fixture_maps_canonical_results_and_keeps_raw_evidence() -> None:
    transport = _FakeTransport([_ok("category.json")])
    attempts: list[RequestAttempt] = []
    adapter = TapTapAdapter(
        transport=transport,
        sleeper=lambda _: None,
        attempt_recorder=attempts.append,
    )

    results = adapter.search_category("模拟经营")

    assert len(results) == 2
    result = results[0]
    assert result.source is SourceName.TAPTAP
    assert result.status is ResultStatus.SUCCESS
    assert result.source_game_id == "45213"
    assert result.name == "心动小镇"
    assert result.score == 8.6
    assert result.tags == ("模拟经营",)
    assert result.platforms == ("android", "ios")
    assert result.comment_count == 1234
    assert result.android_package == "com.xd.xdt"
    assert result.description == "慢节奏生活模拟"
    assert result.raw == {
        "stage": "category",
        "response": json.loads(_fixture("category.json")),
    }
    assert attempts == [
        RequestAttempt(
            stage="category",
            attempt=1,
            max_attempts=3,
            outcome="success",
            http_status=200,
        )
    ]


def test_category_platform_filter_is_client_side() -> None:
    adapter = TapTapAdapter(
        transport=_FakeTransport([_ok("category.json")]),
        sleeper=lambda _: None,
    )

    results = adapter.search_category("模拟经营", platform="ios")

    assert [result.name for result in results] == ["心动小镇"]


def test_detail_fixture_maps_full_tags_platforms_screenshots_and_media() -> None:
    adapter = TapTapAdapter(
        transport=_FakeTransport([_ok("detail.json")]),
        sleeper=lambda _: None,
    )

    result = adapter.get_detail("168332", requested_name="原神")

    assert result.source_game_id == "168332"
    assert result.name == "原神"
    assert result.tags == ("开放世界", "二次元")
    assert result.platforms == ("android", "ios", "pc")
    assert result.screenshot_urls == (
        "https://img.example/shot-01.jpg",
        "https://img.example/shot-02.jpg",
    )
    assert [item.role for item in result.media] == [
        "icon",
        "screenshot",
        "screenshot",
    ]
    assert result.raw is not None
    assert result.raw["stage"] == "detail"


def test_name_search_bootstraps_xsrf_and_accepts_only_exact_brand() -> None:
    bootstrap = HttpResponse(status=200, body=b'{"data":{"list":[]}}')
    transport = _FakeTransport(
        [bootstrap, _ok("name_search.json")],
        xsrf="fixture-xsrf",
    )
    adapter = TapTapAdapter(transport=transport, sleeper=lambda _: None)

    result = adapter.search_by_name("原神")

    assert result.status is ResultStatus.SUCCESS
    assert result.name == "原神"
    assert result.source_game_id == "168332"
    assert result.tags == ("开放世界", "二次元")
    assert result.platforms == ("android", "ios", "pc")
    assert len(transport.calls) == 2
    assert transport.calls[0]["method"] == "GET"
    assert transport.calls[1]["method"] == "POST"
    headers = transport.calls[1]["headers"]
    assert isinstance(headers, dict)
    assert headers["X-XSRF-TOKEN"] == "fixture-xsrf"
    body = transport.calls[1]["body"]
    assert isinstance(body, bytes)
    assert b'name="kw"\r\n\r\n' + "原神".encode() in body
    assert result.raw == {
        "stage": "name_search",
        "response": json.loads(_fixture("name_search.json")),
    }


@pytest.mark.parametrize("status", [403, 405, 429])
def test_block_status_halts_immediately_without_retry(status: int) -> None:
    transport = _FakeTransport(
        [
            HttpResponse(status=status, body=b"blocked"),
            _ok("category.json"),
        ]
    )
    attempts: list[RequestAttempt] = []
    sleeps: list[float] = []
    adapter = TapTapAdapter(
        transport=transport,
        sleeper=sleeps.append,
        attempt_recorder=attempts.append,
    )

    with pytest.raises(TapTapBlockedError) as caught:
        adapter.search_category("卡牌")

    assert caught.value.status == status
    assert caught.value.stage == "category"
    assert caught.value.attempts == 1
    assert caught.value.retryable is False
    assert caught.value.stop_run is True
    assert len(transport.calls) == 1
    assert sleeps == []
    assert [attempt.outcome for attempt in attempts] == ["blocked"]


@pytest.mark.parametrize(
    ("actions", "xsrf", "expected_stage"),
    [
        (
            [HttpResponse(status=429, body=b"blocked")],
            None,
            "name_search_xsrf",
        ),
        (
            [
                HttpResponse(status=200, body=b'{"data":{"list":[]}}'),
                HttpResponse(status=403, body=b"blocked"),
            ],
            "fixture-xsrf",
            "name_search",
        ),
    ],
)
def test_name_search_block_at_any_stage_is_a_run_stop(
    actions: list[HttpResponse | BaseException],
    xsrf: str | None,
    expected_stage: str,
) -> None:
    transport = _FakeTransport(actions, xsrf=xsrf)
    adapter = TapTapAdapter(transport=transport, sleeper=lambda _: None)

    with pytest.raises(TapTapBlockedError) as caught:
        adapter.search_by_name("原神")

    assert caught.value.stage == expected_stage
    assert caught.value.stop_run is True
    assert len(transport.calls) == len(actions)


def test_timeout_retries_conservatively_and_records_every_attempt() -> None:
    transport = _FakeTransport(
        [
            TimeoutError("first"),
            TimeoutError("second"),
            _ok("category.json"),
        ]
    )
    attempts: list[RequestAttempt] = []
    sleeps: list[float] = []
    adapter = TapTapAdapter(
        transport=transport,
        sleeper=sleeps.append,
        attempt_recorder=attempts.append,
    )

    results = adapter.search_category("卡牌")

    assert len(results) == 2
    assert [attempt.outcome for attempt in attempts] == [
        "retryable",
        "retryable",
        "success",
    ]
    assert [attempt.attempt for attempt in attempts] == [1, 2, 3]
    assert sleeps == [0.5, 1.0]


def test_connection_failure_is_bounded_and_reported() -> None:
    transport = _FakeTransport(
        [
            ConnectionError("one"),
            ConnectionError("two"),
            ConnectionError("three"),
        ]
    )
    attempts: list[RequestAttempt] = []
    adapter = TapTapAdapter(
        transport=transport,
        sleeper=lambda _: None,
        attempt_recorder=attempts.append,
    )

    with pytest.raises(TapTapRetryExhaustedError) as caught:
        adapter.get_detail("168332")

    assert caught.value.attempts == 3
    assert caught.value.retryable is True
    assert len(transport.calls) == 3
    assert [attempt.outcome for attempt in attempts] == [
        "retryable",
        "retryable",
        "retry_exhausted",
    ]


def test_http_5xx_retries_but_stays_bounded() -> None:
    transport = _FakeTransport(
        [
            HttpResponse(status=503, body=b"busy"),
            _ok("detail.json"),
        ]
    )
    attempts: list[RequestAttempt] = []
    sleeps: list[float] = []
    adapter = TapTapAdapter(
        transport=transport,
        sleeper=sleeps.append,
        attempt_recorder=attempts.append,
    )

    result = adapter.get_detail("168332")

    assert result.status is ResultStatus.SUCCESS
    assert [attempt.outcome for attempt in attempts] == [
        "retryable",
        "success",
    ]
    assert [attempt.http_status for attempt in attempts] == [503, 200]
    assert sleeps == [0.5]
