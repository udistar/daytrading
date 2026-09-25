from datetime import date, datetime

from daytrading.market import Market
from daytrading.models import SymbolMeta
from daytrading.settings import default_settings
from daytrading.timeutil import KST


def test_first_print_is_baseline_and_later_minutes_use_the_delta():
    meta = SymbolMeta(
        code="005930",
        name="삼성전자",
        prev_close=10_000,
        day_open=10_100,
        upper_limit=13_000,
        listed_on=date(2020, 1, 2),
    )
    market = Market()
    first = datetime(2026, 9, 25, 8, 50, tzinfo=KST)
    market.update(
        meta,
        ts=first,
        price=10_200,
        acc_value=5_000_000_000,
        strength=140,
        best_bid=10_190,
        best_ask=10_210,
        bid_qty=100,
        ask_qty=100,
        vi_active=False,
        vi_price=0,
    )
    snap = market.books["005930"].snapshot(first, default_settings())
    assert snap.minute_value == 0
    second = datetime(2026, 9, 25, 8, 51, tzinfo=KST)
    market.update(
        meta,
        ts=second,
        price=10_200,
        acc_value=5_400_000_000,
        strength=140,
        best_bid=10_190,
        best_ask=10_210,
        bid_qty=100,
        ask_qty=100,
        vi_active=False,
        vi_price=0,
    )
    snap = market.books["005930"].snapshot(second, default_settings())
    assert snap.minute_value == 400_000_000
