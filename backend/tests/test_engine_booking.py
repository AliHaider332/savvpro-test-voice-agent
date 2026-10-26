from datetime import date, time, timedelta

import pytest

from app.brain.states import EventKind as K
from app.brain.states import Intent, State


def kinds(resp):
    return [e.kind for e in resp.events]


def propose_khan(say):
    """Khan, tomorrow 11:00, under Ali -> AWAITING_CONFIRMATION."""
    r = say("Book Dr. Khan tomorrow at 11 under Ali")
    assert r.state == State.AWAITING_CONFIRMATION
    return r


# ------------------------------------------------------- scenario 1: happy path

def test_happy_path_multi_turn(say, repo):
    r = say("Book Dr. Khan tomorrow")
    assert r.state == State.COLLECTING and "What time" in r.assistant_text
    assert r.missing_fields == ["time", "patient_name"]

    r = say("Around 11 in the morning")
    assert "11 AM" in r.assistant_text and "name" in r.assistant_text.lower()
    assert r.missing_fields == ["patient_name"]

    r = say("Ali")
    assert r.state == State.AWAITING_CONFIRMATION
    assert r.pending_action.status == "proposed" and "Ali" in r.pending_action.summary
    assert r.completed_action is None
    assert repo.create_calls == 0                      # nothing written yet

    r = say("Yes")
    assert r.state == State.COMPLETED
    assert r.completed_action.booking_reference == "CL-482913"
    assert "CL-482913" in r.assistant_text
    assert repo.create_calls == 1
    writes = [e for e in r.events if e.kind == K.DB_WRITE]
    assert len(writes) == 1 and writes[0].executed is True
    appt = repo.get_appointment_by_slot(r.completed_action.appointment.slot_id)
    assert appt.patient_name == "Ali" and appt.booking_reference == "CL-482913"


def test_everything_in_one_sentence_goes_straight_to_confirmation(say, repo):
    r = propose_khan(say)
    assert r.intent == Intent.BOOK_APPOINTMENT
    assert "tomorrow at 11 AM" in r.pending_action.summary
    assert repo.create_calls == 0


def test_second_yes_after_completion_does_not_book_again(say, repo):
    propose_khan(say)
    say("yes")
    r = say("yes")
    assert repo.create_calls == 1
    assert any(e.kind == K.REFUSED and e.payload["reason"] == "no_pending_action" for e in r.events)


# ----------------------------------------------------- scenario 2: unavailable

def test_unavailable_slot_offers_alternatives_and_accepts_choice(say, session):
    r = say("Book Dr. Ahmed tomorrow at 11")
    assert r.state == State.COLLECTING and r.pending_action is None
    assert [o.label for o in r.offered_slots] == ["10 AM", "10:30 AM", "11:30 AM"]
    assert "isn't available" in r.assistant_text
    assert session.context.entities.time is None

    r = say("the second one")
    assert session.context.entities.time == time(10, 30)
    assert "10:30 AM" in r.assistant_text and r.offered_slots == []


def test_pick_alternative_by_time(say, session):
    say("Book Dr. Ahmed tomorrow at 11")
    r = say("the 10:30 one")
    assert session.context.entities.time == time(10, 30)
    assert "name" in r.assistant_text.lower()


def test_invalid_option_keeps_offers(say):
    say("Book Dr. Ahmed tomorrow at 11")
    r = say("option 4")
    assert r.state == State.COLLECTING and len(r.offered_slots) == 3
    assert "10:30 AM" in r.assistant_text


def test_period_only_offers_slots_then_last_one(say, session):
    r = say("Book Dr. Malik tomorrow in the afternoon")
    assert [o.label for o in r.offered_slots] == ["12 PM", "12:30 PM", "2 PM"]
    say("the last one")
    assert session.context.entities.time == time(14, 0)


def test_past_time_today_is_not_bookable(say, clock):
    clock.now = clock.now.replace(hour=11)
    r = say("Book Dr. Khan today at 10")
    assert [o.label for o in r.offered_slots] == ["11:30 AM", "12 PM", "12:30 PM"]
    assert "already passed" in r.assistant_text


