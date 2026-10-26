"""Shared Pydantic models — the contract between every layer.

Parser produces ParsedTurn. Engine updates Context and produces TurnResult.
API serialises TurnResponse. Frontend mirrors these in lib/types.ts.

Anything here that changes is a breaking change: bump the field, don't rename.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.brain.states import Flow, Intent, State


# ---------- slots & appointments ----------

class SlotView(BaseModel):
    """One bookable half-hour. Label is the human-readable form ("10:30 AM")."""

    slot_id: UUID
    starts_at: datetime
    label: str


class AppointmentSnapshot(BaseModel):
    """The pieces of an appointment the engine needs to reason about it."""

    id: UUID
    booking_reference: str  # ^CL-\d{6}$
    slot_id: UUID
    doctor_name: str
    starts_at: datetime
    patient_name: str
    status: Literal["confirmed", "cancelled"]
    version: int


# ---------- pending / completed actions ----------

class PendingAction(BaseModel):
    """A proposed mutation. Only the engine can create it, only execute() can apply it.

    fingerprint = sha256(flow|doctor|slot_id|name|appt_id|appt_version)
    age < 10 min. Any entity change clears this.
    """

    action_id: UUID
    type: Literal["book", "reschedule", "cancel"]
    fingerprint: str
    summary: str  # natural-language read-back, e.g. "book Dr. Ahmed tomorrow at 11 AM under Ali"
    created_at: datetime
    expires_at: datetime
    slot_id: UUID | None = None
    appt_id: UUID | None = None
    appt_version: int | None = None


class CompletedAction(BaseModel):
    type: Literal["book", "reschedule", "cancel"]
    executed: bool
    booking_reference: str | None = None
    appointment: AppointmentSnapshot | None = None
    message: str = ""  # e.g. "Booked. Your reference is CL-482913."


# ---------- conversation context ----------

class OfferedSlot(BaseModel):
    """An alternative slot shown to the caller, numbered starting at 1."""

    idx: int
    slot_id: UUID
    label: str


class Entities(BaseModel):
    """Filled fields for the current flow. Missing = None."""

    doctor_id: UUID | None = None
    doctor_name: str | None = None
    date: str | None = None            # ISO YYYY-MM-DD in clinic timezone
    time: str | None = None            # "HH:MM" 24h
    time_period: Literal["morning", "afternoon"] | None = None
    patient_name: str | None = None
    booking_reference: str | None = None
    verify_name: str | None = None
    new_date: str | None = None
    new_time: str | None = None
    selected_slot_id: UUID | None = None


class Context(BaseModel):
    """Everything the engine needs to resume a turn. Persisted as JSONB."""

    model_config = ConfigDict(extra="forbid")

    flow: Flow | None = None
    entities: Entities = Field(default_factory=Entities)
    offered_slots: list[OfferedSlot] = Field(default_factory=list)
    pending_action: PendingAction | None = None
    appointment_snapshot: AppointmentSnapshot | None = None
    retries: dict[str, int] = Field(default_factory=dict)
    last_error: str | None = None
    last_completed_fingerprint: str | None = None


# ---------- parser output ----------

class ParsedTurn(BaseModel):
    """What the parser thinks the caller just said. Pure data; no DB, no state."""

    intent: Intent
    entities: Entities = Field(default_factory=Entities)
    selected_option_idx: int | None = None
    text: str = ""
    stt_confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    is_correction: bool = False
    unsupported_reason: str | None = None  # e.g. "medical", "billing"


# ---------- API request / response ----------

class TurnRequest(BaseModel):
    turn_id: UUID
    text: str = Field(min_length=0, max_length=500)
    input_mode: Literal["voice", "text"] = "text"
    stt_confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class ConversationEvent(BaseModel):
    seq: int
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)
    executed: bool | None = None


class TurnResponse(BaseModel):
    conversation_id: UUID
    turn_no: int
    assistant_text: str
    state: State
    intent: Intent | None = None
    flow: Flow | None = None
    entities: Entities
    missing_fields: list[str] = Field(default_factory=list)
    offered_slots: list[OfferedSlot] = Field(default_factory=list)
    pending_action: PendingAction | None = None
    completed_action: CompletedAction | None = None
    events: list[ConversationEvent] = Field(default_factory=list)
    error: dict[str, Any] | None = None


class ConversationCreated(BaseModel):
    conversation_id: UUID
    state: State = State.IDLE
    assistant_text: str


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    db: Literal["ok", "down", "not_configured"]