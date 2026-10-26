"""ConversationService: one locked, idempotent, atomic unit of work per turn.

Responsibilities (and nothing more):
  * lock the session row before reading anything (SessionStore.turn),
  * replay the cached response when the same turn_id is retried,
  * otherwise run the deterministic Engine exactly once,
  * persist session state + cached response + events together via TurnTx.save.

It never writes appointments itself: every write happens inside Engine._execute,
behind authorize(). Errors are raised as domain exceptions; mapping them to HTTP
status codes is the API layer's job (Task 3).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

from app.brain import responses as R
from app.brain.engine import CONFIRMATION_TTL, Engine, Session
from app.db.session_store import SessionNotFound, SessionStore, TurnInProgress
from app.schemas import (
    ConversationView, CreateConversationResponse, PendingActionView, TurnRequest, TurnResponse,
)

__all__ = [
    "ConversationService", "StorageUnavailable", "TurnResult",
    "SessionNotFound", "TurnInProgress",
]

log = logging.getLogger("cliniccall.conversation")


class StorageUnavailable(Exception):
    """The database could not be reached or dropped the connection mid-turn (HTTP 503).

    Nothing from the turn was persisted. Retrying with the SAME turn_id is safe:
    a booking that did commit is found again through its idempotency key."""


@dataclass(frozen=True)
class TurnResult:
    response: TurnResponse
    duplicate: bool = False        # True when the cached response of a retried turn_id was replayed


def _is_connection_problem(err: DBAPIError) -> bool:
    return isinstance(err, (OperationalError, InterfaceError)) or bool(err.connection_invalidated)


class ConversationService:
    def __init__(self, store: SessionStore, engine: Engine):
        self._store = store
        self._engine = engine

    # ------------------------------------------------------------------ create
    def create_conversation(self) -> CreateConversationResponse:
        session = Session()
        try:
            self._store.create(session)
        except DBAPIError as err:
            if _is_connection_problem(err):
                log.error("conversation create failed: storage unavailable (%s)", type(err).__name__)
                raise StorageUnavailable() from None
            raise
        log.info("conversation created id=%s", session.id)
        return CreateConversationResponse(
            conversation_id=session.id, state=session.state, assistant_text=R.greeting())

    # -------------------------------------------------------------------- read
    def get_conversation(self, conversation_id: UUID) -> ConversationView:
        """Current state of a conversation, without taking the turn lock."""
        try:
            snap = self._store.read(conversation_id)
        except DBAPIError as err:
            if _is_connection_problem(err):
                log.error("read failed: storage unavailable conversation=%s (%s)",
                          conversation_id, type(err).__name__)
                raise StorageUnavailable() from None
            raise
        ctx, pa = snap.session.context, snap.session.context.pending_action
        return ConversationView(
            conversation_id=snap.session.id, state=snap.session.state, flow=ctx.flow,
            turn_no=snap.session.turn_no, entities=ctx.entities,
            pending_action=(PendingActionView(summary=pa.summary,
                                              expires_at=pa.created_at + CONFIRMATION_TTL)
                            if pa else None),
            last_response=(TurnResponse.model_validate(snap.last_response)
                           if snap.last_response else None))

    # -------------------------------------------------------------------- turn
    def handle_turn(self, conversation_id: UUID, request: TurnRequest) -> TurnResult:
        """Process one caller turn.

        Raises SessionNotFound (404), TurnInProgress (409), StorageUnavailable (503).
        Any other exception propagates (500) after the whole turn was rolled back.
        """
        try:
            with self._store.turn(conversation_id) as tx:       # session is locked from here
                if tx.last_turn_id == request.turn_id and tx.last_response is not None:
                    # Retry of the turn that was already processed: replay it. The engine is
                    # NOT run, nothing is saved, and no appointment write can happen.
                    cached = TurnResponse.model_validate(tx.last_response)
                    log.info("duplicate turn replayed conversation=%s turn_no=%s",
                             conversation_id, cached.turn_no)
                    return TurnResult(cached, duplicate=True)

                response = self._engine.handle_turn(
                    tx.session, request.text, stt_confidence=request.stt_confidence)
                # state + cached response + events are committed together when the block exits
                tx.save(tx.session, request.turn_id, response)
        except (SessionNotFound, TurnInProgress):
            raise
        except DBAPIError as err:
            if _is_connection_problem(err):
                log.error("turn failed: storage unavailable conversation=%s (%s)",
                          conversation_id, type(err).__name__)
                raise StorageUnavailable() from None
            log.error("turn failed conversation=%s (%s)", conversation_id, type(err).__name__)
            raise
        except Exception as err:
            # Class name only: exception text may echo user input (names, references).
            log.error("turn failed conversation=%s (%s)", conversation_id, type(err).__name__)
            raise

        log.info("turn processed conversation=%s turn_no=%s state=%s intent=%s",
                 conversation_id, response.turn_no, response.state.value, response.intent.value)
        return TurnResult(response, duplicate=False)