"""Every assistant sentence is a template filled with validated data."""
from __future__ import annotations

from datetime import date, time

from app.brain.datetime_parse import format_date

HELP = "I can book, reschedule or cancel an appointment. What would you like to do?"


def join_or(items) -> str:
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " or " + items[-1]


def when(d: date, today: date) -> str:
    label = format_date(d, today)
    return label if label in ("today", "tomorrow") else f"on {label}"


def greeting() -> str:
    return "Hello, this is ClinicCall, a demo clinic assistant using fictional data. " + HELP


def didnt_catch(prompt: str) -> str:
    return f"Sorry, I didn't catch that. {prompt}"


def not_understood(prompt: str) -> str:
    return f"Sorry, I didn't understand that. {prompt}"


def medical_refusal() -> str:
    return ("I can't give medical advice. If this is urgent, please contact a clinician "
            "or your local emergency services.")


def unsupported() -> str:
    return "Sorry, I can't help with that."


def no_active_request() -> str:
    return f"I'm not sure what you're referring to. {HELP}"


def not_implemented() -> str:
    return "Rescheduling and cancelling existing appointments isn't available in this build yet."


# ---- questions
def ask_doctor(names) -> str:
    return f"Which doctor would you like to see? We have {join_or(names)}."


def ask_date(doctor: str) -> str:
    return f"Which day would you like to see {doctor}? You can say something like tomorrow or Thursday."


def ask_time() -> str:
    return "What time would you prefer?"


def ask_name() -> str:
    return "What name should I use for this booking?"


def slot_free_ask_name(doctor: str, when_str: str, t: str) -> str:
    return f"{doctor} has an opening {when_str} at {t}. {ask_name()}"


# ---- existing-appointment lookup (reschedule / cancel)
def ask_reference() -> str:
    return "What's your booking reference? It starts with CL followed by six digits."


def ask_verify_name() -> str:
    return "And what name is the booking under?"


def appointment_not_found() -> str:
    # Deliberately identical for unknown reference and wrong name (no information leak).
    return ("I couldn't find a matching appointment. Please check the details. "
            "What's your booking reference?")


def lookup_help() -> str:
    return "You can also say 'start over', or contact the clinic if you can't find your reference."


def found_ask_new_date(doctor: str, when_str: str, t: str) -> str:
    return (f"I found your appointment with {doctor} {when_str} at {t}. "
            "Which day would you like to move it to?")


def ask_new_date(doctor: str) -> str:
    return f"Which day would you like to move your appointment with {doctor} to?"


def same_time() -> str:
    return "That's already the time of your appointment. What other time would you like?"


def same_doctor_only(doctor: str) -> str:
    return f"I can only move your appointment within {doctor}'s schedule."


# ---- availability
def unavailable(status: str, doctor: str, when_str: str, asked: str) -> str:
    if status == "past":
        return f"{asked} has already passed."
    if status == "missing":
        return f"{doctor} doesn't have an appointment at {asked} {when_str}."
    return f"{doctor} isn't available {when_str} at {asked}."


def no_longer_available() -> str:
    return "Sorry, that slot is no longer available, so nothing was saved."


def no_longer_available_move() -> str:
    return "Sorry, that slot is no longer available, so your appointment hasn't been changed."


def openings(labels) -> str:
    return f"Openings that day are {join_or(labels)}. Which one would you like?"


def option_list(labels) -> str:
    return f"The openings are {join_or(labels)}. Which one would you like?"


def no_other_openings(doctor: str, when_str: str) -> str:
    return f"{doctor} has no other free appointments {when_str}. Which other day would you like?"


def no_openings(doctor: str, when_str: str) -> str:
    return f"{doctor} has no free appointments {when_str}. Which other day would you like?"


def offer_period(doctor: str, when_str: str, period: str, labels) -> str:
    return (f"{doctor} has openings {when_str} in the {period} at {join_or(labels)}. "
            "Which one would you like?")


def no_period_openings(doctor: str, when_str: str, period: str) -> str:
    return f"{doctor} has no free appointments {when_str} in the {period}. What other time works?"


def option_invalid(labels) -> str:
    if not labels:
        return "I don't have any options to choose from right now."
    return (f"Sorry, I didn't get which one. The openings are {join_or(labels)}. "
            "Please say first, second or third.")


