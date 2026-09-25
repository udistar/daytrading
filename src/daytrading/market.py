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
    high_before_tick: int = 0
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
    value_marks: list[tuple[datetime, int]] = field(default_factory=list)

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
        self.high_before_tick = self.day_high
        self.day_high = max(self.day_high, price)
        self.best_bid = best_bid
        self.best_ask = best_ask
        self.bid_qty = bid_qty
        self.ask_qty = ask_qty
        if vi_active and not self.vi_active:
            self.vi_price = vi_price or self.vi_price
            self.vi_released_at = None
        if vi_price:
            self.vi_price = vi_price
        if self.vi_active and not vi_active:
            self.vi_released_at = ts
        self.vi_active = vi_active
        if strength is not None:
            self.strength_ticks.append((ts, strength))
        self.value_marks.append((ts, acc_value))
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
        self.strength_ticks = [item for item in self.strength_ticks if item[0] >= cutoff - timedelta(minutes=1)]
        strengths = [value for moment, value in self.strength_ticks if moment >= cutoff]
        strength = sum(strengths) / len(strengths) if strengths else None
        breakout_bars = self.completed[-breakout_n:]
        breakout_high = max(bar.high for bar in breakout_bars) if len(breakout_bars) >= breakout_n else None
        # 새 틱이 아니면 현재가가 이미 고가에 포함돼 있으므로 신고가가 아니다.
        session_high_before = self.high_before_tick if self.dirty else self.day_high
        history = max(lookback + 1, breakout_n, int(settings.add_down_bars) + 1, window, 2)
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
            minute_value=self.trailing_value(ts),
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
            completed_bars=tuple(self.completed[-history:]),
            meta=self.meta,
            session_high_before=session_high_before,
        )

    def trailing_value(self, ts: datetime, seconds: int = 60) -> int:
        """직전 60초 거래대금. 분 초반의 미완성 분봉 대신 쓴다."""
        if not self.value_marks:
            return self.forming.value if self.forming else 0
        cutoff = ts - timedelta(seconds=seconds)
        base = None
        for moment, acc in self.value_marks:
            if moment <= cutoff:
                base = acc
            else:
                break
        if base is None:
            base = self.value_marks[0][1]
        return max(0, self.acc_value - base)


def quote_snapshot(code: str, name: str, ts: datetime, price: int, prev_close: int) -> Snapshot:
    """호가 창이 없을 때 마지막 가격으로 시장가 청산할 수 있게 만드는 스냅샷."""
    from datetime import date as _date

    meta = SymbolMeta(
        code=code,
        name=name,
        prev_close=prev_close,
        day_open=prev_close or price,
        upper_limit=0,
        listed_on=_date(2000, 1, 1) if prev_close else None,
        flags_known=False,
    )
    return Snapshot(
        code=code,
        name=name,
        ts=ts,
        price=price,
        prev_price=price,
        prev_close=prev_close,
        day_open=meta.day_open,
        day_high=price,
        acc_value=0,
        minute_value=0,
        prev_minute_value=None,
        avg_minute_value=None,
        minute_samples=0,
        strength=None,
        best_bid=price,
        best_ask=price,
        bid_qty=0,
        ask_qty=0,
        upper_limit=0,
        vi_active=False,
        vi_price=0,
        vi_released_at=None,
        breakout_high=None,
        completed_bars=(),
        meta=meta,
        session_high_before=price,
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
