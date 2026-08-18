"""FastAPI application factory."""

from fastapi import FastAPI

from optima.api.routes.health import router as health_router


def create_app() -> FastAPI:
    """Create the OPTIMA API application."""
    application = FastAPI(title="OPTIMA API")
    application.include_router(health_router, prefix="/api/v1")
    return application


app = create_app()
