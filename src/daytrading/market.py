"""체결 틱을 1분봉과 전략용 스냅샷으로 모은다."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from daytrading.models import Bar, Snapshot, SymbolMeta
from daytrading.settings import Settings


@dataclass
class Book:
    meta: SymbolMeta
    last_price: int = 0
    prev_price: int | None = None
    day_high: int = 0
    acc_value: int = 0
    best_bid: int = 0
    best_ask: int = 0
    bid_qty: int = 0
    ask_qty: int = 0
    vi_active: bool = False
    vi_price: int = 0
    vi_released_at: datetime | None = None
    completed: list[Bar] = field(default_factory=list)
    forming: Bar | None = None
    forming_base_value: int = 0
    dirty: bool = False
    strength_ticks: list[tuple[datetime, float]] = field(default_factory=list)

    def update(
        self,
        ts: datetime,
        price: int,
        acc_value: int,
        strength: float | None,
        best_bid: int,
        best_ask: int,
        bid_qty: int,
        ask_qty: int,
        vi_active: bool,
        vi_price: int,
    ) -> None:
        if self.last_price:
            self.prev_price = self.last_price
        self.last_price = price
        self.day_high = max(self.day_high, price)
        self.best_bid = best_bid
        self.best_ask = best_ask
        self.bid_qty = bid_qty
        self.ask_qty = ask_qty
        if vi_active and not self.vi_active:
            self.vi_price = vi_price or self.vi_price
        if vi_price:
            self.vi_price = vi_price
        if self.vi_active and not vi_active and self.vi_released_at is None:
            self.vi_released_at = ts
        self.vi_active = vi_active
        if strength is not None:
            self.strength_ticks.append((ts, strength))
        previous_acc = self.acc_value
        self.acc_value = acc_value
        self._roll_bar(ts, price, previous_acc, acc_value, strength)
        self.dirty = True

    def _roll_bar(
        self,
        ts: datetime,
        price: int,
        previous_acc: int,
        acc_value: int,
        strength: float | None,
    ) -> None:
        start = ts.replace(second=0, microsecond=0)
        first_print = self.forming is None and not self.completed
        if self.forming is None or self.forming.start != start:
            if self.forming is not None:
                self.completed.append(self.forming)
            self.forming = Bar(start=start, open=price, high=price, low=price, close=price, value=0)
            # 첫 틱은 장중 누적대금의 기준점만 잡는다. 그 다음 분부터 증가분을 1분 대금으로 본다.
            self.forming_base_value = acc_value if first_print else previous_acc
        bar = self.forming
        bar.high = max(bar.high, price)
        bar.low = min(bar.low, price)
        bar.close = price
        bar.value = max(0, acc_value - self.forming_base_value)
        if strength is not None:
            bar.strength_sum += strength
            bar.strength_n += 1

    def snapshot(self, ts: datetime, settings: Settings) -> Snapshot | None:
        if self.last_price <= 0 or self.forming is None:
            return None
        lookback = int(settings.minute_value_lookback_min)
        window = int(settings.strength_window_minutes)
        breakout_n = int(settings.breakout_lookback_min)
        samples = self.completed[-lookback:]
        avg = None
        if len(samples) >= lookback and lookback > 0:
            avg = sum(bar.value for bar in samples) / lookback
        prev_minute = self.completed[-1].value if self.completed else None
        cutoff = ts - timedelta(minutes=window)
        strengths = [value for moment, value in self.strength_ticks if moment >= cutoff]
        strength = sum(strengths) / len(strengths) if strengths else None
        breakout_bars = self.completed[-breakout_n:]
        breakout_high = max(bar.high for bar in breakout_bars) if len(breakout_bars) >= breakout_n else None
        session_high_before = self.day_high
        if self.last_price >= session_high_before:
            # 이번 틱이 고점을 만든 경우, 직전 고점과 비교해야 "갱신"을 알 수 있다.
            previous = self.prev_price or 0
            session_high_before = max((bar.high for bar in self.completed), default=previous)
        return Snapshot(
            code=self.meta.code,
            name=self.meta.name,
            ts=ts,
            price=self.last_price,
            prev_price=self.prev_price,
            prev_close=self.meta.prev_close,
            day_open=self.meta.day_open,
            day_high=self.day_high,
            acc_value=self.acc_value,
            minute_value=self.forming.value,
            prev_minute_value=prev_minute,
            avg_minute_value=avg,
            minute_samples=len(samples),
            strength=strength,
            best_bid=self.best_bid,
            best_ask=self.best_ask,
            bid_qty=self.bid_qty,
            ask_qty=self.ask_qty,
            upper_limit=self.meta.upper_limit,
            vi_active=self.vi_active,
            vi_price=self.vi_price,
            vi_released_at=self.vi_released_at,
            breakout_high=breakout_high,
            completed_bars=tuple(self.completed[-10:]),
            meta=self.meta,
            session_high_before=session_high_before,
        )


class Market:
    def __init__(self) -> None:
        self.books: dict[str, Book] = {}

    def ensure(self, meta: SymbolMeta) -> Book:
        book = self.books.get(meta.code)
        if book is None:
            book = Book(meta=meta)
            self.books[meta.code] = book
        return book

    def update(self, meta: SymbolMeta, **kwargs) -> Book:
        book = self.ensure(meta)
        book.meta = meta
        book.update(**kwargs)
        return book
