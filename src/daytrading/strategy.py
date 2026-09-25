"""매수·매도 규칙. 네트워크와 LLM을 호출하지 않는다."""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from daytrading.models import EXIT_REASONS, Intent, Portfolio, Position, Snapshot
from daytrading.settings import Settings, pyramid_levels, tick_size
from daytrading.timeutil import seconds_between


def _pct(base: float, percent: float) -> float:
    return base * (1 + percent / 100)


def _floor_price(value: float) -> int:
    return int(math.floor(value + 1e-9))


def entry_block_reason(snap: Snapshot, settings: Settings, portfolio: Portfolio) -> str | None:
    now = snap.ts.time()
    if now < settings.clock("entry_start"):
        return "신규 진입 시간 전"
    if now >= settings.clock("entry_end"):
        return "신규 진입 마감"
    if snap.code in portfolio.traded_today and not settings.allow_reentry_same_day:
        return "당일 재진입 안 함"
    if snap.code in portfolio.positions and portfolio.positions[snap.code].qty > 0:
        return "이미 보유"
    meta = snap.meta
    if settings.block_entry_when_meta_unknown and not meta.flags_known:
        return "종목 정보 없음"
    flag_map = (
        (settings.exclude_admin, meta.is_admin, "관리종목"),
        (settings.exclude_caution, meta.is_caution, "투자주의"),
        (settings.exclude_warning, meta.is_warning, "투자경고"),
        (settings.exclude_risk, meta.is_risk, "투자위험"),
        (settings.exclude_overheat, meta.is_overheat, "단기과열"),
        (settings.exclude_cleanup, meta.is_cleanup, "정리매매"),
        (settings.exclude_etf, meta.is_etf, "ETF"),
        (settings.exclude_etn, meta.is_etn, "ETN"),
        (settings.exclude_spac, meta.is_spac, "스팩"),
        (settings.exclude_preferred, meta.is_preferred, "우선주"),
    )
    for enabled, flagged, label in flag_map:
        if enabled and flagged:
            return f"{label} 제외"
    if meta.listed_on is None:
        if settings.block_entry_when_meta_unknown:
            return "상장일 없음"
    else:
        age = (snap.ts.date() - meta.listed_on).days
        if age <= settings.listing_age_min_days:
            return "상장 일수 부족"
    if not (settings.price_min_krw <= snap.price <= settings.price_max_krw):
        return "주가 범위 밖"
    change_prev = snap.change_prev_pct
    change_open = snap.change_open_pct
    gap = snap.gap_pct
    if change_prev is None or not (settings.change_prev_min_pct <= change_prev <= settings.change_prev_max_pct):
        return "전일대비 범위 밖"
    if change_open is None or change_open < settings.change_open_min_pct:
        return "시가대비 부족"
    if gap is not None and gap > settings.gap_exclude_above_pct:
        return "시가 갭 제외"
    upper = snap.upper_limit
    if upper <= 0 and snap.prev_close > 0:
        upper = _floor_price(_pct(snap.prev_close, settings.limit_up_ratio_pct))
    if upper > 0 and (upper - snap.price) / upper * 100 <= settings.limit_up_proximity_pct:
        return "상한가 근접"
    if snap.acc_value < settings.cumulative_value_min_krw:
        return "누적 거래대금 부족"
    if snap.minute_samples < settings.minute_value_lookback_min or snap.avg_minute_value is None:
        return "1분 거래대금 평균 표본 부족"
    if snap.minute_value < settings.minute_value_min_krw:
        return "1분 거래대금 부족"
    if snap.minute_value < snap.avg_minute_value * settings.minute_value_multiple:
        return "1분 거래대금 배수 부족"
    if snap.strength is None or snap.strength < settings.strength_min:
        return "체결강도 부족"
    spread = snap.spread_pct
    if spread is None or spread > settings.spread_max_pct:
        return "스프레드 큼"
    ratio = snap.bid_ask_ratio
    if ratio is None or ratio > settings.bid_ask_ratio_max:
        return "매수/매도 잔량비 초과"
    if snap.breakout_high is None or snap.prev_price is None:
        return "돌파 고가 표본 부족"
    if not (snap.prev_price <= snap.breakout_high < snap.price):
        return "직전 고가 돌파 아님"
    return None


