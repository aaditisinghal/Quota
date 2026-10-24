"""SimClock plus the date, period and quarter helpers everything else uses.

Timestamps are ISO-8601 UTC strings with Z (2026-04-02T17:14:00Z), so string order is time order.
"""

import calendar
from datetime import datetime, timedelta, timezone

TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def parse_ts(ts: str) -> datetime:
    return datetime.strptime(ts, TS_FORMAT).replace(tzinfo=timezone.utc)


def fmt_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(TS_FORMAT)


def start_of_day(date: str) -> str:
    return date if "T" in date else f"{date}T00:00:00Z"


def end_of_day(date: str) -> str:
    return date if "T" in date else f"{date}T23:59:59Z"


def in_window(effective_from: str, expires_at: str, now: str) -> bool:
    """effective_from <= now <= expires_at, where bare dates cover the whole day."""
    return start_of_day(effective_from) <= now <= end_of_day(expires_at)


def period_of(date_or_ts: str) -> str:
    return date_or_ts[:7]


def add_periods(period: str, n: int) -> str:
    year, month = int(period[:4]), int(period[5:7])
    index = year * 12 + (month - 1) + n
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def period_end(period: str) -> str:
    year, month = int(period[:4]), int(period[5:7])
    return f"{period}-{calendar.monthrange(year, month)[1]:02d}"


def add_months(date: str, months: int) -> str:
    """Same day-of-month `months` later, clamped to month end (2026-03-31 + 6 -> 2026-09-30)."""
    period = add_periods(date[:7], months)
    year, month = int(period[:4]), int(period[5:7])
    day = min(int(date[8:10]), calendar.monthrange(year, month)[1])
    return f"{period}-{day:02d}"


def add_days(ts: str, days: int) -> str:
    return fmt_ts(parse_ts(ts) + timedelta(days=days))


def add_minutes(ts: str, minutes: int) -> str:
    return fmt_ts(parse_ts(ts) + timedelta(minutes=minutes))


def add_business_days(ts: str, days: int) -> str:
    """Same time of day, `days` weekdays later (Saturday and Sunday are skipped)."""
    moment = parse_ts(ts)
    while days > 0:
        moment += timedelta(days=1)
        if moment.weekday() < 5:
            days -= 1
    return fmt_ts(moment)


class SimClock:
    """Simulated clock. It only moves forward; moving it backward raises."""

    def __init__(self, start: str):
        parse_ts(start)
        self._now = start

    def now(self) -> str:
        return self._now

    def advance_to(self, ts: str) -> str:
        parse_ts(ts)
        if ts < self._now:
            raise ValueError(f"CLOCK_BACKWARDS: {ts} is before {self._now}")
        self._now = ts
        return self._now
