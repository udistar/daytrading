import datetime
import json
import os
import random
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import main  # noqa: E402
from bot import backtest, krx  # noqa: E402
from bot.broker import BrokerError, Costs, KiwoomBroker, PaperBroker  # noqa: E402
from bot.engine import Engine, Journal  # noqa: E402
from bot.feed import ReplayFeed, parse_minute_rows  # noqa: E402
from bot.kiwoom import KiwoomClient  # noqa: E402
from bot.risk import Risk, RiskConfig  # noqa: E402
from bot.strategy import Breakout, StrategyConfig, DONE, HOLD, WAIT  # noqa: E402

D = datetime.date(2026, 11, 12)


def at(hhmm, day=D):
    return datetime.datetime.combine(day, datetime.datetime.strptime(hhmm, "%H:%M").time())


class StrategyRules(unittest.TestCase):
    def mk(self, **kw):
        return Breakout("005930", StrategyConfig(**kw), prev_high=82000, prev_low=78000, today_open=80000)

    def test_target(self):
        s = self.mk()
        self.assertEqual(s.target, 82000)                  # 80000 + 0.5 x 4000
        s2 = Breakout("X", StrategyConfig(k=0.37), 10250, 9900, 10000)
        self.assertEqual(s2.target, 10130)                 # 10129.5 rounded up to a 10-won tick

    def test_entry_window(self):
        s = self.mk()
        self.assertIsNone(s.on_price("08:59", 83000))
        self.assertIsNone(s.on_price("09:10", 81900))
        sig = s.on_price("09:11", 82000)
        self.assertEqual(sig.side, "buy")
        s = self.mk()
        self.assertIsNone(s.on_price("10:00", 85000))
        self.assertEqual(s.state, DONE)

    def test_stop_loss(self):
        s = self.mk()
        s.on_price("09:05", 82000)
        s.filled("buy", 82100, 10)
        self.assertIsNone(s.on_price("09:20", 80500))
        sig = s.on_price("09:21", 80400)                   # 82100 x 0.98 = 80458
        self.assertEqual(sig.side, "sell")
        self.assertIn("stop loss", sig.reason)

    def test_trailing_needs_a_new_high(self):
        s = self.mk(stop_loss_pct=5)
        s.on_price("09:05", 82000)
        s.filled("buy", 82000, 10)
        self.assertIsNone(s.on_price("09:06", 79600))      # 3% under the entry, but no new high yet
        self.assertIsNone(s.on_price("09:30", 86000))
        sig = s.on_price("09:31", 83400)                   # 86000 x 0.97 = 83420
        self.assertIn("trailing", sig.reason)

    def test_time_cut(self):
        s = self.mk()
        s.on_price("09:05", 82000)
        s.filled("buy", 82000, 10)
        self.assertIsNone(s.on_price("15:17", 82500))
        self.assertIn("time cut", s.on_price("15:18", 82500).reason)

    def test_skips(self):
        s = Breakout("X", StrategyConfig(), 80100, 80000, 80000)
        self.assertEqual(s.state, DONE)                    # range 0.125% < 0.5%
        s = Breakout("X", StrategyConfig(), 82000, 78000, 80000, upper_limit=82000)
        self.assertIsNone(s.on_price("09:05", 82000))
        self.assertEqual(s.state, DONE)

    def test_failed_sell_keeps_holding(self):
        s = self.mk()
        s.on_price("09:05", 82000)
        s.filled("buy", 82000, 10)
        s.on_price("15:18", 82000)
        s.failed("sell", "timeout")
        self.assertEqual(s.state, HOLD)
        self.assertEqual(s.on_price("15:19", 82000).side, "sell")


class RiskRules(unittest.TestCase):
    def test_limits(self):
        with tempfile.TemporaryDirectory() as d:
            stop = os.path.join(d, "STOP")
            r = Risk(RiskConfig(budget_per_trade=1_000_000, max_positions=1, daily_max_loss=10000,
                                kill_switch_file=stop))
            self.assertEqual(r.check_entry("A", 82000), (12, None))
            r.opened("A")
            self.assertIn("max 1 positions", r.check_entry("B", 1000)[1])
            r.closed("A", -10000)
            self.assertIn("daily loss", r.check_entry("B", 1000)[1])
            r.realized = 0
            open(stop, "w").close()
            self.assertIn("kill switch", r.check_entry("B", 1000)[1])
            self.assertIn("under one share", Risk(RiskConfig(budget_per_trade=500, kill_switch_file="")).check_entry("C", 1000)[1])


