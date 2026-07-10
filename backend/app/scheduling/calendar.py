"""Working-day date arithmetic for P6 calendars.

All CPM math runs at whole-working-day granularity (P6 computes in hours;
the ±1-day cross-check tolerance absorbs the difference).  A ``WorkCalendar``
knows its working weekdays, holiday exceptions, and hours-per-day (used only
for hour→day conversions, never inside date arithmetic).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Dict, Iterable, List, Optional

# Python date.weekday(): Mon=0 … Sun=6
_DAY_NUM = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}

# Safety cap on any single date scan (≈10 years) so a degenerate calendar
# (no working days) can never loop forever.
_MAX_SCAN = 3660


def _to_date(value) -> Optional[date]:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


class WorkCalendar:
    """Date arithmetic over a set of working weekdays minus exception dates."""

    def __init__(
        self,
        work_days: Iterable[str] = ("Mon", "Tue", "Wed", "Thu", "Fri"),
        exceptions: Iterable = (),
        hours_per_day: float = 8.0,
        name: str = "",
        work_exceptions: Iterable = (),
    ) -> None:
        nums = {_DAY_NUM[d] for d in work_days if d in _DAY_NUM}
        self.weekday_nums = nums or {0, 1, 2, 3, 4}

        def _date_set(items) -> set:
            out: set[date] = set()
            for item in items:
                if isinstance(item, dict):
                    item = item.get("date")
                d = _to_date(item)
                if d:
                    out.add(d)
            return out

        self.exceptions = _date_set(exceptions)            # non-working overrides
        self.work_exceptions = _date_set(work_exceptions)  # working overrides
        self.hours_per_day = float(hours_per_day or 8.0)
        self.name = name

    def is_work_day(self, d: date) -> bool:
        if d in self.exceptions:
            return False
        if d in self.work_exceptions:
            return True
        return d.weekday() in self.weekday_nums

    def next_work_day(self, d: date) -> date:
        """First working day on or after ``d``."""
        for _ in range(_MAX_SCAN):
            if self.is_work_day(d):
                return d
            d += timedelta(days=1)
        return d

    def prev_work_day(self, d: date) -> date:
        """First working day on or before ``d``."""
        for _ in range(_MAX_SCAN):
            if self.is_work_day(d):
                return d
            d -= timedelta(days=1)
        return d

    def add_work_days(self, d: date, n: int) -> date:
        """Date ``n`` working days from ``d`` (signed; n=0 returns ``d``)."""
        if n == 0:
            return d
        step = timedelta(days=1 if n > 0 else -1)
        remaining = abs(n)
        guard = _MAX_SCAN + remaining * 8
        cur = d
        while remaining > 0 and guard > 0:
            cur += step
            if self.is_work_day(cur):
                remaining -= 1
            guard -= 1
        return cur

    def work_days_between(self, a: date, b: date) -> int:
        """Signed count of working days from ``a`` to ``b``.

        Counts working days in (a, b] when b > a, negated for b < a, and 0
        when the dates are equal — so ``work_days_between(es, ls)`` is the
        total float in working days.
        """
        if a == b:
            return 0
        sign = 1
        if b < a:
            a, b = b, a
            sign = -1
        count = 0
        cur = a + timedelta(days=1)
        guard = _MAX_SCAN * 3
        while cur <= b and guard > 0:
            if self.is_work_day(cur):
                count += 1
            cur += timedelta(days=1)
            guard -= 1
        return sign * count


DEFAULT_CALENDAR = WorkCalendar()


def build_calendars(calendars: List[dict]) -> Dict[str, WorkCalendar]:
    """Build a clndr_id → WorkCalendar map from ``parse_xer_calendars`` output."""
    result: Dict[str, WorkCalendar] = {}
    for cal in calendars or []:
        cal_id = str(cal.get("id", ""))
        if not cal_id:
            continue
        result[cal_id] = WorkCalendar(
            work_days=cal.get("work_days") or ("Mon", "Tue", "Wed", "Thu", "Fri"),
            exceptions=cal.get("exceptions") or (),
            hours_per_day=cal.get("day_hr_cnt") or 8.0,
            name=cal.get("name", ""),
            work_exceptions=cal.get("work_exceptions") or (),
        )
    return result
