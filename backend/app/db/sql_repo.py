"""Postgres Repository (SQLAlchemy 2.0 Core + psycopg). Same contract as MemoryRepository."""
from __future__ import annotations

import secrets
from datetime import date, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from app.db.repository import (
    Doctor, Slot, SlotNotFound, SlotTaken, StaleAppointment,
)
from app.schemas import AppointmentSnapshot

SLOT_INDEX = "one_confirmed_appointment_per_slot"
REF_UNIQUE = "appointments_booking_reference_key"
KEY_UNIQUE = "appointments_idempotency_key_key"
SLOT_FK = "appointments_slot_id_fkey"

_SLOT_SQL = """
select s.id, d.slug as doctor_slug, d.display_name as doctor_name, s.starts_at,
       not exists (select 1 from appointments a
                   where a.slot_id = s.id and a.status = 'confirmed') as free
from slots s join doctors d on d.id = s.doctor_id
"""

_APPT_SQL = """
select a.id, a.booking_reference, a.version, a.status, a.slot_id,
       d.slug as doctor_slug, d.display_name as doctor_name, s.starts_at,
       a.patient_name, a.cancelled_at
from appointments a
join slots s on s.id = a.slot_id
join doctors d on d.id = s.doctor_id
"""


def normalize_url(url: str) -> str:
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def make_engine(url: str) -> Engine:
    return create_engine(normalize_url(url), pool_pre_ping=True,
                         connect_args={"prepare_threshold": None})


def _constraint(err: IntegrityError) -> str:
    diag = getattr(err.orig, "diag", None)
    return getattr(diag, "constraint_name", None) or ""


def _random_reference() -> str:
    return f"CL-{secrets.randbelow(900000) + 100000}"


class SqlRepository:
    def __init__(self, engine: Engine, tz: ZoneInfo):
        self._engine = engine
        self._tz = tz

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _slot(row) -> Slot:
        return Slot(row["id"], row["doctor_slug"], row["doctor_name"],
                    row["starts_at"], bool(row["free"]))

    @staticmethod
    def _appt(conn: Connection, where: str, **params) -> AppointmentSnapshot | None:
        row = conn.execute(text(f"{_APPT_SQL} {where}"), params).mappings().first()
        return AppointmentSnapshot(**row) if row else None

    def _diagnose(self, appointment_id: UUID) -> None:
        """Called after a guarded UPDATE matched no row. Always raises."""
        with self._engine.connect() as conn:
            row = conn.execute(text("select status from appointments where id = :id"),
                               {"id": appointment_id}).first()
        if row is None:
            raise StaleAppointment("not_found")
        if row[0] != "confirmed":
            raise StaleAppointment("cancelled")
        raise StaleAppointment("version_changed")

    # --------------------------------------------------------------- reads
    def list_doctors(self) -> list[Doctor]:
        with self._engine.connect() as conn:
            rows = conn.execute(text(
                "select slug, display_name, specialty from doctors "
                "where active order by display_name")).all()
        return [Doctor(r[0], r[1], r[2]) for r in rows]

    def list_slots(self, doctor_slug: str, day: date) -> list[Slot]:
        start = datetime.combine(day, time(0, 0), tzinfo=self._tz)
        with self._engine.connect() as conn:
            rows = conn.execute(text(
                f"{_SLOT_SQL} where d.slug = :slug and s.starts_at >= :a and s.starts_at < :b "
                "order by s.starts_at"),
                {"slug": doctor_slug, "a": start, "b": start + timedelta(days=1)}
            ).mappings().all()
        return [self._slot(r) for r in rows]

    def get_slot(self, doctor_slug: str, day: date, at: time) -> Slot | None:
        starts = datetime.combine(day, at, tzinfo=self._tz)
        with self._engine.connect() as conn:
            row = conn.execute(text(
                f"{_SLOT_SQL} where d.slug = :slug and s.starts_at = :t"),
                {"slug": doctor_slug, "t": starts}).mappings().first()
        return self._slot(row) if row else None

    def get_slot_by_id(self, slot_id: UUID) -> Slot | None:
        with self._engine.connect() as conn:
            row = conn.execute(text(f"{_SLOT_SQL} where s.id = :id"),
                               {"id": slot_id}).mappings().first()
        return self._slot(row) if row else None

    def get_appointment_by_slot(self, slot_id: UUID) -> AppointmentSnapshot | None:
        with self._engine.connect() as conn:
            return self._appt(conn, "where a.slot_id = :id and a.status = 'confirmed'",
                              id=slot_id)

    def get_appointment_by_reference(self, reference: str) -> AppointmentSnapshot | None:
        with self._engine.connect() as conn:
            return self._appt(conn, "where a.booking_reference = :r", r=reference)

    # ------------------------------------------------------------- writes
    def create_appointment(self, *, slot_id: UUID, patient_name: str,
                           idempotency_key: str) -> AppointmentSnapshot:
        for _ in range(5):
            try:
                with self._engine.begin() as conn:
                    existing = self._appt(conn, "where a.idempotency_key = :k",
                                          k=idempotency_key)
                    if existing:
                        return existing
                    if conn.execute(text("select 1 from slots where id = :id"),
                                    {"id": slot_id}).first() is None:
                        raise SlotNotFound()
                    new_id = conn.execute(text(
                        "insert into appointments "
                        "(booking_reference, slot_id, patient_name, idempotency_key) "
                        "values (:ref, :slot, :name, :key) returning id"),
                        {"ref": _random_reference(), "slot": slot_id,
                         "name": patient_name, "key": idempotency_key}).scalar_one()
                    return self._appt(conn, "where a.id = :id", id=new_id)
            except IntegrityError as err:
                name = _constraint(err)
                if name == SLOT_INDEX:
                    raise SlotTaken() from None
                if name == SLOT_FK:
                    raise SlotNotFound() from None
                if name == KEY_UNIQUE:                      # concurrent retry of the same action
                    with self._engine.connect() as conn:
                        existing = self._appt(conn, "where a.idempotency_key = :k",
                                              k=idempotency_key)
                    if existing:
                        return existing
                if name == REF_UNIQUE:
                    continue                                # reference collision: try another
                raise
        raise RuntimeError("could not allocate a unique booking reference")

    def reschedule(self, *, appointment_id: UUID, expected_version: int,
                   new_slot_id: UUID) -> AppointmentSnapshot:
        try:
            with self._engine.begin() as conn:
                row = conn.execute(text(
                    "update appointments set slot_id = :slot, version = version + 1, "
                    "updated_at = now() "
                    "where id = :id and status = 'confirmed' and version = :v returning id"),
                    {"slot": new_slot_id, "id": appointment_id, "v": expected_version}).first()
                if row is not None:
                    return self._appt(conn, "where a.id = :id", id=appointment_id)
        except IntegrityError as err:
            name = _constraint(err)
            if name == SLOT_INDEX:
                raise SlotTaken() from None
            if name == SLOT_FK:
                raise SlotNotFound() from None
            raise
        self._diagnose(appointment_id)
        raise AssertionError("unreachable")

    def cancel(self, *, appointment_id: UUID, expected_version: int) -> AppointmentSnapshot:
        with self._engine.begin() as conn:
            row = conn.execute(text(
                "update appointments set status = 'cancelled', cancelled_at = now(), "
                "version = version + 1, updated_at = now() "
                "where id = :id and status = 'confirmed' and version = :v returning id"),
                {"id": appointment_id, "v": expected_version}).first()
            if row is not None:
                return self._appt(conn, "where a.id = :id", id=appointment_id)
        self._diagnose(appointment_id)
        raise AssertionError("unreachable")