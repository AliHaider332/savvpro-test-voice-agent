"""ConversationService against real PostgreSQL. Skipped unless DATABASE_URL_TEST is set.

WARNING: like test_db_integration.py, the `world` fixture re-seeds (truncates) every
ClinicCall table so each test starts from known data. Use a fake-data database. To be
safe it refuses to run when DATABASE_URL_TEST points at the same database as DATABASE_URL.
"""
import threading
import time
from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from app.brain.engine import Engine
from app.brain.states import State
from app.db.seed import load_env_value, seed
from app.db.session_store import SqlSessionStore, TurnInProgress
from app.db.sql_repo import SqlRepository, make_engine
from app.schemas import TurnRequest
from app.services.appointments import AppointmentService
from app.services.conversation import ConversationService

pytestmark = pytest.mark.integration
TZ = ZoneInfo("Asia/Karachi")
PATIENT = "Ali"


def _clinic_day() -> str:
    """ISO date 2+ days ahead that is not a Sunday (the clinic is closed on Sundays)."""
    day = datetime.now(TZ).date() + timedelta(days=2)
    while day.weekday() == 6:
        day += timedelta(days=1)
    return day.isoformat()


def book_text() -> str:
    return f"Book Dr. Khan on {_clinic_day()} at 11 am under {PATIENT}"


class FailAfterRun:
    """Real engine, but raises after it ran (after the booking write was committed)."""

    def __init__(self, inner):
        self.inner, self.fail_next = inner, False

    def handle_turn(self, session, text_, stt_confidence=None):
        response = self.inner.handle_turn(session, text_, stt_confidence=stt_confidence)
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("simulated crash after the database write")
        return response


class World:
    def __init__(self, sa_engine):
        self.sa = sa_engine
        self.store = SqlSessionStore(sa_engine)
        self.repo = SqlRepository(sa_engine, TZ)
        self.flaky = FailAfterRun(Engine(AppointmentService(self.repo, TZ),
                                         lambda: datetime.now(TZ)))
        self.svc = ConversationService(self.store, self.flaky)

    def new_conversation(self):
        return self.svc.create_conversation().conversation_id

    def say(self, cid, text_, turn_id=None):
        return self.svc.handle_turn(cid, TurnRequest(turn_id=turn_id or uuid4(), text=text_))

    def scalar(self, sql, **params):
        with self.sa.connect() as conn:
            return conn.execute(text(sql), params).scalar_one()

    def patient_rows(self):
        return self.scalar("select count(*) from appointments where patient_name = :n",
                           n=PATIENT)

    def event_rows(self, cid):
        return self.scalar("select count(*) from conversation_events where session_id = :s", s=cid)


@pytest.fixture(scope="module")
def sa_engine():
    url = load_env_value("DATABASE_URL_TEST")
    if not url:
        pytest.skip("DATABASE_URL_TEST is not set")
    if url == load_env_value("DATABASE_URL"):
        pytest.fail("DATABASE_URL_TEST must not be the production DATABASE_URL: "
                    "these tests truncate every table")
    eng = make_engine(url)
    yield eng
    eng.dispose()


@pytest.fixture
def world(sa_engine):
    seed(sa_engine, datetime.now(TZ), TZ)
    return World(sa_engine)


def test_booking_is_persisted_and_a_repeated_confirmation_is_replayed(world):
    cid = world.new_conversation()
    first = world.say(cid, book_text())
    assert first.response.state == State.AWAITING_CONFIRMATION
    assert world.patient_rows() == 0                     # nothing written before "yes"

    yes_id = uuid4()
    done = world.say(cid, "yes", turn_id=yes_id)
    assert done.response.state == State.COMPLETED and world.patient_rows() == 1
    ref = done.response.completed_action.booking_reference
    events_after = world.event_rows(cid)
    assert events_after == len(first.response.events) + len(done.response.events)

    again = world.say(cid, "yes", turn_id=yes_id)        # client retry of the same turn
    assert again.duplicate is True
    assert again.response.model_dump(mode="json") == done.response.model_dump(mode="json")
    assert world.patient_rows() == 1                     # still one appointment
    assert world.event_rows(cid) == events_after         # trace untouched
    assert world.scalar("select last_turn_id from conversation_sessions where id = :s",
                        s=cid) == yes_id
    assert world.scalar("select version from conversation_sessions where id = :s", s=cid) == 2
    assert world.scalar("select booking_reference from appointments where patient_name = :n",
                        n=PATIENT) == ref


def test_two_simultaneous_requests_with_one_turn_id_book_once(world):
    cid = world.new_conversation()
    world.say(cid, book_text())
    yes_id, barrier, results, errors = uuid4(), threading.Barrier(2), [], []

    def worker():
        barrier.wait()
        try:
            results.append(world.say(cid, "yes", turn_id=yes_id))
        except Exception as exc:                          # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert sorted(r.duplicate for r in results) == [False, True]
    assert world.patient_rows() == 1


def test_crash_after_the_write_rolls_the_session_back_and_a_retry_does_not_rebook(world):
    cid = world.new_conversation()
    world.say(cid, book_text())
    events_before = world.event_rows(cid)
    yes_id = uuid4()

    world.flaky.fail_next = True
    with pytest.raises(RuntimeError):
        world.say(cid, "yes", turn_id=yes_id)
    assert world.patient_rows() == 1                      # the booking itself had committed
    assert world.scalar("select state from conversation_sessions where id = :s",
                        s=cid) == State.AWAITING_CONFIRMATION.value
    assert world.scalar("select turn_no from conversation_sessions where id = :s", s=cid) == 1
    assert world.event_rows(cid) == events_before         # the session transaction rolled back

    retry = world.say(cid, "yes", turn_id=yes_id)
    assert retry.duplicate is False and retry.response.state == State.COMPLETED
    assert world.patient_rows() == 1                      # idempotency key: no second booking


def test_second_caller_on_a_locked_conversation_gets_turn_in_progress(world):
    cid = world.new_conversation()
    holding, release, errors = threading.Event(), threading.Event(), []

    def holder():
        with world.store.turn(cid):
            holding.set()
            release.wait(timeout=15)

    t = threading.Thread(target=holder)
    t.start()
    holding.wait(timeout=5)
    started = time.monotonic()
    try:
        with pytest.raises(TurnInProgress):
            world.say(cid, "hello")                       # waits out the 5 s lock timeout
    finally:
        release.set()
        t.join()
    assert time.monotonic() - started < 12
    assert world.say(cid, "hello").response.turn_no == 1  # works again once released






def test_read_reflects_persisted_state_and_does_not_wait_for_a_turn_lock(world):
    cid = world.new_conversation()
    first = world.say(cid, book_text())
    view = world.svc.get_conversation(cid)
    assert view.state == State.AWAITING_CONFIRMATION and view.turn_no == 1
    assert view.last_response.model_dump(mode="json") == first.response.model_dump(mode="json")

    holding, release = threading.Event(), threading.Event()

    def holder():
        with world.store.turn(cid):
            holding.set()
            release.wait(timeout=10)

    t = threading.Thread(target=holder)
    t.start()
    holding.wait(timeout=5)
    try:
        started = time.monotonic()
        assert world.svc.get_conversation(cid).turn_no == 1   # plain SELECT, not FOR UPDATE
        assert time.monotonic() - started < 2
    finally:
        release.set()
        t.join()