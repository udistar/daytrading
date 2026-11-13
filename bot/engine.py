"""One loop for live runs and backtests: quote -> strategy -> risk -> broker -> journal."""
import csv
import json
import os

from .broker import BrokerError
from .strategy import Breakout, BUYING, HOLD, SELLING


class Journal:
    """trades.csv (one row per closed trade) and events.jsonl (everything), under `folder`."""

    FIELDS = ["date", "code", "qty", "entry_time", "entry", "exit_time", "exit", "reason", "fees", "tax", "pnl",
              "pnl_pct"]

    def __init__(self, folder=None, echo=print):
        self.folder, self.echo = folder, echo
        self.trades = []
        if folder:
            os.makedirs(folder, exist_ok=True)

    def event(self, kind, **kw):
        if self.folder:
            with open(os.path.join(self.folder, "events.jsonl"), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(dict(kind=kind, **kw), ensure_ascii=False, default=str) + "\n")

    def say(self, now, text):
        line = "%s %s" % (now.strftime("%Y-%m-%d %H:%M:%S"), text)
        if self.echo:
            self.echo(line)
        self.event("say", text=line)

    def trade(self, row):
        self.trades.append(row)
        if self.folder:
            path = os.path.join(self.folder, "trades.csv")
            new = not os.path.exists(path)
            with open(path, "a", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=self.FIELDS)
                if new:
                    w.writeheader()
                w.writerow(row)


class Engine:
    def __init__(self, codes, strat_cfg, risk, broker, feed, journal=None):
        self.codes, self.cfg, self.risk, self.broker, self.feed = codes, strat_cfg, risk, broker, feed
        self.j = journal or Journal(echo=None)
        self.strats = {}
        self.buys = {}

    def busy(self):
        return any(s.state in (BUYING, HOLD, SELLING) for s in self.strats.values())

    def step(self, now):
        for code in self.codes:
            try:
                q = self.feed.quote(code, now)
            except Exception as e:          # a bad quote must not kill the loop: log it and move on
                self.j.say(now, "%s quote failed: %s" % (code, e))
                continue
            if q:
                self.on_quote(code, now, q)

    def on_quote(self, code, now, q):
        hhmm = now.strftime("%H:%M")
        st = self.strats.get(code)
        if st is None:
            if hhmm < self.cfg.entry_start or not q.get("open"):
                return
            try:
                ph, pl = self.feed.prev_day(code, now.date())
            except Exception as e:
                self.j.say(now, "%s no previous day: %s" % (code, e))
                return
            st = self.strats[code] = Breakout(code, self.cfg, ph, pl, q["open"], q.get("upper"))
            self.j.say(now, "%s open %s, prev range %s-%s, target %s%s" % (
                code, q["open"], pl, ph, st.target, "" if not st.note else " (skip: %s)" % st.note))
        price = q["price"]
        if st.state == HOLD and self.risk.killed():
            st.state = SELLING
            self._sell(code, st, now, price, "kill switch")
            return
        sig = st.on_price(hhmm, price)
        if sig is None:
            return
        if sig.side == "buy":
            qty, why = self.risk.check_entry(code, price)
            if not qty:
                st.failed("buy", why)
                self.j.say(now, "%s breakout at %s but no entry: %s" % (code, price, why))
                return
            try:
                f = self.broker.buy(code, qty, price)
            except BrokerError as e:
                st.failed("buy", str(e))
                self.j.say(now, "%s BUY FAILED: %s" % (code, e))
                return
            st.filled("buy", f.price, f.qty)
            self.risk.opened(code)
            self.buys[code] = (now, f)
            self.j.say(now, "%s BUY %d @ %s (%s)" % (code, f.qty, f.price, sig.reason))
            self.j.event("fill", side="buy", code=code, qty=f.qty, price=f.price, reason=sig.reason)
        else:
            self._sell(code, st, now, price, sig.reason)

    def _sell(self, code, st, now, price, reason):
        try:
            f = self.broker.sell(code, st.qty, price)
        except BrokerError as e:
            st.failed("sell", str(e))
            self.j.say(now, "%s SELL FAILED (will retry): %s" % (code, e))
            return
        t0, b = self.buys.pop(code)
        pnl = (f.price - b.price) * f.qty - b.fee - f.fee - f.tax
        st.filled("sell", f.price, f.qty)
        self.risk.closed(code, pnl)
        self.j.say(now, "%s SELL %d @ %s (%s) pnl %+d" % (code, f.qty, f.price, reason, pnl))
        self.j.event("fill", side="sell", code=code, qty=f.qty, price=f.price, reason=reason, pnl=pnl)
        self.j.trade({"date": now.date().isoformat(), "code": code, "qty": f.qty, "entry_time": t0.strftime("%H:%M"),
                      "entry": b.price, "exit_time": now.strftime("%H:%M"), "exit": f.price, "reason": reason,
                      "fees": b.fee + f.fee, "tax": f.tax, "pnl": round(pnl),
                      "pnl_pct": round(pnl / (b.price * f.qty) * 100, 3)})