class Paper(unittest.TestCase):
    def test_slippage_fees_tax(self):
        b = PaperBroker(cash=10_000_000, costs=Costs(0.00015, 0.002), slippage_ticks=1)
        f = b.buy("A", 10, 82000)
        self.assertEqual(f.price, 82100)
        self.assertEqual(f.fee, round(821000 * 0.00015))
        s = b.sell("A", 10, 83000)
        self.assertEqual(s.price, 82900)
        self.assertEqual(s.tax, round(829000 * 0.002))
        with self.assertRaises(BrokerError):
            b.sell("A", 1, 83000)

    def test_slippage_at_band_edge(self):
        b = PaperBroker(slippage_ticks=1)
        self.assertEqual(b._px("sell", 50000), 49950)
        self.assertEqual(b._px("buy", 49950), 50000)


class FakeFeed:
    def __init__(self, path, prev=(82000, 78000)):
        self.path, self.prev = path, prev

    def quote(self, code, now):
        hh = now.strftime("%H:%M")
        pts = [p for t, p in self.path if t <= hh]
        return {"price": pts[-1], "open": self.path[0][1], "upper": None} if pts else None

    def prev_day(self, code, today):
        return self.prev


class EngineDay(unittest.TestCase):
    def test_one_trade_with_journal(self):
        path = [("09:00", 80000), ("09:20", 82000), ("09:40", 84000), ("10:30", 86000), ("11:00", 83300)]
        with tempfile.TemporaryDirectory() as d:
            j = Journal(d, echo=None)
            eng = Engine(["005930"], StrategyConfig(), Risk(RiskConfig(kill_switch_file="")),
                         PaperBroker(slippage_ticks=0, costs=Costs(0, 0)), FakeFeed(path), j)
            t = at("09:00")
            while t <= at("15:30"):
                eng.step(t)
                t += datetime.timedelta(minutes=1)
            self.assertEqual(len(j.trades), 1)
            tr = j.trades[0]
            self.assertEqual((tr["entry"], tr["exit"], tr["entry_time"], tr["exit_time"]), (82000, 83300, "09:20", "11:00"))
            self.assertEqual(tr["qty"], 12)
            self.assertEqual(tr["pnl"], 12 * 1300)
            self.assertIn("trailing", tr["reason"])
            self.assertTrue(os.path.exists(os.path.join(d, "trades.csv")))

    def test_kill_switch_flattens(self):
        with tempfile.TemporaryDirectory() as d:
            stop = os.path.join(d, "STOP")
            eng = Engine(["A"], StrategyConfig(), Risk(RiskConfig(kill_switch_file=stop)),
                         PaperBroker(slippage_ticks=0), FakeFeed([("09:00", 80000), ("09:10", 82000)]), Journal(echo=None))
            eng.step(at("09:10"))
            self.assertEqual(eng.strats["A"].state, HOLD)
            open(stop, "w").close()
            eng.step(at("09:11"))
            self.assertEqual(eng.strats["A"].state, DONE)
            self.assertEqual(eng.j.trades[0]["reason"], "kill switch")


class FakeClient:
    def __init__(self, fills=True):
        self.qty, self.fills, self.orders = 0, fills, []

    def holdings(self):
        return {"005930": {"qty": self.qty, "avg_price": 82100}} if self.qty else {}

    def order(self, side, code, qty):
        self.orders.append((side, code, qty))
        if self.fills:
            self.qty += qty if side == "buy" else -qty
        return "0001"

    def quote(self, code):
        return {"price": 83000}


class KiwoomOrders(unittest.TestCase):
    def test_confirmed_by_balance(self):
        c = FakeClient()
        b = KiwoomBroker(c, sleep=lambda s: None)
        f = b.buy("005930", 10, 82000)
        self.assertEqual((f.qty, f.price), (10, 82100))
        s = b.sell("005930", 10, 83000)
        self.assertEqual((s.qty, s.price), (10, 83000))

    def test_unconfirmed_is_an_error(self):
        b = KiwoomBroker(FakeClient(fills=False), sleep=lambda s: None)
        with self.assertRaises(BrokerError):
            b.buy("005930", 10, 82000)

    def test_request_shapes(self):
        sent = []

        class R:
            def __init__(self, doc):
                self.b, self.headers = json.dumps(doc).encode(), {}

            def read(self):
                return self.b

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def opener(req, timeout=15):
            sent.append((req.full_url, {k.lower(): v for k, v in req.header_items()}, json.loads(req.data)))
            if "ordr" in req.full_url:
                return R({"ord_no": "0000123", "return_code": 0})
            if "acnt" in req.full_url:
                return R({"acnt_evlt_remn_indv_tot": [{"stk_cd": "A005930", "rmnd_qty": "000000000012",
                                                         "pur_pric": "000000082100"}], "return_code": 0})
            return R({"stk_nm": "삼성전자", "cur_prc": "-82000", "open_pric": "+80000", "base_pric": "81000",
                      "upl_pric": "+105300", "lst_pric": "-56700", "return_code": 0})
        c = KiwoomClient(token="T", opener=opener, sleep=lambda s: None, pause=0)
        self.assertEqual(c.order("buy", "005930", 12), "0000123")
        url, h, body = sent[-1]
        self.assertTrue(url.endswith("/api/dostk/ordr"))
        self.assertEqual(h["api-id"], "kt10000")
        self.assertEqual(body["trde_tp"], "3")
        self.assertEqual(body["ord_qty"], "12")
        c.order("sell", "005930", 12)
        self.assertEqual(sent[-1][1]["api-id"], "kt10001")
        self.assertEqual(c.holdings(), {"005930": {"qty": 12, "avg_price": 82100}})
        q = c.quote("005930")
        self.assertEqual((q["price"], q["open"], q["upper"], q["lower"]), (82000, 80000, 105300, 56700))
        self.assertTrue(sent[0][0].startswith("https://mockapi.kiwoom.com"))


