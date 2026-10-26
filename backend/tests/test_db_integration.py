"""Real-Postgres tests. Skipped unless DATABASE_URL_TEST is set.
WARNING: the fixture truncates every ClinicCall table. Use a fake-data database."""
import threading
from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db.repository import SlotNotFound, SlotTaken, StaleAppointment
from app.db.seed import load_env_value, seed
from app.db.sql_repo import SqlRepository, make_engine

pytestmark = pytest.mark.integration
TZ = ZoneInfo("Asia/Karachi")


@pytest.fixture(scope="module")
def engine():
    url = load_env_value("DATABASE_URL_TEST")
    if not url:
        pytest.skip("DATABASE_URL_TEST is not set")
    eng = make_engine(url)
    yield eng
    eng.dispose()


@pytest.fixture
def repo(engine):
    seed(engine, datetime.now(TZ), TZ)          # fresh, known data for every test
    return SqlRepository(engine, TZ)


def free_slots(repo, slug, n):
    out = []
    today = datetime.now(TZ).date()
    for i in range(1, 15):
        out += [s for s in repo.list_slots(slug, today + timedelta(days=i)) if s.free]
        if len(out) >= n:
            break
    assert len(out) >= n, "not enough free slots in the seeded data"
    return out[:n]


def race(*fns):
    """Run callables at the same instant; return [('ok', value) | ('err', exc)]."""
    barrier = threading.Barrier(len(fns))
    results = [None] * len(fns)

    def run(i, fn):
        barrier.wait()
        try:
            results[i] = ("ok", fn())
        except Exception as exc:                 # noqa: BLE001 - we inspect it
            results[i] = ("err", exc)

    threads = [threading.Thread(target=run, args=(i, f)) for i, f in enumerate(fns)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def confirmed_count(engine, slot_id):
    with engine.connect() as conn:
        return conn.execute(text(
            "select count(*) from appointments where slot_id = :s and status = 'confirmed'"),
            {"s": slot_id}).scalar_one()


# ------------------------------------------------------------------ seed / reads
def test_seed_creates_doctors_and_demo_appointments(repo):
    assert {d.slug for d in repo.list_doctors()} == {"ahmed", "khan", "malik"}
    maria = repo.get_appointment_by_reference("CL-100002")
    assert maria.patient_name == "Maria Test" and maria.version == 1
    assert maria.status == "confirmed" and maria.doctor_slug == "khan"
    assert repo.get_slot_by_id(maria.slot_id).free is False


def test_unknown_reference_returns_none(repo):
    assert repo.get_appointment_by_reference("CL-999999") is None


# ---------------------------------------------------------------------- create
def test_create_appointment_marks_slot_taken(repo):
    slot = free_slots(repo, "khan", 1)[0]
    appt = repo.create_appointment(slot_id=slot.id, patient_name="Ali", idempotency_key="k-1")
    assert appt.booking_reference.startswith("CL-") and len(appt.booking_reference) == 9
    assert appt.version == 1 and appt.status == "confirmed"
    assert repo.get_slot_by_id(slot.id).free is False


def test_second_booking_of_same_slot_is_rejected_by_the_database(repo, engine):
    slot = free_slots(repo, "khan", 1)[0]
    repo.create_appointment(slot_id=slot.id, patient_name="Ali", idempotency_key="k-a")
    with pytest.raises(SlotTaken):
        repo.create_appointment(slot_id=slot.id, patient_name="Sara", idempotency_key="k-b")
    assert confirmed_count(engine, slot.id) == 1


def test_same_idempotency_key_returns_original(repo, engine):
    slot = free_slots(repo, "khan", 1)[0]
    first = repo.create_appointment(slot_id=slot.id, patient_name="Ali", idempotency_key="same")
    again = repo.create_appointment(slot_id=slot.id, patient_name="Ali", idempotency_key="same")
    assert again.id == first.id and again.booking_reference == first.booking_reference
    assert confirmed_count(engine, slot.id) == 1


def test_unknown_slot_raises_slot_not_found(repo):
    with pytest.raises(SlotNotFound):
        repo.create_appointment(slot_id=uuid4(), patient_name="Ali", idempotency_key="k-x")


def test_two_threads_booking_one_slot_exactly_one_wins(repo, engine):
    slot = free_slots(repo, "khan", 1)[0]
    results = race(
        lambda: repo.create_appointment(slot_id=slot.id, patient_name="A", idempotency_key="r-1"),
        lambda: repo.create_appointment(slot_id=slot.id, patient_name="B", idempotency_key="r-2"),
    )
    wins = [r for r in results if r[0] == "ok"]
    losses = [r for r in results if r[0] == "err"]
    assert len(wins) == 1 and len(losses) == 1
    assert isinstance(losses[0][1], SlotTaken)
    assert confirmed_count(engine, slot.id) == 1


# -------------------------------------------------------------------- reschedule
def test_reschedule_moves_slot_and_bumps_version(repo):
    appt = repo.get_appointment_by_reference("CL-100002")
    target = free_slots(repo, "khan", 1)[0]
    updated = repo.reschedule(appointment_id=appt.id, expected_version=1, new_slot_id=target.id)
    assert updated.version == 2 and updated.slot_id == target.id
    assert repo.get_slot_by_id(appt.slot_id).free is True
    assert repo.get_slot_by_id(target.id).free is False


def test_reschedule_onto_taken_slot_changes_nothing(repo):
    appt = repo.get_appointment_by_reference("CL-100002")
    taken = repo.get_appointment_by_reference("CL-100001").slot_id
    with pytest.raises(SlotTaken):
        repo.reschedule(appointment_id=appt.id, expected_version=1, new_slot_id=taken)
    after = repo.get_appointment_by_reference("CL-100002")
    assert after.version == 1 and after.slot_id == appt.slot_id


def test_stale_version_cannot_overwrite(repo):
    appt = repo.get_appointment_by_reference("CL-100002")
    a, b = free_slots(repo, "khan", 2)
    repo.reschedule(appointment_id=appt.id, expected_version=1, new_slot_id=a.id)
    with pytest.raises(StaleAppointment) as exc:
        repo.reschedule(appointment_id=appt.id, expected_version=1, new_slot_id=b.id)
    assert exc.value.reason == "version_changed"
    assert repo.get_appointment_by_reference("CL-100002").slot_id == a.id


def test_reschedule_to_unknown_slot(repo):
    appt = repo.get_appointment_by_reference("CL-100002")
    with pytest.raises(SlotNotFound):
        repo.reschedule(appointment_id=appt.id, expected_version=1, new_slot_id=uuid4())


# ------------------------------------------------------------------------ cancel
def test_cancel_sets_status_timestamp_and_frees_slot(repo, engine):
    appt = repo.get_appointment_by_reference("CL-100003")
    updated = repo.cancel(appointment_id=appt.id, expected_version=1)
    assert updated.status == "cancelled" and updated.version == 2
    assert updated.cancelled_at is not None
    assert repo.get_slot_by_id(appt.slot_id).free is True


def test_cancelled_appointment_cannot_be_cancelled_or_rescheduled(repo):
    appt = repo.get_appointment_by_reference("CL-100003")
    repo.cancel(appointment_id=appt.id, expected_version=1)
    target = free_slots(repo, "malik", 1)[0]
    with pytest.raises(StaleAppointment) as e1:
        repo.cancel(appointment_id=appt.id, expected_version=2)
    with pytest.raises(StaleAppointment) as e2:
        repo.reschedule(appointment_id=appt.id, expected_version=2, new_slot_id=target.id)
    assert e1.value.reason == e2.value.reason == "cancelled"


def test_cancelled_slot_can_be_booked_again(repo):
    appt = repo.get_appointment_by_reference("CL-100003")
    repo.cancel(appointment_id=appt.id, expected_version=1)
    again = repo.create_appointment(slot_id=appt.slot_id, patient_name="New", idempotency_key="rb")
    assert again.status == "confirmed" and again.slot_id == appt.slot_id


def test_unknown_appointment_is_not_found(repo):
    with pytest.raises(StaleAppointment) as exc:
        repo.cancel(appointment_id=uuid4(), expected_version=1)
    assert exc.value.reason == "not_found"


def test_concurrent_cancel_and_reschedule_exactly_one_wins(repo):
    appt = repo.get_appointment_by_reference("CL-100002")
    target = free_slots(repo, "khan", 1)[0]
    results = race(
        lambda: repo.cancel(appointment_id=appt.id, expected_version=1),
        lambda: repo.reschedule(appointment_id=appt.id, expected_version=1,
                                new_slot_id=target.id),
    )
    assert sorted(r[0] for r in results) == ["err", "ok"]
    loser = next(r[1] for r in results if r[0] == "err")
    assert isinstance(loser, StaleAppointment)
    assert repo.get_appointment_by_reference("CL-100002").version == 2


# --------------------------------------------------------------- DB constraints
def test_check_constraint_rejects_cancelled_without_timestamp(repo, engine):
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(text(
                "update appointments set status = 'cancelled' "
                "where booking_reference = 'CL-100001'"))