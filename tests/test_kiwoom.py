import json

import pytest

from daytrading.kiwoom import (
    LIVE_REST,
    LIMIT_IOC,
    LIMIT_REGULAR,
    MARKET,
    PAPER_REST,
    PAPER_WS,
    KiwoomClient,
    LiveTradingLocked,
    order_fill_reg_packet,
    parse_balance_map,
    parse_order_detail_rows,
    plan_order_sync,
    condition_realtime_packet,
    condition_stop_packet,
    normalize_code,
    order_body,
    parse_real_messages,
    token_body,
    ws_login_packet,
    ws_reg_packet,
)


def test_order_and_token_packets_match_official_fields():
    limit = order_body(code="A005930_AL", qty=3, limit_price=10570, market=False)
    assert limit["trde_tp"] == LIMIT_IOC == "10"
    assert limit["stk_cd"] == "005930"
    assert limit["ord_qty"] == "3"
    assert limit["ord_uv"] == "10570"
    assert limit["dmst_stex_tp"] == "KRX"
    market = order_body(code="000660", qty=1, limit_price=0, market=True)
    assert market["trde_tp"] == MARKET == "3"
    assert market["ord_uv"] == ""
    body = token_body("app", "secret")
    assert body == {"grant_type": "client_credentials", "appkey": "app", "secretkey": "secret"}


def test_websocket_packets_and_signed_price():
    assert ws_login_packet("raw-token") == {"trnm": "LOGIN", "token": "raw-token"}
    assert "Bearer" not in ws_login_packet("raw-token")["token"]
    reg = ws_reg_packet(["005930"], ["0B", "0D"])
    assert reg["trnm"] == "REG"
    assert reg["data"][0]["type"] == ["0B", "0D"]
    assert condition_realtime_packet("0")["trnm"] == "CNSRREQ"
    assert condition_realtime_packet("0")["search_type"] == "1"
    assert condition_stop_packet("0")["trnm"] == "CNSRCLR"
    events = parse_real_messages(
        {"trnm": "REAL", "data": [{"type": "0B", "item": "005930", "values": {"10": "-82000", "27": "+82050"}}]}
    )
    assert events[0]["price"] == 82000
    assert events[0]["best_ask"] == 82050
    assert normalize_code("005930_NX") == "005930"


def test_official_0b_example_scales_cumulative_value_from_million_won_to_won():
    """키움 공식 주식체결(0B) 예제. 14는 백만원, 13은 1주, 10은 원.

    632640백만원 / 30379732주 = 약 20824원으로, 체결가 20800원과 맞다.
    원으로 바꾸기 전에는 누적대금이 632640원이 되어 기본 누적 하한(50억)을 항상 넘지 못한다.
    """
    events = parse_real_messages(
        {
            "trnm": "REAL",
            "data": [
                {
                    "type": "0B",
                    "item": "005930",
                    "values": {"10": "-20800", "13": "30379732", "14": "632640"},
                }
            ],
        }
    )
    acc = events[0]["acc_value"]
    assert acc == 632_640 * 1_000_000
    assert acc >= 5_000_000_000
    implied = acc / 30_379_732
    assert abs(implied - 20_800) < 100


def test_live_host_is_locked():
    with pytest.raises(LiveTradingLocked):
        KiwoomClient("key", "secret", mode="live")
    client = KiwoomClient("key", "secret", mode="paper")
    assert client.base_url == PAPER_REST
    assert LIVE_REST not in client.base_url
    assert client.ws_url.startswith(PAPER_WS)


class _OrderResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class RecordingOpener:
    def __init__(self):
        self.requests = []

    def __call__(self, request, timeout=10):
        self.requests.append(request)
        return _OrderResponse({"return_code": 0, "ord_no": "00024", "token": "tok"})


def _order_client(base_url):
    opener = RecordingOpener()
    client = KiwoomClient("key", "secret", mode="paper", opener=opener)
    client.token = "paper-token"
    client.base_url = base_url
    client.live_trading_enabled = True
    return client, opener


