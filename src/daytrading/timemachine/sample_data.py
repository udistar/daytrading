"""가짜·익명 분봉. 키움 시세가 아니다.

아침 구간은 히스토리 발동이 재현되게 세 가지로 고정하고, 그 이후는 시드로만 움직인다.
"""

from __future__ import annotations

import json
import random
from datetime import date, time, timedelta
from pathlib import Path

from daytrading.settings import Settings, default_settings
from daytrading.timemachine.bars import expected_starts

PLAN_DAY = date(2026, 10, 30)


def packaged_fixture() -> Path:
    return Path(__file__).resolve().parent / "fixtures" / "sample_minute_bars.json"


def weekdays_ending(end: date, count: int) -> list[date]:
    found: list[date] = []
    cursor = end
    while len(found) < count:
        if cursor.weekday() < 5:
            found.append(cursor)
        cursor -= timedelta(days=1)
    return list(reversed(found))


def _forced(mode: int, clock: time) -> tuple[int, int, int, int] | None:
    """09:50 이전 봉. mode 0 상승 돌파, 1 하락 돌파, 2 미발동."""
    if clock < time(9, 0) or clock >= time(9, 50):
        return None
    if mode == 2:
        if clock == time(9, 0):
            return (9960, 10000, 9950, 9970)
        return (9970, 9990, 9960, 9975)
    if mode == 1:
        shaped = {
            time(9, 0): (9900, 10000, 9880, 9950),
            time(9, 5): (10000, 10050, 9990, 10020),
            time(9, 10): (10020, 10030, 9800, 9820),
        }
        return shaped.get(clock, (9820, 9850, 9780, 9800))
    shaped = {
        time(9, 0): (9900, 10000, 9880, 9950),
        time(9, 5): (10000, 10050, 9990, 10020),
        time(9, 10): (10020, 10100, 10000, 10080),
        time(9, 15): (10080, 10200, 10060, 10180),
        time(9, 20): (10180, 10320, 10160, 10310),
    }
    return shaped.get(clock, (10310, 10340, 10290, 10320))


def build_fixture_document(session_count: int = 24, seed: int = 7, settings: Settings | None = None) -> dict:
    settings = settings or default_settings()
    days = weekdays_ending(PLAN_DAY - timedelta(days=1), session_count)
    rng = random.Random(seed)
    bars: list[dict] = []
    for index, day in enumerate(days):
        mode = index % 3
        price = 10000
        for start in expected_starts(day, settings):
            forced = _forced(mode, start.time())
            if forced is not None:
                open_price, high_price, low_price, close_price = forced
            else:
                shock = rng.uniform(-0.004, 0.004)
                open_price = price
                close_price = max(1000, int(round(price * (1 + shock))))
                wiggle = rng.randint(0, 40)
                high_price = max(open_price, close_price) + wiggle
                low_price = max(1000, min(open_price, close_price) - wiggle)
            bars.append(
                {
                    "cntr_tm": start.strftime("%Y%m%d%H%M%S"),
                    "open": open_price,
                    "high": high_price,
                    "low": low_price,
                    "close": close_price,
                    "volume": 1000 + (index % 7) * 10,
                }
            )
            price = close_price
    return {
        "symbol": "900001",
        "name": "샘플전자",
        "market": "KOSPI",
        "tic_scope": str(settings.tm_tic_scope),
        "source": "fixture",
        "anonymous": True,
        "note": "가짜 분봉이다. 키움 시세가 아니고 종목도 익명이다.",
        "plan": {
            "date": PLAN_DAY.isoformat(),
            "entry": 10000,
            "entry_time": "09:10:00",
            "stop": 9850,
            "target": 10300,
        },
        "bars": bars,
    }


def write_fixture(path: Path, document: dict | None = None) -> None:
    payload = document or build_fixture_document()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
