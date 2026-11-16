"""First Map — birth-time resolution and scan windows for web onboarding."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterator, Literal

TimePrecision = Literal["exact", "approximate", "low"]

TIME_BUCKET_HHMMSS: dict[str, str] = {
    "night_early": "03:00:00",
    "morning": "08:30:00",
    "late_morning": "12:30:00",
    "afternoon": "16:00:00",
    "evening": "20:00:00",
    "night_late": "23:00:00",
}

TIME_BUCKET_RANGES: dict[str, tuple[str, str]] = {
    "night_early": ("00:00:00", "05:59:00"),
    "morning": ("06:00:00", "10:59:00"),
    "late_morning": ("11:00:00", "13:59:00"),
    "afternoon": ("14:00:00", "17:59:00"),
    "evening": ("18:00:00", "21:59:00"),
    "night_late": ("22:00:00", "23:59:00"),
}

SCAN_STEP_MINUTES = 30
UNKNOWN_SCAN_STEP_MINUTES = 60

UNKNOWN_BIRTH_TIME = "12:00:00"


def resolve_birth_time(
    time_mode: Literal["exact", "approximate", "unknown"],
    *,
    time_exact: str | None = None,
    time_bucket: str | None = None,
) -> tuple[str, TimePrecision]:
    if time_mode == "exact":
        if not time_exact or not time_exact.strip():
            raise ValueError("time_exact is required when time_mode is exact")
        t = time_exact.strip()
        if len(t) == 5:
            t = f"{t}:00"
        return t, "exact"
    if time_mode == "approximate":
        if not time_bucket or time_bucket not in TIME_BUCKET_HHMMSS:
            raise ValueError("invalid or missing time_bucket")
        return TIME_BUCKET_HHMMSS[time_bucket], "approximate"
    return UNKNOWN_BIRTH_TIME, "low"


def resolve_time_window(
    time_mode: Literal["exact", "approximate", "unknown"],
    *,
    time_bucket: str | None = None,
) -> tuple[str, str] | None:
    """Inclusive birth-time window for archetype scanning. None when mode is exact."""
    if time_mode == "unknown":
        return ("00:00:00", "23:59:00")
    if time_mode == "approximate":
        if not time_bucket or time_bucket not in TIME_BUCKET_RANGES:
            raise ValueError("invalid or missing time_bucket")
        return TIME_BUCKET_RANGES[time_bucket]
    return None


def iter_times_in_window(
    date: str,
    start: str,
    end: str,
    step_minutes: int = SCAN_STEP_MINUTES,
) -> Iterator[str]:
    current = datetime.fromisoformat(f"{date}T{start}")
    end_dt = datetime.fromisoformat(f"{date}T{end}")
    while current <= end_dt:
        yield current.strftime("%H:%M:%S")
        current += timedelta(minutes=step_minutes)
