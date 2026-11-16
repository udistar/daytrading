"""신호·주문·체결·거절·호가를 CSV로 남긴다."""

from __future__ import annotations

import csv
import json
import time
from datetime import datetime
from pathlib import Path

from daytrading.models import Fill, Intent, OrderRecord, Snapshot


class Journal:
    def __init__(self, folder: Path):
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True)
        self._book_flush_seconds = 1.0
        self._book_flushed_at = 0.0
        self.signals = self._open("signals.csv", ["ts", "scenario", "code", "signal", "detail"], immediate=True)
        self.orders = self._open(
            "orders.csv",
            ["ts", "scenario", "order_id", "code", "side", "reason", "qty", "order_type", "limit_price", "status", "note"],
            immediate=True,
        )
        self.fills = self._open(
            "fills.csv",
            [
                "ts",
                "scenario",
                "order_id",
                "code",
                "name",
                "side",
                "reason",
                "qty",
                "price",
                "fee_krw",
                "tax_krw",
                "realized_delta_krw",
                "trigger_price",
                "slippage_krw",
            ],
            immediate=True,
        )
        self.rejections = self._open("rejections.csv", ["ts", "scenario", "code", "side", "reason", "detail"], immediate=True)
        self.books = self._open(
            "orderbook.csv",
            ["ts", "scenario", "code", "price", "best_bid", "best_ask", "bid_qty", "ask_qty", "bid_ask_ratio", "spread_pct"],
            immediate=False,
        )
        self.scenario = ""

    def _open(self, name: str, header: list[str], *, immediate: bool):
        path = self.folder / name
        new = not path.exists()
        # 호가는 묶어서 쓰고, 나머지 기록은 한 줄마다 디스크에 남긴다.
        handle = path.open("a", encoding="utf-8-sig", newline="", buffering=1 if immediate else -1)
        writer = csv.writer(handle)
        if new:
            writer.writerow(header)
            handle.flush()
        return handle, writer

    def _commit(self, slot, immediate: bool) -> None:
        handle = slot[0]
        if immediate:
            handle.flush()
            return
        now = time.monotonic()
        if now - self._book_flushed_at >= self._book_flush_seconds:
            handle.flush()
            self._book_flushed_at = now

    def close(self) -> None:
        self.books[0].flush()
        for handle, _writer in (self.signals, self.orders, self.fills, self.rejections, self.books):
            handle.close()

    def signal(self, ts: datetime, code: str, signal: str, detail: str) -> None:
        self.signals[1].writerow([_ts(ts), self.scenario, code, signal, detail])
        self._commit(self.signals, True)

    def order(self, record: OrderRecord) -> None:
        self.orders[1].writerow(
            [
                _ts(record.ts),
                self.scenario,
                record.order_id,
                record.code,
                record.side,
                record.reason,
                record.qty,
                record.order_type,
                record.limit_price,
                record.status,
                record.note,
            ]
        )
        self._commit(self.orders, True)

    def fill(self, fill: Fill) -> None:
        self.fills[1].writerow(
            [
                _ts(fill.ts),
                self.scenario,
                fill.order_id,
                fill.code,
                fill.name,
                fill.side,
                fill.reason,
                fill.qty,
                fill.price,
                fill.fee_krw,
                fill.tax_krw,
                fill.realized_delta_krw,
                fill.trigger_price,
                fill.slippage_krw,
            ]
        )
        self._commit(self.fills, True)

    def reject(self, ts: datetime, code: str, side: str, reason: str, detail: str = "") -> None:
        self.rejections[1].writerow([_ts(ts), self.scenario, code, side, reason, detail])
        self._commit(self.rejections, True)

    def orderbook(self, snap: Snapshot) -> None:
        ratio = "" if snap.bid_ask_ratio is None else f"{snap.bid_ask_ratio:.4f}"
        spread = "" if snap.spread_pct is None else f"{snap.spread_pct:.4f}"
        self.books[1].writerow(
            [
                _ts(snap.ts),
                self.scenario,
                snap.code,
                snap.price,
                snap.best_bid,
                snap.best_ask,
                snap.bid_qty,
                snap.ask_qty,
                ratio,
                spread,
            ]
        )
        self._commit(self.books, False)

    def write_summary(self, summary: dict) -> None:
        path = self.folder / "summary.json"
        path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        lines = [
            f"시나리오: {summary.get('scenario', '')}",
            f"실현손익: {summary.get('realized_krw', 0):,}원",
            f"평가손익: {summary.get('pnl_krw', 0):,}원",
            f"현금: {summary.get('cash', 0):,}원",
            f"체결 {summary.get('fill_count', 0)}건, 거절 {summary.get('rejection_count', 0)}건",
            "청산 사유: " + ", ".join(f"{key} {value}" for key, value in sorted(summary.get("reasons", {}).items())),
        ]
        (self.folder / "summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _ts(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def intent_side(intent: Intent) -> str:
    return intent.side
