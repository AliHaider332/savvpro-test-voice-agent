from datetime import datetime
from itertools import count
from zoneinfo import ZoneInfo

import pytest

from app.brain.engine import Engine, Session
from app.db.memory_repo import build_demo_repo
from app.services.appointments import AppointmentService

TZ = ZoneInfo("Asia/Karachi")
NOW = datetime(2026, 10, 26, 8, 0, tzinfo=TZ)     # Monday 08:00 clinic time


class FakeClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return FakeClock(NOW)


@pytest.fixture
def repo():
    numbers = count(482913)                         # first booking -> CL-482913
    return build_demo_repo(NOW, TZ, ref_factory=lambda: f"CL-{next(numbers)}")


@pytest.fixture
def service(repo):
    return AppointmentService(repo, TZ)


@pytest.fixture
def engine(service, clock):
    return Engine(service, clock)


@pytest.fixture
def session():
    return Session()


@pytest.fixture
def say(engine, session):
    def _say(text, conf=None):
        return engine.handle_turn(session, text, stt_confidence=conf)
    return _say