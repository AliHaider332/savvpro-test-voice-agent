from datetime import date, time

import pytest

from app.brain.datetime_parse import (
    check_bookable_date, format_date, format_time,
    parse_date, parse_time, snap_to_slot,
)

TODAY = date(2026, 10, 26)  # a Monday


def test_fixture_today_is_monday():
    assert TODAY.weekday() == 0


# ---------------------------------------------------------------- parse_date

@pytest.mark.parametrize("text,expected", [
    ("tomorrow", date(2026, 10, 27)),
    ("book dr ahmed tomorrow", date(2026, 10, 27)),
    ("today", date(2026, 10, 26)),
    ("day after tomorrow", date(2026, 10, 28)),
    ("in 3 days", date(2026, 10, 29)),
    ("thursday", date(2026, 10, 29)),
    ("THURSDAY please", date(2026, 10, 29)),
    ("friday", date(2026, 10, 30)),
    ("this friday", date(2026, 10, 30)),
    ("sunday", date(2026, 11, 1)),
    ("monday", date(2026, 11, 2)),          # strictly after today
    ("next monday", date(2026, 11, 2)),
    ("next thursday", date(2026, 11, 5)),   # next calendar week
    ("november 5", date(2026, 11, 5)),
    ("nov 5th", date(2026, 11, 5)),
    ("5th of november", date(2026, 11, 5)),
    ("the 5th of November", date(2026, 11, 5)),
    ("5 november", date(2026, 11, 5)),
    ("2026-11-05", date(2026, 11, 5)),
    ("the 28th", date(2026, 10, 28)),
    ("the 26th", date(2026, 10, 26)),
    ("the 5th", date(2026, 11, 5)),         # day already passed -> next month
    ("march 5", date(2027, 3, 5)),          # >180 days past -> next year
    ("october 20", date(2026, 10, 20)),     # recent past stays in past
    ("book on thursday around 11 in the morning", date(2026, 10, 29)),
])
def test_parse_date_values(text, expected):
    r = parse_date(text, TODAY)
    assert r.matched and r.error is None and r.value == expected


@pytest.mark.parametrize("text,code", [
    ("book for the 32nd", "INVALID_DATE"),
    ("october 32", "INVALID_DATE"),
    ("february 30", "INVALID_DATE"),
    ("2026-02-30", "INVALID_DATE"),
    ("5/3", "AMBIGUOUS_NUMERIC"),
])
def test_parse_date_errors(text, code):
    r = parse_date(text, TODAY)
    assert r.matched and r.value is None and r.error == code


@pytest.mark.parametrize("text", [
    "", "I want an appointment", "hello there", "can I get a refund",
    "11 in the morning", "may i book",
])
def test_parse_date_no_match(text):
    assert not parse_date(text, TODAY).matched


@pytest.mark.parametrize("d,expected", [
    (date(2026, 10, 25), "PAST_DATE"),
    (date(2026, 10, 26), None),
    (date(2026, 11, 1), "CLOSED_SUNDAY"),
    (date(2026, 11, 9), None),              # exactly today + 14
    (date(2026, 11, 10), "BEYOND_HORIZON"),
    (date(2027, 3, 5), "BEYOND_HORIZON"),
])
def test_check_bookable_date(d, expected):
    assert check_bookable_date(d, TODAY) == expected


# ---------------------------------------------------------------- parse_time

@pytest.mark.parametrize("text,expected", [
    ("11 am", time(11, 0)),
    ("11am", time(11, 0)),
    ("11 a.m.", time(11, 0)),
    ("9 am", time(9, 0)),
    ("11:30", time(11, 30)),
    ("11:30 am", time(11, 30)),
    ("10.30", time(10, 30)),
    ("2 pm", time(14, 0)),
    ("2:30 pm", time(14, 30)),
    ("12:30 pm", time(12, 30)),
    ("14:30", time(14, 30)),
    ("noon", time(12, 0)),
    ("3 o'clock", time(15, 0)),
    ("at 11", time(11, 0)),
    ("at 12", time(12, 0)),
    ("at 9", time(9, 0)),
    ("at 3", time(15, 0)),
    ("at 4", time(16, 0)),
    ("around 11 in the morning", time(11, 0)),
    ("around 2 in the afternoon", time(14, 0)),
    ("tomorrow at 11", time(11, 0)),
    ("november 5 at 3", time(15, 0)),
    ("Around 11", time(11, 0)),
])
def test_parse_time_values(text, expected):
    r = parse_time(text)
    assert r.matched and r.error is None and r.value == expected


