"""키움 모의투자 접속.

실전 호스트는 KiwoomClient가 열지 않는다. 연결 확인은 토큰과 시세 등록만 하고
주문을 내지 않는다. --paper 는 같은 엔진으로 모의투자 주문까지 보낸다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from daytrading.journal import Journal
from daytrading.kiwoom import (
    LIVE_REST,
    KiwoomClient,
    KiwoomError,
    condition_realtime_packet,
    extract_codes,
    parse_real_messages,
    ws_login_packet,
    ws_reg_packet,
)
from daytrading.ledger import apply_fill
from daytrading.meta import FLAG_FIELDS, load_symbol_meta
from daytrading.models import Fill, Intent, OrderRecord, Snapshot, SymbolMeta
from daytrading.secrets import load_secrets, missing_secret_names
from daytrading.session import Engine
from daytrading.settings import Settings, price_band
from daytrading.timeutil import KST


@dataclass
class CheckResult:
    ok: bool
    message: str
    token_ok: bool = False
    login_ok: bool = False
    subscribe_ok: bool = False
    orders_sent: int = 0


class KiwoomBroker:
    """모의투자 REST 주문. dry_run 이면 place()를 호출하지 않는다."""

    def __init__(self, client: KiwoomClient, dry_run: bool = False):
        self.client = client
        self.dry_run = dry_run
        self.place_calls = 0
        self.triggers: dict[tuple[str, str], int] = {}
        if LIVE_REST == client.base_url:
            raise KiwoomError("실전 주소로는 주문할 수 없습니다.")

    def submit(self, order_id: str, intent: Intent, snap: Snapshot, settings: Settings) -> tuple[OrderRecord, Fill | None]:
        record = OrderRecord(
            order_id=order_id,
            ts=snap.ts,
            code=intent.code,
            name=intent.name,
            side=intent.side,
            reason=intent.reason,
            qty=intent.qty,
            order_type=intent.order_type,
            limit_price=intent.limit_price,
            status="dry_run" if self.dry_run else "accepted",
            note=intent.note,
        )
        trigger = intent.trigger_price or (intent.limit_price if intent.side == "buy" else 0)
        self.triggers[(intent.code, intent.side)] = trigger
        if self.dry_run:
            return record, None
        self.place_calls += 1
        payload = self.client.place(
            intent.side,
            intent.code,
            intent.qty,
            intent.limit_price,
            market=intent.order_type == "market",
        )
        record.broker_order_no = str(payload.get("ord_no") or "")
        return record, None


def connect_websocket(url: str, timeout: float = 10):
    import websocket

    return websocket.create_connection(url, timeout=timeout)


def _message(raw: str) -> dict[str, Any]:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        return {}
    return payload


def _ok_code(message: dict[str, Any]) -> bool:
    code = message.get("return_code")
    return code in (None, 0, "0")


def _read(sock, predicate: Callable[[dict[str, Any]], bool], limit: int = 8) -> dict[str, Any] | None:
    for _ in range(limit):
        raw = sock.recv()
        if not raw:
            return None
        message = _message(raw)
        if str(message.get("trnm", "")).upper() == "PING":
            sock.send(json.dumps({"trnm": "PING"}))
            continue
        if predicate(message):
            return message
    return None


def check_connection(
    settings: Settings,
    app_key: str,
    app_secret: str,
    *,
    opener=None,
    connect: Callable[..., Any] | None = None,
    probe_code: str = "005930",
) -> CheckResult:
    """토큰 발급과 웹소켓 시세 등록만 확인한다. 주문 API는 호출하지 않는다."""
    try:
        client = KiwoomClient(app_key, app_secret, mode="paper", opener=opener)
    except KiwoomError as exc:
        return CheckResult(False, str(exc))
    try:
        client.issue_token()
    except KiwoomError as exc:
        return CheckResult(False, f"토큰 발급 실패: {exc}")
    socket_factory = connect or connect_websocket
    try:
        sock = socket_factory(client.ws_url)
    except Exception as exc:  # noqa: BLE001 - show the connection error to the user
        return CheckResult(False, f"웹소켓 연결 실패: {exc}", token_ok=True)
    try:
        sock.send(json.dumps(ws_login_packet(client.token)))
        login = _read(sock, lambda message: str(message.get("trnm", "")).upper() == "LOGIN")
        if login is None or not _ok_code(login):
            detail = "" if login is None else str(login.get("return_msg") or login)
            return CheckResult(False, f"웹소켓 로그인 실패: {detail}", token_ok=True)
        types = ["0B", "0D"]
        sock.send(json.dumps(ws_reg_packet([probe_code], types)))
        registered = _read(
            sock,
            lambda message: str(message.get("trnm", "")).upper() in {"REG", "REAL"},
        )
        if registered is None or not _ok_code(registered):
            return CheckResult(False, "시세 등록 응답이 없습니다.", token_ok=True, login_ok=True)
        for seq in [part.strip() for part in settings.condition_seqs.split(",") if part.strip()][: settings.condition_max]:
            sock.send(json.dumps(condition_realtime_packet(seq)))
    except Exception as exc:  # noqa: BLE001
        return CheckResult(False, f"시세 등록 실패: {exc}", token_ok=True, login_ok=True)
    finally:
        close = getattr(sock, "close", None)
        if close:
            close()
    return CheckResult(
        True,
        f"모의투자 접속 확인. 토큰 발급과 {probe_code} 시세 등록까지 끝났고 주문은 내지 않았습니다.",
        token_ok=True,
        login_ok=True,
        subscribe_ok=True,
        orders_sent=0,
    )


def apply_realtime(engine: Engine, message: dict[str, Any], now: datetime) -> list[str]:
    """웹소켓 REAL을 엔진 시세·체결로 반영한다. 돌려주는 목록은 다시 보낼 패킷이 있을 때."""
    replies: list[str] = []
    if str(message.get("trnm", "")).upper() == "PING":
        replies.append(json.dumps({"trnm": "PING"}))
        return replies
    for event in parse_real_messages(message):
        if event["type"] == "0B" and event.get("price"):
            _apply_trade(engine, event, now)
        elif event["type"] == "0D":
            _apply_quote(engine, event)
        elif event["type"] == "1h":
            _apply_vi(engine, event, now)
        elif event["type"] == "00" and event.get("fill_qty"):
            _apply_notice(engine, event, now)
    return replies


def _learn_reference(book, event: dict[str, Any], settings: Settings) -> None:
    """0B의 시가(16)와 등락률(12)로 전일종가·상한가를 채운다. 이미 있으면 유지한다."""
    price = int(event.get("price") or 0)
    opened = event.get("open")
    if opened and book.meta.day_open <= 0:
        book.meta.day_open = int(opened)
    if book.meta.prev_close <= 0 and price > 0 and event.get("change_pct") is not None:
        factor = 1 + float(event["change_pct"]) / 100
        if factor > 0:
            book.meta.prev_close = max(1, int(round(price / factor)))
    if book.meta.prev_close > 0 and book.meta.upper_limit <= 0:
        _lower, upper = price_band(book.meta.prev_close, settings)
        book.meta.upper_limit = upper


def _apply_trade(engine: Engine, event: dict[str, Any], now: datetime) -> None:
    code = event["code"]
    book = engine.market.books.get(code)
    if book is None:
        return
    _learn_reference(book, event, engine.settings)
    price = int(event["price"])
    engine.market.update(
        book.meta,
        ts=now,
        price=price,
        acc_value=int(event.get("acc_value") or book.acc_value),
        strength=event.get("strength"),
        best_bid=int(event.get("best_bid") or price),
        best_ask=int(event.get("best_ask") or price),
        bid_qty=book.bid_qty,
        ask_qty=book.ask_qty,
        vi_active=book.vi_active,
        vi_price=book.vi_price,
    )
    engine.prices[code] = price


def _apply_quote(engine: Engine, event: dict[str, Any]) -> None:
    book = engine.market.books.get(event["code"])
    if book is None:
        return
    if event.get("best_bid"):
        book.best_bid = int(event["best_bid"])
    if event.get("best_ask"):
        book.best_ask = int(event["best_ask"])
    if event.get("bid_qty"):
        book.bid_qty = int(event["bid_qty"])
    if event.get("ask_qty"):
        book.ask_qty = int(event["ask_qty"])
    if book.last_price:
        book.dirty = True


def _apply_vi(engine: Engine, event: dict[str, Any], now: datetime) -> None:
    book = engine.market.books.get(event["code"])
    if book is None:
        return
    active = bool(event.get("vi_active"))
    if active and not book.vi_active:
        book.vi_released_at = None
    if book.vi_active and not active:
        book.vi_released_at = now
    book.vi_active = active
    if event.get("vi_price"):
        book.vi_price = int(event["vi_price"])
    book.dirty = True


def _apply_notice(engine: Engine, event: dict[str, Any], now: datetime) -> None:
    status = str(event.get("status") or "")
    if status and "체결" not in status and status not in {"0", "00"}:
        # TODO: 00 체결통보의 913 상태코드 표는 예제에 값 설명이 없다. 체결이 아닌 값은 잔고에 넣지 않는다.
        return
    code = event["code"]
    qty = int(event.get("fill_qty") or 0)
    price = int(event.get("fill_price") or 0)
    if qty <= 0 or price <= 0:
        return
    side = "sell" if str(event.get("side_flag")) in {"1", "2", "매도"} else "buy"
    # 907 매도수구분의 숫자는 예제에 표가 없다. 대기 중인 쪽과 맞으면 그 방향을 쓴다.
    if code in engine.portfolio.pending_sells and code not in engine.portfolio.pending_buys:
        side = "sell"
    elif code in engine.portfolio.pending_buys and code not in engine.portfolio.pending_sells:
        side = "buy"
    trigger = price
    broker = engine.broker
    remembered = getattr(broker, "triggers", {}).get((code, side))
    if remembered:
        trigger = int(remembered)
    notional = price * qty
    fee = int(round(notional * engine.settings.commission_rate_pct / 100))
    tax = int(round(notional * engine.settings.sell_tax_rate_pct / 100)) if side == "sell" else 0
    slip = (trigger - price) * qty if side == "sell" else (price - trigger) * qty
    name = code
    book = engine.market.books.get(code)
    if book is not None and book.meta.name:
        name = book.meta.name
    fill = Fill(
        order_id=str(event.get("order_no") or "EX"),
        ts=now,
        code=code,
        name=name,
        side=side,
        reason="exchange",
        qty=qty,
        price=price,
        fee_krw=fee,
        tax_krw=tax,
        trigger_price=trigger,
        slippage_krw=slip,
    )
    apply_fill(engine.portfolio, fill, engine.settings)
    engine.journal.fill(fill)
    engine.prices[code] = price


def run_paper(
    settings: Settings,
    journal: Journal,
    app_key: str,
    app_secret: str,
    *,
    dry_run: bool = False,
    opener=None,
    connect: Callable[..., Any] | None = None,
    max_loops: int | None = None,
    codes: list[str] | None = None,
    meta_path: Path | None = None,
    controls: dict | None = None,
    should_stop: Callable[[], bool] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> Engine:
    """모의투자 시세를 같은 엔진에 넣고, dry_run이 아니면 모의 주문만 전송한다."""
    client = KiwoomClient(app_key, app_secret, mode="paper", opener=opener)
    client.issue_token()
    broker = KiwoomBroker(client, dry_run=dry_run)
    engine = Engine(settings, journal, "paper", broker=broker)
    pinned = codes is not None
    watched = _select_codes(client, settings, codes)
    _watch(engine, watched, meta_path)
    socket_factory = connect or connect_websocket
    sock = socket_factory(client.ws_url)
    try:
        sock.send(json.dumps(ws_login_packet(client.token)))
        _read(sock, lambda message: str(message.get("trnm", "")).upper() == "LOGIN")
        if watched:
            sock.send(json.dumps(ws_reg_packet(watched, ["0B", "0D", "1h", "00"])))
        _send_conditions(sock, settings)
        last_rank_at = (clock or _now)()
        loops = 0
        while max_loops is None or loops < max_loops:
            if should_stop and should_stop():
                break
            loops += 1
            try:
                raw = sock.recv()
            except TimeoutError:
                break
            if not raw:
                break
            message = _message(raw)
            now = (clock or _now)()
            if not pinned and (now - last_rank_at).total_seconds() >= float(settings.ranking_poll_seconds):
                watched = _select_codes(client, settings, None)
                _watch(engine, watched, meta_path)
                if watched:
                    sock.send(json.dumps(ws_reg_packet(watched, ["0B", "0D", "1h", "00"])))
                last_rank_at = now
            if controls:
                engine.portfolio.paused = bool(controls.get("paused"))
                if controls.get("emergency"):
                    engine.portfolio.emergency = True
            for reply in apply_realtime(engine, message, now):
                sock.send(reply)
            _evaluate_dirty(engine, now)
            engine.drain(now)
    finally:
        close = getattr(sock, "close", None)
        if close:
            close()
    return engine


def _evaluate_dirty(engine: Engine, now: datetime) -> None:
    from daytrading.strategy import evaluate

    for code, book in list(engine.market.books.items()):
        if not book.dirty and code not in engine.portfolio.positions:
            continue
        snap = book.snapshot(now, engine.settings)
        if snap is None:
            continue
        if book.dirty:
            engine.journal.orderbook(snap)
        intents, blocked = evaluate(snap, engine.portfolio, engine.settings)
        book.prev_price = book.last_price
        book.dirty = False
        for blocked_code, reason in blocked:
            engine.reject(now, blocked_code, "buy", reason)
        for intent in intents:
            engine._clock = now
            engine.enqueue(intent)


def _send_conditions(sock, settings: Settings) -> None:
    seqs = [part.strip() for part in settings.condition_seqs.split(",") if part.strip()]
    for seq in seqs[: int(settings.condition_max)]:
        sock.send(json.dumps(condition_realtime_packet(seq)))


def _now() -> datetime:
    return datetime.now(KST).replace(microsecond=0)


def _select_codes(client: KiwoomClient, settings: Settings, codes: list[str] | None) -> list[str]:
    if codes:
        return list(codes)[: int(settings.ws_max_symbols)]
    found: list[str] = []
    for loader in (client.volume_rank, client.change_rank):
        try:
            found.extend(extract_codes(loader()))
        except KiwoomError:
            continue
        if found:
            break
    unique: list[str] = []
    for code in found:
        if code not in unique:
            unique.append(code)
    return unique[: int(settings.ws_max_symbols)] or ["005930"]


def _watch(engine: Engine, codes: list[str], meta_path: Path | None) -> None:
    known = load_symbol_meta(meta_path) if meta_path else {}
    for code in codes:
        row = known.get(code, {})
        flags = {name: bool(row.get(name, False)) for name in FLAG_FIELDS}
        engine.market.ensure(
            SymbolMeta(
                code=code,
                name=str(row.get("name") or code),
                prev_close=0,
                day_open=0,
                upper_limit=0,
                listed_on=row.get("listed_on"),
                flags_known=bool(row),
                **flags,
            )
        )


def check_from_home(home, settings: Settings, **kwargs) -> CheckResult:
    secrets = load_secrets(home)
    missing = missing_secret_names(secrets)
    needed = [name for name in missing if name != "KIWOOM_ACCOUNT_NO"]
    if needed:
        return CheckResult(False, "모의투자 키가 없습니다. " + ", ".join(needed))
    return check_connection(settings, secrets["KIWOOM_APP_KEY"], secrets["KIWOOM_APP_SECRET"], **kwargs)


def codes_from_rank(payload: dict[str, Any], limit: int) -> list[str]:
    return extract_codes(payload)[:limit]
