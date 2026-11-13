#!/usr/bin/env python3
"""키움 REST 변동성 돌파 데이트레이딩 봇 (모의투자 먼저).

  python main.py check                                  # 연결/응답 형식 점검 (주문 없음)
  python main.py backtest --fetch 005930,000660 --days 20
  python main.py backtest --minute data/raw/005930-minute.json --code 005930
  python main.py run --codes 005930,000660 --mode paper # 실시세 + 가상 체결
  python main.py run --codes 005930 --mode mock         # 키움 모의투자 서버로 실제 주문
Keys: KIWOOM_APPKEY / KIWOOM_SECRETKEY in the environment. Create a file named STOP to halt.
"""
import argparse
import dataclasses
import datetime
import json
import os
import sys
import time

from bot import backtest
from bot.broker import Costs, KiwoomBroker, PaperBroker
from bot.engine import Engine, Journal
from bot.feed import KiwoomFeed, ReplayFeed, parse_minute_rows
from bot.kiwoom import KiwoomClient, KiwoomError, kst_now
from bot.risk import Risk, RiskConfig
from bot.strategy import StrategyConfig

DATA = "data"


def load_config(path):
    cfg = {}
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    s = StrategyConfig(**cfg.get("strategy", {}))
    r = RiskConfig(**cfg.get("risk", {}))
    c = Costs(**cfg.get("costs", {}))
    return s, r, c, bool(cfg.get("allow_real", False))


def apply_overrides(s, r, a):
    for attr, val in (("k", a.k), ("stop_loss_pct", a.stop), ("trailing_stop_pct", a.trail),
                      ("entry_end", a.entry_end), ("time_cut", a.time_cut)):
        if val is not None:
            setattr(s, attr, val)
    if a.budget is not None:
        r.budget_per_trade = a.budget


def client_for(mode, journal=None):
    return KiwoomClient.from_env("real" if mode == "real" else "mock",
                                 token_cache=os.path.join(DATA, ".kiwoom_token_%s.json" % mode),
                                 log=journal.event if journal else None)


def fmt(n):
    return "{:,}".format(n)


# ---------------------------------------------------------------- check

def cmd_check(a):
    try:
        cli = client_for(a.mode)
        cli.token()
        print("token: ok (%s server)" % cli.host)
        q = cli.quote(a.code)
        print("quote %s %s: price %s, open %s, high %s, low %s, prev close %s, limits %s~%s" % (
            a.code, q.get("name") or "", fmt(q["price"]), q["open"], q["high"], q["low"], q["prev_close"],
            q["lower"], q["upper"]))
        d = cli.daily(a.code, days=3)
        print("daily: " + ", ".join("%s O%s H%s L%s C%s" % (r["date"], r["open"], r["high"], r["low"], r["close"])
                                    for r in d))
        h = cli.holdings()
        print("holdings: %s" % (", ".join("%s x%s @%s" % (k, v["qty"], v["avg_price"]) for k, v in h.items())
                                or "none"))
    except KiwoomError as e:
        print("check failed: %s" % e, file=sys.stderr)
        return 1
    print("All four calls answered. No order was sent.")
    return 0


# ---------------------------------------------------------------- backtest

def cmd_backtest(a):
    s, r, c, _ = load_config(a.config)
    apply_overrides(s, r, a)
    sessions, daily = {}, {}
    if a.fetch:
        cli = client_for(a.mode)
        for code in [x.strip() for x in a.fetch.split(",") if x.strip()]:
            rows = cli.minute(code, days=a.days)
            os.makedirs(os.path.join(DATA, "raw"), exist_ok=True)
            with open(os.path.join(DATA, "raw", code + "-minute.json"), "w", encoding="utf-8") as fh:
                json.dump({"stk_min_pole_chart_qry": rows}, fh, ensure_ascii=False)
            sessions[code] = parse_minute_rows(rows)
            daily[code] = cli.daily(code, days=a.days + 5)
    else:
        with open(a.minute, encoding="utf-8") as fh:
            doc = json.load(fh)
        rows = doc.get("stk_min_pole_chart_qry", doc) if isinstance(doc, dict) else doc
        sessions[a.code] = parse_minute_rows(rows)
    codes = list(sessions)
    res = backtest.run(ReplayFeed(sessions, daily), codes, s, r, c, a.slippage, echo=print if a.verbose else None)
    print("Volatility breakout K=%s, stop -%s%%, trailing -%s%%, entries %s-%s, time cut %s, %s KRW a trade" % (
        s.k, s.stop_loss_pct, s.trailing_stop_pct, s.entry_start, s.entry_end, s.time_cut, fmt(r.budget_per_trade)))
    print("%d days, %d trades, win rate %s%%, total %s KRW, avg %s KRW (%s%%), worst %s, max drawdown %s" % (
        res["days"], res["trades"], res["win_rate"], fmt(res["total_pnl"]), fmt(res["avg_pnl"]), res["avg_pnl_pct"],
        fmt(res["worst"]), fmt(res["max_drawdown"])))
    if res["exits"]:
        print("exits: " + ", ".join("%s %d" % kv for kv in sorted(res["exits"].items(), key=lambda kv: -kv[1])))
    for t in res["list"][-a.show:]:
        print("  %s %s %s %s->%s %s->%s %+d (%s)" % (t["date"], t["code"], t["qty"], t["entry_time"], t["exit_time"],
                                                    fmt(t["entry"]), fmt(t["exit"]), t["pnl"], t["reason"]))
    print("Past days only, fills at the signal price +-%d tick. Not a promise about tomorrow." % a.slippage)
    return 0


