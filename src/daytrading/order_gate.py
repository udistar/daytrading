"""주문 3중 승인.

기존 risk.py 의 한도를 다시 만들지 않는다. 매수는 veto_buy 에 맡기고,
매도·청산은 일손실로 막지 않는다. 세 단계가 끝나기 전에는 send 를 부르지 않는다.

장중 09:00~09:50 자동 경로에는 붙이지 않는다. 수동·복기·시뮬 주문용이다.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from daytrading.kiwoom import normalize_code
from daytrading.models import Intent, Portfolio, Snapshot
from daytrading.risk import veto_buy
from daytrading.settings import Settings

FORBIDDEN_YES = frozenset({"전체 예", "항상 허용", "always", "always allow", "yes", "y"})
ALWAYS = frozenset({"always", "always allow", "항상 허용"})


@dataclass(frozen=True)
class OrderTicket:
    ticket_id: str
    code: str
    name: str
    side: str
    qty: int
    price: int
    order_type: str
    stop: int
    target: int
    reason: str
    amount_krw: int
    market: str = "KOSPI"


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


def fingerprint(ticket: OrderTicket) -> str:
    payload = {
        "amount_krw": int(ticket.amount_krw),
        "code": normalize_code(ticket.code),
        "order_type": ticket.order_type.strip().lower(),
        "price": int(ticket.price),
        "qty": int(ticket.qty),
        "reason": ticket.reason.strip().lower(),
        "side": ticket.side.strip().lower(),
        "stop": int(ticket.stop),
        "target": int(ticket.target),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class VerdictBook:
    """오늘·이 지문만 유효한 PASS/BLOCK 기록."""

    def __init__(self, path: Path):
        self.path = path
        self.rows: list[dict] = []
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self.rows.append(json.loads(line))

    def record(self, row: dict) -> None:
        self.rows.append(row)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    def latest(self, day: str, digest: str) -> dict | None:
        found = None
        for row in self.rows:
            if row.get("day") == day and row.get("fingerprint") == digest:
                found = row
        return found


class OrderGate:
    def __init__(self, book: VerdictBook):
        self.book = book
        self.explicit: dict[str, str] = {}
        self.allowed_once: dict[str, str] = {}
        self.denied: set[str] = set()
        self.rejections: list[str] = []

    def assess(
        self,
        ticket: OrderTicket,
        portfolio: Portfolio,
        settings: Settings,
        snap: Snapshot | None,
        now: datetime,
        pnl: int,
    ) -> dict:
        digest = fingerprint(ticket)
        day = now.date().isoformat()
        reason = _format_problem(ticket)
        status = "BLOCK"
        if reason is None and ticket.side.strip().lower() == "buy":
            reason = veto_buy(_intent(ticket), portfolio, settings, snap, now, pnl)
        if reason is None:
            status = "PASS"
            reason = "형식과 기존 위험 검사를 통과했습니다. 매수 추천이 아닙니다."
        row = {
            "day": day,
            "fingerprint": digest,
            "status": status,
            "reason": reason,
            "ticket_id": ticket.ticket_id,
        }
        self.book.record(row)
        if status != "PASS":
            self._reject(f"{ticket.ticket_id}: {reason}")
        return row

    def confirm(self, ticket: OrderTicket, answer: str, now: datetime) -> Decision:
        text = answer.strip()
        digest = fingerprint(ticket)
        if text in FORBIDDEN_YES or text != "예":
            self.explicit.pop(ticket.ticket_id, None)
            return self._reject_decision("이 주문만 '예'라고 답해야 합니다. 전체 예와 항상 허용은 없습니다.")
        saved = self.book.latest(now.date().isoformat(), digest)
        if saved is None or saved.get("status") != "PASS":
            return self._reject_decision("오늘 이 주문의 PASS 없이 확인할 수 없습니다.")
        self.explicit[ticket.ticket_id] = digest
        return Decision(True, "이 주문만 확인했습니다.")

    def allow_once(self, ticket: OrderTicket, choice: str, now: datetime) -> Decision:
        text = choice.strip()
        folded = text.lower()
        if folded in ALWAYS or text in ALWAYS:
            return self._reject_decision("Always allow 는 없습니다. Allow once 또는 Deny 만 가능합니다.")
        digest = fingerprint(ticket)
        if folded == "deny":
            self.allowed_once.pop(ticket.ticket_id, None)
            self.denied.add(ticket.ticket_id)
            return self._reject_decision("Deny 로 거부했습니다.")
        if folded != "allow_once":
            return self._reject_decision("Allow once 또는 Deny 만 가능합니다.")
        if self.explicit.get(ticket.ticket_id) != digest:
            return self._reject_decision("이 주문 번호에 대한 예가 없습니다.")
        saved = self.book.latest(now.date().isoformat(), digest)
        if saved is None or saved.get("status") != "PASS":
            return self._reject_decision("오늘 이 주문의 PASS 가 없습니다.")
        self.denied.discard(ticket.ticket_id)
        self.allowed_once[ticket.ticket_id] = digest
        return Decision(True, "Allow once")

    def release(self, ticket: OrderTicket, now: datetime) -> Decision:
        digest = fingerprint(ticket)
        saved = self.book.latest(now.date().isoformat(), digest)
        if saved is None or saved.get("status") != "PASS":
            return self._reject_decision("위험 검사 PASS 가 없거나 오늘 이 주문이 아닙니다.")
        if self.explicit.get(ticket.ticket_id) != digest:
            return self._reject_decision("지문이 바뀌었거나 이 주문 번호에 대한 예가 없습니다.")
        if ticket.ticket_id in self.denied:
            return self._reject_decision("Deny 로 거부된 주문입니다.")
        if self.allowed_once.get(ticket.ticket_id) != digest:
            return self._reject_decision("Allow once 가 없습니다.")
        return Decision(True, "세 단계를 통과했습니다.")

    def _reject(self, reason: str) -> None:
        self.rejections.append(reason)

    def _reject_decision(self, reason: str) -> Decision:
        self._reject(reason)
        return Decision(False, reason)


def submit_if_cleared(gate: OrderGate, ticket: OrderTicket, now: datetime, send) -> Decision:
    """통과한 주문만 send 에 넘긴다. send 는 모의 기록용이며 실주문 함수가 아니다."""
    decision = gate.release(ticket, now)
    if not decision.allowed:
        return decision
    send(ticket)
    return decision


def _intent(ticket: OrderTicket) -> Intent:
    return Intent(
        code=normalize_code(ticket.code),
        name=ticket.name,
        side="buy",
        reason=ticket.reason.strip().lower(),
        qty=int(ticket.qty),
        order_type=ticket.order_type.strip().lower(),
        limit_price=int(ticket.price),
        amount_krw=int(ticket.amount_krw),
    )


def _format_problem(ticket: OrderTicket) -> str | None:
    side = ticket.side.strip().lower()
    if side not in {"buy", "sell"}:
        return "매수·매도만 가능합니다."
    if not ticket.code.strip():
        return "종목 코드가 없습니다."
    if int(ticket.qty) < 1:
        return "수량이 없습니다."
    order_type = ticket.order_type.strip().lower()
    if order_type not in {"limit_ioc", "market"}:
        return "주문 유형은 limit_ioc 또는 market 이어야 합니다."
    if order_type == "limit_ioc" and int(ticket.price) <= 0:
        return "지정가는 0보다 커야 합니다."
    if int(ticket.amount_krw) < 0:
        return "금액이 음수입니다."
    return None
