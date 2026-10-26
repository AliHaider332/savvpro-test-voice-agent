from datetime import date, time
from uuid import uuid4

import pytest

from app.brain.engine import fingerprint
from app.brain.states import Flow
from app.db.repository import SlotTaken
from app.schemas import Context, Entities
from app.services.appointments import BookingError

TUE = date(2026, 10, 27)


def test_check_slot_statuses(service, clock):
    assert service.check_slot("ahmed", TUE, time(11, 0), clock.now).status == "taken"
    assert service.check_slot("ahmed", TUE, time(10, 0), clock.now).status == "free"
    assert service.check_slot("ahmed", date(2026, 11, 1), time(10, 0), clock.now).status == "missing"
    later = clock.now.replace(hour=10)
    assert service.check_slot("khan", date(2026, 10, 26), time(9, 0), later).status == "past"


def test_alternatives_are_nearest_then_chronological(service, clock):
    alts = service.alternatives("ahmed", TUE, time(11, 0), clock.now, n=3)
    assert [service.local_time(s) for s in alts] == [time(10, 0), time(10, 30), time(11, 30)]


def test_period_slots(service, clock):
    aft = service.period_slots("malik", TUE, "afternoon", clock.now)
    assert [service.local_time(s) for s in aft] == [time(12, 0), time(12, 30), time(14, 0)]
    morning = service.period_slots("malik", TUE, "morning", clock.now)
    assert [service.local_time(s) for s in morning] == [time(9, 0), time(9, 30), time(10, 0)]


def test_book_is_idempotent_on_key(service, repo, clock):
    slot = repo.get_slot("khan", date(2026, 10, 28), time(10, 0))
    a = service.book(slot_id=slot.id, patient_name="Ali", idempotency_key="k1", now=clock.now)
    b = service.book(slot_id=slot.id, patient_name="Ali", idempotency_key="k1", now=clock.now)
    assert a.id == b.id
    assert len([x for x in repo.all_appointments() if x.patient_name == "Ali"]) == 1


def test_double_booking_is_rejected(service, repo, clock):
    slot = repo.get_slot("khan", date(2026, 10, 28), time(10, 0))
    service.book(slot_id=slot.id, patient_name="Ali", idempotency_key="k1", now=clock.now)
    with pytest.raises(BookingError) as err:
        service.book(slot_id=slot.id, patient_name="Sara", idempotency_key="k2", now=clock.now)
    assert err.value.code == "SLOT_TAKEN"


def test_repo_rejects_taken_slot_directly(repo):
    slot = repo.get_slot("ahmed", TUE, time(11, 0))
    with pytest.raises(SlotTaken):
        repo.create_appointment(slot_id=slot.id, patient_name="X", idempotency_key="z")


def test_book_unknown_slot_and_bad_name(service, repo, clock):
    with pytest.raises(BookingError) as err:
        service.book(slot_id=uuid4(), patient_name="Ali", idempotency_key="a", now=clock.now)
    assert err.value.code == "SLOT_UNAVAILABLE"
    slot = repo.get_slot("khan", date(2026, 10, 28), time(10, 0))
    with pytest.raises(BookingError) as err:
        service.book(slot_id=slot.id, patient_name="  ", idempotency_key="b", now=clock.now)
    assert err.value.code == "INVALID_NAME"


def test_fingerprint_changes_when_anything_material_changes():
    c = Context(flow=Flow.BOOK, entities=Entities(doctor_slug="khan", patient_name="Ali"),
                selected_slot_id=uuid4())
    base = fingerprint(c)
    renamed = c.model_copy(deep=True)
    renamed.entities.patient_name = "Sara"
    reslotted = c.model_copy(deep=True)
    reslotted.selected_slot_id = uuid4()
    assert fingerprint(renamed) != base and fingerprint(reslotted) != base
    assert fingerprint(c.model_copy(deep=True)) == base