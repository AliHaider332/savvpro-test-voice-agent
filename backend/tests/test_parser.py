from datetime import date, time
from uuid import uuid4

import pytest

from app.brain.parser import parse
from app.brain.states import Intent, State, Flow
from app.schemas import Context, Entities, OfferedSlot

TODAY = date(2026, 10, 26)  # Monday


def p(text, **kw):
    kw.setdefault("today", TODAY)
    return parse(text, **kw)


def ctx(flow=None, **entities):
    return Context(flow=flow, entities=Entities(**entities))


def offered(n=3):
    return [OfferedSlot(idx=i, slot_id=uuid4(), label=f"slot {i}") for i in range(1, n + 1)]


# ------------------------------------------------------------ flow intents

@pytest.mark.parametrize("text,intent", [
    ("I want to book an appointment", Intent.BOOK_APPOINTMENT),
    ("Book Dr. Ahmed tomorrow", Intent.BOOK_APPOINTMENT),
    ("I need an appointment", Intent.BOOK_APPOINTMENT),
    ("I'd like to make an appointment with Dr. Khan", Intent.BOOK_APPOINTMENT),
    ("Can I see a doctor", Intent.BOOK_APPOINTMENT),
    ("Hi, I need an appointment", Intent.BOOK_APPOINTMENT),
    ("Good morning, I'd like to book", Intent.BOOK_APPOINTMENT),
    ("I want to reschedule my appointment", Intent.RESCHEDULE_APPOINTMENT),
    ("Move my appointment", Intent.RESCHEDULE_APPOINTMENT),
    ("Please change my booking", Intent.RESCHEDULE_APPOINTMENT),
    ("Reschedule CL-100002", Intent.RESCHEDULE_APPOINTMENT),
    ("Cancel my appointment", Intent.CANCEL_APPOINTMENT),
    ("I want to cancel my booking", Intent.CANCEL_APPOINTMENT),
    ("Cancel CL-100003", Intent.CANCEL_APPOINTMENT),
    ("cancel", Intent.CANCEL_APPOINTMENT),
])
def test_flow_intents(text, intent):
    r = p(text)
    assert r.intent == intent
    assert r.rule


def test_book_with_doctor_and_date():
    r = p("Book Dr. Ahmed tomorrow")
    assert r.entities.doctor_slug == "ahmed"
    assert r.entities.doctor_name == "Dr. Ahmed"
    assert r.entities.date == date(2026, 10, 27)


def test_book_with_everything_in_one_sentence():
    r = p("Book Dr. Khan on Thursday around 2 in the afternoon")
    e = r.entities
    assert (e.doctor_slug, e.date, e.time, e.time_period) == (
        "khan", date(2026, 10, 29), time(14, 0), "afternoon")


def test_reschedule_entities_go_to_new_fields():
    r = p("Move my appointment to Thursday at 3")
    assert r.intent == Intent.RESCHEDULE_APPOINTMENT
    assert r.entities.new_date == date(2026, 10, 29)
    assert r.entities.new_time == time(15, 0)
    assert r.entities.date is None and r.entities.time is None


def test_reschedule_context_routes_date_time_to_new_fields():
    r = p("Thursday at 3", context=ctx(Flow.RESCHEDULE))
    assert r.entities.new_date == date(2026, 10, 29)
    assert r.entities.new_time == time(15, 0)
    assert r.intent == Intent.PROVIDE_INFORMATION


def test_change_appointment_inside_booking_is_a_correction_not_a_reschedule():
    r = p("change the appointment to Friday",
          context=ctx(Flow.BOOK, date=date(2026, 10, 27)))
    assert r.intent == Intent.CORRECT_INFORMATION
    assert r.entities.date == date(2026, 10, 30)
    assert r.changed_fields == ["date"]


# ------------------------------------------------------------------ cancel

def test_bare_cancel_flag():
    assert p("cancel").bare_cancel is True
    assert p("I want to cancel").bare_cancel is True
    assert p("cancel the booking").bare_cancel is True


def test_cancel_my_appointment_is_not_bare():
    assert p("Cancel my appointment").bare_cancel is False
    assert p("Cancel CL-100003").bare_cancel is False


@pytest.mark.parametrize("text", [
    "never mind", "forget it", "start over", "stop", "cancel that",
    "cancel the request", "no, cancel", "don't book", "Never mind please",
])
def test_conversation_cancel(text):
    assert p(text).intent == Intent.CANCEL_CONVERSATION


