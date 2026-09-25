import json
from datetime import datetime

from daytrading.journal import Journal
from daytrading.kiwoom import LIVE_REST, PAPER_REST, KiwoomClient, KiwoomError
from daytrading.models import Intent, Snapshot, SymbolMeta
from daytrading.paper import CheckResult, KiwoomBroker, check_connection, run_paper
from daytrading.settings import default_settings
from daytrading.timeutil import KST


class Response:
    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class Opener:
    def __init__(self, token="paper-token"):
        self.token = token
        self.urls = []

    def __call__(self, request, timeout=10):
        self.urls.append(request.full_url)
        if request.full_url.endswith("/oauth2/token"):
            return Response({"return_code": 0, "token": self.token})
        if request.full_url.endswith("/api/dostk/ordr"):
            return Response({"return_code": 0, "ord_no": "55"})
        return Response({"return_code": 0, "stk_cd": "005930"})


class ScriptedSocket:
    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []
        self.closed = False

    def send(self, raw):
        self.sent.append(raw)

    def recv(self):
        if not self.messages:
            raise TimeoutError()
        return self.messages.pop(0)

    def close(self):
        self.closed = True


def _snap():
    moment = datetime(2026, 9, 25, 9, 6, tzinfo=KST)
    meta = SymbolMeta("005930", "삼성전자", 10_000, 10_100, 13_000, None, flags_known=False)
    return Snapshot(
        code="005930",
        name="삼성전자",
        ts=moment,
        price=10_550,
        prev_price=10_400,
        prev_close=10_000,
        day_open=10_100,
        day_high=10_550,
        acc_value=0,
        minute_value=0,
        prev_minute_value=None,
        avg_minute_value=None,
        minute_samples=0,
        strength=None,
        best_bid=10_540,
        best_ask=10_560,
        bid_qty=1,
        ask_qty=1,
        upper_limit=13_000,
        vi_active=False,
        vi_price=0,
        vi_released_at=None,
        breakout_high=None,
        completed_bars=(),
        meta=meta,
        session_high_before=10_500,
    )


def test_check_connection_never_calls_the_order_api():
    opener = Opener()
    socket = ScriptedSocket(
        [
            json.dumps({"trnm": "PING"}),
            json.dumps({"trnm": "LOGIN", "return_code": 0}),
            json.dumps({"trnm": "REG", "return_code": 0}),
        ]
    )
    result = check_connection(
        default_settings(),
        "key",
        "secret",
        opener=opener,
        connect=lambda url: socket,
    )
    assert result == CheckResult(
        True,
        result.message,
        token_ok=True,
        login_ok=True,
        subscribe_ok=True,
        orders_sent=0,
    )
    assert result.orders_sent == 0
    assert socket.closed
    assert any(raw for raw in socket.sent if "PING" in raw)
    assert all("/api/dostk/ordr" not in url for url in opener.urls)
    assert all(LIVE_REST not in url for url in opener.urls)
    assert any(PAPER_REST in url for url in opener.urls)


def test_paper_order_posts_only_to_the_mock_host():
    opener = Opener()
    client = KiwoomClient("key", "secret", mode="paper", opener=opener)
    client.token = "paper-token"
    broker = KiwoomBroker(client, dry_run=False)
    order = Intent("005930", "삼성전자", "buy", "entry", 1, "limit_ioc", 10_570, 10_570, trigger_price=10_570)
    _record, fill = broker.submit("0001", order, _snap(), default_settings())
    assert fill is None
    assert broker.place_calls == 1
    assert opener.urls == [PAPER_REST + "/api/dostk/ordr"]
    dry = KiwoomBroker(client, dry_run=True)
    dry.submit("0002", order, _snap(), default_settings())
    assert dry.place_calls == 0
    assert opener.urls == [PAPER_REST + "/api/dostk/ordr"]
    client.base_url = LIVE_REST
    try:
        KiwoomBroker(client)
    except KiwoomError:
        pass
    else:
        raise AssertionError("live host was accepted")


def test_run_paper_subscribes_and_does_not_place_orders_on_dry_run(tmp_path):
    opener = Opener()
    trade = {
        "trnm": "REAL",
        "data": [
            {
                "type": "0B",
                "item": "005930",
                "values": {"10": "+11000", "12": "10.00", "16": "10100", "14": "5000000000", "27": "+11010", "28": "+10990"},
            }
        ],
    }
    socket = ScriptedSocket(
        [
            json.dumps({"trnm": "LOGIN", "return_code": 0}),
            json.dumps({"trnm": "REG", "return_code": 0}),
            json.dumps(trade),
        ]
    )
    moment = datetime(2026, 9, 25, 9, 6, tzinfo=KST)
    journal = Journal(tmp_path)
    engine = run_paper(
        default_settings(),
        journal,
        "key",
        "secret",
        dry_run=True,
        opener=opener,
        connect=lambda url: socket,
        max_loops=5,
        codes=["005930"],
        clock=lambda: moment,
    )
    journal.close()
    assert engine.prices["005930"] == 11_000
    assert engine.market.books["005930"].meta.prev_close == 10_000
    assert engine.market.books["005930"].meta.day_open == 10_100
    assert engine.broker.place_calls == 0
    assert all("/api/dostk/ordr" not in url for url in opener.urls)
    assert socket.closed
    sent = " ".join(socket.sent)
    assert "REG" in sent
    assert "005930" in sent
