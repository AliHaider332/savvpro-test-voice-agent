"""Deterministic intent + entity parser for ClinicCall.

Pure function: parse(text, ...) -> ParsedTurn. No I/O, no state mutation.
The engine decides what a result *means* for the current state; the parser
only labels the utterance and extracts entities.
"""
from __future__ import annotations

import difflib
import re
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.brain.datetime_parse import (
    MONTHS, WEEKDAYS, check_bookable_date, parse_date, parse_time,
)
from app.brain.states import Flow, Intent, State
from app.schemas import Context, Entities

LOW_CONFIDENCE = 0.3       # below this the transcript is treated as garbled
CONFIRM_MIN_CONFIDENCE = 0.6  # a spoken "yes" below this cannot authorize a write

DOCTORS = {
    "ahmed": "Dr. Ahmed",
    "khan": "Dr. Sara Khan",
    "malik": "Dr. Hina Malik",
}
_FIRST_NAMES = {"sara": "khan", "hina": "malik"}   # only recognised after "dr"/"doctor"
_NAME_FIELDS = ("patient_name", "verify_name")
_TIME_FIELDS = ("time", "new_time")


class ParsedTurn(BaseModel):
    text: str                                   # normalized text (lowercase, punctuation stripped)
    intent: Intent = Intent.UNKNOWN
    rule: str = "no_match"                      # which rule fired (shown in the decision trace)
    entities: Entities = Field(default_factory=Entities)   # only what THIS turn said
    changed_fields: list[str] = Field(default_factory=list)  # overwrote a different earlier value
    option_index: int | None = None             # 1-based; -1 means "last"
    bare_cancel: bool = False                   # "cancel" with no "my appointment"/reference
    unsupported_reason: Literal["medical", "other"] | None = None
    doctor_unknown: str | None = None           # "Dr. Smith" -> "Smith"
    errors: dict[str, str] = Field(default_factory=dict)   # e.g. {"date": "CLOSED_SUNDAY"}
    quality: Literal["ok", "empty", "low_confidence"] = "ok"
    confirm_allowed: bool = True                # False when STT confidence is too low to confirm


# ------------------------------------------------------------ normalization

