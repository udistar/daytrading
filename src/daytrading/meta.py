"""종목 주의·상장일 파일.

ka10001(주식기본정보) 공식 응답에는 관리·경고·ETF·상장일이 없다.
값은 ka10099(종목정보 리스트, POST /api/dostk/stkinfo)만 쓴다.
공식 명세에 코드가 없는 칸은 unknown 으로 적고 1/0 으로 채우지 않는다.
"""

from __future__ import annotations

import csv
import time
from collections.abc import Callable, Mapping
from datetime import date, datetime
from pathlib import Path

from daytrading.kiwoom import KiwoomError, normalize_code

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

# ka10099 요청 mrkt_tp. 공식 명세의 시장구분 값만 쓴다.
KOSPI_MARKET = "0"
KOSDAQ_MARKET = "10"
ETF_MARKET = "8"
ETN_MARKETS = ("60", "70", "90")
SYMBOL_LIST_MARKETS = (KOSPI_MARKET, KOSDAQ_MARKET, ETF_MARKET, *ETN_MARKETS)

# orderWarning 공식 설명: 0 해당없음, 1 ETF투자주의요망, 2 정리매매, 3 단기과열, 4 투자위험, 5 투자경과.
# 5는 투자경고가 아니다. 명세 문장이 여기서 끊겨 있다.
ORDER_WARNING_KNOWN = frozenset({"0", "1", "2", "3", "4", "5"})
UNKNOWN = "unknown"
_MAX_LIST_PAGES = 40
_COLUMNS = ("code", "name", "listed_on", *FLAG_FIELDS)


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
                item[flag] = _flag_cell(record.get(flag, ""))
            rows[code] = item
    return rows


def symbol_meta_cache_fresh(path: Path, today: date) -> bool:
    stamp = _stamp_path(path)
    if not path.exists() or not stamp.exists():
        return False
    return stamp.read_text(encoding="utf-8").strip() == today.isoformat()


def refresh_symbol_meta(
    client,
    path: Path,
    today: date,
    sleeper: Callable[[float], None] | None = None,
) -> str:
    """하루 한 번 ka10099 로 symbols_meta.csv 를 채운다.

    같은 날짜 스탬프가 있으면 호출하지 않는다. 조회가 실패하고 파일이 있으면 그 파일을 둔다.
    """
    if symbol_meta_cache_fresh(path, today):
        return "cache"
    pause = sleeper or time.sleep
    try:
        by_market = fetch_symbol_lists(client, pause)
    except KiwoomError:
        if path.exists():
            return "stale"
        raise
    write_symbol_meta(path, build_symbol_meta_rows(by_market))
    _stamp_path(path).write_text(today.isoformat() + "\n", encoding="utf-8")
    return "fetched"


def fetch_symbol_lists(client, sleeper: Callable[[float], None]) -> dict[str, list[dict]]:
    """모의투자 호스트의 ka10099. 같은 TR 이라 호출 사이에 1초를 둔다."""
    by_market: dict[str, list[dict]] = {}
    first = True
    for market in SYMBOL_LIST_MARKETS:
        rows: list[dict] = []
        cont, nxt = "N", ""
        for _page in range(_MAX_LIST_PAGES):
            if not first:
                sleeper(1.0)
            first = False
            chunk, cont, nxt = client.symbol_list_page(market, cont_yn=cont, next_key=nxt)
            rows.extend(chunk)
            if str(cont).upper() != "Y":
                break
            if not str(nxt).strip():
                raise KiwoomError("종목정보 리스트 연속조회 키가 없습니다.")
            cont = "Y"
        else:
            raise KiwoomError("종목정보 리스트 연속조회가 끝나지 않습니다.")
        by_market[market] = rows
    return by_market


def build_symbol_meta_rows(by_market: Mapping[str, list[dict]]) -> list[dict]:
    """ka10099 행을 symbols_meta.csv 칸으로 옮긴다. 명세에 없는 플래그는 unknown."""
    etf_codes = _codes(by_market.get(ETF_MARKET, []))
    etn_codes: set[str] = set()
    for market in ETN_MARKETS:
        etn_codes |= _codes(by_market.get(market, []))
    grouped: dict[str, list[dict]] = {}
    for rows in by_market.values():
        for row in rows:
            code = normalize_code(str(row.get("code") or ""))
            if not code:
                continue
            grouped.setdefault(code, []).append(row)
    built = []
    for code in sorted(grouped):
        built.append(_meta_row(code, grouped[code], code in etf_codes, code in etn_codes, by_market))
    return built


def write_symbol_meta(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(_COLUMNS), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def _meta_row(code: str, rows: list[dict], in_etf: bool, in_etn: bool, by_market: Mapping[str, list]) -> dict:
    warning = _order_warning(rows)
    etf_known = ETF_MARKET in by_market
    etn_known = all(market in by_market for market in ETN_MARKETS)
    is_etf = "1" if in_etf or warning == "1" else ("0" if etf_known else UNKNOWN)
    is_etn = "1" if in_etn else ("0" if etn_known else UNKNOWN)
    return {
        "code": code,
        "name": _first_text(rows, "name"),
        "listed_on": _listed_on(rows),
        "is_admin": _admin_flag(rows),
        "is_caution": UNKNOWN,
        "is_warning": UNKNOWN,
        "is_risk": _warning_flag(warning, "4"),
        "is_overheat": _warning_flag(warning, "3"),
        "is_cleanup": _warning_flag(warning, "2"),
        "is_etf": is_etf,
        "is_etn": is_etn,
        "is_spac": UNKNOWN,
        "is_preferred": UNKNOWN,
    }


def _order_warning(rows: list[dict]) -> str:
    found = []
    for row in rows:
        text = str(row.get("orderWarning") or "").strip()
        if text in ORDER_WARNING_KNOWN:
            found.append(text)
    for code in ("2", "4", "3", "5", "1", "0"):
        if code in found:
            return code
    return ""


def _warning_flag(warning: str, code: str) -> str:
    if warning not in ORDER_WARNING_KNOWN:
        return UNKNOWN
    return "1" if warning == code else "0"


def _admin_flag(rows: list[dict]) -> str:
    """state 예제 값이 '관리종목'이다. 그 토큰일 때만 1, 다른 상태 문구면 0."""
    seen = False
    for row in rows:
        state = str(row.get("state") or "").strip()
        if not state:
            continue
        seen = True
        tokens = [part.strip() for part in state.split("|")]
        if "관리종목" in tokens:
            return "1"
    return "0" if seen else UNKNOWN


def _listed_on(rows: list[dict]) -> str:
    for row in rows:
        text = str(row.get("regDay") or "").strip()
        if len(text) == 8 and text.isdigit():
            return text
    return ""


def _first_text(rows: list[dict], key: str) -> str:
    for row in rows:
        text = str(row.get(key) or "").strip()
        if text:
            return text
    return ""


def _codes(rows: list[dict]) -> set[str]:
    return {normalize_code(str(row.get("code") or "")) for row in rows if str(row.get("code") or "").strip()}


def _stamp_path(path: Path) -> Path:
    return path.with_name(path.name + ".asof")


def _flag_cell(value: str | None) -> bool | None:
    text = (value or "").strip().lower()
    if text == UNKNOWN:
        return None
    return _truthy(value or "")


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "y", "yes", "true", "예"}


def _parse_date(value: str) -> date:
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"상장일 형식이 아닙니다: {value}")