def synth_rows(days=15, seed=5, price=60000):
    rnd = random.Random(seed)
    rows, d = [], datetime.date(2026, 10, 19)
    for _ in range(days):
        while d.weekday() >= 5:
            d += datetime.timedelta(days=1)
        x = price * (1 + rnd.uniform(-0.01, 0.01))
        p = krx.round_down_tick(x + krx.tick_size(x) / 2.0)
        trend = rnd.choice([0.0012, -0.0006, 0.0])
        for i in range(76):
            o = p
            x = max(1000, o * (1 + trend * (i < 20) + rnd.gauss(0, 0.002)))
            c = krx.round_down_tick(x + krx.tick_size(x) / 2.0)        # nearest tick, no downward drift
            h, lo = max(o, c) + krx.tick_size(c), min(o, c) - krx.tick_size(c)
            t = datetime.datetime.combine(d, datetime.time(9)) + datetime.timedelta(minutes=5 * i)
            rows.append({"cntr_tm": t.strftime("%Y%m%d%H%M%S"), "cur_prc": "+%d" % c, "open_pric": str(o),
                         "high_pric": str(h), "low_pric": str(lo), "trde_qty": "1000"})
            p = c
        price = p
        d += datetime.timedelta(days=1)
    return rows


class Backtest(unittest.TestCase):
    def test_runs_and_is_repeatable(self):
        sessions = {"005930": parse_minute_rows(synth_rows())}
        r1 = backtest.run(ReplayFeed(sessions), ["005930"], StrategyConfig(), RiskConfig(kill_switch_file=""))
        r2 = backtest.run(ReplayFeed(sessions), ["005930"], StrategyConfig(), RiskConfig(kill_switch_file=""))
        self.assertEqual(r1["days"], 14)
        self.assertGreater(r1["trades"], 0)
        self.assertEqual(r1["total_pnl"], r2["total_pnl"])
        for t in r1["list"]:
            self.assertLessEqual(t["entry_time"], "10:00")
            self.assertLessEqual(t["exit_time"], "15:18")

    def test_replay_path_order(self):
        rows = [{"cntr_tm": "20261112090000", "cur_prc": "110", "open_pric": "100", "high_pric": "120",
                 "low_pric": "90", "trde_qty": "1"}]
        f = ReplayFeed({"X": parse_minute_rows(rows)})
        self.assertEqual([f.quote("X", at(t))["price"] for t in ("09:00", "09:01", "09:03", "09:04")],
                         [100, 90, 120, 110])


class CutOffDay(unittest.TestCase):
    def test_oldest_partial_day_dropped(self):
        rows = synth_rows(days=3)
        oldest = min(r["cntr_tm"][:8] for r in rows)
        rows = [r for r in rows if not (r["cntr_tm"][:8] == oldest and r["cntr_tm"][8:12] < "1350")]
        s = parse_minute_rows(rows)
        self.assertEqual(len(s), 2)

    def test_after_hours_bars_ignored(self):
        rows = synth_rows(days=2)
        day = max(r["cntr_tm"][:8] for r in rows)
        rows.append({"cntr_tm": day + "160000", "cur_prc": "99999", "open_pric": "99999", "high_pric": "99999",
                     "low_pric": "99999", "trde_qty": "5"})
        s = parse_minute_rows(rows)
        self.assertTrue(all(b["m"] <= 15 * 60 + 30 for bars in s.values() for b in bars))


class RealMoneyLock(unittest.TestCase):
    def test_refuses_without_both_switches(self):
        self.assertEqual(main.main(["run", "--codes", "005930", "--mode", "real", "--config", "nope.json"]), 2)
        self.assertEqual(main.main(["run", "--codes", "005930", "--mode", "real", "--real-money",
                                    "--config", "nope.json"]), 2)


if __name__ == "__main__":
    unittest.main()
