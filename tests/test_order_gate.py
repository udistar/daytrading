from datetime import datetime

import pytest
from PySide6.QtWidgets import QApplication, QPushButton

from daytrading.models import Portfolio
from daytrading.order_gate import (
    OrderGate,
    OrderTicket,
    VerdictBook,
    fingerprint,
    submit_if_cleared,
)
from daytrading.review_ui import AllowOnceDialog, ExplicitDialog
from daytrading.settings import default_settings
from daytrading.timeutil import KST


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def now_at(hour, minute, day=2):
    return datetime(2026, 10, day, hour, minute, tzinfo=KST)


def buy_ticket(**updates):
    raw = dict(
        ticket_id="M-001",
        code="900001",
        name="샘플",
        side="buy",
        qty=10,
        price=10000,
        order_type="limit_ioc",
        stop=9850,
        target=10300,
        reason="entry",
        amount_krw=3_000_000,
    )
    raw.update(updates)
    return OrderTicket(**raw)


def sell_ticket():
    return buy_ticket(side="sell", reason="stop", order_type="market", price=0, amount_krw=100_000)


def gate(tmp_path):
    return OrderGate(VerdictBook(tmp_path / "state" / "risk_verdicts.jsonl"))


def test_pass_then_each_missing_step_blocks_the_send(tmp_path):
    book = Portfolio(cash=50_000_000)
    settings = default_settings()
    moment = now_at(9, 10)
    checker = gate(tmp_path)
    ticket = buy_ticket()
    sent = []
    assert checker.assess(ticket, book, settings, None, moment, 0)["status"] == "PASS"
    assert submit_if_cleared(checker, ticket, moment, sent.append).allowed is False
    assert sent == []
    assert checker.confirm(ticket, "예", moment).allowed is True
    assert submit_if_cleared(checker, ticket, moment, sent.append).allowed is False
    assert sent == []
    assert checker.allow_once(ticket, "allow_once", moment).allowed is True
    assert submit_if_cleared(checker, ticket, moment, sent.append).allowed is True
    assert sent == [ticket]


def test_deny_and_blanket_yes_never_send(tmp_path):
    checker = gate(tmp_path)
    ticket = buy_ticket()
    moment = now_at(9, 10)
    settings = default_settings()
    sent = []
    checker.assess(ticket, Portfolio(cash=50_000_000), settings, None, moment, 0)
    assert checker.confirm(ticket, "전체 예", moment).allowed is False
    assert checker.confirm(ticket, "항상 허용", moment).allowed is False
    assert checker.allow_once(ticket, "Always allow", moment).allowed is False
    checker.confirm(ticket, "예", moment)
    denied = checker.allow_once(ticket, "deny", moment)
    assert denied.allowed is False
    assert "Deny" in denied.reason
    assert submit_if_cleared(checker, ticket, moment, sent.append).allowed is False
    assert sent == []
    assert checker.rejections


def test_fingerprint_change_requires_a_new_pass(tmp_path):
    checker = gate(tmp_path)
    ticket = buy_ticket()
    moment = now_at(9, 10)
    settings = default_settings()
    book = Portfolio(cash=50_000_000)
    checker.assess(ticket, book, settings, None, moment, 0)
    checker.confirm(ticket, "예", moment)
    checker.allow_once(ticket, "allow_once", moment)
    changed = buy_ticket(qty=11)
    assert fingerprint(changed) != fingerprint(ticket)
    sent = []
    assert submit_if_cleared(checker, changed, moment, sent.append).allowed is False
    assert sent == []
    assert checker.assess(changed, book, settings, None, moment, 0)["status"] == "PASS"
    assert submit_if_cleared(checker, changed, moment, sent.append).allowed is False
    checker.confirm(changed, "예", moment)
    checker.allow_once(changed, "allow_once", moment)
    assert submit_if_cleared(checker, changed, moment, sent.append).allowed is True


def test_sell_is_not_blocked_by_the_daily_loss(tmp_path):
    checker = gate(tmp_path)
    moment = now_at(9, 10)
    settings = default_settings()
    halted = Portfolio(cash=50_000_000)
    halted.loss_halted = True
    sell = checker.assess(sell_ticket(), halted, settings, None, moment, -2_000_000)
    assert sell["status"] == "PASS"
    blocked = checker.assess(buy_ticket(), halted, settings, None, moment, -2_000_000)
    assert blocked["status"] == "BLOCK"
    assert "하루 손실" in blocked["reason"]
    early = checker.assess(buy_ticket(ticket_id="M-002"), Portfolio(cash=50_000_000), settings, None, now_at(9, 4), 0)
    assert early["status"] == "BLOCK"
    assert "신규 진입" in early["reason"]


def test_yesterdays_pass_does_not_count(tmp_path):
    ticket = buy_ticket()
    path = tmp_path / "state" / "risk_verdicts.jsonl"
    book = VerdictBook(path)
    book.record(
        {
            "day": "2026-10-01",
            "fingerprint": fingerprint(ticket),
            "status": "PASS",
            "reason": "어제",
            "ticket_id": ticket.ticket_id,
        }
    )
    checker = OrderGate(VerdictBook(path))
    moment = now_at(9, 10, day=2)
    assert checker.confirm(ticket, "예", moment).allowed is False
    assert checker.release(ticket, moment).allowed is False
    checker.assess(ticket, Portfolio(cash=50_000_000), default_settings(), None, moment, 0)
    reloaded = OrderGate(VerdictBook(path))
    assert reloaded.book.latest(moment.date().isoformat(), fingerprint(ticket))["status"] == "PASS"
    assert reloaded.release(ticket, moment).allowed is False


def test_dialogs_offer_only_this_order(qapp):
    ticket = buy_ticket()
    explicit = ExplicitDialog(ticket)
    allow = AllowOnceDialog(ticket)
    explicit_labels = {button.text() for button in explicit.findChildren(QPushButton)}
    allow_labels = {button.text() for button in allow.findChildren(QPushButton)}
    assert explicit_labels == {"예", "아니오"}
    assert allow_labels == {"Allow once", "Deny"}
    joined = " ".join(explicit_labels | allow_labels)
    assert "항상" not in joined
    assert "Always allow" not in joined