# ---------------------------------------------------------- confirm / decline

@pytest.mark.parametrize("text", [
    "yes", "Yeah", "yes please", "that's right", "go ahead", "sure", "Okay",
    "correct", "yes, confirm", "sounds good", "do it", "that is correct",
])
def test_pure_confirm(text):
    assert p(text).intent == Intent.CONFIRM


def test_yes_but_with_new_info_is_not_a_confirmation():
    r = p("yes but make it noon", state=State.AWAITING_CONFIRMATION)
    assert r.intent == Intent.CORRECT_INFORMATION
    assert r.entities.time == time(12, 0)


def test_yes_but_marks_changed_field():
    r = p("yes but make it noon", context=ctx(Flow.BOOK, time=time(11, 0)))
    assert r.intent == Intent.CORRECT_INFORMATION
    assert r.changed_fields == ["time"]


@pytest.mark.parametrize("text", ["no", "nope", "No thanks", "that's wrong", "not that"])
def test_decline(text):
    assert p(text).intent == Intent.DECLINE


def test_confirm_with_moderate_confidence_is_flagged_not_allowed():
    r = p("yes", stt_confidence=0.5)
    assert r.intent == Intent.CONFIRM and r.confirm_allowed is False


def test_confirm_with_good_confidence_or_text_is_allowed():
    assert p("yes", stt_confidence=0.9).confirm_allowed is True
    assert p("yes").confirm_allowed is True


# ---------------------------------------------------------- empty / low conf

@pytest.mark.parametrize("text", ["", "   ", "?", "...", "a"])
def test_empty_or_garbled(text):
    r = p(text)
    assert r.quality == "empty" and r.intent == Intent.UNKNOWN and r.confirm_allowed is False


def test_single_digit_is_not_empty():
    assert p("2").quality == "ok"


def test_very_low_confidence():
    r = p("yes", stt_confidence=0.2)
    assert r.quality == "low_confidence" and r.intent == Intent.UNKNOWN
    assert r.confirm_allowed is False


# ------------------------------------------------------------------ corrections

def test_actually_make_it_friday_changes_date():
    r = p("Actually make it Friday", context=ctx(Flow.BOOK, date=date(2026, 10, 27)))
    assert r.intent == Intent.CORRECT_INFORMATION
    assert r.entities.date == date(2026, 10, 30)
    assert r.changed_fields == ["date"]


def test_switch_doctor():
    r = p("Switch to Dr. Khan", context=ctx(Flow.BOOK, doctor_slug="ahmed", doctor_name="Dr. Ahmed"))
    assert r.intent == Intent.CORRECT_INFORMATION
    assert r.entities.doctor_slug == "khan"
    assert r.changed_fields == ["doctor_slug"]


def test_name_correction_while_awaiting_confirmation():
    r = p("No, the name is Sara", state=State.AWAITING_CONFIRMATION)
    assert r.intent == Intent.CORRECT_INFORMATION
    assert r.entities.patient_name == "Sara"


def test_marker_only_is_correction_without_entities():
    r = p("actually")
    assert r.intent == Intent.CORRECT_INFORMATION and r.rule == "correction_marker_only"


def test_same_value_is_not_a_change():
    r = p("tomorrow", context=ctx(Flow.BOOK, date=date(2026, 10, 27)))
    assert r.changed_fields == []
    assert r.intent == Intent.PROVIDE_INFORMATION


# ---------------------------------------------------------------- slot filling

def test_bare_time_needs_expectation():
    assert p("11", context=ctx(Flow.BOOK)).intent == Intent.UNKNOWN
    r = p("11", context=ctx(Flow.BOOK), expected_field="time")
    assert r.entities.time == time(11, 0) and r.intent == Intent.PROVIDE_INFORMATION


def test_around_eleven_in_the_morning():
    r = p("Around 11 in the morning", context=ctx(Flow.BOOK), expected_field="time")
    assert r.entities.time == time(11, 0) and r.entities.time_period == "morning"


def test_period_only():
    r = p("in the afternoon", context=ctx(Flow.BOOK))
    assert r.entities.time is None and r.entities.time_period == "afternoon"


# ------------------------------------------------------------------------ names