# ---- confirmation / results
def summary(doctor: str, when_str: str, t: str, name: str) -> str:
    return f"book an appointment with {doctor} {when_str} at {t} under the name {name}"


def reschedule_summary(ref: str, doctor: str, old_when: str, old_t: str,
                       new_when: str, new_t: str) -> str:
    return (f"move appointment {ref} with {doctor}, currently {old_when} at {old_t}, "
            f"to {new_when} at {new_t}")


def cancel_summary(ref: str, doctor: str, when_str: str, t: str) -> str:
    return f"cancel appointment {ref} with {doctor} {when_str} at {t}"


def confirm_prompt(summary_text: str) -> str:
    return f"Please confirm: {summary_text}? Say yes to confirm, or tell me what to change."


def booked(reference: str) -> str:
    return f"Your appointment has been booked successfully. Your booking reference is {reference}."


def rescheduled(reference: str, doctor: str, when_str: str, t: str) -> str:
    return f"Done. Appointment {reference} with {doctor} is now {when_str} at {t}."


def cancelled(reference: str) -> str:
    return f"Your appointment {reference} has been cancelled."


def already_booked(reference: str) -> str:
    return (f"You've already booked that appointment. Your booking reference is {reference}. "
            "Is there anything else I can help with?")


def already_cancelled(reference: str) -> str:
    return (f"Appointment {reference} is already cancelled, so I haven't changed anything. "
            "Is there anything else I can help with?")


def cannot_reschedule_cancelled(reference: str) -> str:
    return (f"Appointment {reference} has been cancelled, so it can't be rescheduled. "
            "I can book a new appointment if you like.")


def appointment_past() -> str:
    return "That appointment is in the past, so I can't change it."


def appointment_changed() -> str:
    return "That appointment changed since I last checked, so I haven't made any change."


def declined() -> str:
    return ("No problem, nothing has been booked. What would you like to change: "
            "the doctor, the day, the time, or the name?")


def reschedule_declined() -> str:
    return ("No problem, your appointment hasn't been changed. "
            "What would you like to change: the day or the time?")


def cancel_declined() -> str:
    return "Okay, I haven't cancelled anything. Your appointment is unchanged."


def what_to_change() -> str:
    return "Sure. What would you like to change?"


def stopped() -> str:
    return "Okay, I've stopped that and nothing was changed. How else can I help?"


def nothing_in_progress() -> str:
    return f"There's nothing in progress to cancel. {HELP}"


def nothing_to_confirm() -> str:
    return "There's nothing waiting for confirmation, so I haven't changed anything."


def confirm_unclear() -> str:
    return "I want to be sure I heard you correctly. Please say yes again, or type it."


def stale_confirmation() -> str:
    return "That confirmation was out of date, so I didn't save anything."


def expired() -> str:
    return "That confirmation expired, so I didn't save anything."


def disambiguate(activity: str = "booking") -> str:
    return f"Do you want to stop the {activity} you're working on, or cancel an existing appointment?"


# ---- input problems
_ENTITY_ERRORS = {
    ("date", "INVALID_DATE"): "I couldn't read that date. Try something like 'tomorrow' or 'November 5th'.",
    ("date", "AMBIGUOUS_NUMERIC"): "I couldn't tell the day from the month in that date. Try something like 'November 5th'.",
    ("date", "BEYOND_HORIZON"): "I can only book up to two weeks ahead.",
    ("date", "CLOSED_SUNDAY"): "We're closed on Sundays.",
    ("time", "INVALID_TIME"): "I couldn't read that time.",
    ("time", "OUT_OF_HOURS"): "We're open 9 AM to 1 PM and 2 PM to 5 PM, Monday to Saturday.",
    ("time", "LUNCH_BREAK"): "We're closed for lunch from 1 PM to 2 PM.",
}


def entity_error(field: str, code: str) -> str:
    return _ENTITY_ERRORS.get((field, code), f"I couldn't use that {field}.")


def unknown_doctor(name: str, names) -> str:
    return f"I don't have a Dr. {name}. Our doctors are {join_or(names)}."


def type_instead() -> str:
    return "If speaking isn't working well, you can type your answer instead."