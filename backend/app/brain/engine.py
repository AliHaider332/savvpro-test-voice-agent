"""Deterministic conversation engine (book / reschedule / cancel).

Rules that must always hold:
  * The ONLY method that mutates the database is Engine._execute (through the
    private _execute_book / _execute_reschedule / _execute_cancel helpers).
  * _execute runs only after authorize() returns ok, via _confirm.
  * Any change to a field that defines the action clears the pending action.
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

_SLOT_FIELDS = {"doctor_slug", "date", "time", "new_date", "new_time"}
_LOOKUP_FIELDS = {"booking_reference", "verify_name"}
_ENTITY_FIELDS = ("doctor_slug", "date", "time", "time_period", "patient_name",
                  "booking_reference", "verify_name", "new_date", "new_time")

# (entity attribute, label used in missing_fields / expected_field)
_BOOK_FIELDS = (("doctor_slug", "doctor"), ("date", "date"),
                ("time", "time"), ("patient_name", "patient_name"))
_LOOKUP = (("booking_reference", "booking_reference"), ("verify_name", "verify_name"))
_MOVE_TARGET = (("new_date", "new_date"), ("new_time", "new_time"))

_APPT_FLOWS = (Flow.RESCHEDULE, Flow.CANCEL)
_ACTIVITY = {Flow.BOOK: "booking", Flow.RESCHEDULE: "rescheduling", Flow.CANCEL: "cancellation"}
_ALLOWED = {
    Flow.BOOK: {"doctor_slug", "doctor_name", "date", "time", "time_period", "patient_name"},
    Flow.RESCHEDULE: {"booking_reference", "verify_name", "new_date", "new_time", "time_period"},
    Flow.CANCEL: {"booking_reference", "verify_name"},
}
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

def _required_fields(ctx: Context):
    if ctx.flow == Flow.BOOK:
        return _BOOK_FIELDS
    if ctx.flow in _APPT_FLOWS:
        base = () if ctx.appointment_snapshot else _LOOKUP
        return (*base, *(_MOVE_TARGET if ctx.flow == Flow.RESCHEDULE else ()))
    return ()


def missing_fields(ctx: Context) -> list[str]:
    return [label for attr, label in _required_fields(ctx) if getattr(ctx.entities, attr) is None]


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
    if ctx.flow is None or missing_fields(ctx):
        return False, "missing_fields"
    if ctx.flow == Flow.BOOK:
        if ctx.selected_slot_id is None or ctx.selected_slot_id != pa.slot_id:
            return False, "missing_fields"
        return True, "ok"
    snap = ctx.appointment_snapshot
    if (snap is None or snap.status != "confirmed" or pa.appointment_id != snap.id
            or pa.appointment_version != snap.version):
        return False, "appointment_mismatch"
    if ctx.flow == Flow.RESCHEDULE and (
            ctx.selected_slot_id is None or ctx.selected_slot_id != pa.slot_id):
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
            return self._other_flow(s, p, now, today, tr)
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
            flow = ctx.pending_action.type
            tr.add(K.CONFIRMATION_DECLINED, executed=False,
                   action_id=str(ctx.pending_action.action_id))
            ctx.pending_action = None
            if flow == Flow.CANCEL:
                self._reset(s, tr, "cancellation_declined")
                return _Out(R.cancel_declined())
            self._move(s, State.COLLECTING, tr, "confirmation_declined")
            return _Out(R.reschedule_declined() if flow == Flow.RESCHEDULE else R.declined())
        tr.add(K.NO_ACTION, reason="decline_without_pending")
        return _Out(f"Okay. {self._prompt(s)}")

    # ---- reschedule / cancel entry
    def _other_flow(self, s: Session, p: ParsedTurn, now: datetime, today: date,
                    tr: _Trace) -> _Out:
        ctx = s.context
        # bare "cancel" while booking/rescheduling: stop the request, or cancel an appointment?
        if (p.intent == Intent.CANCEL_APPOINTMENT and p.bare_cancel
                and ctx.flow in (Flow.BOOK, Flow.RESCHEDULE)
                and s.state in (State.COLLECTING, State.AWAITING_CONFIRMATION)):
            if ctx.pending_action:
                ctx.pending_action = None
                tr.add(K.STALE_PENDING_CLEARED, reason="ambiguous_cancel")
            self._move(s, State.DISAMBIGUATING, tr, "bare_cancel_mid_flow")
            return _Out(R.disambiguate(_ACTIVITY[ctx.flow]))
        target = Flow.RESCHEDULE if p.intent == Intent.RESCHEDULE_APPOINTMENT else Flow.CANCEL
        self._enter_flow(s, target, tr)
        return self._collect(s, p, now, today, tr)

    def _enter_flow(self, s: Session, target: Flow, tr: _Trace) -> None:
        """Start a flow or switch to another one. A switch drops the old pending action;
        switching between reschedule and cancel keeps the verified appointment."""
        old = s.context
        if old.flow == target:
            return
        if old.flow is not None:
            tr.add(K.INTENT_SWITCH, from_flow=old.flow.value, to_flow=target.value)
            if old.pending_action:
                tr.add(K.STALE_PENDING_CLEARED, reason="intent_switch")
        new = Context(flow=target, last_completed=old.last_completed)
        if old.flow in _APPT_FLOWS and target in _APPT_FLOWS:
            new.entities = Entities(booking_reference=old.entities.booking_reference,
                                    verify_name=old.entities.verify_name)
            new.appointment_snapshot = old.appointment_snapshot
        s.context = new

    # ---- confirmation gate
    def _confirm(self, s: Session, p: ParsedTurn, now: datetime, today: date, tr: _Trace) -> _Out:
        ok, reason = authorize(s, p, now)
        if not ok:
            tr.add(K.REFUSED, executed=False, reason=reason)
            if reason in ("stale_fingerprint", "expired", "missing_fields", "appointment_mismatch"):
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
        pa = s.context.pending_action
        assert pa is not None
        if pa.type == Flow.RESCHEDULE:
            return self._execute_reschedule(s, now, today, tr)
        if pa.type == Flow.CANCEL:
            return self._execute_cancel(s, now, today, tr)
        return self._execute_book(s, now, today, tr)

    def _execute_book(self, s: Session, now: datetime, today: date, tr: _Trace) -> _Out:
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

    def _execute_reschedule(self, s: Session, now: datetime, today: date, tr: _Trace) -> _Out:
        ctx = s.context
        pa, snap = ctx.pending_action, ctx.appointment_snapshot
        assert pa is not None and snap is not None and ctx.selected_slot_id is not None
        try:
            appt = self._svc.reschedule(appointment=snap, new_slot_id=ctx.selected_slot_id, now=now)
        except BookingError as err:
            return self._write_failed(s, err, now, today, tr)
        tr.add(K.DB_WRITE, executed=True, type=Flow.RESCHEDULE.value,
               booking_reference=appt.booking_reference, appointment_id=str(appt.id),
               slot_id=str(appt.slot_id), version=appt.version)
        ctx.pending_action = None
        ctx.offered_slots = []
        ctx.appointment_snapshot = appt
        self._move(s, State.COMPLETED, tr, "action_executed")
        local = appt.starts_at.astimezone(self._svc.tz)
        return _Out(R.rescheduled(appt.booking_reference, appt.doctor_name,
                                  R.when(local.date(), today), format_time(local.time())),
                    completed=CompletedAction(type=Flow.RESCHEDULE,
                                              booking_reference=appt.booking_reference,
                                              appointment=appt))

    def _execute_cancel(self, s: Session, now: datetime, today: date, tr: _Trace) -> _Out:
        ctx = s.context
        pa, snap = ctx.pending_action, ctx.appointment_snapshot
        assert pa is not None and snap is not None
        try:
            appt = self._svc.cancel(appointment=snap, now=now)
        except BookingError as err:
            return self._write_failed(s, err, now, today, tr)
        tr.add(K.DB_WRITE, executed=True, type=Flow.CANCEL.value,
               booking_reference=appt.booking_reference, appointment_id=str(appt.id),
               slot_id=str(appt.slot_id), version=appt.version)
        ctx.pending_action = None
        ctx.appointment_snapshot = appt
        self._move(s, State.COMPLETED, tr, "action_executed")
        return _Out(R.cancelled(appt.booking_reference),
                    completed=CompletedAction(type=Flow.CANCEL,
                                              booking_reference=appt.booking_reference,
                                              appointment=appt))

    def _write_failed(self, s: Session, err: BookingError, now: datetime, today: date,
                      tr: _Trace) -> _Out:
        """A reschedule/cancel write was refused. Nothing changed; recover safely."""
        ctx, e = s.context, s.context.entities
        pa = ctx.pending_action
        flow, ref, code = pa.type, e.booking_reference or "", err.code
        tr.add(K.DB_WRITE, executed=False, outcome=code, action_id=str(pa.action_id))
        ctx.pending_action = None

        if code in ("SLOT_TAKEN", "SLOT_UNAVAILABLE"):
            self._move(s, State.COLLECTING, tr, code.lower())
            status = "taken" if code == "SLOT_TAKEN" else "missing"
            text = self._offer_alternatives(s, status, now, today, tr,
                                            sentence=R.no_longer_available_move())
            return _Out(text, error=code)

        if code == "SAME_SLOT":
            e.new_time, e.time_period, ctx.selected_slot_id = None, None, None
            self._move(s, State.COLLECTING, tr, "same_slot")
            return _Out(R.same_time(), error=code)

        if code == "STALE_VERSION":
            fresh = self._svc.verify_appointment(e.booking_reference, e.verify_name)
            if fresh is not None and fresh.status == "confirmed" and fresh.starts_at > now:
                ctx.appointment_snapshot = fresh
                self._move(s, State.COLLECTING, tr, "appointment_changed")
                return _Out(f"{R.appointment_changed()} {self._advance(s, now, today, tr)}",
                            error=code)
            if fresh is None:
                code = "NOT_FOUND"
            elif fresh.status == "cancelled":
                code = "ALREADY_CANCELLED"
            else:
                code = "APPOINTMENT_PAST"

        if code == "ALREADY_CANCELLED":
            text = R.already_cancelled(ref) if flow == Flow.CANCEL else R.cannot_reschedule_cancelled(ref)
        elif code == "APPOINTMENT_PAST":
            text = R.appointment_past()
        else:
            text = R.appointment_not_found()
        self._reset(s, tr, code.lower())
        return _Out(text, error=code)

    # ---- slot filling
    def _collect(self, s: Session, p: ParsedTurn, now: datetime, today: date, tr: _Trace) -> _Out:
        if p.intent == Intent.BOOK_APPOINTMENT and s.context.flow not in (None, Flow.BOOK):
            self._enter_flow(s, Flow.BOOK, tr)
        ctx = s.context
        if p.intent == Intent.BOOK_APPOINTMENT and ctx.flow is None:
            ctx.flow = Flow.BOOK
        if ctx.flow is None:
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
            tkey = "new_time" if ctx.flow == Flow.RESCHEDULE else "time"
            changed = self._absorb_entities(s, Entities(**{tkey: self._svc.local_time(slot)}), tr)
        else:
            if p.rule == "correction_marker_only":
                tr.add(K.NO_ACTION, reason="correction_without_details")
                return _Out(f"{R.what_to_change()} {self._prompt(s)}")
            snap = ctx.appointment_snapshot
            if (ctx.flow == Flow.RESCHEDULE and snap and p.entities.doctor_slug
                    and p.entities.doctor_slug != snap.doctor_slug):
                notes.append(R.same_doctor_only(snap.doctor_name))
            changed = self._absorb_entities(s, self._relevant(ctx.flow, p.entities), tr)
            if ctx.flow != Flow.CANCEL:
                for fld, code in p.errors.items():
                    notes.append(R.entity_error(fld, code))
                    hint = self._bump(ctx, fld) >= HINT_AFTER_RETRIES or hint
                if p.doctor_unknown and ctx.flow == Flow.BOOK:
                    notes.append(R.unknown_doctor(p.doctor_unknown, self._doctor_names()))
                    hint = self._bump(ctx, "doctor") >= HINT_AFTER_RETRIES or hint
        for f in changed:
            ctx.retries.pop("doctor" if f == "doctor_slug" else f, None)

        text = " ".join([*notes, self._advance(s, now, today, tr)])
        if hint:
            text += " " + R.type_instead()
        return _Out(text, error=next(iter(p.errors.values()), None))

    @staticmethod
    def _relevant(flow: Flow | None, inc: Entities) -> Entities:
        keep = _ALLOWED.get(flow, set())
        return Entities(**{k: v for k, v in inc.model_dump().items()
                           if k in keep and v is not None})

    def _absorb_entities(self, s: Session, inc: Entities, tr: _Trace) -> list[str]:
        ctx, e = s.context, s.context.entities
        changed: list[str] = []
        for f in _ENTITY_FIELDS:
            new = getattr(inc, f)
            if new is None or getattr(e, f) == new:
                continue
            old = getattr(e, f)
            setattr(e, f, new)
            if f == "doctor_slug":
                e.doctor_name = inc.doctor_name
            changed.append(f)
            tr.add(K.ENTITY_UPDATED, field=f, old=old, new=new)
        tkey = "new_time" if ctx.flow == Flow.RESCHEDULE else "time"
        if inc.time_period and getattr(inc, tkey) is None and getattr(e, tkey) is not None:
            tr.add(K.ENTITY_UPDATED, field=tkey, old=getattr(e, tkey), new=None)
            setattr(e, tkey, None)
            changed.append(tkey)
        if changed:
            if _SLOT_FIELDS & set(changed) or "time_period" in changed:
                ctx.offered_slots = []
            if _SLOT_FIELDS & set(changed):
                ctx.selected_slot_id = None
            if _LOOKUP_FIELDS & set(changed):          # different reference/name: re-verify
                ctx.appointment_snapshot = None
                ctx.selected_slot_id = None
                ctx.offered_slots = []
            if ctx.pending_action:
                ctx.pending_action = None
                tr.add(K.STALE_PENDING_CLEARED, reason="entity_changed", fields=changed)
        return changed

    # ---- advance: decide what to ask / check / propose next
    def _advance(self, s: Session, now: datetime, today: date, tr: _Trace) -> str:
        ctx = s.context
        if ctx.pending_action:                      # nothing changed: re-read it
            return R.confirm_prompt(ctx.pending_action.summary)
        self._move(s, State.COLLECTING, tr, "collecting_details")
        if ctx.flow in _APPT_FLOWS:
            return self._advance_appointment(s, now, today, tr)
        return self._advance_book(s, now, today, tr)

    def _propose(self, s: Session, summary: str, now: datetime, tr: _Trace) -> str:
        ctx = s.context
        snap = ctx.appointment_snapshot
        ctx.pending_action = PendingAction(
            action_id=self._new_id(), type=ctx.flow, fingerprint=fingerprint(ctx),
            slot_id=ctx.selected_slot_id,
            appointment_id=snap.id if snap else None,
            appointment_version=snap.version if snap else None,
            summary=summary, created_at=now)
        tr.add(K.CONFIRMATION_PROPOSED, executed=False,
               action_id=str(ctx.pending_action.action_id), summary=summary)
        self._move(s, State.AWAITING_CONFIRMATION, tr, "confirmation_proposed")
        return R.confirm_prompt(summary)

    def _advance_book(self, s: Session, now: datetime, today: date, tr: _Trace) -> str:
        ctx, e = s.context, s.context.entities
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
        return self._propose(s, R.summary(e.doctor_name, when, t_label, e.patient_name), now, tr)

    def _advance_appointment(self, s: Session, now: datetime, today: date, tr: _Trace) -> str:
        ctx, e = s.context, s.context.entities
        just_found = False
        if ctx.appointment_snapshot is None:
            if not e.booking_reference:
                return R.ask_reference()
            if not e.verify_name:
                return R.ask_verify_name()
            found = self._svc.verify_appointment(e.booking_reference, e.verify_name)
            tr.add(K.APPOINTMENT_LOOKUP, verified=found is not None)
            if found is None:                       # same text for unknown reference / wrong name
                attempts = self._bump(ctx, "appointment_lookup")
                e.booking_reference, e.verify_name = None, None
                text = R.appointment_not_found()
                return f"{text} {R.lookup_help()}" if attempts >= HINT_AFTER_RETRIES else text
            ctx.retries.pop("appointment_lookup", None)
            ctx.appointment_snapshot = found
            just_found = True

        snap = ctx.appointment_snapshot
        ref = snap.booking_reference
        local = snap.starts_at.astimezone(self._svc.tz)
        cur_when, cur_t = R.when(local.date(), today), format_time(local.time())

        if snap.status != "confirmed":
            tr.add(K.REFUSED, executed=False, reason="already_cancelled", booking_reference=ref)
            text = (R.already_cancelled(ref) if ctx.flow == Flow.CANCEL
                    else R.cannot_reschedule_cancelled(ref))
            self._reset(s, tr, "appointment_already_cancelled")
            return text
        if snap.starts_at <= now:
            tr.add(K.REFUSED, executed=False, reason="appointment_past", booking_reference=ref)
            self._reset(s, tr, "appointment_past")
            return R.appointment_past()

        if ctx.flow == Flow.CANCEL:
            ctx.selected_slot_id = None
            return self._propose(s, R.cancel_summary(ref, snap.doctor_name, cur_when, cur_t),
                                 now, tr)

        # ---- reschedule (same doctor only)
        slug, dname = snap.doctor_slug, snap.doctor_name
        if e.new_date is None:
            return (R.found_ask_new_date(dname, cur_when, cur_t) if just_found
                    else R.ask_new_date(dname))
        when = R.when(e.new_date, today)

        if e.new_time is None:
            free = self._svc.free_slots(slug, e.new_date, now)
            tr.add(K.AVAILABILITY_CHECK, doctor=slug, date=e.new_date, free_count=len(free))
            if not free:
                text = R.no_openings(dname, when)
                e.new_date = None
                ctx.offered_slots, ctx.selected_slot_id = [], None
                return text
            if e.time_period:
                return self._offer_period(s, now, when, tr)
            return R.ask_time()

        chk = self._svc.check_slot(slug, e.new_date, e.new_time, now)
        tr.add(K.AVAILABILITY_CHECK, doctor=slug, date=e.new_date, time=e.new_time,
               result=chk.status)
        if chk.status == "taken" and chk.slot.id == snap.slot_id:
            tr.add(K.NO_ACTION, reason="same_slot")
            e.new_time, e.time_period = None, None
            ctx.selected_slot_id, ctx.offered_slots = None, []
            return R.same_time()
        if chk.status != "free":
            return self._offer_alternatives(s, chk.status, now, today, tr)

        ctx.selected_slot_id = chk.slot.id
        ctx.offered_slots = []
        summary = R.reschedule_summary(ref, dname, cur_when, cur_t, when, format_time(e.new_time))
        return self._propose(s, summary, now, tr)

    def _slot_target(self, ctx: Context):
        """(doctor_slug, doctor_name, date_attr, time_attr) of the slot being chosen."""
        e, snap = ctx.entities, ctx.appointment_snapshot
        if ctx.flow == Flow.RESCHEDULE:
            return (snap.doctor_slug, snap.doctor_name, "new_date", "new_time") if snap \
                else (None, None, "new_date", "new_time")
        return e.doctor_slug, e.doctor_name, "date", "time"

    def _offer_alternatives(self, s: Session, status: str, now: datetime, today: date,
                            tr: _Trace, sentence: str | None = None) -> str:
        ctx, e = s.context, s.context.entities
        slug, dname, dkey, tkey = self._slot_target(ctx)
        d, t = getattr(e, dkey), getattr(e, tkey)
        when = R.when(d, today)
        lead = sentence or R.unavailable(status, dname, when, format_time(t))
        alts = self._svc.alternatives(slug, d, t, now, n=3)
        setattr(e, tkey, None)
        e.time_period, ctx.selected_slot_id = None, None
        if not alts:
            ctx.offered_slots = []
            setattr(e, dkey, None)
            tr.add(K.ALTERNATIVES_OFFERED, count=0, doctor=slug)
            return f"{lead} {R.no_other_openings(dname, when)}"
        self._set_offers(ctx, alts)
        labels = [o.label for o in ctx.offered_slots]
        tr.add(K.ALTERNATIVES_OFFERED, count=len(alts), slots=labels)
        return f"{lead} {R.openings(labels)}"

    def _offer_period(self, s: Session, now: datetime, when: str, tr: _Trace) -> str:
        ctx, e = s.context, s.context.entities
        slug, dname, dkey, _ = self._slot_target(ctx)
        slots = self._svc.period_slots(slug, getattr(e, dkey), e.time_period, now, n=3)
        if not slots:
            period, e.time_period = e.time_period, None
            tr.add(K.ALTERNATIVES_OFFERED, count=0, period=period)
            return R.no_period_openings(dname, when, period)
        self._set_offers(ctx, slots)
        labels = [o.label for o in ctx.offered_slots]
        tr.add(K.ALTERNATIVES_OFFERED, count=len(slots), period=e.time_period, slots=labels)
        return R.offer_period(dname, when, e.time_period, labels)

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
        ctx = s.context
        if s.state != State.COLLECTING or ctx.flow is None:
            return None
        if ctx.flow == Flow.BOOK:
            for attr, label in _BOOK_FIELDS:
                if getattr(ctx.entities, attr) is None:
                    return label
            return None
        if ctx.appointment_snapshot is None:
            for attr, label in _LOOKUP:
                if getattr(ctx.entities, attr) is None:
                    return label
            return None
        if ctx.flow == Flow.RESCHEDULE:
            for attr, label in _MOVE_TARGET:
                if getattr(ctx.entities, attr) is None:
                    return label
        return None

    def _prompt(self, s: Session) -> str:
        """The question appropriate to the current state (no side effects)."""
        ctx, e = s.context, s.context.entities
        if s.state == State.DISAMBIGUATING:
            return R.disambiguate(_ACTIVITY.get(ctx.flow, "booking"))
        if s.state == State.AWAITING_CONFIRMATION and ctx.pending_action:
            return R.confirm_prompt(ctx.pending_action.summary)
        if s.state == State.COLLECTING:
            if ctx.offered_slots:
                return R.option_list([o.label for o in ctx.offered_slots])
            if ctx.flow in _APPT_FLOWS:
                return self._prompt_appointment(ctx)
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

    @staticmethod
    def _prompt_appointment(ctx: Context) -> str:
        e, snap = ctx.entities, ctx.appointment_snapshot
        if snap is None:
            return R.ask_verify_name() if e.booking_reference and not e.verify_name \
                else R.ask_reference()
        if ctx.flow == Flow.RESCHEDULE:
            if e.new_date is None:
                return R.ask_new_date(snap.doctor_name)
            if e.new_time is None:
                return R.ask_time()
        return R.what_to_change()

    def _move(self, s: Session, new: State, tr: _Trace, reason: str) -> None:
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