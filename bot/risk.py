"""Limits checked before every new entry. Getting out is never blocked."""
import os
from dataclasses import dataclass


@dataclass
class RiskConfig:
    budget_per_trade: int = 1_000_000     # KRW per entry
    max_positions: int = 3
    max_trades_per_day: int = 5
    daily_max_loss: int = 50_000          # KRW realized; no new entries once lost
    kill_switch_file: str = "STOP"        # create this file to stop new entries and flatten


class Risk:
    def __init__(self, cfg):
        self.cfg = cfg
        self.realized = 0
        self.trades = 0
        self.open = set()

    def killed(self):
        return bool(self.cfg.kill_switch_file) and os.path.exists(self.cfg.kill_switch_file)

    def check_entry(self, code, price):
        """-> (qty, None) or (0, reason)."""
        c = self.cfg
        if self.killed():
            return 0, "kill switch file %s exists" % c.kill_switch_file
        if self.realized <= -c.daily_max_loss:
            return 0, "daily loss limit hit (%s KRW)" % self.realized
        if self.trades >= c.max_trades_per_day:
            return 0, "max %d trades a day" % c.max_trades_per_day
        if len(self.open) >= c.max_positions:
            return 0, "max %d positions" % c.max_positions
        qty = int(c.budget_per_trade // price)
        if qty < 1:
            return 0, "budget %s is under one share at %s" % (c.budget_per_trade, price)
        return qty, None

    def opened(self, code):
        self.open.add(code)
        self.trades += 1

    def closed(self, code, pnl):
        self.open.discard(code)
        self.realized += pnl
