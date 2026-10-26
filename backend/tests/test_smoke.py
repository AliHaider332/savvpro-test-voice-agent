"""Task 1 acceptance: app boots, /health responds, contract types load."""
from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from app.brain.states import EventKind, Flow, Intent, State
from app.main import app
from app.schemas import (
    Context,
    Entities,
    ParsedTurn,
    PendingAction,
    TurnRequest,
)


client = TestClient(app)


def test_health_returns_ok_when_db_not_configured(monkeypatch):
    # Ensure the test is independent of any developer .env
    from app.config import get_settings
    get_settings.cache_clear()
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "")

    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["db"] in {"not_configured", "ok"}

    get_settings.cache_clear()


def test_enum_vocabulary_is_complete():
    # These names are referenced everywhere. Fail loudly if any is dropped.
    assert {s.value for s in State} == {
        "IDLE", "COLLECTING", "AWAITING_CONFIRMATION",
        "DISAMBIGUATING", "COMPLETED",
    }
    assert {f.value for f in Flow} == {"BOOK", "RESCHEDULE", "CANCEL"}
    assert Intent.CONFIRM.value == "CONFIRM"
    assert Intent.DECLINE.value == "DECLINE"
    assert EventKind.DB_WRITE.value == "DB_WRITE"


def test_context_defaults_are_safe():
    ctx = Context()
    assert ctx.flow is None
    assert ctx.entities == Entities()
    assert ctx.offered_slots == []
    assert ctx.pending_action is None
    # extra="forbid" guards against silent typo drift between layers
    with pytest.raises(ValidationError):
        Context.model_validate({"flow": "BOOK", "unknown_typo": 1})


def test_turn_request_validation():
    req = TurnRequest(turn_id=uuid4(), text="book dr ahmed tomorrow", input_mode="text")
    assert req.stt_confidence is None

    with pytest.raises(ValidationError):
        TurnRequest(turn_id=uuid4(), text="x" * 501)  # over max_length


def test_pending_action_round_trip():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(tz=timezone.utc)
    p = PendingAction(
        action_id=uuid4(),
        type="book",
        fingerprint="a" * 64,
        summary="book Dr. Ahmed tomorrow at 11 AM under Ali",
        created_at=now,
        expires_at=now + timedelta(minutes=10),
    )
    assert p.expires_at > p.created_at
    assert p.slot_id is None and p.appt_id is None


def test_parsed_turn_confidence_bounds():
    base = ParsedTurn(intent=Intent.UNKNOWN)
    assert base.stt_confidence == 1.0
    with pytest.raises(ValidationError):
        ParsedTurn(intent=Intent.UNKNOWN, stt_confidence=1.5)