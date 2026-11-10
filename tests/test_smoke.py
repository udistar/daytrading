from datetime import datetime

import pytest

from daytrading.kiwoom import LIVE_REST, PAPER_REST, PAPER_WS, LiveTradingLocked
from daytrading.settings import default_settings
from daytrading.timeutil import KST


class FakeClient:
    def __init__(self, base_url=PAPER_REST):
        self.base_url = base_url
        self.ws_url = PAPER_WS + "/api/dostk/websocket"
        self.token = "paper-token"
        self.placed = []

    def place(self, side, code, qty, limit_price, market):
        self.placed.append(
            {
                "side": side,
                "code": code,
                "qty": qty,
                "limit_price": limit_price,
                "market": market,
                "url": self.base_url,
            }
        )
        return {"return_code": 0, "ord_no": "00024" if side == "buy" else "00025"}


def test_cli_diagnose_and_smoke_stay_on_the_paper_helpers(tmp_path, monkeypatch):
    from daytrading.__main__ import main

    seen = []
    monkeypatch.setattr("daytrading.diagnose.run_diagnose", lambda home, settings: seen.append(("diagnose", home)) or 0)
    monkeypatch.setattr("daytrading.smoke.run_smoke", lambda home: seen.append(("smoke", home)) or 0)
    assert main(["--diagnose", "--home", str(tmp_path)]) == 0
    assert main(["--smoke", "--home", str(tmp_path)]) == 0
    assert seen[0][0] == "diagnose"
    assert seen[1][0] == "smoke"
    assert seen[0][1] == seen[1][1] == tmp_path


def test_fill_listener_is_registered_before_it_reads_the_fill():
    import json

    from daytrading.smoke import FillListener

    class Socket:
        def __init__(self):
            self.sent = []
            self.messages = [
                json.dumps({"trnm": "LOGIN", "return_code": 0}),
                json.dumps(
                    {
                        "trnm": "REAL",
                        "data": [
                            {
                                "type": "00",
                                "item": "",
                                "values": {"9203": "00024", "910": "70000", "911": "1", "913": "체결", "907": "2"},
                            }
                        ],
                    }
                ),
            ]
            self.closed = False

        def settimeout(self, _seconds):
            return None

        def send(self, raw):
            self.sent.append(raw)

        def recv(self):
            if not self.messages:
                raise TimeoutError()
            return self.messages.pop(0)

        def close(self):
            self.closed = True

    socket = Socket()
    listener = FillListener(FakeClient(), connect=lambda _url, timeout=10: socket)
    listener.start()
    found = listener.wait(None, "00024", 5)
    listener.close()
    assert found["qty"] == 1
    assert found["price"] == 70_000
    assert found["side"] == "buy"
    sent = " ".join(socket.sent)
    assert "LOGIN" in sent
    assert '"00"' in sent
    assert socket.closed


def test_smoke_refuses_the_live_host_before_any_order():
    from daytrading.smoke import run_smoke_roundtrip

    client = FakeClient(LIVE_REST)
    with pytest.raises(LiveTradingLocked):
        run_smoke_roundtrip(client, market_open=True, wait_fill=lambda *_args: None)
    assert client.placed == []


def test_smoke_buys_one_share_on_mock_and_sells_the_fill_while_the_session_is_open():
    from daytrading.smoke import run_smoke_roundtrip

    client = FakeClient()
    seen = []

    def wait_fill(_client, order_no, _timeout):
        seen.append(order_no)
        return {"order_no": order_no, "qty": 1, "price": 70_000, "side": "buy"}

    report = run_smoke_roundtrip(client, market_open=True, wait_fill=wait_fill)
    assert seen == ["00024", "00025"]
    assert client.placed[0]["side"] == "buy"
    assert client.placed[0]["code"] == "005930"
    assert client.placed[0]["qty"] == 1
    assert client.placed[0]["market"] is True
    assert client.placed[0]["url"] == PAPER_REST
    assert LIVE_REST not in client.placed[0]["url"]
    assert client.placed[1]["side"] == "sell"
    assert client.placed[1]["qty"] == 1
    assert report["buy_order_no"] == "00024"
    assert report["buy_fill_price"] == 70_000
    assert report["sell_order_no"] == "00025"


def test_smoke_does_not_sell_when_the_session_is_closed_or_the_buy_did_not_fill():
    from daytrading.smoke import run_smoke_roundtrip

    closed = FakeClient()
    report = run_smoke_roundtrip(closed, market_open=False, wait_fill=lambda *_args: {"order_no": "00024", "qty": 1, "price": 70_000, "side": "buy"})
    assert len(closed.placed) == 1
    assert report["sell_order_no"] == ""

    unfilled = FakeClient()
    report = run_smoke_roundtrip(unfilled, market_open=True, wait_fill=lambda *_args: None)
    assert len(unfilled.placed) == 1
    assert report["sell_order_no"] == ""


def test_diagnose_report_shows_scaled_value_rank_window_and_no_orders():
    from daytrading.diagnose import build_diagnose_report
    from daytrading.kiwoom import volume_surge_body

    class RankClient:
        def __init__(self):
            self.base_url = PAPER_REST
            self.ws_url = PAPER_WS + "/api/dostk/websocket"
            self.placed = []

        def volume_rank(self):
            return {"trde_qty_sdnin": [{"stk_cd": "005930"}, {"stk_cd": "000660"}]}

    client = RankClient()
    text = build_diagnose_report(
        client,
        default_settings(),
        datetime(2026, 11, 10, 11, 47, tzinfo=KST),
        {"10": "-20800", "13": "30379732", "14": "632640", "20": "114700"},
    )
    assert client.placed == []
    assert "mockapi.kiwoom.com" in text
    assert "api.kiwoom.com" not in text.replace("mockapi.kiwoom.com", "")
    assert "632640000000" in text
    assert "632640" in text
    assert "백만원" in text
    assert "11:47:00" in text
    assert "판정: 신규 진입 마감" in text
    assert volume_surge_body()["tm_tp"] == "2"
    assert "전일" in text
    assert "005930" in text
