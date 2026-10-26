"""HTTP layer on a memory-backed container (no database)."""
import threading
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.api.deps import build_memory_container, get_container, get_db_probe
from app.config import get_settings
from app.db.session_store import MemorySessionStore
from app.main import create_app
from app.services.conversation import ConversationService

TZ = ZoneInfo("Asia/Karachi")
BOOK = "Book Dr. Khan tomorrow at 11 under Ali"
ERROR_KEYS = {"code", "message", "details"}


@pytest.fixture
def container(repo, clock):
    return build_memory_container(repo, TZ, clock, MemorySessionStore(lock_timeout=0.3))


@pytest.fixture
def client(container):
    app = create_app()
    app.dependency_overrides[get_container] = lambda: container
    app.dependency_overrides[get_db_probe] = lambda: container.db_probe
    return TestClient(app)


def new_conversation(client) -> str:
    r = client.post("/api/conversations")
    assert r.status_code == 201
    return r.json()["conversation_id"]


def turn(client, cid, text, turn_id=None, **extra):
    return client.post(f"/api/conversations/{cid}/turns",
                       json={"turn_id": str(turn_id or uuid4()), "text": text, **extra})


def assert_error(r, status, code):
    assert r.status_code == status, r.text
    body = r.json()
    assert set(body) == {"error"} and set(body["error"]) == ERROR_KEYS
    assert body["error"]["code"] == code


# ---------------------------------------------------------------------- health
def test_health_reports_database_status(client):
    assert client.get("/health").json() == {"status": "ok", "db": "ok"}


# ---------------------------------------------------------------- conversations
def test_create_conversation(client):
    r = client.post("/api/conversations")
    assert r.status_code == 201
    body = r.json()
    assert body["state"] == "IDLE" and body["assistant_text"] and body["conversation_id"]


def test_book_over_http_and_repeat_the_confirmation(client, repo):
    cid = new_conversation(client)
    r = turn(client, cid, BOOK)
    assert r.status_code == 200 and r.json()["state"] == "AWAITING_CONFIRMATION"
    assert r.json()["pending_action"]["status"] == "proposed" and repo.create_calls == 0

    yes_id = uuid4()
    done = turn(client, cid, "yes", turn_id=yes_id)
    assert done.status_code == 200 and done.json()["state"] == "COMPLETED"
    assert done.json()["completed_action"]["executed"] is True
    again = turn(client, cid, "yes", turn_id=yes_id)
    assert again.status_code == 200 and again.json() == done.json()
    assert repo.create_calls == 1


def test_get_conversation_reflects_the_last_turn(client):
    cid = new_conversation(client)
    first = turn(client, cid, BOOK).json()
    view = client.get(f"/api/conversations/{cid}").json()
    assert view["conversation_id"] == cid and view["state"] == "AWAITING_CONFIRMATION"
    assert view["turn_no"] == 1 and view["last_response"] == first
    assert view["pending_action"]["summary"] == first["pending_action"]["summary"]


def test_unknown_conversation_is_404_on_get_and_post(client):
    missing = uuid4()
    assert_error(client.get(f"/api/conversations/{missing}"), 404, "CONVERSATION_NOT_FOUND")
    assert_error(turn(client, missing, "hello"), 404, "CONVERSATION_NOT_FOUND")


@pytest.mark.parametrize("payload", [
    {},                                                        # no turn_id
    {"turn_id": "not-a-uuid", "text": "hi"},
    {"turn_id": str(uuid4()), "text": "x" * 501},
    {"turn_id": str(uuid4()), "text": "hi", "stt_confidence": 1.5},
    {"turn_id": str(uuid4()), "text": "hi", "input_mode": "fax"},
])
def test_invalid_turn_payloads_are_422(client, payload):
    cid = new_conversation(client)
    assert_error(client.post(f"/api/conversations/{cid}/turns", json=payload),
                 422, "VALIDATION_ERROR")


def test_malformed_conversation_id_is_422(client):
    assert_error(client.get("/api/conversations/not-a-uuid"), 422, "VALIDATION_ERROR")


