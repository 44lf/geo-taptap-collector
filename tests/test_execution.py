from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from geo_game_collector.contracts import (
    CanonicalGameResult,
    MediaReference,
    ResultStatus,
    SourceName,
)
from geo_game_collector.execution import (
    JobExecutionError,
    JobExecutor,
    executed_item_from_payload,
    executed_item_to_payload,
)
from geo_game_collector.sources.taptap import TapTapBlockedError


def _success(source: SourceName, name: str, identity: str) -> CanonicalGameResult:
    return CanonicalGameResult(
        source=source,
        requested_name=name,
        status=ResultStatus.SUCCESS,
        source_game_id=identity,
        name=name,
        raw={"id": identity},
    )


@dataclass
class _Runtime:
    refresh_results: dict[SourceName, object] = field(default_factory=dict)
    discovery_results: dict[SourceName, object] = field(default_factory=dict)
    calls: list[tuple[str, SourceName, str]] = field(default_factory=list)

    def refresh(self, source: SourceName, *, name: str) -> CanonicalGameResult:
        self.calls.append(("refresh", source, name))
        value = self.refresh_results[source]
        if isinstance(value, BaseException):
            raise value
        assert isinstance(value, CanonicalGameResult)
        return value

    def discover(self, source: SourceName, *, path: str, limit: int):
        self.calls.append(("discovery", source, path))
        value = self.discovery_results[source]
        if isinstance(value, BaseException):
            raise value
        return value


def _refresh_manifest() -> dict:
    return {
        "mode": "refresh",
        "source_order": ["baidu", "ninegame", "taptap"],
        "targets": [{"target_game_id": 7, "name": "Fixture", "category_id": 3}],
        "discovery_paths": [],
        "limits": {"max_requests": 10},
    }


def test_refresh_executes_all_sources_with_stable_per_source_item_keys():
    runtime = _Runtime(
        refresh_results={
            SourceName.BAIDU: _success(SourceName.BAIDU, "Fixture", "b-1"),
            SourceName.NINEGAME: CanonicalGameResult(
                source=SourceName.NINEGAME,
                requested_name="Fixture",
                status=ResultStatus.MISS,
            ),
            SourceName.TAPTAP: _success(SourceName.TAPTAP, "Fixture", "t-1"),
        }
    )

    summary = JobExecutor(runtime).execute(_refresh_manifest())

    assert [item.item_key for item in summary.items] == [
        "target-7-baidu",
        "target-7-ninegame",
        "target-7-taptap",
    ]
    assert all(item.target_game_id == 7 for item in summary.items)
    assert summary.request_count == 3
    assert summary.halted_by_block is False


def test_resume_skips_checkpointed_source_item_without_network_access():
    runtime = _Runtime(
        refresh_results={
            SourceName.NINEGAME: CanonicalGameResult(
                source=SourceName.NINEGAME,
                requested_name="Fixture",
                status=ResultStatus.MISS,
            ),
            SourceName.TAPTAP: _success(SourceName.TAPTAP, "Fixture", "t-1"),
        }
    )

    summary = JobExecutor(runtime).execute(
        _refresh_manifest(),
        completed_item_keys=("target-7-baidu",),
    )

    assert all(call[1] is not SourceName.BAIDU for call in runtime.calls)
    assert summary.completed_item_keys == (
        "target-7-baidu",
        "target-7-ninegame",
        "target-7-taptap",
    )


def test_403_405_429_stops_the_entire_remaining_run():
    runtime = _Runtime(
        refresh_results={
            SourceName.BAIDU: _success(SourceName.BAIDU, "Fixture", "b-1"),
            SourceName.NINEGAME: TapTapBlockedError(
                "blocked",
                stage="search",
                attempts=1,
                status=429,
            ),
            SourceName.TAPTAP: _success(SourceName.TAPTAP, "Fixture", "t-1"),
        }
    )

    summary = JobExecutor(runtime).execute(_refresh_manifest())

    assert summary.halted_by_block is True
    assert [item.result.status for item in summary.items] == [
        ResultStatus.SUCCESS,
        ResultStatus.BLOCKED,
    ]
    assert all(call[1] is not SourceName.TAPTAP for call in runtime.calls)


