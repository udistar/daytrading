"""Prices for the engine: live from Kiwoom, or replayed from saved 5-minute bars."""
import datetime

from .kiwoom import num


class KiwoomFeed:
    def __init__(self, client):
        self.client = client
        self._prev = {}

    def quote(self, code, now):
        return self.client.quote(code)

    def prev_day(self, code, today):
        if code not in self._prev:
            rows = [r for r in self.client.daily(code, days=10) if r["date"] < today]
            if not rows:
                raise ValueError("no daily bar before %s for %s" % (today, code))
            self._prev[code] = (rows[-1]["high"], rows[-1]["low"])
        return self._prev[code]


def parse_minute_rows(rows):
    """Kiwoom ka10080 rows -> {date: [{"m": minutes after midnight (bar start), o,h,l,c,v}]} for 09:00-15:30."""
    bars = {}
    for r in rows:
        t = str(r.get("cntr_tm") or "")
        c = num(r.get("cur_prc"))
        if len(t) < 12 or not c:
            continue
        dt = datetime.datetime.strptime(t[:12], "%Y%m%d%H%M")
        m = dt.hour * 60 + dt.minute
        if not 9 * 60 <= m <= 15 * 60 + 30:
            continue
        bars.setdefault(dt.date(), {})[m] = {"m": m, "o": num(r.get("open_pric")) or c, "h": num(r.get("high_pric")) or c,
                                             "l": num(r.get("low_pric")) or c, "c": c, "v": num(r.get("trde_qty")) or 0}
    return {d: [v[m] for m in sorted(v)] for d, v in bars.items()}


class ReplayFeed:
    """Walks each 5-minute bar as open -> one extreme -> the other -> close
    (low first on an up bar, high first on a down bar), at minutes 0, 1, 3 and 4."""

    def __init__(self, sessions, daily=None):
        self.sessions = sessions            # {code: {date: [bars]}}
        self.daily = daily or {}            # {code: [{"date","high","low",...}]}
        self._paths = {}

    def _path(self, code, day):
        key = (code, day)
        if key not in self._paths:
            pts = []
            for b in self.sessions.get(code, {}).get(day, []):
                first, second = (b["l"], b["h"]) if b["c"] >= b["o"] else (b["h"], b["l"])
                pts += [(b["m"], b["o"]), (b["m"] + 1, first), (b["m"] + 3, second), (b["m"] + 4, b["c"])]
            self._paths[key] = pts
        return self._paths[key]

    def quote(self, code, now):
        pts = self._path(code, now.date())
        m = now.hour * 60 + now.minute
        last = None
        for t, p in pts:
            if t > m:
                break
            last = p
        if last is None:
            return None
        return {"price": last, "open": pts[0][1], "upper": None, "lower": None}

    def prev_day(self, code, today):
        rows = [r for r in self.daily.get(code, []) if r["date"] < today]
        if rows:
            return rows[-1]["high"], rows[-1]["low"]
        days = sorted(d for d in self.sessions.get(code, {}) if d < today)
        if not days:
            raise ValueError("no day before %s for %s" % (today, code))
        bars = self.sessions[code][days[-1]]
        return max(b["h"] for b in bars), min(b["l"] for b in bars)

    def days(self, code):
        return sorted(self.sessions.get(code, {}))
