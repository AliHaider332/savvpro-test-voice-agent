"""Seed the Postgres database with fictional demo data.

    python -m app.db.seed --reset

DESTRUCTIVE: truncates all ClinicCall tables first. Fake data only.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.brain.datetime_parse import is_slot_time
from app.db.memory_repo import DOCTORS_SEED
from app.db.sql_repo import make_engine

DEMO_APPOINTMENTS = [
    (1, "ahmed", time(11, 0), "CL-100001", "Test Patient"),
    (2, "khan", time(14, 0), "CL-100002", "Maria Test"),
    (3, "malik", time(10, 0), "CL-100003", "Omar Test"),
]


def load_env_value(name: str) -> str | None:
    """Environment variable first, then backend/.env. Never prints the value."""
    if value := os.environ.get(name):
        return value
    env = Path(".env")
    if env.exists():
        m = re.search(rf"^{name}=(.+)$", env.read_text(encoding="utf-8"), re.M)
        if m:
            return m.group(1).strip().strip('"').strip("'")
    return None


def seed(engine: Engine, now: datetime, tz: ZoneInfo, days: int = 15) -> None:
    today = now.astimezone(tz).date()
    with engine.begin() as conn:
        conn.execute(text(
            "truncate conversation_events, conversation_sessions, appointments, "
            "slots, doctors restart identity cascade"))

        for d in DOCTORS_SEED:
            conn.execute(text(
                "insert into doctors (slug, display_name, specialty) "
                "values (:s, :n, :sp)"),
                {"s": d.slug, "n": d.display_name, "sp": d.specialty})
        ids = {r[0]: r[1] for r in conn.execute(text("select slug, id from doctors"))}

        rows = []
        for offset in range(days):
            day = today + timedelta(days=offset)
            if day.weekday() == 6:                      # closed on Sundays
                continue
            for d in DOCTORS_SEED:
                for minute in range(9 * 60, 17 * 60, 30):
                    t = time(minute // 60, minute % 60)
                    if is_slot_time(t):
                        start = datetime.combine(day, t, tzinfo=tz)
                        rows.append({"doc": ids[d.slug], "a": start,
                                     "b": start + timedelta(minutes=30)})
        conn.execute(text(
            "insert into slots (doctor_id, starts_at, ends_at) values (:doc, :a, :b)"), rows)

        for offset, slug, t, ref, name in DEMO_APPOINTMENTS:
            day = today + timedelta(days=offset)
            if day.weekday() == 6:
                day += timedelta(days=1)
            slot_id = conn.execute(text(
                "select s.id from slots s where s.doctor_id = :doc and s.starts_at = :t"),
                {"doc": ids[slug], "t": datetime.combine(day, t, tzinfo=tz)}).scalar_one()
            conn.execute(text(
                "insert into appointments (booking_reference, slot_id, patient_name, "
                "idempotency_key) values (:r, :s, :n, :k)"),
                {"r": ref, "s": slot_id, "n": name, "k": f"seed:{ref}"})


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed ClinicCall demo data (destructive).")
    parser.add_argument("--reset", action="store_true",
                        help="required: confirms you want all tables truncated first")
    args = parser.parse_args()
    if not args.reset:
        print("Refusing to run: seeding truncates all tables. Re-run with --reset.")
        return 2
    url = load_env_value("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set (environment or backend/.env).")
        return 2
    tz = ZoneInfo(load_env_value("CLINIC_TIMEZONE") or "Asia/Karachi")
    engine = make_engine(url)
    seed(engine, datetime.now(tz), tz)
    with engine.connect() as conn:
        for table in ("doctors", "slots", "appointments"):
            n = conn.execute(text(f"select count(*) from {table}")).scalar_one()
            print(f"{table}: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())