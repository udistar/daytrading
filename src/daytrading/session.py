"""전략, 위험 계층, 모의 체결을 한 시계로 돌린다."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from daytrading.broker import MockBroker
from daytrading.execution import OrderQueue, RateGate
from daytrading.journal import Journal
from daytrading.ledger import apply_fill
from daytrading.market import Market, quote_snapshot
from daytrading.models import Intent, Portfolio, Snapshot
from daytrading.risk import mark_to_market, protection_intents, veto_buy
from daytrading.settings import Settings
from daytrading.strategy import evaluate, phase_name


class Engine:
    def __init__(self, settings: Settings, journal: Journal, scenario: str, broker=None):
        self.settings = settings
        self.journal = journal
        self.journal.scenario = scenario
        self.market = Market()
        self.portfolio = Portfolio(cash=settings.initial_capital_krw)
        self.queue = OrderQueue()
        self.gate = RateGate(settings)
        self.broker = broker or MockBroker()
        self.prices: dict[str, int] = {}
        self._seq = 0
        self.fill_count = 0
        self.rejection_count = 0
        self.reasons: dict[str, int] = defaultdict(int)
        self._clock = datetime.now().astimezone()

    def enqueue(self, intent: Intent) -> None:
        if intent.side == "sell":
            self._cancel_buys(intent.code)
            room = self.portfolio.sell_room(intent.code)
            if room <= 0:
                return
            if intent.qty > room:
                intent.qty = room
                intent.amount_krw = max(intent.amount_krw, intent.qty)
            self.portfolio.hold_sell(intent.code, intent.qty)
        else:
            if intent.code in self.portfolio.pending_buys:
                return
            self.portfolio.pending_buys.add(intent.code)
            self.portfolio.pending_buy_amount[intent.code] = intent.amount_krw
        self.queue.push(intent)
        self.journal.signal(self._clock, intent.code, intent.reason, intent.note)

    def _cancel_buys(self, code: str) -> None:
        for pending in self.queue.cancel(code, "buy"):
            self.portfolio.pending_buys.discard(code)
            self.portfolio.pending_buy_amount.pop(code, None)
            self.reject(self._clock, code, "buy", "청산으로 매수 취소", pending.reason)

    def reject(self, ts: datetime, code: str, side: str, reason: str, detail: str = "") -> None:
        self.rejection_count += 1
        self.journal.reject(ts, code, side, reason, detail)

    def _release(self, intent: Intent) -> None:
        if intent.side == "buy":
            self.portfolio.pending_buys.discard(intent.code)
            self.portfolio.pending_buy_amount.pop(intent.code, None)
            return
        self.portfolio.free_sell(intent.code, intent.qty)
        position = self.portfolio.positions.get(intent.code)
        if position is None:
            return
        if intent.reason == "partial_tp":
            position.partial_done = False
        if intent.reason == "schedule_flat":
            position.schedule_partial_done = False

    def drain(self, now: datetime) -> None:
        self._clock = now
        pnl = self._pnl()
        if pnl <= -self.settings.daily_loss_limit_krw:
            self.portfolio.loss_halted = True
        for intent in protection_intents(self.portfolio, self.prices, self.settings, now, pnl):
            self.enqueue(intent)
        deferred: list[Intent] = []
        blocked_trs: set[str] = set()
        while self.queue:
            intent = self.queue.peek()
            if intent is None:
                break
            if intent.tr_id in blocked_trs:
                deferred.append(self.queue.pop())
                continue
            snap = self._snap(intent.code, now)
            if intent.side == "buy":
                veto = veto_buy(intent, self.portfolio, self.settings, snap, now, self._pnl())
                if veto:
                    self.queue.pop()
                    self._release(intent)
                    self.reject(now, intent.code, intent.side, veto, intent.note)
                    continue
            if snap is None and intent.side == "sell":
                snap = self._synthetic_snap(intent, now)
            if snap is None:
                deferred.append(self.queue.pop())
                continue
            if not self.gate.bucket_ready(now):
                break
            if not self.gate.tr_ready(intent.tr_id, now):
                blocked_trs.add(intent.tr_id)
                deferred.append(self.queue.pop())
                continue
            self.gate.take(intent.tr_id, now)
            self.queue.pop()
            self._seq += 1
            record, fill = self.broker.submit(f"{self._seq:04d}", intent, snap, self.settings)
            self.journal.order(record)
            if record.status in {"accepted", "dry_run"}:
                if record.status == "dry_run":
                    self._release(intent)
                continue
            if fill is None:
                self._release(intent)
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
        for intent in deferred:
            self.queue.push(intent)

    def _pnl(self) -> int:
        return mark_to_market(self.portfolio, self.prices, self.settings)

    def _snap(self, code: str, now: datetime) -> Snapshot | None:
        book = self.market.books.get(code)
        if book is None:
            return None
        return book.snapshot(now, self.settings)

    def _synthetic_snap(self, intent: Intent, now: datetime) -> Snapshot | None:
        book = self.market.books.get(intent.code)
        price = self.prices.get(intent.code, 0)
        if price <= 0 and book is not None:
            price = book.last_price
        if price <= 0:
            return None
        prev_close = book.meta.prev_close if book is not None else 0
        return quote_snapshot(intent.code, intent.name, now, price, prev_close)

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
