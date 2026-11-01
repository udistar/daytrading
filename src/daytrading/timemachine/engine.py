"""몬테카를로 재현과 히스토리 재실행.

재현은 예측이 아니다. 손절은 같은 봉에서 목표보다 먼저 본다.
판정 문장은 여기서 만든 문자열을 그대로 보여 준다.
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from daytrading.settings import Settings, tick_size
from daytrading.timeutil import KST, parse_hms
from daytrading.timemachine.bars import MinuteBar, RatioBar, bars_to_walk, full_sessions

DISCLAIMER = "시뮬레이션이며 예측이 아닙니다."
COST_NOTE = "비용 차감 후"


class TimeMachineError(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class TradePlan:
    symbol: str
    name: str
    market: str
    day: date
    entry: int
    entry_time: time
    stop: int
    target: int


def _market(value: str) -> str:
    text = value.strip().upper()
    if text not in {"KOSPI", "KOSDAQ"}:
        raise TimeMachineError("시장은 KOSPI 또는 KOSDAQ 이어야 합니다.")
    return text


def _slip(price: float, side: str, settings: Settings) -> float:
    ticks = int(settings.tm_slippage_ticks)
    if ticks <= 0:
        return float(price)
    basis = max(1, int(round(price)))
    tick = tick_size(settings, basis)
    if side == "buy":
        return float(price) + ticks * tick
    return max(1.0, float(price) - ticks * tick)


def buy_cost(price: float, settings: Settings) -> float:
    slipped = _slip(price, "buy", settings)
    rate = (float(settings.tm_buy_fee_pct) + float(settings.tm_related_fee_pct)) / 100
    return slipped * (1 + rate)


def sell_proceeds(price: float, settings: Settings, market: str) -> float:
    slipped = _slip(price, "sell", settings)
    tax_pct = float(settings.tm_kospi_tax_pct) if market == "KOSPI" else float(settings.tm_kosdaq_tax_pct)
    rate = (float(settings.tm_sell_fee_pct) + tax_pct + float(settings.tm_related_fee_pct)) / 100
    return slipped * (1 - rate)


def net_r(entry: float, exit_price: float, risk: float, settings: Settings, market: str) -> float:
    if risk <= 0:
        raise TimeMachineError("위험 폭(진입-손절)이 0 이하입니다.")
    return (sell_proceeds(exit_price, settings, market) - buy_cost(entry, settings)) / risk


def _r6(value: float) -> float:
    rounded = round(float(value), 6)
    if abs(rounded) < 5e-7:
        return 0.0
    return rounded


def _r4(value: float) -> float:
    return round(float(value), 4)


def percentile(values: list[float], pct: float) -> float:
    """가장 가까운 위 순위. pct 는 0~100."""
    ordered = sorted(values)
    rank = math.ceil(pct / 100 * len(ordered))
    index = min(len(ordered), max(1, rank)) - 1
    return ordered[index]


def verdict_sentence(target_hits: int, stop_hits: int, diff_pp: float | None, comparable: bool) -> str:
    if not comparable:
        return "비용 차감 후 목표 이익 또는 손절 손실이 남아 있지 않아 손익분기를 계산하지 않습니다. " + DISCLAIMER
    if target_hits + stop_hits <= 0:
        return "손절이나 목표에 닿은 재현이 없어 판정하지 않습니다. " + DISCLAIMER
    gap = abs(float(diff_pp or 0))
    if gap < 5:
        return (
            "비용 차감 후, 손절·목표에 닿은 재현만 보면 목표 비율이 손익분기와 5%p 안쪽입니다. "
            "동전 던지기에 가깝습니다. " + DISCLAIMER
        )
    direction = "높습니다" if (diff_pp or 0) > 0 else "낮습니다"
    if gap < 15:
        return (
            "비용 차감 후, 손절·목표에 닿은 재현만 보면 목표 비율이 손익분기보다 "
            f"5%p 이상 15%p 미만 {direction}. " + DISCLAIMER
        )
    return (
        "비용 차감 후, 손절·목표에 닿은 재현만 보면 목표 비율이 손익분기보다 "
        f"15%p 이상 {direction}. " + DISCLAIMER
    )


def _scale(price: float, ratio: RatioBar) -> tuple[float, float, float, float]:
    return (
        price * ratio.open_ratio,
        price * ratio.high_ratio,
        price * ratio.low_ratio,
        price * ratio.close_ratio,
    )


def _exit_on_bar(open_price: float, high: float, low: float, stop: float, target: float) -> tuple[str, float] | None:
    """손절을 목표보다 먼저 본다. 갭이면 시가, 아니면 손절·목표 가격."""
    if low <= stop:
        raw = open_price if open_price < stop else stop
        return "stop", raw
    if high >= target:
        raw = open_price if open_price > target else target
        return "target", raw
    return None


def _candidates(pool: dict[int, list[RatioBar]], slot: int, window: int) -> list[RatioBar]:
    found: list[RatioBar] = []
    for delta in range(-window, window + 1):
        found.extend(pool.get(slot + delta, []))
    return found


def _walk_levels(bars: list[MinuteBar], start_index: int, until_slot: int, stop: float, target: float) -> tuple[str, float]:
    last_close = bars[start_index].close
    for index in range(start_index, until_slot):
        bar = bars[index]
        hit = _exit_on_bar(bar.open, bar.high, bar.low, stop, target)
        if hit is not None:
            return hit
        last_close = bar.close
    return "neither", float(last_close)


def _history_one(session_bars: tuple[MinuteBar, ...], plan: TradePlan, settings: Settings) -> dict | None:
    day = session_bars[0].start.date()
    entry_start = settings.clock("entry_start")
    until = settings.clock("tm_until")
    lookback = timedelta(minutes=int(settings.tm_breakout_lookback_min))
    opening_high = 0
    if settings.tm_use_opening_range:
        opened = settings.clock("tm_session_open")
        range_end = (datetime.combine(day, opened) + timedelta(minutes=int(settings.tm_opening_range_min))).time()
        if range_end > entry_start:
            range_end = entry_start
        for bar in session_bars:
            clock = bar.start.time()
            if opened <= clock < range_end:
                opening_high = max(opening_high, bar.high)
    indexed = list(session_bars)
    for index, bar in enumerate(indexed):
        clock = bar.start.time()
        if clock < entry_start or clock >= until:
            continue
        window_start = bar.start - lookback
        prior = [item.high for item in indexed[:index] if item.start >= window_start]
        if not prior:
            continue
        prior_high = max(prior)
        if settings.tm_use_opening_range and opening_high > 0 and prior_high < opening_high:
            continue
        if bar.high <= prior_high:
            continue
        if bar.low <= prior_high <= bar.high:
            fill = float(prior_high)
        elif bar.open > prior_high:
            fill = float(bar.open)
        else:
            continue
        stop_pct = (plan.stop - plan.entry) / plan.entry
        target_pct = (plan.target - plan.entry) / plan.entry
        stop = fill * (1 + stop_pct)
        target = fill * (1 + target_pct)
        until_index = index
        for later in range(index, len(indexed)):
            if indexed[later].start.time() >= until:
                break
            until_index = later + 1
        outcome, exit_price = _walk_levels(indexed, index, until_index, stop, target)
        risk = fill - stop
        return {
            "day": day.isoformat(),
            "outcome": outcome,
            "entry": fill,
            "exit": float(exit_price),
            "r": net_r(fill, exit_price, risk, settings, plan.market),
        }
    return None


def run_timemachine(bars: list[MinuteBar], plan: TradePlan, settings: Settings) -> dict:
    market = _market(plan.market)
    plan = TradePlan(
        symbol=plan.symbol,
        name=plan.name,
        market=market,
        day=plan.day,
        entry=int(plan.entry),
        entry_time=plan.entry_time,
        stop=int(plan.stop),
        target=int(plan.target),
    )
    if plan.stop >= plan.entry:
        raise TimeMachineError("손절은 진입가보다 낮아야 합니다.")
    if plan.target <= plan.entry:
        raise TimeMachineError("목표는 진입가보다 높아야 합니다.")
    if not settings.tm_run_montecarlo and not settings.tm_run_history:
        raise TimeMachineError("몬테카를로와 히스토리가 모두 꺼져 있습니다.")
    sessions, total_full = full_sessions(bars, settings, plan.day)
    if len(sessions) < int(settings.tm_min_full_sessions):
        raise TimeMachineError(
            f"풀세션이 {len(sessions)}개입니다. 최소 {int(settings.tm_min_full_sessions)}개가 필요합니다. "
            "반일·불완전·계획일·계획일 이후 세션은 빼었습니다. "
            "분봉을 며칠까지 주는지는 키움 문서에서 확인되지 않았습니다."
        )
    note = ""
    if total_full < int(settings.tm_lookback_sessions):
        note = (
            f"요청한 {int(settings.tm_lookback_sessions)}세션보다 풀세션이 {total_full}개로 적습니다. "
            "있는 풀세션만 사용합니다. 최소 개수는 넘었습니다."
        )
    pool: dict[int, list[RatioBar]] = {}
    for session in sessions:
        for ratio in session.ratios:
            pool.setdefault(ratio.slot, []).append(ratio)
    anchor = sessions[-1].day
    slots = bars_to_walk(anchor, plan.entry_time, settings)
    if settings.tm_run_montecarlo and not slots:
        raise TimeMachineError("진입 시각과 청산 시각 사이에 봉이 없습니다.")

    risk = float(plan.entry - plan.stop)
    gain = sell_proceeds(plan.target, settings, market) - buy_cost(plan.entry, settings)
    loss = buy_cost(plan.entry, settings) - sell_proceeds(plan.stop, settings, market)
    comparable = gain > 0 and loss > 0
    breakeven = (1 / (1 + (gain / loss))) if comparable else None

    monte = {
        "target_first": 0,
        "stop_first": 0,
        "neither": 0,
        "green": 0,
        "avg_r": 0.0,
        "breakeven_win_rate": _r6(breakeven) if breakeven is not None else None,
        "target_rate_given_hit": None,
        "diff_pp": None,
        "close_percentiles": None,
    }
    if settings.tm_run_montecarlo:
        rng = random.Random(int(settings.tm_seed))
        window = int(settings.tm_window)
        terminal: list[float] = []
        rs: list[float] = []
        target_first = stop_first = neither = green = 0
        for _ in range(int(settings.tm_paths)):
            price = float(plan.entry)
            outcome = "neither"
            exit_price = price
            for slot in slots:
                choices = _candidates(pool, slot, window)
                if not choices:
                    raise TimeMachineError(f"{slot}번 봉 근처에 과거 봉이 없습니다.")
                chosen = choices[rng.randrange(len(choices))]
                open_price, high, low, close = _scale(price, chosen)
                hit = _exit_on_bar(open_price, high, low, plan.stop, plan.target)
                if hit is not None:
                    outcome, exit_price = hit
                    break
                price = close
                exit_price = close
            if outcome == "target":
                target_first += 1
            elif outcome == "stop":
                stop_first += 1
            else:
                neither += 1
            realized = net_r(plan.entry, exit_price, risk, settings, market)
            rs.append(realized)
            if realized > 0:
                green += 1
            terminal.append(float(exit_price))
        touched = target_first + stop_first
        target_rate = (target_first / touched) if touched else None
        diff_pp = ((target_rate - breakeven) * 100) if (target_rate is not None and breakeven is not None) else None
        if diff_pp is not None:
            diff_pp = _r4(diff_pp)
        monte = {
            "target_first": target_first,
            "stop_first": stop_first,
            "neither": neither,
            "green": green,
            "avg_r": _r6(sum(rs) / len(rs)),
            "breakeven_win_rate": _r6(breakeven) if breakeven is not None else None,
            "target_rate_given_hit": _r6(target_rate) if target_rate is not None else None,
            "diff_pp": diff_pp,
            "close_percentiles": {
                "p10": _r4(percentile(terminal, 10)),
                "p25": _r4(percentile(terminal, 25)),
                "p50": _r4(percentile(terminal, 50)),
                "p75": _r4(percentile(terminal, 75)),
                "p90": _r4(percentile(terminal, 90)),
            },
        }
        verdict = verdict_sentence(target_first, stop_first, diff_pp, comparable)
    else:
        verdict = "몬테카를로를 끄면 판정 문장을 만들지 않습니다. " + DISCLAIMER

    history = {"fired": 0, "target_first": 0, "stop_first": 0, "neither": 0, "avg_r": None}
    if settings.tm_run_history:
        fired_r: list[float] = []
        target_first = stop_first = neither = 0
        for session in sessions:
            row = _history_one(session.bars, plan, settings)
            if row is None:
                continue
            fired_r.append(row["r"])
            if row["outcome"] == "target":
                target_first += 1
            elif row["outcome"] == "stop":
                stop_first += 1
            else:
                neither += 1
        history = {
            "fired": len(fired_r),
            "target_first": target_first,
            "stop_first": stop_first,
            "neither": neither,
            "avg_r": _r6(sum(fired_r) / len(fired_r)) if fired_r else None,
        }

    return {
        "disclaimer": DISCLAIMER,
        "cost_note": COST_NOTE,
        "symbol": plan.symbol,
        "name": plan.name,
        "market": market,
        "plan_date": plan.day.isoformat(),
        "entry": plan.entry,
        "entry_time": plan.entry_time.strftime("%H:%M:%S"),
        "stop": plan.stop,
        "target": plan.target,
        "seed": int(settings.tm_seed),
        "paths": int(settings.tm_paths) if settings.tm_run_montecarlo else 0,
        "window": int(settings.tm_window),
        "tic_scope": str(settings.tm_tic_scope),
        "until": settings.tm_until,
        "pool_sessions": len(sessions),
        "full_sessions_before_plan": total_full,
        "lookback_requested": int(settings.tm_lookback_sessions),
        "pool_note": note,
        "planned_r_after_cost": _r6(gain / loss) if comparable else None,
        "monte_carlo": monte,
        "history": history,
        "verdict": verdict,
        "broker": "kiwoom-mock",
        "tr": "ka11080",
        "live_trading": False,
        "unconfirmed": ["분봉 조회가 며칠을 돌려주는지는 키움 공식 문서에서 확인하지 못했다."],
    }


def load_fixture(path: Path) -> tuple[list[MinuteBar], TradePlan]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TimeMachineError("픽스처는 객체여야 합니다.")
    plan_raw = raw.get("plan") or {}
    try:
        day = date.fromisoformat(str(plan_raw["date"]))
        entry = int(plan_raw["entry"])
        stop = int(plan_raw["stop"])
        entry_time = parse_hms(str(plan_raw["entry_time"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise TimeMachineError("픽스처 계획(date, entry, entry_time, stop, target)을 읽지 못했습니다.") from exc
    if plan_raw.get("target") not in (None, ""):
        target = int(plan_raw["target"])
    elif plan_raw.get("target_r") not in (None, ""):
        target = int(round(entry + float(plan_raw["target_r"]) * (entry - stop)))
    else:
        raise TimeMachineError("목표 가격 또는 target_r이 필요합니다.")
    plan = TradePlan(
        symbol=str(raw.get("symbol") or plan_raw.get("symbol") or ""),
        name=str(raw.get("name") or "샘플"),
        market=str(raw.get("market") or "KOSPI"),
        day=day,
        entry=entry,
        entry_time=entry_time,
        stop=stop,
        target=target,
    )
    bars: list[MinuteBar] = []
    for row in raw.get("bars") or []:
        stamp = str(row.get("cntr_tm") or row.get("start") or "")
        if len(stamp) >= 14 and stamp[:14].isdigit():
            start = datetime.strptime(stamp[:14], "%Y%m%d%H%M%S").replace(tzinfo=KST)
        else:
            start = datetime.fromisoformat(stamp)
            if start.tzinfo is None:
                start = start.replace(tzinfo=KST)
        def _px(primary: str, alternate: str) -> int:
            return abs(int(row[primary] if primary in row else row[alternate]))

        bars.append(
            MinuteBar(
                start=start,
                open=_px("open", "open_pric"),
                high=_px("high", "high_pric"),
                low=_px("low", "low_pric"),
                close=_px("close", "cur_prc"),
                volume=abs(int(row.get("volume", row.get("trde_qty", 0)) or 0)),
            )
        )
    if not bars:
        raise TimeMachineError("픽스처에 분봉이 없습니다.")
    return bars, plan


def write_result(path: Path, result: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
