
from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import get_settings


def _error(
    status: int,
    code: str,
    message: str,
    details=None,
) -> JSONResponse:
    return JSONResponse(
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
        request: Request,
        exc: RequestValidationError,
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
        request: Request,
        exc: StarletteHTTPException,
    ) -> JSONResponse:
        if exc.status_code == 404:
            return _error(
                404,
                "NOT_FOUND",
                "The requested resource was not found.",
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
        request: Request,
        exc: Exception,
    ) -> JSONResponse:
        # Keep internal exception details out of client responses.
        return _error(
            500,
            "INTERNAL_ERROR",
            "Something went wrong.",
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        # Database connectivity will be added in the Supabase phase.
        return {"status": "ok", "db": "not_configured"}

    return app


app = create_app()