def _norm(text: str) -> str:
    t = text.lower().replace("\u2019", "'").replace("`", "'")
    t = re.sub(r"[^a-z0-9' ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


_LEAD = re.compile(
    r"^(?:please |i would like to |i'd like to |i want to |i need to |i wanna |can i |could i |let me )+"
)


def _core(norm: str) -> str:
    c = _LEAD.sub("", norm)
    return re.sub(r" please$", "", c)


# ---------------------------------------------------------------- vocabulary

_CONFIRM_STRONG = {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "correct", "confirm",
                   "confirmed", "right", "absolutely", "definitely", "ahead", "course"}
_CONFIRM_FILLER = {"please", "go", "do", "it", "that's", "thats", "that", "is", "of", "i",
                   "thanks", "thank", "you"}
_CONFIRM_PHRASES = {"do it", "go for it", "please do", "sounds good", "that sounds good"}

_DECLINE_START = re.compile(
    r"^(?:no|nope|nah|negative|incorrect|wrong|not (?:right|correct|that|this)"
    r"|that'?s (?:not|wrong|incorrect)|thats (?:not|wrong)|don'?t|do not)\b"
)
_CONV_CANCEL = re.compile(
    r"\b(?:never ?mind|forget (?:it|that|about it)|start over|start again|stop|quit|exit|leave it"
    r"|cancel (?:that|this|it|the request|the process|everything)"
    r"|don'?t (?:want to )?book|do not book|don'?t bother)\b"
    r"|^(?:no|nope)\s+cancel\b"
)
_CANCEL_WORD = re.compile(r"\bcancel(?:l?ed|l?ing)?\b")
_APPT_NOUN = re.compile(r"\b(?:appointment|booking|reservation|visit|slot)\b")
_OWNER = re.compile(r"\b(?:my|existing|already)\b")
_RESCHED_EXPLICIT = re.compile(r"\bre ?schedul\w*|\bpostpone\b|\bre ?book\b")
_RESCHED_MOVE = re.compile(
    r"\b(?:move|shift|change|push back|push)\b.{0,30}?\b(?:appointment|booking|slot|visit)\b"
    r"|\b(?:move|shift)\b.{0,30}?\bcl\b"
)
_BOOK = re.compile(
    r"\b(?:book|reserve)\b"
    r"|\b(?:need|want|wanna|like|get|make|set up|arrange|take|have)\b.{0,25}?\b(?:appointment|booking)\b"
    r"|\bsee (?:a |the )?(?:doctor|dr)\b"
    r"|\bappointment with\b"
)
_CORRECT_MARKER = re.compile(
    r"\b(?:actually|instead|rather|i meant|i mean|no wait|wait|change (?:it|that|the|my)"
    r"|make it|switch (?:to|it)|let'?s (?:do|make|say|go)|not that)\b"
)
_GREETING = re.compile(
    r"^(?:hi|hello|hey|good (?:morning|afternoon|evening)|goodbye|bye|thanks|thank you)\b"
)
_MEDICAL = re.compile(
    r"\b(?:chest pain|pain|painful|symptoms?|fever|bleeding|emergency|diagnos\w*|prescri\w*"
    r"|medicine|medication|dosage|dose|sick|hurts?|infection|rash|headache|pregnan\w*"
    r"|overdose|dying|suicid\w*|heart attack|breathing)\b"
)
_OTHER_UNSUPPORTED = re.compile(
    r"\b(?:refunds?|payments?|pay|billing|invoice|insurance|prices?|cost|fees?"
    r"|lab results?|test results?|directions|address|phone number|opening hours"
    r"|(?:what time|when) do you (?:open|close))\b"
    r"|\bspeak to (?:a |the )?(?:human|person|receptionist|someone)\b"
)

# disambiguation answers ("stop booking, or cancel an existing appointment?")
_DISAMB_APPT_STRONG = re.compile(
    r"\b(?:existing|already|my appointment|my booking|scheduled|confirmed|the one i (?:made|booked))\b")
_DISAMB_CONV = re.compile(
    r"\b(?:stop|never ?mind|request|current|this|that|process|new|forget|start over|nothing|conversation)\b")
_DISAMB_APPT_WEAK = re.compile(r"\bappointment\b")

# ----------------------------------------------------------------- reference

_DIGIT_WORDS = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4",
                "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9"}
_REF_PREFIXED = re.compile(r"\bcl[\s-]*(\d(?:[\s-]*\d){5})(?!\d)")
_REF_BARE = re.compile(r"(?<!\d)(\d(?:[\s-]*\d){5})(?!\d)")
_REF_STRIP = re.compile(r"\bcl[\s-]*\d(?:[\s-]*\d){5}(?!\d)|(?<!\d)\d{6}(?!\d)")


def _spoken_digits(norm: str) -> str:
    return " ".join(_DIGIT_WORDS.get(w, w) for w in norm.split())


def _extract_reference(digits_norm: str, expected_field: str | None) -> str | None:
    m = _REF_PREFIXED.search(digits_norm)

    if not m and expected_field == "booking_reference":
        m = _REF_BARE.search(digits_norm)

    if not m:
        return None

    digits = re.sub("[^0-9]", "", m.group(1))
    return f"CL-{digits}"

# ---------------------------------------------------------------------- names

_NAME_STRONG = re.compile(
    r"\b(?:my name is|the name is|name is|name's|name should be|under the name|under"
    r"|call me|this is|put it under)\s+([a-z][a-z' -]*)"
)
_NAME_WEAK = re.compile(r"\b(?:i'm|i am|it's|its|for|name)\s+([a-z][a-z' -]*)")  # only if a name is expected
_NAME_TOKEN = re.compile(r"[a-z][a-z'-]{0,19}")
_NAME_STOP = (
    {"and", "at", "on", "for", "to", "please", "tomorrow", "today", "with", "dr", "doctor", "the",
     "a", "an", "in", "am", "pm", "around", "about", "book", "appointment", "booking", "cancel",
     "my", "no", "nope", "nah", "morning", "afternoon", "evening", "next", "this", "then", "but"}
    | _CONFIRM_STRONG | _CONFIRM_FILLER | set(MONTHS) | set(WEEKDAYS)
)


