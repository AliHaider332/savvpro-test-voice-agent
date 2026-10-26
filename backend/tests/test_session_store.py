import threading
import time
from uuid import uuid4

import pytest

from app.brain.engine import Session
from app.brain.states import State
from app.db.session_store import MemorySessionStore, SessionNotFound, TurnInProgress


@pytest.fixture
def store():
    return MemorySessionStore(lock_timeout=0.3)


def run_turn(engine, store, session_id, text):
    with store.turn(session_id) as tx:
        resp = engine.handle_turn(tx.session, text)
        tx.save(tx.session, uuid4(), resp)
    return resp


def test_unknown_session(store):
    with pytest.raises(SessionNotFound):
        with store.turn(uuid4()):
            pass


def test_conversation_survives_json_round_trip_and_still_books(engine, store, repo):
    s = Session()
    store.create(s)
    r = run_turn(engine, store, s.id, "Book Dr. Khan tomorrow at 11 under Ali")
    assert r.state == State.AWAITING_CONFIRMATION and repo.create_calls == 0
    r = run_turn(engine, store, s.id, "yes")          # fingerprint must survive serialization
    assert r.state == State.COMPLETED and repo.create_calls == 1


def test_reschedule_and_cancel_survive_round_trip(engine, store, repo):
    s = Session()
    store.create(s)
    run_turn(engine, store, s.id, "Reschedule CL-100002 under Maria to Thursday at 3 pm")
    assert run_turn(engine, store, s.id, "yes").state == State.COMPLETED
    s2 = Session()
    store.create(s2)
    run_turn(engine, store, s2.id, "Cancel CL-100003 under Omar")
    assert run_turn(engine, store, s2.id, "yes").state == State.COMPLETED
    assert (repo.reschedule_calls, repo.cancel_calls) == (1, 1)


def test_saved_turn_id_response_and_events(engine, store):
    s = Session()
    store.create(s)
    with store.turn(s.id) as tx:
        assert tx.last_turn_id is None
        turn_id = uuid4()
        resp = engine.handle_turn(tx.session, "hello")
        tx.save(tx.session, turn_id, resp)
    with store.turn(s.id) as tx:
        assert tx.last_turn_id == turn_id
        assert tx.last_response["assistant_text"] == resp.assistant_text
    assert len(store.events(s.id)) == len(resp.events)


def test_exception_inside_turn_persists_nothing(engine, store):
    s = Session()
    store.create(s)
    with pytest.raises(RuntimeError):
        with store.turn(s.id) as tx:
            tx.save(tx.session, uuid4(), engine.handle_turn(tx.session, "hello"))
            raise RuntimeError("boom")
    with store.turn(s.id) as tx:
        assert tx.last_turn_id is None and tx.session.turn_no == 0
    assert store.events(s.id) == []


def test_second_request_on_a_busy_session_gets_turn_in_progress(store):
    s = Session()
    store.create(s)
    with store.turn(s.id):
        with pytest.raises(TurnInProgress):
            with store.turn(s.id):
                pass


def test_turns_on_one_session_are_serial(engine):
    store = MemorySessionStore(lock_timeout=5)
    s = Session()
    store.create(s)
    started = threading.Event()
    seen = {}

    def slow():
        with store.turn(s.id) as tx:
            started.set()
            time.sleep(0.4)
            tx.save(tx.session, uuid4(), engine.handle_turn(tx.session, "hello"))

    def second():
        started.wait()
        with store.turn(s.id) as tx:                 # blocks until slow() commits
            seen["turn_no"] = tx.session.turn_no

    threads = [threading.Thread(target=slow), threading.Thread(target=second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert seen["turn_no"] == 1