def test_busy_conversation_is_409_with_retry_after(client, container):
    cid = new_conversation(client)
    from uuid import UUID
    holding, release = threading.Event(), threading.Event()

    def hold():
        with container.store.turn(UUID(cid)):
            holding.set()
            release.wait(timeout=5)

    t = threading.Thread(target=hold)
    t.start()
    holding.wait(timeout=2)
    try:
        r = turn(client, cid, "hello")
        assert_error(r, 409, "TURN_IN_PROGRESS")
        assert r.headers["retry-after"] == "1"
    finally:
        release.set()
        t.join()
    assert turn(client, cid, "hello").status_code == 200


def test_database_failure_is_503_and_the_turn_can_be_retried(client, container):
    cid = new_conversation(client)

    class Boom:
        def handle_turn(self, *a, **k):
            raise OperationalError("select", {}, Exception("connection refused"))

    healthy = container.conversations
    container.conversations = ConversationService(container.store, Boom())
    turn_id = uuid4()
    r = turn(client, cid, "hello", turn_id=turn_id)
    assert_error(r, 503, "STORAGE_UNAVAILABLE")
    assert r.headers["retry-after"] == "2"
    assert "connection refused" not in r.text                  # no internals leaked

    container.conversations = healthy
    assert turn(client, cid, "hello", turn_id=turn_id).status_code == 200


# --------------------------------------------------------------------- doctors
def test_doctors(client):
    r = client.get("/api/doctors")
    assert r.status_code == 200
    assert {d["slug"] for d in r.json()} == {"ahmed", "khan", "malik"}
    assert set(r.json()[0]) == {"slug", "display_name", "specialty"}


# ---------------------------------------------------------------- availability
def test_availability(client):
    r = client.get("/api/availability", params={"doctor": "ahmed", "date": "2026-10-27"})
    assert r.status_code == 200
    body = r.json()
    times = [s["time"] for s in body["slots"]]
    assert "11:00" not in times and "10:00" in times           # 11:00 is a seeded appointment
    assert body["doctor_slug"] == "ahmed" and body["timezone"] == "Asia/Karachi"


def test_availability_errors(client):
    assert_error(client.get("/api/availability", params={"doctor": "nobody", "date": "2026-10-27"}),
                 404, "DOCTOR_NOT_FOUND")
    assert_error(client.get("/api/availability", params={"doctor": "ahmed", "date": "tomorrow"}),
                 422, "VALIDATION_ERROR")
    assert_error(client.get("/api/availability", params={"doctor": "ahmed"}),
                 422, "VALIDATION_ERROR")


# --------------------------------------------------------------- appointments
def test_appointment_lookup_needs_reference_and_name(client):
    r = client.get("/api/appointments/CL-100002", params={"name": "Maria"})
    assert r.status_code == 200
    body = r.json()
    assert body["booking_reference"] == "CL-100002" and body["status"] == "confirmed"
    assert not ({"id", "slot_id", "version"} & set(body))


def test_appointment_lookup_failures_look_identical(client):
    wrong_name = client.get("/api/appointments/CL-100002", params={"name": "Someone Else"})
    unknown = client.get("/api/appointments/CL-999999", params={"name": "Maria"})
    assert_error(wrong_name, 404, "APPOINTMENT_NOT_FOUND")
    assert_error(unknown, 404, "APPOINTMENT_NOT_FOUND")
    assert wrong_name.json() == unknown.json()
    assert_error(client.get("/api/appointments/CL-100002"), 422, "VALIDATION_ERROR")


def test_booked_appointment_can_be_looked_up_after_the_conversation(client):
    cid = new_conversation(client)
    turn(client, cid, BOOK)
    ref = turn(client, cid, "yes").json()["completed_action"]["booking_reference"]
    r = client.get(f"/api/appointments/{ref}", params={"name": "Ali"})
    assert r.status_code == 200 and r.json()["doctor_slug"] == "khan"


# ------------------------------------------------------------ errors and CORS
def test_unknown_route_uses_the_error_format(client):
    assert_error(client.get("/api/nope"), 404, "NOT_FOUND")


def test_cors_allows_only_configured_origins(client):
    allowed = get_settings().origins_list[0]
    ok = client.options("/api/conversations", headers={
        "Origin": allowed, "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type"})
    assert ok.status_code == 200 and ok.headers["access-control-allow-origin"] == allowed
    bad = client.options("/api/conversations", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in bad.headers