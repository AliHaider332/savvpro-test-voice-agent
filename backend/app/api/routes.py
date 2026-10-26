"""HTTP routes. Thin: validation in, domain service call, model out.
Domain exceptions become the standard error body in app.main."""
from __future__ import annotations

from collections.abc import Callable
from datetime import date as Date
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.api.deps import Container, get_container, get_db_probe
from app.schemas import (
    AppointmentView, AvailabilityResponse, ConversationView, CreateConversationResponse,
    DoctorOut, ErrorResponse, HealthResponse, TurnRequest, TurnResponse,
)
from app.services import queries

router = APIRouter()

_E404 = {404: {"model": ErrorResponse}}
_E422 = {422: {"model": ErrorResponse}}
_E503 = {503: {"model": ErrorResponse}}


@router.get("/health", response_model=HealthResponse)
def health(probe: Callable[[], str] = Depends(get_db_probe)) -> HealthResponse:
    """Liveness plus a database status field. Always 200 while the process is up."""
    return HealthResponse(db=probe())


@router.post("/api/conversations", status_code=201,
             response_model=CreateConversationResponse, responses={**_E503})
def create_conversation(c: Container = Depends(get_container)):
    return c.conversations.create_conversation()


@router.post("/api/conversations/{conversation_id}/turns", response_model=TurnResponse,
             responses={**_E404, 409: {"model": ErrorResponse}, **_E422, **_E503})
def post_turn(conversation_id: UUID, body: TurnRequest,
              c: Container = Depends(get_container)):
    """Retrying the same `turn_id` returns the original response and never repeats an action."""
    return c.conversations.handle_turn(conversation_id, body).response


@router.get("/api/conversations/{conversation_id}", response_model=ConversationView,
            responses={**_E404, **_E422, **_E503})
def get_conversation(conversation_id: UUID, c: Container = Depends(get_container)):
    return c.conversations.get_conversation(conversation_id)


@router.get("/api/doctors", response_model=list[DoctorOut], responses={**_E503})
def list_doctors(c: Container = Depends(get_container)):
    return queries.list_doctors(c.appointments)


@router.get("/api/availability", response_model=AvailabilityResponse,
            responses={**_E404, **_E422, **_E503})
def availability(doctor: str = Query(min_length=1, max_length=40, description="doctor slug"),
                 date: Date = Query(description="YYYY-MM-DD, clinic timezone"),
                 c: Container = Depends(get_container)):
    return queries.availability(c.appointments, doctor, date, c.clock())


@router.get("/api/appointments/{reference}", response_model=AppointmentView,
            responses={**_E404, **_E422, **_E503})
def get_appointment(reference: str,
                    name: str = Query(min_length=1, max_length=40,
                                      description="patient name, must match the booking"),
                    c: Container = Depends(get_container)):
    return queries.appointment(c.appointments, reference, name)