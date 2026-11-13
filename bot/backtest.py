"""Replay saved 5-minute bars through the same engine, a day at a time."""
import datetime

from .broker import PaperBroker
from .engine import Engine, Journal
from .risk import Risk
from .strategy import HOLD


def run(feed, codes, strat_cfg, risk_cfg, costs=None, slippage_ticks=1, echo=None):
    j = Journal(echo=echo)
    days = sorted({d for c in codes for d in feed.days(c)})
    tested = 0
    for day in days:
        if not any(d < day for c in codes for d in feed.days(c)) and not any(feed.daily.get(c) for c in codes):
            continue                      # the first day has no previous day to measure the range
        eng = Engine(codes, strat_cfg, Risk(risk_cfg), PaperBroker(cash=10 ** 12, costs=costs,
                                                                   slippage_ticks=slippage_ticks), feed, j)
        t = datetime.datetime.combine(day, datetime.time(9, 0))
        end = datetime.datetime.combine(day, datetime.time(15, 30))
        while t <= end:
            eng.step(t)
            t += datetime.timedelta(minutes=1)
        for code, st in eng.strats.items():          # a session that ended early: close at the last price
            if st.state == HOLD:
                q = feed.quote(code, end)
                eng._sell(code, st, end, q["price"], "end of data")
        tested += 1
    return summarize(j.trades, tested)


def summarize(trades, days):
    n = len(trades)
    pnl = [t["pnl"] for t in trades]
    wins = [p for p in pnl if p > 0]
    cum = peak = mdd = 0
    for p in pnl:
        cum += p
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    reasons = {}
    for t in trades:
        key = t["reason"].split(" ")[0] + (" " + t["reason"].split(" ")[1] if t["reason"].startswith(("stop", "time", "trailing")) else "")
        reasons[key] = reasons.get(key, 0) + 1
    return {"days": days, "trades": n, "win_rate": round(len(wins) / n * 100, 1) if n else 0.0,
            "total_pnl": sum(pnl), "avg_pnl": round(sum(pnl) / n) if n else 0,
            "avg_pnl_pct": round(sum(t["pnl_pct"] for t in trades) / n, 3) if n else 0.0,
            "max_drawdown": mdd, "best": max(pnl) if pnl else 0, "worst": min(pnl) if pnl else 0,
            "exits": reasons, "list": trades}
