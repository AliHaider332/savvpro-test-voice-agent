"""Frozen vocabulary of the decision engine.

Every module imports these — do not rename values without also updating
schema.sql CHECK constraints and the frontend types.
"""
from __future__ import annotations

from enum import Enum


class State(str, Enum):
    """The five persisted conversation states.

    CHECKING_AVAILABILITY / EXECUTING_ACTION / RECOVERABLE_ERROR are *events*,
    not stored states. An error leaves state unchanged and sets last_error.
    """

    IDLE = "IDLE"
    COLLECTING = "COLLECTING"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    DISAMBIGUATING = "DISAMBIGUATING"
    COMPLETED = "COMPLETED"


class Flow(str, Enum):
    """Which transaction the caller is currently in. None/absent = no flow."""

    BOOK = "BOOK"
    RESCHEDULE = "RESCHEDULE"
    CANCEL = "CANCEL"


class Intent(str, Enum):
    """Parser output. CHANGE_INTENT is derived by the engine, never parsed."""

    BOOK_APPOINTMENT = "BOOK_APPOINTMENT"
    RESCHEDULE_APPOINTMENT = "RESCHEDULE_APPOINTMENT"
    CANCEL_APPOINTMENT = "CANCEL_APPOINTMENT"
    CONFIRM = "CONFIRM"
    DECLINE = "DECLINE"
    CORRECT_INFORMATION = "CORRECT_INFORMATION"
    CANCEL_CONVERSATION = "CANCEL_CONVERSATION"
    SELECT_OPTION = "SELECT_OPTION"
    PROVIDE_INFORMATION = "PROVIDE_INFORMATION"
    UNSUPPORTED = "UNSUPPORTED"
    GREETING_GOODBYE = "GREETING_GOODBYE"
    UNKNOWN = "UNKNOWN"


class EventKind(str, Enum):
    """Audit-trace events. Add new kinds here, not ad-hoc strings."""

    TURN_RECEIVED = "TURN_RECEIVED"
    PARSED = "PARSED"
    STATE_TRANSITION = "STATE_TRANSITION"
    ENTITY_EXTRACTED = "ENTITY_EXTRACTED"
    ENTITY_UPDATED = "ENTITY_UPDATED"
    MISSING_FIELD_ASKED = "MISSING_FIELD_ASKED"
    AVAILABILITY_CHECK = "AVAILABILITY_CHECK"
    SLOT_TAKEN = "SLOT_TAKEN"
    SLOT_OFFERED = "SLOT_OFFERED"
    CONFIRMATION_PROPOSED = "CONFIRMATION_PROPOSED"
    CONFIRMATION_ACCEPTED = "CONFIRMATION_ACCEPTED"
    CONFIRMATION_DECLINED = "CONFIRMATION_DECLINED"
    STALE_PENDING_CLEARED = "STALE_PENDING_CLEARED"
    INTENT_SWITCH = "INTENT_SWITCH"
    AMBIGUITY_DETECTED = "AMBIGUITY_DETECTED"
    REFUSED = "REFUSED"
    DB_WRITE = "DB_WRITE"
    ERROR = "ERROR"