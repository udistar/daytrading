"""모의 브로커. 지정가 IOC는 호가가 닿을 때만 체결한다."""

from __future__ import annotations

from daytrading.models import Fill, Intent, OrderRecord, Snapshot
from daytrading.settings import Settings, clamp_to_market, tick_size


class MockBroker:
    def submit(self, order_id: str, intent: Intent, snap: Snapshot, settings: Settings) -> tuple[OrderRecord, Fill | None]:
        if intent.side == "buy":
            price, status = self._buy_price(intent, snap, settings)
        else:
            price, status = self._sell_price(intent, snap, settings)
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
            status=status,
            note=intent.note,
            broker_order_no=f"SIM-{order_id}",
        )
        if status != "filled" or price is None:
            return record, None
        price = clamp_to_market(price, snap.prev_close, settings)
        trigger = intent.trigger_price or (intent.limit_price if intent.side == "buy" else snap.price)
        notional = price * intent.qty
        fee = int(round(notional * settings.commission_rate_pct / 100))
        tax = int(round(notional * settings.sell_tax_rate_pct / 100)) if intent.side == "sell" else 0
        if intent.side == "sell":
            slip = (trigger - price) * intent.qty if trigger else 0
        else:
            slip = (price - trigger) * intent.qty if trigger else 0
        fill = Fill(
            order_id=order_id,
            ts=snap.ts,
            code=intent.code,
            name=intent.name,
            side=intent.side,
            reason=intent.reason,
            qty=intent.qty,
            price=price,
            fee_krw=fee,
            tax_krw=tax,
            trigger_price=trigger,
            slippage_krw=slip,
        )
        return record, fill

    def _buy_price(self, intent: Intent, snap: Snapshot, settings: Settings) -> tuple[int | None, str]:
        ask = snap.best_ask or snap.price
        tick = tick_size(settings, ask)
        worse = ask + tick * int(settings.sim_adverse_ticks)
        if intent.order_type == "market":
            return worse, "filled"
        if ask <= intent.limit_price:
            return min(intent.limit_price, worse), "filled"
        return None, "cancelled"

    def _sell_price(self, intent: Intent, snap: Snapshot, settings: Settings) -> tuple[int | None, str]:
        bid = snap.best_bid or snap.price
        touch = min(bid, snap.price)
        tick = tick_size(settings, touch)
        worse = max(1, touch - tick * int(settings.sim_adverse_ticks))
        if intent.order_type == "market":
            return worse, "filled"
        if bid >= intent.limit_price:
            return max(intent.limit_price, worse), "filled"
        return None, "cancelled"
