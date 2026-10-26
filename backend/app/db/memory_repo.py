"""In-memory Repository used by tests (and for offline demos)."""
from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from app.brain.datetime_parse import is_slot_time
from app.db.repository import Doctor, Slot, SlotNotFound, SlotTaken, StaleAppointment
from app.schemas import AppointmentSnapshot

DOCTORS_SEED = [
    Doctor("ahmed", "Dr. Ahmed", "General Medicine"),
    Doctor("khan", "Dr. Sara Khan", "Pediatrics"),
    Doctor("malik", "Dr. Hina Malik", "Dermatology"),
]


def _random_reference() -> str:
    return f"CL-{secrets.randbelow(900000) + 100000}"


class MemoryRepository:
    def __init__(self, tz: ZoneInfo, ref_factory: Callable[[], str] | None = None):
        self._tz = tz
        self._ref_factory = ref_factory or _random_reference
        self._doctors: dict[str, Doctor] = {}
        self._slots: dict[UUID, tuple[str, datetime]] = {}
        self._appts: dict[UUID, AppointmentSnapshot] = {}
        self._keys: dict[str, UUID] = {}
        self.create_calls = 0       # every attempted mutation (tests assert on this)
        self.reschedule_calls = 0   # every attempted reschedule
        self.cancel_calls = 0       # every attempted cancel

    # ---- seeding helpers (not part of the Repository protocol)
    def add_doctor(self, doctor: Doctor) -> None:
        self._doctors[doctor.slug] = doctor

    def add_slot(self, doctor_slug: str, starts_at: datetime) -> UUID:
        slot_id = uuid4()
        self._slots[slot_id] = (doctor_slug, starts_at)
        return slot_id

    def seed_appointment(self, slot_id: UUID, patient_name: str, reference: str) -> AppointmentSnapshot:
        return self._insert(slot_id, patient_name, reference, f"seed:{reference}")

    def all_appointments(self) -> list[AppointmentSnapshot]:
        return list(self._appts.values())

    # ---- Repository protocol
    def list_doctors(self) -> list[Doctor]:
        return list(self._doctors.values())

    def list_slots(self, doctor_slug: str, day: date) -> list[Slot]:
        rows = [
            sid for sid, (slug, start) in self._slots.items()
            if slug == doctor_slug and start.astimezone(self._tz).date() == day
        ]
        return sorted((self._view(sid) for sid in rows), key=lambda s: s.starts_at)

    def get_slot(self, doctor_slug: str, day: date, at: time) -> Slot | None:
        for slot in self.list_slots(doctor_slug, day):
            if slot.starts_at.astimezone(self._tz).time() == at:
                return slot
        return None

    def get_slot_by_id(self, slot_id: UUID) -> Slot | None:
        return self._view(slot_id) if slot_id in self._slots else None

    def get_appointment_by_slot(self, slot_id: UUID) -> AppointmentSnapshot | None:
        return self._active_for_slot(slot_id)

    def create_appointment(self, *, slot_id: UUID, patient_name: str,
                           idempotency_key: str) -> AppointmentSnapshot:
        self.create_calls += 1
        if (existing := self._keys.get(idempotency_key)) is not None:
            return self._appts[existing]
        if slot_id not in self._slots:
            raise SlotNotFound()
        if self._active_for_slot(slot_id):
            raise SlotTaken()      # mirrors the partial unique index
        return self._insert(slot_id, patient_name, self._unique_reference(), idempotency_key)

    def get_appointment_by_reference(self, reference: str) -> AppointmentSnapshot | None:
        return next((a for a in self._appts.values()
                     if a.booking_reference == reference), None)

    def reschedule(self, *, appointment_id: UUID, expected_version: int,
                   new_slot_id: UUID) -> AppointmentSnapshot:
        self.reschedule_calls += 1
        appt = self._require_current(appointment_id, expected_version)
        if new_slot_id not in self._slots:
            raise SlotNotFound()
        if self._active_for_slot(new_slot_id):
            raise SlotTaken()      # mirrors the partial unique index
        slug, start = self._slots[new_slot_id]
        updated = appt.model_copy(update={
            "slot_id": new_slot_id, "doctor_slug": slug,
            "doctor_name": self._doctors[slug].display_name,
            "starts_at": start, "version": appt.version + 1})
        self._appts[appt.id] = updated          # copy-on-write: old snapshots stay old
        return updated

    def cancel(self, *, appointment_id: UUID, expected_version: int) -> AppointmentSnapshot:
        self.cancel_calls += 1
        appt = self._require_current(appointment_id, expected_version)
        updated = appt.model_copy(update={"status": "cancelled",
                                          "version": appt.version + 1})
        self._appts[appt.id] = updated
        return updated

    # ---- internals
    def _require_current(self, appointment_id: UUID, expected_version: int) -> AppointmentSnapshot:
        appt = self._appts.get(appointment_id)
        if appt is None:
            raise StaleAppointment("not_found")
        if appt.status != "confirmed":
            raise StaleAppointment("cancelled")
        if appt.version != expected_version:
            raise StaleAppointment("version_changed")
        return appt

    def _active_for_slot(self, slot_id: UUID) -> AppointmentSnapshot | None:
        for a in self._appts.values():
            if a.slot_id == slot_id and a.status == "confirmed":
                return a
        return None

    def _view(self, slot_id: UUID) -> Slot:
        slug, start = self._slots[slot_id]
        return Slot(slot_id, slug, self._doctors[slug].display_name, start,
                    self._active_for_slot(slot_id) is None)

    def _unique_reference(self) -> str:
        used = {a.booking_reference for a in self._appts.values()}
        while (ref := self._ref_factory()) in used:
            pass
        return ref

    def _insert(self, slot_id: UUID, name: str, reference: str, key: str) -> AppointmentSnapshot:
        slug, start = self._slots[slot_id]
        appt = AppointmentSnapshot(
            id=uuid4(), booking_reference=reference, version=1, status="confirmed",
            slot_id=slot_id, doctor_slug=slug, doctor_name=self._doctors[slug].display_name,
            starts_at=start, patient_name=name,
        )
        self._appts[appt.id] = appt
        self._keys[key] = appt.id
        return appt


def build_demo_repo(now: datetime, tz: ZoneInfo,
                    ref_factory: Callable[[], str] | None = None,
                    days: int = 15) -> MemoryRepository:
    """3 doctors, 30-min slots Mon-Sat for `days` days, plus 3 fake appointments."""
    repo = MemoryRepository(tz, ref_factory)
    for d in DOCTORS_SEED:
        repo.add_doctor(d)
    today = now.astimezone(tz).date()
    for offset in range(days):
        day = today + timedelta(days=offset)
        if day.weekday() == 6:
            continue
        for d in DOCTORS_SEED:
            for minute in range(9 * 60, 17 * 60, 30):
                t = time(minute // 60, minute % 60)
                if is_slot_time(t):
                    repo.add_slot(d.slug, datetime.combine(day, t, tzinfo=tz))

    def open_day(day: date) -> date:
        return day + timedelta(days=1) if day.weekday() == 6 else day

    for offset, slug, t, ref, name in [
        (1, "ahmed", time(11, 0), "CL-100001", "Test Patient"),
        (2, "khan", time(14, 0), "CL-100002", "Maria Test"),
        (3, "malik", time(10, 0), "CL-100003", "Omar Test"),
    ]:
        slot = repo.get_slot(slug, open_day(today + timedelta(days=offset)), t)
        assert slot is not None
        repo.seed_appointment(slot.id, name, ref)
    return repo