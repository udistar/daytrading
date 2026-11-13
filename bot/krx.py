"""KRX market rules and the KST -> New York clock mapping.

Standard library only. No time zone database is needed: KST is a fixed UTC+9,
and the US Eastern offset is worked out from the US daylight-saving rule
(second Sunday of March to first Sunday of November), the same way the
day-trader skill pack does it.
"""
import datetime
import math

KST_OFFSET = 9                 # hours, no daylight saving in Korea
KRX_OPEN_MIN = 9 * 60          # 09:00 KST, continuous trading starts
KRX_AUCTION_MIN = 15 * 60 + 20  # 15:20 KST, closing single-price auction starts
KRX_CLOSE_MIN = 15 * 60 + 30   # 15:30 KST, the closing price prints
NY_OPEN_MIN = 9 * 60 + 30      # 09:30 New York
SHIFT_MIN = NY_OPEN_MIN - KRX_OPEN_MIN   # +30 minutes: KST wall time -> NY wall time
BAR_MIN = 5
SESSION_BARS = 78              # 09:00-15:30 KST == 09:30-16:00 NY, in 5-minute bars
CONT_BARS = (KRX_AUCTION_MIN - KRX_OPEN_MIN) // BAR_MIN   # 76 continuous bars, 09:00-15:15 starts
PRICE_LIMIT = 0.30

# KOSPI and KOSDAQ share one tick table since the January 2023 reform.
TICKS = [(2000, 1), (5000, 5), (20000, 10), (50000, 50), (200000, 100), (500000, 500)]


def tick_size(price):
    for bound, tick in TICKS:
        if price < bound:
            return tick
    return 1000


def round_down_tick(price):
    t = tick_size(price)
    return int(math.floor(price / t + 1e-9) * t)


def round_up_tick(price):
    t = tick_size(price)
    v = int(math.ceil(price / t - 1e-9) * t)
    # crossing a band edge upward can land on a price whose tick is larger
    t2 = tick_size(v)
    return v if v % t2 == 0 else int(math.ceil(v / t2) * t2)


def on_tick(price):
    return float(price).is_integer() and int(price) % tick_size(price) == 0


def price_limits(prev_close):
    """(lower, upper) daily limits: +-30% of the previous close, inside the band."""
    return round_up_tick(prev_close * (1 - PRICE_LIMIT)), round_down_tick(prev_close * (1 + PRICE_LIMIT))


# ---------------------------------------------------------------- clocks

def _nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def ny_offset(day):
    """US Eastern UTC offset (hours) for the daytime of `day`."""
    start = _nth_sunday(day.year, 3, 2)
    end = _nth_sunday(day.year, 11, 1)
    return -4 if start <= day < end else -5


def ny_epoch(day, minutes):
    """Epoch seconds of New York wall time `minutes` after midnight on `day`."""
    local = datetime.datetime.combine(day, datetime.time()) + datetime.timedelta(minutes=minutes)
    utc = local - datetime.timedelta(hours=ny_offset(day))
    return int(utc.replace(tzinfo=datetime.timezone.utc).timestamp())


def kst_to_ny_wall(dt):
    """A KST wall-clock datetime -> the New York wall clock the skill will see."""
    return dt + datetime.timedelta(minutes=SHIFT_MIN)


def ny_wall_to_kst(dt):
    return dt - datetime.timedelta(minutes=SHIFT_MIN)


def kst_now():
    return (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=KST_OFFSET)).replace(
        tzinfo=None, microsecond=0)