def test_transient_source_error_is_preserved_and_does_not_fake_a_miss():
    runtime = _Runtime(
        refresh_results={
            SourceName.BAIDU: TimeoutError("secret response body"),
            SourceName.NINEGAME: CanonicalGameResult(
                source=SourceName.NINEGAME,
                requested_name="Fixture",
                status=ResultStatus.MISS,
            ),
            SourceName.TAPTAP: _success(SourceName.TAPTAP, "Fixture", "t-1"),
        }
    )

    summary = JobExecutor(runtime).execute(_refresh_manifest())

    assert summary.items[0].result.status is ResultStatus.ERROR
    assert summary.items[0].result.error_category.value == "retryable"
    assert summary.items[0].result.raw == {"error_type": "TimeoutError"}


def test_discovery_emits_source_identity_items_and_respects_resume():
    runtime = _Runtime(
        discovery_results={
            SourceName.YINGYONGBAO: (
                _success(SourceName.YINGYONGBAO, "A", "pkg.a"),
                _success(SourceName.YINGYONGBAO, "B", "pkg.b"),
            )
        }
    )
    manifest = {
        "mode": "discovery",
        "source_order": ["yingyongbao"],
        "targets": [],
        "discovery_paths": [{"source": "yingyongbao", "path": "/game/1-1-1-0-0"}],
        "limits": {"max_requests": 4},
    }

    summary = JobExecutor(runtime).execute(
        manifest,
        completed_item_keys=("discovery-yingyongbao-pkg.a",),
    )

    assert [item.item_key for item in summary.items] == ["discovery-yingyongbao-pkg.b"]


def test_request_limit_fails_closed_before_extra_source_call():
    manifest = _refresh_manifest()
    manifest["limits"]["max_requests"] = 1
    runtime = _Runtime(
        refresh_results={
            SourceName.BAIDU: _success(SourceName.BAIDU, "Fixture", "b-1"),
        }
    )

    with pytest.raises(JobExecutionError, match="max_requests"):
        JobExecutor(runtime).execute(manifest)

    assert len(runtime.calls) == 1


def test_execution_checkpoint_callback_preserves_full_result_for_restart():
    success = CanonicalGameResult(
        source=SourceName.BAIDU,
        requested_name="Fixture",
        status=ResultStatus.SUCCESS,
        source_game_id="b-1",
        name="Fixture",
        raw={"id": "b-1"},
        media=(MediaReference(role="icon", source_url="https://img.test/a.png"),),
    )
    runtime = _Runtime(
        refresh_results={
            SourceName.BAIDU: success,
            SourceName.NINEGAME: CanonicalGameResult(
                source=SourceName.NINEGAME,
                requested_name="Fixture",
                status=ResultStatus.MISS,
            ),
            SourceName.TAPTAP: _success(SourceName.TAPTAP, "Fixture", "t-1"),
        }
    )
    checkpoints = []

    summary = JobExecutor(runtime).execute(
        _refresh_manifest(),
        on_item=lambda item, _request_count: checkpoints.append(
            executed_item_to_payload(item)
        ),
    )

    assert len(checkpoints) == 3
    restored = executed_item_from_payload(checkpoints[0])
    assert restored == summary.items[0]
    assert restored.result.media == success.media


def test_resume_request_budget_counts_requests_before_restart():
    manifest = _refresh_manifest()
    manifest["limits"]["max_requests"] = 2
    runtime = _Runtime(
        refresh_results={
            SourceName.NINEGAME: CanonicalGameResult(
                source=SourceName.NINEGAME,
                requested_name="Fixture",
                status=ResultStatus.MISS,
            )
        }
    )

    with pytest.raises(JobExecutionError, match="max_requests"):
        JobExecutor(runtime).execute(
            manifest,
            completed_item_keys=("target-7-baidu",),
            prior_request_count=1,
        )

    assert runtime.calls == [("refresh", SourceName.NINEGAME, "Fixture")]
