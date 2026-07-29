from __future__ import annotations

import random
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from geo_game_collector.scheduler import (
    ExecutionTrigger,
    SchedulePolicy,
    ScheduleState,
    ScheduleStateStore,
    WindowQuota,
    build_execution_request,
    compute_next_gap,
    in_window,
    parse_hhmm,
    plan_resume,
    window_start_instant,
)

UTC8 = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 7, 29, 10, 0, tzinfo=UTC)


def test_geo_compatible_time_windows_and_random_gap_bounds() -> None:
    assert parse_hhmm("03:15") == time(3, 15)
    with pytest.raises(ValueError):
        parse_hhmm("24:00")
    assert in_window(
        time(23, 0),
        time(2, 0),
        datetime(2026, 7, 29, 1, 0, tzinfo=UTC8),
    )
    assert not in_window(
        time(3, 0),
        time(6, 0),
        datetime(2026, 7, 29, 2, 59, tzinfo=UTC8),
    )
    assert window_start_instant(
        time(23, 0),
        datetime(2026, 7, 29, 1, 0, tzinfo=UTC8),
    ) == datetime(2026, 7, 28, 15, 0)

    rng = random.Random(7)
    gaps = [
        compute_next_gap(
            remaining_window_seconds=600,
            remaining_due=10,
            min_gap=20,
            max_gap=90,
            rng=rng,
        )
        for _ in range(20)
    ]
    assert all(20 <= gap <= 60 for gap in gaps)
    assert (
        compute_next_gap(
            remaining_window_seconds=-1,
            remaining_due=100,
            min_gap=20,
            max_gap=90,
            rng=rng,
        )
        == 20
    )


def test_success_quota_and_attempt_cap_match_geo_policy() -> None:
    quota = WindowQuota(success_quota=3, attempt_multiplier=4)

    assert quota.can_attempt(successes=0, attempts=0)
    assert not quota.can_attempt(successes=3, attempts=3)
    assert not quota.can_attempt(successes=0, attempts=12)
    assert quota.remaining_successes(successes=1) == 2


def _policy(**changes) -> SchedulePolicy:
    values = {
        "schedule_id": "refresh-nightly",
        "config_version": 3,
        "mode": "refresh",
        "window_start": time(3, 0),
        "window_end": time(6, 0),
        "timezone_name": "Asia/Shanghai",
        "catch_up_max_age": timedelta(days=2),
        "max_runs_per_start": 2,
        "config_retrieved_at": NOW - timedelta(minutes=5),
        "config_max_staleness": timedelta(hours=1),
    }
    values.update(changes)
    return SchedulePolicy(**values)


def test_resume_runs_only_recent_bounded_occurrences_and_skips_old_backlog() -> None:
    state = ScheduleState(
        schedule_id="refresh-nightly",
        config_version=3,
        next_due_at=datetime(2026, 7, 24, 19, 0, tzinfo=UTC),
        updated_at=datetime(2026, 7, 24, 18, 0, tzinfo=UTC),
    )

    plan = plan_resume(_policy(), state, now=NOW)

    assert plan.safe_stop_reason is None
    assert plan.run_occurrences == (
        datetime(2026, 7, 27, 19, 0, tzinfo=UTC),
        datetime(2026, 7, 28, 19, 0, tzinfo=UTC),
    )
    assert len(plan.skipped_occurrences) == 3
    assert plan.next_state.next_due_at == datetime(2026, 7, 29, 19, 0, tzinfo=UTC)


def test_resume_does_not_advance_schedule_when_configuration_is_expired() -> None:
    state = ScheduleState(
        schedule_id="refresh-nightly",
        config_version=3,
        next_due_at=NOW - timedelta(minutes=1),
        updated_at=NOW - timedelta(hours=2),
    )
    policy = _policy(
        config_retrieved_at=NOW - timedelta(hours=2),
        config_max_staleness=timedelta(hours=1),
    )

    plan = plan_resume(policy, state, now=NOW)

    assert plan.safe_stop_reason == "configuration_expired"
    assert plan.run_occurrences == ()
    assert plan.skipped_occurrences == ()
    assert plan.next_state == state


def test_schedule_state_persists_next_due_and_survives_restart(tmp_path: Path) -> None:
    store = ScheduleStateStore(tmp_path / "state" / "schedules")
    state = ScheduleState(
        schedule_id="refresh-nightly",
        config_version=3,
        next_due_at=NOW + timedelta(hours=1),
        updated_at=NOW,
    )

    store.save(state)

    assert (
        ScheduleStateStore(tmp_path / "state" / "schedules").load("refresh-nightly")
        == state
    )


def test_manual_and_scheduled_runs_share_one_execution_request_contract() -> None:
    scheduled = build_execution_request(
        job_id="job-1",
        config_version=3,
        mode="refresh",
        trigger=ExecutionTrigger.SCHEDULED,
        scheduled_for=NOW,
    )
    manual = build_execution_request(
        job_id="job-2",
        config_version=3,
        mode="refresh",
        trigger=ExecutionTrigger.MANUAL,
        scheduled_for=NOW,
    )

    assert scheduled.__class__ is manual.__class__
    assert scheduled.config_version == manual.config_version == 3
    assert scheduled.mode == manual.mode == "refresh"
    assert scheduled.trigger is ExecutionTrigger.SCHEDULED
    assert manual.trigger is ExecutionTrigger.MANUAL
