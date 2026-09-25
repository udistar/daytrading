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


def _meta():
    return SymbolMeta(
        code="005930",
        name="삼성전자",
        prev_close=10_000,
        day_open=10_100,
        upper_limit=13_000,
        listed_on=date(2020, 1, 2),
    )


def _print(market, meta, moment, price, acc, vi_active=False):
    market.update(
        meta,
        ts=moment,
        price=price,
        acc_value=acc,
        strength=140,
        best_bid=price - 10,
        best_ask=price + 10,
        bid_qty=100,
        ask_qty=100,
        vi_active=vi_active,
        vi_price=10_700 if vi_active else 0,
    )


def test_heartbeat_is_not_a_new_high_and_a_higher_tick_is():
    market = Market()
    meta = _meta()
    first = datetime(2026, 9, 25, 9, 6, tzinfo=KST)
    _print(market, meta, first, 10_550, 5_000_000_000)
    book = market.books["005930"]
    live = book.snapshot(first, default_settings())
    assert live.session_high_before < live.price
    book.dirty = False
    quiet = book.snapshot(first, default_settings())
    assert quiet.session_high_before >= quiet.price
    later = datetime(2026, 9, 25, 9, 7, tzinfo=KST)
    _print(market, meta, later, 10_800, 5_400_000_000)
    higher = book.snapshot(later, default_settings())
    assert higher.session_high_before == 10_550
    assert higher.price > higher.session_high_before


def test_each_vi_release_records_its_own_time():
    market = Market()
    meta = _meta()
    base = datetime(2026, 9, 25, 9, 6, tzinfo=KST)
    _print(market, meta, base, 10_500, 1, vi_active=True)
    book = market.books["005930"]
    first_release = datetime(2026, 9, 25, 9, 7, tzinfo=KST)
    _print(market, meta, first_release, 10_500, 2, vi_active=False)
    assert book.vi_released_at == first_release
    _print(market, meta, datetime(2026, 9, 25, 9, 8, tzinfo=KST), 10_500, 3, vi_active=True)
    assert book.vi_released_at is None
    second_release = datetime(2026, 9, 25, 9, 9, tzinfo=KST)
    _print(market, meta, second_release, 10_500, 4, vi_active=False)
    assert book.vi_released_at == second_release


def test_minute_value_uses_the_trailing_minute_not_only_the_forming_bar():
    market = Market()
    meta = _meta()
    _print(market, meta, datetime(2026, 9, 25, 9, 4, tzinfo=KST), 10_200, 5_000_000_000)
    _print(market, meta, datetime(2026, 9, 25, 9, 5, 50, tzinfo=KST), 10_200, 5_400_000_000)
    forming = datetime(2026, 9, 25, 9, 6, 5, tzinfo=KST)
    _print(market, meta, forming, 10_210, 5_401_000_000)
    book = market.books["005930"]
    snap = book.snapshot(forming, default_settings())
    assert book.forming.value == 1_000_000
    assert snap.minute_value == 401_000_000


def test_completed_bar_window_follows_settings():
    from daytrading.settings import validate_settings

    market = Market()
    meta = _meta()
    start = datetime(2026, 9, 25, 9, 0, tzinfo=KST)
    for offset in range(8):
        moment = start.replace(minute=offset)
        _print(market, meta, moment, 10_200 + offset * 10, 1_000_000 * (offset + 1))
    settings = validate_settings(
        {
            **default_settings().to_dict(),
            "minute_value_lookback_min": 3,
            "breakout_lookback_min": 3,
            "add_down_bars": 1,
            "strength_window_minutes": 1,
        }
    )
    snap = market.books["005930"].snapshot(start.replace(minute=7), settings)
    assert len(snap.completed_bars) == 4
