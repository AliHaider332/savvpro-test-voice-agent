"""Persistence interface. The engine/service depend on this Protocol only,
so tests use MemoryRepository and production uses a Postgres implementation."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Protocol
from uuid import UUID

from app.schemas import AppointmentSnapshot


@dataclass(frozen=True)
class Doctor:
    slug: str
    display_name: str
    specialty: str


@dataclass(frozen=True)
class Slot:
    id: UUID
    doctor_slug: str
    doctor_name: str
    starts_at: datetime      # timezone-aware
    free: bool               # no confirmed appointment references it


class SlotTaken(Exception):
    """The slot already has a confirmed appointment (unique-index violation)."""


class SlotNotFound(Exception):
    pass


class Repository(Protocol):
    def list_doctors(self) -> list[Doctor]: ...
    def list_slots(self, doctor_slug: str, day: date) -> list[Slot]: ...
    def get_slot(self, doctor_slug: str, day: date, at: time) -> Slot | None: ...
    def get_slot_by_id(self, slot_id: UUID) -> Slot | None: ...
    def get_appointment_by_slot(self, slot_id: UUID) -> AppointmentSnapshot | None: ...
    def create_appointment(
        self, *, slot_id: UUID, patient_name: str, idempotency_key: str
    ) -> AppointmentSnapshot:
        """Raises SlotTaken / SlotNotFound. Same idempotency_key returns the
        original appointment instead of creating a second one."""
        ...