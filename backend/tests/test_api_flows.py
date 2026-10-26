"""Task 4: end-to-end API flows on a memory-backed container (no database).

Seeded demo data (clock = Mon 2026-10-26 08:00 Asia/Karachi):
  CL-100001 Test Patient / Dr. Ahmed  Tue 27 Oct 11:00
  CL-100002 Maria Test   / Dr. Khan   Wed 28 Oct 14:00
  CL-100003 Omar Test    / Dr. Malik  Thu 29 Oct 10:00
"""
import threading
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.api.deps import build_memory_container, get_container, get_db_probe
from app.db.session_store import MemorySessionStore
from app.main import create_app
from app.services.conversation import ConversationService

TZ = ZoneInfo("Asia/Karachi")
BOOK_ALI = "Book Dr. Khan tomorrow at 11 under Ali"
BOOK_SARA = "Book Dr. Khan tomorrow at 11 under Sara"
ERROR_KEYS = {"code", "message", "details"}


def make_app(container):
    app = create_app()
    app.dependency_overrides[get_container] = lambda: container
    app.dependency_overrides[get_db_probe] = lambda: container.db_probe
    return app


@pytest.fixture
def container(repo, clock):
    return build_memory_container(repo, TZ, clock, MemorySessionStore(lock_timeout=0.3))


@pytest.fixture
def client(container):
    return TestClient(make_app(container))


def new_conversation(client) -> str:
    r = client.post("/api/conversations")
    assert r.status_code == 201
    return r.json()["conversation_id"]


def turn(client, cid, text, turn_id=None, **extra):
    return client.post(f"/api/conversations/{cid}/turns",
                       json={"turn_id": str(turn_id or uuid4()), "text": text, **extra})


def say(client, cid, text, **extra) -> dict:
    r = turn(client, cid, text, **extra)
    assert r.status_code == 200, r.text
    return r.json()


def free_times(client, doctor, day) -> list[str]:
    r = client.get("/api/availability", params={"doctor": doctor, "date": day})
    assert r.status_code == 200
    return [s["time"] for s in r.json()["slots"]]


def lookup(client, ref, name):
    return client.get(f"/api/appointments/{ref}", params={"name": name})


def db_writes(body) -> list[dict]:
    return [e for e in body["events"] if e["kind"] == "DB_WRITE" and e["executed"] is True]


# --------------------------------------------------------------------- booking
def test_booking_is_visible_in_availability_and_lookup(client):
    assert "11:00" in free_times(client, "khan", "2026-10-27")
    cid = new_conversation(client)
    say(client, cid, BOOK_ALI)
    assert "11:00" in free_times(client, "khan", "2026-10-27")      # proposal reserves nothing
    done = say(client, cid, "yes")
    assert done["state"] == "COMPLETED"
    assert "11:00" not in free_times(client, "khan", "2026-10-27")
    ref = done["completed_action"]["booking_reference"]
    found = lookup(client, ref, "Ali")
    assert found.status_code == 200 and found.json()["starts_at"].startswith("2026-10-27T11:00")


def test_only_the_confirming_turn_reports_an_executed_write(client):
    cid = new_conversation(client)
    proposal = say(client, cid, BOOK_ALI)
    assert db_writes(proposal) == []
    yes_id = uuid4()
    done = say(client, cid, "yes", turn_id=yes_id)
    assert len(db_writes(done)) == 1
    replay = say(client, cid, "yes", turn_id=yes_id)
    assert replay == done                                           # same trace, no second write


def test_two_callers_cannot_book_the_same_slot(client, repo):
    a, b = new_conversation(client), new_conversation(client)
    say(client, a, BOOK_ALI)
    say(client, b, BOOK_SARA)
    first = say(client, a, "yes")
    second = say(client, b, "yes")
    assert first["state"] == "COMPLETED"
    assert second["state"] == "COLLECTING" and second["error"] == "SLOT_TAKEN"
    assert second["completed_action"] is None and second["offered_slots"]
    assert db_writes(second) == []
    assert lookup(client, first["completed_action"]["booking_reference"], "Ali").status_code == 200
    assert [a.patient_name for a in repo.all_appointments()
            if a.patient_name in ("Ali", "Sara")] == ["Ali"]         # Sara got nothing


def test_confirmation_before_any_proposal_books_nothing(client, repo):
    cid = new_conversation(client)
    body = say(client, cid, "yes")
    assert body["completed_action"] is None and body["state"] == "IDLE"
    assert repo.create_calls == 0


def test_low_confidence_confirmation_books_nothing(client, repo):
    cid = new_conversation(client)
    say(client, cid, BOOK_ALI)
    body = say(client, cid, "yes", stt_confidence=0.1)
    assert body["state"] == "AWAITING_CONFIRMATION" and body["completed_action"] is None
    assert repo.create_calls == 0 and "11:00" in free_times(client, "khan", "2026-10-27")


# ------------------------------------------------------------ reschedule/cancel
def test_reschedule_over_http_moves_the_appointment(client):
    assert "14:00" not in free_times(client, "khan", "2026-10-28")
    assert "15:00" in free_times(client, "khan", "2026-10-29")
    cid = new_conversation(client)
    proposal = say(client, cid, "Reschedule CL-100002 under Maria to Thursday at 3 pm")
    assert proposal["state"] == "AWAITING_CONFIRMATION" and db_writes(proposal) == []
    assert lookup(client, "CL-100002", "Maria").json()["starts_at"].startswith("2026-10-28T14:00")
    done = say(client, cid, "yes")
    assert done["state"] == "COMPLETED" and len(db_writes(done)) == 1
    moved = lookup(client, "CL-100002", "Maria").json()
    assert moved["starts_at"].startswith("2026-10-29T15:00") and moved["status"] == "confirmed"
    assert "14:00" in free_times(client, "khan", "2026-10-28")      # old slot released
    assert "15:00" not in free_times(client, "khan", "2026-10-29")  # new slot taken


