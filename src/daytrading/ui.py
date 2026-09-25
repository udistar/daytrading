"""로컬 데스크톱 화면. 매매 판단은 이 파일이 하지 않는다."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from daytrading import __version__
from daytrading.settings import (
    FIELDS,
    GROUPS,
    SCHEMA,
    Field,
    Settings,
    SettingsError,
    SettingsStore,
    default_settings,
)
from daytrading.sim import load_dashboard, run_builtin

STYLE = """
QMainWindow, QWidget { background: #14181f; color: #e7e3dc; font-size: 13px; }
QTabWidget::pane { border: 1px solid #2c3340; }
QTabBar::tab { background: #1d232d; color: #e7e3dc; padding: 8px 14px; }
QTabBar::tab:selected { background: #2e3848; }
QPushButton { background: #2a3342; color: #f3efe8; border: 1px solid #445066; padding: 8px 12px; border-radius: 4px; }
QPushButton:hover { background: #364356; }
QPushButton#danger { background: #8d2434; border-color: #c45a68; font-weight: 700; }
QPushButton#warn { background: #7a5b12; border-color: #c6a15a; }
QPushButton#go { background: #1f6b4a; border-color: #3e9d72; font-weight: 700; }
QLineEdit, QDoubleSpinBox, QTextEdit, QTableWidget {
  background: #0f131a; color: #f4f1ea; border: 1px solid #394355; padding: 4px;
}
QHeaderView::section { background: #222a36; color: #f4f1ea; padding: 4px; }
QLabel#pnlUp { color: #3dd68c; font-size: 28px; font-weight: 700; }
QLabel#pnlDown { color: #ff6b6b; font-size: 28px; font-weight: 700; }
QLabel#muted { color: #b7c0cc; }
"""


class SimWorker(QThread):
    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(self, log_dir: Path, settings: Settings, controls: dict):
        super().__init__()
        self.log_dir = log_dir
        self.settings = settings
        self.controls = controls

    def run(self) -> None:
        try:
            run_builtin(self.log_dir, self.settings, self.controls)
            self.finished_ok.emit(str(self.log_dir))
        except Exception as exc:  # noqa: BLE001 - show the message in the window
            self.failed.emit(str(exc))


class SettingsPage(QWidget):
    def __init__(self, store: SettingsStore):
        super().__init__()
        self.store = store
        self.widgets: dict[str, QWidget] = {}
        root = QVBoxLayout(self)
        note = QLabel("값을 바꾼 뒤 저장을 눌러야 다음 매매부터 적용됩니다. 범위를 벗어나면 저장되지 않고 이력도 남지 않습니다.")
        note.setWordWrap(True)
        note.setObjectName("muted")
        root.addWidget(note)
        tabs = QTabWidget()
        root.addWidget(tabs, 1)
        grouped: dict[str, list[Field]] = {group: [] for group in GROUPS}
        for field in SCHEMA:
            grouped[field.group].append(field)
        for group, fields in grouped.items():
            tabs.addTab(self._group(fields), group)
        self.error = QLabel("")
        self.error.setWordWrap(True)
        self.error.setStyleSheet("color: #ff8f8f;")
        root.addWidget(self.error)
        buttons = QHBoxLayout()
        save = QPushButton("저장")
        save.setObjectName("go")
        save.clicked.connect(self.save)
        reset = QPushButton("기본값으로")
        reset.clicked.connect(self.reset_defaults)
        history = QPushButton("변경 이력")
        history.clicked.connect(self.show_history)
        buttons.addWidget(save)
        buttons.addWidget(reset)
        buttons.addWidget(history)
        buttons.addStretch(1)
        root.addLayout(buttons)
        self.apply_settings(self.store.load())

    def _group(self, fields: list[Field]) -> QWidget:
        inner = QWidget()
        form = QFormLayout(inner)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        for field in fields:
            widget = self._editor(field)
            self.widgets[field.key] = widget
            label = QLabel(field.label)
            if field.help:
                label.setToolTip(field.help)
                widget.setToolTip(field.help)
            form.addRow(label, widget)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        return scroll

    def _editor(self, field: Field) -> QWidget:
        if field.kind == "bool":
            box = QCheckBox("사용")
            if field.key == "live_trading_enabled":
                box.setEnabled(False)
                box.setText("v0에서는 잠김")
            return box
        if field.kind in {"time", "str"}:
            edit = QLineEdit()
            if field.key == "trading_mode":
                edit.setReadOnly(True)
            return edit
        spin = QDoubleSpinBox()
        spin.setDecimals(0 if field.kind == "int" else field.decimals)
        spin.setRange(field.minimum if field.minimum is not None else -1_000_000_000, field.maximum if field.maximum is not None else 1_000_000_000_000)
        spin.setSingleStep(1 if field.kind == "int" else 10 ** (-field.decimals))
        spin.setGroupSeparatorShown(field.kind == "int")
        return spin

    def apply_settings(self, settings: Settings) -> None:
        for field in SCHEMA:
            widget = self.widgets[field.key]
            value = getattr(settings, field.key)
            if isinstance(widget, QCheckBox):
                widget.setChecked(bool(value))
            elif isinstance(widget, QLineEdit):
                widget.setText(str(value))
            else:
                widget.setValue(float(value))

    def collect(self) -> dict:
        raw = {}
        for field in SCHEMA:
            widget = self.widgets[field.key]
            if isinstance(widget, QCheckBox):
                raw[field.key] = widget.isChecked()
            elif isinstance(widget, QLineEdit):
                raw[field.key] = widget.text().strip()
            elif field.kind == "int":
                raw[field.key] = int(widget.value())
            else:
                raw[field.key] = float(widget.value())
        return raw

    def save(self) -> bool:
        try:
            from daytrading.settings import validate_settings

            settings = validate_settings(self.collect())
            changes = self.store.save(settings)
        except SettingsError as exc:
            self.error.setStyleSheet("color: #ff8f8f;")
            self.error.setText("\n".join(exc.errors))
            return False
        if changes:
            self.error.setText(f"저장했습니다. 바꾼 항목 {len(changes)}개. 이력에 남겨 두었습니다.")
        else:
            self.error.setText("저장했습니다. 바뀐 값은 없습니다.")
        self.error.setStyleSheet("color: #9ddec0;")
        return True

    def reset_defaults(self) -> None:
        self.apply_settings(default_settings())
        self.error.setStyleSheet("color: #f4f1ea;")
        self.error.setText("화면의 값을 기본값으로 되돌렸습니다. 저장을 눌러야 파일에 반영됩니다.")

    def show_history(self) -> None:
        rows = self.store.history()
        if not rows:
            QMessageBox.information(self, "변경 이력", "아직 저장된 변경이 없습니다.")
            return
        lines = []
        for row in rows[-30:]:
            lines.append(row["saved_at"])
            for change in row["changes"]:
                lines.append(f"  {change['label']}: {change['old']} → {change['new']}")
        box = QMessageBox(self)
        box.setWindowTitle("변경 이력")
        box.setText("최근 저장")
        box.setDetailedText("\n".join(lines))
        box.exec()


class MainWindow(QMainWindow):
    def __init__(self, home: Path):
        super().__init__()
        self.home = home
        self.store = SettingsStore(home)
        self.controls = {"paused": False, "emergency": False}
        self.worker: SimWorker | None = None
        self.log_dir: Path | None = None
        self.setWindowTitle(f"초단타 데이트레이딩 v{__version__} · 모의투자")
        self.resize(1180, 760)
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.addWidget(self._banner())
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        self.tabs.addTab(self._desk(), "매매")
        self.settings_page = SettingsPage(self.store)
        self.tabs.addTab(self.settings_page, "설정")
        self.statusBar().showMessage("모의투자 모드. 실전 주소는 v0에서 잠겨 있습니다. 키가 없어도 시뮬레이션으로 전체를 돌릴 수 있습니다.")

    def _banner(self) -> QWidget:
        frame = QFrame()
        row = QHBoxLayout(frame)
        self.mode = QLabel("모의투자")
        self.mode.setStyleSheet("background:#1f6b4a; color:white; padding:6px 10px; font-weight:700;")
        self.pnl = QLabel("오늘 손익 0원")
        self.pnl.setObjectName("pnlUp")
        self.budget = QLabel("손실 한도까지 1,000,000원")
        self.budget.setObjectName("muted")
        row.addWidget(self.mode)
        row.addWidget(self.pnl, 1)
        row.addWidget(self.budget)
        self.stop_button = QPushButton("매매 중지")
        self.stop_button.setObjectName("warn")
        self.stop_button.clicked.connect(self.toggle_pause)
        self.panic_button = QPushButton("긴급 전체 청산")
        self.panic_button.setObjectName("danger")
        self.panic_button.clicked.connect(self.emergency)
        row.addWidget(self.stop_button)
        row.addWidget(self.panic_button)
        return frame

    def _desk(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        tools = QHBoxLayout()
        self.sim_button = QPushButton("시뮬레이션 실행")
        self.sim_button.setObjectName("go")
        self.sim_button.clicked.connect(self.start_sim)
        tools.addWidget(self.sim_button)
        tools.addWidget(QLabel("키 없이 진입·불타기·손절·익절·시간청산·장 마감 청산·하루 손실 한도를 재현합니다."))
        tools.addStretch(1)
        layout.addLayout(tools)
        self.summary = QTextEdit()
        self.summary.setReadOnly(True)
        self.summary.setPlaceholderText("시뮬레이션을 실행하면 여기에 손익 요약이 나옵니다.")
        self.summary.setMaximumHeight(160)
        layout.addWidget(self.summary)
        tables = QTabWidget()
        self.quote_table = self._table(["종목", "이름", "가격", "방향"])
        self.position_table = self._table(["종목", "이름", "수량", "평균가", "실현손익", "상태"])
        self.order_table = self._table(["시각", "종목", "구분", "사유", "수량", "상태"])
        self.fill_table = self._table(["시각", "종목", "구분", "사유", "수량", "가격", "실현"])
        self.flow = QTextEdit()
        self.flow.setReadOnly(True)
        tables.addTab(self.quote_table, "종목·방향")
        tables.addTab(self.position_table, "보유")
        tables.addTab(self.order_table, "주문")
        tables.addTab(self.fill_table, "체결")
        tables.addTab(self.flow, "흐름·거절")
        layout.addWidget(tables, 1)
        return page

    def _table(self, headers: list[str]) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.horizontalHeader().setStretchLastSection(True)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        return table

    def toggle_pause(self) -> None:
        self.controls["paused"] = not self.controls["paused"]
        if self.controls["paused"]:
            self.stop_button.setText("매매 재개")
            self.statusBar().showMessage("매매 중지. 신규·추가 매수는 막히고, 손절·청산은 계속됩니다.")
        else:
            self.stop_button.setText("매매 중지")
            self.statusBar().showMessage("매매를 재개했습니다.")

    def emergency(self) -> None:
        answer = QMessageBox.question(
            self,
            "긴급 전체 청산",
            "보유 종목을 시장가로 모두 팔고 오늘 신규 매수를 멈출까요?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.controls["emergency"] = True
        self.statusBar().showMessage("긴급 청산을 요청했습니다. 진행 중인 시뮬레이션은 다음 초에 전량 시장가입니다.")

    def start_sim(self) -> None:
        if self.worker and self.worker.isRunning():
            return
        try:
            settings = self.store.load()
        except SettingsError as exc:
            QMessageBox.warning(self, "설정", "\n".join(exc.errors))
            return
        self.controls["paused"] = False
        self.controls["emergency"] = False
        self.stop_button.setText("매매 중지")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.log_dir = self.home / "logs" / stamp
        self.sim_button.setEnabled(False)
        self.sim_button.setText("실행 중…")
        self.worker = SimWorker(self.log_dir, settings, self.controls)
        self.worker.finished_ok.connect(self._sim_done)
        self.worker.failed.connect(self._sim_failed)
        self.worker.start()

    def _sim_done(self, folder: str) -> None:
        self.sim_button.setEnabled(True)
        self.sim_button.setText("시뮬레이션 실행")
        board = load_dashboard(Path(folder))
        self._show_board(board)
        self.statusBar().showMessage(f"시뮬레이션 로그: {folder}")

    def _sim_failed(self, message: str) -> None:
        self.sim_button.setEnabled(True)
        self.sim_button.setText("시뮬레이션 실행")
        QMessageBox.warning(self, "시뮬레이션", message)

    def _show_board(self, board: dict) -> None:
        pnl = int(board["pnl"])
        self.pnl.setText(f"오늘 손익 {pnl:,}원")
        self.pnl.setObjectName("pnlUp" if pnl >= 0 else "pnlDown")
        self.pnl.style().unpolish(self.pnl)
        self.pnl.style().polish(self.pnl)
        settings = self.store.load()
        left = settings.daily_loss_limit_krw + pnl
        self.budget.setText(f"손실 한도까지 {left:,}원")
        self.summary.setPlainText(board["text"])
        self._fill(self.quote_table, [[q["code"], q["name"], f"{q['price']:,}", q["direction"]] for q in board["quotes"]])
        self._fill(
            self.position_table,
            [
                [p["code"], p["name"], str(p["qty"]), f"{p['avg']:,}" if p["avg"] else "-", f"{p['realized']:,}", p["status"]]
                for p in board["positions"]
            ],
        )
        self._fill(
            self.order_table,
            [[o["ts"][11:], o["code"], o["side"], o["reason"], o["qty"], o["status"]] for o in board["orders"]],
        )
        self._fill(
            self.fill_table,
            [
                [f["ts"][11:], f["code"], f["side"], f["reason"], f["qty"], f"{int(f['price']):,}", f"{int(f['realized_delta_krw']):,}"]
                for f in board["fills"]
            ],
        )
        self.flow.setPlainText("\n".join(board["flow"]))

    def _fill(self, table: QTableWidget, rows: list[list[str]]) -> None:
        table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                item = QTableWidgetItem(value)
                if c == 3 and table is self.quote_table:
                    if value == "상승":
                        item.setForeground(Qt.GlobalColor.green)
                    elif value == "하락":
                        item.setForeground(Qt.GlobalColor.red)
                table.setItem(r, c, item)
        table.resizeColumnsToContents()


def launch(home: Path | None = None) -> int:
    from daytrading.settings import default_home

    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(STYLE)
    font = QFont("Malgun Gothic")
    if not font.exactMatch():
        font = QFont()
        font.setPointSize(11)
    app.setFont(font)
    window = MainWindow(home or default_home())
    window.show()
    return app.exec()
