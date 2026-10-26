"""ConversationService.handle_turn on the in-memory store and repository (offline)."""
import logging
import threading
from uuid import uuid4

import pytest
from sqlalchemy.exc import OperationalError

from app.brain.states import State
from app.db.session_store import MemorySessionStore, SessionNotFound, TurnInProgress
from app.schemas import TurnRequest
from app.services.conversation import ConversationService, StorageUnavailable

BOOK = "Book Dr. Khan tomorrow at 11 under Ali"


class SpyEngine:
    """Delegates to the real engine; counts calls and can fail after the engine ran
    (i.e. after any database write already happened)."""

    def __init__(self, inner):
        self.inner = inner
        self.calls = 0
        self.fail_after_run: Exception | None = None

    def handle_turn(self, session, text, stt_confidence=None):
        self.calls += 1
        response = self.inner.handle_turn(session, text, stt_confidence=stt_confidence)
        if self.fail_after_run is not None:
            err, self.fail_after_run = self.fail_after_run, None
            raise err
        return response


@pytest.fixture
def store():
    return MemorySessionStore(lock_timeout=0.3)


@pytest.fixture
def spy(engine):
    return SpyEngine(engine)


@pytest.fixture
def svc(store, spy):
    return ConversationService(store, spy)


@pytest.fixture
def cid(svc):
    return svc.create_conversation().conversation_id


def dump(response):
    """What a client receives: a replay comes back from JSON, so compare the JSON form."""
    return response.model_dump(mode="json")


def say(svc, cid, text, turn_id=None, **extra):
    return svc.handle_turn(cid, TurnRequest(turn_id=turn_id or uuid4(), text=text, **extra))


# --------------------------------------------------------------------- create
def test_create_conversation_returns_idle_greeting_and_persists(svc, store):
    created = svc.create_conversation()
    assert created.state == State.IDLE and created.assistant_text
    with store.turn(created.conversation_id) as tx:
        assert tx.session.turn_no == 0 and tx.last_turn_id is None


# ----------------------------------------------------------------- happy path
def test_full_booking_flow_goes_through_the_engine_and_confirmation(svc, cid, repo):
    first = say(svc, cid, BOOK)
    assert first.duplicate is False
    assert first.response.state == State.AWAITING_CONFIRMATION
    assert repo.create_calls == 0                       # nothing is written before "yes"
    second = say(svc, cid, "yes")
    assert second.response.state == State.COMPLETED
    assert second.response.completed_action is not None
    assert second.response.turn_no == 2 and repo.create_calls == 1


def test_stt_confidence_reaches_the_engine_and_blocks_a_low_confidence_yes(svc, cid, repo):
    say(svc, cid, BOOK)
    r = say(svc, cid, "yes", stt_confidence=0.05).response
    assert r.state == State.AWAITING_CONFIRMATION and repo.create_calls == 0


def test_state_response_and_events_are_saved_together(svc, cid, store):
    r1 = say(svc, cid, BOOK).response
    r2 = say(svc, cid, "yes").response
    assert len(store.events(cid)) == len(r1.events) + len(r2.events)
    assert [e["turn_no"] for e in store.events(cid)][-1] == 2


# ---------------------------------------------------------------- idempotency
def test_repeated_turn_id_replays_cached_response_without_running_the_engine(svc, cid, spy, store):
    turn_id = uuid4()
    first = say(svc, cid, BOOK, turn_id=turn_id)
    calls, events = spy.calls, len(store.events(cid))
    again = say(svc, cid, BOOK, turn_id=turn_id)
    assert again.duplicate is True
    assert dump(again.response) == dump(first.response)
    assert spy.calls == calls                           # engine not executed again
    assert len(store.events(cid)) == events             # no new events
    with store.turn(cid) as tx:
        assert tx.session.turn_no == 1                  # state did not advance


def test_repeated_confirmation_never_books_twice(svc, cid, repo):
    say(svc, cid, BOOK)
    yes_id = uuid4()
    first = say(svc, cid, "yes", turn_id=yes_id)
    for _ in range(3):
        again = say(svc, cid, "yes", turn_id=yes_id)
        assert again.duplicate and dump(again.response) == dump(first.response)
    assert repo.create_calls == 1
    assert len([a for a in repo.all_appointments() if a.patient_name == "Ali"]) == 1


def test_repeated_turn_id_replays_even_if_the_text_differs(svc, cid, spy):
    turn_id = uuid4()
    first = say(svc, cid, BOOK, turn_id=turn_id)
    calls = spy.calls
    again = say(svc, cid, "cancel everything", turn_id=turn_id)
    assert again.duplicate and dump(again.response) == dump(first.response)
    assert spy.calls == calls


def test_new_turn_id_after_a_replay_is_processed_normally(svc, cid, repo):
    turn_id = uuid4()
    say(svc, cid, BOOK, turn_id=turn_id)
    say(svc, cid, BOOK, turn_id=turn_id)                # replay
    r = say(svc, cid, "yes")
    assert r.duplicate is False and r.response.turn_no == 2 and repo.create_calls == 1


