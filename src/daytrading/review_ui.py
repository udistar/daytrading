"""Time Machine 과 주문 승인 화면.

매매 탭의 장중 루프와 분리되어 있다. 여기서 주문을 내도 모의 기록만 남고
키움 주문 API 는 호출하지 않는다.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from daytrading.models import Portfolio
from daytrading.order_gate import OrderGate, OrderTicket, VerdictBook, submit_if_cleared
from daytrading.settings import SettingsStore
from daytrading.timeutil import KST
from daytrading.timemachine.engine import TimeMachineError, load_fixture, run_timemachine
from daytrading.timemachine.sample_data import packaged_fixture

FONT_FILE = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"


def apply_korean_font(app) -> None:
    if Path(FONT_FILE).exists():
        from PySide6.QtGui import QFontDatabase

        QFontDatabase.addApplicationFont(FONT_FILE)
        font = QFont("Noto Sans CJK KR")
        font.setPointSize(11)
        app.setFont(font)


class TimeMachinePage(QWidget):
    def __init__(self, store: SettingsStore):
        super().__init__()
        self.store = store
        self.fixture_path = packaged_fixture()
        root = QVBoxLayout(self)
        banner = QLabel("시뮬레이션 ≠ 예측. 과거 분봉을 재배열한 결과이며, 앞으로의 가격을 맞춘다는 뜻이 아닙니다.")
        banner.setWordWrap(True)
        banner.setObjectName("muted")
        root.addWidget(banner)
        tools = QHBoxLayout()
        self.run_button = QPushButton("샘플 픽스처 실행")
        self.run_button.setObjectName("go")
        self.run_button.clicked.connect(self.run_sample)
        tools.addWidget(self.run_button)
        tools.addWidget(QLabel("키움 주문·실전 시세는 호출하지 않습니다."))
        tools.addStretch(1)
        root.addLayout(tools)
        self.verdict = QLabel("아직 실행하지 않았습니다.")
        self.verdict.setWordWrap(True)
        self.verdict.setStyleSheet("font-size: 16px; font-weight: 700;")
        root.addWidget(self.verdict)
        self.stats = QTextEdit()
        self.stats.setReadOnly(True)
        root.addWidget(self.stats, 1)
        self.error = QLabel("")
        self.error.setWordWrap(True)
        self.error.setStyleSheet("color: #ff8f8f;")
        root.addWidget(self.error)

    def run_sample(self) -> None:
        self.error.setText("")
        try:
            settings = self.store.load()
            bars, plan = load_fixture(self.fixture_path)
            result = run_timemachine(bars, plan, settings)
        except (TimeMachineError, OSError, ValueError) as exc:
            self.error.setText(str(exc))
            return
        self.show_result(result)

    def show_result(self, result: dict) -> None:
        self.verdict.setText(result["verdict"])
        monte = result["monte_carlo"]
        history = result["history"]
        lines = [
            result["cost_note"],
            f"종목 {result['symbol']} {result['name']}  시드 {result['seed']}  재현 {result['paths']}  창 ±{result['window']}봉",
            f"목표 먼저 {monte['target_first']}  손절 먼저 {monte['stop_first']}  둘 다 아님 {monte['neither']}  비용 차감 후 이익 {monte['green']}",
            f"비용 차감 후 평균 R {monte['avg_r']}  손익분기 승률 {monte['breakeven_win_rate']}  닿은 재현의 목표 비율 {monte['target_rate_given_hit']}  차이(pp) {monte['diff_pp']}",
            f"청산가 분위수 {monte['close_percentiles']}",
            f"히스토리 발동 {history['fired']}  목표 {history['target_first']}  손절 {history['stop_first']}  둘 다 아님 {history['neither']}  평균 R {history['avg_r']}",
            f"풀세션 {result['pool_sessions']} / 요청 {result['lookback_requested']}",
            result["pool_note"],
            "정규장 분봉 TR ka11080. 분봉 보관 일수는 키움 문서에서 확인하지 못했습니다.",
            "실주문·결제 없음.",
        ]
        self.stats.setPlainText("\n".join(line for line in lines if line))


class ExplicitDialog(QDialog):
    def __init__(self, ticket: OrderTicket, parent=None):
        super().__init__(parent)
        self.answer = ""
        self.setWindowTitle("이 주문만 확인")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"주문 번호 {ticket.ticket_id} 만 확인합니다. 전체 예와 항상 허용은 없습니다."))
        layout.addWidget(QLabel(_summary(ticket)))
        row = QHBoxLayout()
        yes = QPushButton("예")
        no = QPushButton("아니오")
        yes.clicked.connect(self._yes)
        no.clicked.connect(self.reject)
        row.addWidget(yes)
        row.addWidget(no)
        layout.addLayout(row)

    def _yes(self) -> None:
        self.answer = "예"
        self.accept()


class AllowOnceDialog(QDialog):
    def __init__(self, ticket: OrderTicket, parent=None):
        super().__init__(parent)
        self.choice = ""
        self.setWindowTitle("Allow once")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("이 주문 한 번만 허용합니다. Always allow 는 없습니다."))
        layout.addWidget(QLabel(_summary(ticket)))
        row = QHBoxLayout()
        allow = QPushButton("Allow once")
        deny = QPushButton("Deny")
        allow.clicked.connect(self._allow)
        deny.clicked.connect(self._deny)
        row.addWidget(allow)
        row.addWidget(deny)
        layout.addLayout(row)

    def _allow(self) -> None:
        self.choice = "allow_once"
        self.accept()

    def _deny(self) -> None:
        self.choice = "deny"
        self.accept()


class GatePage(QWidget):
    """수동 주문 승인. 자동 매매 핫패스와 연결되어 있지 않다."""

    def __init__(self, store: SettingsStore):
        super().__init__()
        self.store = store
        self.now_fn = lambda: datetime.now(KST)
        self.gate = OrderGate(VerdictBook(store.home / "state" / "risk_verdicts.jsonl"))
        self.sent: list[OrderTicket] = []
        self._seq = 1
        root = QVBoxLayout(self)
        note = QLabel(
            "수동·복기용 승인입니다. 09:00~09:50 자동 주문 경로에는 연결되어 있지 않습니다. "
            "세 단계 전에는 모의 기록도 남지 않고, 실주문·결제는 하지 않습니다."
        )
        note.setWordWrap(True)
        note.setObjectName("muted")
        root.addWidget(note)
        form = QFormLayout()
        self.ticket_id = QLineEdit("M-001")
        self.ticket_id.setReadOnly(True)
        self.code = QLineEdit("900001")
        self.side = QLineEdit("buy")
        self.qty = QLineEdit("10")
        self.price = QLineEdit("10000")
        self.order_type = QLineEdit("limit_ioc")
        self.stop = QLineEdit("9850")
        self.target = QLineEdit("10300")
        self.reason = QLineEdit("manual")
        self.amount = QLineEdit("3000000")
        for label, widget in (
            ("주문 번호", self.ticket_id),
            ("종목", self.code),
            ("매수/매도", self.side),
            ("수량", self.qty),
            ("가격", self.price),
            ("유형", self.order_type),
            ("손절", self.stop),
            ("목표", self.target),
            ("사유", self.reason),
            ("금액", self.amount),
        ):
            form.addRow(label, widget)
        root.addLayout(form)
        row = QHBoxLayout()
        risk = QPushButton("위험 검사")
        risk.clicked.connect(self.on_assess)
        yes = QPushButton("이 주문만 예")
        yes.clicked.connect(self.on_confirm)
        allow = QPushButton("Allow once")
        allow.clicked.connect(self.on_allow)
        deny = QPushButton("Deny")
        deny.clicked.connect(self.on_deny)
        send = QPushButton("모의 기록")
        send.setObjectName("go")
        send.clicked.connect(self.on_send)
        for button in (risk, yes, allow, deny, send):
            row.addWidget(button)
        root.addLayout(row)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        root.addWidget(self.log, 1)

    def ticket(self) -> OrderTicket:
        return OrderTicket(
            ticket_id=self.ticket_id.text().strip(),
            code=self.code.text().strip(),
            name="수동",
            side=self.side.text().strip(),
            qty=int(self.qty.text()),
            price=int(self.price.text()),
            order_type=self.order_type.text().strip(),
            stop=int(self.stop.text()),
            target=int(self.target.text()),
            reason=self.reason.text().strip(),
            amount_krw=int(self.amount.text()),
        )

    def _write(self, text: str) -> None:
        self.log.append(text)

    def on_assess(self) -> None:
        try:
            settings = self.store.load()
            ticket = self.ticket()
        except (ValueError, OSError) as exc:
            self._write(str(exc))
            return
        book = Portfolio(cash=int(settings.initial_capital_krw))
        row = self.gate.assess(ticket, book, settings, None, self.now_fn(), 0)
        self._write(f"{row['status']} {row['reason']}")

    def on_confirm(self) -> None:
        ticket = self.ticket()
        dialog = ExplicitDialog(ticket, self)
        dialog.exec()
        decision = self.gate.confirm(ticket, dialog.answer or "아니오", self.now_fn())
        self._write(decision.reason)

    def on_allow(self) -> None:
        ticket = self.ticket()
        dialog = AllowOnceDialog(ticket, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self._write("Allow once 를 닫았습니다.")
            return
        decision = self.gate.allow_once(ticket, dialog.choice, self.now_fn())
        self._write(decision.reason)

    def on_deny(self) -> None:
        decision = self.gate.allow_once(self.ticket(), "deny", self.now_fn())
        self._write(decision.reason)

    def on_send(self) -> None:
        def _send(ticket: OrderTicket) -> None:
            self.sent.append(ticket)
            self._write(f"모의 기록만 남겼습니다. 실주문·결제는 하지 않았습니다. 주문 {ticket.ticket_id}")

        try:
            decision = submit_if_cleared(self.gate, self.ticket(), self.now_fn(), _send)
        except ValueError as exc:
            self._write(str(exc))
            return
        if not decision.allowed:
            self._write(decision.reason)


def _summary(ticket: OrderTicket) -> str:
    return (
        f"{ticket.side} {ticket.code} 수량 {ticket.qty} 가격 {ticket.price} "
        f"{ticket.order_type} 손절 {ticket.stop} 목표 {ticket.target}"
    )


def save_time_machine_png(home: Path, result: dict, path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from daytrading.ui import MainWindow

    app = QApplication.instance() or QApplication([])
    apply_korean_font(app)
    window = MainWindow(home)
    window.time_machine_page.show_result(result)
    window.tabs.setCurrentWidget(window.time_machine_page)
    window.resize(1280, 900)
    window.show()
    app.processEvents()
    pixmap = window.grab()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not pixmap.save(str(path), "PNG"):
        raise RuntimeError("화면 PNG 를 저장하지 못했습니다.")
    window.close()
