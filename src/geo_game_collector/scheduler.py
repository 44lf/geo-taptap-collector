"""Pure GEO-compatible scheduling policy and restart-safe schedule state."""

from __future__ import annotations

import json
import os
import random
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from enum import Enum
from pathlib import Path
from zoneinfo import ZoneInfo

_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def parse_hhmm(value: str) -> time:
    matched = _HHMM.fullmatch(value)
    if matched is None:
        raise ValueError("time must use HH:MM")
    return time(hour=int(matched.group(1)), minute=int(matched.group(2)))


def in_window(start: time, end: time, now: datetime) -> bool:
    current = now.timetz().replace(tzinfo=None)
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end


def window_start_instant(start: time, now_local: datetime) -> datetime:
    candidate = now_local.replace(
        hour=start.hour,
        minute=start.minute,
        second=0,
        microsecond=0,
    )
    if candidate > now_local:
        candidate -= timedelta(days=1)
    return candidate.astimezone(UTC).replace(tzinfo=None)


def compute_next_gap(
    *,
    remaining_window_seconds: float,
    remaining_due: int,
    min_gap: float,
    max_gap: float,
    rng: random.Random,
) -> float:
    if min_gap <= 0 or max_gap < min_gap:
        raise ValueError("gap bounds are invalid")
    cap = max(0.0, remaining_window_seconds) / max(1, remaining_due)
    high = min(max_gap, max(min_gap, cap))
    return rng.uniform(min_gap, high)


@dataclass(frozen=True, slots=True)
class WindowQuota:
    success_quota: int
    attempt_multiplier: int = 4

    def __post_init__(self) -> None:
        if self.success_quota <= 0 or self.attempt_multiplier <= 0:
            raise ValueError("quota and attempt_multiplier must be positive")

    def can_attempt(self, *, successes: int, attempts: int) -> bool:
        if successes < 0 or attempts < 0:
            raise ValueError("successes and attempts must be non-negative")
        return (
            successes < self.success_quota
            and attempts < self.success_quota * self.attempt_multiplier
        )

    def remaining_successes(self, *, successes: int) -> int:
        if successes < 0:
            raise ValueError("successes must be non-negative")
        return max(0, self.success_quota - successes)


@dataclass(frozen=True, slots=True)
class SchedulePolicy:
    schedule_id: str
    config_version: int
    mode: str
    window_start: time
    window_end: time
    timezone_name: str
    catch_up_max_age: timedelta
    max_runs_per_start: int
    config_retrieved_at: datetime
    config_max_staleness: timedelta

    def __post_init__(self) -> None:
        if _SAFE_ID.fullmatch(self.schedule_id) is None:
            raise ValueError("schedule_id is invalid")
        if self.config_version <= 0:
            raise ValueError("config_version must be positive")
        if self.mode not in {"refresh", "discovery"}:
            raise ValueError("mode is invalid")
        ZoneInfo(self.timezone_name)
        if self.catch_up_max_age.total_seconds() <= 0:
            raise ValueError("catch_up_max_age must be positive")
        if self.max_runs_per_start <= 0:
            raise ValueError("max_runs_per_start must be positive")
        if self.config_max_staleness.total_seconds() <= 0:
            raise ValueError("config_max_staleness must be positive")
        _require_utc(self.config_retrieved_at)


@dataclass(frozen=True, slots=True)
class ScheduleState:
    schedule_id: str
    config_version: int
    next_due_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if _SAFE_ID.fullmatch(self.schedule_id) is None:
            raise ValueError("schedule_id is invalid")
        if self.config_version <= 0:
            raise ValueError("config_version must be positive")
        _require_utc(self.next_due_at)
        _require_utc(self.updated_at)


@dataclass(frozen=True, slots=True)
class ResumePlan:
    run_occurrences: tuple[datetime, ...]
    skipped_occurrences: tuple[datetime, ...]
    next_state: ScheduleState
    safe_stop_reason: str | None = None


def _require_utc(value: datetime) -> None:
    offset = value.utcoffset()
    if value.tzinfo is None or offset is None or offset.total_seconds() != 0:
        raise ValueError("timestamp must be UTC")


def plan_resume(
    policy: SchedulePolicy,
    state: ScheduleState,
    *,
    now: datetime,
) -> ResumePlan:
    _require_utc(now)
    if policy.schedule_id != state.schedule_id:
        raise ValueError("schedule policy and state identity differ")
    if policy.config_retrieved_at + policy.config_max_staleness <= now:
        return ResumePlan((), (), state, safe_stop_reason="configuration_expired")

    occurrences: list[datetime] = []
    cursor = state.next_due_at
    while cursor <= now:
        occurrences.append(cursor)
        if len(occurrences) > 10_000:
            raise ValueError("schedule backlog exceeds safety bound")
        cursor += timedelta(days=1)
    recent = [
        occurrence
        for occurrence in occurrences
        if now - occurrence <= policy.catch_up_max_age
    ]
    runs = tuple(recent[-policy.max_runs_per_start :])
    run_set = set(runs)
    skipped = tuple(
        occurrence for occurrence in occurrences if occurrence not in run_set
    )
    next_state = ScheduleState(
        schedule_id=state.schedule_id,
        config_version=policy.config_version,
        next_due_at=cursor,
        updated_at=now,
    )
    return ResumePlan(runs, skipped, next_state)


def _time_text(value: datetime) -> str:
    _require_utc(value)
    return value.isoformat().replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    _require_utc(parsed)
    return parsed


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


class ScheduleStateStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, schedule_id: str) -> Path:
        if _SAFE_ID.fullmatch(schedule_id) is None:
            raise ValueError("schedule_id is invalid")
        return self.root / f"{schedule_id}.json"

    def save(self, state: ScheduleState) -> None:
        _atomic_json(
            self._path(state.schedule_id),
            {
                "schedule_id": state.schedule_id,
                "config_version": state.config_version,
                "next_due_at": _time_text(state.next_due_at),
                "updated_at": _time_text(state.updated_at),
            },
        )

    def load(self, schedule_id: str) -> ScheduleState:
        payload = json.loads(self._path(schedule_id).read_text("utf-8"))
        return ScheduleState(
            schedule_id=str(payload["schedule_id"]),
            config_version=int(payload["config_version"]),
            next_due_at=_parse_time(payload["next_due_at"]),
            updated_at=_parse_time(payload["updated_at"]),
        )


class ExecutionTrigger(str, Enum):
    MANUAL = "manual"
    SCHEDULED = "scheduled"


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    job_id: str
    config_version: int
    mode: str
    trigger: ExecutionTrigger
    scheduled_for: datetime


def build_execution_request(
    *,
    job_id: str,
    config_version: int,
    mode: str,
    trigger: ExecutionTrigger,
    scheduled_for: datetime,
) -> ExecutionRequest:
    if _SAFE_ID.fullmatch(job_id) is None:
        raise ValueError("job_id is invalid")
    if config_version <= 0:
        raise ValueError("config_version must be positive")
    if mode not in {"refresh", "discovery"}:
        raise ValueError("mode is invalid")
    _require_utc(scheduled_for)
    return ExecutionRequest(
        job_id=job_id,
        config_version=config_version,
        mode=mode,
        trigger=trigger,
        scheduled_for=scheduled_for,
    )
