"""Dependency wiring. Routes depend on `get_container`; tests override it with a
memory-backed container (see build_memory_container)."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.brain.engine import Clock, Engine
from app.config import get_settings
from app.db.repository import Repository
from app.db.session_store import MemorySessionStore, SessionStore, SqlSessionStore
from app.db.sql_repo import SqlRepository, make_engine
from app.services.appointments import AppointmentService
from app.services.conversation import ConversationService, StorageUnavailable


@dataclass
class Container:
    conversations: ConversationService
    appointments: AppointmentService
    store: SessionStore
    clock: Clock
    db_probe: Callable[[], str]            # "ok" | "unavailable"


def build_memory_container(repo: Repository, tz: ZoneInfo, clock: Clock,
                           store: SessionStore | None = None) -> Container:
    store = store or MemorySessionStore()
    appointments = AppointmentService(repo, tz)
    return Container(ConversationService(store, Engine(appointments, clock)),
                     appointments, store, clock, lambda: "ok")


@lru_cache
def _sql_container() -> Container:
    settings = get_settings()
    tz = ZoneInfo(settings.clinic_timezone)
    sa_engine = make_engine(settings.database_url)          # connects lazily
    appointments = AppointmentService(SqlRepository(sa_engine, tz), tz)
    store = SqlSessionStore(sa_engine)

    def clock() -> datetime:
        return datetime.now(tz)

    def probe() -> str:
        try:
            with sa_engine.connect() as conn:
                conn.execute(text("select 1"))
            return "ok"
        except Exception:                                    # noqa: BLE001 - any failure = down
            return "unavailable"

    return Container(ConversationService(store, Engine(appointments, clock)),
                     appointments, store, clock, probe)


def get_container() -> Container:
    if not get_settings().database_url:
        raise StorageUnavailable()                           # -> 503 STORAGE_UNAVAILABLE
    return _sql_container()


def get_db_probe() -> Callable[[], str]:
    if not get_settings().database_url:
        return lambda: "not_configured"
    return _sql_container().db_probe