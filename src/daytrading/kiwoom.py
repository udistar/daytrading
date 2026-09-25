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
TR_CONDITION_LIST = "ka10171"
TR_CONDITION_REAL = "ka10173"
TR_CONDITION_STOP = "ka10174"

# 지정가 IOC / 시장가. 공식 예제의 trde_tp 설명.
LIMIT_IOC = "10"
MARKET = "3"


class LiveTradingLocked(RuntimeError):
    """v0는 실전 주소를 열지 않는다."""


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
                    "acc_value": signed_price(values.get("14")),
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

    def volume_rank(self) -> dict[str, Any]:
        return self._post(RANK_PATH, volume_surge_body(), api_id=TR_VOLUME_SURGE, auth=True)

    def change_rank(self) -> dict[str, Any]:
        return self._post(RANK_PATH, change_rank_body(), api_id=TR_CHANGE_RANK, auth=True)

    def stock_info(self, code: str) -> dict[str, Any]:
        # TODO: ka10001 공식 컬럼에는 관리·투자경고·ETF·상장일이 없다.
        # 그 플래그는 symbols_meta.csv 로 넘긴다.
        return self._post(STOCK_PATH, {"stk_cd": normalize_code(code)}, api_id=TR_STOCK_INFO, auth=True)

    def _post(self, path: str, body: dict, api_id: str, auth: bool) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json;charset=UTF-8", "api-id": api_id}
        if auth:
            if not self.token:
                raise KiwoomError("토큰이 없습니다.")
            headers = request_headers(self.token, api_id)
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers, method="POST")
        try:
            with self._opener(request, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise KiwoomError(f"키움 호출 실패 ({exc.code}): {detail[:300]}") from exc
        except urllib.error.URLError as exc:
            raise KiwoomError(f"키움 서버에 연결하지 못했습니다: {exc.reason}") from exc
        if isinstance(payload, dict) and payload.get("return_code") not in (None, 0, "0"):
            raise KiwoomError(str(payload.get("return_msg") or payload.get("return_code")))
        return payload
