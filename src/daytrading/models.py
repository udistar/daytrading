"""매매 루프에서 쓰는 데이터."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


EXIT_REASONS = frozenset(
    {
        "stop",
        "breakeven",
        "trail",
        "partial_tp",
        "time_stop",
        "vi_exit",
        "schedule_flat",
        "market_flat",
        "hard_cutoff",
        "daily_loss",
        "emergency",
    }
)


def priority_for(reason: str) -> int:
    if reason in EXIT_REASONS:
        return 0
    if reason == "add":
        return 1
    return 2


@dataclass
class SymbolMeta:
    code: str
    name: str
    prev_close: int
    day_open: int
    upper_limit: int
    listed_on: date | None
    flags_known: bool = True
    is_admin: bool = False
    is_caution: bool = False
    is_warning: bool = False
    is_risk: bool = False
    is_overheat: bool = False
    is_cleanup: bool = False
    is_etf: bool = False
    is_etn: bool = False
    is_spac: bool = False
    is_preferred: bool = False
    market: str = "KOSDAQ"


@dataclass
class Bar:
    start: datetime
    open: int
    high: int
    low: int
    close: int
    value: int
    strength_sum: float = 0.0
    strength_n: int = 0

    @property
    def strength(self) -> float | None:
        if self.strength_n <= 0:
            return None
        return self.strength_sum / self.strength_n

    @property
    def down(self) -> bool:
        return self.close < self.open

    @property
    def upper_wick_ratio(self) -> float:
        span = self.high - self.low
        if span <= 0:
            return 0.0
        wick = self.high - max(self.open, self.close)
        return wick / span


@dataclass
class Snapshot:
    code: str
    name: str
    ts: datetime
    price: int
    prev_price: int | None
    prev_close: int
    day_open: int
    day_high: int
    acc_value: int
    minute_value: int
    prev_minute_value: int | None
    avg_minute_value: float | None
    minute_samples: int
    strength: float | None
    best_bid: int
    best_ask: int
    bid_qty: int
    ask_qty: int
    upper_limit: int
    vi_active: bool
    vi_price: int
    vi_released_at: datetime | None
    breakout_high: int | None
    completed_bars: tuple[Bar, ...]
    meta: SymbolMeta
    session_high_before: int

    @property
    def spread_pct(self) -> float | None:
        if self.price <= 0 or self.best_ask <= 0 or self.best_bid <= 0:
            return None
        return (self.best_ask - self.best_bid) / self.price * 100

    @property
    def bid_ask_ratio(self) -> float | None:
        if self.ask_qty <= 0:
            return None
        return self.bid_qty / self.ask_qty

    @property
    def change_prev_pct(self) -> float | None:
        if self.prev_close <= 0:
            return None
        return (self.price - self.prev_close) / self.prev_close * 100

    @property
    def change_open_pct(self) -> float | None:
        if self.day_open <= 0:
            return None
        return (self.price - self.day_open) / self.day_open * 100

    @property
    def gap_pct(self) -> float | None:
        if self.prev_close <= 0:
            return None
        return (self.day_open - self.prev_close) / self.prev_close * 100


@dataclass
class Intent:
    code: str
    name: str
    side: str
    reason: str
    qty: int
    order_type: str
    limit_price: int
    amount_krw: int
    note: str = ""

    @property
    def priority(self) -> int:
        return priority_for(self.reason)

    @property
    def tr_id(self) -> str:
        return "kt10000" if self.side == "buy" else "kt10001"


@dataclass
class Position:
    code: str
    name: str
    qty: int
    cost_krw: int
    fill_notional: int
    entry_price: int
    last_add_price: int
    buy_count: int
    opened_at: datetime
    last_buy_at: datetime
    high_since_entry: int
    partial_done: bool = False
    breakeven_armed: bool = False
    time_stop_cleared: bool = False
    vi_seen: bool = False
    vi_released_at: datetime | None = None
    vi_exit_bar_start: datetime | None = None
    schedule_partial_done: bool = False
    round_realized_krw: int = 0
    market: str = "KOSDAQ"

    @property
    def avg_price(self) -> float:
        if self.qty <= 0:
            return 0.0
        return self.fill_notional / self.qty


@dataclass
class OrderRecord:
    order_id: str
    ts: datetime
    code: str
    name: str
    side: str
    reason: str
    qty: int
    order_type: str
    limit_price: int
    status: str
    note: str = ""
    broker_order_no: str = ""


@dataclass
class Fill:
    order_id: str
    ts: datetime
    code: str
    name: str
    side: str
    reason: str
    qty: int
    price: int
    fee_krw: int
    tax_krw: int
    realized_delta_krw: int = 0


@dataclass
class Portfolio:
    cash: int
    realized_krw: int = 0
    positions: dict[str, Position] = field(default_factory=dict)
    consecutive_losses: int = 0
    pause_until: datetime | None = None
    day_halted: bool = False
    loss_halted: bool = False
    paused: bool = False
    emergency: bool = False
    pending_buys: set[str] = field(default_factory=set)
    pending_sells: set[str] = field(default_factory=set)
    traded_today: set[str] = field(default_factory=set)
    closed_trade_pnls: list[int] = field(default_factory=list)

    def open_count(self) -> int:
        return sum(1 for position in self.positions.values() if position.qty > 0) + len(self.pending_buys)