@pytest.mark.parametrize("text,field,expected", [
    ("Ali", "patient_name", "Ali"),
    ("Sara Khan", "patient_name", "Sara Khan"),
    ("Ahmed", "patient_name", "Ahmed"),
    ("I'm Sara", "patient_name", "Sara"),
    ("it's Omar", "patient_name", "Omar"),
])
def test_bare_and_weak_names_when_expected(text, field, expected):
    r = p(text, context=ctx(Flow.BOOK), expected_field=field)
    assert r.entities.patient_name == expected
    assert r.entities.doctor_slug is None


@pytest.mark.parametrize("text,expected", [
    ("my name is Ali Khan", "Ali Khan"),
    ("book under the name Ali", "Ali"),
    ("The name is Sara", "Sara"),
    ("name is Ali at 11", "Ali"),
])
def test_explicit_names_without_expectation(text, expected):
    assert p(text).entities.patient_name == expected


def test_explicit_name_is_not_read_as_doctor():
    r = p("my name is Ahmed")
    assert r.entities.patient_name == "Ahmed" and r.entities.doctor_slug is None


def test_name_and_doctor_together():
    r = p("Book Dr. Ahmed tomorrow under Ali")
    assert r.entities.patient_name == "Ali"
    assert r.entities.doctor_slug == "ahmed"


def test_weak_trigger_ignored_when_name_not_expected():
    r = p("I'm looking for Dr Ahmed")
    assert r.entities.patient_name is None and r.entities.doctor_slug == "ahmed"


def test_name_goes_to_verify_name_in_reschedule_and_cancel_flows():
    r = p("Maria", context=ctx(Flow.CANCEL), expected_field="verify_name")
    assert r.entities.verify_name == "Maria" and r.entities.patient_name is None
    r = p("the name is Maria", context=ctx(Flow.RESCHEDULE))
    assert r.entities.verify_name == "Maria"


def test_booking_flow_bare_word_without_expectation_is_not_a_name():
    assert p("Ali", context=ctx(Flow.BOOK)).intent == Intent.UNKNOWN


# ---------------------------------------------------------------------- doctors

@pytest.mark.parametrize("text,slug", [
    ("Dr. Ahmed", "ahmed"), ("doctor khan", "khan"), ("Dr Malik", "malik"),
    ("dr sara", "khan"), ("doctor hina", "malik"),
    ("doctor ahmad", "ahmed"), ("dr malek", "malik"),     # fuzzy after "dr"
    ("Khan", "khan"),                                     # bare surname
])
def test_doctor_aliases(text, slug):
    assert p(text).entities.doctor_slug == slug


def test_bare_surname_with_doctor_expected():
    assert p("Ahmed", expected_field="doctor").entities.doctor_slug == "ahmed"


def test_unknown_doctor_is_reported():
    r = p("book dr smith")
    assert r.intent == Intent.BOOK_APPOINTMENT
    assert r.doctor_unknown == "Smith" and r.entities.doctor_slug is None


def test_generic_doctor_word_is_not_an_unknown_doctor():
    r = p("see a doctor tomorrow")
    assert r.doctor_unknown is None and r.entities.date == date(2026, 10, 27)


# ------------------------------------------------------------------- references

@pytest.mark.parametrize("text", ["CL-482913", "cl 482913", "CL482913", "my reference is CL 482 913"])
def test_reference_with_prefix(text):
    assert p(text, context=ctx(Flow.CANCEL)).entities.booking_reference == "CL-482913"


def test_reference_bare_digits_only_when_expected():
    assert p("482913").entities.booking_reference is None
    r = p("482913", expected_field="booking_reference")
    assert r.entities.booking_reference == "CL-482913"


@pytest.mark.parametrize("text", [
    "four eight two nine one three",
    "CL four eight two nine one three",
])
def test_spoken_digit_reference(text):
    r = p(text, expected_field="booking_reference")
    assert r.entities.booking_reference == "CL-482913"


def test_spoken_oh_is_zero():
    r = p("CL one oh oh oh oh two")
    assert r.entities.booking_reference == "CL-100002"


def test_reference_digits_are_not_read_as_time_or_date():
    r = p("cancel CL-100003")
    assert r.entities.time is None and r.entities.date is None and r.errors == {}


# ----------------------------------------------------------------- date errors

