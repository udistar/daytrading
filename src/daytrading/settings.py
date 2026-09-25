"""설정 스키마, 검증, 저장, 변경 이력.

명세의 숫자는 여기 기본값으로만 두고 코드 경로에 다시 적지 않는다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from daytrading.timeutil import KST, parse_hms

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    group: str
    kind: str
    default: Any
    minimum: float | None = None
    maximum: float | None = None
    help: str = ""
    decimals: int = 2


def _f(
    key: str,
    label: str,
    group: str,
    kind: str,
    default: Any,
    minimum: float | None = None,
    maximum: float | None = None,
    help: str = "",
    decimals: int = 2,
) -> Field:
    return Field(key, label, group, kind, default, minimum, maximum, help, decimals)


SCHEMA: tuple[Field, ...] = (
    _f("initial_capital_krw", "시작 금액 (원)", "계좌", "int", 50_000_000, 1_000_000, 10_000_000_000, "기본 5,000만 원"),
    _f("trading_mode", "매매 모드", "계좌", "str", "paper", help="v0은 paper만 허용합니다. live는 저장할 수 없습니다."),
    _f("live_trading_enabled", "실전 매매 사용", "계좌", "bool", False, help="v0에서는 실전 주소가 잠겨 있어 켤 수 없습니다."),
    _f("allow_reentry_same_day", "당일 같은 종목 재진입", "계좌", "bool", False, help="한 번 청산한 종목을 같은 날 다시 살지. 기본은 끄기."),
    _f("block_entry_when_meta_unknown", "종목 정보 없으면 신규 매수 금지", "계좌", "bool", True, help="상장일·투자경고 여부를 모르면 사지 않습니다. 시뮬레이션은 정보를 채워서 보냅니다."),
    _f("watch_start", "관망 시작", "시간", "time", "09:00:00"),
    _f("entry_start", "신규 진입 시작", "시간", "time", "09:05:00"),
    _f("entry_end", "신규 진입 마감", "시간", "time", "09:40:00"),
    _f("add_end", "추가 매수 마감", "시간", "time", "09:45:00"),
    _f("partial_flat_start", "분할 청산 시작", "시간", "time", "09:47:00"),
    _f("market_flat_time", "잔량 시장가 시각", "시간", "time", "09:49:30"),
    _f("hard_cutoff", "신규 매수 전면 금지", "시간", "time", "09:50:00", help="이 시각 이후 신규·추가 매수를 막고 남은 수량을 시장가로 팝니다."),
    _f("daily_loss_limit_krw", "하루 손실 한도 (원)", "위험", "int", 1_000_000, 10_000, 100_000_000),
    _f("loss_buffer_krw", "신규 매수 중단 완충 (원)", "위험", "int", 700_000, 0, 100_000_000, "당일 손익이 -이 금액 이하면 신규 매수를 멈춥니다."),
    _f("per_stock_cap_krw", "종목당 최대 금액 (원)", "위험", "int", 10_000_000, 100_000, 1_000_000_000, "추가 매수 포함"),
    _f("max_buys_per_stock", "종목당 최대 매수 횟수", "위험", "int", 5, 1, 5),
    _f("max_concurrent_holdings", "동시 보유 종목 수", "위험", "int", 5, 1, 20),
    _f("vi_release_block_seconds", "VI 해제 후 신규 매수 금지 (초)", "위험", "int", 180, 0, 3600),
    _f("vi_proximity_pct", "VI 발동가 근접 금지 (%)", "위험", "float", 1.0, 0.0, 20.0, "발동가의 이 퍼센트 안에서는 매수하지 않습니다."),
    _f("consecutive_loss_pause_count", "연속 손절 휴식 횟수", "위험", "int", 2, 1, 10),
    _f("consecutive_loss_pause_seconds", "연속 손절 휴식 (초)", "위험", "int", 600, 0, 86400),
    _f("consecutive_loss_halt_count", "연속 손절 당일 종료 횟수", "위험", "int", 3, 1, 20),
    _f("add_min_remaining_budget_krw", "추가 매수에 필요한 남은 손실 여유 (원)", "위험", "int", 300_000, 0, 100_000_000),
    _f("change_prev_min_pct", "전일대비 최소 (%)", "진입", "float", 3.0, -30.0, 30.0),
    _f("change_prev_max_pct", "전일대비 최대 (%)", "진입", "float", 12.0, -30.0, 30.0),
    _f("change_open_min_pct", "시가대비 최소 (%)", "진입", "float", 2.0, -30.0, 30.0),
    _f("cumulative_value_min_krw", "누적 거래대금 최소 (원)", "진입", "int", 5_000_000_000, 0, 1_000_000_000_000),
    _f("minute_value_min_krw", "1분 거래대금 최소 (원)", "진입", "int", 200_000_000, 0, 1_000_000_000_000),
    _f("minute_value_multiple", "1분 거래대금 / 직전 평균 배수", "진입", "float", 3.0, 0.1, 50.0),
    _f("minute_value_lookback_min", "1분 거래대금 평균 구간 (분)", "진입", "int", 10, 1, 60),
    _f("strength_min", "진입 체결강도 최소", "진입", "float", 125.0, 0.0, 1000.0),
    _f("strength_window_minutes", "체결강도 평균 구간 (분)", "진입", "int", 3, 1, 3, "1~3분"),
    _f("price_min_krw", "주가 최소 (원)", "진입", "int", 2_000, 1, 5_000_000),
    _f("price_max_krw", "주가 최대 (원)", "진입", "int", 100_000, 1, 5_000_000),
    _f("spread_max_pct", "스프레드 최대 (%)", "진입", "float", 0.3, 0.0, 10.0),
    _f("gap_exclude_above_pct", "시가 갭 제외 초과 (%)", "진입", "float", 5.0, 0.0, 30.0, "시가가 전일종가보다 이 값 초과로 높으면 제외"),
    _f("limit_up_proximity_pct", "상한가 근접 제외 (%)", "진입", "float", 5.0, 0.0, 30.0, "상한가까지 남은 폭이 이 값 이하면 제외"),
    _f("limit_up_ratio_pct", "상한가 폭 (%)", "진입", "float", 30.0, 1.0, 30.0, "호가에 상한가가 없을 때 전일종가 대비로 계산. 기본 30%."),
    _f("listing_age_min_days", "최소 상장 일수", "진입", "int", 5, 0, 3650, "상장 이 일수 이내인 종목은 제외"),
    _f("bid_ask_ratio_max", "매수/매도 잔량비 최대", "진입", "float", 5.0, 0.1, 100.0, "이 값을 넘으면 제외. 방향 자체는 필터로 쓰지 않고 기록만 합니다."),
    _f("breakout_lookback_min", "돌파 고가 구간 (분)", "진입", "int", 5, 3, 5, "직전 3~5분 고가"),
    _f("entry_tick_offset", "매도1호가에 더할 틱 수", "진입", "int", 1, 0, 10),
    _f("exclude_admin", "관리종목 제외", "제외", "bool", True),
    _f("exclude_caution", "투자주의 제외", "제외", "bool", True),
    _f("exclude_warning", "투자경고 제외", "제외", "bool", True),
    _f("exclude_risk", "투자위험 제외", "제외", "bool", True),
    _f("exclude_overheat", "단기과열 제외", "제외", "bool", True),
    _f("exclude_cleanup", "정리매매 제외", "제외", "bool", True),
    _f("exclude_etf", "ETF 제외", "제외", "bool", True),
    _f("exclude_etn", "ETN 제외", "제외", "bool", True),
    _f("exclude_spac", "스팩 제외", "제외", "bool", True),
    _f("exclude_preferred", "우선주 제외", "제외", "bool", True),
    _f("pyramid_1_trigger_pct", "1회차 진입 조건 (최초가 대비 %)", "불타기", "float", 0.0, 0.0, 0.0),
    _f("pyramid_1_amount_krw", "1회차 금액 (원)", "불타기", "int", 3_000_000, 10_000, 1_000_000_000),
    _f("pyramid_2_trigger_pct", "2회차 조건 (최초가 대비 %)", "불타기", "float", 1.5, 0.1, 50.0),
    _f("pyramid_2_amount_krw", "2회차 금액 (원)", "불타기", "int", 2_500_000, 10_000, 1_000_000_000),
    _f("pyramid_3_trigger_pct", "3회차 조건 (최초가 대비 %)", "불타기", "float", 3.0, 0.1, 50.0),
    _f("pyramid_3_amount_krw", "3회차 금액 (원)", "불타기", "int", 2_000_000, 10_000, 1_000_000_000),
    _f("pyramid_4_trigger_pct", "4회차 조건 (최초가 대비 %)", "불타기", "float", 4.5, 0.1, 50.0),
    _f("pyramid_4_amount_krw", "4회차 금액 (원)", "불타기", "int", 1_500_000, 10_000, 1_000_000_000),
    _f("pyramid_5_trigger_pct", "5회차 조건 (최초가 대비 %)", "불타기", "float", 6.0, 0.1, 50.0),
    _f("pyramid_5_amount_krw", "5회차 금액 (원)", "불타기", "int", 1_000_000, 10_000, 1_000_000_000),
    _f("add_min_seconds", "추가 매수 최소 간격 (초)", "불타기", "int", 30, 0, 3600),
    _f("add_strength_min", "추가 매수 체결강도 최소", "불타기", "float", 110.0, 0.0, 1000.0),
    _f("add_block_strength_below", "추가 매수 금지 체결강도", "불타기", "float", 100.0, 0.0, 1000.0, "이 값 미만이면 추가 매수 금지"),
    _f("add_down_bars", "추가 매수 금지 연속 음봉 수", "불타기", "int", 2, 1, 10),
    _f("long_wick_ratio", "긴 윗꼬리 기준 (윗꼬리/범위)", "불타기", "float", 0.5, 0.05, 1.0, "명세에 비율이 없어 1분봉 범위의 50% 이상을 긴 윗꼬리로 봅니다."),
    _f("minute_value_drop_block_pct", "1분 거래대금 급감 금지 (%)", "불타기", "float", 50.0, 1.0, 99.0, "직전 1분보다 이 비율 이상 줄면 추가 매수 금지"),
    _f("add_require_new_high", "추가 매수에 신고가 갱신 필요", "불타기", "bool", True),
    _f("add_require_profit", "추가 매수는 이익 중일 때만", "불타기", "bool", True),
    _f("first_stop_pct", "첫 매수 손절 (진입가 대비 %)", "청산", "float", -1.5, -30.0, -0.05),
    _f("avg_stop_pct", "추가 후 손절 (평균가 대비 %)", "청산", "float", -1.2, -30.0, -0.05),
    _f("last_add_stop_pct", "추가 후 손절 (최근 추가가 대비 %)", "청산", "float", -2.0, -30.0, -0.05, "평균가 손절과 최근 추가가 손절 중 먼저 닿는 쪽으로 전량"),
    _f("breakeven_arm_pct", "본전 스톱 발동 (평균가 대비 %)", "청산", "float", 2.0, 0.1, 50.0),
    _f("breakeven_stop_pct", "본전 스톱 가격 (평균가 대비 %)", "청산", "float", 0.3, 0.0, 20.0),
    _f("trail_pct", "트레일링 (고점 대비 %)", "청산", "float", -1.5, -30.0, -0.05),
    _f("trail_wide_arm_pct", "넓은 트레일링 시작 (평균가 대비 %)", "청산", "float", 4.0, 0.1, 50.0),
    _f("trail_wide_pct", "넓은 트레일링 (고점 대비 %)", "청산", "float", -2.0, -30.0, -0.05),
    _f("partial_tp_pct", "부분 익절 시작 (평균가 대비 %)", "청산", "float", 4.0, 0.1, 50.0),
    _f("partial_tp_ratio_pct", "부분 익절 비율 (%)", "청산", "float", 30.0, 1.0, 100.0),
    _f("time_stop_seconds", "시간 손절 (초)", "청산", "int", 180, 1, 3600),
    _f("time_stop_min_gain_pct", "시간 손절을 피할 최소 상승 (진입가 대비 %)", "청산", "float", 0.7, 0.0, 20.0),
    _f("vi_exit_on_down_bar", "VI 해제 후 첫 음봉이면 청산", "청산", "bool", True),
    _f("partial_flat_ratio_pct", "09:47 분할 청산 비율 (%)", "청산", "float", 50.0, 1.0, 100.0, "명세는 분할만 정하고 비율은 없어 절반을 기본값으로 둡니다. 나머지는 잔량 시장가 시각에 팝니다."),
    _f("token_bucket_per_sec", "자체 호출 상한 (초당)", "호출제한", "float", 4.0, 0.2, 4.0, decimals=1),
    _f("paper_tr_per_sec", "모의투자 TR당 초당 호출", "호출제한", "float", 1.0, 0.1, 1.0, decimals=1),
    _f("live_order_per_sec", "실전 주문 초당 상한", "호출제한", "float", 5.0, 0.2, 5.0, "v0는 실전 주문을 보내지 않습니다. 상한만 저장합니다.", 1),
    _f("live_query_per_sec", "실전 조회 초당 상한", "호출제한", "float", 5.0, 0.2, 5.0, decimals=1),
    _f("ws_max_symbols", "실시간 등록 종목 수", "호출제한", "int", 80, 1, 80),
    _f("ranking_poll_seconds", "순위 조회 간격 (초)", "호출제한", "int", 8, 1, 120, "모의투자는 TR당 초당 1회라 기본을 8초로 둡니다."),
    _f("condition_max", "실시간 조건검색 최대 개수", "호출제한", "int", 10, 1, 10),
    _f("condition_seqs", "영웅문4 조건식 일련번호", "호출제한", "str", "", help="쉼표로 구분. 예: 0,1,2. 비우면 순위 조회만 사용합니다."),
    _f("commission_rate_pct", "매매 수수료 (%, 편도)", "비용", "float", 0.015, 0.0, 1.0, "키움 FAQ 기준 0.015%. 계좌 요율에 맞게 바꾸세요.", 4),
    _f("sell_tax_rate_pct", "매도세 (%, 거래세+농특세)", "비용", "float", 0.20, 0.0, 1.0, "비교 문서 기준 코스피·코스닥 매도세 0.20%.", 4),
    _f("sim_adverse_ticks", "시뮬레이션 불리 틱", "비용", "int", 1, 0, 10, "모의 체결을 이 틱만큼 불리하게 잡습니다."),
    _f(
        "tick_rules",
        "호가 단위 (가격미만:틱,...)",
        "비용",
        "str",
        "2000:1,5000:5,20000:10,50000:50,200000:100,500000:500,1000000000:1000",
        help="예: 2000원 미만 1원. 매도1호가 +1틱 계산에 씁니다.",
    ),
)

FIELDS = {field.key: field for field in SCHEMA}
GROUPS = tuple(dict.fromkeys(field.group for field in SCHEMA))
TIME_ORDER = (
    "watch_start",
    "entry_start",
    "entry_end",
    "add_end",
    "partial_flat_start",
    "market_flat_time",
    "hard_cutoff",
)


class SettingsError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("\n".join(errors))


class Settings:
    def __init__(self, data: dict[str, Any]):
        self._data = dict(data)

    def __getattr__(self, item: str) -> Any:
        if item.startswith("_"):
            raise AttributeError(item)
        try:
            return self._data[item]
        except KeyError as exc:
            raise AttributeError(item) from exc

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Settings) and self._data == other._data

    def to_dict(self) -> dict[str, Any]:
        return dict(self._data)

    def clock(self, key: str):
        return parse_hms(str(self._data[key]))


def default_settings() -> Settings:
    return Settings({field.key: field.default for field in SCHEMA})


def parse_tick_rules(text: str) -> list[tuple[int, int]]:
    rules: list[tuple[int, int]] = []
    for part in text.split(","):
        piece = part.strip()
        if not piece:
            continue
        upper_text, tick_text = piece.split(":")
        upper = int(upper_text)
        tick = int(tick_text)
        if upper <= 0 or tick <= 0:
            raise ValueError("호가 단위는 양수여야 합니다.")
        if rules and upper <= rules[-1][0]:
            raise ValueError("호가 단위 구간은 오름차순이어야 합니다.")
        rules.append((upper, tick))
    if not rules:
        raise ValueError("호가 단위가 비어 있습니다.")
    return rules


def tick_size(settings: Settings, price: int) -> int:
    rules = parse_tick_rules(settings.tick_rules)
    for upper, tick in rules:
        if price < upper:
            return tick
    return rules[-1][1]


def pyramid_levels(settings: Settings) -> list[tuple[float, int]]:
    count = int(settings.max_buys_per_stock)
    levels = []
    for index in range(1, count + 1):
        levels.append((float(getattr(settings, f"pyramid_{index}_trigger_pct")), int(getattr(settings, f"pyramid_{index}_amount_krw"))))
    return levels


def _coerce(field: Field, value: Any) -> Any:
    if field.kind == "bool":
        if not isinstance(value, bool):
            raise ValueError("예/아니오로 입력하세요.")
        return value
    if field.kind == "int":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or isinstance(value, str):
            raise ValueError("정수를 입력하세요.")
        if isinstance(value, float):
            if not value.is_integer():
                raise ValueError("정수를 입력하세요.")
            value = int(value)
        return int(value)
    if field.kind == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("숫자를 입력하세요.")
        return float(value)
    if field.kind == "time":
        if not isinstance(value, str):
            raise ValueError("HH:MM:SS 형식으로 입력하세요.")
        parse_hms(value)
        return value
    if field.kind == "str":
        if not isinstance(value, str):
            raise ValueError("문자열을 입력하세요.")
        return value.strip()
    raise ValueError("알 수 없는 형식")


def validate_settings(raw: dict[str, Any]) -> Settings:
    errors: list[str] = []
    unknown = sorted(set(raw) - set(FIELDS))
    for key in unknown:
        errors.append(f"알 수 없는 설정: {key}")
    cleaned: dict[str, Any] = {field.key: field.default for field in SCHEMA}
    for field in SCHEMA:
        if field.key not in raw:
            continue
        try:
            value = _coerce(field, raw[field.key])
        except ValueError as exc:
            errors.append(f"{field.label}: {exc}")
            continue
        if field.kind in {"int", "float"} and field.minimum is not None and field.maximum is not None:
            if value < field.minimum or value > field.maximum:
                errors.append(f"{field.label}: {field.minimum}~{field.maximum} 범위여야 합니다.")
                continue
        cleaned[field.key] = value
    if errors:
        raise SettingsError(errors)

    settings = Settings(cleaned)
    range_errors: list[str] = []
    if settings.trading_mode != "paper":
        range_errors.append("v0에서는 매매 모드를 paper 이외로 저장할 수 없습니다.")
    if settings.live_trading_enabled:
        range_errors.append("v0에서는 실전 매매를 켤 수 없습니다. 실전 주소는 잠겨 있습니다.")
    if settings.change_prev_min_pct > settings.change_prev_max_pct:
        range_errors.append("전일대비 최소가 최대보다 클 수 없습니다.")
    if settings.price_min_krw > settings.price_max_krw:
        range_errors.append("주가 최소가 최대보다 클 수 없습니다.")
    if settings.loss_buffer_krw > settings.daily_loss_limit_krw:
        range_errors.append("손실 완충은 하루 손실 한도보다 클 수 없습니다.")
    if settings.consecutive_loss_halt_count < settings.consecutive_loss_pause_count:
        range_errors.append("당일 종료 횟수는 휴식 횟수보다 작을 수 없습니다.")
    clocks = []
    for key in TIME_ORDER:
        try:
            clocks.append(settings.clock(key))
        except ValueError:
            range_errors.append(f"{FIELDS[key].label} 시각이 올바르지 않습니다.")
    if len(clocks) == len(TIME_ORDER) and tuple(clocks) != tuple(sorted(clocks)):
        range_errors.append("시간은 관망 → 신규 → 추가매수 → 분할청산 → 잔량시장가 → 전면금지 순서여야 합니다.")
    try:
        levels = pyramid_levels(settings)
    except Exception as exc:  # pragma: no cover - defensive
        range_errors.append(str(exc))
        levels = []
    if levels and levels[0][0] != 0:
        range_errors.append("1회차 조건은 0% (진입)이어야 합니다.")
    triggers = [level[0] for level in levels]
    if triggers != sorted(triggers) or len(triggers) != len(set(triggers)):
        range_errors.append("불타기 조건(%)은 회차마다 더 커져야 합니다.")
    total_amount = sum(level[1] for level in levels)
    if total_amount > settings.per_stock_cap_krw:
        range_errors.append("불타기 금액 합계가 종목당 최대 금액을 넘습니다.")
    if settings.add_block_strength_below > settings.add_strength_min:
        range_errors.append("추가 매수 금지 체결강도는 최소 체결강도보다 클 수 없습니다.")
    if settings.breakeven_stop_pct >= settings.breakeven_arm_pct:
        range_errors.append("본전 스톱 가격은 발동 수익률보다 낮아야 합니다.")
    if settings.trail_wide_pct > settings.trail_pct:
        # both negative; -2 is wider (smaller) than -1.5, so wide must be <= trail
        range_errors.append("넓은 트레일링은 기본 트레일링보다 더 넓어야(더 작은 음수) 합니다.")
    seqs = [part.strip() for part in settings.condition_seqs.split(",") if part.strip()]
    if len(seqs) > settings.condition_max:
        range_errors.append("조건식 개수가 실시간 조건검색 최대 개수를 넘습니다.")
    if any(not part.isdigit() for part in seqs):
        range_errors.append("조건식 일련번호는 숫자만 쉼표로 구분하세요.")
    try:
        parse_tick_rules(settings.tick_rules)
    except ValueError as exc:
        range_errors.append(f"호가 단위: {exc}")
    if range_errors:
        raise SettingsError(range_errors)
    return settings


def diff_settings(old: Settings, new: Settings) -> list[dict[str, Any]]:
    changes = []
    for field in SCHEMA:
        before = old.to_dict()[field.key]
        after = new.to_dict()[field.key]
        if before != after:
            changes.append({"key": field.key, "label": field.label, "old": before, "new": after})
    return changes


class SettingsStore:
    def __init__(self, home: Path):
        self.home = home
        self.path = home / "settings.json"
        self.history_path = home / "settings_history.jsonl"

    def load(self) -> Settings:
        if not self.path.exists():
            return default_settings()
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and "values" in raw:
            raw = raw["values"]
        if not isinstance(raw, dict):
            raise SettingsError(["설정 파일 형식이 올바르지 않습니다."])
        return validate_settings(raw)

    def save(self, settings: Settings) -> list[dict[str, Any]]:
        validated = validate_settings(settings.to_dict())
        self.home.mkdir(parents=True, exist_ok=True)
        previous = self.load() if self.path.exists() else default_settings()
        changes = diff_settings(previous, validated)
        payload = {"schema_version": SCHEMA_VERSION, "values": validated.to_dict()}
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if changes:
            record = {
                "saved_at": datetime.now(KST).isoformat(timespec="seconds"),
                "changes": changes,
            }
            with self.history_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return changes

    def history(self) -> list[dict[str, Any]]:
        if not self.history_path.exists():
            return []
        rows = []
        for line in self.history_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows


def default_home() -> Path:
    import os

    override = os.environ.get("DAYTRADING_HOME")
    if override:
        return Path(override)
    return Path.home() / ".daytrading"
