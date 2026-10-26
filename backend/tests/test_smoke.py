from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.brain.states import EventKind, Flow, Intent, State
from app.main import app
from app.schemas import Context, TurnRequest

client = TestClient(app)


def test_health_ok():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_unknown_route_uses_error_format():
    r = client.get("/nope")
    assert r.status_code == 404
    assert set(r.json()["error"]) == {"code", "message", "details"}


def test_state_set_is_exactly_five():
    assert {s.value for s in State} == {
        "IDLE", "COLLECTING", "AWAITING_CONFIRMATION", "DISAMBIGUATING", "COMPLETED"
    }


def test_core_intents_and_flows_exist():
    for name in ["BOOK_APPOINTMENT", "RESCHEDULE_APPOINTMENT", "CANCEL_APPOINTMENT",
                 "CONFIRM", "DECLINE", "CORRECT_INFORMATION", "CANCEL_CONVERSATION", "UNKNOWN"]:
        assert Intent(name)
    assert {f.value for f in Flow} == {"BOOK", "RESCHEDULE", "CANCEL"}
    assert EventKind.DB_WRITE.value == "DB_WRITE"


def test_context_defaults_are_empty():
    ctx = Context()
    assert ctx.flow is None
    assert ctx.pending_action is None
    assert ctx.entities.date is None
    assert ctx.offered_slots == []


def test_context_round_trips_through_json():
    ctx = Context(flow=Flow.BOOK)
    assert Context.model_validate_json(ctx.model_dump_json()) == ctx


def test_turn_request_accepts_empty_text():
    assert TurnRequest(turn_id=uuid4(), text="").text == ""


def test_turn_request_rejects_overlong_text_and_bad_confidence():
    with pytest.raises(ValidationError):
        TurnRequest(turn_id=uuid4(), text="x" * 501)
    with pytest.raises(ValidationError):
        TurnRequest(turn_id=uuid4(), text="hi", stt_confidence=1.5)


def test_turn_request_rejects_non_uuid_turn_id():
    with pytest.raises(ValidationError):
        TurnRequest(turn_id="not-a-uuid", text="hi")