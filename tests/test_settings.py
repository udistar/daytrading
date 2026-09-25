from daytrading.settings import (
    SCHEMA,
    SettingsError,
    SettingsStore,
    default_settings,
    parse_tick_rules,
    tick_size,
    validate_settings,
)


def test_defaults_match_spec():
    settings = default_settings()
    assert settings.initial_capital_krw == 50_000_000
    assert settings.trading_mode == "paper"
    assert settings.live_trading_enabled is False
    assert settings.daily_loss_limit_krw == 1_000_000
    assert settings.loss_buffer_krw == 700_000
    assert settings.per_stock_cap_krw == 10_000_000
    assert settings.max_buys_per_stock == 5
    assert settings.max_concurrent_holdings == 5
    assert settings.vi_release_block_seconds == 180
    assert settings.vi_proximity_pct == 1.0
    assert settings.consecutive_loss_pause_count == 2
    assert settings.consecutive_loss_pause_seconds == 600
    assert settings.consecutive_loss_halt_count == 3
    assert settings.add_min_remaining_budget_krw == 300_000
    assert settings.entry_start == "09:05:00"
    assert settings.entry_end == "09:40:00"
    assert settings.add_end == "09:45:00"
    assert settings.partial_flat_start == "09:47:00"
    assert settings.market_flat_time == "09:49:30"
    assert settings.hard_cutoff == "09:50:00"
    assert settings.change_prev_min_pct == 3.0
    assert settings.change_prev_max_pct == 12.0
    assert settings.change_open_min_pct == 2.0
    assert settings.cumulative_value_min_krw == 5_000_000_000
    assert settings.minute_value_min_krw == 200_000_000
    assert settings.minute_value_multiple == 3.0
    assert settings.strength_min == 125.0
    assert settings.strength_window_minutes == 3
    assert settings.price_min_krw == 2_000
    assert settings.price_max_krw == 100_000
    assert settings.spread_max_pct == 0.3
    assert settings.gap_exclude_above_pct == 5.0
    assert settings.limit_up_proximity_pct == 5.0
    assert settings.bid_ask_ratio_max == 5.0
    assert settings.breakout_lookback_min == 5
    assert settings.entry_tick_offset == 1
    assert settings.pyramid_1_amount_krw == 3_000_000
    assert settings.pyramid_2_trigger_pct == 1.5
    assert settings.pyramid_2_amount_krw == 2_500_000
    assert settings.pyramid_3_amount_krw == 2_000_000
    assert settings.pyramid_4_amount_krw == 1_500_000
    assert settings.pyramid_5_trigger_pct == 6.0
    assert settings.pyramid_5_amount_krw == 1_000_000
    assert settings.add_min_seconds == 30
    assert settings.add_strength_min == 110.0
    assert settings.add_block_strength_below == 100.0
    assert settings.first_stop_pct == -1.5
    assert settings.avg_stop_pct == -1.2
    assert settings.last_add_stop_pct == -2.0
    assert settings.breakeven_arm_pct == 2.0
    assert settings.breakeven_stop_pct == 0.3
    assert settings.trail_pct == -1.5
    assert settings.trail_wide_arm_pct == 4.0
    assert settings.trail_wide_pct == -2.0
    assert settings.partial_tp_pct == 4.0
    assert settings.partial_tp_ratio_pct == 30.0
    assert settings.time_stop_seconds == 180
    assert settings.time_stop_min_gain_pct == 0.7
    assert settings.token_bucket_per_sec == 4.0
    assert settings.paper_tr_per_sec == 1.0
    assert settings.ws_max_symbols == 80
    assert settings.commission_rate_pct == 0.015
    assert settings.sell_tax_rate_pct == 0.20
    assert len({field.key for field in SCHEMA}) == len(SCHEMA)


def test_missing_keys_use_defaults():
    assert validate_settings({}).to_dict() == default_settings().to_dict()


