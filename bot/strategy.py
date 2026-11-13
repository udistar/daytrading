"""Volatility breakout, one long trade per stock per day.

  target = today's open + K x (yesterday's high - yesterday's low), rounded up to a KRX tick
  buy    when the price reaches the target inside the entry window (default 09:00-10:00)
  sell   at the first of: stop loss (entry -2%), trailing stop (3% under the high since entry),
         time cut (15:18, before the 15:20 closing auction starts)

Pure logic: no network, no clock of its own. The engine feeds it (time, price)
and tells it when an order filled or failed.
"""
from dataclasses import dataclass

from . import krx

WAIT, BUYING, HOLD, SELLING, DONE = "WAIT", "BUYING", "HOLD", "SELLING", "DONE"


@dataclass
class StrategyConfig:
    k: float = 0.5
    stop_loss_pct: float = 2.0
    trailing_stop_pct: float = 3.0
    entry_start: str = "09:00"
    entry_end: str = "10:00"
    time_cut: str = "15:18"
    min_range_pct: float = 0.5      # skip when yesterday's range is under 0.5% of the open: nothing to break out of


@dataclass
class Signal:
    side: str       # "buy" or "sell"
    reason: str
    price: float


class Breakout:
    def __init__(self, code, cfg, prev_high, prev_low, today_open, upper_limit=None):
        self.code, self.cfg = code, cfg
        self.prev_high, self.prev_low, self.open = prev_high, prev_low, today_open
        self.range = prev_high - prev_low
        self.target = krx.round_up_tick(today_open + cfg.k * self.range) if self.range > 0 else None
        self.upper_limit = upper_limit
        self.state = WAIT
        self.entry = self.peak = None
        self.qty = 0
        self.note = None
        if self.range <= 0 or self.range < today_open * cfg.min_range_pct / 100:
            self.state, self.note = DONE, "yesterday's range %s is too small" % self.range

    @property
    def stop_price(self):
        return self.entry * (1 - self.cfg.stop_loss_pct / 100) if self.entry else None

    @property
    def trail_price(self):
        if not self.peak or not self.cfg.trailing_stop_pct:
            return None
        return self.peak * (1 - self.cfg.trailing_stop_pct / 100)

    def on_price(self, hhmm, price):
        """hhmm 'HH:MM' KST, price -> Signal or None."""
        c = self.cfg
        if self.state == WAIT:
            if hhmm >= c.entry_end or hhmm >= c.time_cut:
                self.state, self.note = DONE, "no breakout by %s" % c.entry_end
                return None
            if hhmm < c.entry_start or price < self.target:
                return None
            if self.upper_limit and price >= self.upper_limit:
                self.state, self.note = DONE, "at the upper limit, not chasing"
                return None
            self.state = BUYING
            return Signal("buy", "breakout: %s >= target %s" % (price, self.target), price)
        if self.state == HOLD:
            self.peak = max(self.peak, price)
            if hhmm >= c.time_cut:
                return self._sell("time cut %s" % c.time_cut, price)
            if price <= self.stop_price:
                return self._sell("stop loss -%s%% (%.0f)" % (c.stop_loss_pct, self.stop_price), price)
            tp = self.trail_price
            if tp is not None and self.peak > self.entry and price <= tp:
                return self._sell("trailing stop -%s%% from high %s" % (c.trailing_stop_pct, self.peak), price)
        return None

    def _sell(self, reason, price):
        self.state = SELLING
        return Signal("sell", reason, price)

    def filled(self, side, price, qty):
        if side == "buy":
            self.state, self.entry, self.peak, self.qty = HOLD, price, price, qty
        else:
            self.state, self.qty = DONE, 0

    def failed(self, side, why):
        """An order did not go through: buy -> give up today, sell -> keep holding and retry."""
        if side == "buy":
            self.state, self.note = DONE, "buy failed: %s" % why
        else:
            self.state = HOLD
