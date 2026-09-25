from datetime import datetime

from daytrading.models import Intent, Portfolio, Position
from daytrading.risk import note_closed_trade, protection_intents, veto_buy
from daytrading.settings import default_settings
from daytrading.timeutil import KST
from tests.test_strategy import good_snap


def now_at(hour, minute, second=0):
    return datetime(2026, 9, 25, hour, minute, second, tzinfo=KST)


def buy(reason="entry", amount=3_000_000, code="005930"):
    return Intent(
        code=code,
        name="테스트",
        side="buy",
        reason=reason,
        qty=10,
        order_type="limit_ioc",
        limit_price=10_000,
        amount_krw=amount,
    )


def sell():
    return Intent(
        code="005930",
        name="테스트",
        side="sell",
        reason="stop",
        qty=10,
        order_type="market",
        limit_price=0,
        amount_krw=100_000,
    )


def held(code, buy_count=1, notional=3_000_000):
    moment = now_at(9, 6)
    return Position(
        code=code,
        name=code,
        qty=10,
        cost_krw=notional,
        fill_notional=notional,
        entry_price=10_000,
        last_add_price=10_000,
        buy_count=buy_count,
        opened_at=moment,
        last_buy_at=moment,
        high_since_entry=10_000,
    )


def test_buffer_blocks_new_buys_not_adds_until_budget():
    settings = default_settings()
    moment = now_at(9, 10)
    fresh = Portfolio(cash=50_000_000)
    assert veto_buy(buy("entry"), fresh, settings, None, moment, -700_000) == "손실 완충"
    book = Portfolio(cash=50_000_000)
    book.positions["005930"] = held("005930")
    assert veto_buy(buy("add"), book, settings, None, moment, -700_000) is None
    assert veto_buy(buy("add"), book, settings, None, moment, -700_001) == "남은 손실 여유 부족"


def test_caps_holdings_and_schedule():
    settings = default_settings()
    moment = now_at(9, 10)
    book = Portfolio(cash=50_000_000)
    for index in range(5):
        book.positions[f"00000{index}"] = held(f"00000{index}")
    assert veto_buy(buy(), book, settings, None, moment, 0) == "동시 보유 한도"
    crowded = Portfolio(cash=50_000_000)
    crowded.positions["005930"] = held("005930", buy_count=5)
    assert veto_buy(buy("add"), crowded, settings, None, moment, 0) == "종목당 매수 횟수"
    heavy = Portfolio(cash=50_000_000)
    heavy.positions["005930"] = held("005930", notional=9_000_000)
    assert veto_buy(buy("add", amount=2_000_000), heavy, settings, None, moment, 0) == "종목당 금액 한도"
    assert veto_buy(buy(), Portfolio(cash=1), settings, None, now_at(9, 50), 0) == "09:50 이후"


def test_vi_pause_halt_and_sells_continue():
    settings = default_settings()
    moment = now_at(9, 10)
    released = good_snap(vi_released_at=now_at(9, 8), vi_price=0, price=10_550)
    assert veto_buy(buy(), Portfolio(cash=50_000_000), settings, released, moment, 0) == "VI 해제 후 대기"
    near = good_snap(vi_price=10_000, price=10_050, vi_released_at=None)
    assert veto_buy(buy(), Portfolio(cash=50_000_000), settings, near, moment, 0) == "VI 발동가 근접"
    paused = Portfolio(cash=50_000_000, pause_until=now_at(9, 20))
    assert veto_buy(buy(), paused, settings, None, moment, 0) == "연속 손절 휴식"
    halted = Portfolio(cash=50_000_000, day_halted=True)
    assert veto_buy(buy(), halted, settings, None, moment, 0) == "연속 손절 당일 종료"
    stopped = Portfolio(cash=50_000_000, paused=True)
    assert veto_buy(buy(), stopped, settings, None, moment, 0) == "매매 중지"
    assert veto_buy(sell(), halted, settings, None, moment, 0) is None
    assert veto_buy(sell(), stopped, settings, None, moment, 0) is None


def test_consecutive_losses_and_a_win_resets_the_streak():
    settings = default_settings()
    book = Portfolio(cash=1)
    moment = now_at(9, 10)
    note_closed_trade(book, -10_000, moment, settings)
    assert book.pause_until is None
    note_closed_trade(book, -10_000, moment, settings)
    assert book.consecutive_losses == 2
    assert book.pause_until is not None
    assert book.day_halted is False
    note_closed_trade(book, 5_000, moment, settings)
    assert book.consecutive_losses == 0
    assert book.day_halted is False
    book = Portfolio(cash=1)
    for _ in range(3):
        note_closed_trade(book, -1, moment, settings)
    assert book.day_halted is True
    note_closed_trade(book, 100, moment, settings)
    assert book.consecutive_losses == 0
    assert book.day_halted is True


