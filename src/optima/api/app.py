"""FastAPI application factory."""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import replace

from fastapi import FastAPI

from optima.api.dependencies import ExecutionDependencies
from optima.api.routes.health import router as health_router
from optima.api.routes.runs import ExecutionDependencyResolver, build_runs_router
from optima.api.security import (
    MAX_REQUEST_BODY_BYTES,
    ExecutionConcurrencyLimiter,
    RequestBodyLimitMiddleware,
)
from optima.observability.azure_monitor import build_observability
from optima.observability.resilient import FailureIsolatedObservability


def create_app(
    *,
    execution_dependencies: ExecutionDependencies | None = None,
    execution_dependency_resolver: ExecutionDependencyResolver | None = None,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
) -> FastAPI:
    """Create the OPTIMA API application."""
    if execution_dependencies is not None and execution_dependency_resolver is not None:
        raise ValueError("Configure dependencies or a resolver, not both")
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
            execution_limiter=(
                resolved_dependencies.execution_limiter
                or ExecutionConcurrencyLimiter(
                    resolved_dependencies.settings.execution_concurrency_limit
                )
            ),
        )

    application = FastAPI(title="OPTIMA API", lifespan=lifespan)
    application.add_middleware(
        RequestBodyLimitMiddleware,
        maximum_body_bytes=MAX_REQUEST_BODY_BYTES,
    )
    application.include_router(health_router, prefix="/api/v1")
    application.include_router(
        build_runs_router(execution_dependency_resolver or resolved_dependencies),
        prefix="/api/v1",
    )
    if resolved_dependencies is not None:
        observability = resolved_dependencies.observability
        if observability is None:
            raise AssertionError("resolved observability must be configured")
        observability.instrument_fastapi(application)
    return application


app = create_app()
