"""모의 계좌 주문 스모크.

mockapi.kiwoom.com 에만 붙는다. 삼성전자 1주를 시장가로 산 뒤,
장중이고 체결이 보이면 그 수량만 시장가로 판다.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, time as clock_time
from pathlib import Path
from urllib.parse import urlsplit

from daytrading.kiwoom import LiveTradingLocked
from daytrading.timeutil import KST

SMOKE_CODE = "005930"
SMOKE_QTY = 1


def assert_paper_endpoint(url: str) -> str:
    """접속 주소가 모의투자 호스트가 아니면 주문을 열기 전에 거부한다."""
    if "://api.kiwoom.com" in url:
        raise LiveTradingLocked("실전 서버(api.kiwoom.com)로는 접속할 수 없습니다.")
    host = (urlsplit(url).hostname or "").lower()
    if host != "mockapi.kiwoom.com":
        raise LiveTradingLocked("모의투자 서버(mockapi.kiwoom.com)로만 접속할 수 있습니다.")
    return url


def regular_session_open(moment: datetime) -> bool:
    """평일 09:00~15:30 한국 시각. 휴장일은 여기서 거르지 않는다."""
    if moment.weekday() >= 5:
        return False
    clock = moment.time()
    return clock_time(9, 0) <= clock < clock_time(15, 30)


def _same_order(left: object, right: object) -> bool:
    def norm(value: object) -> str:
        text = str(value or "").strip()
        return text.lstrip("0") or "0"

    return norm(left) == norm(right)


def _fill_from_event(event: dict, order_no: str) -> dict | None:
    if event.get("type") != "00" or not _same_order(event.get("order_no"), order_no):
        return None
    status = str(event.get("status") or "")
    if "거부" in status:
        print(f"주문 거부 {event.get('reject_reason') or status}")
        return {"order_no": order_no, "qty": 0, "price": 0, "side": "", "rejected": True}
    qty = int(event.get("fill_qty") or 0)
    price = int(event.get("fill_price") or 0)
    if "체결" not in status or qty < 1 or price < 1:
        return None
    side = "sell" if str(event.get("side_flag")) in {"1", "매도"} else "buy"
    return {"order_no": order_no, "qty": qty, "price": price, "side": side}


class FillListener:
    """주문보다 먼저 00을 등록해 두고, 그 소켓에서 체결을 읽는다."""

    def __init__(self, client, connect=None):
        self.client = client
        self._connect = connect
        self.sock = None

    def start(self) -> None:
        from daytrading.kiwoom import ws_login_packet
        from daytrading.paper import connect_websocket

        assert_paper_endpoint(self.client.ws_url)
        opener = self._connect or connect_websocket
        self.sock = opener(self.client.ws_url, timeout=10)
        self.sock.settimeout(2)
        self.sock.send(json.dumps(ws_login_packet(self.client.token)))
        self.sock.send(
            json.dumps(
                {
                    "trnm": "REG",
                    "grp_no": "1",
                    "refresh": "1",
                    "data": [{"item": [], "type": ["00"]}],
                }
            )
        )

    def wait(self, _client, order_no: str, timeout: float) -> dict | None:
        from daytrading.kiwoom import parse_real_messages

        if self.sock is None:
            return None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                raw = self.sock.recv()
            except TimeoutError:
                continue
            except Exception as exc:
                if "Timeout" in type(exc).__name__:
                    continue
                raise
            if not raw:
                return None
            message = json.loads(raw)
            if str(message.get("trnm", "")).upper() == "PING":
                self.sock.send(json.dumps({"trnm": "PING"}))
                continue
            for event in parse_real_messages(message):
                found = _fill_from_event(event, order_no)
                if found is not None:
                    return found
        return None

    def close(self) -> None:
        close = getattr(self.sock, "close", None)
        if close:
            close()
        self.sock = None


def run_smoke_roundtrip(
    client,
    *,
    market_open: bool,
    wait_fill,
    code: str = SMOKE_CODE,
    timeout: float = 20,
) -> dict:
    """1주 시장가 매수 후, 장중 체결이 있으면 그 수량만 시장가로 판다."""
    assert_paper_endpoint(client.base_url)
    ws_url = getattr(client, "ws_url", "")
    if ws_url:
        assert_paper_endpoint(ws_url)
    report = {
        "buy_order_no": "",
        "buy_fill_price": 0,
        "buy_fill_qty": 0,
        "sell_order_no": "",
        "sell_fill_price": 0,
    }
    buy = client.place("buy", code, SMOKE_QTY, 0, True)
    order_no = str(buy.get("ord_no") or "")
    report["buy_order_no"] = order_no
    print(f"매수 주문번호 {order_no}")
    fill = wait_fill(client, order_no, timeout) if order_no else None
    qty = int(fill.get("qty") or 0) if fill else 0
    if qty > 0:
        report["buy_fill_qty"] = qty
        report["buy_fill_price"] = int(fill.get("price") or 0)
        print(f"매수 체결 {qty}주 {report['buy_fill_price']}원")
    else:
        print("매수 체결 통보가 없습니다.")
    if not market_open or qty < 1:
        if not market_open:
            print("장이 아니라 매도로 정리하지 않습니다.")
        return report
    assert_paper_endpoint(client.base_url)
    sell = client.place("sell", code, qty, 0, True)
    sell_no = str(sell.get("ord_no") or "")
    report["sell_order_no"] = sell_no
    print(f"매도 주문번호 {sell_no}")
    if sell_no:
        sold = wait_fill(client, sell_no, timeout)
        sold_qty = int(sold.get("qty") or 0) if sold else 0
        if sold_qty > 0:
            report["sell_fill_price"] = int(sold.get("price") or 0)
            print(f"매도 체결 {sold_qty}주 {report['sell_fill_price']}원")
        else:
            print("매도 체결 통보가 없습니다.")
    return report


def run_smoke(home: Path) -> int:
    from daytrading.kiwoom import KiwoomClient, KiwoomError
    from daytrading.secrets import load_secrets, missing_secret_names

    secrets = load_secrets(home)
    missing = [name for name in missing_secret_names(secrets) if name != "KIWOOM_ACCOUNT_NO"]
    if missing:
        print("모의투자 키가 없습니다. " + ", ".join(missing), file=sys.stderr)
        return 1
    try:
        client = KiwoomClient(secrets["KIWOOM_APP_KEY"], secrets["KIWOOM_APP_SECRET"], mode="paper")
        assert_paper_endpoint(client.base_url)
        assert_paper_endpoint(client.ws_url)
        client.issue_token()
        now = datetime.now(KST).replace(microsecond=0)
        open_now = regular_session_open(now)
        print(f"시각 {now.strftime('%Y-%m-%d %H:%M:%S')} 장중={open_now}")
        print(f"모의투자 서버에 {SMOKE_CODE} {SMOKE_QTY}주 시장가 매수를 보냅니다.")
        listener = FillListener(client)
        listener.start()
        try:
            run_smoke_roundtrip(client, market_open=open_now, wait_fill=listener.wait)
        finally:
            listener.close()
    except (LiveTradingLocked, KiwoomError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0