def test_concurrent_requests_with_the_same_turn_id_book_once(engine, repo):
    svc = ConversationService(MemorySessionStore(lock_timeout=5), SpyEngine(engine))
    cid = svc.create_conversation().conversation_id
    say(svc, cid, BOOK)
    yes_id, barrier, results = uuid4(), threading.Barrier(2), []

    def worker():
        barrier.wait()
        results.append(say(svc, cid, "yes", turn_id=yes_id))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert repo.create_calls == 1
    assert sorted(r.duplicate for r in results) == [False, True]
    assert dump(results[0].response) == dump(results[1].response)


# ------------------------------------------------------------ missing / busy
def test_unknown_conversation_raises_and_never_runs_the_engine(svc, spy):
    with pytest.raises(SessionNotFound):
        say(svc, uuid4(), "hello")
    assert spy.calls == 0


def test_busy_conversation_raises_turn_in_progress_and_never_runs_the_engine(svc, cid, spy, store):
    with store.turn(cid):                               # another request holds the lock
        with pytest.raises(TurnInProgress):
            say(svc, cid, "hello")
    assert spy.calls == 0
    assert say(svc, cid, "hello").response.turn_no == 1  # recovers once the lock is free


# -------------------------------------------------------------------- failures
def test_exception_after_the_engine_ran_persists_nothing_and_can_be_retried(svc, cid, spy, store):
    turn_id = uuid4()
    spy.fail_after_run = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        say(svc, cid, "hello", turn_id=turn_id)
    with store.turn(cid) as tx:
        assert tx.session.turn_no == 0 and tx.last_turn_id is None
    assert store.events(cid) == []
    retry = say(svc, cid, "hello", turn_id=turn_id)     # not mistaken for a duplicate
    assert retry.duplicate is False and retry.response.turn_no == 1


def test_crash_after_the_booking_write_is_recovered_without_a_second_booking(svc, cid, spy, repo):
    say(svc, cid, BOOK)
    yes_id = uuid4()
    spy.fail_after_run = RuntimeError("crash after commit of the booking")
    with pytest.raises(RuntimeError):
        say(svc, cid, "yes", turn_id=yes_id)
    assert len([a for a in repo.all_appointments() if a.patient_name == "Ali"]) == 1
    retry = say(svc, cid, "yes", turn_id=yes_id)        # session was rolled back: engine runs again
    assert retry.response.state == State.COMPLETED
    assert len([a for a in repo.all_appointments() if a.patient_name == "Ali"]) == 1
    booked = [a for a in repo.all_appointments() if a.patient_name == "Ali"][0]
    assert retry.response.completed_action.booking_reference == booked.booking_reference


def test_database_connection_error_becomes_storage_unavailable(svc, cid, spy, store):
    spy.fail_after_run = OperationalError("select 1", {}, Exception("connection refused"))
    with pytest.raises(StorageUnavailable):
        say(svc, cid, "hello")
    with store.turn(cid) as tx:
        assert tx.session.turn_no == 0


def test_create_conversation_storage_error_becomes_storage_unavailable(engine):
    class DownStore(MemorySessionStore):
        def create(self, session):
            raise OperationalError("insert", {}, Exception("connection refused"))

    with pytest.raises(StorageUnavailable):
        ConversationService(DownStore(), engine).create_conversation()


# ------------------------------------------------------------------------ logs
def test_logs_contain_no_patient_names_text_or_booking_references(svc, cid):
    log = logging.getLogger("cliniccall.conversation")
    seen, old_level = [], log.level

    class Collect(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())

    handler = Collect()
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)
    try:
        turn_id = uuid4()
        say(svc, cid, BOOK, turn_id=turn_id)
        say(svc, cid, "yes")
        say(svc, cid, BOOK, turn_id=turn_id)
    finally:
        log.removeHandler(handler)
        log.setLevel(old_level)
    joined = " ".join(seen)
    assert seen and "Ali" not in joined and "CL-" not in joined and "Khan" not in joined






# ------------------------------------------------------------------- read view
def test_get_conversation_unknown_raises(svc):
    with pytest.raises(SessionNotFound):
        svc.get_conversation(uuid4())


def test_get_conversation_fresh_and_after_turns(svc, cid, spy, repo):
    fresh = svc.get_conversation(cid)
    assert fresh.state == State.IDLE and fresh.turn_no == 0 and fresh.last_response is None
    say(svc, cid, BOOK)
    view = svc.get_conversation(cid)
    assert view.state == State.AWAITING_CONFIRMATION and view.turn_no == 1
    assert view.pending_action is not None and view.pending_action.status == "proposed"
    assert view.entities.doctor_slug == "khan" and view.last_response.turn_no == 1
    assert repo.create_calls == 0 and spy.calls == 1      # reading runs nothing


def test_get_conversation_is_not_blocked_by_a_turn_in_progress(svc, cid, store):
    with store.turn(cid):
        assert svc.get_conversation(cid).turn_no == 0


def test_get_conversation_storage_error_becomes_storage_unavailable(engine):
    class DownStore(MemorySessionStore):
        def read(self, session_id):
            raise OperationalError("select", {}, Exception("connection refused"))

    with pytest.raises(StorageUnavailable):
        ConversationService(DownStore(), engine).get_conversation(uuid4())