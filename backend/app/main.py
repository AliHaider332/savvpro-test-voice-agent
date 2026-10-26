
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.routes import router
from app.config import get_settings
from app.db.session_store import SessionNotFound, TurnInProgress
from app.services.conversation import StorageUnavailable
from app.services.queries import AppointmentNotFound, DoctorNotFound


_DOMAIN_ERRORS = {
    SessionNotFound: (
        404, "CONVERSATION_NOT_FOUND", "Conversation not found.", None
    ),
    DoctorNotFound: (
        404, "DOCTOR_NOT_FOUND", "Doctor not found.", None
    ),
    AppointmentNotFound: (
        404,
        "APPOINTMENT_NOT_FOUND",
        "No appointment matches that reference and name.",
        None,
    ),
    TurnInProgress: (
        409,
        "TURN_IN_PROGRESS",
        "Another request for this conversation is still being processed. "
        "Retry shortly with the same turn_id.",
        1,
    ),
    StorageUnavailable: (
        503,
        "STORAGE_UNAVAILABLE",
        "The service is temporarily unavailable. Retry shortly; "
        "retrying the same turn_id is safe.",
        2,
    ),
}


def _error(
    status: int,
    code: str,
    message: str,
    details=None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        headers=headers,
        status_code=status,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details,
            }
        },
    )


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="ClinicCall API", version="0.1.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.origins_list,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return _error(
            422,
            "VALIDATION_ERROR",
            "Invalid request.",
            jsonable_encoder(
                exc.errors(),
                custom_encoder={Exception: str},
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        if exc.status_code == 404:
            return _error(
                404, "NOT_FOUND", "The requested resource was not found."
            )

        code = (
            exc.detail
            if isinstance(exc.detail, str) and exc.detail.isupper()
            else "HTTP_ERROR"
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": code,
                    "message": str(exc.detail),
                    "details": None,
                }
            },
            headers=exc.headers,
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        return _error(
            500, "INTERNAL_ERROR", "Something went wrong."
        )

    def _domain_handler(
        status: int, code: str, message: str, retry_after: int | None
    ):
        async def handler(request: Request, exc: Exception) -> JSONResponse:
            headers = (
                {"Retry-After": str(retry_after)}
                if retry_after is not None
                else None
            )
            return _error(status, code, message, headers=headers)

        return handler

    for exc_type, (
        status, code, message, retry_after
    ) in _DOMAIN_ERRORS.items():
        app.add_exception_handler(
            exc_type,
            _domain_handler(status, code, message, retry_after),
        )

    @app.exception_handler(DBAPIError)
    async def database_error_handler(
        request: Request, exc: DBAPIError
    ) -> JSONResponse:
        if exc.connection_invalidated:
            return _error(
                503,
                "STORAGE_UNAVAILABLE",
                "The service is temporarily unavailable. Retry shortly.",
                headers={"Retry-After": "2"},
            )

        return _error(
            500, "INTERNAL_ERROR", "Something went wrong."
        )

    app.include_router(router)
    return app


app = create_app()
