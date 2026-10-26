from uuid import uuid4

import pytest

from app.brain.engine import Session
from app.brain.states import State
from app.db.session_store import MemorySessionStore, SessionNotFound


@pytest.fixture
def store():
    return MemorySessionStore(lock_timeout=0.3)


def test_read_unknown_session(store):
    with pytest.raises(SessionNotFound):
        store.read(uuid4())


def test_read_returns_saved_state_and_last_turn(engine, store):
    s = Session()
    store.create(s)
    assert store.read(s.id).last_turn_id is None
    turn_id = uuid4()
    with store.turn(s.id) as tx:
        resp = engine.handle_turn(tx.session, "Book Dr. Khan tomorrow at 11 under Ali")
        tx.save(tx.session, turn_id, resp)
    snap = store.read(s.id)
    assert snap.session.state == State.AWAITING_CONFIRMATION and snap.session.turn_no == 1
    assert snap.last_turn_id == turn_id
    assert snap.last_response["assistant_text"] == resp.assistant_text


def test_read_does_not_wait_for_the_turn_lock_and_never_sees_unsaved_work(engine, store):
    s = Session()
    store.create(s)
    with store.turn(s.id) as tx:                      # a turn is in flight
        engine.handle_turn(tx.session, "hello")       # mutated in memory, not saved
        snap = store.read(s.id)                        # must not raise TurnInProgress
        assert snap.session.turn_no == 0 and snap.last_turn_id is None