def test_rejects_live_mode_and_unknown_key():
    raw = default_settings().to_dict()
    raw["trading_mode"] = "live"
    try:
        validate_settings(raw)
    except SettingsError as exc:
        assert any("paper" in item for item in exc.errors)
    else:
        raise AssertionError("live mode was accepted")
    raw = default_settings().to_dict()
    raw["live_trading_enabled"] = True
    try:
        validate_settings(raw)
    except SettingsError as exc:
        assert any("실전" in item for item in exc.errors)
    else:
        raise AssertionError("live flag was accepted")
    raw = default_settings().to_dict()
    raw["not_a_setting"] = 1
    try:
        validate_settings(raw)
    except SettingsError as exc:
        assert any("알 수 없는 설정" in item for item in exc.errors)
    else:
        raise AssertionError("unknown key was accepted")


def test_cross_field_rules():
    def attempt(**updates):
        raw = default_settings().to_dict()
        raw.update(updates)
        return validate_settings(raw)

    for updates, needle in (
        ({"loss_buffer_krw": 2_000_000, "daily_loss_limit_krw": 1_000_000}, "손실 완충"),
        ({"watch_start": "10:00:00", "entry_start": "09:05:00"}, "순서"),
        ({"pyramid_2_trigger_pct": 0.4, "pyramid_3_trigger_pct": 0.2}, "불타기"),
        ({"per_stock_cap_krw": 1_000_000}, "불타기 금액"),
        ({"add_block_strength_below": 150, "add_strength_min": 110}, "체결강도"),
        ({"breakeven_stop_pct": 3, "breakeven_arm_pct": 2}, "본전"),
        ({"trail_pct": -1.0, "trail_wide_pct": -0.5}, "트레일링"),
        ({"condition_seqs": "1,2,3", "condition_max": 2}, "조건식"),
        ({"tick_rules": "5000:5,2000:1"}, "호가"),
        ({"consecutive_loss_halt_count": 1, "consecutive_loss_pause_count": 2}, "당일 종료"),
    ):
        try:
            attempt(**updates)
        except SettingsError as exc:
            assert any(needle in item for item in exc.errors), exc.errors
        else:
            raise AssertionError(f"accepted {updates}")


def test_save_roundtrip_and_history(tmp_path):
    store = SettingsStore(tmp_path)
    raw = default_settings().to_dict()
    raw["daily_loss_limit_krw"] = 1_500_000
    raw["loss_buffer_krw"] = 600_000
    saved = validate_settings(raw)
    changes = store.save(saved)
    assert changes[0]["key"] == "daily_loss_limit_krw"
    loaded = store.load()
    assert loaded.daily_loss_limit_krw == 1_500_000
    assert loaded.loss_buffer_krw == 600_000
    history = store.history()
    assert len(history) == 1
    assert history[0]["changes"][0]["old"] == 1_000_000
    assert store.save(loaded) == []
    assert len(store.history()) == 1


def test_price_band_and_corrupt_settings_file(tmp_path):
    from daytrading.settings import clamp_to_market, price_band
    from daytrading.scenarios import Tape, _meta

    settings = default_settings()
    assert price_band(10_000, settings) == (7_000, 13_000)
    assert clamp_to_market(6_000, 10_000, settings) == 7_000
    assert clamp_to_market(10_555, 10_000, settings) == 10_550
    tape = Tape(_meta("096770", "갭"))
    tape.add("09:08:00", 6_000, 0)
    tick = tape.ticks[-1]
    assert tick.price == 7_000
    assert 7_000 <= tick.best_bid <= 13_000
    assert 7_000 <= tick.best_ask <= 13_000
    store = SettingsStore(tmp_path)
    (tmp_path / "settings.json").write_text("{", encoding="utf-8")
    store.save(default_settings())
    assert store.load().daily_loss_limit_krw == 1_000_000


def test_tick_table():
    settings = default_settings()
    rules = parse_tick_rules(settings.tick_rules)
    assert rules[0] == (2000, 1)
    assert tick_size(settings, 1999) == 1
    assert tick_size(settings, 2000) == 5
    assert tick_size(settings, 10000) == 10
    assert tick_size(settings, 50000) == 100
    assert tick_size(settings, 600000) == 1000
