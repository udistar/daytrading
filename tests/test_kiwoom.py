import pytest

from daytrading.kiwoom import (
    LIVE_REST,
    LIMIT_IOC,
    MARKET,
    PAPER_REST,
    PAPER_WS,
    KiwoomClient,
    LiveTradingLocked,
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


def test_live_host_is_locked():
    with pytest.raises(LiveTradingLocked):
        KiwoomClient("key", "secret", mode="live")
    client = KiwoomClient("key", "secret", mode="paper")
    assert client.base_url == PAPER_REST
    assert LIVE_REST not in client.base_url
    assert client.ws_url.startswith(PAPER_WS)
