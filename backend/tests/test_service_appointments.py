from datetime import timedelta
from uuid import uuid4

import pytest

from app.services.appointments import BookingError


def maria(service):
    return service.verify_appointment("CL-100002", "Maria")      # Khan, Wed 14:00


def free_slot(repo, doctor, day):
    return next(s for s in repo.list_slots(doctor, day) if s.free)


def code_of(fn, **kw):
    with pytest.raises(BookingError) as exc:
        fn(**kw)
    return exc.value.code


# ------------------------------------------------------------------ verify
@pytest.mark.parametrize("ref,name", [
    ("CL-100002", "Maria"),
    ("CL-100002", "  maria   TEST "),
    ("cl-100002", "MARIA TEST"),
    ("CL-100002", "Maria Test"),
])
def test_verify_accepts_case_spacing_and_leading_name(service, ref, name):
    assert service.verify_appointment(ref, name).booking_reference == "CL-100002"


@pytest.mark.parametrize("ref,name", [
    ("CL-100002", "Sara"),          # wrong name
    ("CL-100002", "Test"),          # not a leading part of the stored name
    ("CL-100002", ""),              # no name
    ("CL-999999", "Maria"),         # unknown reference
    ("garbage", "Maria"),           # malformed reference
])
def test_verify_failures_are_all_none(service, ref, name):
    assert service.verify_appointment(ref, name) is None


def test_verify_returns_cancelled_appointment(service, clock):
    appt = maria(service)
    service.cancel(appointment=appt, now=clock())
    assert service.verify_appointment("CL-100002", "Maria").status == "cancelled"


# -------------------------------------------------------------- reschedule
def test_reschedule_success(service, repo, clock):
    appt = maria(service)
    target = free_slot(repo, "khan", appt.starts_at.date())
    updated = service.reschedule(appointment=appt, new_slot_id=target.id, now=clock())
    assert updated.slot_id == target.id and updated.version == 2
    assert repo.reschedule_calls == 1


def test_reschedule_other_doctor_refused_without_mutation(service, repo, clock):
    appt = maria(service)
    target = free_slot(repo, "ahmed", appt.starts_at.date())
    assert code_of(service.reschedule, appointment=appt, new_slot_id=target.id,
                   now=clock()) == "SLOT_UNAVAILABLE"
    assert repo.reschedule_calls == 0


def test_reschedule_to_same_slot(service, repo, clock):
    appt = maria(service)
    assert code_of(service.reschedule, appointment=appt, new_slot_id=appt.slot_id,
                   now=clock()) == "SAME_SLOT"
    assert repo.reschedule_calls == 0


def test_reschedule_unknown_slot(service, repo, clock):
    assert code_of(service.reschedule, appointment=maria(service), new_slot_id=uuid4(),
                   now=clock()) == "SLOT_UNAVAILABLE"
    assert repo.reschedule_calls == 0


def test_reschedule_to_past_slot(service, repo, clock):
    appt = maria(service)
    today = clock().date()
    earlier = repo.list_slots("khan", today)[0]                 # today 09:00
    later = clock() + timedelta(hours=4)                        # 12:00
    assert code_of(service.reschedule, appointment=appt, new_slot_id=earlier.id,
                   now=later) == "SLOT_UNAVAILABLE"
    assert repo.reschedule_calls == 0


def test_reschedule_to_taken_slot(service, repo, clock):
    appt = maria(service)
    taken = repo.get_appointment_by_reference("CL-100001")      # Ahmed, but make Khan's taken:
    other = service.verify_appointment("CL-100003", "Omar")     # Malik
    # occupy a free Khan slot with a fresh booking, then try to move onto it
    target = free_slot(repo, "khan", appt.starts_at.date())
    repo.create_appointment(slot_id=target.id, patient_name="Zed", idempotency_key="k1")
    assert code_of(service.reschedule, appointment=appt, new_slot_id=target.id,
                   now=clock()) == "SLOT_TAKEN"
    assert repo.get_appointment_by_reference("CL-100002").slot_id == appt.slot_id
    assert taken and other


def test_reschedule_with_stale_version(service, repo, clock):
    appt = maria(service)
    day = appt.starts_at.date()
    first = free_slot(repo, "khan", day)
    service.reschedule(appointment=appt, new_slot_id=first.id, now=clock())
    second = free_slot(repo, "khan", day)
    assert code_of(service.reschedule, appointment=appt, new_slot_id=second.id,
                   now=clock()) == "STALE_VERSION"


def test_reschedule_cancelled_appointment(service, repo, clock):
    appt = maria(service)
    service.cancel(appointment=appt, now=clock())
    target = free_slot(repo, "khan", appt.starts_at.date())
    assert code_of(service.reschedule, appointment=appt, new_slot_id=target.id,
                   now=clock()) == "ALREADY_CANCELLED"


# ------------------------------------------------------------------ cancel
def test_cancel_success_frees_slot(service, repo, clock):
    appt = maria(service)
    updated = service.cancel(appointment=appt, now=clock())
    assert updated.status == "cancelled" and updated.version == 2
    assert repo.get_slot_by_id(appt.slot_id).free is True


def test_cancel_with_stale_version(service, repo, clock):
    appt = maria(service)
    target = free_slot(repo, "khan", appt.starts_at.date())
    service.reschedule(appointment=appt, new_slot_id=target.id, now=clock())
    assert code_of(service.cancel, appointment=appt, now=clock()) == "STALE_VERSION"
    assert repo.get_appointment_by_reference("CL-100002").status == "confirmed"


def test_cancel_twice(service, clock):
    appt = maria(service)
    service.cancel(appointment=appt, now=clock())
    assert code_of(service.cancel, appointment=appt, now=clock()) == "ALREADY_CANCELLED"


def test_cancel_past_appointment_is_not_attempted(service, repo, clock):
    appt = maria(service)
    assert code_of(service.cancel, appointment=appt,
                   now=clock() + timedelta(days=3)) == "APPOINTMENT_PAST"
    assert repo.cancel_calls == 0