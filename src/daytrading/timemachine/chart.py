"""키움 정규장 분봉 조회.

공식 문서에서 확인한 값만 쓴다.
- ka11080 주식분봉차트정규장조회요청 (Time Machine 기본)
  https://openapi.kiwoom.com/guide/apiGuideContents/07/ka11080
- ka10080 주식분봉차트조회요청
  https://openapi.kiwoom.com/guide/apiGuideContents/07/ka10080
둘 다 POST /api/dostk/chart, 모의 호스트는 mockapi.kiwoom.com (문서: KRX만).
Body: stk_cd, tic_scope, upd_stkpc_tp, base_dt.
ka11080 응답 목록 키는 result_list, ka10080 은 stk_min_pole_chart_qry.
예시 JSON의 tic_scope 가 비어 있으나 Body 표는 필수라 표의 값을 보낸다.

TODO: 분봉을 최대 며칠까지 주는지 공식 문서에 없다. 미확인.
TODO: KIS 모의투자 어댑터는 후속 과제다. 이 프로젝트의 브로커는 키움 REST 모의투자다.

이 모듈은 장중 토큰버킷과 상태를 공유하지 않는다. 오프라인·장후 전용이다.
실전 호스트로는 열지 않는다. 키가 없으면 호출하지 않고 픽스처를 쓴다.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Callable

from daytrading.kiwoom import (
    PAPER_REST,
    LiveTradingLocked,
    normalize_code,
    request_headers,
    signed_price,
)
from daytrading.timeutil import KST
from daytrading.timemachine.bars import MinuteBar

CHART_PATH = "/api/dostk/chart"
TR_REGULAR_MINUTE = "ka11080"
TR_MINUTE = "ka10080"
LIST_KEYS = ("result_list", "stk_min_pole_chart_qry")

# 공식 문서에 보관 일수가 없다.
UNCONFIRMED_LOOKBACK = "분봉 조회가 며칠을 돌려주는지는 키움 공식 문서에서 확인하지 못했다."


class ChartError(RuntimeError):
    pass


def minute_chart_body(code: str, tic_scope: str, base_dt: str = "", adjusted: bool = False) -> dict[str, str]:
    body = {
        "stk_cd": normalize_code(code),
        "tic_scope": str(tic_scope),
        "upd_stkpc_tp": "1" if adjusted else "0",
    }
    if base_dt:
        body["base_dt"] = base_dt
    return body


def _is_live_host(host: str) -> bool:
    text = host.lower()
    return "api.kiwoom.com" in text and "mockapi.kiwoom.com" not in text


def _header(response: Any, name: str) -> str:
    headers = getattr(response, "headers", None)
    if headers is not None:
        value = headers.get(name)
        if value:
            return str(value)
        value = headers.get(name.title())
        if value:
            return str(value)
    getter = getattr(response, "getheader", None)
    if getter:
        return str(getter(name) or "")
    return ""


def parse_chart_payload(payload: dict[str, Any]) -> list[MinuteBar]:
    if payload.get("return_code") not in (None, 0, "0"):
        raise ChartError(str(payload.get("return_msg") or payload.get("return_code")))
    rows: list[dict[str, Any]] | None = None
    for key in LIST_KEYS:
        if isinstance(payload.get(key), list):
            rows = payload[key]
            break
    if rows is None:
        raise ChartError("분봉 목록(result_list 또는 stk_min_pole_chart_qry)이 없습니다.")
    bars: list[MinuteBar] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        stamp = str(row.get("cntr_tm") or "").strip()
        if len(stamp) < 14 or not stamp[:14].isdigit():
            continue
        start = datetime.strptime(stamp[:14], "%Y%m%d%H%M%S").replace(tzinfo=KST)
        open_price = signed_price(row.get("open_pric"))
        high_price = signed_price(row.get("high_pric"))
        low_price = signed_price(row.get("low_pric"))
        close_price = signed_price(row.get("cur_prc"))
        volume = signed_price(row.get("trde_qty")) or 0
        if None in (open_price, high_price, low_price, close_price):
            continue
        bars.append(
            MinuteBar(
                start=start,
                open=int(open_price),
                high=int(high_price),
                low=int(low_price),
                close=int(close_price),
                volume=int(volume),
            )
        )
    unique: dict[datetime, MinuteBar] = {}
    for bar in bars:
        unique[bar.start] = bar
    return [unique[stamp] for stamp in sorted(unique)]


def fetch_minute_bars(
    opener: Callable,
    *,
    token: str,
    code: str,
    tic_scope: str,
    base_dt: str = "",
    host: str = PAPER_REST,
    api_id: str = TR_REGULAR_MINUTE,
    min_interval: float = 1.0,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
    adjusted: bool = False,
) -> list[MinuteBar]:
    """연속조회로 분봉을 모은다. 모의 호스트만 허용하고, 호출 간격은 따로 둔다."""
    if _is_live_host(host):
        raise LiveTradingLocked("v0에서는 실전 주소(api.kiwoom.com)로 접속할 수 없습니다.")
    if api_id not in {TR_REGULAR_MINUTE, TR_MINUTE}:
        raise ChartError("분봉 TR은 ka11080 또는 ka10080 만 사용합니다.")
    pause = sleep or (lambda _seconds: None)
    now = clock or (lambda: 0.0)
    collected: list[MinuteBar] = []
    cont_yn = "N"
    next_key = ""
    last_at: float | None = None
    seen_keys: set[str] = set()
    while True:
        if last_at is not None and min_interval > 0:
            wait = min_interval - (now() - last_at)
            if wait > 0:
                pause(wait)
        body = minute_chart_body(code, tic_scope, base_dt=base_dt, adjusted=adjusted)
        data = json.dumps(body).encode("utf-8")
        headers = request_headers(token, api_id, cont_yn=cont_yn, next_key=next_key)
        request = urllib.request.Request(host.rstrip("/") + CHART_PATH, data=data, headers=headers, method="POST")
        try:
            with opener(request, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
                response_cont = _header(response, "cont-yn") or _header(response, "Cont-yn")
                response_key = _header(response, "next-key") or _header(response, "Next-key")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ChartError(f"키움 분봉 호출 실패 ({exc.code}): {detail[:300]}") from exc
        except urllib.error.URLError as exc:
            raise ChartError(f"키움 모의 서버에 연결하지 못했습니다: {exc.reason}") from exc
        last_at = now()
        if not isinstance(payload, dict):
            raise ChartError("분봉 응답이 객체가 아닙니다.")
        collected.extend(parse_chart_payload(payload))
        if str(response_cont).upper() != "Y" or not response_key:
            break
        marker = f"{response_cont}:{response_key}"
        if marker in seen_keys:
            break
        seen_keys.add(marker)
        cont_yn = "Y"
        next_key = response_key
    unique: dict[datetime, MinuteBar] = {}
    for bar in collected:
        unique[bar.start] = bar
    return [unique[stamp] for stamp in sorted(unique)]