def _take_name_tokens(tokens: list[str]) -> list[str]:
    out: list[str] = []
    for tok in tokens:
        if tok in _NAME_STOP or not _NAME_TOKEN.fullmatch(tok):
            break
        out.append(tok)
        if len(out) == 3:
            break
    if out and sum(len(t) for t in out) + len(out) - 1 > 40:
        return []
    return out


def _extract_name(norm: str, expected_field: str | None, has_flow_intent: bool) -> tuple[str | None, str]:
    """Return (name, text with the name removed so it is not read as a doctor)."""
    name_expected = expected_field in _NAME_FIELDS
    patterns = [_NAME_STRONG] + ([_NAME_WEAK] if name_expected else [])
    for pat in patterns:
        m = pat.search(norm)
        if not m:
            continue
        tokens = m.group(1).split()
        out = _take_name_tokens(tokens)
        if out:
            end = m.start(1) + len(" ".join(tokens[: len(out)]))
            return " ".join(out).title(), norm[: m.start()] + " " + norm[end:]
    if name_expected and not has_flow_intent:
        tokens = norm.split()
        if 1 <= len(tokens) <= 3:
            out = _take_name_tokens(tokens)
            if len(out) == len(tokens):
                return " ".join(out).title(), ""
    return None, norm


# -------------------------------------------------------------------- doctors

_DR = re.compile(r"\b(?:dr|doctor)\s+([a-z]+)")
_DR_SKIP = (
    {"tomorrow", "today", "on", "at", "for", "please", "with", "in", "the", "a", "an", "and",
     "named", "appointment", "booking", "next", "this", "around", "about", "by", "to", "that",
     "who", "i", "my", "then", "or", "any", "anyone", "whoever", "available", "tonight"}
    | set(MONTHS) | set(WEEKDAYS)
)


def _match_doctor(tok: str) -> str | None:
    if tok in DOCTORS:
        return tok
    if tok in _FIRST_NAMES:
        return _FIRST_NAMES[tok]
    if len(tok) >= 3:
        close = difflib.get_close_matches(tok, [*DOCTORS, *_FIRST_NAMES], n=1, cutoff=0.75)
        if close:
            return close[0] if close[0] in DOCTORS else _FIRST_NAMES[close[0]]
    return None


def _extract_doctor(text: str, allow_bare: bool) -> tuple[str | None, str | None]:
    unknown: str | None = None
    for m in _DR.finditer(text):
        tok = m.group(1)
        if tok in _DR_SKIP:
            continue
        slug = _match_doctor(tok)
        if slug:
            return slug, None
        unknown = unknown or tok.title()
    if allow_bare:
        for tok in text.split():
            if tok in DOCTORS:
                return tok, None
    return None, unknown


# -------------------------------------------------------------------- options

_ORDINALS = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "last": -1}
_NUMWORDS = {"one": 1, "two": 2, "three": 3}
_OPTION_WORD = re.compile(r"\b(first|1st|second|2nd|third|3rd|last)\b")
_OPTION_NUM = re.compile(r"\b(?:option|number|choice)\s+(one|two|three|[1-9])\b")
_OPTION_BARE = re.compile(r"(?:the |number |option )*(one|two|three|[1-9])(?: one| please)*")


def _option_index(norm: str) -> int | None:
    if m := _OPTION_WORD.search(norm):
        return _ORDINALS[m.group(1)]
    if m := _OPTION_NUM.search(norm):
        g = m.group(1)
        return _NUMWORDS.get(g) or int(g)
    if m := _OPTION_BARE.fullmatch(norm):
        g = m.group(1)
        return _NUMWORDS.get(g) or int(g)
    return None