def test_slot_taken_between_proposal_and_confirmation(say, repo):
    propose_khan(say)
    slot = repo.get_slot("khan", date(2026, 10, 27), time(11, 0))
    repo.create_appointment(slot_id=slot.id, patient_name="Other", idempotency_key="rival")
    r = say("yes")
    assert r.completed_action is None and r.state == State.COLLECTING
    assert len(r.offered_slots) == 3
    assert repo.get_appointment_by_slot(slot.id).patient_name == "Other"
    db = [e for e in r.events if e.kind == K.DB_WRITE]
    assert db and db[0].executed is False


# ------------------------------------------------- scenarios 3, 4, 8: changes

def test_date_change_midway_reissues_confirmation(say, repo):
    r1 = say("Book Dr. Malik tomorrow at 10 under Omar")
    assert r1.state == State.AWAITING_CONFIRMATION
    old_summary = r1.pending_action.summary
    r2 = say("Actually make it Friday")
    assert r2.state == State.AWAITING_CONFIRMATION
    assert "Friday 30 October" in r2.pending_action.summary
    assert r2.pending_action.summary != old_summary
    assert K.STALE_PENDING_CLEARED in kinds(r2) and K.CONFIRMATION_PROPOSED in kinds(r2)
    assert repo.create_calls == 0
    r3 = say("yes")
    assert r3.completed_action.appointment.starts_at.date() == date(2026, 10, 30)


def test_doctor_change_rechecks_availability(say, session):
    propose_khan(say)
    r = say("Switch to Dr. Ahmed")                    # Ahmed 11:00 tomorrow is taken
    assert r.state == State.COLLECTING and r.pending_action is None
    assert len(r.offered_slots) == 3
    assert session.context.entities.doctor_name == "Dr. Ahmed"


def test_name_correction_invalidates_old_confirmation(say, session):
    r1 = propose_khan(say)
    old_action = session.context.pending_action.action_id
    r2 = say("No, the name is Sara")
    assert r2.state == State.AWAITING_CONFIRMATION and "Sara" in r2.pending_action.summary
    assert session.context.pending_action.action_id != old_action


# ------------------------------------------------------ scenarios 7, 9, 10, 11

def test_declined_confirmation_writes_nothing(say, repo):
    propose_khan(say)
    r = say("no")
    assert r.state == State.COLLECTING and r.pending_action is None
    assert K.CONFIRMATION_DECLINED in kinds(r)
    r = say("yes")
    assert any(e.kind == K.REFUSED for e in r.events)
    assert repo.create_calls == 0


def test_incomplete_request_asks_for_missing_fields(say):
    r = say("I need an appointment")
    assert r.state == State.COLLECTING and "Which doctor" in r.assistant_text
    r = say("Book for the 32nd")
    assert "couldn't read that date" in r.assistant_text
    assert "Which doctor" in r.assistant_text and r.error == "INVALID_DATE"


def test_sunday_is_rejected_with_reason(say, session):
    r = say("Book Dr. Ahmed on Sunday")
    assert "closed on Sundays" in r.assistant_text
    assert session.context.entities.date is None
    assert "Which day" in r.assistant_text


def test_out_of_hours_time_is_rejected(say, session):
    r = say("Book Dr. Khan tomorrow at 7")
    assert "We're open" in r.assistant_text and "What time" in r.assistant_text
    assert session.context.entities.time is None


def test_unknown_doctor(say):
    r = say("Book Dr. Smith tomorrow")
    assert "Dr. Smith" in r.assistant_text and "Dr. Ahmed" in r.assistant_text
    assert "Which doctor" in r.assistant_text


