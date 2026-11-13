"""Where orders go. PaperBroker fills locally; KiwoomBroker sends market orders
to the Kiwoom server (mock or real) and confirms them against the balance."""
import time
from dataclasses import dataclass

from . import krx


class BrokerError(Exception):
    pass


@dataclass
class Fill:
    side: str
    code: str
    qty: int
    price: float
    fee: float
    tax: float


@dataclass
class Costs:
    fee_rate: float = 0.00015    # broker commission each side (check your account's rate)
    tax_rate: float = 0.0020     # securities transaction tax on sells (check the current year's rate)

    def of(self, side, qty, price):
        amt = qty * price
        return round(amt * self.fee_rate), (round(amt * self.tax_rate) if side == "sell" else 0)


class PaperBroker:
    """Fills every market order at the signal price, `slippage_ticks` KRX ticks worse."""

    def __init__(self, cash=10_000_000, costs=None, slippage_ticks=1):
        self.cash = cash
        self.costs = costs or Costs()
        self.slip = slippage_ticks
        self.positions = {}

    def _px(self, side, price):
        p = krx.round_up_tick(price) if side == "buy" else krx.round_down_tick(price)
        for _ in range(self.slip):
            p = p + krx.tick_size(p) if side == "buy" else p - krx.tick_size(p - 1)
        return p

    def buy(self, code, qty, price):
        px = self._px("buy", price)
        fee, tax = self.costs.of("buy", qty, px)
        if qty * px + fee > self.cash:
            raise BrokerError("paper cash %s is short of %s" % (self.cash, qty * px + fee))
        self.cash -= qty * px + fee
        self.positions[code] = self.positions.get(code, 0) + qty
        return Fill("buy", code, qty, px, fee, tax)

    def sell(self, code, qty, price):
        if self.positions.get(code, 0) < qty:
            raise BrokerError("paper position %s has %s, not %s" % (code, self.positions.get(code, 0), qty))
        px = self._px("sell", price)
        fee, tax = self.costs.of("sell", qty, px)
        self.cash += qty * px - fee - tax
        self.positions[code] -= qty
        return Fill("sell", code, qty, px, fee, tax)


class KiwoomBroker:
    """Market orders through the REST API. A fill is believed only when the balance shows it."""

    def __init__(self, client, costs=None, confirm_tries=6, wait=1.0, sleep=time.sleep):
        self.client = client
        self.costs = costs or Costs()
        self.tries, self.wait, self._sleep = confirm_tries, wait, sleep

    def _qty(self, code):
        h = self.client.holdings().get(code)
        return (h["qty"], h.get("avg_price")) if h else (0, None)

    def _confirm(self, code, want):
        got = None
        for _ in range(self.tries):
            self._sleep(self.wait)
            q, avg = self._qty(code)
            got = (q, avg)
            if want(q):
                return q, avg
        return got

    def buy(self, code, qty, price):
        before, _ = self._qty(code)
        ord_no = self.client.order("buy", code, qty)
        q, avg = self._confirm(code, lambda q: q >= before + qty)
        done = q - before
        if done <= 0:
            raise BrokerError("buy %s x%d (order %s) not in the balance after %ds: check HTS/MTS"
                              % (code, qty, ord_no, self.tries * self.wait))
        px = avg or price
        fee, tax = self.costs.of("buy", done, px)
        return Fill("buy", code, done, px, fee, tax)

    def sell(self, code, qty, price):
        before, _ = self._qty(code)
        if before < qty:
            qty = before
        if qty <= 0:
            raise BrokerError("no %s in the balance to sell" % code)
        ord_no = self.client.order("sell", code, qty)
        q, _ = self._confirm(code, lambda q: q <= before - qty)
        done = before - q
        if done <= 0:
            raise BrokerError("sell %s x%d (order %s) not reflected in the balance: check HTS/MTS"
                              % (code, qty, ord_no))
        px = self.client.quote(code)["price"]      # market fill: the last price is the best estimate here
        fee, tax = self.costs.of("sell", done, px)
        return Fill("sell", code, done, px, fee, tax)
