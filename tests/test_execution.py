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
    journal = Journal(tmp_path)
    engine = Engine(default_settings(), journal, "queue")
    engine.enqueue(intent("stop", "sell"))
    engine.enqueue(intent("entry", "buy"))
    engine.drain(datetime(2026, 9, 25, 9, 6, tzinfo=KST))
    assert engine.queue.peek().reason == "stop"
    assert len(engine.queue) == 2
    journal.close()
