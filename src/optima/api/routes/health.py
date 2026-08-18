"""Lightweight service health route."""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


class HealthResponse(BaseModel):
    """Stable health response contract."""

    status: Literal["ok"] = "ok"


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Report that the API process is available."""
    return HealthResponse()