def test_place_modify_and_cancel_post_only_when_the_host_is_exactly_mock():
    client, opener = _order_client(PAPER_REST)
    client.place("buy", "005930", 1, 0, True)
    client.place("sell", "005930", 1, 0, True)
    client.modify("005930", "0000139", 1, 199700)
    client.cancel("005930", "0000140", 1)
    assert [request.full_url for request in opener.requests] == [PAPER_REST + "/api/dostk/ordr"] * 4
    api_ids = [request.get_header("Api-id") for request in opener.requests]
    assert api_ids == ["kt10000", "kt10001", "kt10002", "kt10003"]
    bodies = [json.loads(request.data.decode("utf-8")) for request in opener.requests]
    assert bodies[2]["orig_ord_no"] == "0000139"
    assert bodies[2]["mdfy_qty"] == "1"
    assert bodies[2]["mdfy_uv"] == "199700"
    assert bodies[3]["orig_ord_no"] == "0000140"
    assert bodies[3]["cncl_qty"] == "1"


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.kiwoom.com",
        "https://api.kiwoom.com:443",
        "https://API.KIWOOM.COM",
        "",
        None,
        "https://evil.example",
        "https://MockAPI.kiwoom.com",
        "https://MOCKAPI.KIWOOM.COM",
        "https://mockapi.kiwoom.com.",
        "https://mockapi.kiwoom.com:443",
        "https://mockapi.kiwoom.com:8443",
        "https://mockapi.kiwoom.com.evil.com",
        "https://evil.mockapi.kiwoom.com",
        "http://mockapi.kiwoom.com",
        "https://user:pass@mockapi.kiwoom.com",
    ],
)
def test_order_paths_refuse_before_any_http_call(base_url):
    client, opener = _order_client(base_url)
    for call in (
        lambda: client.place("buy", "005930", 1, 0, True),
        lambda: client.modify("005930", "0000139", 1, 199700),
        lambda: client.cancel("005930", "0000140", 1),
    ):
        with pytest.raises(LiveTradingLocked):
            call()
    assert opener.requests == []


def test_token_and_rank_queries_are_not_blocked_by_the_order_host_gate():
    client, opener = _order_client(LIVE_REST)
    assert client.issue_token() == "tok"
    assert opener.requests[0].full_url == LIVE_REST + "/oauth2/token"


def test_mock_limit_order_is_regular_not_ioc():
    client, opener = _order_client(PAPER_REST)
    client.place("buy", "036540", 1, 197500, False)
    body = json.loads(opener.requests[0].data.decode("utf-8"))
    assert body["trde_tp"] == LIMIT_REGULAR == "0"
    assert body["trde_tp"] != LIMIT_IOC
    client.place("sell", "036540", 1, 0, True)
    market = json.loads(opener.requests[1].data.decode("utf-8"))
    assert market["trde_tp"] == MARKET == "3"


def test_order_detail_and_balance_parse_official_rows():
    rows = parse_order_detail_rows(
        {
            "acnt_ord_cntr_prps_dtl": [
                {
                    "ord_no": "0166849",
                    "stk_cd": "A005930",
                    "stk_nm": "삼성전자",
                    "io_tp_nm": "현금매수",
                    "cntr_qty": "0000000001",
                    "cntr_uv": "0000197400",
                    "ord_remnq": "0000000000",
                }
            ]
        }
    )
    assert rows[0]["code"] == "005930"
    assert rows[0]["side"] == "buy"
    assert rows[0]["cntr_qty"] == 1
    assert rows[0]["cntr_price"] == 197400
    held = parse_balance_map(
        {"acnt_evlt_remn_indv_tot": [{"stk_cd": "A005930", "rmnd_qty": "000000000000001", "cur_prc": "000000197400"}]}
    )
    assert held["005930"]["qty"] == 1
    assert held["005930"]["price"] == 197400