# ---------------------------------------------------------------- run

def cmd_run(a):
    s, r, c, allow_real = load_config(a.config)
    apply_overrides(s, r, a)
    if a.mode == "real" and not (allow_real and a.real_money):
        print("Real-money mode needs \"allow_real\": true in the config AND --real-money. Refusing.", file=sys.stderr)
        return 2
    codes = [x.strip() for x in a.codes.split(",") if x.strip()]
    folder = os.path.join(DATA, "runs", kst_now().strftime("%Y%m%d") + "-" + a.mode)
    j = Journal(folder)
    try:
        cli = client_for(a.mode if a.mode != "paper" else "mock", j)
        cli.token()
    except KiwoomError as e:
        print("kiwoom: %s" % e, file=sys.stderr)
        return 1
    broker = PaperBroker(costs=c) if a.mode == "paper" else KiwoomBroker(cli, costs=c)
    eng = Engine(codes, s, Risk(r), broker, KiwoomFeed(cli), j)
    j.say(kst_now(), "start %s mode, %s, config %s" % (a.mode, ",".join(codes), json.dumps(
        {"strategy": dataclasses.asdict(s), "risk": dataclasses.asdict(r)}, ensure_ascii=False)))
    try:
        while True:
            now = kst_now()
            hhmm = now.strftime("%H:%M")
            if now.weekday() >= 5:
                j.say(now, "weekend, nothing to do")
                break
            if hhmm >= "15:30" or (hhmm >= s.time_cut and not eng.busy()):
                break
            if hhmm < s.entry_start:
                time.sleep(min(30, a.poll * 5))
                continue
            eng.step(now)
            if hhmm >= s.entry_end and not eng.busy() and all(st.state == "DONE" for st in eng.strats.values()) \
                    and len(eng.strats) == len(codes):
                j.say(now, "every stock is done for today")
                break
            time.sleep(a.poll)
    except KeyboardInterrupt:
        held = [code for code, st in eng.strats.items() if st.state in ("HOLD", "BUYING", "SELLING")]
        j.say(kst_now(), "stopped by hand" + (": STILL HOLDING %s, close them in HTS/MTS" % held if held else ""))
    t = j.trades
    j.say(kst_now(), "done: %d trades, realized %s KRW. Log: %s" % (len(t), fmt(sum(x["pnl"] for x in t)), folder))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def strat_flags(p):
        p.add_argument("--config", default="config.json")
        p.add_argument("--k", type=float)
        p.add_argument("--stop", type=float, help="stop loss %%")
        p.add_argument("--trail", type=float, help="trailing stop %% (0 = off)")
        p.add_argument("--entry-end", help="no new entries from HH:MM")
        p.add_argument("--time-cut", help="sell everything at HH:MM")
        p.add_argument("--budget", type=int, help="KRW per trade")

    c = sub.add_parser("check", help="token, quote, daily, holdings: no orders")
    c.add_argument("--mode", default="mock", choices=["mock", "real"])
    c.add_argument("--code", default="005930")
    b = sub.add_parser("backtest", help="replay 5-minute bars")
    b.add_argument("--fetch", help="codes to download from Kiwoom first, e.g. 005930,000660")
    b.add_argument("--mode", default="mock", choices=["mock", "real"])
    b.add_argument("--days", type=int, default=20)
    b.add_argument("--minute", help="a saved ka10080 answer")
    b.add_argument("--code", default="005930")
    b.add_argument("--slippage", type=int, default=1, help="ticks against you on each fill")
    b.add_argument("--show", type=int, default=10)
    b.add_argument("--verbose", action="store_true")
    strat_flags(b)
    r = sub.add_parser("run", help="trade today")
    r.add_argument("--codes", required=True)
    r.add_argument("--mode", default="paper", choices=["paper", "mock", "real"])
    r.add_argument("--poll", type=float, default=3.0, help="seconds between quote rounds")
    r.add_argument("--real-money", action="store_true")
    strat_flags(r)
    a = ap.parse_args(argv)
    if a.cmd == "backtest" and not a.fetch and not a.minute:
        ap.error("backtest needs --fetch CODES or --minute FILE")
    return {"check": cmd_check, "backtest": cmd_backtest, "run": cmd_run}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
