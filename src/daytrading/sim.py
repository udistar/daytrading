"""내장 시뮬레이션을 돌리고 요약을 남긴다."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from daytrading.journal import Journal
from daytrading.scenarios import all_scenarios
from daytrading.session import Engine, run_ticks
from daytrading.settings import Settings


def run_builtin(log_dir: Path, settings: Settings, controls: dict | None = None) -> list[dict]:
    log_dir.mkdir(parents=True, exist_ok=True)
    journal = Journal(log_dir)
    summaries: list[dict] = []
    try:
        for name, ticks in all_scenarios():
            engine = Engine(settings, journal, name)
            summaries.append(run_ticks(engine, ticks, controls))
    finally:
        journal.close()
    text = render_report(summaries)
    (log_dir / "summary.json").write_text(json.dumps(summaries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (log_dir / "summary.txt").write_text(text, encoding="utf-8")
    return summaries


def render_report(summaries: list[dict]) -> str:
    lines = ["시뮬레이션 요약 (모의 체결, 실계좌 아님)", ""]
    for summary in summaries:
        lines.append(f"[{summary['scenario']}]")
        lines.append(f"  평가손익 {summary['pnl_krw']:,}원  실현 {summary['realized_krw']:,}원")
        lines.append(f"  체결 {summary['fill_count']}  거절 {summary['rejection_count']}")
        reasons = ", ".join(f"{key} {value}" for key, value in sorted(summary["reasons"].items()))
        lines.append(f"  사유: {reasons}")
        lines.append(
            "  상태: "
            + ("하루손실중단 " if summary["loss_halted"] else "")
            + ("연속손절단기종료 " if summary["day_halted"] else "")
            + f"연속손절 {summary['consecutive_losses']}"
        )
        lines.append("")
    return "\n".join(lines)


def _rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def load_dashboard(log_dir: Path) -> dict:
    fills = _rows(log_dir / "fills.csv")
    orders = _rows(log_dir / "orders.csv")
    rejections = _rows(log_dir / "rejections.csv")
    signals = _rows(log_dir / "signals.csv")
    books = _rows(log_dir / "orderbook.csv")
    summary_path = log_dir / "summary.json"
    summaries = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else []
    pnl = sum(int(item["pnl_krw"]) for item in summaries) if summaries else 0
    last_price: dict[str, int] = {}
    prev_price: dict[str, int] = {}
    names: dict[str, str] = {}
    for row in books:
        code = row["code"]
        price = int(row["price"])
        if code in last_price:
            prev_price[code] = last_price[code]
        last_price[code] = price
    for row in fills:
        names[row["code"]] = row["name"]
    quotes = []
    for code, price in sorted(last_price.items()):
        before = prev_price.get(code, price)
        if price > before:
            direction = "상승"
        elif price < before:
            direction = "하락"
        else:
            direction = "보합"
        quotes.append({"code": code, "name": names.get(code, ""), "price": price, "direction": direction})
    positions: dict[str, dict] = {}
    for row in fills:
        code = row["code"]
        item = positions.setdefault(
            code,
            {"code": code, "name": row["name"], "qty": 0, "notional": 0, "realized": 0},
        )
        qty = int(row["qty"])
        price = int(row["price"])
        if row["side"] == "buy":
            item["qty"] += qty
            item["notional"] += price * qty
        else:
            item["realized"] += int(row["realized_delta_krw"] or 0)
            item["qty"] -= qty
            if item["qty"] > 0 and item["notional"] > 0:
                avg = item["notional"] / (item["qty"] + qty)
                item["notional"] = int(avg * item["qty"])
            else:
                item["notional"] = 0
    held = []
    for item in positions.values():
        avg = int(item["notional"] / item["qty"]) if item["qty"] else 0
        held.append(
            {
                "code": item["code"],
                "name": item["name"],
                "qty": item["qty"],
                "avg": avg,
                "realized": item["realized"],
                "status": "보유" if item["qty"] else "청산",
            }
        )
    flow = []
    for row in signals:
        flow.append(f"{row['ts']}  {row['code']}  신호 {row['signal']}  {row['detail']}")
    for row in rejections:
        flow.append(f"{row['ts']}  {row['code']}  거절 {row['reason']}")
    flow = flow[-300:]
    return {
        "summaries": summaries,
        "pnl": pnl,
        "quotes": quotes,
        "positions": held,
        "orders": orders[-400:],
        "fills": fills[-400:],
        "flow": flow,
        "text": (log_dir / "summary.txt").read_text(encoding="utf-8") if (log_dir / "summary.txt").exists() else "",
    }
