import json
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from daytrading.__main__ import main
from daytrading.kiwoom import LiveTradingLocked
from daytrading.settings import default_settings, validate_settings
from daytrading.timeutil import KST
from daytrading.timemachine.bars import MinuteBar, expected_starts
from daytrading.timemachine.chart import fetch_minute_bars, minute_chart_body
from daytrading.timemachine.engine import (
    TimeMachineError,
    buy_cost,
    load_fixture,
    run_timemachine,
    sell_proceeds,
    verdict_sentence,
    write_result,
)
from daytrading.timemachine.sample_data import build_fixture_document, packaged_fixture, write_fixture

ROOT = Path(__file__).resolve().parents[1]
SAMPLE_RESULT = ROOT / "docs" / "timemachine-sample.json"


def tuned(**updates):
    raw = default_settings().to_dict()
    raw.update(updates)
    return validate_settings(raw)


def days(count, start=date(2026, 9, 1)):
    return [start + timedelta(days=offset) for offset in range(count)]


def session_bars(day_list, settings, shape):
    bars = []
    for day in day_list:
        for start in expected_starts(day, settings):
            open_price, high_price, low_price, close_price = shape(start.time())
            bars.append(MinuteBar(start, open_price, high_price, low_price, close_price, 100))
    return bars


def plan_for(day, entry=10000, stop=9850, target=10300, entry_time=time(9, 10)):
    from daytrading.timemachine.engine import TradePlan

    return TradePlan("900001", "샘플", "KOSPI", day, entry, entry_time, stop, target)


def test_regular_session_length_comes_from_the_clock():
    settings = default_settings()
    starts = expected_starts(date(2026, 10, 1), settings)
    assert starts[0].strftime("%H:%M") == "09:00"
    assert starts[-1].strftime("%H:%M") == "15:25"
    assert len(starts) == 78
    one_minute = tuned(tm_tic_scope="1")
    minute_starts = expected_starts(date(2026, 10, 1), one_minute)
    assert minute_starts[-1].strftime("%H:%M") == "15:29"
    assert len(minute_starts) == 390


def test_costs_move_breakeven_off_the_gross_payoff():
    settings = default_settings()
    gain = sell_proceeds(10300, settings, "KOSPI") - buy_cost(10000, settings)
    loss = buy_cost(10000, settings) - sell_proceeds(9850, settings, "KOSPI")
    assert gain > 0 and loss > 0
    assert abs((1 / (1 + gain / loss)) - (loss / (gain + loss))) < 1e-12
    assert (10300 - 10000) / (10000 - 9850) == 2
    assert gain / loss != 2


def test_verdict_sentences_are_fixed():
    assert "5%p 안쪽" in verdict_sentence(50, 50, 1.0, True)
    assert "동전 던지기에 가깝습니다" in verdict_sentence(50, 50, 1.0, True)
    assert "5%p 이상 15%p 미만 높습니다" in verdict_sentence(60, 40, 7.0, True)
    assert "15%p 이상 낮습니다" in verdict_sentence(10, 90, -20.0, True)
    assert "판정하지 않습니다" in verdict_sentence(0, 0, None, True)
    assert "손익분기를 계산하지 않습니다" in verdict_sentence(1, 1, None, False)
    assert verdict_sentence(50, 50, 1.0, True).endswith("시뮬레이션이며 예측이 아닙니다.")


def test_same_bar_stop_beats_target_and_repeats_with_the_seed():
    settings = tuned(tm_paths=64, tm_run_history=False)
    chosen = days(20)
    bars = session_bars(chosen, settings, lambda _clock: (10000, 10500, 9600, 10000))
    trade = plan_for(chosen[-1] + timedelta(days=1))
    first = run_timemachine(bars, trade, settings)
    second = run_timemachine(bars, trade, settings)
    assert first["monte_carlo"] == second["monte_carlo"]
    assert first["monte_carlo"]["stop_first"] == 64
    assert first["monte_carlo"]["target_first"] == 0
    assert first["monte_carlo"]["neither"] == 0
    assert first["seed"] == 19
    assert "시뮬레이션이며 예측이 아닙니다." in first["verdict"]


