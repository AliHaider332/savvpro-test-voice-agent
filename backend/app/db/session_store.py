"""Conversation session persistence: one locked, atomic unit of work per turn."""
from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Engine as SaEngine
from sqlalchemy.exc import OperationalError

from app.brain.engine import Session
from app.brain.states import State
from app.schemas import Context, TurnResponse

LOCK_TIMEOUT_SECONDS = 5


class SessionNotFound(Exception):
    pass


class TurnInProgress(Exception):
    """Another request holds this session's lock (HTTP 409)."""


class TurnTx:
    """Handed out by `turn()`. Call save() once to commit this turn's result."""

    def __init__(self, session: Session, last_turn_id: UUID | None,
                 last_response: dict[str, Any] | None):
        self.session = session
        self.last_turn_id = last_turn_id
        self.last_response = last_response
        self._saved: tuple[Session, UUID, TurnResponse] | None = None

    def save(self, session: Session, turn_id: UUID, response: TurnResponse) -> None:
        self._saved = (session, turn_id, response)


@dataclass(frozen=True)
class SessionSnapshot:
    """Read-only copy of a session. Taken without the turn lock, so it may lag a turn
    that is being processed right now; it never shows a half-finished turn."""
    session: Session
    last_turn_id: UUID | None
    last_response: dict[str, Any] | None


class SessionStore(Protocol):
    def create(self, session: Session) -> None: ...
    def read(self, session_id: UUID) -> SessionSnapshot:
        """Non-locking read. Raises SessionNotFound."""
        ...
    def turn(self, session_id: UUID):
        """Context manager yielding a TurnTx with the session locked.
        Raises SessionNotFound / TurnInProgress. Nothing persists unless save()
        was called and the block exited without an exception."""
        ...


# --------------------------------------------------------------------- memory
class MemorySessionStore:
    def __init__(self, lock_timeout: float = LOCK_TIMEOUT_SECONDS):
        self._rows: dict[UUID, dict[str, Any]] = {}
        self._locks: dict[UUID, threading.Lock] = {}
        self._events: dict[UUID, list[dict[str, Any]]] = {}
        self._lock_timeout = lock_timeout

    def create(self, session: Session) -> None:
        self._rows[session.id] = {"session": session.model_dump_json(),
                                  "last_turn_id": None, "last_response": None}
        self._locks[session.id] = threading.Lock()
        self._events[session.id] = []

    def events(self, session_id: UUID) -> list[dict[str, Any]]:
        return list(self._events[session_id])

    def read(self, session_id: UUID) -> SessionSnapshot:
        row = self._rows.get(session_id)                # whole-row replacement: never torn
        if row is None:
            raise SessionNotFound()
        return SessionSnapshot(Session.model_validate_json(row["session"]),
                               row["last_turn_id"], row["last_response"])

    @contextmanager
    def turn(self, session_id: UUID) -> Iterator[TurnTx]:
        row = self._rows.get(session_id)
        if row is None:
            raise SessionNotFound()
        lock = self._locks[session_id]
        if not lock.acquire(timeout=self._lock_timeout):
            raise TurnInProgress()
        try:
            row = self._rows[session_id]               # re-read under the lock
            tx = TurnTx(Session.model_validate_json(row["session"]),
                        row["last_turn_id"], row["last_response"])
            yield tx
            if tx._saved:                              # reached only if the block succeeded
                session, turn_id, response = tx._saved
                self._rows[session_id] = {
                    "session": session.model_dump_json(),
                    "last_turn_id": turn_id,
                    "last_response": response.model_dump(mode="json")}
                self._events[session_id] += [e.model_dump(mode="json") | {"turn_no": session.turn_no}
                                             for e in response.events]
        finally:
            lock.release()


# ------------------------------------------------------------------- postgres
class SqlSessionStore:
    def __init__(self, engine: SaEngine):
        self._engine = engine

    def create(self, session: Session) -> None:
        with self._engine.begin() as conn:
            conn.execute(text(
                "insert into conversation_sessions "
                "(id, state, flow, context, turn_no) "
                "values (:id, :state, :flow, cast(:ctx as jsonb), :turn_no)"),
                self._params(session))

    def read(self, session_id: UUID) -> SessionSnapshot:
        with self._engine.connect() as conn:            # plain SELECT: never waits on a turn lock
            row = conn.execute(text(
                "select state, context, turn_no, last_turn_id, last_response "
                "from conversation_sessions where id = :id"),
                {"id": session_id}).mappings().first()
        if row is None:
            raise SessionNotFound()
        session = Session(id=session_id, state=State(row["state"]),
                          context=Context.model_validate(row["context"]),
                          turn_no=row["turn_no"])
        return SessionSnapshot(session, row["last_turn_id"], row["last_response"])

    @staticmethod
    def _params(session: Session) -> dict[str, Any]:
        ctx = session.context
        return {"id": session.id, "state": session.state.value,
                "flow": ctx.flow.value if ctx.flow else None,
                "ctx": ctx.model_dump_json(), "turn_no": session.turn_no}

    @contextmanager
    def turn(self, session_id: UUID) -> Iterator[TurnTx]:
        with self._engine.begin() as conn:
            conn.execute(text(f"set local lock_timeout = '{LOCK_TIMEOUT_SECONDS}s'"))
            try:
                row = conn.execute(text(
                    "select state, context, turn_no, last_turn_id, last_response "
                    "from conversation_sessions where id = :id for update"),
                    {"id": session_id}).mappings().first()
            except OperationalError as err:
                if "lock" in str(err).lower():
                    raise TurnInProgress() from None
                raise
            if row is None:
                raise SessionNotFound()
            session = Session(id=session_id, state=State(row["state"]),
                              context=Context.model_validate(row["context"]),
                              turn_no=row["turn_no"])
            tx = TurnTx(session, row["last_turn_id"], row["last_response"])
            yield tx                                   # an exception here rolls everything back
            if tx._saved:
                saved, turn_id, response = tx._saved
                conn.execute(text(
                    "update conversation_sessions set state = :state, flow = :flow, "
                    "context = cast(:ctx as jsonb), turn_no = :turn_no, "
                    "version = version + 1, last_turn_id = :turn, "
                    "last_response = cast(:resp as jsonb), updated_at = now() "
                    "where id = :id"),
                    self._params(saved) | {"turn": turn_id,
                                           "resp": response.model_dump_json()})
                if response.events:
                    conn.execute(text(
                        "insert into conversation_events "
                        "(session_id, turn_no, seq, kind, payload, executed) "
                        "values (:sid, :turn_no, :seq, :kind, cast(:payload as jsonb), :executed)"),
                        [{"sid": session_id, "turn_no": saved.turn_no, "seq": e.seq,
                          "kind": e.kind.value,
                          "payload": json.dumps(e.model_dump(mode="json")["payload"]),
                          "executed": e.executed} for e in response.events])