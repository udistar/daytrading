from dataclasses import replace
from datetime import date, datetime

from daytrading.models import Bar, Portfolio, Position, Snapshot, SymbolMeta
from daytrading.settings import default_settings, validate_settings
from daytrading.strategy import (
    add_block_reason,
    entry_block_reason,
    evaluate,
    maybe_exit,
    phase_name,
)
from daytrading.timeutil import KST


def ts(hour, minute, second=0):
    return datetime(2026, 9, 25, hour, minute, second, tzinfo=KST)


def meta(**kwargs):
    base = dict(
        code="005930",
        name="삼성전자",
        prev_close=10_000,
        day_open=10_100,
        upper_limit=13_000,
        listed_on=date(2020, 1, 2),
        flags_known=True,
    )
    base.update(kwargs)
    return SymbolMeta(**base)


def good_snap(**kwargs):
    base = dict(
        code="005930",
        name="삼성전자",
        ts=ts(9, 6, 0),
        price=10_550,
        prev_price=10_400,
        prev_close=10_000,
        day_open=10_100,
        day_high=10_550,
        acc_value=6_000_000_000,
        minute_value=400_000_000,
        prev_minute_value=400_000_000,
        avg_minute_value=100_000_000,
        minute_samples=10,
        strength=140.0,
        best_bid=10_540,
        best_ask=10_560,
        bid_qty=2_000,
        ask_qty=1_000,
        upper_limit=13_000,
        vi_active=False,
        vi_price=0,
        vi_released_at=None,
        breakout_high=10_450,
        completed_bars=(),
        meta=meta(),
        session_high_before=10_500,
    )
    base.update(kwargs)
    return Snapshot(**base)


def portfolio():
    return Portfolio(cash=50_000_000)


def position(**kwargs):
    base = dict(
        code="005930",
        name="삼성전자",
        qty=100,
        cost_krw=1_000_000,
        fill_notional=1_000_000,
        entry_price=10_000,
        last_add_price=10_000,
        buy_count=1,
        opened_at=ts(9, 6, 0),
        last_buy_at=ts(9, 6, 0),
        high_since_entry=10_000,
    )
    base.update(kwargs)
    return Position(**base)


def test_good_snapshot_enters():
    settings = default_settings()
    snap = good_snap()
    assert entry_block_reason(snap, settings, portfolio()) is None
    intents, rejected = evaluate(snap, portfolio(), settings)
    assert rejected == []
    assert intents[0].reason == "entry"
    assert intents[0].order_type == "limit_ioc"
    assert intents[0].limit_price == 10_560 + 10


def test_entry_filters_one_by_one():
    settings = default_settings()
    book = portfolio()
    cases = [
        (dict(ts=ts(9, 4, 0)), "신규 진입 시간 전"),
        (dict(ts=ts(9, 40, 0)), "신규 진입 마감"),
        (dict(price=1_000), "주가 범위 밖"),
        (dict(price=10_200, prev_close=10_000), "전일대비 범위 밖"),
        (dict(price=11_500), "전일대비 범위 밖"),
        (dict(price=10_500, day_open=10_400), "시가대비 부족"),
        (dict(day_open=10_600, price=11_000), "시가 갭 제외"),
        (dict(upper_limit=11_000, price=10_500), "상한가 근접"),
        (dict(acc_value=1_000_000_000), "누적 거래대금 부족"),
        (dict(minute_samples=3), "1분 거래대금 평균 표본 부족"),
        (dict(minute_value=100_000_000), "1분 거래대금 부족"),
        (dict(minute_value=250_000_000, avg_minute_value=100_000_000), "1분 거래대금 배수 부족"),
        (dict(strength=120), "체결강도 부족"),
        (dict(best_bid=10_000, best_ask=10_600), "스프레드 큼"),
        (dict(bid_qty=6_000, ask_qty=1_000), "매수/매도 잔량비 초과"),
        (dict(prev_price=10_600), "직전 고가 돌파 아님"),
    ]
    for updates, reason in cases:
        snap = good_snap(**updates)
        assert entry_block_reason(snap, settings, book) == reason, updates


def test_ratio_over_five_blocks_but_both_directions_pass():
    settings = default_settings()
    book = portfolio()
    assert entry_block_reason(good_snap(bid_qty=6_000, ask_qty=1_000), settings, book) == "매수/매도 잔량비 초과"
    assert entry_block_reason(good_snap(bid_qty=400, ask_qty=1_000), settings, book) is None
    assert entry_block_reason(good_snap(bid_qty=2_000, ask_qty=1_000), settings, book) is None
    flagged = replace(good_snap().meta, is_etf=True)
    assert entry_block_reason(good_snap(meta=flagged), settings, book) == "ETF 제외"
    young = replace(good_snap().meta, listed_on=date(2026, 9, 24))
    assert entry_block_reason(good_snap(meta=young), settings, book) == "상장 일수 부족"


def test_breakout_rejection_is_recorded():
    settings = default_settings()
    snap = good_snap(bid_qty=6_000, ask_qty=1_000)
    intents, rejected = evaluate(snap, portfolio(), settings)
    assert intents == []
    assert rejected == [("005930", "매수/매도 잔량비 초과")]


