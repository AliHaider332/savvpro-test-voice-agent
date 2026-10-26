from datetime import date, time

import pytest

from app.services import queries

TUE = date(2026, 10, 27)


def test_doctors(service):
    docs = queries.list_doctors(service)
    assert {d.slug for d in docs} == {"ahmed", "khan", "malik"}
    assert all(d.display_name and d.specialty for d in docs)


def test_availability_lists_only_free_future_slots_in_clinic_time(service, clock):
    out = queries.availability(service, "ahmed", TUE, clock.now)
    times = [s.time for s in out.slots]
    assert "11:00" not in times and "10:00" in times      # 11:00 holds a seeded appointment
    assert times == sorted(times)
    assert out.doctor_name == "Dr. Ahmed" and out.date == TUE and out.timezone == "Asia/Karachi"
    first = out.slots[0]
    assert first.starts_at.utcoffset().total_seconds() == 5 * 3600
    assert first.label == "9 AM"


def test_availability_is_case_insensitive_and_hides_past_slots(service, clock):
    later = clock.now.replace(hour=10, minute=15)
    out = queries.availability(service, " KHAN ", date(2026, 10, 26), later)
    assert out.doctor_slug == "khan"
    assert all(time.fromisoformat(s.time) > time(10, 15) for s in out.slots)


def test_availability_empty_for_a_day_without_slots_and_unknown_doctor(service, clock):
    assert queries.availability(service, "khan", date(2026, 11, 1), clock.now).slots == []
    with pytest.raises(queries.DoctorNotFound):
        queries.availability(service, "nobody", TUE, clock.now)


def test_appointment_requires_matching_name_and_hides_internal_fields(service):
    view = queries.appointment(service, "CL-100002", "maria")
    assert view.booking_reference == "CL-100002" and view.status == "confirmed"
    assert view.doctor_slug == "khan" and view.patient_name == "Maria Test"
    assert not ({"id", "slot_id", "version"} & set(view.model_dump()))


@pytest.mark.parametrize("ref,name", [
    ("CL-100002", "Wrong Person"),     # wrong name
    ("CL-999999", "Maria"),            # unknown reference
    ("not-a-ref", "Maria"),            # malformed reference
    ("CL-100002", "   "),              # blank name
])
def test_appointment_failures_are_indistinguishable(service, ref, name):
    with pytest.raises(queries.AppointmentNotFound):
        queries.appointment(service, ref, name)
        