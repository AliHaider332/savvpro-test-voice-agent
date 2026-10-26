"""Business rules for appointments. No parsing, no conversation state."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from app.db.repository import (
    Doctor, Repository, Slot, SlotNotFound, SlotTaken, StaleAppointment,
)
from app.schemas import AppointmentSnapshot

SlotStatus = Literal["free", "taken", "past", "missing"]
AFTERNOON_STARTS_HOUR = 12
_REFERENCE = re.compile(r"CL-\d{6}")

_STALE_CODES = {
    "not_found": "NOT_FOUND",
    "cancelled": "ALREADY_CANCELLED",
    "version_changed": "STALE_VERSION",
}


@dataclass(frozen=True)
class SlotCheck:
    status: SlotStatus
    slot: Slot | None = None


class BookingError(Exception):
    """code: SLOT_TAKEN | SLOT_UNAVAILABLE | INVALID_NAME   (book)
             SAME_SLOT | APPOINTMENT_PAST                   (reschedule / cancel)
             NOT_FOUND | ALREADY_CANCELLED | STALE_VERSION  (reschedule / cancel)"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _name_tokens(name: str) -> list[str]:
    return (name or "").casefold().split()


def _names_match(given: str, stored: str) -> bool:
    """Case/space-insensitive; `given` must be a leading part of `stored`
    ("Maria" matches "Maria Test"; "Test" does not)."""
    g, s = _name_tokens(given), _name_tokens(stored)
    return bool(g) and len(g) <= len(s) and s[: len(g)] == g


class AppointmentService:
    def __init__(self, repo: Repository, tz: ZoneInfo):
        self._repo = repo
        self.tz = tz

    def local_time(self, slot: Slot) -> time:
        return slot.starts_at.astimezone(self.tz).time()

    def _minutes(self, slot: Slot) -> int:
        t = self.local_time(slot)
        return t.hour * 60 + t.minute

    def doctors(self) -> list[Doctor]:
        return self._repo.list_doctors()

    def get_slot_by_id(self, slot_id: UUID) -> Slot | None:
        return self._repo.get_slot_by_id(slot_id)

    def appointment_for_slot(self, slot_id: UUID) -> AppointmentSnapshot | None:
        return self._repo.get_appointment_by_slot(slot_id)

    def check_slot(self, doctor_slug: str, day: date, at: time, now: datetime) -> SlotCheck:
        slot = self._repo.get_slot(doctor_slug, day, at)
        if slot is None:
            return SlotCheck("missing")
        if slot.starts_at <= now:
            return SlotCheck("past", slot)
        if not slot.free:
            return SlotCheck("taken", slot)
        return SlotCheck("free", slot)

    def free_slots(self, doctor_slug: str, day: date, now: datetime) -> list[Slot]:
        return [s for s in self._repo.list_slots(doctor_slug, day)
                if s.free and s.starts_at > now]

    def alternatives(self, doctor_slug: str, day: date, near: time,
                     now: datetime, n: int = 3) -> list[Slot]:
        """Nearest free slots to `near` (ties: earlier first), shown chronologically."""
        target = near.hour * 60 + near.minute
        free = self.free_slots(doctor_slug, day, now)
        nearest = sorted(free, key=lambda s: (abs(self._minutes(s) - target), s.starts_at))[:n]
        return sorted(nearest, key=lambda s: s.starts_at)

    def period_slots(self, doctor_slug: str, day: date, period: str,
                     now: datetime, n: int = 3) -> list[Slot]:
        free = self.free_slots(doctor_slug, day, now)
        if period == "morning":
            free = [s for s in free if self.local_time(s).hour < AFTERNOON_STARTS_HOUR]
        else:
            free = [s for s in free if self.local_time(s).hour >= AFTERNOON_STARTS_HOUR]
        return free[:n]

    # ------------------------------------------------------------------ book
    def book(self, *, slot_id: UUID, patient_name: str, idempotency_key: str,
             now: datetime) -> AppointmentSnapshot:
        name = (patient_name or "").strip()
        if not 1 <= len(name) <= 40:
            raise BookingError("INVALID_NAME")
        slot = self._repo.get_slot_by_id(slot_id)
        if slot is None or slot.starts_at <= now:
            raise BookingError("SLOT_UNAVAILABLE")
        try:
            return self._repo.create_appointment(
                slot_id=slot_id, patient_name=name, idempotency_key=idempotency_key)
        except SlotTaken:
            raise BookingError("SLOT_TAKEN") from None
        except SlotNotFound:
            raise BookingError("SLOT_UNAVAILABLE") from None

    # ---------------------------------------------------------------- verify
    def verify_appointment(self, reference: str | None,
                           name: str | None) -> AppointmentSnapshot | None:
        """Reference + name. Returns None for unknown reference, malformed
        reference AND wrong name alike, so callers cannot tell them apart.
        A cancelled appointment still verifies (the caller proved who they are)."""
        ref = (reference or "").strip().upper()
        if not _REFERENCE.fullmatch(ref) or not (name or "").strip():
            return None
        appt = self._repo.get_appointment_by_reference(ref)
        if appt is None or not _names_match(name, appt.patient_name):
            return None
        return appt

    # ------------------------------------------------------------ reschedule
    def reschedule(self, *, appointment: AppointmentSnapshot, new_slot_id: UUID,
                   now: datetime) -> AppointmentSnapshot:
        """Move to another slot of the SAME doctor. Version-checked."""
        slot = self._repo.get_slot_by_id(new_slot_id)
        if slot is None or slot.starts_at <= now or slot.doctor_slug != appointment.doctor_slug:
            raise BookingError("SLOT_UNAVAILABLE")
        if new_slot_id == appointment.slot_id:
            raise BookingError("SAME_SLOT")
        if appointment.starts_at <= now:
            raise BookingError("APPOINTMENT_PAST")
        try:
            return self._repo.reschedule(appointment_id=appointment.id,
                                         expected_version=appointment.version,
                                         new_slot_id=new_slot_id)
        except SlotTaken:
            raise BookingError("SLOT_TAKEN") from None
        except SlotNotFound:
            raise BookingError("SLOT_UNAVAILABLE") from None
        except StaleAppointment as err:
            raise BookingError(_STALE_CODES[err.reason]) from None

    # ---------------------------------------------------------------- cancel
    def cancel(self, *, appointment: AppointmentSnapshot, now: datetime) -> AppointmentSnapshot:
        """Version-checked. A past appointment cannot be cancelled."""
        if appointment.starts_at <= now:
            raise BookingError("APPOINTMENT_PAST")
        try:
            return self._repo.cancel(appointment_id=appointment.id,
                                     expected_version=appointment.version)
        except StaleAppointment as err:
            raise BookingError(_STALE_CODES[err.reason]) from None