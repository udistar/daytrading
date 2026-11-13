"""Kiwoom REST API client: token, quotes, daily bars, orders, balance.

Standard library only. Keys come from the environment (KIWOOM_APPKEY,
KIWOOM_SECRETKEY), or KIWOOM_TOKEN for a token issued elsewhere. Never put
keys in a file that is committed, or in chat.

API ids used. Every request and answer is written to the event log, so a field
name that differs from the guide shows up on the first `main.py check`:
  POST /oauth2/token                       token
  POST /api/dostk/stkinfo  ka10001         quote (cur_prc, open_pric, high_pric, low_pric, base_pric, upl_pric, lst_pric)
  POST /api/dostk/chart    ka10081         daily bars, list stk_dt_pole_chart_qry
  POST /api/dostk/chart    ka10080         minute bars, list stk_min_pole_chart_qry
  POST /api/dostk/ordr     kt10000/kt10001 buy / sell (trde_tp 3 = market)
  POST /api/dostk/acnt     kt00018         holdings, list acnt_evlt_remn_indv_tot
"""
import datetime
import json
import os
import time
import urllib.error
import urllib.request

HOSTS = {"mock": "https://mockapi.kiwoom.com", "real": "https://api.kiwoom.com"}


class KiwoomError(Exception):
    pass


def num(v):
    """Kiwoom numbers are strings, often with a +/- direction sign and leading zeros."""
    if v is None:
        return None
    s = str(v).strip().replace(",", "").lstrip("+-")
    if not s:
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    return int(f) if f.is_integer() else f


def kst_now():
    return (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=9)).replace(
        tzinfo=None, microsecond=0)


