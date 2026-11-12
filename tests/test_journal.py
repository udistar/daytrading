from datetime import date, datetime

from daytrading.journal import Journal
from daytrading.models import Fill, OrderRecord, Snapshot, SymbolMeta
from daytrading.timeutil import KST


def _snap(code="005930", price=10_000):
    moment = datetime(2026, 11, 11, 10, 0, tzinfo=KST)
    meta = SymbolMeta(code, code, price, price, price + 100, date(2020, 1, 2))
    return Snapshot(
        code=code,
        name=code,
        ts=moment,
        price=price,
        prev_price=price,
        prev_close=price,
        day_open=price,
        day_high=price,
        acc_value=0,
        minute_value=0,
        prev_minute_value=None,
        avg_minute_value=None,
        minute_samples=0,
        strength=None,
        best_bid=price,
        best_ask=price,
        bid_qty=1,
        ask_qty=1,
        upper_limit=price + 100,
        vi_active=False,
        vi_price=0,
        vi_released_at=None,
        breakout_high=None,
        completed_bars=(),
        meta=meta,
        session_high_before=price,
    )


def test_trade_rows_are_on_disk_before_close(tmp_path):
    journal = Journal(tmp_path)
    header = (tmp_path / "signals.csv").read_text(encoding="utf-8-sig")
    assert header.startswith("ts,")
    moment = datetime(2026, 11, 11, 10, 0, tzinfo=KST)
    journal.signal(moment, "036540", "entry", "지정가")
    journal.order(
        OrderRecord("1", moment, "036540", "SFA", "buy", "entry", 1, "limit_ioc", 5000, "rejected", "RC4001")
    )
    journal.fill(Fill("1", moment, "036540", "SFA", "buy", "entry", 1, 5000, 18, 0))
    journal.reject(moment, "036540", "buy", "주문 거절", "RC4001")
    signals = (tmp_path / "signals.csv").read_text(encoding="utf-8-sig")
    orders = (tmp_path / "orders.csv").read_text(encoding="utf-8-sig")
    fills = (tmp_path / "fills.csv").read_text(encoding="utf-8-sig")
    rejections = (tmp_path / "rejections.csv").read_text(encoding="utf-8-sig")
    assert "036540" in signals
    assert "RC4001" in orders
    assert "5000" in fills
    assert "주문 거절" in rejections
    journal.close()


def test_orderbook_flushes_on_a_timer_not_every_row(tmp_path):
    journal = Journal(tmp_path)
    journal.orderbook(_snap("005930", 10_000))
    first = (tmp_path / "orderbook.csv").read_text(encoding="utf-8-sig")
    assert "005930" in first
    journal.orderbook(_snap("000660", 20_000))
    second = (tmp_path / "orderbook.csv").read_text(encoding="utf-8-sig")
    assert "000660" not in second
    journal._book_flushed_at = 0.0
    journal.orderbook(_snap("035720", 30_000))
    third = (tmp_path / "orderbook.csv").read_text(encoding="utf-8-sig")
    assert "000660" in third
    assert "035720" in third
    journal.close()