def test_fifth_holding_is_allowed_and_the_sixth_is_not():
    settings = default_settings()
    moment = now_at(9, 10)
    book = Portfolio(cash=50_000_000)
    for index in range(4):
        book.positions[f"00000{index}"] = held(f"00000{index}")
    book.pending_buys.add("000000")
    assert book.open_count() == 4
    assert veto_buy(buy(code="005930"), book, settings, None, moment, 0) is None
    book.pending_buys.add("999999")
    assert veto_buy(buy(code="005930"), book, settings, None, moment, 0) == "동시 보유 한도"


def test_risk_rechecks_entry_and_add_cutoffs_and_cash():
    settings = default_settings()
    rich = Portfolio(cash=50_000_000)
    assert veto_buy(buy(), rich, settings, None, now_at(9, 40), 0) == "신규 진입 마감"
    assert veto_buy(buy("add"), rich, settings, None, now_at(9, 44), 0) == "보유 없는 추가 매수"
    held_book = Portfolio(cash=50_000_000)
    held_book.positions["005930"] = held("005930")
    assert veto_buy(buy("add"), held_book, settings, None, now_at(9, 45), 0) == "추가 매수 마감"
    assert veto_buy(buy(), Portfolio(cash=1_000), settings, None, now_at(9, 10), 0) == "현금 부족"
    rich.traded_today.add("005930")
    assert veto_buy(buy(), rich, settings, None, now_at(9, 10), 0) == "당일 재진입 안 함"


def test_cap_uses_cumulative_buys_after_a_partial_sell():
    settings = default_settings()
    book = Portfolio(cash=50_000_000)
    position = held("005930", notional=2_000_000)
    position.bought_krw = 9_500_000
    book.positions["005930"] = position
    book.bought_today["005930"] = 9_500_000
    assert veto_buy(buy("add", amount=1_000_000), book, settings, None, now_at(9, 10), 0) == "종목당 금액 한도"


def test_oversell_scales_fee_to_the_shares_actually_sold():
    from daytrading.ledger import apply_fill
    from daytrading.models import Fill

    settings = default_settings()
    book = Portfolio(cash=50_000_000)
    moment = now_at(9, 6)
    apply_fill(book, Fill("B1", moment, "005930", "삼성", "buy", "entry", 10, 10_000, 0, 0), settings)
    sell = Fill("S1", moment, "005930", "삼성", "sell", "stop", 15, 10_000, 100, 200)
    apply_fill(book, sell, settings)
    assert sell.qty == 10
    assert sell.fee_krw == 67
    assert sell.tax_krw == 133
    assert sell.realized_delta_krw == -200
    assert "005930" not in book.positions


def test_partial_fills_of_one_order_count_as_one_buy():
    from daytrading.ledger import apply_fill
    from daytrading.models import Fill

    book = Portfolio(cash=50_000_000)
    moment = now_at(9, 6)
    first = Fill("A1", moment, "005930", "삼성", "buy", "entry", 10, 10_000, 0, 0)
    second = Fill("A1", moment, "005930", "삼성", "buy", "entry", 5, 10_000, 0, 0)
    third = Fill("A2", moment, "005930", "삼성", "buy", "add", 4, 10_200, 0, 0)
    apply_fill(book, first, default_settings())
    apply_fill(book, second, default_settings())
    assert book.positions["005930"].buy_count == 1
    apply_fill(book, third, default_settings())
    assert book.positions["005930"].buy_count == 2
    assert book.bought_today["005930"] == 10 * 10_000 + 5 * 10_000 + 4 * 10_200


def test_daily_loss_and_emergency_liquidation():
    settings = default_settings()
    book = Portfolio(cash=1)
    book.positions["005930"] = held("005930")
    moment = now_at(9, 10)
    forced = protection_intents(book, {"005930": 9_000}, settings, moment, -1_000_000)
    assert book.loss_halted is True
    assert forced[0].reason == "daily_loss"
    book.emergency = True
    emergency = protection_intents(book, {"005930": 9_000}, settings, moment, -1_000_000)
    assert emergency[0].reason == "emergency"