def test_cancel_over_http_and_a_repeated_yes_cancels_once(client, repo):
    assert "10:00" not in free_times(client, "malik", "2026-10-29")
    cid = new_conversation(client)
    proposal = say(client, cid, "Cancel CL-100003 under Omar")
    assert proposal["state"] == "AWAITING_CONFIRMATION" and db_writes(proposal) == []
    assert lookup(client, "CL-100003", "Omar").json()["status"] == "confirmed"
    yes_id = uuid4()
    done = say(client, cid, "yes", turn_id=yes_id)
    assert done["state"] == "COMPLETED"
    assert say(client, cid, "yes", turn_id=yes_id) == done
    assert repo.cancel_calls == 1
    assert lookup(client, "CL-100003", "Omar").json()["status"] == "cancelled"
    assert "10:00" in free_times(client, "malik", "2026-10-29")     # slot released


def test_cancel_with_the_wrong_name_changes_nothing(client, repo):
    cid = new_conversation(client)
    body = say(client, cid, "Cancel CL-100003 under Somebody Else")
    assert body["state"] != "AWAITING_CONFIRMATION" and body["pending_action"] is None
    assert repo.cancel_calls == 0
    assert lookup(client, "CL-100003", "Omar").json()["status"] == "confirmed"


# ---------------------------------------------------------------- idempotency
def test_replay_ignores_changed_text_and_returns_the_original(client, repo):
    cid = new_conversation(client)
    say(client, cid, BOOK_ALI)
    yes_id = uuid4()
    done = say(client, cid, "yes", turn_id=yes_id)
    again = say(client, cid, "no thanks, cancel that", turn_id=yes_id)
    assert again == done and repo.create_calls == 1


def test_simultaneous_http_requests_with_one_turn_id_book_once(repo, clock):
    container = build_memory_container(repo, TZ, clock, MemorySessionStore(lock_timeout=5))
    app = make_app(container)
    setup = TestClient(app)
    cid = new_conversation(setup)
    say(setup, cid, BOOK_ALI)
    yes_id, barrier, results = uuid4(), threading.Barrier(2), []

    def worker():
        c = TestClient(app)
        barrier.wait()
        results.append(turn(c, cid, "yes", turn_id=yes_id))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert [r.status_code for r in results] == [200, 200]
    assert results[0].json() == results[1].json()
    assert repo.create_calls == 1


# ---------------------------------------------------------------- error format
@pytest.mark.parametrize("method,url,kwargs,status", [
    ("get", "/api/nope", {}, 404),
    ("post", "/api/doctors", {}, 405),                                       # wrong method
    ("get", f"/api/conversations/{uuid4()}", {}, 404),
    ("get", "/api/conversations/not-a-uuid", {}, 422),
    ("post", f"/api/conversations/{uuid4()}/turns", {"json": {}}, 422),
    ("get", "/api/availability", {"params": {"doctor": "khan"}}, 422),
    ("get", "/api/availability", {"params": {"doctor": "zzz", "date": "2026-10-27"}}, 404),
    ("get", "/api/appointments/CL-100002", {}, 422),
    ("get", "/api/appointments/CL-100002", {"params": {"name": "Nobody"}}, 404),
])
def test_every_error_has_the_same_json_shape(client, method, url, kwargs, status):
    r = getattr(client, method)(url, **kwargs)
    assert r.status_code == status, r.text
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert set(body) == {"error"} and set(body["error"]) == ERROR_KEYS
    assert body["error"]["code"] == body["error"]["code"].upper()   # codes are UPPER_SNAKE
    assert "Traceback" not in r.text


def test_malformed_json_body_is_a_422_in_the_standard_shape(client):
    cid = new_conversation(client)
    r = client.post(f"/api/conversations/{cid}/turns", content="{not json",
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_unexpected_exception_is_a_500_without_internals_and_leaves_the_session_intact(
        container):
    class Boom:
        def handle_turn(self, *a, **k):
            raise RuntimeError("secret detail about patient Ali CL-123456")

    client = TestClient(make_app(container), raise_server_exceptions=False)
    cid = new_conversation(client)
    healthy = container.conversations
    container.conversations = ConversationService(container.store, Boom())
    turn_id = uuid4()
    r = turn(client, cid, "hello", turn_id=turn_id)
    assert r.status_code == 500
    assert r.json() == {"error": {"code": "INTERNAL_ERROR", "message": "Something went wrong.",
                                  "details": None}}
    assert "secret" not in r.text and "Ali" not in r.text
    assert client.get(f"/api/conversations/{cid}").json()["turn_no"] == 0
    container.conversations = healthy
    assert turn(client, cid, "hello", turn_id=turn_id).status_code == 200


def test_conversations_are_isolated_from_each_other(client):
    a, b = new_conversation(client), new_conversation(client)
    say(client, a, BOOK_ALI)
    assert UUID(a) != UUID(b)
    assert client.get(f"/api/conversations/{b}").json()["turn_no"] == 0   # b is untouched by a