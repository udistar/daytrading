"""체결을 잔고·실현손익에 반영한다."""

from __future__ import annotations

from daytrading.models import Fill, Portfolio, Position
from daytrading.risk import note_closed_trade
from daytrading.settings import Settings


def apply_fill(portfolio: Portfolio, fill: Fill, settings: Settings) -> None:
    if fill.side == "buy":
        _apply_buy(portfolio, fill)
        return
    _apply_sell(portfolio, fill, settings)


def _apply_buy(portfolio: Portfolio, fill: Fill) -> None:
    spent = fill.price * fill.qty + fill.fee_krw
    portfolio.cash -= spent
    position = portfolio.positions.get(fill.code)
    if position is None or position.qty <= 0:
        position = Position(
            code=fill.code,
            name=fill.name,
            qty=0,
            cost_krw=0,
            fill_notional=0,
            entry_price=fill.price,
            last_add_price=fill.price,
            buy_count=0,
            opened_at=fill.ts,
            last_buy_at=fill.ts,
            high_since_entry=fill.price,
        )
        portfolio.positions[fill.code] = position
    position.qty += fill.qty
    position.cost_krw += spent
    position.fill_notional += fill.price * fill.qty
    position.buy_count += 1
    position.last_add_price = fill.price
    position.last_buy_at = fill.ts
    position.high_since_entry = max(position.high_since_entry, fill.price)
    portfolio.pending_buys.discard(fill.code)


def _apply_sell(portfolio: Portfolio, fill: Fill, settings: Settings) -> None:
    position = portfolio.positions.get(fill.code)
    if position is None or position.qty <= 0:
        portfolio.pending_sells.discard(fill.code)
        return
    qty = min(fill.qty, position.qty)
    proceeds = fill.price * qty - fill.fee_krw - fill.tax_krw
    cost_out = int(round(position.cost_krw * qty / position.qty))
    notional_out = int(round(position.fill_notional * qty / position.qty))
    realized = proceeds - cost_out
    portfolio.cash += proceeds
    portfolio.realized_krw += realized
    position.round_realized_krw += realized
    fill.realized_delta_krw = realized
    position.qty -= qty
    position.cost_krw -= cost_out
    position.fill_notional -= notional_out
    portfolio.pending_sells.discard(fill.code)
    if position.qty <= 0:
        note_closed_trade(portfolio, position.round_realized_krw, fill.ts, settings)
        portfolio.traded_today.add(fill.code)
        portfolio.positions.pop(fill.code, None)
