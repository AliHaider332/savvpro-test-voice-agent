"""Business rules for appointments. No parsing, no conversation state."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from app.db.repository import Doctor, Repository, Slot, SlotNotFound, SlotTaken
from app.schemas import AppointmentSnapshot

SlotStatus = Literal["free", "taken", "past", "missing"]
AFTERNOON_STARTS_HOUR = 12


@dataclass(frozen=True)
class SlotCheck:
    status: SlotStatus
    slot: Slot | None = None


class BookingError(Exception):
    """code: SLOT_TAKEN | SLOT_UNAVAILABLE | INVALID_NAME"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


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