def test_add_blocks():
    settings = default_settings()
    held = position(last_buy_at=ts(9, 5, 0))
    snap = good_snap(ts=ts(9, 6, 30), price=10_550, session_high_before=10_500)
    assert add_block_reason(snap, held, settings) is None
    assert add_block_reason(good_snap(ts=ts(9, 6, 10)), position(last_buy_at=ts(9, 6, 0)), settings) == "추가 매수 간격"
    assert add_block_reason(good_snap(strength=105), held, settings) == "추가 체결강도 부족"
    assert add_block_reason(good_snap(strength=90), held, settings) == "체결강도 급락"
    assert add_block_reason(good_snap(session_high_before=20_000), held, settings) == "신고가 갱신 아님"
    assert add_block_reason(good_snap(price=10_550, session_high_before=10_550), held, settings) == "신고가 갱신 아님"
    rich = position(fill_notional=1_200_000, qty=100, last_buy_at=ts(9, 5, 0))
    assert add_block_reason(snap, rich, settings) == "이익 상태 아님"
    down = Bar(start=ts(9, 4), open=110, high=110, low=100, close=100, value=1)
    assert add_block_reason(good_snap(completed_bars=(down, down)), held, settings) == "연속 음봉"
    wick = Bar(start=ts(9, 5), open=100, high=120, low=100, close=110, value=1)
    assert add_block_reason(good_snap(completed_bars=(wick,)), held, settings) == "긴 윗꼬리"
    tall = Bar(start=ts(9, 4), open=100, high=100, low=100, close=100, value=1_000_000)
    short = Bar(start=ts(9, 5), open=100, high=100, low=100, close=100, value=400_000)
    assert add_block_reason(good_snap(completed_bars=(tall, short)), held, settings) == "1분 거래대금 급감"
    assert add_block_reason(good_snap(ts=ts(9, 45, 0)), held, settings) == "추가 매수 마감"


def test_hard_stop_uses_the_higher_of_avg_and_last_add():
    settings = validate_settings({**default_settings().to_dict(), "trail_pct": -30.0, "trail_wide_pct": -30.0})
    held = position(buy_count=2, qty=2, fill_notional=21_000, entry_price=10_000, last_add_price=11_000, high_since_entry=11_000)
    snap = good_snap(ts=ts(9, 10), price=10_700, prev_price=10_800)
    intent = maybe_exit(snap, held, settings)
    assert intent is not None
    assert intent.reason == "stop"
    assert "10780" in intent.note


def test_first_entry_stop_and_trail_and_breakeven():
    settings = default_settings()
    wide = validate_settings({**settings.to_dict(), "trail_pct": -30.0, "trail_wide_pct": -30.0})
    stop = maybe_exit(good_snap(price=9_800, prev_price=9_900), position(), wide)
    assert stop.reason == "stop"
    trail = maybe_exit(
        good_snap(ts=ts(9, 6, 30), price=10_140, prev_price=10_200),
        position(high_since_entry=10_300),
        settings,
    )
    assert trail.reason == "trail"
    held_be = position(high_since_entry=10_200)
    armed = maybe_exit(good_snap(price=10_250, prev_price=10_200), held_be, wide)
    assert armed is None
    assert held_be.breakeven_armed is True
    be = maybe_exit(good_snap(price=10_020, prev_price=10_040), held_be, wide)
    assert be is not None
    assert be.reason == "breakeven"
    raised = position(high_since_entry=10_300)
    maybe_exit(good_snap(price=10_250, prev_price=10_200), raised, wide)
    assert raised.breakeven_armed is True
    raised.fill_notional = 1_080_000
    after_add = maybe_exit(good_snap(price=10_250, prev_price=10_240), raised, wide)
    assert raised.breakeven_armed is False
    assert after_add is None or after_add.reason != "breakeven"


def test_partial_take_profit_once_and_time_stop():
    settings = default_settings()
    held = position()
    first = maybe_exit(good_snap(ts=ts(9, 10), price=10_400), held, settings)
    assert first.reason == "partial_tp"
    assert first.qty == 30
    second = maybe_exit(good_snap(ts=ts(9, 10, 5), price=10_450), held, settings)
    assert second is None or second.reason != "partial_tp"
    timed = maybe_exit(
        good_snap(ts=ts(9, 9), price=10_020),
        position(opened_at=ts(9, 6), high_since_entry=10_040),
        settings,
    )
    assert timed.reason == "time_stop"


def test_vi_down_bar_and_schedule():
    settings = default_settings()
    bar = Bar(start=ts(9, 9), open=11_000, high=11_000, low=10_400, close=10_500, value=1)
    held = position(vi_released_at=ts(9, 8, 30), high_since_entry=10_600)
    exit_intent = maybe_exit(
        good_snap(ts=ts(9, 10), price=10_600, completed_bars=(bar,), vi_price=10_700),
        held,
        settings,
    )
    assert exit_intent.reason == "vi_exit"
    flat = maybe_exit(good_snap(ts=ts(9, 47), price=10_100), position(opened_at=ts(9, 46)), settings)
    assert flat.reason == "schedule_flat"
    assert flat.qty == 50
    rest = maybe_exit(good_snap(ts=ts(9, 49, 30), price=10_100), position(opened_at=ts(9, 46), schedule_partial_done=True), settings)
    assert rest.reason == "market_flat"
    assert rest.order_type == "market"


def test_phase_names():
    settings = default_settings()
    assert phase_name(ts(8, 59), settings) == "장전"
    assert phase_name(ts(9, 1), settings) == "관망"
    assert phase_name(ts(9, 6), settings) == "신규진입"
    assert phase_name(ts(9, 42), settings) == "추가매수만"
    assert phase_name(ts(9, 46), settings) == "청산대기"
    assert phase_name(ts(9, 48), settings) == "분할청산"
    assert phase_name(ts(9, 49, 30), settings) == "잔량청산"
    assert phase_name(ts(9, 50), settings) == "마감"
