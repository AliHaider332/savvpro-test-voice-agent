"""Deterministic date/time extraction for ClinicCall (stdlib only, no I/O).

parse_date / parse_time only *extract*. check_bookable_date validates against
clinic rules. "today" is always injected so tests are deterministic.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, time, timedelta

HORIZON_DAYS = 14
SLOT_MINUTES = 30
MORNING_OPEN, MORNING_CLOSE = time(9, 0), time(13, 0)
AFTERNOON_OPEN, AFTERNOON_CLOSE = time(14, 0), time(17, 0)

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12, "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
    "aug": 8, "sept": 9, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4,
    "saturday": 5, "sunday": 6, "tues": 1, "thurs": 3, "thur": 3,
}


@dataclass(frozen=True)
class DateResult:
    matched: bool = False          # did the text contain a date expression?
    value: date | None = None
    error: str | None = None       # INVALID_DATE | AMBIGUOUS_NUMERIC


@dataclass(frozen=True)
class TimeResult:
    matched: bool = False
    value: time | None = None      # snapped to the 30-minute slot grid
    period: str | None = None      # "morning" | "afternoon" (as spoken)
    error: str | None = None       # INVALID_TIME | OUT_OF_HOURS | LUNCH_BREAK
    snapped: bool = False          # True if value differs from what was said


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().strip())


# ------------------------------------------------------------------ dates

_MON = "|".join(sorted(MONTHS, key=len, reverse=True))
_WD = "|".join(sorted(WEEKDAYS, key=len, reverse=True))

_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_SLASH = re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b")
_IN_DAYS = re.compile(r"\bin (\d{1,2}) days?\b")
_MONTH_THEN_DAY = re.compile(rf"\b({_MON})\b\.?\s*(\d{{1,2}})(?:st|nd|rd|th)?\b")
_DAY_THEN_MONTH = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?(?:\s+of)?\s+({_MON})\b")
_WEEKDAY = re.compile(rf"\b(?:(next)\s+|this\s+)?({_WD})\b")
_BARE_ORDINAL = re.compile(r"\bthe (\d{1,2})(?:st|nd|rd|th)\b")
_ORDINAL = re.compile(r"\b\d{1,2}(?:st|nd|rd|th)\b")

_INVALID = DateResult(True, None, "INVALID_DATE")


def _build(y: int, m: int, d: int) -> DateResult:
    try:
        return DateResult(True, date(y, m, d))
    except ValueError:
        return _INVALID


def _month_day(month: int, day: int, today: date) -> DateResult:
    try:
        cand = date(today.year, month, day)
    except ValueError:
        return _INVALID
    if cand < today and (today - cand).days > 180:
        return _build(today.year + 1, month, day)
    return DateResult(True, cand)


def parse_date(text: str, today: date) -> DateResult:
    t = _norm(text)

    if m := _ISO.search(t):
        return _build(*map(int, m.groups()))
    if re.search(r"\bday after tomorrow\b", t):
        return DateResult(True, today + timedelta(days=2))
    if re.search(r"\btomorrow\b", t):
        return DateResult(True, today + timedelta(days=1))
    if re.search(r"\btoday\b", t):
        return DateResult(True, today)
    if m := _IN_DAYS.search(t):
        return DateResult(True, today + timedelta(days=int(m.group(1))))
    if m := _MONTH_THEN_DAY.search(t):
        return _month_day(MONTHS[m.group(1)], int(m.group(2)), today)
    if m := _DAY_THEN_MONTH.search(t):
        return _month_day(MONTHS[m.group(2)], int(m.group(1)), today)
    if _SLASH.search(t):
        return DateResult(True, None, "AMBIGUOUS_NUMERIC")
    if m := _WEEKDAY.search(t):
        idx = WEEKDAYS[m.group(2)]
        if m.group(1):  # "next <weekday>": that weekday in the next calendar week
            next_monday = today + timedelta(days=7 - today.weekday())
            return DateResult(True, next_monday + timedelta(days=idx))
        delta = (idx - today.weekday()) % 7 or 7
        return DateResult(True, today + timedelta(days=delta))
    if m := _BARE_ORDINAL.search(t):
        n = int(m.group(1))
        year, month = today.year, today.month
        if n < today.day:
            month += 1
            if month == 13:
                month, year = 1, year + 1
        return _build(year, month, n)
    return DateResult()


def check_bookable_date(d: date, today: date) -> str | None:
    """Return an error code if the clinic cannot take this date, else None."""
    if d < today:
        return "PAST_DATE"
    if (d - today).days > HORIZON_DAYS:
        return "BEYOND_HORIZON"
    if d.weekday() == 6:
        return "CLOSED_SUNDAY"
    return None


# ------------------------------------------------------------------ times

_GREETING = re.compile(r"\bgood (?:morning|afternoon|evening|night)\b")
_HHMM = re.compile(r"(?<![\d:.])(\d{1,2})[:.](\d{2})(?!\d)\s*(a\.?m\.?|p\.?m\.?)?(?![a-z])")
_H_AMPM = re.compile(r"(?<![\d:.])(\d{1,2})\s*(a\.?m\.?|p\.?m\.?)(?![a-z])")
_OCLOCK = re.compile(r"(?<![\d:.])(\d{1,2})\s*o'?\s?clock")
_TRIGGER = re.compile(
    r"\b(?:at|around|about|by|near|like|say)\s+(\d{1,2})(?![\d:]|\s*(?:st|nd|rd|th)\b)"
)
_BARE = re.compile(r"(?<![\d:/.-])(\d{1,2})(?![\d:/.-])")

# Removed before time extraction so date words are never read as clock times.
_STRIP_FOR_TIME = (_GREETING, _ISO, _SLASH, _IN_DAYS, _MONTH_THEN_DAY,
                   _DAY_THEN_MONTH, _ORDINAL)


def _in_hours(t: time) -> bool:
    return (MORNING_OPEN <= t < MORNING_CLOSE) or (AFTERNOON_OPEN <= t < AFTERNOON_CLOSE)


def is_slot_time(t: time) -> bool:
    return t.minute % SLOT_MINUTES == 0 and t.second == 0 and _in_hours(t)


def snap_to_slot(t: time) -> time | None:
    """Nearest 30-min slot start (ties round up); falls back to the other
    neighbour if the first choice is outside open hours (e.g. 12:45 -> 12:30)."""
    m = t.hour * 60 + t.minute
    down = m - m % SLOT_MINUTES
    up = down if m % SLOT_MINUTES == 0 else down + SLOT_MINUTES
    order = (down, up) if m % SLOT_MINUTES < SLOT_MINUTES // 2 else (up, down)
    for cand in order:
        if cand >= 24 * 60:
            continue
        ct = time(cand // 60, cand % 60)
        if is_slot_time(ct):
            return ct
    return None


def _meridiem(token: str | None) -> str | None:
    return None if not token else ("am" if token[0] == "a" else "pm")


def _extract_clock(t: str, bare_ok: bool) -> tuple[int, int, str | None] | None:
    if re.search(r"\b(?:noon|midday)\b", t):
        return 12, 0, None
    if m := _HHMM.search(t):
        return int(m.group(1)), int(m.group(2)), _meridiem(m.group(3))
    if m := _H_AMPM.search(t):
        return int(m.group(1)), 0, _meridiem(m.group(2))
    if m := _OCLOCK.search(t):
        return int(m.group(1)), 0, None
    if m := _TRIGGER.search(t):
        return int(m.group(1)), 0, None
    if bare_ok and (m := _BARE.search(t)):
        return int(m.group(1)), 0, None
    return None


def _to_24h(h: int, mi: int, mer: str | None, period: str | None) -> time | None:
    if mi > 59 or h > 23:
        return None
    if mer:
        if not 1 <= h <= 12:
            return None
        h24 = (0 if h == 12 else h) if mer == "am" else (12 if h == 12 else h + 12)
    elif h > 12 or h == 0:
        h24 = h                                   # already 24-hour
    elif period == "morning":
        h24 = h
    elif period in ("afternoon", "evening"):
        h24 = h if h == 12 else h + 12
    elif h == 12 or 9 <= h <= 11:
        h24 = h
    elif 1 <= h <= 5:
        h24 = h + 12
    else:
        h24 = h                                   # 6-8: closed either way
    return time(h24, mi)


def parse_time(text: str, bare_ok: bool = False) -> TimeResult:
    """bare_ok=True lets a lone number ("11") count as a time; the engine sets
    it only when it just asked the caller for a time."""
    t = _norm(text)
    for pat in _STRIP_FOR_TIME:
        t = pat.sub(" ", t)

    if re.search(r"\bmorning\b", t):
        period = "morning"
    elif re.search(r"\bafternoon\b", t):
        period = "afternoon"
    elif re.search(r"\b(?:evening|night|tonight)\b", t):
        period = "evening"
    else:
        period = None
    spoken = period if period in ("morning", "afternoon") else None

    clock = _extract_clock(t, bare_ok)
    if clock is None:
        if period == "evening":
            return TimeResult(True, None, None, "OUT_OF_HOURS")
        if spoken:
            return TimeResult(True, None, spoken)
        return TimeResult()

    raw = _to_24h(*clock, period)
    if raw is None:
        return TimeResult(True, None, spoken, "INVALID_TIME")
    if not _in_hours(raw):
        code = "LUNCH_BREAK" if MORNING_CLOSE <= raw < AFTERNOON_OPEN else "OUT_OF_HOURS"
        return TimeResult(True, None, spoken, code)

    snapped = snap_to_slot(raw)
    return TimeResult(True, snapped, spoken, None, snapped != raw)


# ------------------------------------------------------- spoken formatting

def format_time(t: time) -> str:
    h12 = t.hour % 12 or 12
    suffix = "AM" if t.hour < 12 else "PM"
    return f"{h12} {suffix}" if t.minute == 0 else f"{h12}:{t.minute:02d} {suffix}"


def format_date(d: date, today: date) -> str:
    if d == today:
        return "today"
    if d == today + timedelta(days=1):
        return "tomorrow"
    return f"{calendar.day_name[d.weekday()]} {d.day} {calendar.month_name[d.month]}"