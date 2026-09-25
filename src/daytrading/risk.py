"""주문 직전 위험 계층. 전략이 낸 주문을 거절하거나 청산을 강제한다."""

from __future__ import annotations

from datetime import datetime

from daytrading.models import Intent, Portfolio, Position, Snapshot
from daytrading.settings import Settings
from daytrading.timeutil import add_seconds, seconds_between


def mark_to_market(portfolio: Portfolio, prices: dict[str, int], settings: Settings) -> int:
    unrealized = 0
    for code, position in portfolio.positions.items():
        if position.qty <= 0:
            continue
        price = prices.get(code, position.last_add_price)
        gross = price * position.qty
        fee = int(round(gross * settings.commission_rate_pct / 100))
        tax = int(round(gross * settings.sell_tax_rate_pct / 100))
        unrealized += gross - fee - tax - position.cost_krw
    return portfolio.realized_krw + unrealized


def _spent_on(portfolio: Portfolio, code: str) -> int:
    """당일 누적 매수금액. 부분 매도로 보유 원가가 줄어도 한도는 돌아가지 않는다."""
    remembered = portfolio.bought_today.get(code, 0)
    position = portfolio.positions.get(code)
    from_position = 0
    if position is not None and position.qty > 0:
        from_position = position.bought_krw or position.fill_notional
    return max(remembered, from_position)


def loss_budget_left(pnl: int, settings: Settings) -> int:
    return settings.daily_loss_limit_krw + pnl


def vi_buy_block(snap: Snapshot | None, settings: Settings, now: datetime) -> str | None:
    if snap is None:
        return None
    if snap.vi_released_at is not None:
        elapsed = seconds_between(now, snap.vi_released_at)
        if 0 <= elapsed < settings.vi_release_block_seconds:
            return "VI 해제 후 대기"
    if snap.vi_price > 0:
        distance = abs(snap.price - snap.vi_price) / snap.vi_price * 100
        if distance <= settings.vi_proximity_pct:
            return "VI 발동가 근접"
    return None


def veto_buy(
    intent: Intent,
    portfolio: Portfolio,
    settings: Settings,
    snap: Snapshot | None,
    now: datetime,
    pnl: int,
) -> str | None:
    if intent.side != "buy":
        return None
    if portfolio.emergency:
        return "긴급 청산 중"
    if portfolio.paused:
        return "매매 중지"
    if portfolio.loss_halted:
        return "하루 손실 한도"
    if portfolio.day_halted:
        return "연속 손절 당일 종료"
    if portfolio.pause_until is not None and now < portfolio.pause_until:
        return "연속 손절 휴식"
    if now.time() >= settings.clock("hard_cutoff"):
        return "09:50 이후"
    if intent.reason == "entry" and now.time() < settings.clock("entry_start"):
        return "신규 진입 시간 전"
    if intent.reason == "entry" and now.time() >= settings.clock("entry_end"):
        return "신규 진입 마감"
    if intent.reason == "add" and now.time() >= settings.clock("add_end"):
        return "추가 매수 마감"
    if pnl <= -settings.daily_loss_limit_krw:
        return "하루 손실 한도"
    if intent.reason == "entry" and pnl <= -settings.loss_buffer_krw:
        return "손실 완충"
    if intent.reason == "add" and loss_budget_left(pnl, settings) < settings.add_min_remaining_budget_krw:
        return "남은 손실 여유 부족"
    position = portfolio.positions.get(intent.code)
    holding = position is not None and position.qty > 0
    if intent.reason == "add" and not holding:
        return "보유 없는 추가 매수"
    if intent.reason == "entry" and intent.code in portfolio.traded_today and not settings.allow_reentry_same_day:
        return "당일 재진입 안 함"
    if intent.reason == "entry" and portfolio.projected_entries(intent.code) > settings.max_concurrent_holdings:
        return "동시 보유 한도"
    buy_count = position.buy_count if holding else 0
    if buy_count >= settings.max_buys_per_stock:
        return "종목당 매수 횟수"
    invested = _spent_on(portfolio, intent.code)
    if invested + intent.amount_krw > settings.per_stock_cap_krw:
        return "종목당 금액 한도"
    reserved = sum(amount for code, amount in portfolio.pending_buy_amount.items() if code != intent.code)
    fee = int(round(intent.amount_krw * settings.commission_rate_pct / 100))
    if reserved + intent.amount_krw + fee > portfolio.cash:
        return "현금 부족"
    blocked = vi_buy_block(snap, settings, now)
    if blocked:
        return blocked
    if intent.qty < 1:
        return "수량 없음"
    return None


def note_closed_trade(portfolio: Portfolio, pnl: int, now: datetime, settings: Settings) -> None:
    portfolio.closed_trade_pnls.append(pnl)
    if pnl < 0:
        portfolio.consecutive_losses += 1
    elif pnl > 0:
        portfolio.consecutive_losses = 0
    if portfolio.consecutive_losses >= settings.consecutive_loss_halt_count:
        portfolio.day_halted = True
        portfolio.pause_until = None
        return
    if portfolio.consecutive_losses >= settings.consecutive_loss_pause_count:
        portfolio.pause_until = add_seconds(now, settings.consecutive_loss_pause_seconds)


def liquidate_intents(portfolio: Portfolio, prices: dict[str, int], reason: str) -> list[Intent]:
    intents = []
    for position in portfolio.positions.values():
        room = portfolio.sell_room(position.code)
        if room <= 0:
            continue
        price = prices.get(position.code, position.last_add_price)
        intents.append(
            Intent(
                code=position.code,
                name=position.name,
                side="sell",
                reason=reason,
                qty=room,
                order_type="market",
                limit_price=0,
                amount_krw=price * room,
                note="위험 계층 강제 청산",
                trigger_price=price,
            )
        )
    return intents


def protection_intents(
    portfolio: Portfolio,
    prices: dict[str, int],
    settings: Settings,
    now: datetime,
    pnl: int,
) -> list[Intent]:
    if pnl <= -settings.daily_loss_limit_krw:
        portfolio.loss_halted = True
    if portfolio.emergency:
        return liquidate_intents(portfolio, prices, "emergency")
    if portfolio.loss_halted:
        return liquidate_intents(portfolio, prices, "daily_loss")
    if now.time() >= settings.clock("hard_cutoff"):
        return liquidate_intents(portfolio, prices, "hard_cutoff")
    return []


def position_for(portfolio: Portfolio, code: str) -> Position | None:
    position = portfolio.positions.get(code)
    if position and position.qty > 0:
        return position
    return None