def _buy_qty(amount: int, limit_price: int) -> int:
    if limit_price <= 0:
        return 0
    return amount // limit_price


def _limit_buy(snap: Snapshot, settings: Settings) -> int:
    tick = tick_size(settings, snap.best_ask or snap.price)
    return (snap.best_ask or snap.price) + tick * int(settings.entry_tick_offset)


def maybe_entry(snap: Snapshot, settings: Settings, portfolio: Portfolio) -> Intent | None:
    reason = entry_block_reason(snap, settings, portfolio)
    if reason:
        return None
    limit_price = _limit_buy(snap, settings)
    amount = pyramid_levels(settings)[0][1]
    qty = _buy_qty(amount, limit_price)
    if qty < 1:
        return None
    return Intent(
        code=snap.code,
        name=snap.name,
        side="buy",
        reason="entry",
        qty=qty,
        order_type="limit_ioc",
        limit_price=limit_price,
        amount_krw=limit_price * qty,
        note="매도1호가+틱 지정가 IOC",
        trigger_price=limit_price,
    )


def _hard_stop(position: Position, settings: Settings) -> tuple[int, str]:
    if position.buy_count <= 1:
        return _floor_price(_pct(position.entry_price, settings.first_stop_pct)), "stop"
    avg_stop = _pct(position.avg_price, settings.avg_stop_pct)
    last_stop = _pct(position.last_add_price, settings.last_add_stop_pct)
    if last_stop >= avg_stop:
        return _floor_price(last_stop), "stop"
    return _floor_price(avg_stop), "stop"


def _trail_stop(position: Position, price: int, settings: Settings) -> int:
    wide = price >= _pct(position.avg_price, settings.trail_wide_arm_pct)
    percent = settings.trail_wide_pct if wide else settings.trail_pct
    return _floor_price(_pct(position.high_since_entry, percent))


def _update_position_marks(position: Position, snap: Snapshot, settings: Settings) -> None:
    position.high_since_entry = max(position.high_since_entry, snap.price)
    basis = (position.qty, position.fill_notional)
    reached = snap.price >= _pct(position.avg_price, settings.breakeven_arm_pct)
    if basis != position.breakeven_basis:
        # 추가 매수로 평균가가 바뀌면 예전 평균으로 켜 둔 본전 스톱을 다시 판단한다.
        # 새 본전가가 현재가보다 위면 그 가격에 바로 팔지 않고 끈다.
        position.breakeven_basis = basis
        position.breakeven_armed = reached
        if position.breakeven_armed:
            be_price = _floor_price(_pct(position.avg_price, settings.breakeven_stop_pct))
            if be_price > snap.price:
                position.breakeven_armed = False
    elif reached:
        position.breakeven_armed = True
    if position.high_since_entry >= _pct(position.entry_price, settings.time_stop_min_gain_pct):
        position.time_stop_cleared = True
    if snap.vi_active:
        position.vi_seen = True
        if position.vi_released_at is not None:
            position.vi_released_at = None
            position.vi_exit_bar_start = None
    elif snap.vi_released_at is not None and position.vi_released_at != snap.vi_released_at:
        position.vi_seen = True
        position.vi_released_at = snap.vi_released_at
        position.vi_exit_bar_start = None


