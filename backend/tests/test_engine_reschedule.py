from datetime import date, time

import pytest

from app.brain.states import EventKind as K
from app.brain.states import Flow, State

THU, FRI = date(2026, 10, 29), date(2026, 10, 30)
MOVE = "Reschedule CL-100002 under Maria to Thursday at 3 pm"


def kinds(r):
    return [e.kind for e in r.events]


def maria(repo):
    return repo.get_appointment_by_reference("CL-100002")      # Khan, Wed 14:00


def propose_move(say):
    r = say(MOVE)
    assert r.state == State.AWAITING_CONFIRMATION
    return r


def untouched(repo):
    assert (repo.create_calls, repo.reschedule_calls, repo.cancel_calls) == (0, 0, 0)
    assert maria(repo).version == 1 and maria(repo).status == "confirmed"


def test_multi_turn_happy_path(say, repo):
    original_slot = maria(repo).slot_id
    r = say("Move my appointment")
    assert r.state == State.COLLECTING and r.flow == Flow.RESCHEDULE
    assert "reference" in r.assistant_text.lower()
    r = say("CL-100002")
    assert "name" in r.assistant_text.lower()
    r = say("Maria")
    assert r.state == State.COLLECTING and "day" in r.assistant_text.lower()
    assert r.missing_fields == ["new_date", "new_time"]
    r = say("Thursday at 3 pm")
    assert r.state == State.AWAITING_CONFIRMATION
    assert "CL-100002" in r.pending_action.summary
    untouched(repo)                                       # proposal wrote nothing
    r = say("yes")
    assert r.state == State.COMPLETED and r.completed_action.type == Flow.RESCHEDULE
    appt = maria(repo)
    assert appt.version == 2 and appt.starts_at.date() == THU and appt.starts_at.hour == 15
    assert repo.get_slot_by_id(original_slot).free is True
    writes = [e for e in r.events if e.kind == K.DB_WRITE]
    assert len(writes) == 1 and writes[0].executed is True
    assert repo.reschedule_calls == 1


def test_everything_in_one_sentence_proposes_without_writing(say, repo):
    r = propose_move(say)
    assert r.completed_action is None
    untouched(repo)


def test_wrong_name_and_unknown_reference_look_identical(say, repo):
    r1 = say("Reschedule CL-100002 under Sara")
    r2 = say("Reschedule CL-999999 under Maria")
    assert r1.assistant_text == r2.assistant_text
    assert r1.state == r2.state == State.COLLECTING
    assert r1.entities.booking_reference is None and r1.entities.verify_name is None
    assert "Khan" not in r1.assistant_text and "Maria" not in r1.assistant_text
    untouched(repo)


def test_three_failed_lookups_add_a_hint(say):
    r = say("Reschedule CL-999999 under Maria")
    assert "start over" not in r.assistant_text.lower()
    r = say("CL-999998 under Maria")
    assert "start over" not in r.assistant_text.lower()
    r = say("CL-999997 under Maria")
    assert "start over" in r.assistant_text.lower()

def test_occupied_destination_is_never_proposed(say, repo, session):
    taken = repo.get_slot("khan", THU, time(15, 0))
    repo.create_appointment(slot_id=taken.id, patient_name="Zed", idempotency_key="z1")
    r = say(MOVE)
    assert r.state == State.COLLECTING and r.pending_action is None
    assert r.offered_slots and repo.reschedule_calls == 0
    first = r.offered_slots[0]
    r = say("the first one")
    assert r.state == State.AWAITING_CONFIRMATION
    assert session.context.selected_slot_id == first.slot_id


def test_moving_to_own_current_time_is_rejected(say, repo):
    r = say("Reschedule CL-100002 under Maria to Wednesday at 2 pm")
    assert r.state == State.COLLECTING and r.pending_action is None
    assert "already" in r.assistant_text.lower()
    untouched(repo)


def test_cancelled_appointment_cannot_be_rescheduled(say, repo):
    a = maria(repo)
    repo.cancel(appointment_id=a.id, expected_version=1)
    r = say(MOVE)
    assert r.state == State.IDLE and r.pending_action is None
    assert "cancelled" in r.assistant_text.lower()
    assert repo.reschedule_calls == 0


def test_declined_proposal_changes_nothing(say, repo):
    propose_move(say)
    r = say("no")
    assert r.state == State.COLLECTING and r.pending_action is None
    assert any(e.kind == K.CONFIRMATION_DECLINED for e in r.events)
    untouched(repo)
    r = say("yes")                                        # nothing left to confirm
    assert any(e.kind == K.REFUSED and e.payload["reason"] == "no_pending_action"
               for e in r.events)
    untouched(repo)


def test_correction_invalidates_old_proposal(say, repo, session):
    propose_move(say)
    first = session.context.pending_action.action_id
    r = say("Actually make it Friday")
    assert r.state == State.AWAITING_CONFIRMATION
    assert K.STALE_PENDING_CLEARED in kinds(r)
    assert session.context.pending_action.action_id != first
    untouched(repo)
    say("yes")
    assert maria(repo).starts_at.date() == FRI and maria(repo).starts_at.hour == 15


def test_stale_version_at_confirmation_reproposes_and_needs_new_yes(say, repo):
    propose_move(say)
    a = maria(repo)
    other = next(s for s in repo.list_slots("khan", date(2026, 10, 28)) if s.free)
    repo.reschedule(appointment_id=a.id, expected_version=1, new_slot_id=other.id)  # concurrent change
    r = say("yes")
    fails = [e for e in r.events if e.kind == K.DB_WRITE]
    assert len(fails) == 1 and fails[0].executed is False
    assert fails[0].payload["outcome"] == "STALE_VERSION"
    assert maria(repo).slot_id == other.id and maria(repo).version == 2   # not overwritten
    assert r.state == State.AWAITING_CONFIRMATION                          # fresh proposal
    r = say("yes")
    assert r.state == State.COMPLETED
    assert maria(repo).version == 3 and maria(repo).starts_at.date() == THU


def test_slot_taken_between_proposal_and_confirmation(say, repo):
    propose_move(say)
    taken = repo.get_slot("khan", THU, time(15, 0))
    repo.create_appointment(slot_id=taken.id, patient_name="Zed", idempotency_key="z2")
    r = say("yes")
    assert r.state == State.COLLECTING and r.offered_slots
    assert maria(repo).version == 1 and maria(repo).starts_at.date() != THU


def test_booking_can_switch_to_reschedule(say, repo):
    r = say("Book Dr. Khan tomorrow at 11 under Ali")
    assert r.state == State.AWAITING_CONFIRMATION
    r = say("Actually reschedule my appointment")
    assert r.flow == Flow.RESCHEDULE and r.pending_action is None
    assert K.INTENT_SWITCH in kinds(r) and K.STALE_PENDING_CLEARED in kinds(r)
    assert repo.create_calls == 0


@pytest.mark.parametrize("text", [
    "no", "maybe", "hello", "what", "banana", "yes but make it noon",
    "Switch to Dr. Ahmed", "Actually make it Friday", "No, the name is Sara",
    "cancel", "never mind",
])
def test_nothing_but_a_pure_confirm_can_write(say, repo, text):
    propose_move(say)
    say(text)
    untouched(repo)