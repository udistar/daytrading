from datetime import datetime, timedelta

from daytrading.execution import OrderQueue, RateGate
from daytrading.journal import Journal
from daytrading.models import Intent
from daytrading.session import Engine
from daytrading.settings import default_settings
from daytrading.timeutil import KST


def intent(reason, side):
    return Intent(
        code="005930" if side == "buy" else "000660",
        name="t",
        side=side,
        reason=reason,
        qty=1,
        order_type="market" if side == "sell" else "limit_ioc",
        limit_price=0 if side == "sell" else 10_000,
        amount_krw=10_000,
    )


def test_exit_pops_before_entry():
    queue = OrderQueue()
    queue.push(intent("entry", "buy"))
    queue.push(intent("add", "buy"))
    queue.push(intent("stop", "sell"))
    assert queue.pop().reason == "stop"
    assert queue.pop().reason == "add"
    assert queue.pop().reason == "entry"


def test_token_bucket_and_paper_tr_spacing():
    gate = RateGate(default_settings())
    moment = datetime(2026, 9, 25, 9, 6, tzinfo=KST)
    for index in range(4):
        assert gate.take(f"tr{index}", moment)
    assert gate.ready("tr4", moment) is False
    later = moment + timedelta(seconds=1)
    assert gate.ready("tr4", later) is True
    other = RateGate(default_settings())
    assert other.take("kt10001", moment)
    assert other.ready("kt10001", moment) is False
    assert other.ready("kt10000", moment) is True
    assert other.ready("kt10001", moment + timedelta(seconds=1)) is True


def test_blocked_head_does_not_let_lower_priority_through(tmp_path):
    from tests.test_risk import held

    journal = Journal(tmp_path)
    engine = Engine(default_settings(), journal, "queue")
    engine.portfolio.positions["000660"] = held("000660")
    engine.enqueue(intent("stop", "sell"))
    engine.enqueue(intent("entry", "buy"))
    engine.drain(datetime(2026, 9, 25, 9, 6, tzinfo=KST))
    assert engine.queue.peek().reason == "stop"
    assert len(engine.queue) == 2
    journal.close()


def test_missing_quote_does_not_block_a_later_liquidation(tmp_path):
    from tests.test_risk import held

    journal = Journal(tmp_path)
    engine = Engine(default_settings(), journal, "queue")
    engine.portfolio.positions["AAAAAA"] = held("AAAAAA")
    engine.portfolio.positions["BBBBBB"] = held("BBBBBB")
    engine.prices["BBBBBB"] = 9_000
    engine.portfolio.loss_halted = True
    moment = datetime(2026, 9, 25, 9, 10, tzinfo=KST)
    engine.drain(moment)
    assert "BBBBBB" not in engine.portfolio.positions
    assert engine.queue.peek() is not None
    assert engine.queue.peek().code == "AAAAAA"
    assert engine.queue.peek().reason == "daily_loss"
    journal.close()


def test_every_rejection_is_logged(tmp_path):
    import csv

    journal = Journal(tmp_path)
    engine = Engine(default_settings(), journal, "queue")
    moment = datetime(2026, 9, 25, 9, 10, tzinfo=KST)
    engine.reject(moment, "005930", "buy", "같은 사유")
    engine.reject(moment, "005930", "buy", "같은 사유")
    journal.close()
    with (tmp_path / "rejections.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = [row for row in csv.DictReader(handle) if row["reason"] == "같은 사유"]
    assert len(rows) == 2


def test_cancelled_partial_can_be_tried_again(tmp_path):
    from daytrading.models import Intent, OrderRecord
    from tests.test_risk import held

    class CancelBroker:
        def submit(self, order_id, order, snap, settings):
            record = OrderRecord(
                order_id=order_id,
                ts=snap.ts,
                code=order.code,
                name=order.name,
                side=order.side,
                reason=order.reason,
                qty=order.qty,
                order_type=order.order_type,
                limit_price=order.limit_price,
                status="cancelled",
            )
            return record, None

    journal = Journal(tmp_path)
    engine = Engine(default_settings(), journal, "queue", broker=CancelBroker())
    position = held("005930")
    position.partial_done = True
    engine.portfolio.positions["005930"] = position
    engine.prices["005930"] = 10_400
    moment = datetime(2026, 9, 25, 9, 10, tzinfo=KST)
    engine.enqueue(Intent("005930", "t", "sell", "partial_tp", 3, "market", 0, 30_000, trigger_price=10_400))
    engine.drain(moment)
    assert position.partial_done is False
    journal.close()


def test_queued_add_does_not_fill_after_a_stop(tmp_path):
    from datetime import timedelta

    from daytrading.models import Intent
    from daytrading.scenarios import SIM_DAY, Tape, _meta
    from daytrading.strategy import evaluate
    from daytrading.timeutil import parse_stamp

    journal = Journal(tmp_path)
    settings = default_settings()
    engine = Engine(settings, journal, "repro")
    tape = Tape(_meta("111111", "A"))
    tape.seed("08:50:00", "09:05:00", 10_200)
    tape.add("09:06:00", 10_550, 400_000_000)
    for tick in tape.ticks:
        engine.market.update(
            tick.meta,
            ts=tick.ts,
            price=tick.price,
            acc_value=tick.acc_value,
            strength=tick.strength,
            best_bid=tick.best_bid,
            best_ask=tick.best_ask,
            bid_qty=tick.bid_qty,
            ask_qty=tick.ask_qty,
            vi_active=False,
            vi_price=0,
        )
        engine.prices["111111"] = tick.price
    now = parse_stamp(SIM_DAY, "09:06:00")
    intents, _blocked = evaluate(engine.market.books["111111"].snapshot(now, settings), engine.portfolio, settings)
    for item in intents:
        engine.enqueue(item)
    engine.drain(now)
    assert engine.portfolio.positions["111111"].qty > 0
    later = now + timedelta(seconds=40)
    engine.gate.tr_last["kt10000"] = later
    engine.enqueue(Intent("111111", "A", "buy", "add", 230, "limit_ioc", 10_860, 230 * 10_860, "2회차"))
    engine.market.update(
        tape.meta,
        ts=later,
        price=10_300,
        acc_value=tape.acc + 1,
        strength=140,
        best_bid=10_290,
        best_ask=10_310,
        bid_qty=1,
        ask_qty=1,
        vi_active=False,
        vi_price=0,
    )
    engine.prices["111111"] = 10_300
    exits, _blocked = evaluate(engine.market.books["111111"].snapshot(later, settings), engine.portfolio, settings)
    assert exits and exits[0].side == "sell"
    for item in exits:
        engine.enqueue(item)
    engine.drain(later)
    engine.drain(later + timedelta(seconds=1))
    assert engine.portfolio.positions.get("111111") is None
    journal.close()