def maybe_exit(snap: Snapshot, position: Position, settings: Settings) -> Intent | None:
    _update_position_marks(position, snap, settings)
    now = snap.ts.time()
    hard_price, hard_reason = _hard_stop(position, settings)
    if position.breakeven_armed:
        be_price = _floor_price(_pct(position.avg_price, settings.breakeven_stop_pct))
        if be_price > hard_price:
            hard_price = be_price
            hard_reason = "breakeven"
    trail_price = _trail_stop(position, snap.price, settings)
    if snap.price <= max(hard_price, trail_price):
        reason = "trail" if trail_price > hard_price else hard_reason
        level = max(hard_price, trail_price)
        return _sell_all(snap, position, reason, f"기준 {level}", trigger=level)

    if (
        not position.time_stop_cleared
        and seconds_between(snap.ts, position.opened_at) >= settings.time_stop_seconds
    ):
        return _sell_all(snap, position, "time_stop", "진입 후 목표 상승 미달", trigger=snap.price)

    if settings.vi_exit_on_down_bar and position.vi_released_at is not None and position.vi_exit_bar_start is None:
        bar = first_completed_bar_after(snap, position.vi_released_at)
        if bar is not None:
            position.vi_exit_bar_start = bar.start
            if bar.down:
                return _sell_all(snap, position, "vi_exit", "VI 해제 후 첫 1분봉 음봉", trigger=snap.price)

    if now >= settings.clock("hard_cutoff"):
        return _sell_all(snap, position, "hard_cutoff", "09:50 전량", trigger=snap.price)
    if now >= settings.clock("market_flat_time"):
        return _sell_all(snap, position, "market_flat", "잔량 시장가", trigger=snap.price)
    if now >= settings.clock("partial_flat_start") and not position.schedule_partial_done:
        qty = int(position.qty * settings.partial_flat_ratio_pct / 100)
        if qty >= position.qty or qty <= 0:
            position.schedule_partial_done = True
            return _sell_all(snap, position, "schedule_flat", "분할 청산 전량", trigger=snap.price)
        position.schedule_partial_done = True
        return _sell(snap, position, "schedule_flat", qty, "분할 청산", trigger=snap.price)

    if not position.partial_done and snap.price >= _pct(position.avg_price, settings.partial_tp_pct):
        qty = int(position.qty * settings.partial_tp_ratio_pct / 100)
        if 0 < qty < position.qty:
            position.partial_done = True
            level = _floor_price(_pct(position.avg_price, settings.partial_tp_pct))
            return _sell(snap, position, "partial_tp", qty, "부분 익절", trigger=level)
    return None


def first_completed_bar_after(snap: Snapshot, released_at: datetime):
    """해제 시각 이후 처음으로 끝난 1분봉. 분 중간 해제는 다음 분봉을 본다."""
    if released_at.second == 0 and released_at.microsecond == 0:
        threshold = released_at.replace(microsecond=0)
    else:
        threshold = released_at.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for bar in snap.completed_bars:
        if bar.start >= threshold:
            return bar
    return None


def add_block_reason(snap: Snapshot, position: Position, settings: Settings) -> str | None:
    if snap.ts.time() >= settings.clock("add_end"):
        return "추가 매수 마감"
    if position.buy_count >= settings.max_buys_per_stock:
        return "매수 횟수 한도"
    levels = pyramid_levels(settings)
    if position.buy_count >= len(levels):
        return "매수 횟수 한도"
    trigger, _amount = levels[position.buy_count]
    if snap.price < _pct(position.entry_price, trigger):
        return "추가 매수 가격 미달"
    if seconds_between(snap.ts, position.last_buy_at) < settings.add_min_seconds:
        return "추가 매수 간격"
    if settings.add_require_new_high and snap.price <= snap.session_high_before:
        return "신고가 갱신 아님"
    if snap.strength is not None and snap.strength < settings.add_block_strength_below:
        return "체결강도 급락"
    if snap.strength is None or snap.strength < settings.add_strength_min:
        return "추가 체결강도 부족"
    if settings.add_require_profit and snap.price <= position.avg_price:
        return "이익 상태 아님"
    bars = list(snap.completed_bars)
    need = int(settings.add_down_bars)
    if len(bars) >= need and all(bar.down for bar in bars[-need:]):
        return "연속 음봉"
    if bars and bars[-1].upper_wick_ratio >= settings.long_wick_ratio and bars[-1].high > bars[-1].low:
        return "긴 윗꼬리"
    if len(bars) >= 2 and bars[-2].value > 0:
        dropped = (1 - bars[-1].value / bars[-2].value) * 100
        if dropped >= settings.minute_value_drop_block_pct:
            return "1분 거래대금 급감"
    if _near_vi(snap, settings):
        return "VI 발동가 근접"
    return None