# ----------------------------------------------------------------------- main

def _is_pure_confirm(norm: str) -> bool:
    if norm in _CONFIRM_PHRASES:
        return True
    toks = norm.split()
    return (bool(toks)
            and all(t in _CONFIRM_STRONG or t in _CONFIRM_FILLER for t in toks)
            and any(t in _CONFIRM_STRONG for t in toks))


def _parse_disambiguation(norm: str, confirm_allowed: bool) -> ParsedTurn:
    base = dict(text=norm, confirm_allowed=confirm_allowed)
    if _DISAMB_APPT_STRONG.search(norm):
        return ParsedTurn(intent=Intent.CANCEL_APPOINTMENT, rule="disambiguation_appointment", **base)
    if _DISAMB_CONV.search(norm):
        return ParsedTurn(intent=Intent.CANCEL_CONVERSATION, rule="disambiguation_conversation", **base)
    if _DISAMB_APPT_WEAK.search(norm):
        return ParsedTurn(intent=Intent.CANCEL_APPOINTMENT, rule="disambiguation_appointment", **base)
    return ParsedTurn(intent=Intent.UNKNOWN, rule="disambiguation_unclear", **base)


def parse(
    text: str,
    *,
    today: date,
    state: State = State.IDLE,
    context: Context | None = None,
    expected_field: str | None = None,
    stt_confidence: float | None = None,
) -> ParsedTurn:
    """expected_field: the field the engine just asked for (doctor, date, time,
    patient_name, verify_name, booking_reference, new_date, new_time). It lets a
    bare "11", "Ali" or "482913" be read correctly."""
    ctx = context or Context()
    raw_lower = (text or "").lower().replace("\u2019", "'")
    norm = _norm(text or "")

    alnum = re.sub(r"[^a-z0-9]", "", norm)
    if not alnum or (len(alnum) < 2 and not alnum.isdigit()):
        return ParsedTurn(text=norm, quality="empty", rule="empty_input", confirm_allowed=False)
    if stt_confidence is not None and stt_confidence < LOW_CONFIDENCE:
        return ParsedTurn(text=norm, quality="low_confidence", rule="low_confidence", confirm_allowed=False)
    confirm_allowed = stt_confidence is None or stt_confidence >= CONFIRM_MIN_CONFIDENCE

    if state == State.DISAMBIGUATING:
        return _parse_disambiguation(norm, confirm_allowed)

    if _is_pure_confirm(norm):
        return ParsedTurn(text=norm, intent=Intent.CONFIRM, rule="pure_confirm",
                          confirm_allowed=confirm_allowed)
    if _MEDICAL.search(norm):
        return ParsedTurn(text=norm, intent=Intent.UNSUPPORTED, rule="medical_refusal",
                          unsupported_reason="medical")
    if _OTHER_UNSUPPORTED.search(norm):
        return ParsedTurn(text=norm, intent=Intent.UNSUPPORTED, rule="unsupported_request",
                          unsupported_reason="other")

    ref = _extract_reference(_spoken_digits(norm), expected_field)
    core = _core(norm)
    appt_cancel = bool(_CANCEL_WORD.search(norm)) and (
        core == "cancel" or ref is not None or bool(_APPT_NOUN.search(norm)))

    if _CONV_CANCEL.search(norm) and not appt_cancel:
        return ParsedTurn(text=norm, intent=Intent.CANCEL_CONVERSATION, rule="conversation_cancel")

    flow_intent: Intent | None = None
    rule = ""
    bare_cancel = False
    if appt_cancel:
        flow_intent, rule = Intent.CANCEL_APPOINTMENT, "intent_cancel_appointment"
        bare_cancel = core == "cancel" or (ref is None and not _OWNER.search(norm))
    elif _RESCHED_EXPLICIT.search(norm) or (
            _RESCHED_MOVE.search(norm) and (ctx.flow != Flow.BOOK or ref is not None)):
        flow_intent, rule = Intent.RESCHEDULE_APPOINTMENT, "intent_reschedule"
    elif _BOOK.search(norm):
        flow_intent, rule = Intent.BOOK_APPOINTMENT, "intent_book"

    # option selection ("the second one") only when slots were offered
    if ctx.offered_slots and flow_intent is None:
        opt = _option_index(norm)
        if opt is not None:
            return ParsedTurn(text=norm, intent=Intent.SELECT_OPTION, rule="option_selected",
                              option_index=opt)

    target_flow = {
        Intent.BOOK_APPOINTMENT: Flow.BOOK,
        Intent.RESCHEDULE_APPOINTMENT: Flow.RESCHEDULE,
        Intent.CANCEL_APPOINTMENT: Flow.CANCEL,
    }.get(flow_intent, ctx.flow)  # type: ignore[arg-type]

    ents: dict[str, Any] = {}
    errors: dict[str, str] = {}
    if ref:
        ents["booking_reference"] = ref

    # name (and remove it from the text used for doctor matching)
    name, doc_text = _extract_name(norm, expected_field, flow_intent is not None)
    if name:
        if expected_field in _NAME_FIELDS:
            name_key = expected_field
        elif target_flow in (Flow.RESCHEDULE, Flow.CANCEL):
            name_key = "verify_name"
        else:
            name_key = "patient_name"
        ents[name_key] = name

    slug, doctor_unknown = _extract_doctor(doc_text, allow_bare=expected_field not in _NAME_FIELDS)
    if slug:
        ents["doctor_slug"], ents["doctor_name"] = slug, DOCTORS[slug]
        doctor_unknown = None

    # dates and times (reference digits are removed first)
    clean = _REF_STRIP.sub(" ", raw_lower)
    resched = target_flow == Flow.RESCHEDULE
    date_key, time_key = ("new_date", "new_time") if resched else ("date", "time")

    d = parse_date(clean, today)
    if d.matched:
        if d.error:
            errors["date"] = d.error
        elif (bad := check_bookable_date(d.value, today)):
            errors["date"] = bad
        else:
            ents[date_key] = d.value

    t = parse_time(clean, bare_ok=expected_field in _TIME_FIELDS)
    if t.matched:
        if t.error:
            errors["time"] = t.error
        else:
            if t.value:
                ents[time_key] = t.value
            if t.period:
                ents["time_period"] = t.period

    changed = [k for k, v in ents.items()
               if k != "doctor_name" and getattr(ctx.entities, k) not in (None, v)]
    common = dict(text=norm, entities=Entities(**ents), changed_fields=changed, errors=errors,
                  doctor_unknown=doctor_unknown, confirm_allowed=confirm_allowed)

    if flow_intent:
        return ParsedTurn(intent=flow_intent, rule=rule, bare_cancel=bare_cancel, **common)

    marker = bool(_CORRECT_MARKER.search(norm)) or bool(re.match(r"(?:no|nope)\b", norm))
    if not ents and _DECLINE_START.match(norm):
        return ParsedTurn(intent=Intent.DECLINE, rule="decline", **common)
    if ents:
        if marker or changed or state == State.AWAITING_CONFIRMATION:
            return ParsedTurn(intent=Intent.CORRECT_INFORMATION, rule="correction", **common)
        return ParsedTurn(intent=Intent.PROVIDE_INFORMATION, rule="provide_information", **common)
    if errors or doctor_unknown:
        return ParsedTurn(intent=Intent.PROVIDE_INFORMATION, rule="entity_errors", **common)
    if _CORRECT_MARKER.search(norm):
        return ParsedTurn(intent=Intent.CORRECT_INFORMATION, rule="correction_marker_only", **common)
    if _GREETING.match(norm):
        return ParsedTurn(intent=Intent.GREETING_GOODBYE, rule="greeting", **common)
    return ParsedTurn(intent=Intent.UNKNOWN, rule="no_match", **common)