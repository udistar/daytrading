import json
from datetime import date

import pytest

from daytrading.kiwoom import LIVE_REST, PAPER_REST, KiwoomClient, KiwoomError
from daytrading.meta import (
    UNKNOWN,
    build_symbol_meta_rows,
    load_symbol_meta,
    refresh_symbol_meta,
)


class Response:
    def __init__(self, payload: dict, headers: dict | None = None):
        self.payload = json.dumps(payload).encode("utf-8")
        self.headers = headers or {}

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _markets(**lists):
    base = {market: [] for market in ("0", "10", "8", "60", "70", "90")}
    base.update(lists)
    return base


def test_rows_use_only_documented_ka10099_fields():
    rows = build_symbol_meta_rows(
        _markets(
            **{
                "0": [
                    {
                        "code": "005930",
                        "name": "삼성전자",
                        "regDay": "19750611",
                        "state": "증거금100%",
                        "orderWarning": "0",
                        "auditInfo": "투자주의환기종목",
                    },
                    {
                        "code": "000660",
                        "name": "관리예",
                        "regDay": "20000104",
                        "state": "관리종목|증거금100%",
                        "orderWarning": "2",
                    },
                    {
                        "code": "111111",
                        "name": "아님",
                        "regDay": "20100101",
                        "state": "관리종목아님",
                        "orderWarning": "5",
                    },
                    {
                        "code": "A222222_AL",
                        "name": "주의요망",
                        "regDay": "20150101",
                        "state": "증거금100%",
                        "orderWarning": "1",
                    },
                ],
                "8": [
                    {
                        "code": "069500",
                        "name": "KODEX 200",
                        "regDay": "20021014",
                        "state": "증거금100%",
                        "orderWarning": "0",
                    }
                ],
                "60": [
                    {
                        "code": "500011",
                        "name": "ETN예",
                        "regDay": "20200102",
                        "state": "증거금100%",
                        "orderWarning": "4",
                    }
                ],
            }
        )
    )
    by_code = {row["code"]: row for row in rows}
    samsung = by_code["005930"]
    assert samsung["listed_on"] == "19750611"
    assert samsung["is_admin"] == "0"
    assert samsung["is_cleanup"] == "0"
    assert samsung["is_etf"] == "0"
    assert samsung["is_etn"] == "0"
    assert samsung["is_caution"] == UNKNOWN
    assert samsung["is_warning"] == UNKNOWN
    assert samsung["is_spac"] == UNKNOWN
    assert samsung["is_preferred"] == UNKNOWN
    assert by_code["000660"]["is_admin"] == "1"
    assert by_code["000660"]["is_cleanup"] == "1"
    assert by_code["000660"]["is_risk"] == "0"
    assert by_code["111111"]["is_admin"] == "0"
    assert by_code["111111"]["is_warning"] == UNKNOWN
    assert by_code["111111"]["is_overheat"] == "0"
    assert by_code["222222"]["is_etf"] == "1"
    assert by_code["222222"]["is_caution"] == UNKNOWN
    assert by_code["069500"]["is_etf"] == "1"
    assert by_code["500011"]["is_etn"] == "1"
    assert by_code["500011"]["is_risk"] == "1"


