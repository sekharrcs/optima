"""FastAPI application factory."""

from dataclasses import replace

from fastapi import FastAPI

from optima.api.dependencies import ExecutionDependencies
from optima.api.routes.health import router as health_router
from optima.api.routes.runs import build_runs_router
from optima.observability.azure_monitor import build_observability
from optima.observability.resilient import FailureIsolatedObservability


def create_app(
    *,
    execution_dependencies: ExecutionDependencies | None = None,
) -> FastAPI:
    """Create the OPTIMA API application."""
    resolved_dependencies = execution_dependencies
    if resolved_dependencies is not None:
        observability = resolved_dependencies.observability
        if observability is None:
            observability = build_observability(resolved_dependencies.settings)
        if not isinstance(observability, FailureIsolatedObservability):
            observability = FailureIsolatedObservability(observability)
        resolved_dependencies = replace(
            resolved_dependencies,
            observability=observability,
        )

    application = FastAPI(title="OPTIMA API")
    application.include_router(health_router, prefix="/api/v1")
    application.include_router(
        build_runs_router(resolved_dependencies),
        prefix="/api/v1",
    )
    if resolved_dependencies is not None:
        observability = resolved_dependencies.observability
        if observability is None:
            raise AssertionError("resolved observability must be configured")
        observability.instrument_fastapi(application)
    return application


app = create_app()
