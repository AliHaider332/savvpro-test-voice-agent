from uuid import uuid4

import pytest

from app.db.repository import SlotTaken, StaleAppointment


def maria(repo):
    return repo.get_appointment_by_reference("CL-100002")   # Khan, 14:00


def free_khan_slot(repo, appt):
    return next(s for s in repo.list_slots("khan", appt.starts_at.date()) if s.free)


def test_lookup_by_reference(repo):
    assert maria(repo).patient_name == "Maria Test"
    assert repo.get_appointment_by_reference("CL-999999") is None


def test_reschedule_moves_slot_and_bumps_version(repo):
    appt = maria(repo)
    target = free_khan_slot(repo, appt)
    updated = repo.reschedule(appointment_id=appt.id, expected_version=1, new_slot_id=target.id)
    assert updated.version == 2 and updated.slot_id == target.id
    assert updated.doctor_slug == "khan"
    assert repo.get_slot_by_id(appt.slot_id).free is True        # old slot released
    assert repo.get_slot_by_id(target.id).free is False          # new slot occupied


def test_reschedule_to_taken_slot_changes_nothing(repo):
    appt = maria(repo)
    taken = repo.get_appointment_by_reference("CL-100001").slot_id
    with pytest.raises(SlotTaken):
        repo.reschedule(appointment_id=appt.id, expected_version=1, new_slot_id=taken)
    assert maria(repo).version == 1 and maria(repo).slot_id == appt.slot_id


def test_stale_version_cannot_overwrite(repo):
    appt = maria(repo)
    target = free_khan_slot(repo, appt)
    repo.reschedule(appointment_id=appt.id, expected_version=1, new_slot_id=target.id)
    with pytest.raises(StaleAppointment) as exc:
        repo.cancel(appointment_id=appt.id, expected_version=1)   # old version
    assert exc.value.reason == "version_changed"
    assert maria(repo).status == "confirmed"


def test_cancel_frees_slot_and_bumps_version(repo):
    appt = maria(repo)
    updated = repo.cancel(appointment_id=appt.id, expected_version=1)
    assert updated.status == "cancelled" and updated.version == 2
    assert repo.get_slot_by_id(appt.slot_id).free is True


def test_cancelled_appointment_cannot_be_cancelled_or_rescheduled(repo):
    appt = maria(repo)
    repo.cancel(appointment_id=appt.id, expected_version=1)
    target = free_khan_slot(repo, appt)
    with pytest.raises(StaleAppointment) as e1:
        repo.cancel(appointment_id=appt.id, expected_version=2)
    with pytest.raises(StaleAppointment) as e2:
        repo.reschedule(appointment_id=appt.id, expected_version=2, new_slot_id=target.id)
    assert e1.value.reason == e2.value.reason == "cancelled"


def test_unknown_appointment_is_not_found(repo):
    with pytest.raises(StaleAppointment) as exc:
        repo.cancel(appointment_id=uuid4(), expected_version=1)
    assert exc.value.reason == "not_found"


def test_earlier_snapshot_is_not_mutated(repo):
    before = maria(repo)
    repo.cancel(appointment_id=before.id, expected_version=1)
    assert before.status == "confirmed" and before.version == 1


def test_attempt_counters(repo):
    appt = maria(repo)
    repo.cancel(appointment_id=appt.id, expected_version=1)
    assert (repo.create_calls, repo.reschedule_calls, repo.cancel_calls) == (0, 0, 1)