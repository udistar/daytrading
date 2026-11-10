"""모의 시세가 전략 숫자와 같은 단위인지 확인한다. 주문은 내지 않는다."""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

from daytrading.kiwoom import extract_codes, parse_real_messages, signed_price, volume_surge_body
from daytrading.settings import Settings
from daytrading.smoke import assert_paper_endpoint
from daytrading.strategy import entry_time_block
from daytrading.timeutil import KST


def _clock_line(moment: datetime, settings: Settings) -> str:
    start = settings.clock("entry_start").strftime("%H:%M:%S")
    end = settings.clock("entry_end").strftime("%H:%M:%S")
    verdict = entry_time_block(moment, settings) or "신규 진입 창 안"
    return f"시각 {moment.strftime('%H:%M:%S')} / 진입 {start}~{end} / 판정: {verdict}"


def build_diagnose_report(client, settings: Settings, now: datetime, trade_values: dict | None) -> str:
    """순위 조회 결과와 체결 한 건을 원 단위로 적는다. place()는 호출하지 않는다."""
    assert_paper_endpoint(client.base_url)
    ws_url = getattr(client, "ws_url", "")
    if ws_url:
        assert_paper_endpoint(ws_url)
    lines = [f"접속 {client.base_url}", _clock_line(now, settings)]
    lines.append(
        "1분 거래대금 최소 "
        f"{int(settings.minute_value_min_krw)}원 / 누적 최소 {int(settings.cumulative_value_min_krw)}원"
    )
    body = volume_surge_body()
    tm_label = {"1": "분", "2": "전일"}.get(body["tm_tp"], body["tm_tp"])
    lines.append(
        f"ka10023 tm_tp={body['tm_tp']} ({tm_label}) stk_cnd={body['stk_cnd']}. "
        "tm_tp 2는 전일 대비이고, stk_cnd 0은 전체조회라 관리종목이 들어온다. "
        f"paper 루프는 {int(settings.ranking_poll_seconds)}초마다 이 조회를 다시 부른다."
    )
    payload = client.volume_rank()
    codes = extract_codes(payload)
    lines.append("후보 " + (", ".join(codes[:10]) if codes else "(없음)"))
    lines.append("후보가 비거나 조회가 실패하면 ka10027(전일대비 등락률)을 보고, 그것도 없으면 005930을 본다.")
    values = trade_values or {}
    if not values:
        lines.append("주식체결(0B)을 받지 못했습니다.")
        return "\n".join(lines) + "\n"
    code = codes[0] if codes else "005930"
    event = parse_real_messages({"trnm": "REAL", "data": [{"type": "0B", "item": code, "values": values}]})[0]
    raw = str(values.get("14") or "")
    acc = event.get("acc_value")
    volume = signed_price(values.get("13"))
    price = signed_price(values.get("10"))
    lines.append(f"{code} 체결시각 {values.get('20') or ''} 현재가 {price}원")
    lines.append(f"FID 14 원문 {raw or '없음'} (단위: 백만원) / 환산 {acc if acc is not None else '없음'}원")
    if acc and volume:
        lines.append(f"누적거래량 {volume}주 / 대금÷거래량 {acc / volume:.0f}원")
    return "\n".join(lines) + "\n"


def _read_one_trade(client, code: str, timeout: float = 20) -> dict:
    from daytrading.kiwoom import ws_login_packet
    from daytrading.paper import connect_websocket

    assert_paper_endpoint(client.ws_url)
    sock = connect_websocket(client.ws_url, timeout=10)
    sock.settimeout(2)
    deadline = time.monotonic() + timeout
    try:
        sock.send(json.dumps(ws_login_packet(client.token)))
        sock.send(
            json.dumps(
                {
                    "trnm": "REG",
                    "grp_no": "1",
                    "refresh": "1",
                    "data": [{"item": [code], "type": ["0B"]}],
                }
            )
        )
        while time.monotonic() < deadline:
            try:
                raw = sock.recv()
            except TimeoutError:
                continue
            except Exception as exc:
                if "Timeout" in type(exc).__name__:
                    continue
                raise
            if not raw:
                return {}
            message = json.loads(raw)
            if str(message.get("trnm", "")).upper() == "PING":
                sock.send(json.dumps({"trnm": "PING"}))
                continue
            for entry in message.get("data") or []:
                if isinstance(entry, dict) and str(entry.get("type")) == "0B" and isinstance(entry.get("values"), dict):
                    return entry["values"]
        return {}
    finally:
        close = getattr(sock, "close", None)
        if close:
            close()


def run_diagnose(home: Path, settings: Settings) -> int:
    from daytrading.kiwoom import KiwoomClient, KiwoomError, LiveTradingLocked
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
        trade = _read_one_trade(client, "005930")
        print(build_diagnose_report(client, settings, now, trade), end="")
    except (KiwoomError, LiveTradingLocked) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0
