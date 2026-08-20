"""FastAPI application factory."""

from fastapi import FastAPI

from optima.api.dependencies import ExecutionDependencies
from optima.api.routes.health import router as health_router
from optima.api.routes.runs import build_runs_router


def create_app(
    *,
    execution_dependencies: ExecutionDependencies | None = None,
) -> FastAPI:
    """Create the OPTIMA API application."""
    application = FastAPI(title="OPTIMA API")
    application.include_router(health_router, prefix="/api/v1")
    application.include_router(
        build_runs_router(execution_dependencies),
        prefix="/api/v1",
    )
    return application


app = create_app()
