"""Deterministic conversation engine (booking flow).

Rules that must always hold:
  * The ONLY call that mutates the database is Engine._execute -> service.book.
  * _execute runs only after authorize() returns ok.
  * Any change to doctor/date/time/name clears the pending action.
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from app.brain import responses as R
from app.brain.datetime_parse import format_time
from app.brain.parser import ParsedTurn, parse
from app.brain.states import EventKind as K
from app.brain.states import Flow, Intent, State
from app.schemas import (
    CompletedAction, Context, ConversationEvent, Entities, LastCompleted,
    OfferedSlot, PendingAction, PendingActionView, TurnResponse,
)
from app.services.appointments import AppointmentService, BookingError

CONFIRMATION_TTL = timedelta(minutes=10)
HINT_AFTER_RETRIES = 3
_SLOT_FIELDS = {"doctor_slug", "date", "time"}
_BOOK_FIELDS = (("doctor_slug", "doctor"), ("date", "date"),
                ("time", "time"), ("patient_name", "patient_name"))
Clock = Callable[[], datetime]


class Session(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    state: State = State.IDLE
    context: Context = Field(default_factory=Context)
    turn_no: int = 0


@dataclass
class _Out:
    text: str
    completed: CompletedAction | None = None
    error: str | None = None


class _Trace:
    def __init__(self) -> None:
        self.events: list[ConversationEvent] = []

    def add(self, kind: K, executed: bool | None = None, **payload) -> None:
        self.events.append(ConversationEvent(
            seq=len(self.events) + 1, kind=kind, payload=payload, executed=executed))


# ----------------------------------------------------------------- pure helpers

def missing_fields(ctx: Context) -> list[str]:
    if ctx.flow != Flow.BOOK:
        return []
    return [label for attr, label in _BOOK_FIELDS if getattr(ctx.entities, attr) is None]


def fingerprint(ctx: Context) -> str:
    e, snap = ctx.entities, ctx.appointment_snapshot
    parts = [
        ctx.flow.value if ctx.flow else "",
        e.doctor_slug or "",
        str(ctx.selected_slot_id or ""),
        (e.patient_name or "").strip().lower(),
        str(snap.id) if snap else "",
        str(snap.version) if snap else "",
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def authorize(session: Session, parsed: ParsedTurn, now: datetime) -> tuple[bool, str]:
    """Every condition must hold before any mutation. Returns (ok, reason)."""
    ctx, pa = session.context, session.context.pending_action
    if session.state != State.AWAITING_CONFIRMATION or pa is None:
        return False, "no_pending_action"
    if parsed.intent != Intent.CONFIRM:
        return False, "not_a_confirmation"
    if not parsed.confirm_allowed:
        return False, "low_confidence"
    if ctx.flow != pa.type or fingerprint(ctx) != pa.fingerprint:
        return False, "stale_fingerprint"
    if now - pa.created_at > CONFIRMATION_TTL:
        return False, "expired"
    if missing_fields(ctx) or ctx.selected_slot_id is None or ctx.selected_slot_id != pa.slot_id:
        return False, "missing_fields"
    return True, "ok"


# ----------------------------------------------------------------------- engine

class Engine:
    def __init__(self, service: AppointmentService, clock: Clock,
                 id_factory: Callable[[], UUID] = uuid4):
        self._svc = service
        self._clock = clock
        self._new_id = id_factory

    # ---- entry point (mutates `session` in place; caller persists it)
    def handle_turn(self, session: Session, text: str,
                    stt_confidence: float | None = None) -> TurnResponse:
        now = self._clock()
        today = now.astimezone(self._svc.tz).date()
        tr = _Trace()
        session.turn_no += 1
        tr.add(K.TURN_RECEIVED, chars=len(text or ""), state=session.state.value)

        if session.state == State.COMPLETED:
            self._reset(session, tr, "new_turn_after_completion")

        expected = self._expected_field(session)
        parsed = parse(text or "", today=today, state=session.state, context=session.context,
                       expected_field=expected, stt_confidence=stt_confidence)
        tr.add(K.PARSED, intent=parsed.intent.value, rule=parsed.rule, quality=parsed.quality,
               expected_field=expected,
               entities=parsed.entities.model_dump(mode="json", exclude_none=True),
               errors=parsed.errors, changed_fields=parsed.changed_fields)

        out = self._dispatch(session, parsed, now, today, tr)
        return self._response(session, parsed, out, tr)

    # ---- dispatch
    def _dispatch(self, s: Session, p: ParsedTurn, now: datetime, today: date, tr: _Trace) -> _Out:
        if p.quality != "ok":
            tr.add(K.EMPTY_INPUT if p.quality == "empty" else K.LOW_CONFIDENCE)
            return _Out(R.didnt_catch(self._prompt(s)))
        i = p.intent
        if i == Intent.UNSUPPORTED:
            tr.add(K.UNSUPPORTED_REQUEST, reason=p.unsupported_reason)
            lead = R.medical_refusal() if p.unsupported_reason == "medical" else R.unsupported()
            return _Out(f"{lead} {self._prompt(s)}")
        if i == Intent.CANCEL_CONVERSATION:
            return self._cancel_conversation(s, tr)
        if i == Intent.GREETING_GOODBYE:
            tr.add(K.NO_ACTION, reason="greeting")
            return _Out(R.greeting() if s.state == State.IDLE else f"Hello! {self._prompt(s)}")
        if i == Intent.CONFIRM:
            return self._confirm(s, p, now, today, tr)
        if i == Intent.DECLINE:
            return self._decline(s, tr)
        if i in (Intent.CANCEL_APPOINTMENT, Intent.RESCHEDULE_APPOINTMENT):
            return self._other_flow(s, p, tr)
        if i in (Intent.BOOK_APPOINTMENT, Intent.PROVIDE_INFORMATION,
                 Intent.CORRECT_INFORMATION, Intent.SELECT_OPTION):
            return self._collect(s, p, now, today, tr)
        tr.add(K.NO_ACTION, reason="not_understood")
        return _Out(R.not_understood(self._prompt(s)))

    # ---- simple handlers
    def _cancel_conversation(self, s: Session, tr: _Trace) -> _Out:
        if s.state == State.IDLE:
            tr.add(K.NO_ACTION, reason="nothing_in_progress")
            return _Out(R.nothing_in_progress())
        if s.context.pending_action:
            tr.add(K.STALE_PENDING_CLEARED, reason="conversation_cancelled")
        self._reset(s, tr, "caller_cancelled_conversation")
        return _Out(R.stopped())

    def _decline(self, s: Session, tr: _Trace) -> _Out:
        ctx = s.context
        if s.state == State.AWAITING_CONFIRMATION and ctx.pending_action:
            tr.add(K.CONFIRMATION_DECLINED, executed=False,
                   action_id=str(ctx.pending_action.action_id))
            ctx.pending_action = None
            self._move(s, State.COLLECTING, tr, "confirmation_declined")
            return _Out(R.declined())
        tr.add(K.NO_ACTION, reason="decline_without_pending")
        return _Out(f"Okay. {self._prompt(s)}")

    def _other_flow(self, s: Session, p: ParsedTurn, tr: _Trace) -> _Out:
        """Reschedule / cancel-appointment: implemented in Task 5. Only the
        'bare cancel mid-booking' disambiguation exists now."""
        ctx = s.context
        if (p.intent == Intent.CANCEL_APPOINTMENT and p.bare_cancel and ctx.flow
                and s.state in (State.COLLECTING, State.AWAITING_CONFIRMATION)):
            if ctx.pending_action:
                ctx.pending_action = None
                tr.add(K.STALE_PENDING_CLEARED, reason="ambiguous_cancel")
            self._move(s, State.DISAMBIGUATING, tr, "bare_cancel_mid_flow")
            return _Out(R.disambiguate())
        tr.add(K.UNSUPPORTED_REQUEST, reason="not_implemented_in_this_build", intent=p.intent.value)
        if s.state == State.DISAMBIGUATING:
            self._reset(s, tr, "disambiguation_resolved")
        return _Out(f"{R.not_implemented()} {self._prompt(s)}")

    # ---- confirmation gate
    def _confirm(self, s: Session, p: ParsedTurn, now: datetime, today: date, tr: _Trace) -> _Out:
        ok, reason = authorize(s, p, now)
        if not ok:
            tr.add(K.REFUSED, executed=False, reason=reason)
            if reason in ("stale_fingerprint", "expired", "missing_fields"):
                tr.add(K.STALE_PENDING_CLEARED, reason=reason)
                s.context.pending_action = None
                lead = R.expired() if reason == "expired" else R.stale_confirmation()
                return _Out(f"{lead} {self._advance(s, now, today, tr)}")
            if reason == "low_confidence":
                return _Out(f"{R.confirm_unclear()} {self._prompt(s)}")
            return _Out(f"{R.nothing_to_confirm()} {self._prompt(s)}")
        tr.add(K.CONFIRMATION_ACCEPTED, action_id=str(s.context.pending_action.action_id))
        return self._execute(s, now, today, tr)

    def _execute(self, s: Session, now: datetime, today: date, tr: _Trace) -> _Out:
        """The single mutation point. Reached only through _confirm after authorize()."""
        ctx = s.context
        pa, e = ctx.pending_action, ctx.entities
        assert pa is not None and ctx.selected_slot_id is not None and e.patient_name
        try:
            appt = self._svc.book(slot_id=ctx.selected_slot_id, patient_name=e.patient_name,
                                  idempotency_key=f"{s.id}:{pa.action_id}", now=now)
        except BookingError as err:
            tr.add(K.DB_WRITE, executed=False, outcome=err.code, action_id=str(pa.action_id))
            ctx.pending_action = None
            self._move(s, State.COLLECTING, tr, err.code.lower())
            status = "taken" if err.code == "SLOT_TAKEN" else "missing"
            text = self._offer_alternatives(s, status, now, today, tr,
                                            sentence=R.no_longer_available())
            return _Out(text, error=err.code)

        tr.add(K.DB_WRITE, executed=True, type=Flow.BOOK.value,
               booking_reference=appt.booking_reference,
               appointment_id=str(appt.id), slot_id=str(appt.slot_id))
        ctx.last_completed = LastCompleted(fingerprint=pa.fingerprint,
                                           booking_reference=appt.booking_reference)
        ctx.pending_action = None
        ctx.offered_slots = []
        self._move(s, State.COMPLETED, tr, "action_executed")
        return _Out(R.booked(appt.booking_reference),
                    completed=CompletedAction(type=Flow.BOOK,
                                              booking_reference=appt.booking_reference,
                                              appointment=appt))

    # ---- slot filling
    def _collect(self, s: Session, p: ParsedTurn, now: datetime, today: date, tr: _Trace) -> _Out:
        ctx = s.context
        if p.intent == Intent.BOOK_APPOINTMENT and ctx.flow is None:
            ctx.flow = Flow.BOOK
        if ctx.flow != Flow.BOOK:
            tr.add(K.NO_ACTION, reason="no_active_request")
            return _Out(R.no_active_request())

        notes: list[str] = []
        hint = False
        if p.intent == Intent.SELECT_OPTION:
            pick = self._pick_option(ctx, p.option_index)
            slot = self._svc.get_slot_by_id(pick.slot_id) if pick else None
            if slot is None:
                tr.add(K.NO_ACTION, reason="invalid_option", option=p.option_index)
                return _Out(R.option_invalid([o.label for o in ctx.offered_slots]))
            changed = self._absorb_entities(s, Entities(time=self._svc.local_time(slot)), tr)
        else:
            if p.rule == "correction_marker_only":
                tr.add(K.NO_ACTION, reason="correction_without_details")
                return _Out(f"{R.what_to_change()} {self._prompt(s)}")
            changed = self._absorb_entities(s, p.entities, tr)
            for fld, code in p.errors.items():
                notes.append(R.entity_error(fld, code))
                hint = self._bump(ctx, fld) >= HINT_AFTER_RETRIES or hint
            if p.doctor_unknown:
                notes.append(R.unknown_doctor(p.doctor_unknown, self._doctor_names()))
                hint = self._bump(ctx, "doctor") >= HINT_AFTER_RETRIES or hint
        for f in changed:
            ctx.retries.pop("doctor" if f == "doctor_slug" else f, None)

        text = " ".join([*notes, self._advance(s, now, today, tr)])
        if hint:
            text += " " + R.type_instead()
        return _Out(text, error=next(iter(p.errors.values()), None))

    def _absorb_entities(self, s: Session, inc: Entities, tr: _Trace) -> list[str]:
        ctx, e = s.context, s.context.entities
        changed: list[str] = []
        for f in ("doctor_slug", "date", "time", "time_period", "patient_name"):
            new = getattr(inc, f)
            if new is None or getattr(e, f) == new:
                continue
            old = getattr(e, f)
            setattr(e, f, new)
            if f == "doctor_slug":
                e.doctor_name = inc.doctor_name
            changed.append(f)
            tr.add(K.ENTITY_UPDATED, field=f, old=old, new=new)
        if inc.time_period and inc.time is None and e.time is not None:
            tr.add(K.ENTITY_UPDATED, field="time", old=e.time, new=None)
            e.time = None
            changed.append("time")
        if changed:
            if _SLOT_FIELDS & set(changed) or "time_period" in changed:
                ctx.offered_slots = []
            if _SLOT_FIELDS & set(changed):
                ctx.selected_slot_id = None
            if ctx.pending_action:
                ctx.pending_action = None
                tr.add(K.STALE_PENDING_CLEARED, reason="entity_changed", fields=changed)
        return changed

    # ---- advance: decide what to ask / check / propose next
    def _advance(self, s: Session, now: datetime, today: date, tr: _Trace) -> str:
        ctx, e = s.context, s.context.entities
        if ctx.pending_action:                      # nothing changed: re-read it
            return R.confirm_prompt(ctx.pending_action.summary)
        self._move(s, State.COLLECTING, tr, "collecting_details")
        if not e.doctor_slug:
            return R.ask_doctor(self._doctor_names())
        if not e.date:
            return R.ask_date(e.doctor_name)
        when = R.when(e.date, today)

        if e.time is None:
            free = self._svc.free_slots(e.doctor_slug, e.date, now)
            tr.add(K.AVAILABILITY_CHECK, doctor=e.doctor_slug, date=e.date, free_count=len(free))
            if not free:
                text = R.no_openings(e.doctor_name, when)
                e.date = None
                ctx.offered_slots, ctx.selected_slot_id = [], None
                return text
            if e.time_period:
                return self._offer_period(s, now, when, tr)
            return R.ask_time()

        chk = self._svc.check_slot(e.doctor_slug, e.date, e.time, now)
        tr.add(K.AVAILABILITY_CHECK, doctor=e.doctor_slug, date=e.date, time=e.time,
               result=chk.status)
        if chk.status != "free":
            if chk.status == "taken" and self._is_own_booking(ctx, chk.slot.id):
                ref = ctx.last_completed.booking_reference
                tr.add(K.REFUSED, executed=False, reason="already_booked", booking_reference=ref)
                self._reset(s, tr, "duplicate_request")
                return R.already_booked(ref)
            return self._offer_alternatives(s, chk.status, now, today, tr)

        ctx.selected_slot_id = chk.slot.id
        ctx.offered_slots = []
        t_label = format_time(e.time)
        if not e.patient_name:
            return R.slot_free_ask_name(e.doctor_name, when, t_label)

        summary = R.summary(e.doctor_name, when, t_label, e.patient_name)
        ctx.pending_action = PendingAction(
            action_id=self._new_id(), type=Flow.BOOK, fingerprint=fingerprint(ctx),
            slot_id=ctx.selected_slot_id, summary=summary, created_at=now)
        tr.add(K.CONFIRMATION_PROPOSED, executed=False,
               action_id=str(ctx.pending_action.action_id), summary=summary)
        self._move(s, State.AWAITING_CONFIRMATION, tr, "confirmation_proposed")
        return R.confirm_prompt(summary)

    def _offer_alternatives(self, s: Session, status: str, now: datetime, today: date,
                            tr: _Trace, sentence: str | None = None) -> str:
        ctx, e = s.context, s.context.entities
        when = R.when(e.date, today)
        lead = sentence or R.unavailable(status, e.doctor_name, when, format_time(e.time))
        alts = self._svc.alternatives(e.doctor_slug, e.date, e.time, now, n=3)
        e.time, e.time_period, ctx.selected_slot_id = None, None, None
        if not alts:
            ctx.offered_slots, e.date = [], None
            tr.add(K.ALTERNATIVES_OFFERED, count=0, doctor=e.doctor_slug)
            return f"{lead} {R.no_other_openings(e.doctor_name, when)}"
        self._set_offers(ctx, alts)
        labels = [o.label for o in ctx.offered_slots]
        tr.add(K.ALTERNATIVES_OFFERED, count=len(alts), slots=labels)
        return f"{lead} {R.openings(labels)}"

    def _offer_period(self, s: Session, now: datetime, when: str, tr: _Trace) -> str:
        ctx, e = s.context, s.context.entities
        slots = self._svc.period_slots(e.doctor_slug, e.date, e.time_period, now, n=3)
        if not slots:
            period, e.time_period = e.time_period, None
            tr.add(K.ALTERNATIVES_OFFERED, count=0, period=period)
            return R.no_period_openings(e.doctor_name, when, period)
        self._set_offers(ctx, slots)
        labels = [o.label for o in ctx.offered_slots]
        tr.add(K.ALTERNATIVES_OFFERED, count=len(slots), period=e.time_period, slots=labels)
        return R.offer_period(e.doctor_name, when, e.time_period, labels)

    # ---- small helpers
    def _set_offers(self, ctx: Context, slots) -> None:
        ctx.offered_slots = [
            OfferedSlot(idx=i, slot_id=sl.id, label=format_time(self._svc.local_time(sl)))
            for i, sl in enumerate(slots, 1)
        ]

    @staticmethod
    def _pick_option(ctx: Context, idx: int | None) -> OfferedSlot | None:
        if not ctx.offered_slots or idx is None:
            return None
        if idx == -1:
            return ctx.offered_slots[-1]
        return next((o for o in ctx.offered_slots if o.idx == idx), None)

    def _is_own_booking(self, ctx: Context, slot_id: UUID) -> bool:
        appt = self._svc.appointment_for_slot(slot_id)
        lc = ctx.last_completed
        return bool(lc and appt and appt.booking_reference == lc.booking_reference)

    @staticmethod
    def _bump(ctx: Context, key: str) -> int:
        ctx.retries[key] = ctx.retries.get(key, 0) + 1
        return ctx.retries[key]

    def _doctor_names(self) -> list[str]:
        return [d.display_name for d in self._svc.doctors()]

    def _expected_field(self, s: Session) -> str | None:
        if s.state != State.COLLECTING or s.context.flow != Flow.BOOK:
            return None
        for attr, label in _BOOK_FIELDS:
            if getattr(s.context.entities, attr) is None:
                return label
        return None

    def _prompt(self, s: Session) -> str:
        """The question appropriate to the current state (no side effects)."""
        ctx, e = s.context, s.context.entities
        if s.state == State.DISAMBIGUATING:
            return R.disambiguate()
        if s.state == State.AWAITING_CONFIRMATION and ctx.pending_action:
            return R.confirm_prompt(ctx.pending_action.summary)
        if s.state == State.COLLECTING:
            if ctx.offered_slots:
                return R.option_list([o.label for o in ctx.offered_slots])
            if not e.doctor_slug:
                return R.ask_doctor(self._doctor_names())
            if not e.date:
                return R.ask_date(e.doctor_name)
            if e.time is None:
                return R.ask_time()
            if not e.patient_name:
                return R.ask_name()
            return R.what_to_change()
        return R.HELP

    def _move(self, s: Session, new: State, tr: _Trace, reason: str) -> None:
        if s.state != new:
            tr.add(K.STATE_TRANSITION, from_state=s.state.value, to_state=new.value, reason=reason)
            s.state = new

    def _reset(self, s: Session, tr: _Trace, reason: str) -> None:
        s.context = Context(last_completed=s.context.last_completed)
        self._move(s, State.IDLE, tr, reason)

    def _response(self, s: Session, p: ParsedTurn, out: _Out, tr: _Trace) -> TurnResponse:
        ctx, pa = s.context, s.context.pending_action
        return TurnResponse(
            conversation_id=s.id, turn_no=s.turn_no, assistant_text=out.text.strip(),
            state=s.state, intent=p.intent, flow=ctx.flow,
            entities=ctx.entities.model_copy(deep=True), missing_fields=missing_fields(ctx),
            offered_slots=list(ctx.offered_slots),
            pending_action=(PendingActionView(summary=pa.summary,
                                              expires_at=pa.created_at + CONFIRMATION_TTL)
                            if pa else None),
            completed_action=out.completed, events=tr.events, error=out.error)