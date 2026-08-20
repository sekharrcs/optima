"""HTTP client for the versioned OPTIMA execution API."""

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import ValidationError

from optima.api.models import RunRequest
from optima.domain.run import RunResult

DEFAULT_API_BASE_URL = "http://127.0.0.1:8000"


@dataclass
class ApiClientError(Exception):
    """A display-safe API or transport failure."""

    code: str
    message: str
    status_code: int | None = None
    facts: dict[str, object] | None = None

    def __str__(self) -> str:
        return self.message


class OptimaApiClient:
    """Call the FastAPI boundary with explicit timeouts and strict parsing."""

    def __init__(
        self,
        base_url: str = DEFAULT_API_BASE_URL,
        *,
        timeout_seconds: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        normalized = base_url.rstrip("/")
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("API base URL must be an absolute HTTP(S) URL")
        self._base_url = normalized
        self._timeout = httpx.Timeout(timeout_seconds, connect=5.0)
        self._transport = transport

    def execute(self, request: RunRequest) -> RunResult:
        """Execute one request and return only a validated RunResult."""
        try:
            with httpx.Client(
                base_url=self._base_url,
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                response = client.post(
                    "/api/v1/runs",
                    json=request.model_dump(mode="json", exclude_none=True),
                )
        except httpx.TimeoutException as error:
            raise ApiClientError(
                code="API_TIMEOUT",
                message="OPTIMA execution timed out before a response was received.",
            ) from error
        except httpx.TransportError as error:
            raise ApiClientError(
                code="API_CONNECTION_FAILED",
                message="Could not connect to the configured OPTIMA API.",
            ) from error

        try:
            body: Any = response.json()
        except ValueError as error:
            raise ApiClientError(
                code="INVALID_API_RESPONSE",
                message="The OPTIMA API returned a non-JSON response.",
                status_code=response.status_code,
            ) from error

        if response.is_error:
            raise _structured_error(response.status_code, body)
        try:
            return _parse_run_result(body)
        except (ValidationError, ValueError) as error:
            raise ApiClientError(
                code="INVALID_RUN_RESULT",
                message="The OPTIMA API response did not match the RunResult contract.",
                status_code=response.status_code,
            ) from error


_COMPUTED_RUN_FIELDS = (
    "total_input_tokens",
    "total_output_tokens",
    "total_tokens",
    "total_calculated_cost",
    "total_cost_provenance",
)


def _parse_run_result(body: object) -> RunResult:
    """Validate stored fields and verify serialized computed totals."""
    if not isinstance(body, dict):
        raise ValueError("RunResult response must be a JSON object")
    stored_fields = {
        key: value for key, value in body.items() if key not in _COMPUTED_RUN_FIELDS
    }
    result = RunResult.model_validate(stored_fields)
    serialized_result = result.model_dump(mode="json")
    for field_name in _COMPUTED_RUN_FIELDS:
        if field_name in body and body[field_name] != serialized_result[field_name]:
            raise ValueError(f"computed RunResult field mismatch: {field_name}")
    return result


def _structured_error(status_code: int, body: object) -> ApiClientError:
    """Parse stable API errors and FastAPI validation failures."""
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, dict):
            code = detail.get("code")
            message = detail.get("message")
            facts = detail.get("facts")
            if isinstance(code, str) and isinstance(message, str):
                return ApiClientError(
                    code=code,
                    message=message,
                    status_code=status_code,
                    facts=facts if isinstance(facts, dict) else None,
                )
        if isinstance(detail, list):
            return ApiClientError(
                code="REQUEST_VALIDATION_FAILED",
                message="The OPTIMA API rejected the supplied request fields.",
                status_code=status_code,
                facts={"errors": detail},
            )
    return ApiClientError(
        code="API_REQUEST_FAILED",
        message=f"The OPTIMA API returned HTTP {status_code}.",
        status_code=status_code,
    )
