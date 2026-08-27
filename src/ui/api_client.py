"""HTTP client for the versioned OPTIMA execution API."""

import os
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import ValidationError

from optima.api.models import RunRequest
from optima.domain.run import RunResult

DEFAULT_API_BASE_URL = "http://127.0.0.1:8000"
API_BASE_URL_ENV = "OPTIMA_API_BASE_URL"
API_TIMEOUT_SECONDS_ENV = "OPTIMA_API_TIMEOUT_SECONDS"
PRODUCTION_MODE_ENV = "OPTIMA_UI_PRODUCTION_MODE"
LOCAL_API_TIMEOUT_SECONDS = 30.0
MAX_API_TIMEOUT_SECONDS = 360.0


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
        timeout_seconds: float = LOCAL_API_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
        require_https: bool = False,
    ) -> None:
        self._base_url = _validate_api_base_url(
            base_url,
            require_https=require_https,
        )
        self._timeout = httpx.Timeout(timeout_seconds, connect=5.0)
        self._transport = transport

    @classmethod
    def from_environment(
        cls,
        *,
        timeout_seconds: float | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> "OptimaApiClient":
        """Build a client from trusted process configuration only."""
        production_mode = _production_mode_from_environment()
        configured_base_url = os.getenv(API_BASE_URL_ENV)
        if production_mode and configured_base_url is None:
            raise ValueError(
                f"{API_BASE_URL_ENV} is required when {PRODUCTION_MODE_ENV}=true"
            )
        configured_timeout = os.getenv(API_TIMEOUT_SECONDS_ENV)
        effective_timeout = timeout_seconds
        if effective_timeout is None:
            effective_timeout = (
                _configured_timeout_seconds(configured_timeout)
                if configured_timeout is not None
                else LOCAL_API_TIMEOUT_SECONDS
            )
        return cls(
            configured_base_url or DEFAULT_API_BASE_URL,
            timeout_seconds=effective_timeout,
            transport=transport,
            require_https=production_mode,
        )

    def execute(self, request: RunRequest) -> RunResult:
        """Execute one request and return only a validated RunResult."""
        try:
            with httpx.Client(
                base_url=self._base_url,
                timeout=self._timeout,
                transport=self._transport,
                follow_redirects=False,
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

        if response.is_redirect:
            raise ApiClientError(
                code="API_REDIRECT_REJECTED",
                message="The configured OPTIMA API returned a disallowed redirect.",
                status_code=response.status_code,
            )

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


def _production_mode_from_environment() -> bool:
    """Read the explicit production mode without permissive coercion."""
    value = os.getenv(PRODUCTION_MODE_ENV, "false").casefold()
    if value not in {"true", "false"}:
        raise ValueError(f"{PRODUCTION_MODE_ENV} must be true or false")
    return value == "true"


def _configured_timeout_seconds(value: str) -> float:
    """Parse one finite positive UI transport timeout within a fixed ceiling."""
    try:
        timeout_seconds = float(value)
    except ValueError as error:
        raise ValueError(f"{API_TIMEOUT_SECONDS_ENV} must be a number") from error
    if not 0 < timeout_seconds <= MAX_API_TIMEOUT_SECONDS:
        raise ValueError(
            f"{API_TIMEOUT_SECONDS_ENV} must be greater than 0 and at most "
            f"{MAX_API_TIMEOUT_SECONDS:g}"
        )
    return timeout_seconds


def _validate_api_base_url(base_url: str, *, require_https: bool) -> str:
    """Validate and normalize the exact URL that HTTPX will use."""
    if not base_url or any(character.isspace() for character in base_url):
        raise ValueError("API base URL must be an absolute HTTP(S) root URL")
    try:
        parsed = httpx.URL(base_url)
    except httpx.InvalidURL as error:
        raise ValueError("API base URL must be an absolute HTTP(S) root URL") from error
    allowed_schemes = {"https"} if require_https else {"http", "https"}
    if (
        not parsed.is_absolute_url
        or parsed.scheme not in allowed_schemes
        or not parsed.host
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        requirement = "HTTPS" if require_https else "HTTP(S)"
        raise ValueError(f"API base URL must be an absolute {requirement} root URL")
    return str(parsed).rstrip("/")


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
