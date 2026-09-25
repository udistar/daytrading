import csv

from daytrading.sim import run_builtin
from daytrading.settings import default_settings


def _rows(path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def test_builtin_session_covers_the_trading_loop(tmp_path):
    summaries = run_builtin(tmp_path, default_settings())
    by_name = {item["scenario"]: item for item in summaries}
    morning = by_name["morning"]
    assert {"entry", "add", "partial_tp", "trail", "stop", "time_stop", "schedule_flat", "market_flat"} <= set(morning["reasons"])
    assert morning["day_halted"] is True
    assert morning["loss_halted"] is False
    assert morning["open_positions"] == []
    daily = by_name["daily_loss"]
    assert daily["loss_halted"] is True
    assert daily["reasons"].get("daily_loss", 0) >= 1
    vi = by_name["vi"]
    assert vi["reasons"].get("vi_exit", 0) >= 1
    rejections = _rows(tmp_path / "rejections.csv")
    reasons = {row["reason"] for row in rejections}
    assert "연속 손절 휴식" in reasons
    assert "연속 손절 당일 종료" in reasons
    assert "VI 해제 후 대기" in reasons
    fills = _rows(tmp_path / "fills.csv")
    assert fills
    assert "trigger_price" in fills[0]
    assert "slippage_krw" in fills[0]
    assert any(row["trigger_price"] for row in fills)
    assert (tmp_path / "summary.txt").read_text(encoding="utf-8")
    books = _rows(tmp_path / "orderbook.csv")
    assert "bid_ask_ratio" in books[0]
    assert any(row["bid_ask_ratio"] for row in books)