def _near_vi(snap: Snapshot, settings: Settings) -> bool:
    if snap.vi_price <= 0:
        return False
    distance = abs(snap.price - snap.vi_price) / snap.vi_price * 100
    return distance <= settings.vi_proximity_pct


def maybe_add(snap: Snapshot, position: Position, settings: Settings) -> Intent | None:
    if add_block_reason(snap, position, settings):
        return None
    _trigger, amount = pyramid_levels(settings)[position.buy_count]
    limit_price = _limit_buy(snap, settings)
    qty = _buy_qty(amount, limit_price)
    if qty < 1:
        return None
    level = position.buy_count + 1
    return Intent(
        code=snap.code,
        name=snap.name,
        side="buy",
        reason="add",
        qty=qty,
        order_type="limit_ioc",
        limit_price=limit_price,
        amount_krw=limit_price * qty,
        note=f"{level}회차",
        trigger_price=limit_price,
    )


def _sell_all(snap: Snapshot, position: Position, reason: str, note: str, trigger: int = 0) -> Intent:
    return _sell(snap, position, reason, position.qty, note, trigger=trigger)


def _sell(snap: Snapshot, position: Position, reason: str, qty: int, note: str, trigger: int = 0) -> Intent:
    return Intent(
        code=snap.code,
        name=snap.name,
        side="sell",
        reason=reason,
        qty=qty,
        order_type="market",
        limit_price=0,
        amount_krw=snap.price * qty,
        note=note,
        trigger_price=trigger or snap.price,
    )


def crossed_breakout(snap: Snapshot) -> bool:
    return (
        snap.breakout_high is not None
        and snap.prev_price is not None
        and snap.prev_price <= snap.breakout_high < snap.price
    )


def evaluate(snap: Snapshot, portfolio: Portfolio, settings: Settings) -> tuple[list[Intent], list[tuple[str, str]]]:
    """의도와, 돌파했는데 필터에 걸린 (코드, 사유) 목록을 돌려준다."""
    position = portfolio.positions.get(snap.code)
    rejections: list[tuple[str, str]] = []
    if position and position.qty > 0:
        room = portfolio.sell_room(snap.code)
        if room <= 0:
            return [], rejections
        exit_intent = maybe_exit(snap, position, settings)
        if exit_intent:
            exit_intent.qty = min(exit_intent.qty, room)
            if exit_intent.qty >= 1:
                return [exit_intent], rejections
        if portfolio.pending_sell_qty.get(snap.code, 0) > 0:
            return [], rejections
        if snap.code in portfolio.pending_buys:
            return [], rejections
        blocked = add_block_reason(snap, position, settings)
        if blocked is None:
            add_intent = maybe_add(snap, position, settings)
            return ([add_intent] if add_intent else []), rejections
        levels = pyramid_levels(settings)
        if position.buy_count < len(levels):
            trigger, _amount = levels[position.buy_count]
            if snap.price >= _pct(position.entry_price, trigger):
                rejections.append((snap.code, blocked))
        return [], rejections
    if snap.code in portfolio.pending_buys:
        return [], rejections
    if crossed_breakout(snap):
        blocked = entry_block_reason(snap, settings, portfolio)
        if blocked:
            rejections.append((snap.code, blocked))
            return [], rejections
    entry = maybe_entry(snap, settings, portfolio)
    return ([entry] if entry else []), rejections


def phase_name(moment: datetime, settings: Settings) -> str:
    clock = moment.time()
    if clock < settings.clock("watch_start"):
        return "장전"
    if clock < settings.clock("entry_start"):
        return "관망"
    if clock < settings.clock("entry_end"):
        return "신규진입"
    if clock < settings.clock("add_end"):
        return "추가매수만"
    if clock < settings.clock("partial_flat_start"):
        return "청산대기"
    if clock < settings.clock("market_flat_time"):
        return "분할청산"
    if clock < settings.clock("hard_cutoff"):
        return "잔량청산"
    return "마감"


def assert_exit_reason(reason: str) -> None:
    if reason not in EXIT_REASONS and reason not in {"entry", "add"}:
        raise ValueError(reason)
