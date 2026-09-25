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
    book = Portfolio(cash=50_000_000)
    moment = now_at(9, 10)
    assert veto_buy(buy("entry"), book, settings, None, moment, -700_000) == "손실 완충"
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
    assert veto_buy(buy(), Portfolio(cash=1), settings, released, moment, 0) == "VI 해제 후 대기"
    near = good_snap(vi_price=10_000, price=10_050, vi_released_at=None)
    assert veto_buy(buy(), Portfolio(cash=1), settings, near, moment, 0) == "VI 발동가 근접"
    paused = Portfolio(cash=1, pause_until=now_at(9, 20))
    assert veto_buy(buy(), paused, settings, None, moment, 0) == "연속 손절 휴식"
    halted = Portfolio(cash=1, day_halted=True)
    assert veto_buy(buy(), halted, settings, None, moment, 0) == "연속 손절 당일 종료"
    stopped = Portfolio(cash=1, paused=True)
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
