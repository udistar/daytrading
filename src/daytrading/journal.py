"""신호·주문·체결·거절·호가를 CSV로 남긴다."""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

from daytrading.models import Fill, Intent, OrderRecord, Snapshot


class Journal:
    def __init__(self, folder: Path):
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True)
        self.signals = self._open("signals.csv", ["ts", "scenario", "code", "signal", "detail"])
        self.orders = self._open(
            "orders.csv",
            ["ts", "scenario", "order_id", "code", "side", "reason", "qty", "order_type", "limit_price", "status", "note"],
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
        )
        self.rejections = self._open("rejections.csv", ["ts", "scenario", "code", "side", "reason", "detail"])
        self.books = self._open(
            "orderbook.csv",
            ["ts", "scenario", "code", "price", "best_bid", "best_ask", "bid_qty", "ask_qty", "bid_ask_ratio", "spread_pct"],
        )
        self.scenario = ""

    def _open(self, name: str, header: list[str]):
        path = self.folder / name
        new = not path.exists()
        handle = path.open("a", encoding="utf-8-sig", newline="")
        writer = csv.writer(handle)
        if new:
            writer.writerow(header)
        return handle, writer

    def close(self) -> None:
        for handle, _writer in (self.signals, self.orders, self.fills, self.rejections, self.books):
            handle.close()

    def signal(self, ts: datetime, code: str, signal: str, detail: str) -> None:
        self.signals[1].writerow([_ts(ts), self.scenario, code, signal, detail])

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

    def reject(self, ts: datetime, code: str, side: str, reason: str, detail: str = "") -> None:
        self.rejections[1].writerow([_ts(ts), self.scenario, code, side, reason, detail])

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
