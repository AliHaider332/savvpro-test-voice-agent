"""Skipped unless DATABASE_URL_TEST is set. Uses only fresh session ids."""
import threading
import time
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.brain.engine import Session
from app.brain.states import State
from app.db.seed import load_env_value
from app.db.session_store import SessionNotFound, SqlSessionStore, TurnInProgress
from app.db.sql_repo import make_engine

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def sa_engine():
    url = load_env_value("DATABASE_URL_TEST")
    if not url:
        pytest.skip("DATABASE_URL_TEST is not set")
    eng = make_engine(url)
    yield eng
    eng.dispose()


@pytest.fixture
def store(sa_engine):
    return SqlSessionStore(sa_engine)


def run_turn(engine, store, session_id, text_):
    with store.turn(session_id) as tx:
        resp = engine.handle_turn(tx.session, text_)
        tx.save(tx.session, uuid4(), resp)
    return resp


def test_unknown_session(store):
    with pytest.raises(SessionNotFound):
        with store.turn(uuid4()):
            pass


def test_round_trip_through_postgres_and_book(engine, store, repo):
    s = Session()
    store.create(s)
    assert run_turn(engine, store, s.id, "Book Dr. Khan tomorrow at 11 under Ali").state \
        == State.AWAITING_CONFIRMATION
    assert repo.create_calls == 0
    assert run_turn(engine, store, s.id, "yes").state == State.COMPLETED
    assert repo.create_calls == 1


def test_turn_id_response_and_events_are_stored(engine, store, sa_engine):
    s = Session()
    store.create(s)
    turn_id = uuid4()
    with store.turn(s.id) as tx:
        resp = engine.handle_turn(tx.session, "hello")
        tx.save(tx.session, turn_id, resp)
    with store.turn(s.id) as tx:
        assert tx.last_turn_id == turn_id
        assert tx.last_response["assistant_text"] == resp.assistant_text
    with sa_engine.connect() as conn:
        n = conn.execute(text("select count(*) from conversation_events where session_id = :s"),
                         {"s": s.id}).scalar_one()
    assert n == len(resp.events)


def test_exception_rolls_back(engine, store):
    s = Session()
    store.create(s)
    with pytest.raises(RuntimeError):
        with store.turn(s.id) as tx:
            tx.save(tx.session, uuid4(), engine.handle_turn(tx.session, "hello"))
            raise RuntimeError("boom")
    with store.turn(s.id) as tx:
        assert tx.last_turn_id is None and tx.session.turn_no == 0


def test_for_update_serializes_two_requests(engine, store):
    s = Session()
    store.create(s)
    started = threading.Event()
    seen = {}

    def slow():
        with store.turn(s.id) as tx:
            started.set()
            time.sleep(0.6)
            tx.save(tx.session, uuid4(), engine.handle_turn(tx.session, "hello"))

    def second():
        started.wait()
        time.sleep(0.1)
        with store.turn(s.id) as tx:                 # waits on FOR UPDATE
            seen["turn_no"] = tx.session.turn_no

    threads = [threading.Thread(target=slow), threading.Thread(target=second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert seen["turn_no"] == 1