def test_empty_and_low_confidence_input_do_not_change_state(say, session, repo):
    r = say("")
    assert r.state == State.IDLE and K.EMPTY_INPUT in kinds(r)
    propose_khan(say)
    before = session.context.pending_action.action_id
    r = say("yes", conf=0.2)
    assert K.LOW_CONFIDENCE in kinds(r) and r.state == State.AWAITING_CONFIRMATION
    r = say("yes", conf=0.5)                          # heard, but too unsure to authorize
    assert any(e.kind == K.REFUSED and e.payload["reason"] == "low_confidence" for e in r.events)
    assert session.context.pending_action.action_id == before
    assert repo.create_calls == 0
    r = say("yes", conf=0.9)
    assert r.state == State.COMPLETED and repo.create_calls == 1


# ------------------------------------------------- scenarios 13, 14, 15

def test_repeating_a_completed_request_does_not_double_book(say, repo):
    propose_khan(say)
    say("yes")
    r = say("Book Dr. Khan tomorrow at 11 under Ali")
    assert "already booked" in r.assistant_text and "CL-482913" in r.assistant_text
    assert r.state == State.IDLE and repo.create_calls == 1


def test_bare_cancel_mid_booking_asks_which_cancel(say, repo):
    propose_khan(say)
    r = say("cancel")
    assert r.state == State.DISAMBIGUATING and r.pending_action is None
    r = say("yes")                                    # a bare yes must not authorize anything
    assert r.state == State.DISAMBIGUATING and repo.create_calls == 0
    r = say("stop booking")
    assert r.state == State.IDLE and r.flow is None


def test_cancel_that_resets_the_conversation(say, session):
    propose_khan(say)
    r = say("cancel that")
    assert r.state == State.IDLE and session.context.entities.doctor_slug is None


def test_unsupported_and_medical_requests(say):
    r = say("I have chest pain")
    assert "can't give medical advice" in r.assistant_text and r.state == State.IDLE
    r = say("Can I get a refund")
    assert "can't help with that" in r.assistant_text




def test_greeting(say):
    assert "ClinicCall" in say("hello").assistant_text


# ---------------------------------------------- confirmation gate (the safety net)

def test_expired_confirmation_is_refused_and_reproposed(say, clock, repo):
    propose_khan(say)
    clock.now += timedelta(minutes=11)
    r = say("yes")
    assert "expired" in r.assistant_text
    assert r.state == State.AWAITING_CONFIRMATION and repo.create_calls == 0
    r = say("yes")                                    # fresh confirmation, fresh TTL
    assert r.state == State.COMPLETED and repo.create_calls == 1


def test_tampered_context_fails_fingerprint_check(say, session, repo):
    propose_khan(say)
    session.context.entities.patient_name = "Tampered"   # simulate a stale/changed request
    r = say("yes")
    assert any(e.kind == K.REFUSED and e.payload["reason"] == "stale_fingerprint"
               for e in r.events)
    assert repo.create_calls == 0
    assert all(a.patient_name != "Tampered" for a in repo.all_appointments())


@pytest.mark.parametrize("utterance", [
    "no", "maybe", "hello", "what", "actually", "cancel", "never mind",
    "I have chest pain", "banana", "yes but make it noon", "Switch to Dr. Ahmed",
    "Actually make it Friday", "No, the name is Sara",
])
def test_nothing_but_a_pure_confirm_can_write(say, repo, utterance):
    propose_khan(say)
    r = say(utterance)
    assert repo.create_calls == 0
    assert r.completed_action is None
    assert not any(e.kind == K.DB_WRITE for e in r.events)


# -------------------------------------------------------------- decision trace

def test_every_turn_starts_with_received_and_parsed_and_numbers_events(say):
    for text in ["Book Dr. Khan tomorrow", "banana", ""]:
        r = say(text)
        assert kinds(r)[0] == K.TURN_RECEIVED
        assert [e.seq for e in r.events] == list(range(1, len(r.events) + 1))
        assert len(r.events) >= 2
    r = say("Book Dr. Khan tomorrow")
    parsed = next(e for e in r.events if e.kind == K.PARSED)
    assert parsed.payload["rule"] and parsed.payload["intent"]


def test_only_db_write_events_can_be_executed(say):
    for text in ["Book Dr. Khan tomorrow at 11 under Ali", "yes"]:
        r = say(text)
        for e in r.events:
            if e.executed:
                assert e.kind == K.DB_WRITE