def test_window_zero_ignores_the_neighbouring_crash():
    settings = tuned(tm_paths=80, tm_window=0, tm_until="09:15:00", tm_run_history=False)

    def shape(clock):
        if clock == time(9, 5):
            return (10000, 10010, 9000, 10000)
        return (10000, 10000, 10000, 10000)

    chosen = days(20)
    bars = session_bars(chosen, settings, shape)
    trade = plan_for(chosen[-1] + timedelta(days=1))
    quiet = run_timemachine(bars, trade, settings)
    assert quiet["monte_carlo"]["stop_first"] == 0
    assert quiet["monte_carlo"]["neither"] == 80
    wider = run_timemachine(bars, trade, tuned(tm_paths=80, tm_window=1, tm_until="09:15:00", tm_run_history=False))
    assert wider["monte_carlo"]["stop_first"] > 0
    assert wider["monte_carlo"] == run_timemachine(bars, trade, tuned(tm_paths=80, tm_window=1, tm_until="09:15:00", tm_run_history=False))["monte_carlo"]


def test_fewer_than_twenty_full_sessions_is_refused():
    settings = default_settings()
    chosen = days(19)
    bars = session_bars(chosen, settings, lambda _clock: (10000, 10000, 10000, 10000))
    with pytest.raises(TimeMachineError) as caught:
        run_timemachine(bars, plan_for(chosen[-1] + timedelta(days=1)), settings)
    assert "풀세션" in caught.value.reason
    assert "20" in caught.value.reason


def test_plan_day_and_a_missing_bar_do_not_count():
    settings = tuned(tm_paths=5, tm_run_history=False, tm_min_full_sessions=20, tm_lookback_sessions=30)
    chosen = days(21)
    bars = session_bars(chosen, settings, lambda _clock: (10000, 10000, 10000, 10000))
    trade = plan_for(chosen[-1])
    result = run_timemachine(bars, trade, settings)
    assert result["pool_sessions"] == 20
    broken = [bar for bar in bars if not (bar.start.date() == chosen[0] and bar.start.time() == time(15, 25))]
    with pytest.raises(TimeMachineError):
        run_timemachine(broken, trade, settings)


def test_sample_history_is_eight_and_eight_and_matches_the_saved_json():
    settings = default_settings()
    document = build_fixture_document()
    path = packaged_fixture()
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["bars"] == document["bars"]
    bars, trade = load_fixture(path)
    result = run_timemachine(bars, trade, settings)
    assert result["history"]["fired"] == 16
    assert result["history"]["target_first"] == 8
    assert result["history"]["stop_first"] == 8
    assert result["history"]["neither"] == 0
    monte = result["monte_carlo"]
    assert monte["target_first"] + monte["stop_first"] + monte["neither"] == 10_000
    assert result["live_trading"] is False
    assert result["tr"] == "ka11080"
    saved = json.loads(SAMPLE_RESULT.read_text(encoding="utf-8"))
    assert result["monte_carlo"] == saved["monte_carlo"]
    assert result["history"] == saved["history"]
    assert result["verdict"] == saved["verdict"]
    assert saved["seed"] == 19
    assert saved["paths"] == 10_000


def test_time_machine_tab_shows_the_engine_sentence(tmp_path):
    from PySide6.QtWidgets import QApplication

    from daytrading.ui import MainWindow

    QApplication.instance() or QApplication([])
    window = MainWindow(tmp_path)
    verdict = "비용 차감 후, 테스트 문장입니다. 시뮬레이션이며 예측이 아닙니다."
    window.time_machine_page.show_result(
        {
            "verdict": verdict,
            "cost_note": "비용 차감 후",
            "symbol": "900001",
            "name": "샘플전자",
            "seed": 19,
            "paths": 1,
            "window": 2,
            "monte_carlo": {
                "target_first": 0,
                "stop_first": 0,
                "neither": 1,
                "green": 0,
                "avg_r": 0.0,
                "breakeven_win_rate": 0.5,
                "target_rate_given_hit": None,
                "diff_pp": None,
                "close_percentiles": {},
            },
            "history": {"fired": 0, "target_first": 0, "stop_first": 0, "neither": 0, "avg_r": None},
            "pool_sessions": 20,
            "lookback_requested": 60,
            "pool_note": "",
        }
    )
    assert window.tabs.tabText(2) == "Time Machine"
    assert window.tabs.tabText(3) == "주문 승인"
    assert window.time_machine_page.verdict.text() == verdict
    window.close()


