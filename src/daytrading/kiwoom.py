"""키움 REST 어댑터.

호스트, 경로, 헤더, 주문 필드, 웹소켓 패킷은 키움 공식 예제
(https://github.com/Kiwoom-Securities/Kiwoom-REST-API)와 같은 값만 쓴다.
공식 예제에 없는 해석은 TODO로 남긴다. v0는 실전 호스트로 접속하지 않는다.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit

PAPER_REST = "https://mockapi.kiwoom.com"
LIVE_REST = "https://api.kiwoom.com"
PAPER_WS = "wss://mockapi.kiwoom.com:10000"
LIVE_WS = "wss://api.kiwoom.com:10000"
WS_PATH = "/api/dostk/websocket"
TOKEN_PATH = "/oauth2/token"
ORDER_PATH = "/api/dostk/ordr"
RANK_PATH = "/api/dostk/rkinfo"
STOCK_PATH = "/api/dostk/stkinfo"

# 국내주식 > 주문. trde_tp는 공식 buy_domestic_stock.py 주석.
TR_BUY = "kt10000"
TR_SELL = "kt10001"
TR_MODIFY = "kt10002"
TR_CANCEL = "kt10003"
TR_TOKEN = "au10001"
TR_VOLUME_SURGE = "ka10023"
TR_CHANGE_RANK = "ka10027"
TR_STOCK_INFO = "ka10001"
TR_SYMBOL_LIST = "ka10099"
TR_CONDITION_LIST = "ka10171"
TR_CONDITION_REAL = "ka10173"
TR_CONDITION_STOP = "ka10174"

# 지정가 IOC / 시장가. 공식 예제의 trde_tp 설명.
LIMIT_IOC = "10"
MARKET = "3"


class LiveTradingLocked(RuntimeError):
    """v0는 실전 주소를 열지 않는다."""


ORDER_API_IDS = frozenset({TR_BUY, TR_SELL, TR_MODIFY, TR_CANCEL})


def require_paper_order_url(url: str | None) -> None:
    """주문 URL의 호스트가 mockapi.kiwoom.com 과 글자가 같을 때만 통과한다.

    대소문자, 끝의 점, 포트, 다른 스킴, 사용자 정보, 서브도메인 변형은 거절한다.
    설정의 live_trading_enabled 는 보지 않는다.
    """
    text = "" if url is None else str(url).strip()
    parts = urlsplit(text)
    allowed = (
        parts.scheme == "https"
        and parts.port is None
        and not parts.username
        and not parts.password
        and parts.netloc == "mockapi.kiwoom.com"
    )
    if not allowed:
        raise LiveTradingLocked("모의투자 서버(mockapi.kiwoom.com)로만 주문할 수 있습니다.")


class KiwoomError(RuntimeError):
    pass


def normalize_code(code: str) -> str:
    text = code.strip().upper()
    for suffix in ("_AL", "_NX"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    if text.startswith("A") and len(text) == 7:
        text = text[1:]
    return text


# 주식체결(0B) FID 14 누적거래대금. 공식 명세 description은 "단위: 백만원".
ACC_VALUE_UNIT_KRW = 1_000_000


def signed_price(value: Any) -> int | None:
    """현재가 FID는 '+82000' '-82000'처럼 부호가 붙는다. 가격은 절대값이다."""
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    digits = text[1:] if text[0] in "+-" else text
    if not digits.replace(".", "", 1).isdigit():
        return None
    return int(abs(float(digits)))


def trading_value_krw(value: Any) -> int | None:
    """0B 14를 원으로 바꾼다. 명세의 단위는 백만원이고 설정 하한은 원이다."""
    raw = signed_price(value)
    if raw is None:
        return None
    return raw * ACC_VALUE_UNIT_KRW


def order_body(
    *,
    code: str,
    qty: int,
    limit_price: int,
    market: bool,
    exchange: str = "KRX",
) -> dict[str, str]:
    return {
        "dmst_stex_tp": exchange,
        "stk_cd": normalize_code(code),
        "ord_qty": str(int(qty)),
        "trde_tp": MARKET if market else LIMIT_IOC,
        "ord_uv": "" if market else str(int(limit_price)),
        "cond_uv": "",
    }


def request_headers(token: str, api_id: str, cont_yn: str = "N", next_key: str = "") -> dict[str, str]:
    return {
        "Content-Type": "application/json;charset=UTF-8",
        "authorization": f"Bearer {token}",
        "api-id": api_id,
        "cont-yn": cont_yn,
        "next-key": next_key,
    }


def token_body(app_key: str, app_secret: str) -> dict[str, str]:
    return {
        "grant_type": "client_credentials",
        "appkey": app_key,
        "secretkey": app_secret,
    }


def ws_login_packet(token: str) -> dict[str, str]:
    return {"trnm": "LOGIN", "token": token}


def ws_reg_packet(codes: list[str], types: list[str], grp_no: str = "1") -> dict[str, Any]:
    return {
        "trnm": "REG",
        "grp_no": grp_no,
        "refresh": "1",
        "data": [{"item": [normalize_code(code) for code in codes], "type": types}],
    }


def condition_list_packet() -> dict[str, str]:
    return {"trnm": "CNSRLST"}


def condition_realtime_packet(seq: str) -> dict[str, str]:
    return {"trnm": "CNSRREQ", "seq": str(seq), "search_type": "1", "stex_tp": "K"}


def condition_stop_packet(seq: str) -> dict[str, str]:
    return {"trnm": "CNSRCLR", "seq": str(seq)}


def volume_surge_body() -> dict[str, str]:
    """ka10023 공식 예제 __main__ 과 같은 값. 종목조건 0은 전체조회다."""
    return {
        "mrkt_tp": "000",
        "sort_tp": "1",
        "tm_tp": "2",
        "trde_qty_tp": "5",
        "stk_cnd": "0",
        "pric_tp": "0",
        "stex_tp": "3",
        "tm": "",
    }


def change_rank_body() -> dict[str, str]:
    """ka10027 공식 예제 __main__ 과 같은 값."""
    return {
        "mrkt_tp": "000",
        "sort_tp": "1",
        "trde_qty_cnd": "0000",
        "stk_cnd": "0",
        "crd_cnd": "0",
        "updown_incls": "1",
        "pric_cnd": "0",
        "trde_prica_cnd": "0",
        "stex_tp": "3",
    }


def extract_codes(payload: Any) -> list[str]:
    """순위 응답에서 stk_cd만 모은다. 목록 키 이름은 예제마다 달라서 값 이름으로 찾는다."""
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in {"stk_cd", "code"} and isinstance(value, str) and value.strip():
                    found.append(normalize_code(value))
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    unique: list[str] = []
    for code in found:
        if code and code not in unique:
            unique.append(code)
    return unique


def parse_real_messages(message: dict[str, Any]) -> list[dict[str, Any]]:
    """REAL 패킷을 이벤트 목록으로 푼다.

    공식 디코더가 적은 모양: {trnm: REAL, data: [{type, item, values}]}.
    values 키는 0B/0D/1h/00 예제의 FID다.
    """
    if str(message.get("trnm", "")).upper() != "REAL":
        return []
    events = []
    for entry in message.get("data") or []:
        if not isinstance(entry, dict):
            continue
        values = entry.get("values") if isinstance(entry.get("values"), dict) else {}
        real_type = str(entry.get("type", ""))
        code = normalize_code(str(entry.get("item", "")))
        event: dict[str, Any] = {"type": real_type, "code": code, "raw": values}
        if real_type == "0B":
            event.update(
                {
                    "price": signed_price(values.get("10")),
                    "open": signed_price(values.get("16")),
                    "high": signed_price(values.get("17")),
                    "low": signed_price(values.get("18")),
                    "acc_value": trading_value_krw(values.get("14")),
                    "acc_volume": signed_price(values.get("13")),
                    "strength": _float(values.get("228")),
                    "best_ask": signed_price(values.get("27")),
                    "best_bid": signed_price(values.get("28")),
                    "change_pct": _float(values.get("12")),
                }
            )
        elif real_type == "0D":
            event.update(
                {
                    "best_ask": signed_price(values.get("41")),
                    "best_bid": signed_price(values.get("51")),
                    "ask_qty": signed_price(values.get("121")) or 0,
                    "bid_qty": signed_price(values.get("125")) or 0,
                }
            )
        elif real_type == "1h":
            # TODO: 9068(VI발동구분)의 해제 코드는 공식 예제에 값 표가 없다.
            # 1224(VI해제시각)가 채워지면 해제로 보고, 그 전까지는 발동으로만 표시한다.
            event.update(
                {
                    "code": normalize_code(str(values.get("9001") or code)),
                    "vi_price": signed_price(values.get("1221")) or 0,
                    "vi_flag": str(values.get("9068", "")),
                    "vi_release_time": str(values.get("1224", "") or ""),
                    "vi_active": not str(values.get("1224", "") or "").strip(),
                }
            )
        elif real_type == "00":
            event.update(
                {
                    "order_no": str(values.get("9203", "")),
                    "code": normalize_code(str(values.get("9001") or code)),
                    "fill_price": signed_price(values.get("910")),
                    "fill_qty": signed_price(values.get("911")) or 0,
                    "side_flag": str(values.get("907", "")),
                    "status": str(values.get("913", "")),
                    "reject_reason": str(values.get("919", "")),
                }
            )
        events.append(event)
    return events


def _response_headers(response: Any) -> dict[str, str]:
    raw = getattr(response, "headers", None)
    if raw is None:
        return {}
    items = raw.items() if hasattr(raw, "items") else ()
    return {str(key).lower(): str(value) for key, value in items}


def _float(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip().replace(",", "")
    try:
        return float(text)
    except ValueError:
        return None


class KiwoomClient:
    """모의투자 REST. 네트워크는 호출자가 명시적으로 요청할 때만 연다."""

    def __init__(self, app_key: str, app_secret: str, mode: str = "paper", opener=None):
        if mode != "paper":
            raise LiveTradingLocked("v0에서는 실전 주소(api.kiwoom.com)로 접속할 수 없습니다.")
        if not app_key or not app_secret:
            raise KiwoomError("모의투자 앱 키와 시크릿이 없습니다.")
        self.mode = "paper"
        self.app_key = app_key
        self.app_secret = app_secret
        self.base_url = PAPER_REST
        self.ws_url = PAPER_WS + WS_PATH
        self.token = ""
        self._opener = opener or urllib.request.urlopen

    def issue_token(self) -> str:
        payload = self._post(TOKEN_PATH, token_body(self.app_key, self.app_secret), api_id=TR_TOKEN, auth=False)
        token = str(payload.get("token") or "")
        if not token:
            raise KiwoomError("토큰 응답에 token이 없습니다.")
        self.token = token
        return token

    def place(self, intent_side: str, code: str, qty: int, limit_price: int, market: bool) -> dict[str, Any]:
        api_id = TR_BUY if intent_side == "buy" else TR_SELL
        body = order_body(code=code, qty=qty, limit_price=limit_price, market=market)
        return self._post(ORDER_PATH, body, api_id=api_id, auth=True)

    def modify(self, code: str, orig_ord_no: str, qty: int, limit_price: int, exchange: str = "KRX") -> dict[str, Any]:
        """kt10002 공식 예제와 같은 필드. 호스트 검사는 전송 직전에 한다."""
        body = {
            "dmst_stex_tp": exchange,
            "orig_ord_no": str(orig_ord_no),
            "stk_cd": normalize_code(code),
            "mdfy_qty": str(int(qty)),
            "mdfy_uv": str(int(limit_price)),
            "mdfy_cond_uv": "",
        }
        return self._post(ORDER_PATH, body, api_id=TR_MODIFY, auth=True)

    def cancel(self, code: str, orig_ord_no: str, qty: int, exchange: str = "KRX") -> dict[str, Any]:
        """kt10003 공식 예제와 같은 필드. 호스트 검사는 전송 직전에 한다."""
        body = {
            "dmst_stex_tp": exchange,
            "orig_ord_no": str(orig_ord_no),
            "stk_cd": normalize_code(code),
            "cncl_qty": str(int(qty)),
        }
        return self._post(ORDER_PATH, body, api_id=TR_CANCEL, auth=True)

    def volume_rank(self) -> dict[str, Any]:
        return self._post(RANK_PATH, volume_surge_body(), api_id=TR_VOLUME_SURGE, auth=True)

    def change_rank(self) -> dict[str, Any]:
        return self._post(RANK_PATH, change_rank_body(), api_id=TR_CHANGE_RANK, auth=True)

    def stock_info(self, code: str) -> dict[str, Any]:
        # ka10001 공식 응답에는 관리·투자경고·ETF·상장일이 없다. 그 값은 ka10099 로 받는다.
        return self._post(STOCK_PATH, {"stk_cd": normalize_code(code)}, api_id=TR_STOCK_INFO, auth=True)

    def symbol_list_page(
        self,
        mrkt_tp: str,
        *,
        cont_yn: str = "N",
        next_key: str = "",
    ) -> tuple[list[dict[str, Any]], str, str]:
        """ka10099 한 페이지. 이어 받으려면 응답 헤더 cont-yn, next-key 를 그대로 넘긴다."""
        payload, headers = self._exchange(
            STOCK_PATH,
            {"mrkt_tp": str(mrkt_tp)},
            api_id=TR_SYMBOL_LIST,
            auth=True,
            cont_yn=cont_yn,
            next_key=next_key,
            timeout=30,
        )
        rows = payload.get("list")
        if not isinstance(rows, list):
            raise KiwoomError("종목정보 리스트 응답에 list가 없습니다.")
        items = [row for row in rows if isinstance(row, dict)]
        return items, headers.get("cont-yn", "N"), headers.get("next-key", "")

    def _post(self, path: str, body: dict, api_id: str, auth: bool) -> dict[str, Any]:
        payload, _headers = self._exchange(path, body, api_id=api_id, auth=auth)
        return payload

    def _exchange(
        self,
        path: str,
        body: dict,
        api_id: str,
        auth: bool,
        cont_yn: str = "N",
        next_key: str = "",
        timeout: float = 10,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        data = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json;charset=UTF-8", "api-id": api_id}
        if auth:
            if not self.token:
                raise KiwoomError("토큰이 없습니다.")
            headers = request_headers(self.token, api_id, cont_yn=cont_yn, next_key=next_key)
        ordering = api_id in ORDER_API_IDS or path == ORDER_PATH
        if ordering:
            require_paper_order_url(self.base_url)
        request = urllib.request.Request(f"{self.base_url}{path}", data=data, headers=headers, method="POST")
        if ordering:
            require_paper_order_url(request.full_url)
        try:
            with self._opener(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
                response_headers = _response_headers(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise KiwoomError(f"키움 호출 실패 ({exc.code}): {detail[:300]}") from exc
        except urllib.error.URLError as exc:
            raise KiwoomError(f"키움 서버에 연결하지 못했습니다: {exc.reason}") from exc
        if isinstance(payload, dict) and payload.get("return_code") not in (None, 0, "0"):
            raise KiwoomError(str(payload.get("return_msg") or payload.get("return_code")))
        if not isinstance(payload, dict):
            raise KiwoomError("키움 응답이 객체가 아닙니다.")
        return payload, response_headers


def check_live_order_block() -> int:
    """실전 주소로 place()를 호출해 요청 전에 거절되는지만 본다. 키와 네트워크는 쓰지 않는다."""
    import sys

    opened: list[str] = []

    def opener(request, timeout=10):
        opened.append(getattr(request, "full_url", ""))
        raise RuntimeError("주문을 보내면 안 됩니다.")

    client = KiwoomClient("unused", "unused", mode="paper", opener=opener)
    client.token = "unused"
    client.base_url = LIVE_REST
    try:
        client.place("buy", "005930", 1, 0, True)
    except LiveTradingLocked as exc:
        if opened:
            print("실전 주소로 주문 요청이 나갔습니다.", file=sys.stderr)
            return 1
        print(f"실전 주문 거절: {exc}")
        print(f"주소 {LIVE_REST} 로는 주문을 보내지 않았습니다.")
        return 0
    print("실전 주소로 주문이 허용되었습니다.", file=sys.stderr)
    return 1
