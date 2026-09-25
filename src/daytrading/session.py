"""전략, 위험 계층, 모의 체결을 한 시계로 돌린다."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from daytrading.broker import MockBroker
from daytrading.execution import OrderQueue, RateGate
from daytrading.journal import Journal
from daytrading.ledger import apply_fill
from daytrading.market import Market
from daytrading.models import Intent, Portfolio, Snapshot
from daytrading.risk import mark_to_market, protection_intents, veto_buy
from daytrading.settings import Settings
from daytrading.strategy import evaluate, phase_name


class Engine:
    def __init__(self, settings: Settings, journal: Journal, scenario: str):
        self.settings = settings
        self.journal = journal
        self.journal.scenario = scenario
        self.market = Market()
        self.portfolio = Portfolio(cash=settings.initial_capital_krw)
        self.queue = OrderQueue()
        self.gate = RateGate(settings)
        self.broker = MockBroker()
        self.prices: dict[str, int] = {}
        self.noted: set[tuple[str, str, str]] = set()
        self._seq = 0
        self.fill_count = 0
        self.rejection_count = 0
        self.reasons: dict[str, int] = defaultdict(int)
        self._clock = datetime.now().astimezone()

    def enqueue(self, intent: Intent) -> None:
        if intent.side == "buy" and intent.code in self.portfolio.pending_buys:
            return
        if intent.side == "sell" and intent.code in self.portfolio.pending_sells:
            return
        if intent.side == "buy":
            self.portfolio.pending_buys.add(intent.code)
        else:
            self.portfolio.pending_sells.add(intent.code)
        self.queue.push(intent)
        self.journal.signal(self._clock, intent.code, intent.reason, intent.note)

    def reject(self, ts: datetime, code: str, side: str, reason: str, detail: str = "") -> None:
        key = (code, side, reason)
        if key in self.noted:
            return
        self.noted.add(key)
        self.rejection_count += 1
        self.journal.reject(ts, code, side, reason, detail)

    def drain(self, now: datetime) -> None:
        self._clock = now
        pnl = self._pnl()
        if pnl <= -self.settings.daily_loss_limit_krw:
            self.portfolio.loss_halted = True
        for intent in protection_intents(self.portfolio, self.prices, self.settings, now, pnl):
            self.enqueue(intent)
        while self.queue:
            intent = self.queue.peek()
            snap = self._snap(intent.code, now)
            if intent.side == "buy":
                veto = veto_buy(intent, self.portfolio, self.settings, snap, now, self._pnl())
                if veto:
                    self.queue.pop()
                    self.portfolio.pending_buys.discard(intent.code)
                    self.reject(now, intent.code, intent.side, veto, intent.note)
                    continue
            if snap is None or not self.gate.ready(intent.tr_id, now):
                break
            self.gate.take(intent.tr_id, now)
            self.queue.pop()
            self._seq += 1
            record, fill = self.broker.submit(f"{self._seq:04d}", intent, snap, self.settings)
            self.journal.order(record)
            if fill is None:
                self.portfolio.pending_buys.discard(intent.code)
                self.portfolio.pending_sells.discard(intent.code)
                self.reject(now, intent.code, intent.side, "미체결 취소", intent.reason)
                continue
            apply_fill(self.portfolio, fill, self.settings)
            self.journal.fill(fill)
            self.fill_count += 1
            self.reasons[fill.reason] += 1
            self.prices[fill.code] = fill.price
            pnl = self._pnl()
            if pnl <= -self.settings.daily_loss_limit_krw:
                self.portfolio.loss_halted = True
            for extra in protection_intents(self.portfolio, self.prices, self.settings, now, pnl):
                self.enqueue(extra)

    def _pnl(self) -> int:
        return mark_to_market(self.portfolio, self.prices, self.settings)

    def _snap(self, code: str, now: datetime) -> Snapshot | None:
        book = self.market.books.get(code)
        if book is None:
            return None
        return book.snapshot(now, self.settings)

    def summary(self) -> dict:
        pnl = self._pnl()
        return {
            "scenario": self.journal.scenario,
            "realized_krw": self.portfolio.realized_krw,
            "pnl_krw": pnl,
            "cash": self.portfolio.cash,
            "fill_count": self.fill_count,
            "rejection_count": self.rejection_count,
            "reasons": dict(self.reasons),
            "day_halted": self.portfolio.day_halted,
            "loss_halted": self.portfolio.loss_halted,
            "consecutive_losses": self.portfolio.consecutive_losses,
            "open_positions": sorted(code for code, pos in self.portfolio.positions.items() if pos.qty > 0),
            "phase_end": "",
        }


def run_ticks(engine: Engine, ticks: list, controls: dict | None = None) -> dict:
    grouped: dict[datetime, list] = defaultdict(list)
    for tick in ticks:
        stamp = tick.ts.replace(microsecond=0)
        grouped[stamp].append(tick)
    if not grouped:
        return engine.summary()
    cursor = min(grouped)
    end = max(grouped)
    while cursor <= end:
        engine._clock = cursor
        if controls:
            engine.portfolio.paused = bool(controls.get("paused"))
            if controls.get("emergency"):
                engine.portfolio.emergency = True
        for tick in grouped.get(cursor, []):
            engine.market.update(
                tick.meta,
                ts=tick.ts,
                price=tick.price,
                acc_value=tick.acc_value,
                strength=tick.strength,
                best_bid=tick.best_bid,
                best_ask=tick.best_ask,
                bid_qty=tick.bid_qty,
                ask_qty=tick.ask_qty,
                vi_active=tick.vi_active,
                vi_price=tick.vi_price,
            )
            engine.prices[tick.meta.code] = tick.price
        codes = [
            code
            for code, book in engine.market.books.items()
            if book.dirty or code in engine.portfolio.positions or code in engine.portfolio.pending_buys
        ]
        for code in sorted(codes):
            book = engine.market.books[code]
            snap = book.snapshot(cursor, engine.settings)
            if snap is None:
                continue
            if book.dirty:
                engine.journal.orderbook(snap)
            intents, blocked = evaluate(snap, engine.portfolio, engine.settings)
            book.prev_price = book.last_price
            book.dirty = False
            for blocked_code, reason in blocked:
                side = "buy"
                engine.reject(cursor, blocked_code, side, reason)
            for intent in intents:
                engine._clock = cursor
                engine.enqueue(intent)
        engine.drain(cursor)
        cursor += timedelta(seconds=1)
    summary = engine.summary()
    summary["phase_end"] = phase_name(end, engine.settings)
    return summary