class KiwoomClient:
    def __init__(self, appkey=None, secretkey=None, mode="mock", token=None, token_cache=None, pause=0.25,
                 opener=None, sleep=time.sleep, log=None):
        if mode not in HOSTS:
            raise KiwoomError("mode must be mock or real, not %r" % mode)
        if not token and not (appkey and secretkey):
            raise KiwoomError("set KIWOOM_APPKEY and KIWOOM_SECRETKEY (or KIWOOM_TOKEN) in the environment")
        self.appkey, self.secretkey, self.mode = appkey, secretkey, mode
        self.host = HOSTS[mode]
        self._token = token
        self.token_cache = token_cache
        self.pause = pause
        self._open = opener or urllib.request.urlopen
        self._sleep = sleep
        self._last = 0.0
        self.log = log or (lambda kind, **kw: None)

    @classmethod
    def from_env(cls, mode="mock", **kw):
        kw.setdefault("token", os.environ.get("KIWOOM_TOKEN"))
        return cls(os.environ.get("KIWOOM_APPKEY"), os.environ.get("KIWOOM_SECRETKEY"), mode, **kw)

    # ------------------------------------------------------------ http

    def _request(self, path, body, headers):
        wait = self.pause - (time.monotonic() - self._last)
        if wait > 0:
            self._sleep(wait)
        h = {"Content-Type": "application/json;charset=UTF-8"}
        h.update(headers)
        req = urllib.request.Request(self.host + path, data=json.dumps(body).encode("utf-8"), headers=h,
                                     method="POST")
        for attempt in range(4):
            try:
                with self._open(req, timeout=15) as resp:
                    text = resp.read().decode("utf-8")
                    rh = {k.lower(): v for k, v in resp.headers.items()}
                break
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 3:
                    self._sleep(1.0 * (attempt + 1))
                    continue
                raise KiwoomError("HTTP %d from %s: %s" % (e.code, path, e.read().decode("utf-8", "replace")[:300]))
            except urllib.error.URLError as e:
                raise KiwoomError("could not reach %s: %s" % (self.host, e.reason))
        self._last = time.monotonic()
        try:
            doc = json.loads(text)
        except ValueError:
            raise KiwoomError("%s did not return JSON" % path)
        rc = doc.get("return_code")
        if rc not in (None, 0, "0"):
            raise KiwoomError("Kiwoom %s %s: return_code %s, %s" % (path, headers.get("api-id", ""), rc,
                                                                   doc.get("return_msg")))
        return doc, rh

    def token(self):
        if self._token:
            return self._token
        if self.token_cache and os.path.exists(self.token_cache):
            try:
                with open(self.token_cache, encoding="utf-8") as fh:
                    c = json.load(fh)
                exp = datetime.datetime.strptime(c["expires_dt"], "%Y%m%d%H%M%S")
                if c.get("mode") == self.mode and exp - datetime.timedelta(minutes=10) > kst_now():
                    self._token = c["token"]
                    return self._token
            except (OSError, ValueError, KeyError):
                pass
        doc, _ = self._request("/oauth2/token", {"grant_type": "client_credentials", "appkey": self.appkey,
                                                 "secretkey": self.secretkey}, {})
        tok = doc.get("token")
        if not tok:
            raise KiwoomError("no token in the answer: %s" % doc.get("return_msg"))
        if self.token_cache:
            d = os.path.dirname(self.token_cache)
            if d:
                os.makedirs(d, exist_ok=True)
            fd = os.open(self.token_cache, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"mode": self.mode, "token": tok, "expires_dt": doc.get("expires_dt") or
                           (kst_now() + datetime.timedelta(hours=12)).strftime("%Y%m%d%H%M%S")}, fh)
        self._token = tok
        return tok

    def call(self, path, api_id, body, cont_key=None):
        h = {"authorization": "Bearer " + self.token(), "api-id": api_id}
        if cont_key:
            h.update({"cont-yn": "Y", "next-key": cont_key})
        doc, rh = self._request(path, body, h)
        self.log("api", api_id=api_id, body=body, answer=doc)
        return doc, rh

    # ------------------------------------------------------------ market data

    def quote(self, code):
        doc, _ = self.call("/api/dostk/stkinfo", "ka10001", {"stk_cd": code})
        q = {k: num(doc.get(f)) for k, f in (("price", "cur_prc"), ("open", "open_pric"), ("high", "high_pric"),
                                             ("low", "low_pric"), ("prev_close", "base_pric"),
                                             ("upper", "upl_pric"), ("lower", "lst_pric"))}
        if not q["price"]:
            raise KiwoomError("ka10001 for %s has no cur_prc: %s" % (code, json.dumps(doc, ensure_ascii=False)[:300]))
        q["name"] = doc.get("stk_nm")
        return q

    def daily(self, code, days=30):
        rows, key = [], None
        body = {"stk_cd": code, "base_dt": kst_now().strftime("%Y%m%d"), "upd_stkpc_tp": "1"}
        for _ in range(10):
            doc, rh = self.call("/api/dostk/chart", "ka10081", body, key)
            rows.extend(doc.get("stk_dt_pole_chart_qry") or [])
            key = rh.get("next-key") if rh.get("cont-yn") == "Y" else None
            if not key or len(rows) >= days:
                break
        out = []
        for r in rows[:days]:
            d, c = str(r.get("dt") or ""), num(r.get("cur_prc"))
            if len(d) == 8 and c:
                out.append({"date": datetime.date(int(d[:4]), int(d[4:6]), int(d[6:])), "open": num(r.get("open_pric")),
                            "high": num(r.get("high_pric")), "low": num(r.get("low_pric")), "close": c,
                            "volume": num(r.get("trde_qty")) or 0})
        return sorted(out, key=lambda x: x["date"])

    def minute(self, code, days=20, scope=5):
        rows, key = [], None
        body = {"stk_cd": code, "tic_scope": str(scope), "upd_stkpc_tp": "1"}
        for _ in range(40):
            doc, rh = self.call("/api/dostk/chart", "ka10080", body, key)
            rows.extend(doc.get("stk_min_pole_chart_qry") or [])
            key = rh.get("next-key") if rh.get("cont-yn") == "Y" else None
            if not key or len({str(r.get("cntr_tm", ""))[:8] for r in rows}) > days:
                break
        return rows

    # ------------------------------------------------------------ account

    def order(self, side, code, qty):
        """Market order on KRX. Returns the order number."""
        api_id = {"buy": "kt10000", "sell": "kt10001"}[side]
        body = {"dmst_stex_tp": "KRX", "stk_cd": code, "ord_qty": str(int(qty)), "ord_uv": "", "trde_tp": "3",
                "cond_uv": ""}
        doc, _ = self.call("/api/dostk/ordr", api_id, body)
        return doc.get("ord_no")

    def holdings(self):
        """{code: {"qty", "avg_price"}} from the account balance."""
        doc, _ = self.call("/api/dostk/acnt", "kt00018", {"qry_tp": "1", "dmst_stex_tp": "KRX"})
        out = {}
        for r in doc.get("acnt_evlt_remn_indv_tot") or []:
            code = str(r.get("stk_cd") or "").strip().lstrip("A")
            qty = num(r.get("rmnd_qty")) or 0
            if code and qty:
                out[code] = {"qty": qty, "avg_price": num(r.get("pur_pric"))}
        return out
