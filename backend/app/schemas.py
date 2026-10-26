"""Pydantic models: pending-conversation context, events, and API contract.

Mirrors PLAN sections 5 (context) and 7 (API). Pure data; no logic.
"""
from __future__ import annotations

from datetime import date as Date
from datetime import datetime, time as Time
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.brain.states import EventKind, Flow, Intent, State

TimePeriod = Literal["morning", "afternoon"]


# ---------- conversation context (persisted as JSON on the session row) ----------

class Entities(BaseModel):
    doctor_slug: str | None = None
    doctor_name: str | None = None
    date: Date | None = None
    time: Time | None = None
    time_period: TimePeriod | None = None
    patient_name: str | None = None
    booking_reference: str | None = None
    verify_name: str | None = None
    new_date: Date | None = None
    new_time: Time | None = None


class OfferedSlot(BaseModel):
    idx: int = Field(ge=1)
    slot_id: UUID
    label: str                             # e.g. "Thu 11:30 AM"


class AppointmentSnapshot(BaseModel):
    id: UUID
    booking_reference: str
    version: int
    status: Literal["confirmed", "cancelled"]
    slot_id: UUID
    doctor_slug: str
    doctor_name: str
    starts_at: datetime
    patient_name: str


class PendingAction(BaseModel):
    action_id: UUID
    type: Flow
    fingerprint: str
    slot_id: UUID | None = None            # target slot (book / reschedule)
    appointment_id: UUID | None = None     # reschedule / cancel
    appointment_version: int | None = None
    summary: str                           # text read back to the caller
    created_at: datetime


class LastCompleted(BaseModel):
    fingerprint: str
    booking_reference: str


class Context(BaseModel):
    flow: Flow | None = None
    entities: Entities = Field(default_factory=Entities)
    appointment_snapshot: AppointmentSnapshot | None = None
    selected_slot_id: UUID | None = None
    offered_slots: list[OfferedSlot] = Field(default_factory=list)
    pending_action: PendingAction | None = None
    retries: dict[str, int] = Field(default_factory=dict)
    last_error: str | None = None
    last_completed: LastCompleted | None = None


# ---------- events (decision trace) ----------

class ConversationEvent(BaseModel):
    seq: int
    kind: EventKind
    payload: dict[str, Any] = Field(default_factory=dict)
    executed: bool | None = None           # True only for real DB writes


# ---------- API request / response ----------

class TurnRequest(BaseModel):
    turn_id: UUID                          # client-generated; makes retries idempotent
    text: str = Field(default="", max_length=500)   # empty allowed: engine handles it
    input_mode: Literal["voice", "text"] = "text"
    stt_confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class PendingActionView(BaseModel):
    status: Literal["proposed"] = "proposed"        # never "executed"
    summary: str
    expires_at: datetime


class CompletedAction(BaseModel):
    type: Flow
    executed: Literal[True] = True
    booking_reference: str
    appointment: AppointmentSnapshot


class TurnResponse(BaseModel):
    conversation_id: UUID
    turn_no: int
    assistant_text: str
    state: State
    intent: Intent
    flow: Flow | None = None
    entities: Entities
    missing_fields: list[str] = Field(default_factory=list)
    offered_slots: list[OfferedSlot] = Field(default_factory=list)
    pending_action: PendingActionView | None = None
    completed_action: CompletedAction | None = None
    events: list[ConversationEvent] = Field(default_factory=list)
    error: str | None = None


class CreateConversationResponse(BaseModel):
    conversation_id: UUID
    state: State = State.IDLE
    assistant_text: str


class ErrorBody(BaseModel):
    code: str
    message: str
    details: Any | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody