from datetime import timedelta

import pytest

from app.brain.states import EventKind as K
from app.brain.states import Flow, State

CANCEL = "Cancel CL-100003 under Omar"


def kinds(r):
    return [e.kind for e in r.events]


def omar(repo):
    return repo.get_appointment_by_reference("CL-100003")      # Malik, Thu 10:00


def propose_cancel(say):
    r = say(CANCEL)
    assert r.state == State.AWAITING_CONFIRMATION
    return r


def untouched(repo):
    assert (repo.create_calls, repo.reschedule_calls, repo.cancel_calls) == (0, 0, 0)
    assert omar(repo).status == "confirmed" and omar(repo).version == 1


# ------------------------------------------------------------------ happy path
def test_multi_turn_cancel(say, repo):
    slot_id = omar(repo).slot_id
    r = say("Cancel my appointment")
    assert r.state == State.COLLECTING and r.flow == Flow.CANCEL
    assert "reference" in r.assistant_text.lower()
    r = say("CL-100003")
    assert "name" in r.assistant_text.lower()
    r = say("Omar")
    assert r.state == State.AWAITING_CONFIRMATION and "CL-100003" in r.pending_action.summary
    untouched(repo)                                       # nothing before confirmation
    r = say("yes")
    assert r.state == State.COMPLETED and r.completed_action.type == Flow.CANCEL
    assert omar(repo).status == "cancelled" and omar(repo).version == 2
    assert repo.get_slot_by_id(slot_id).free is True
    writes = [e for e in r.events if e.kind == K.DB_WRITE]
    assert len(writes) == 1 and writes[0].executed is True


def test_one_sentence_cancel_only_proposes(say, repo):
    r = propose_cancel(say)
    assert r.completed_action is None
    untouched(repo)


def test_wrong_name_is_generic(say, repo):
    r = say("Cancel CL-100003 under Sara")
    assert r.state == State.COLLECTING and r.pending_action is None
    assert r.assistant_text == say("Cancel CL-999999 under Omar").assistant_text
    untouched(repo)


def test_already_cancelled_is_not_cancelled_again(say, repo):
    propose_cancel(say)
    say("yes")
    r = say(CANCEL)
    assert r.state == State.IDLE and "already cancelled" in r.assistant_text.lower()
    assert any(e.kind == K.REFUSED and e.payload["reason"] == "already_cancelled"
               for e in r.events)
    assert repo.cancel_calls == 1


def test_declined_cancellation_changes_nothing(say, repo):
    propose_cancel(say)
    r = say("no")
    assert r.state == State.IDLE and r.pending_action is None
    untouched(repo)


def test_name_correction_invalidates_proposal(say, repo):
    propose_cancel(say)
    r = say("No, the name is Sara")
    assert r.pending_action is None and K.STALE_PENDING_CLEARED in kinds(r)
    untouched(repo)


def test_stale_version_cannot_cancel_a_changed_appointment(say, repo):
    propose_cancel(say)
    a = omar(repo)
    other = next(s for s in repo.list_slots("malik", a.starts_at.date()) if s.free)
    repo.reschedule(appointment_id=a.id, expected_version=1, new_slot_id=other.id)
    r = say("yes")
    fails = [e for e in r.events if e.kind == K.DB_WRITE]
    assert len(fails) == 1 and fails[0].executed is False
    assert fails[0].payload["outcome"] == "STALE_VERSION"
    assert omar(repo).status == "confirmed"
    assert r.state == State.AWAITING_CONFIRMATION                   # must confirm again
    r = say("yes")
    assert r.state == State.COMPLETED
    assert omar(repo).status == "cancelled" and omar(repo).version == 3


def test_cancelled_by_someone_else_before_confirmation(say, repo):
    propose_cancel(say)
    repo.cancel(appointment_id=omar(repo).id, expected_version=1)
    r = say("yes")
    assert r.state == State.IDLE and "already cancelled" in r.assistant_text.lower()


# ---------------------------------------------------- ambiguous "cancel" rules
def test_bare_cancel_in_idle_starts_the_cancellation_flow(say):
    r = say("cancel")
    assert r.flow == Flow.CANCEL and r.state == State.COLLECTING
    assert "reference" in r.assistant_text.lower()


