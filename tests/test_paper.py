import csv
import json
from datetime import datetime, timedelta

from daytrading.journal import Journal
from daytrading.kiwoom import LIVE_REST, PAPER_REST, PAPER_WS, KiwoomClient, KiwoomError
from daytrading.models import Intent, Snapshot, SymbolMeta
from daytrading.paper import CheckResult, KiwoomBroker, PaperSessionEnded, check_connection, run_paper
from daytrading.settings import default_settings
from daytrading.timeutil import KST


class Response:
    def __init__(self, payload: dict, headers: dict | None = None):
        self.payload = json.dumps(payload).encode("utf-8")
        self.headers = headers or {}

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
            raise PaperSessionEnded()
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


def test_rejected_order_is_recorded_and_the_next_order_still_runs(tmp_path):
    from daytrading.models import Intent, OrderRecord
    from daytrading.session import Engine

    class Broker:
        def __init__(self):
            self.codes = []

        def submit(self, order_id, intent, snap, settings):
            self.codes.append(intent.code)
            if intent.code == "036540":
                return (
                    OrderRecord(
                        order_id,
                        snap.ts,
                        intent.code,
                        intent.name,
                        intent.side,
                        intent.reason,
                        intent.qty,
                        intent.order_type,
                        intent.limit_price,
                        "rejected",
                        "[2000](RC4001:모의투자 IOC/FOK 주문은 불가합니다.)",
                    ),
                    None,
                )
            return (
                OrderRecord(
                    order_id,
                    snap.ts,
                    intent.code,
                    intent.name,
                    intent.side,
                    intent.reason,
                    intent.qty,
                    intent.order_type,
                    intent.limit_price,
                    "accepted",
                    "",
                ),
                None,
            )

    from datetime import date

    from daytrading.models import SymbolMeta

    journal = Journal(tmp_path)
    engine = Engine(default_settings(), journal, "paper", broker=Broker())
    moment = datetime(2026, 11, 10, 9, 6, 17, tzinfo=KST)
    for code in ("036540", "005930"):
        engine.market.update(
            SymbolMeta(code, code, 1000, 1000, 1300, date(2020, 1, 2), flags_known=True),
            ts=moment,
            price=1000,
            acc_value=1,
            strength=100,
            best_bid=990,
            best_ask=1010,
            bid_qty=1,
            ask_qty=1,
            vi_active=False,
            vi_price=0,
        )
        engine.enqueue(Intent(code, code, "buy", "entry", 1, "limit_ioc", 1000, 1000, "지정가"))
    engine.drain(moment)
    engine.drain(moment.replace(second=19))
    journal.close()
    assert engine.broker.codes == ["036540", "005930"]
    with (tmp_path / "rejections.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["code"] == "036540"
    assert rows[0]["reason"] == "주문 거절"
    assert "RC4001" in rows[0]["detail"]
    assert "036540" not in engine.portfolio.pending_buys


def test_paper_loop_continues_after_an_order_error(tmp_path, monkeypatch):
    from daytrading.session import Engine

    calls = {"n": 0}
    real = Engine.drain

    def drain(self, now):
        calls["n"] += 1
        if calls["n"] == 1:
            raise KiwoomError("[2000](RC4001:모의투자 IOC/FOK 주문은 불가합니다.)")
        return real(self, now)

    monkeypatch.setattr(Engine, "drain", drain)
    socket = ScriptedSocket(
        [
            json.dumps({"trnm": "LOGIN", "return_code": 0}),
            json.dumps(_trade()),
            json.dumps(_trade("000660")),
        ]
    )
    journal = Journal(tmp_path)
    run_paper(
        default_settings(),
        journal,
        "key",
        "secret",
        dry_run=True,
        opener=Opener(),
        connect=lambda url: socket,
        codes=["005930"],
        clock=lambda: datetime(2026, 11, 10, 9, 10, tzinfo=KST),
    )
    journal.close()
    assert calls["n"] >= 2
    text = (tmp_path / "rejections.csv").read_text(encoding="utf-8-sig")
    assert "RC4001" in text
    sent = " ".join(socket.sent)
    assert '"00"' in sent


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
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["scenario"] == "paper"
    assert (tmp_path / "summary.txt").exists()


def _trade(code="005930"):
    return {
        "trnm": "REAL",
        "data": [
            {
                "type": "0B",
                "item": code,
                "values": {"10": "+11000", "12": "10.00", "16": "10100", "14": "5000000000", "27": "+11010", "28": "+10990"},
            }
        ],
    }


def _notice(side_flag: str):
    return {
        "trnm": "REAL",
        "data": [
            {
                "type": "00",
                "item": "005930",
                "values": {"9001": "005930", "910": "10000", "911": "1", "913": "체결", "907": side_flag},
            }
        ],
    }


def test_entry_block_is_written_once_per_minute_without_a_breakout(tmp_path):
    opener = Opener()
    socket = ScriptedSocket(
        [
            json.dumps({"trnm": "LOGIN", "return_code": 0}),
            json.dumps(_trade()),
            json.dumps(_trade()),
        ]
    )
    moment = datetime(2026, 9, 25, 9, 6, tzinfo=KST)
    journal = Journal(tmp_path)
    run_paper(
        default_settings(),
        journal,
        "key",
        "secret",
        dry_run=True,
        opener=opener,
        connect=lambda url: socket,
        codes=["005930"],
        clock=lambda: moment,
    )
    journal.close()
    with (tmp_path / "rejections.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows == [
        {
            "ts": "2026-09-25 09:06:00",
            "scenario": "paper",
            "code": "005930",
            "side": "buy",
            "reason": "종목 정보 없음",
            "detail": "flags_known=false",
        }
    ]
    assert socket.messages == []


def test_run_paper_fetches_symbol_meta_before_subscribe(tmp_path):
    opener = _MetaOpener()
    socket = ScriptedSocket(
        [
            json.dumps({"trnm": "LOGIN", "return_code": 0}),
            json.dumps(_trade()),
        ]
    )
    order = []

    def connect(url):
        order.append("ws")
        assert url.startswith(PAPER_WS)
        return socket

    moment = datetime(2026, 9, 25, 9, 6, tzinfo=KST)
    meta_path = tmp_path / "symbols_meta.csv"
    journal = Journal(tmp_path / "logs")
    engine = run_paper(
        default_settings(),
        journal,
        "key",
        "secret",
        dry_run=True,
        opener=opener,
        connect=connect,
        codes=["005930"],
        meta_path=meta_path,
        clock=lambda: moment,
        sleeper=lambda _seconds: order.append("sleep"),
    )
    journal.close()
    assert opener.apis[0] == "au10001"
    assert opener.apis[1:] == ["ka10099"] * 6
    assert order == ["sleep"] * 5 + ["ws"]
    assert all(url.startswith(PAPER_REST) for url in opener.urls)
    assert all(LIVE_REST not in url for url in opener.urls)
    assert all("/api/dostk/ordr" not in url for url in opener.urls)
    assert opener.apis.count("ka10099") == 6
    book = engine.market.books["005930"]
    assert book.meta.flags_known is True
    assert book.meta.listed_on.isoformat() == "1975-06-11"
    assert book.meta.is_etf is False
    assert book.meta.is_admin is False
    text = meta_path.read_text(encoding="utf-8-sig")
    assert "unknown" in text
    assert (tmp_path / "symbols_meta.csv.asof").read_text(encoding="utf-8").strip() == "2026-09-25"
    with (tmp_path / "logs" / "rejections.csv").open(encoding="utf-8-sig", newline="") as handle:
        reasons = {row["reason"] for row in csv.DictReader(handle)}
    assert "종목 정보 없음" not in reasons
    assert reasons


def test_run_paper_leaves_at_hard_cutoff_when_flat(tmp_path):
    socket = ScriptedSocket(
        [
            json.dumps({"trnm": "LOGIN", "return_code": 0}),
            json.dumps(_trade()),
            json.dumps(_trade()),
            json.dumps(_trade()),
        ]
    )
    moment = datetime(2026, 9, 25, 9, 50, tzinfo=KST)
    journal = Journal(tmp_path)
    engine = run_paper(
        default_settings(),
        journal,
        "key",
        "secret",
        dry_run=True,
        opener=Opener(),
        connect=lambda url: socket,
        codes=["005930"],
        clock=lambda: moment,
    )
    journal.close()
    assert len(socket.messages) == 2
    assert socket.closed
    assert engine.portfolio.positions == {}
    assert engine.broker.place_calls == 0


def test_run_paper_stays_until_the_position_is_flat_then_exits(tmp_path):
    socket = ScriptedSocket(
        [
            json.dumps({"trnm": "LOGIN", "return_code": 0}),
            json.dumps(_notice("매수")),
            json.dumps(_notice("매도")),
            json.dumps(_trade()),
        ]
    )
    moment = datetime(2026, 9, 25, 9, 50, tzinfo=KST)
    journal = Journal(tmp_path)
    engine = run_paper(
        default_settings(),
        journal,
        "key",
        "secret",
        dry_run=True,
        opener=Opener(),
        connect=lambda url: socket,
        codes=["005930"],
        clock=lambda: moment,
    )
    journal.close()
    assert len(socket.messages) == 1
    assert "005930" not in engine.portfolio.positions
    assert socket.closed
    assert engine.summary()["fill_count"] == 2
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["fill_count"] == 2


def test_run_paper_does_not_stop_before_hard_cutoff(tmp_path):
    socket = ScriptedSocket(
        [
            json.dumps({"trnm": "LOGIN", "return_code": 0}),
            json.dumps(_trade()),
            json.dumps(_trade()),
        ]
    )
    moment = datetime(2026, 9, 25, 9, 49, tzinfo=KST)
    journal = Journal(tmp_path)
    run_paper(
        default_settings(),
        journal,
        "key",
        "secret",
        dry_run=True,
        opener=Opener(),
        connect=lambda url: socket,
        codes=["005930"],
        clock=lambda: moment,
    )
    journal.close()
    assert socket.messages == []


class _MetaOpener:
    def __init__(self):
        self.urls = []
        self.apis = []

    def __call__(self, request, timeout=10):
        api = request.get_header("Api-id") or ""
        self.urls.append(request.full_url)
        self.apis.append(api)
        if request.full_url.endswith("/oauth2/token"):
            return Response({"return_code": 0, "token": "paper-token"})
        body = json.loads(request.data.decode())
        market = body.get("mrkt_tp")
        if market == "0":
            rows = [
                {
                    "code": "005930",
                    "name": "삼성전자",
                    "regDay": "19750611",
                    "state": "증거금100%",
                    "orderWarning": "0",
                    "auditInfo": "정상",
                }
            ]
        elif market == "8":
            rows = [
                {
                    "code": "069500",
                    "name": "KODEX 200",
                    "regDay": "20021014",
                    "state": "증거금100%",
                    "orderWarning": "0",
                }
            ]
        else:
            rows = []
        return Response({"return_code": 0, "list": rows}, {"cont-yn": "N", "next-key": ""})


def test_fill_poll_does_not_repeat_inside_its_interval():
    class Client:
        def __init__(self):
            self.base_url = PAPER_REST
            self.calls = []

        def order_detail(self, stk_cd=""):
            self.calls.append("kt00007")
            return {"acnt_ord_cntr_prps_dtl": []}

        def balance(self):
            self.calls.append("kt00018")
            return {"acnt_evlt_remn_indv_tot": []}

    client = Client()
    broker = KiwoomBroker(client)
    now = datetime(2026, 11, 11, 9, 10, tzinfo=KST)
    broker.working["1"] = {
        "ord_no": "1",
        "code": "005930",
        "side": "buy",
        "qty": 1,
        "filled": 0,
        "limit_price": 100,
        "placed_at": now,
        "order_type": "limit_ioc",
        "reason": "entry",
        "cancel_sent": False,
    }
    settings = default_settings()
    assert settings.paper_fill_poll_seconds >= settings.api_id_min_interval_seconds
    assert settings.paper_fill_poll_seconds == 3
    broker.poll_working(now, settings)
    broker.poll_working(now, settings)
    assert client.calls == ["kt00007", "kt00018"]
    broker.poll_working(now + timedelta(seconds=3), settings)
    assert client.calls == ["kt00007", "kt00018", "kt00007", "kt00018"]


def test_backoff_doubles_until_the_cap():
    from daytrading.paper import next_backoff

    assert next_backoff(1) == 2
    assert next_backoff(16) == 30
    assert next_backoff(30) == 30


def test_quotes_down_blocks_a_new_buy():
    from daytrading.models import Portfolio
    from daytrading.risk import veto_buy

    portfolio = Portfolio(cash=50_000_000, quotes_down=True)
    intent = Intent("005930", "삼성전자", "buy", "entry", 1, "limit_ioc", 10_000, 10_000)
    moment = datetime(2026, 11, 12, 9, 26, tzinfo=KST)
    assert veto_buy(intent, portfolio, default_settings(), None, moment, 0) == "시세 끊김"


def _holding_row():
    return {
        "stk_cd": "A005930",
        "stk_nm": "삼성전자",
        "rmnd_qty": "000000000000003",
        "cur_prc": "000000070000",
        "pur_pric": "000000069000",
    }


class _HoldingOpener(Opener):
    def __init__(self):
        super().__init__()
        self.apis = []
        self.bodies = []

    def __call__(self, request, timeout=10):
        api = request.get_header("Api-id")
        self.apis.append(api)
        body = json.loads(request.data.decode("utf-8")) if request.data else {}
        self.bodies.append(body)
        if api == "kt00018":
            return Response({"return_code": 0, "acnt_evlt_remn_indv_tot": [_holding_row()]})
        return super().__call__(request, timeout)


def test_carried_position_is_logged_and_sold_after_the_open(tmp_path):
    assert default_settings().sell_carried_positions is True
    journal = Journal(tmp_path / "early")
    run_paper(
        default_settings(),
        journal,
        "key",
        "secret",
        opener=_HoldingOpener(),
        connect=lambda url: ScriptedSocket([json.dumps({"trnm": "LOGIN", "return_code": 0})]),
        codes=["005930"],
        clock=lambda: datetime(2026, 11, 13, 8, 55, tzinfo=KST),
    )
    journal.close()
    early = (tmp_path / "early" / "signals.csv").read_text(encoding="utf-8-sig")
    assert "전일 보유" in early
    assert "3주" in early
    assert "69000" in early
    assert "carried" not in (tmp_path / "early" / "orders.csv").read_text(encoding="utf-8-sig")

    opener = _HoldingOpener()
    journal = Journal(tmp_path / "open")
    run_paper(
        default_settings(),
        journal,
        "key",
        "secret",
        opener=opener,
        connect=lambda url: ScriptedSocket([json.dumps({"trnm": "LOGIN", "return_code": 0})]),
        codes=["005930"],
        clock=lambda: datetime(2026, 11, 13, 9, 5, tzinfo=KST),
    )
    journal.close()
    assert opener.apis.count("kt10001") == 1
    sell = opener.bodies[opener.apis.index("kt10001")]
    assert sell["ord_qty"] == "3"
    assert sell["trde_tp"] == "3"
    orders = (tmp_path / "open" / "orders.csv").read_text(encoding="utf-8-sig")
    assert "carried" in orders

    from daytrading.settings import Settings

    saved = default_settings().to_dict()
    saved["sell_carried_positions"] = False
    quiet = _HoldingOpener()
    journal = Journal(tmp_path / "off")
    run_paper(
        Settings(saved),
        journal,
        "key",
        "secret",
        opener=quiet,
        connect=lambda url: ScriptedSocket([json.dumps({"trnm": "LOGIN", "return_code": 0})]),
        codes=["005930"],
        clock=lambda: datetime(2026, 11, 13, 9, 5, tzinfo=KST),
    )
    journal.close()
    assert "kt10001" not in quiet.apis
    assert "전일 보유" not in (tmp_path / "off" / "signals.csv").read_text(encoding="utf-8-sig")


def test_partial_flat_sells_without_a_new_quote(tmp_path):
    socket = ScriptedSocket(
        [
            json.dumps({"trnm": "LOGIN", "return_code": 0}),
            json.dumps(
                {
                    "trnm": "REAL",
                    "data": [
                        {
                            "type": "00",
                            "item": "005930",
                            "values": {"9001": "005930", "910": "10000", "911": "10", "913": "체결", "907": "2"},
                        }
                    ],
                }
            ),
        ]
    )
    journal = Journal(tmp_path)
    run_paper(
        default_settings(),
        journal,
        "key",
        "secret",
        dry_run=True,
        opener=Opener(),
        connect=lambda url: socket,
        codes=["005930"],
        clock=lambda: datetime(2026, 11, 12, 9, 47, tzinfo=KST),
    )
    journal.close()
    orders = (tmp_path / "orders.csv").read_text(encoding="utf-8-sig")
    assert "schedule_flat" in orders
    assert ",5," in orders or ",5\n" in orders or "5,market" in orders


def test_disconnect_retries_with_backoff_and_sells_at_the_cutoff(tmp_path):
    class Sock:
        def __init__(self, kind):
            self.kind = kind
            self.sent = []
            self.reads = 0
            self.closed = False

        def send(self, raw):
            self.sent.append(raw)

        def recv(self):
            self.reads += 1
            if self.kind == "fail":
                raise ConnectionResetError("connection lost")
            if self.reads == 1:
                return json.dumps({"trnm": "LOGIN", "return_code": 0})
            if self.kind == "first":
                if self.reads == 2:
                    return json.dumps(_notice("2"))
                raise ConnectionResetError("connection lost")
            raise PaperSessionEnded()

        def close(self):
            self.closed = True

    sockets = []
    phase = {"down": False}

    def clock():
        return datetime(2026, 11, 12, 9, 50 if phase["down"] else 9, 26, tzinfo=KST)

    class First:
        def __init__(self):
            self.inner = Sock("first")

        def send(self, raw):
            self.inner.send(raw)

        def recv(self):
            raw = self.inner.recv()
            if self.inner.reads >= 2:
                phase["down"] = True
            return raw

        def close(self):
            self.inner.close()

    sockets.clear()

    def connect2(url):
        if not sockets:
            sock = First()
        elif len(sockets) < 3:
            sock = Sock("fail")
        else:
            sock = Sock("ok")
        sockets.append(sock)
        return sock

    sleeps = []
    opener = Opener()
    journal = Journal(tmp_path)
    run_paper(
        default_settings(),
        journal,
        "key",
        "secret",
        opener=opener,
        connect=connect2,
        codes=["005930"],
        clock=clock,
        sleeper=lambda seconds: sleeps.append(seconds),
        max_loops=12,
    )
    journal.close()
    assert sleeps[:2] == [1.0, 2.0]
    assert any('"00"' in raw for raw in sockets[0].inner.sent)
    assert any("LOGIN" in raw for raw in sockets[-1].sent)
    assert any('"00"' in raw for raw in sockets[-1].sent)
    assert any(url.endswith("/api/dostk/ordr") for url in opener.urls)


def test_an_unexpected_error_sells_before_it_exits(tmp_path):
    class Boom:
        def __init__(self):
            self.sent = []
            self.reads = 0

        def send(self, raw):
            self.sent.append(raw)

        def recv(self):
            self.reads += 1
            if self.reads == 1:
                return json.dumps({"trnm": "LOGIN", "return_code": 0})
            if self.reads == 2:
                return json.dumps(_notice("2"))
            raise RuntimeError("socket died")

        def close(self):
            return None

    opener = Opener()
    journal = Journal(tmp_path)
    try:
        run_paper(
            default_settings(),
            journal,
            "key",
            "secret",
            opener=opener,
            connect=lambda url: Boom(),
            codes=["005930"],
            clock=lambda: datetime(2026, 11, 12, 9, 10, tzinfo=KST),
        )
    except RuntimeError as exc:
        assert "socket died" in str(exc)
    else:
        raise AssertionError("the socket error was swallowed")
    journal.close()
    assert any(url.endswith("/api/dostk/ordr") for url in opener.urls)


def test_align_holdings_follows_kt00018_and_does_not_invent_realized_pnl(tmp_path):
    from daytrading.paper import align_holdings
    from daytrading.session import Engine

    journal = Journal(tmp_path)
    client = KiwoomClient("key", "secret", opener=Opener())
    client.token = "paper-token"
    broker = KiwoomBroker(client, dry_run=True)
    engine = Engine(default_settings(), journal, "paper", broker=broker)
    now = datetime(2026, 11, 12, 9, 30, tzinfo=KST)
    from daytrading.paper import _install_carried

    _install_carried(engine, "005930", 5, 10_000, 10_000, now)
    align_holdings(engine, {"005930": {"qty": 2, "price": 11_000, "pur": 10_000}}, now)
    assert engine.portfolio.positions["005930"].qty == 2
    assert engine.portfolio.realized_krw == 0
    align_holdings(engine, {"005930": {"qty": 4, "price": 12_000, "pur": 10_000}}, now)
    assert engine.portfolio.positions["005930"].qty == 4
    assert engine.portfolio.realized_krw == 0
    journal.close()
