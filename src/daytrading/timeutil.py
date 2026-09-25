"""한국 시각 도우미."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


def parse_hms(value: str) -> time:
    parts = value.strip().split(":")
    if len(parts) != 3:
        raise ValueError(f"시각 형식이 아닙니다: {value}")
    hour, minute, second = (int(part) for part in parts)
    return time(hour, minute, second)


def format_hms(value: time) -> str:
    return f"{value.hour:02d}:{value.minute:02d}:{value.second:02d}"


def at_clock(day: date, clock: time) -> datetime:
    return datetime(day.year, day.month, day.day, clock.hour, clock.minute, clock.second, tzinfo=KST)


def parse_stamp(day: date, stamp: str) -> datetime:
    return at_clock(day, parse_hms(stamp))


def seconds_between(later: datetime, earlier: datetime) -> float:
    return (later - earlier).total_seconds()


def add_seconds(moment: datetime, seconds: float) -> datetime:
    return moment + timedelta(seconds=seconds)