def test_bare_cancel_mid_reschedule_asks_then_switches(say, repo, session):
    r = say("Reschedule CL-100002 under Maria")
    assert r.state == State.COLLECTING
    r = say("cancel")
    assert r.state == State.DISAMBIGUATING
    untouched_cancel = repo.cancel_calls == 0
    r = say("the existing appointment")
    assert untouched_cancel and r.state == State.AWAITING_CONFIRMATION
    assert session.context.flow == Flow.CANCEL
    assert session.context.pending_action.type == Flow.CANCEL
    assert K.INTENT_SWITCH in kinds(r)
    assert repo.cancel_calls == 0


def test_bare_cancel_then_stop_cancels_only_the_conversation(say, repo):
    say("Reschedule CL-100002 under Maria")
    say("cancel")
    r = say("stop")
    assert r.state == State.IDLE
    assert (repo.reschedule_calls, repo.cancel_calls) == (0, 0)


def test_never_mind_cancels_conversation_not_appointment(say, repo):
    propose_cancel(say)
    r = say("never mind")
    assert r.state == State.IDLE
    untouched(repo)


def test_booking_switches_to_cancellation(say, repo, session):
    say("Book Dr. Khan tomorrow at 11 under Ali")
    r = say("Actually cancel my appointment")
    assert r.flow == Flow.CANCEL and r.pending_action is None
    assert K.INTENT_SWITCH in kinds(r) and K.STALE_PENDING_CLEARED in kinds(r)
    r = say("yes")                                        # old booking must be gone
    assert repo.create_calls == 0 and repo.cancel_calls == 0
    assert any(e.kind == K.REFUSED for e in r.events)


# ------------------------------------------------------- security / idempotency
def test_yes_without_pending_action_mutates_nothing(say, repo):
    r = say("yes")
    assert any(e.kind == K.REFUSED and e.payload["reason"] == "no_pending_action"
               for e in r.events)
    assert (repo.create_calls, repo.reschedule_calls, repo.cancel_calls) == (0, 0, 0)


def test_second_yes_after_completion_does_not_cancel_twice(say, repo):
    propose_cancel(say)
    say("yes")
    say("yes")
    assert repo.cancel_calls == 1


def test_low_confidence_yes_cannot_authorize(say, repo):
    propose_cancel(say)
    r = say("yes", conf=0.5)
    assert any(e.kind == K.REFUSED and e.payload["reason"] == "low_confidence"
               for e in r.events)
    untouched(repo)
    assert say("yes", conf=0.9).state == State.COMPLETED


def test_expired_confirmation_is_refused_and_reproposed(say, repo, clock, session):
    propose_cancel(say)
    first = session.context.pending_action.action_id
    clock.now = clock.now + timedelta(minutes=11)
    r = say("yes")
    refused = [e for e in r.events if e.kind == K.REFUSED]
    assert refused and refused[0].payload["reason"] == "expired"
    assert repo.cancel_calls == 0
    assert r.state == State.AWAITING_CONFIRMATION
    assert session.context.pending_action.action_id != first


def test_tampered_context_fails_the_fingerprint_check(say, repo, session):
    propose_cancel(say)
    snap = session.context.appointment_snapshot
    session.context.appointment_snapshot = snap.model_copy(update={"version": 99})
    r = say("yes")
    refused = [e for e in r.events if e.kind == K.REFUSED]
    assert refused and refused[0].payload["reason"] == "stale_fingerprint"
    assert repo.cancel_calls == 0


def test_confirmation_mixed_with_new_details_is_not_authorization(say, repo):
    propose_cancel(say)
    say("yes but make it Friday")
    untouched(repo)


def test_trace_separates_proposal_refusal_and_write(say, repo):
    r = propose_cancel(say)
    proposed = [e for e in r.events if e.kind == K.CONFIRMATION_PROPOSED]
    assert proposed and proposed[0].executed is False
    assert not any(e.kind == K.DB_WRITE for e in r.events)
    r = say("yes")
    assert [e.executed for e in r.events if e.kind == K.DB_WRITE] == [True]