@pytest.mark.parametrize("text,expected", [
    ("11:10", time(11, 0)),
    ("11:20", time(11, 30)),
    ("11:15", time(11, 30)),      # tie rounds up
    ("12:45", time(12, 30)),      # 13:00 is closed -> falls back down
    ("16:45", time(16, 30)),      # 17:00 is closed -> falls back down
])
def test_parse_time_snaps_to_grid(text, expected):
    r = parse_time(text)
    assert r.value == expected and r.snapped is True


def test_exact_slot_is_not_marked_snapped():
    assert parse_time("11:30").snapped is False


@pytest.mark.parametrize("text,code", [
    ("at 7", "OUT_OF_HOURS"),
    ("at 8", "OUT_OF_HOURS"),
    ("7 am", "OUT_OF_HOURS"),
    ("8:30 am", "OUT_OF_HOURS"),
    ("at 5", "OUT_OF_HOURS"),              # 5 PM = closing time
    ("5 pm", "OUT_OF_HOURS"),
    ("at 7 in the evening", "OUT_OF_HOURS"),
    ("in the evening", "OUT_OF_HOURS"),
    ("1 pm", "LUNCH_BREAK"),
    ("at 1", "LUNCH_BREAK"),
    ("9:60", "INVALID_TIME"),
    ("25:00", "INVALID_TIME"),
    ("13 pm", "INVALID_TIME"),
])
def test_parse_time_errors(text, code):
    r = parse_time(text)
    assert r.matched and r.value is None and r.error == code


@pytest.mark.parametrize("text,period", [
    ("in the afternoon", "afternoon"),
    ("tomorrow morning", "morning"),
    ("morning", "morning"),
])
def test_period_only(text, period):
    r = parse_time(text)
    assert r.matched and r.value is None and r.period == period and r.error is None


def test_period_is_reported_alongside_time():
    r = parse_time("around 11 in the morning")
    assert r.period == "morning" and r.value == time(11, 0)


@pytest.mark.parametrize("text", [
    "good morning", "Good afternoon, I'd like to book", "I want to book",
    "november 5", "2026-11-05", "in 3 days", "the 5th", "5/3", "",
])
def test_parse_time_no_false_positives(text):
    assert not parse_time(text).matched


def test_bare_number_needs_expectation():
    assert not parse_time("11").matched
    r = parse_time("11", bare_ok=True)
    assert r.value == time(11, 0)


@pytest.mark.parametrize("text,expected", [
    ("3", time(15, 0)),
    ("11 in the morning", time(11, 0)),
    ("2:30", time(14, 30)),
])
def test_bare_ok_values(text, expected):
    assert parse_time(text, bare_ok=True).value == expected


@pytest.mark.parametrize("text", ["in 3 days", "the 5th", "november 5", "2026-11-05"])
def test_bare_ok_still_ignores_dates(text):
    assert not parse_time(text, bare_ok=True).matched


# ------------------------------------------------------------ snap + format

def test_snap_to_slot_direct():
    assert snap_to_slot(time(11, 10)) == time(11, 0)
    assert snap_to_slot(time(16, 45)) == time(16, 30)
    assert snap_to_slot(time(9, 0)) == time(9, 0)


@pytest.mark.parametrize("t,expected", [
    (time(9, 0), "9 AM"), (time(11, 0), "11 AM"), (time(12, 0), "12 PM"),
    (time(15, 30), "3:30 PM"), (time(14, 0), "2 PM"),
])
def test_format_time(t, expected):
    assert format_time(t) == expected


def test_format_date():
    assert format_date(TODAY, TODAY) == "today"
    assert format_date(date(2026, 10, 27), TODAY) == "tomorrow"
    assert format_date(date(2026, 10, 29), TODAY) == "Thursday 29 October"