def test_timemachine_does_not_import_the_live_loop():
    text = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "src" / "daytrading" / "timemachine").glob("*.py"))
    for name in ("strategy", "session", "execution", "risk"):
        assert f"daytrading.{name}" not in text


def test_cli_fixture_does_not_trade(tmp_path, capsys):
    settings = tuned(tm_paths=20, tm_run_history=False)
    from daytrading.settings import SettingsStore

    SettingsStore(tmp_path).save(settings)
    chosen = days(20)
    payload = {
        "symbol": "900001",
        "name": "샘플",
        "market": "KOSPI",
        "plan": {"date": "2026-10-30", "entry": 10000, "entry_time": "09:10:00", "stop": 9850, "target": 10300},
        "bars": [
            {
                "cntr_tm": start.strftime("%Y%m%d%H%M%S"),
                "open": 10000,
                "high": 10000,
                "low": 10000,
                "close": 10000,
                "volume": 1,
            }
            for day in chosen
            for start in expected_starts(day, settings)
        ],
    }
    fixture = tmp_path / "bars.json"
    out = tmp_path / "out.json"
    write_fixture(fixture, payload)
    code = main(["--timemachine", "--home", str(tmp_path), "--fixture", str(fixture), "--out", str(out)])
    assert code == 0
    text = capsys.readouterr().out
    assert "시뮬레이션이며 예측이 아닙니다." in text
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["monte_carlo"]["neither"] == 20
    assert saved["live_trading"] is False


class _Response:
    def __init__(self, payload, headers):
        self._payload = json.dumps(payload).encode("utf-8")
        self.headers = headers

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_chart_body_pagination_and_live_lock():
    assert minute_chart_body("A900001", "5", "20261001") == {
        "stk_cd": "900001",
        "tic_scope": "5",
        "upd_stkpc_tp": "0",
        "base_dt": "20261001",
    }
    pages = [
        (
            {"cont-yn": "Y", "next-key": "K1"},
            {
                "return_code": 0,
                "result_list": [
                    {
                        "cur_prc": "-100",
                        "open_pric": "-110",
                        "high_pric": "-120",
                        "low_pric": "-90",
                        "trde_qty": "3",
                        "cntr_tm": "20261001100500",
                    }
                ],
            },
        ),
        (
            {"cont-yn": "N", "next-key": ""},
            {
                "return_code": 0,
                "stk_min_pole_chart_qry": [
                    {
                        "cur_prc": "+90",
                        "open_pric": "+90",
                        "high_pric": "+95",
                        "low_pric": "+80",
                        "trde_qty": "1",
                        "cntr_tm": "20261001100000",
                    }
                ],
            },
        ),
    ]
    calls = []
    slept = []
    clock = {"t": 0.0}

    def opener(request, timeout):
        calls.append(request)
        headers, body = pages[len(calls) - 1]
        return _Response(body, headers)

    def sleep(seconds):
        slept.append(seconds)
        clock["t"] += seconds

    bars = fetch_minute_bars(
        opener,
        token="token",
        code="900001",
        tic_scope="5",
        sleep=sleep,
        clock=lambda: clock["t"],
        min_interval=1.0,
    )
    assert slept == [1.0]
    assert [bar.close for bar in bars] == [90, 100]
    assert bars[0].start == datetime(2026, 10, 1, 10, 0, tzinfo=KST)
    header_text = " ".join(f"{key}:{value}" for key, value in calls[0].header_items())
    assert "ka11080" in header_text
    assert "K1" in " ".join(f"{key}:{value}" for key, value in calls[1].header_items())

    def boom(*_args, **_kwargs):
        raise AssertionError("실전 호스트로 요청이 나갔습니다.")

    with pytest.raises(LiveTradingLocked):
        fetch_minute_bars(boom, token="token", code="900001", tic_scope="5", host="https://api.kiwoom.com")


def test_roundtrip_helper_is_used(tmp_path):
    document = build_fixture_document(session_count=20)
    path = tmp_path / "fixture.json"
    write_result(path, {"ok": True})
    write_fixture(path, document)
    bars, trade = load_fixture(path)
    assert trade.symbol == "900001"
    assert len(bars) == 20 * 78
