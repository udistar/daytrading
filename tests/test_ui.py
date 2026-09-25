import time

import pytest
from PySide6.QtWidgets import QApplication, QCheckBox, QLineEdit, QMessageBox

from daytrading.settings import SCHEMA, SettingsStore, default_settings
from daytrading.ui import MainWindow

ALTERNATES = {
    "initial_capital_krw": 40_000_000,
    "trading_mode": "paper",
    "live_trading_enabled": False,
    "allow_reentry_same_day": True,
    "block_entry_when_meta_unknown": False,
    "watch_start": "08:50:00",
    "entry_start": "09:00:00",
    "entry_end": "09:20:00",
    "add_end": "09:30:00",
    "partial_flat_start": "09:35:00",
    "market_flat_time": "09:40:00",
    "hard_cutoff": "09:45:00",
    "daily_loss_limit_krw": 1_500_000,
    "loss_buffer_krw": 500_000,
    "per_stock_cap_krw": 11_000_000,
    "max_buys_per_stock": 4,
    "max_concurrent_holdings": 4,
    "vi_release_block_seconds": 120,
    "vi_proximity_pct": 1.5,
    "consecutive_loss_pause_count": 1,
    "consecutive_loss_pause_seconds": 300,
    "consecutive_loss_halt_count": 4,
    "add_min_remaining_budget_krw": 200_000,
    "change_prev_min_pct": 2.5,
    "change_prev_max_pct": 11.0,
    "change_open_min_pct": 1.5,
    "cumulative_value_min_krw": 6_000_000_000,
    "minute_value_min_krw": 250_000_000,
    "minute_value_multiple": 2.5,
    "minute_value_lookback_min": 8,
    "strength_min": 130.0,
    "strength_window_minutes": 2,
    "price_min_krw": 3_000,
    "price_max_krw": 80_000,
    "spread_max_pct": 0.25,
    "gap_exclude_above_pct": 4.0,
    "limit_up_proximity_pct": 4.0,
    "limit_up_ratio_pct": 25.0,
    "listing_age_min_days": 10,
    "bid_ask_ratio_max": 4.0,
    "breakout_lookback_min": 4,
    "entry_tick_offset": 2,
    "exclude_admin": False,
    "exclude_caution": False,
    "exclude_warning": False,
    "exclude_risk": False,
    "exclude_overheat": False,
    "exclude_cleanup": False,
    "exclude_etf": False,
    "exclude_etn": False,
    "exclude_spac": False,
    "exclude_preferred": False,
    "pyramid_1_trigger_pct": 0.0,
    "pyramid_1_amount_krw": 3_200_000,
    "pyramid_2_trigger_pct": 1.2,
    "pyramid_2_amount_krw": 2_400_000,
    "pyramid_3_trigger_pct": 2.4,
    "pyramid_3_amount_krw": 1_900_000,
    "pyramid_4_trigger_pct": 4.0,
    "pyramid_4_amount_krw": 1_400_000,
    "pyramid_5_trigger_pct": 5.5,
    "pyramid_5_amount_krw": 900_000,
    "add_min_seconds": 45,
    "add_strength_min": 120.0,
    "add_block_strength_below": 90.0,
    "add_down_bars": 3,
    "long_wick_ratio": 0.55,
    "minute_value_drop_block_pct": 40.0,
    "add_require_new_high": False,
    "add_require_profit": False,
    "first_stop_pct": -1.8,
    "avg_stop_pct": -1.1,
    "last_add_stop_pct": -2.4,
    "breakeven_arm_pct": 2.5,
    "breakeven_stop_pct": 0.2,
    "trail_pct": -1.3,
    "trail_wide_arm_pct": 4.5,
    "trail_wide_pct": -2.5,
    "partial_tp_pct": 3.5,
    "partial_tp_ratio_pct": 25.0,
    "time_stop_seconds": 120,
    "time_stop_min_gain_pct": 0.9,
    "vi_exit_on_down_bar": False,
    "partial_flat_ratio_pct": 40.0,
    "token_bucket_per_sec": 3.0,
    "paper_tr_per_sec": 0.5,
    "live_order_per_sec": 4.0,
    "live_query_per_sec": 3.0,
    "ws_max_symbols": 40,
    "ranking_poll_seconds": 12,
    "condition_max": 4,
    "condition_seqs": "0,1",
    "commission_rate_pct": 0.02,
    "sell_tax_rate_pct": 0.18,
    "sim_adverse_ticks": 2,
    "tick_rules": "2000:1,5000:5,20000:10,50000:50,200000:100,500000:500,1000000000:500",
}


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _same(left, right) -> bool:
    if isinstance(right, float) or isinstance(left, float):
        return abs(float(left) - float(right)) < 1e-6
    return left == right


def test_every_schema_key_has_an_alternate_and_roundtrips(qapp, tmp_path):
    assert set(ALTERNATES) == {field.key for field in SCHEMA}
    window = MainWindow(tmp_path)
    window.show()
    qapp.processEvents()
    page = window.settings_page
    live = page.widgets["live_trading_enabled"]
    assert isinstance(live, QCheckBox)
    assert live.isEnabled() is False
    mode = page.widgets["trading_mode"]
    assert isinstance(mode, QLineEdit)
    assert mode.isReadOnly()
    for field in SCHEMA:
        widget = page.widgets[field.key]
        value = ALTERNATES[field.key]
        if isinstance(widget, QCheckBox):
            widget.setChecked(bool(value))
        elif isinstance(widget, QLineEdit):
            widget.setText(str(value))
        else:
            widget.setValue(float(value))
    before = page.collect()
    assert before["live_trading_enabled"] is False
    assert before["trading_mode"] == "paper"
    assert page.save() is True
    again = MainWindow(tmp_path)
    loaded = again.settings_page.collect()
    for key, value in before.items():
        assert _same(loaded[key], value), key
    history = SettingsStore(tmp_path).history()
    assert history
    assert any(change["key"] == "daily_loss_limit_krw" for change in history[0]["changes"])
    page.widgets["loss_buffer_krw"].setValue(2_000_000)
    page.widgets["daily_loss_limit_krw"].setValue(1_000_000)
    assert page.save() is False
    assert len(SettingsStore(tmp_path).history()) == 1
    page.reset_defaults()
    defaults = default_settings().to_dict()
    for key, value in page.collect().items():
        assert _same(value, defaults[key]), key
    window.close()
    again.close()


def test_pause_emergency_and_sim_tables(qapp, tmp_path, monkeypatch):
    window = MainWindow(tmp_path)
    window.show()
    qapp.processEvents()
    window.toggle_pause()
    assert window.controls["paused"] is True
    assert window.stop_button.text() == "매매 재개"
    window.toggle_pause()
    assert window.controls["paused"] is False
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.No)
    window.emergency()
    assert window.controls["emergency"] is False
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Yes)
    window.emergency()
    assert window.controls["emergency"] is True
    window.sim_button.click()
    deadline = time.time() + 30
    while time.time() < deadline:
        qapp.processEvents()
        if window.fill_table.rowCount() > 0 and window.sim_button.isEnabled():
            break
        time.sleep(0.02)
    else:
        raise AssertionError("시뮬레이션이 표를 채우지 못했습니다.")
    assert "시뮬레이션" in window.summary.toPlainText()
    assert window.quote_table.rowCount() > 0
    assert window.position_table.rowCount() > 0
    assert window.order_table.rowCount() > 0
    assert "거절" in window.flow.toPlainText() or "신호" in window.flow.toPlainText()
    window.close()
