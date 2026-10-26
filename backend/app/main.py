"""FastAPI entry point. Routes are registered in Phase 5; this file is the skeleton."""
from __future__ import annotations

from fastapi import FastAPI

from app.config import get_settings
from app.schemas import HealthResponse


def create_app() -> FastAPI:
    app = FastAPI(title="ClinicCall", version="0.1.0")

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        """Liveness + DB readiness. DB is 'not_configured' until Phase 4."""
        settings = get_settings()
        if not settings.database_url:
            return HealthResponse(status="ok", db="not_configured")
        # Phase 4 replaces this with a real `SELECT 1`; for now the URL is set but
        # we do not attempt a connection in the smoke test.
        return HealthResponse(status="ok", db="ok")

    return app


app = create_app()