class _PagingOpener:
    def __init__(self, fail_market: str | None = None):
        self.fail_market = fail_market
        self.urls = []
        self.sent = []

    def __call__(self, request, timeout=10):
        api = request.get_header("Api-id") or ""
        body = json.loads(request.data.decode()) if request.data else {}
        self.urls.append(request.full_url)
        self.sent.append(
            {
                "api": api,
                "body": body,
                "cont": request.get_header("Cont-yn"),
                "next": request.get_header("Next-key"),
                "timeout": timeout,
            }
        )
        if self.fail_market and body.get("mrkt_tp") == self.fail_market:
            return Response({"return_code": 3, "return_msg": "조회 실패"})
        market = body.get("mrkt_tp")
        if market == "10" and request.get_header("Cont-yn") != "Y":
            return Response(
                {"return_code": 0, "list": [{"code": "035420", "name": "NAVER", "regDay": "20021029", "state": "증거금40%", "orderWarning": "0"}]},
                {"cont-yn": "Y", "next-key": "p2"},
            )
        if market == "10":
            return Response(
                {
                    "return_code": 0,
                    "list": [
                        {
                            "code": "035720",
                            "name": "카카오",
                            "regDay": "19991109",
                            "state": "증거금40%",
                            "orderWarning": "3",
                            "auditInfo": "투자주의환기종목",
                        }
                    ],
                },
                {"cont-yn": "N", "next-key": ""},
            )
        if market == "0":
            return Response(
                {
                    "return_code": 0,
                    "list": [
                        {"code": "005930", "name": "삼성전자", "regDay": "19750611", "state": "증거금100%", "orderWarning": "0"}
                    ],
                }
            )
        return Response({"return_code": 0, "list": []})


def test_refresh_pages_mock_host_and_caches_for_the_day(tmp_path):
    opener = _PagingOpener()
    client = KiwoomClient("key", "secret", mode="paper", opener=opener)
    client.token = "paper-token"
    path = tmp_path / "symbols_meta.csv"
    sleeps = []
    assert refresh_symbol_meta(client, path, date(2026, 9, 25), sleeper=sleeps.append) == "fetched"
    assert sleeps == [1.0] * 6
    assert all(url.startswith(PAPER_REST) for url in opener.urls)
    assert all(LIVE_REST not in url for url in opener.urls)
    assert all("/api/dostk/ordr" not in url for url in opener.urls)
    pages = [item for item in opener.sent if item["body"].get("mrkt_tp") == "10"]
    assert pages[0]["cont"] == "N"
    assert pages[1]["cont"] == "Y"
    assert pages[1]["next"] == "p2"
    assert pages[0]["timeout"] == 30
    assert all(item["api"] == "ka10099" for item in opener.sent)
    loaded = load_symbol_meta(path)
    assert loaded["035420"]["listed_on"] == date(2002, 10, 29)
    assert loaded["035720"]["is_overheat"] is True
    assert loaded["035720"]["is_caution"] is None
    assert loaded["035720"]["is_warning"] is None
    assert loaded["005930"]["flags_known"] is True
    calls = len(opener.sent)
    assert refresh_symbol_meta(client, path, date(2026, 9, 25), sleeper=sleeps.append) == "cache"
    assert len(opener.sent) == calls


def test_refresh_keeps_yesterday_file_when_the_call_fails(tmp_path):
    path = tmp_path / "symbols_meta.csv"
    path.write_text("code,name,listed_on\n005930,그대로,20200102\n", encoding="utf-8")
    opener = _PagingOpener(fail_market="8")
    client = KiwoomClient("key", "secret", mode="paper", opener=opener)
    client.token = "paper-token"
    assert refresh_symbol_meta(client, path, date(2026, 9, 25), sleeper=lambda _seconds: None) == "stale"
    assert "그대로" in path.read_text(encoding="utf-8")
    assert not (tmp_path / "symbols_meta.csv.asof").exists()


def test_refresh_raises_when_there_is_no_file_and_the_call_fails(tmp_path):
    opener = _PagingOpener(fail_market="0")
    client = KiwoomClient("key", "secret", mode="paper", opener=opener)
    client.token = "paper-token"
    with pytest.raises(KiwoomError):
        refresh_symbol_meta(client, tmp_path / "symbols_meta.csv", date(2026, 9, 25), sleeper=lambda _seconds: None)


def test_symbol_list_rejects_a_payload_without_list():
    def opener(request, timeout=10):
        return Response({"return_code": 0, "stk_cd": "005930"})

    client = KiwoomClient("key", "secret", mode="paper", opener=opener)
    client.token = "paper-token"
    with pytest.raises(KiwoomError):
        client.symbol_list_page("0")
