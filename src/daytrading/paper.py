"""키움 모의투자 접속.

실전 호스트는 KiwoomClient가 열지 않는다. 연결 확인은 토큰과 시세 등록만 하고
주문을 내지 않는다. --paper 는 같은 엔진으로 모의투자 주문까지 보낸다.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from daytrading.journal import Journal
from daytrading.kiwoom import (
    LIVE_REST,
    KiwoomClient,
    KiwoomError,
    LiveTradingLocked,
    call_limit_kwargs,
    condition_realtime_packet,
    extract_codes,
    order_fill_reg_packet,
    order_key,
    parse_balance_map,
    parse_order_detail_rows,
    parse_real_messages,
    plan_order_sync,
    ws_login_packet,
    ws_reg_packet,
)
from daytrading.ledger import apply_fill
from daytrading.meta import FLAG_FIELDS, load_symbol_meta, refresh_symbol_meta
from daytrading.models import Fill, Intent, OrderRecord, Position, Snapshot, SymbolMeta
from daytrading.secrets import load_secrets, missing_secret_names
from daytrading.session import Engine
from daytrading.settings import Settings, price_band, trade_fee_krw
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
        self.working: dict[str, dict] = {}
        self.applied: set[str] = set()
        self.unfilled_done: list[dict] = []
        self.baseline: dict[str, int] = {}
        self.opening: dict[str, dict[str, int]] = {}
        self.carried: dict[str, int] = {}
        self.baseline_ready = False
        self._last_poll: datetime | None = None
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
        try:
            payload = self.client.place(
                intent.side,
                intent.code,
                intent.qty,
                intent.limit_price,
                market=intent.order_type == "market",
            )
        except KiwoomError as exc:
            record.status = "rejected"
            record.note = str(exc)
            return record, None
        record.broker_order_no = str(payload.get("ord_no") or "")
        if intent.order_type != "market":
            record.note = (record.note + " / " if record.note else "") + "모의 보통 지정가"
        if record.broker_order_no:
            self.applied.add(order_key(record.broker_order_no))
            self.working[order_key(record.broker_order_no)] = {
                "ord_no": record.broker_order_no,
                "code": intent.code,
                "name": intent.name,
                "side": intent.side,
                "qty": intent.qty,
                "filled": 0,
                "limit_price": intent.limit_price,
                "placed_at": snap.ts,
                "order_type": intent.order_type,
                "reason": intent.reason,
                "cancel_sent": False,
            }
        return record, None

    def take_fill_qty(self, order_no: str, qty: int) -> int:
        """실시간 체결량. 이미 조회에 반영된 수량은 다시 넣지 않는다."""
        key = order_key(order_no)
        item = self.working.get(key)
        if item is None:
            return 0 if key in self.applied else qty
        room = int(item["qty"]) - int(item["filled"])
        delta = max(0, min(int(qty), room))
        item["filled"] = int(item["filled"]) + delta
        if item["filled"] >= item["qty"]:
            item["done"] = True
        return delta

    def capture_baseline(self) -> None:
        try:
            balances = parse_balance_map(self.client.balance())
        except KiwoomError:
            self.baseline_ready = False
            return
        self.opening = balances
        self.baseline = {code: int(slot["qty"]) for code, slot in balances.items()}
        self.baseline_ready = True

    def poll_working(self, now: datetime, settings: Settings) -> tuple[list[dict], list[str]]:
        if not self.working:
            return [], []
        # 기본 3초. kt00007·kt00018은 서로 다른 API ID이고, 같은 ID는 클라이언트 간격 제한이 한 번 더 막는다.
        if self._last_poll is not None and (now - self._last_poll).total_seconds() < float(settings.paper_fill_poll_seconds):
            return [], []
        self._last_poll = now
        rows = parse_order_detail_rows(self.client.order_detail(""))
        balances = parse_balance_map(self.client.balance())
        fills, cancels = plan_order_sync(
            self.working,
            rows,
            balances,
            self.baseline,
            now,
            float(settings.paper_unfilled_cancel_seconds),
            use_balance=self.baseline_ready,
        )
        for key, item in list(self.working.items()):
            if item.get("done"):
                if int(item["filled"]) < int(item["qty"]):
                    self.unfilled_done.append(item)
                self.working.pop(key, None)
        return fills, cancels

    def forget_cancel(self, order_no: str) -> None:
        item = self.working.get(order_key(order_no))
        if item is not None:
            item["cancel_sent"] = False


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
        client = KiwoomClient(app_key, app_secret, mode="paper", opener=opener, **call_limit_kwargs(settings))
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
        # 00의 913은 접수, 체결, 확인, 취소, 거부. 체결이 아닌 값은 잔고에 넣지 않는다.
        return
    code = event["code"]
    qty = int(event.get("fill_qty") or 0)
    price = int(event.get("fill_price") or 0)
    if qty <= 0 or price <= 0:
        return
    # 907 매도수구분 공식 설명: 1 매도, 2 매수.
    side = "sell" if str(event.get("side_flag")) in {"1", "매도"} else "buy"
    broker = engine.broker
    take = getattr(broker, "take_fill_qty", None)
    if take is not None:
        qty = take(str(event.get("order_no") or ""), qty)
        if qty <= 0:
            return
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
    fee = trade_fee_krw(notional, engine.settings, paper_host=_engine_on_paper_host(engine))
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
    _record_fill(engine, fill)


class PaperSessionEnded(Exception):
    """스크립트 소켓이 메시지를 다 보낸 뒤 루프를 끝낼 때 쓴다."""


RECONNECT_BACKOFF_START = 1.0
RECONNECT_BACKOFF_CAP = 30.0


def _record_fill(engine: Engine, fill: Fill) -> None:
    apply_fill(engine.portfolio, fill, engine.settings)
    engine.journal.fill(fill)
    engine.prices[fill.code] = fill.price
    engine.fill_count += 1
    engine.reasons[fill.reason] += 1


def _socket_timeout(exc: BaseException) -> bool:
    return isinstance(exc, TimeoutError) or type(exc).__name__ == "WebSocketTimeoutException"


def _socket_closed(exc: BaseException) -> bool:
    if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, EOFError)):
        return True
    return type(exc).__name__ == "WebSocketConnectionClosedException"


def _retryable_disconnect(exc: BaseException) -> bool:
    """시세 연결과 토큰 갱신이 실패한 경우만 다시 붙인다. 실전 잠금은 올리지 않는다."""
    if isinstance(exc, (LiveTradingLocked, PaperSessionEnded)):
        return False
    if isinstance(exc, KiwoomError):
        return True
    return _socket_closed(exc) or _socket_timeout(exc) or isinstance(exc, OSError)


def next_backoff(current: float, cap: float = RECONNECT_BACKOFF_CAP) -> float:
    return min(cap, max(current * 2, RECONNECT_BACKOFF_START))


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
    sleeper: Callable[[float], None] | None = None,
) -> Engine:
    """모의투자 시세를 같은 엔진에 넣고, dry_run이 아니면 모의 주문만 전송한다."""
    client = KiwoomClient(app_key, app_secret, mode="paper", opener=opener, **call_limit_kwargs(settings))
    client.issue_token()
    now_fn = clock or _now
    if meta_path is not None:
        refresh_symbol_meta(client, meta_path, now_fn().date(), sleeper=sleeper)
    broker = KiwoomBroker(client, dry_run=dry_run)
    if not dry_run:
        broker.capture_baseline()
    engine = Engine(settings, journal, "paper", broker=broker)
    pinned = codes is not None
    watched = _select_codes(client, settings, codes)
    _watch(engine, watched, meta_path)
    if not dry_run:
        _adopt_carried(engine, broker, now_fn())
    socket_factory = connect or connect_websocket
    pause = sleeper or time.sleep
    sock = None
    down = True
    ever_connected = False
    backoff = RECONNECT_BACKOFF_START
    last_rank_at = now_fn()
    seen: dict = {}
    loops = 0
    try:
        while max_loops is None or loops < max_loops:
            if should_stop and should_stop():
                break
            loops += 1
            if down:
                try:
                    if ever_connected:
                        client.issue_token()
                    _close_socket(sock)
                    sock = socket_factory(client.ws_url)
                    _arm_recv_timeout(sock)
                    _open_stream(sock, client, settings, watched)
                    was_reconnect = ever_connected
                    ever_connected = True
                    down = False
                    engine.portfolio.quotes_down = False
                    backoff = RECONNECT_BACKOFF_START
                    if was_reconnect and not dry_run:
                        _reconcile_holdings(engine, now_fn())
                except PaperSessionEnded:
                    break
                except LiveTradingLocked:
                    raise
                except Exception as exc:
                    if not _retryable_disconnect(exc):
                        _flatten_open(engine, now_fn())
                        raise
                    engine.portfolio.quotes_down = True
                    down = True
                    try:
                        _clock_exits(engine, now_fn())
                    except KiwoomError as order_error:
                        engine.reject(now_fn(), "", "buy", "주문 거절", str(order_error))
                    pause(backoff)
                    backoff = next_backoff(backoff)
                    continue
                try:
                    _clock_exits(engine, now_fn())
                except KiwoomError as exc:
                    engine.reject(now_fn(), "", "buy", "주문 거절", str(exc))
            now = now_fn()
            try:
                raw = sock.recv()
            except PaperSessionEnded:
                break
            except Exception as exc:
                if _socket_timeout(exc):
                    _clock_exits(engine, now_fn())
                    continue
                if _socket_closed(exc):
                    engine.portfolio.quotes_down = True
                    down = True
                    _clock_exits(engine, now_fn())
                    continue
                _flatten_open(engine, now)
                raise
            if not raw:
                engine.portfolio.quotes_down = True
                down = True
                _clock_exits(engine, now_fn())
                continue
            message = _message(raw)
            try:
                if not pinned and (now - last_rank_at).total_seconds() >= float(settings.ranking_poll_seconds):
                    watched = _select_codes(client, settings, None)
                    _watch(engine, watched, meta_path)
                    if watched:
                        sock.send(json.dumps(ws_reg_packet(watched, ["0B", "0D", "1h"])))
                    last_rank_at = now
                if controls:
                    engine.portfolio.paused = bool(controls.get("paused"))
                    if controls.get("emergency"):
                        engine.portfolio.emergency = True
                for reply in apply_realtime(engine, message, now):
                    sock.send(reply)
                _evaluate_dirty(engine, now, seen)
                engine.drain(now)
                _settle_working(engine, now)
                _clock_exits(engine, now)
            except KiwoomError as exc:
                engine.reject(now, "", "buy", "주문 거절", str(exc))
            if now.time() >= settings.clock("hard_cutoff") and not _open_positions(engine):
                break
    finally:
        _close_socket(sock)
        journal.write_summary(engine.summary())
    return engine


def _open_stream(sock, client: KiwoomClient, settings: Settings, watched: list[str]) -> None:
    sock.send(json.dumps(ws_login_packet(client.token)))
    _read(sock, lambda message: str(message.get("trnm", "")).upper() == "LOGIN")
    if watched:
        sock.send(json.dumps(ws_reg_packet(watched, ["0B", "0D", "1h"])))
    sock.send(json.dumps(order_fill_reg_packet()))
    _send_conditions(sock, settings)


def _arm_recv_timeout(sock) -> None:
    arm = getattr(sock, "settimeout", None)
    if arm is None:
        return
    try:
        arm(1)
    except Exception:
        return


def _close_socket(sock) -> None:
    close = getattr(sock, "close", None)
    if close is None:
        return
    try:
        close()
    except Exception:
        return


def _clock_exits(engine: Engine, now: datetime) -> None:
    """시세가 없어도 전일 보유 매도와 분할·강제 청산 시각을 돌린다."""
    engine._clock = now
    _sell_carried(engine, now)
    _flat_on_schedule(engine, now)
    engine.drain(now)


def _sell_carried(engine: Engine, now: datetime) -> None:
    if not engine.settings.sell_carried_positions:
        return
    if now.time() < engine.settings.clock("watch_start"):
        return
    broker = engine.broker
    for code, qty in list(getattr(broker, "carried", {}).items()):
        room = engine.portfolio.sell_room(code)
        take = min(int(qty), room)
        if take <= 0:
            continue
        position = engine.portfolio.positions.get(code)
        price = engine.prices.get(code, 0) or (position.last_add_price if position else 0)
        if price <= 0:
            continue
        engine.enqueue(
            Intent(
                code=code,
                name=position.name if position else code,
                side="sell",
                reason="carried",
                qty=take,
                order_type="market",
                limit_price=0,
                amount_krw=price * take,
                note="전일 보유",
                trigger_price=price,
            )
        )


def _flat_on_schedule(engine: Engine, now: datetime) -> None:
    if now.time() < engine.settings.clock("partial_flat_start"):
        return
    from daytrading.market import quote_snapshot
    from daytrading.strategy import maybe_exit

    for code, position in list(engine.portfolio.positions.items()):
        if position.qty <= 0 or engine.portfolio.sell_room(code) <= 0:
            continue
        price = engine.prices.get(code, 0) or position.last_add_price
        if price <= 0:
            continue
        book = engine.market.books.get(code)
        prev = book.meta.prev_close if book is not None else 0
        name = book.meta.name if book is not None else position.name
        snap = quote_snapshot(code, name, now, price, prev)
        intent = maybe_exit(snap, position, engine.settings)
        if intent is None or intent.qty < 1:
            continue
        intent.qty = min(intent.qty, engine.portfolio.sell_room(code))
        if intent.qty >= 1:
            engine.enqueue(intent)


def _flatten_open(engine: Engine, now: datetime) -> None:
    from daytrading.risk import liquidate_intents

    engine._clock = now
    for intent in liquidate_intents(engine.portfolio, engine.prices, "emergency"):
        intent.note = "예외 종료 전 청산"
        engine.enqueue(intent)
    engine.drain(now)


def _adopt_carried(engine: Engine, broker: KiwoomBroker, now: datetime) -> None:
    if not engine.settings.sell_carried_positions:
        return
    for code, slot in broker.opening.items():
        qty = int(slot.get("qty") or 0)
        if qty <= 0:
            continue
        broker.carried[code] = qty
        pur = int(slot.get("pur") or 0)
        price = int(slot.get("price") or 0) or pur
        cost_each = pur or price
        detail = f"{qty}주"
        if cost_each > 0:
            detail += f" 매입단가 {cost_each}"
            _install_carried(engine, code, qty, cost_each, price or cost_each, now)
        else:
            detail += " 매입단가 없음"
        engine.journal.signal(now, code, "전일 보유", detail)


def _install_carried(engine: Engine, code: str, qty: int, cost_each: int, price: int, now: datetime) -> None:
    engine.portfolio.positions[code] = Position(
        code=code,
        name=code,
        qty=qty,
        cost_krw=cost_each * qty,
        fill_notional=cost_each * qty,
        entry_price=cost_each,
        last_add_price=price,
        buy_count=0,
        opened_at=now,
        last_buy_at=now,
        high_since_entry=price,
    )
    engine.prices[code] = price


def align_holdings(engine: Engine, balances: dict[str, dict[str, int]], now: datetime) -> None:
    """kt00018 수량에 보유를 맞춘다. 실현손익은 매도 단가를 모르면 바꾸지 않는다."""
    codes = set(balances) | {code for code, position in engine.portfolio.positions.items() if position.qty > 0}
    for code in sorted(codes):
        want = int(balances.get(code, {}).get("qty") or 0)
        position = engine.portfolio.positions.get(code)
        have = int(position.qty) if position is not None and position.qty > 0 else 0
        if want == have:
            continue
        if want > have:
            _grow_position(engine, code, balances.get(code, {}), want - have, now)
        else:
            _shrink_position(engine, code, want)
        mark = int(balances.get(code, {}).get("price") or 0)
        if mark > 0:
            engine.prices[code] = mark
        engine.journal.signal(now, code, "보유 보정", f"{have}주 -> {want}주")


def _grow_position(engine: Engine, code: str, slot: dict[str, int], delta: int, now: datetime) -> None:
    pur = int(slot.get("pur") or 0)
    price = int(slot.get("price") or 0) or pur or engine.prices.get(code, 0)
    cost_each = pur or price
    if cost_each <= 0 or delta <= 0:
        return
    position = engine.portfolio.positions.get(code)
    if position is None or position.qty <= 0:
        _install_carried(engine, code, delta, cost_each, price or cost_each, now)
        return
    position.qty += delta
    position.cost_krw += cost_each * delta
    position.fill_notional += cost_each * delta
    position.last_add_price = price or position.last_add_price


def _shrink_position(engine: Engine, code: str, want: int) -> None:
    position = engine.portfolio.positions.get(code)
    if position is None or position.qty <= 0:
        return
    if want <= 0:
        engine.portfolio.positions.pop(code, None)
        return
    position.cost_krw = int(round(position.cost_krw * want / position.qty))
    position.fill_notional = int(round(position.fill_notional * want / position.qty))
    position.qty = want


def _reconcile_holdings(engine: Engine, now: datetime) -> None:
    try:
        balances = parse_balance_map(engine.broker.client.balance())
    except KiwoomError as exc:
        engine.reject(now, "", "buy", "보유 조회 실패", str(exc))
        return
    align_holdings(engine, balances, now)


def _engine_on_paper_host(engine: Engine) -> bool:
    from daytrading.settings import on_paper_host

    client = getattr(engine.broker, "client", None)
    return on_paper_host(getattr(client, "base_url", None))


def _settle_working(engine: Engine, now: datetime) -> None:
    broker = engine.broker
    poll = getattr(broker, "poll_working", None)
    if poll is None:
        return
    try:
        fills, cancels = poll(now, engine.settings)
    except KiwoomError as exc:
        engine.reject(now, "", "buy", "체결 조회 실패", str(exc))
        return
    for item in getattr(broker, "unfilled_done", []):
        _release_working(engine, item)
    if hasattr(broker, "unfilled_done"):
        broker.unfilled_done.clear()
    for spec in fills:
        _book_synced_fill(engine, spec, now)
    for order_no in cancels:
        _cancel_working(engine, order_no, now)


def _book_synced_fill(engine: Engine, spec: dict, now: datetime) -> None:
    code = spec["code"]
    side = spec["side"]
    qty = int(spec["qty"])
    price = int(spec["price"])
    trigger = price
    remembered = getattr(engine.broker, "triggers", {}).get((code, side))
    if remembered:
        trigger = int(remembered)
    notional = price * qty
    fee = trade_fee_krw(notional, engine.settings, paper_host=_engine_on_paper_host(engine))
    tax = int(round(notional * engine.settings.sell_tax_rate_pct / 100)) if side == "sell" else 0
    slip = (trigger - price) * qty if side == "sell" else (price - trigger) * qty
    fill = Fill(
        order_id=str(spec.get("ord_no") or "Q"),
        ts=now,
        code=code,
        name=str(spec.get("name") or code),
        side=side,
        reason=str(spec.get("reason") or "exchange"),
        qty=qty,
        price=price,
        fee_krw=fee,
        tax_krw=tax,
        trigger_price=trigger,
        slippage_krw=slip,
    )
    _record_fill(engine, fill)


def _release_working(engine: Engine, item: dict) -> None:
    code = item["code"]
    left = int(item["qty"]) - int(item["filled"])
    if item["side"] == "buy":
        engine.portfolio.pending_buys.discard(code)
        engine.portfolio.pending_buy_amount.pop(code, None)
    elif left > 0:
        engine.portfolio.free_sell(code, left)


def _cancel_working(engine: Engine, order_no: str, now: datetime) -> None:
    broker = engine.broker
    item = broker.working.get(order_key(order_no)) if hasattr(broker, "working") else None
    code = item["code"] if item else ""
    try:
        if item is not None:
            broker.client.cancel(code, order_no, 0)
    except KiwoomError as exc:
        if hasattr(broker, "forget_cancel"):
            broker.forget_cancel(order_no)
        engine.reject(now, code, item["side"] if item else "buy", "취소 거절", str(exc))
        return
    if item is not None:
        _release_working(engine, item)
        broker.working.pop(order_key(order_no), None)
    engine.reject(now, code, item["side"] if item else "buy", "미체결 취소", order_no)


def _open_positions(engine: Engine) -> bool:
    return any(position.qty > 0 for position in engine.portfolio.positions.values())


def _evaluate_dirty(engine: Engine, now: datetime, seen: dict) -> None:
    from daytrading.strategy import evaluate

    for code, book in list(engine.market.books.items()):
        if not book.dirty and code not in engine.portfolio.positions:
            continue
        snap = book.snapshot(now, engine.settings)
        if snap is None:
            continue
        if book.dirty:
            engine.journal.orderbook(snap)
        intents, blocked = evaluate(snap, engine.portfolio, engine.settings, seen=seen)
        book.prev_price = book.last_price
        book.dirty = False
        for blocked_code, reason, detail in blocked:
            engine.reject(now, blocked_code, "buy", reason, detail)
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
