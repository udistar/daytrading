"""정규장 분봉을 풀세션으로 가른다.

풀세션은 설정한 시작~끝 시각의 분봉이 빠짐없이 있는 날이다.
봉 개수는 시계로 계산하고 코드에 78 같은 상수를 두지 않는다.
KRX 조기폐장·휴장 캘린더는 이 버전에서 확인하지 않았다. 봉이 빠지면 그 날은 풀세션이 아니다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from daytrading.settings import Settings
from daytrading.timeutil import KST, at_clock


@dataclass(frozen=True)
class MinuteBar:
    start: datetime
    open: int
    high: int
    low: int
    close: int
    volume: int


@dataclass(frozen=True)
class RatioBar:
    session_date: date
    slot: int
    open_ratio: float
    high_ratio: float
    low_ratio: float
    close_ratio: float


@dataclass(frozen=True)
class Session:
    day: date
    bars: tuple[MinuteBar, ...]
    ratios: tuple[RatioBar, ...]


def tic_minutes(settings: Settings) -> int:
    return int(settings.tm_tic_scope)


def expected_starts(day: date, settings: Settings) -> list[datetime]:
    """정규장 시작 이상, 끝 미만에 시작하는 봉 시각."""
    step = timedelta(minutes=tic_minutes(settings))
    cursor = at_clock(day, settings.clock("tm_session_open"))
    end = at_clock(day, settings.clock("tm_session_close"))
    starts: list[datetime] = []
    while cursor < end:
        starts.append(cursor)
        cursor += step
    return starts


def ratios_for(bars: list[MinuteBar]) -> tuple[RatioBar, ...]:
    """각 봉의 OHLC를 직전 봉 종가 대비 비율로 둔다. 첫 봉은 자신의 시가 대비다."""
    rows: list[RatioBar] = []
    previous = bars[0].open if bars else 0
    for slot, bar in enumerate(bars):
        base = previous if previous > 0 else bar.open
        if base <= 0:
            base = 1
        rows.append(
            RatioBar(
                session_date=bar.start.date(),
                slot=slot,
                open_ratio=bar.open / base,
                high_ratio=bar.high / base,
                low_ratio=bar.low / base,
                close_ratio=bar.close / base,
            )
        )
        previous = bar.close
    return tuple(rows)


def full_sessions(bars: list[MinuteBar], settings: Settings, before: date) -> tuple[list[Session], int]:
    """계획일 이전의 풀세션. 반환은 (채택, 계획일 이전의 풀세션 전체 개수)."""
    grouped: dict[date, list[MinuteBar]] = {}
    for bar in bars:
        grouped.setdefault(bar.start.date(), []).append(bar)
    complete: list[Session] = []
    for day in sorted(grouped):
        if day >= before:
            continue
        expected = expected_starts(day, settings)
        ordered = sorted(grouped[day], key=lambda item: item.start)
        unique: dict[datetime, MinuteBar] = {}
        for bar in ordered:
            unique[bar.start] = bar
        if [stamp for stamp in expected] != [stamp for stamp in expected if stamp in unique]:
            continue
        chosen = [unique[stamp] for stamp in expected]
        if len(chosen) != len(expected) or not chosen:
            continue
        complete.append(Session(day=day, bars=tuple(chosen), ratios=ratios_for(chosen)))
    total = len(complete)
    kept = complete[-int(settings.tm_lookback_sessions) :]
    return kept, total


def slot_index(bar_time: time, settings: Settings, day: date) -> int | None:
    starts = expected_starts(day, settings)
    for index, stamp in enumerate(starts):
        if stamp.time() == bar_time:
            return index
    return None


def bars_to_walk(day: date, entry_time: time, settings: Settings) -> list[int]:
    """진입 시각 이후(같으면 포함)이고 청산 시각 전인 봉의 슬롯."""
    until = settings.clock("tm_until")
    slots = []
    for index, stamp in enumerate(expected_starts(day, settings)):
        clock = stamp.time()
        if entry_time <= clock < until:
            slots.append(index)
    return slots
