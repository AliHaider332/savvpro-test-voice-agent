"""Read-only lookups behind the doctor / availability / appointment endpoints."""
from __future__ import annotations

from datetime import date, datetime

from app.brain.datetime_parse import format_time
from app.schemas import AppointmentView, AvailabilityResponse, DoctorOut, SlotOut
from app.services.appointments import AppointmentService


class DoctorNotFound(Exception):
    pass


class AppointmentNotFound(Exception):
    """Unknown reference, malformed reference and wrong name all look the same."""


def list_doctors(svc: AppointmentService) -> list[DoctorOut]:
    return [DoctorOut(slug=d.slug, display_name=d.display_name, specialty=d.specialty)
            for d in svc.doctors()]


def availability(svc: AppointmentService, doctor_slug: str, day: date,
                 now: datetime) -> AvailabilityResponse:
    slug = doctor_slug.strip().lower()
    doctor = next((d for d in svc.doctors() if d.slug == slug), None)
    if doctor is None:
        raise DoctorNotFound()
    slots = []
    for slot in svc.free_slots(slug, day, now):
        local = slot.starts_at.astimezone(svc.tz)
        slots.append(SlotOut(starts_at=local, time=local.strftime("%H:%M"),
                             label=format_time(local.time())))
    return AvailabilityResponse(doctor_slug=doctor.slug, doctor_name=doctor.display_name,
                                date=day, timezone=str(svc.tz), slots=slots)


def appointment(svc: AppointmentService, reference: str, name: str) -> AppointmentView:
    """Reference AND patient name must match, as in the phone flow, so a booking
    reference alone never reveals who the patient is."""
    appt = svc.verify_appointment(reference, name)
    if appt is None:
        raise AppointmentNotFound()
    return AppointmentView(
        booking_reference=appt.booking_reference, status=appt.status,
        doctor_slug=appt.doctor_slug, doctor_name=appt.doctor_name,
        starts_at=appt.starts_at.astimezone(svc.tz), patient_name=appt.patient_name,
        cancelled_at=appt.cancelled_at.astimezone(svc.tz) if appt.cancelled_at else None)