@pytest.mark.parametrize("text,field,code", [
    ("book for the 32nd", "date", "INVALID_DATE"),
    ("book on sunday", "date", "CLOSED_SUNDAY"),
    ("book 5/3", "date", "AMBIGUOUS_NUMERIC"),
    ("book on october 20", "date", "PAST_DATE"),
    ("book on december 20", "date", "BEYOND_HORIZON"),
    ("book at 7", "time", "OUT_OF_HOURS"),
    ("book at 1", "time", "LUNCH_BREAK"),
])
def test_date_time_errors_are_reported_not_stored(text, field, code):
    r = p(text)
    assert r.intent == Intent.BOOK_APPOINTMENT
    assert r.errors[field] == code
    assert getattr(r.entities, field) is None


def test_error_only_turn_is_provide_information():
    r = p("the 32nd", context=ctx(Flow.BOOK))
    assert r.intent == Intent.PROVIDE_INFORMATION and r.errors["date"] == "INVALID_DATE"


# ---------------------------------------------------------------------- options

@pytest.mark.parametrize("text,idx", [
    ("the second one", 2), ("first", 1), ("2", 2), ("option 3", 3),
    ("the last one", -1), ("number two", 2), ("the 3rd", 3),
])
def test_option_selection(text, idx):
    r = p(text, context=Context(flow=Flow.BOOK, offered_slots=offered()))
    assert r.intent == Intent.SELECT_OPTION and r.option_index == idx


def test_option_words_ignored_without_offered_slots():
    assert p("the second one", context=ctx(Flow.BOOK)).intent == Intent.UNKNOWN


def test_pick_by_time_is_provide_information_for_engine_to_match():
    r = p("the 10:30 one", context=Context(flow=Flow.BOOK, offered_slots=offered()))
    assert r.intent == Intent.PROVIDE_INFORMATION and r.entities.time == time(10, 30)


# ------------------------------------------------------------- disambiguation

@pytest.mark.parametrize("text,intent", [
    ("stop booking", Intent.CANCEL_CONVERSATION),
    ("never mind", Intent.CANCEL_CONVERSATION),
    ("the request", Intent.CANCEL_CONVERSATION),
    ("stop booking an appointment", Intent.CANCEL_CONVERSATION),
    ("the existing appointment", Intent.CANCEL_APPOINTMENT),
    ("my appointment", Intent.CANCEL_APPOINTMENT),
    ("the appointment", Intent.CANCEL_APPOINTMENT),
    ("I don't know", Intent.UNKNOWN),
])
def test_disambiguation_answers(text, intent):
    assert p(text, state=State.DISAMBIGUATING).intent == intent


# ----------------------------------------------------------------- unsupported

@pytest.mark.parametrize("text", [
    "I have chest pain", "I need a prescription", "what medicine should I take",
    "I have a fever and want an appointment",
])
def test_medical_is_refused_even_with_booking_words(text):
    r = p(text)
    assert r.intent == Intent.UNSUPPORTED and r.unsupported_reason == "medical"


@pytest.mark.parametrize("text", [
    "Can I get a refund", "Can I get a refund for my appointment",
    "how much does it cost", "what is your address", "I want to speak to a human",
])
def test_other_unsupported(text):
    r = p(text)
    assert r.intent == Intent.UNSUPPORTED and r.unsupported_reason == "other"


def test_patient_named_bill_is_not_a_billing_request():
    r = p("my name is Bill")
    assert r.intent != Intent.UNSUPPORTED and r.entities.patient_name == "Bill"


# --------------------------------------------------------------- greeting / unknown

@pytest.mark.parametrize("text", ["hello", "Hi there", "thanks", "goodbye", "Good morning"])
def test_greeting(text):
    assert p(text).intent == Intent.GREETING_GOODBYE


@pytest.mark.parametrize("text", ["banana smoothie", "blah blah blah"])
def test_unknown(text):
    r = p(text)
    assert r.intent == Intent.UNKNOWN and r.rule == "no_match"


# ------------------------------------------------------------------- properties

def test_parser_does_not_mutate_context():
    c = ctx(Flow.BOOK, date=date(2026, 10, 27))
    before = c.model_dump()
    p("Actually make it Friday at noon", context=c)
    assert c.model_dump() == before


def test_parsed_turn_is_json_serializable():
    r = p("Book Dr. Ahmed tomorrow at 11 under Ali")
    data = r.model_dump(mode="json")
    assert data["intent"] == "BOOK_APPOINTMENT"
    assert data["entities"]["time"] == "11:00:00"


def test_same_input_same_output():
    assert p("Book Dr. Khan Friday at 3") == p("Book Dr. Khan Friday at 3")