def test_sync_uses_kt00007_fill_and_cancels_a_resting_limit():
    from datetime import datetime, timedelta

    from daytrading.timeutil import KST

    placed = datetime(2026, 11, 10, 14, 0, tzinfo=KST)
    working = {
        "166849": {
            "ord_no": "0166849",
            "code": "005930",
            "name": "삼성전자",
            "side": "buy",
            "qty": 1,
            "filled": 0,
            "limit_price": 197500,
            "placed_at": placed,
            "order_type": "limit_ioc",
            "reason": "entry",
            "cancel_sent": False,
        }
    }
    fills, cancels = plan_order_sync(
        working,
        [{"ord_no": "0166849", "code": "005930", "side": "buy", "cntr_qty": 1, "cntr_price": 197400, "rem_qty": 0}],
        {},
        {},
        placed + timedelta(seconds=1),
        3,
        use_balance=True,
    )
    assert fills[0]["price"] == 197400
    assert fills[0]["qty"] == 1
    assert cancels == []

    resting = {
        "1": {
            "ord_no": "0000001",
            "code": "036540",
            "name": "SFA",
            "side": "buy",
            "qty": 1,
            "filled": 0,
            "limit_price": 5000,
            "placed_at": placed,
            "order_type": "limit_ioc",
            "reason": "entry",
            "cancel_sent": False,
        }
    }
    fills, cancels = plan_order_sync(
        resting,
        [{"ord_no": "0000001", "code": "036540", "side": "buy", "cntr_qty": 0, "cntr_price": 0, "rem_qty": 1}],
        {},
        {},
        placed + timedelta(seconds=3),
        3,
        use_balance=False,
    )
    assert fills == []
    assert cancels == ["0000001"]


def test_balance_increase_fills_when_the_order_query_has_no_row():
    from datetime import datetime, timedelta

    from daytrading.timeutil import KST

    placed = datetime(2026, 11, 10, 13, 43, tzinfo=KST)
    working = {
        "166849": {
            "ord_no": "0166849",
            "code": "005930",
            "name": "삼성전자",
            "side": "buy",
            "qty": 1,
            "filled": 0,
            "limit_price": 197500,
            "placed_at": placed,
            "order_type": "market",
            "reason": "entry",
            "cancel_sent": False,
        }
    }
    fills, cancels = plan_order_sync(
        working,
        [],
        {"005930": {"qty": 1, "price": 197400}},
        {"005930": 0},
        placed + timedelta(seconds=2),
        3,
        use_balance=True,
    )
    assert fills[0]["qty"] == 1
    assert fills[0]["price"] == 197400
    assert cancels == []


def test_order_fill_registration_is_its_own_packet():
    packet = order_fill_reg_packet()
    assert packet["trnm"] == "REG"
    assert packet["grp_no"] == "2"
    assert packet["data"][0]["type"] == ["00"]
    assert packet["data"][0]["item"] == [""]


def test_check_live_block_command_rejects_without_network_or_secrets(tmp_path, monkeypatch, capsys):
    from daytrading.__main__ import main

    monkeypatch.setenv("KIWOOM_APP_KEY", "LIVESECRETKEY")
    monkeypatch.setenv("KIWOOM_APP_SECRET", "LIVESECRETVALUE")

    def secrets_must_not_be_read(*args, **kwargs):
        raise AssertionError("secrets were read")

    def network_must_not_open(*args, **kwargs):
        raise AssertionError("network was opened")

    monkeypatch.setattr("daytrading.secrets.load_secrets", secrets_must_not_be_read)
    monkeypatch.setattr("urllib.request.urlopen", network_must_not_open)
    code = main(["--check-live-block", "--home", str(tmp_path)])
    captured = capsys.readouterr()
    assert code == 0
    assert "거절" in captured.out
    assert "api.kiwoom.com" in captured.out
    assert "LIVESECRET" not in captured.out + captured.err
