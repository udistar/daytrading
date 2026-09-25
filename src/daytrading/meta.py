"""종목 주의·상장일 파일. ka10001 예제에 이 필드가 없어 파일로 받는다."""

from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path

FLAG_FIELDS = (
    "is_admin",
    "is_caution",
    "is_warning",
    "is_risk",
    "is_overheat",
    "is_cleanup",
    "is_etf",
    "is_etn",
    "is_spac",
    "is_preferred",
)


def load_symbol_meta(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    rows: dict[str, dict] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for record in csv.DictReader(handle):
            code = (record.get("code") or "").strip()
            if not code:
                continue
            item: dict = {"name": (record.get("name") or "").strip(), "flags_known": True}
            listed = (record.get("listed_on") or "").strip()
            item["listed_on"] = _parse_date(listed) if listed else None
            for flag in FLAG_FIELDS:
                item[flag] = _truthy(record.get(flag, ""))
            rows[code] = item
    return rows


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "y", "yes", "true", "예"}


def _parse_date(value: str) -> date:
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"상장일 형식이 아닙니다: {value}")
