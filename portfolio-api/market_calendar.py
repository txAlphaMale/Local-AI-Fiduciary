# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""NYSE trading-day calendar and trading-time arithmetic.

2026-08-08. Added because wall-clock aging produces false "dated inputs" flags every weekend.
Jason's case: the daily brief run finished Friday evening, the panel was read Saturday morning,
and a 24h wall-clock window called Friday's analyst consensus dated -- even though the market
had not opened since it was pulled, so no fresher value existed anywhere in the world. A flag
that fires when nothing is wrong is a flag the reader learns to skip.

The fix is to measure age in TRADING TIME: only seconds that fall on an NYSE session day count
toward a source's freshness window. Friday 18:00 -> Saturday 09:00 is 6 hours of trading time,
not 15. Friday 18:00 -> Monday 09:00 is 15, not 63.

Deliberately NOT modelled: intraday session hours (09:30-16:00). Counting only the 6.5-hour
session would make a Tuesday-morning read of Monday-afternoon data look ~2 hours old, which
overstates freshness in the other direction, and pre/post-market news is real. Whole trading
DAYS is the honest granularity for datasets that update at most daily.

All timestamps are epoch seconds. The calendar is evaluated in US/Eastern, since that is the
exchange's day boundary -- a UTC-based day boundary would misclassify Sunday 20:00 ET (= Monday
00:00 UTC) as a trading day.

No third-party dependency (no pandas_market_calendars): the rules below are stable federal
holiday law, this module needs no historical accuracy before ~2020, and adding a dependency to
the container for a date table is a poor trade.
"""

import datetime
import os

try:                                    # py3.9+; the container is 3.11
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:                       # pragma: no cover - defensive only
    _ET = None

DAY = 86400

# Escape hatch: set MARKET_CALENDAR_AGING=0 to fall back to plain wall-clock aging everywhere.
CALENDAR_AGING = (os.environ.get("MARKET_CALENDAR_AGING", "1") or "1").strip().lower() \
    not in ("0", "false", "no", "off")


def _easter(year):
    """Gregorian Easter Sunday (Meeus/Jones/Butcher). Needed only for Good Friday, which is the
    one NYSE closure with no fixed date."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return datetime.date(year, month, day + 1)


def _nth_weekday(year, month, weekday, n):
    """n-th `weekday` (Mon=0) of a month; n=-1 means the last one."""
    if n > 0:
        d = datetime.date(year, month, 1)
        offset = (weekday - d.weekday()) % 7
        return d + datetime.timedelta(days=offset + 7 * (n - 1))
    d = datetime.date(year, month, 1) + datetime.timedelta(days=32)
    d = datetime.date(d.year, d.month, 1) - datetime.timedelta(days=1)   # last day of month
    return d - datetime.timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d):
    """NYSE observes a Saturday holiday on the preceding Friday and a Sunday one on the
    following Monday -- except a Saturday July 4th / New Year's Day, where the exchange simply
    stays open the Friday before. Handled by the caller for New Year's; for the rest the
    standard shift applies."""
    if d.weekday() == 5:
        return d - datetime.timedelta(days=1)
    if d.weekday() == 6:
        return d + datetime.timedelta(days=1)
    return d


_HOLIDAY_CACHE = {}


def holidays(year):
    """Full-day NYSE closures for a calendar year, as a set of dates.

    Not included: the half-days (day after Thanksgiving, Christmas Eve). The market IS open on
    those, so they are trading days for our purposes."""
    if year in _HOLIDAY_CACHE:
        return _HOLIDAY_CACHE[year]
    out = set()
    # New Year's Day -- a Saturday Jan 1 is NOT observed on Dec 31 (exchange stays open).
    ny = datetime.date(year, 1, 1)
    if ny.weekday() == 6:
        out.add(datetime.date(year, 1, 2))
    elif ny.weekday() != 5:
        out.add(ny)
    out.add(_nth_weekday(year, 1, 0, 3))          # MLK Jr Day -- 3rd Monday in January
    out.add(_nth_weekday(year, 2, 0, 3))          # Washington's Birthday -- 3rd Monday in February
    out.add(_easter(year) - datetime.timedelta(days=2))   # Good Friday
    out.add(_nth_weekday(year, 5, 0, -1))         # Memorial Day -- last Monday in May
    if year >= 2022:                              # Juneteenth became an NYSE holiday in 2022
        out.add(_observed(datetime.date(year, 6, 19)))
    out.add(_observed(datetime.date(year, 7, 4)))         # Independence Day
    out.add(_nth_weekday(year, 9, 0, 1))          # Labor Day -- 1st Monday in September
    out.add(_nth_weekday(year, 11, 3, 4))         # Thanksgiving -- 4th Thursday in November
    out.add(_observed(datetime.date(year, 12, 25)))       # Christmas
    _HOLIDAY_CACHE[year] = out
    return out


def is_trading_day(d):
    """True if `d` (a date) is an NYSE session day."""
    return d.weekday() < 5 and d not in holidays(d.year)


def _et_date(ts):
    dt = datetime.datetime.fromtimestamp(ts, _ET) if _ET else datetime.datetime.utcfromtimestamp(ts)
    return dt.date(), dt


def trading_seconds_between(start_ts, end_ts):
    """Seconds elapsed between two epoch timestamps that fall on NYSE session DAYS.

    Weekend and holiday seconds contribute zero, so a value pulled Friday evening does not age
    while the market is shut. Returns 0.0 if end precedes start."""
    if end_ts is None or start_ts is None or end_ts <= start_ts:
        return 0.0
    if not CALENDAR_AGING:
        return float(end_ts - start_ts)

    start_date, start_dt = _et_date(start_ts)
    end_date, end_dt = _et_date(end_ts)

    # Same ET calendar day: all-or-nothing.
    if start_date == end_date:
        return float(end_ts - start_ts) if is_trading_day(start_date) else 0.0

    total = 0.0
    # Partial first day: from start until ET midnight.
    if is_trading_day(start_date):
        midnight = datetime.datetime.combine(start_date + datetime.timedelta(days=1),
                                             datetime.time(0, 0), tzinfo=_ET)
        total += midnight.timestamp() - start_ts
    # Whole days strictly between. Bounded by the span, and spans here are days-to-weeks; a
    # pathological multi-year gap still terminates, it just walks more dates.
    d = start_date + datetime.timedelta(days=1)
    while d < end_date:
        if is_trading_day(d):
            total += DAY
        d += datetime.timedelta(days=1)
    # Partial last day: from ET midnight until end.
    if is_trading_day(end_date):
        midnight = datetime.datetime.combine(end_date, datetime.time(0, 0), tzinfo=_ET)
        total += end_ts - midnight.timestamp()
    return max(0.0, total)


def describe(start_ts, end_ts):
    """Both clocks, for diagnostics and for explaining a flag to the user."""
    wall = None if (start_ts is None or end_ts is None) else max(0.0, end_ts - start_ts)
    market = trading_seconds_between(start_ts, end_ts)
    return {"wall_age": wall, "market_age": market,
            "nontrading_skipped": None if wall is None else round(wall - market, 3),
            "calendar_aging": CALENDAR_AGING}
