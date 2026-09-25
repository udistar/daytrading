"""키 없이 하루 장을 재현하는 스크립트. 진입·불타기·손절·트레일·부분익절·시간손절·청산·일한도를 넣는다."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from daytrading.models import SymbolMeta
from daytrading.settings import default_settings, tick_size
from daytrading.timeutil import parse_stamp

SIM_DAY = date(2026, 9, 25)


@dataclass
class Tick:
    ts: datetime
    meta: SymbolMeta
    price: int
    acc_value: int
    strength: float
    best_bid: int
    best_ask: int
    bid_qty: int = 10_000
    ask_qty: int = 10_000
    vi_active: bool = False
    vi_price: int = 0


def _meta(code: str, name: str, prev: int = 10_000, day_open: int = 10_100, **flags) -> SymbolMeta:
    upper = int(prev * (1 + default_settings().limit_up_ratio_pct / 100))
    return SymbolMeta(
        code=code,
        name=name,
        prev_close=prev,
        day_open=day_open,
        upper_limit=upper,
        listed_on=date(2020, 1, 2),
        flags_known=True,
        **flags,
    )


class Tape:
    def __init__(self, meta: SymbolMeta, acc: int = 5_000_000_000):
        self.meta = meta
        self.acc = acc
        self.ticks: list[Tick] = []

    def add(
        self,
        stamp: str,
        price: int,
        delta: int,
        strength: float = 140,
        *,
        bid_qty: int = 10_000,
        ask_qty: int = 10_000,
        vi_active: bool = False,
        vi_price: int = 0,
    ) -> None:
        self.acc += delta
        settings = default_settings()
        tick = tick_size(settings, price)
        self.ticks.append(
            Tick(
                ts=parse_stamp(SIM_DAY, stamp),
                meta=self.meta,
                price=price,
                acc_value=self.acc,
                strength=strength,
                best_bid=max(1, price - tick),
                best_ask=price + tick,
                bid_qty=bid_qty,
                ask_qty=ask_qty,
                vi_active=vi_active,
                vi_price=vi_price,
            )
        )

    def seed(self, start: str, end: str, price: int, step: int = 100_000_000, strength: float = 140) -> None:
        cursor = parse_stamp(SIM_DAY, start)
        last = parse_stamp(SIM_DAY, end)
        first = True
        while cursor <= last:
            stamp = cursor.strftime("%H:%M:%S")
            self.add(stamp, price, 0 if first else step, strength)
            first = False
            cursor += timedelta(minutes=1)


def morning_ticks() -> list[Tick]:
    quiet = 10_200
    rise = Tape(_meta("005930", "상승"))
    stop = Tape(_meta("000660", "손절"))
    hold = Tape(_meta("051910", "보유"))
    timed = Tape(_meta("035420", "시간"))
    addstop = Tape(_meta("006400", "추가손절"))
    reject = Tape(_meta("068270", "휴식거절"))
    late = Tape(_meta("207940", "종료거절"))
    for tape in (rise, stop, hold, timed, addstop, reject, late):
        tape.seed("08:50:00", "09:05:00", quiet)

    # 상승: 분마다 거래대금을 유지하며 불타기 → 부분 익절 → 트레일링.
    rise.add("09:06:00", 10_550, 400_000_000)
    rise.add("09:07:00", 10_800, 400_000_000)
    rise.add("09:08:00", 11_050, 400_000_000)
    rise.add("09:09:00", 11_300, 400_000_000)
    rise.add("09:10:00", 11_600, 400_000_000)
    rise.add("09:11:00", 12_100, 400_000_000)
    rise.add("09:12:00", 11_700, 400_000_000)

    # 첫 매수 손절.
    stop.add("09:13:00", 10_550, 400_000_000)
    stop.add("09:13:20", 10_300, 50_000_000)

    # 시간 손절을 피하지 못할 만큼만 오른 보유. 09:47/09:49:30까지 남긴다.
    hold.add("09:13:30", 11_000, 400_000_000)
    hold.add("09:14:10", 11_120, 200_000_000)
    hold.add("09:47:00", 11_120, 100_000_000)
    hold.add("09:49:30", 11_120, 100_000_000)
    hold.add("09:50:05", 11_120, 50_000_000)

    # 3분 안에 +0.7%를 못 넘김.
    timed.add("09:14:00", 10_550, 400_000_000)
    timed.add("09:16:00", 10_580, 200_000_000)
    timed.add("09:17:05", 10_580, 200_000_000)

    # 휴식 중 돌파. 위험 계층이 거절해야 한다.
    reject.add("09:20:00", 10_550, 400_000_000)

    # 휴식 후 추가 매수까지 갔다가 급락. 연속 손절 3회.
    addstop.add("09:28:00", 10_550, 400_000_000)
    addstop.add("09:28:40", 10_820, 400_000_000)
    addstop.add("09:29:20", 9_900, 50_000_000)

    # 당일 종료 뒤 돌파.
    late.add("09:36:00", 10_550, 400_000_000)
    late.add("09:51:00", 10_200, 0, strength=80)
    ticks: list[Tick] = []
    for tape in (rise, stop, hold, timed, addstop, reject, late):
        ticks.extend(tape.ticks)
    return ticks


def daily_loss_ticks() -> list[Tick]:
    gap = Tape(_meta("096770", "갭하락"))
    other = Tape(_meta("003670", "동반청산"))
    for tape in (gap, other):
        tape.seed("08:50:00", "09:05:00", 10_200)
    gap.add("09:06:00", 10_550, 400_000_000)
    other.add("09:06:02", 10_550, 400_000_000)
    gap.add("09:08:00", 6_000, 50_000_000)
    other.add("09:08:05", 10_500, 50_000_000)
    other.add("09:12:00", 10_200, 0, strength=80)
    ticks: list[Tick] = []
    for tape in (gap, other):
        ticks.extend(tape.ticks)
    return ticks


def vi_ticks() -> list[Tick]:
    held = Tape(_meta("247540", "VI보유"))
    fresh = Tape(_meta("086520", "VI신규"))
    for tape in (held, fresh):
        tape.seed("08:50:00", "09:05:00", 10_200)
    held.add("09:06:00", 10_550, 400_000_000)
    held.add("09:07:00", 10_680, 200_000_000, vi_active=True, vi_price=10_700)
    held.add("09:08:30", 10_680, 50_000_000, vi_active=False, vi_price=10_700)
    held.add("09:09:00", 10_620, 50_000_000, vi_price=10_700)
    held.add("09:09:40", 10_540, 50_000_000, vi_price=10_700)
    held.add("09:10:05", 10_540, 50_000_000, vi_price=10_700)
    fresh.add("09:06:10", 10_200, 0, vi_active=True, vi_price=9_000)
    fresh.add("09:06:30", 10_200, 0, vi_active=False, vi_price=9_000)
    fresh.add("09:07:00", 10_550, 400_000_000, vi_price=9_000)
    return held.ticks + fresh.ticks


def all_scenarios() -> list[tuple[str, list[Tick]]]:
    return [
        ("morning", morning_ticks()),
        ("daily_loss", daily_loss_ticks()),
        ("vi", vi_ticks()),
    ]
