"""Microsoft Foundry and APIM model-provider adapter."""

import asyncio
import math
from collections.abc import Mapping
from typing import Protocol
from urllib.parse import urlparse

import httpx
from azure.core.credentials import TokenCredential

from optima.cache.contracts import (
    EmbeddingProviderError,
    EmbeddingProviderResult,
    EmbeddingProviderTimeout,
    SemanticCacheLookupRequest,
)
from optima.domain.embedding import EmbeddingProfile, build_semantic_input
from optima.domain.execution import ModelRole
from optima.domain.run import ModelUsage
from optima.providers.contracts import (
    ModelProvider,
    ModelProviderRequest,
    ModelProviderResult,
    ModelResponseFormat,
    MonotonicClock,
    system_monotonic_time,
    validate_usage_alignment,
)

FOUNDRY_PROVIDER_NAME = "microsoft-foundry-apim"


class FoundryProviderError(RuntimeError):
    """Safe categorized failure from the Foundry provider boundary."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        status_code: int | None = None,
        retry_after_ms: int | None = None,
        outbound_attempted: bool = True,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retry_after_ms = retry_after_ms
        self.outbound_attempted = outbound_attempted


class FoundryAuthentication(Protocol):
    """Produce authentication headers for one outbound Foundry request."""

    async def headers(self) -> Mapping[str, str]:
        """Return authentication headers without exposing credential values."""


class ApiKeyAuthentication:
    """Authenticate with one explicitly configured Azure OpenAI API key."""

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise ValueError("api_key must not be empty")
        self._api_key = api_key

    async def headers(self) -> Mapping[str, str]:
        """Return the Azure OpenAI API-key header."""
        return {"api-key": self._api_key}


class EntraTokenAuthentication:
    """Authenticate with one explicit Azure Identity credential and token scope."""

    def __init__(self, credential: TokenCredential, token_scope: str) -> None:
        if not token_scope:
            raise ValueError("token_scope must not be empty")
        self._credential = credential
        self._token_scope = token_scope

    async def headers(self) -> Mapping[str, str]:
        """Acquire one cached-or-fresh token without blocking the event loop."""
        try:
            access_token = await asyncio.to_thread(
                self._credential.get_token,
                self._token_scope,
            )
        except Exception as error:
            raise FoundryProviderError(
                code="AUTHENTICATION_FAILED",
                message="Microsoft Entra authentication failed.",
                outbound_attempted=False,
            ) from error
        return {"Authorization": f"Bearer {access_token.token}"}


class FoundryModelProvider(ModelProvider):
    """Call one configured Foundry deployment through an Azure OpenAI v1 endpoint."""

    provider_name: str
    deployment_name: str
    model_role: ModelRole

    def __init__(
        self,
        *,
        base_url: str,
        deployment_name: str,
        model_role: ModelRole,
        authentication: FoundryAuthentication,
        client: httpx.AsyncClient,
        provider_name: str = FOUNDRY_PROVIDER_NAME,
        expected_response_model: str | None = None,
        clock: MonotonicClock | None = None,
    ) -> None:
        self._base_url = _normalize_base_url(base_url)
        if not deployment_name:
            raise ValueError("deployment_name must not be empty")
        if not provider_name:
            raise ValueError("provider_name must not be empty")
        self.deployment_name = deployment_name
        self.model_role = model_role
        self.provider_name = provider_name
        if expected_response_model is not None and not expected_response_model:
            raise ValueError("expected_response_model must not be empty")
        self._expected_response_model = expected_response_model
        self._authentication = authentication
        self._client = client
        self._clock = clock

    async def generate(self, request: ModelProviderRequest) -> ModelProviderResult:
        """Execute one non-retried chat-completion request and map measured facts."""
        if request.model_role is not self.model_role:
            raise ValueError(
                "request model role does not match provider role: "
                f"expected {self.model_role.value}, got {request.model_role.value}"
            )

        clock_now = (
            self._clock.now if self._clock is not None else system_monotonic_time
        )
        started_at = clock_now()
        headers = await self._authentication.headers()
        try:
            body: dict[str, object] = {
                "model": self.deployment_name,
                "messages": _messages(request),
            }
            if request.response_format is ModelResponseFormat.JSON_OBJECT:
                body["response_format"] = {"type": "json_object"}
            response = await self._client.post(
                f"{self._base_url}/chat/completions",
                headers=headers,
                json=body,
            )
        except httpx.TimeoutException as error:
            raise TimeoutError("Foundry model request timed out.") from error
        except httpx.TransportError as error:
            raise FoundryProviderError(
                code="TRANSPORT_FAILED",
                message="Foundry model transport failed.",
            ) from error

        if response.is_error:
            if response.status_code in (408, 504):
                raise TimeoutError("Foundry model request timed out.")
            raise _status_error(response)

        latency_ms = max(0, int(round((clock_now() - started_at) * 1000)))
        output_text, request_id, usage_values = _parse_response(
            response,
            expected_model=self._expected_response_model,
        )
        usage = ModelUsage(
            request_id=request_id,
            run_id=request.run_id,
            provider=self.provider_name,
            deployment=self.deployment_name,
            model_role=self.model_role,
            input_tokens=usage_values[0],
            output_tokens=usage_values[1],
            provider_total_tokens=usage_values[2],
            cached_tokens=usage_values[3],
            latency_ms=latency_ms,
        )
        validate_usage_alignment(
            run_id=request.run_id,
            model_role=request.model_role,
            usage=usage,
        )
        return ModelProviderResult(output_text=output_text, usage=usage)


class FoundryEmbeddingProvider:
    """Call one Azure OpenAI v1 embeddings deployment for cache lookups."""

    provider_name: str

    def __init__(
        self,
        *,
        base_url: str,
        profile: EmbeddingProfile,
        authentication: FoundryAuthentication,
        client: httpx.AsyncClient,
        provider_name: str = FOUNDRY_PROVIDER_NAME,
    ) -> None:
        self._base_url = _normalize_base_url(base_url)
        if not provider_name:
            raise ValueError("provider_name must not be empty")
        self._profile = profile
        self.provider_name = provider_name
        self._authentication = authentication
        self._client = client

    @property
    def profile(self) -> EmbeddingProfile:
        """Return the embedding profile this provider produces."""
        return self._profile

    async def embed(
        self,
        request: SemanticCacheLookupRequest,
    ) -> EmbeddingProviderResult:
        """Execute one non-retried embedding request and map measured facts."""
        try:
            headers = await self._authentication.headers()
        except FoundryProviderError as error:
            # Authentication is acquired before any outbound embedding request, so
            # a failure here proves no paid provider request was attempted.
            raise EmbeddingProviderError(
                "Foundry embedding authentication failed.",
                outbound_attempted=False,
            ) from error

        # From this point an outbound request may reach the paid provider, so every
        # failure is reported as a possibly-billed attempt.
        try:
            response = await self._client.post(
                f"{self._base_url}/embeddings",
                headers=headers,
                json={
                    "model": self._profile.deployment,
                    "input": build_semantic_input(
                        input_text=request.input_text,
                        context=request.context,
                    ),
                },
            )
        except httpx.TimeoutException as error:
            raise EmbeddingProviderTimeout(
                "Foundry embedding request timed out.",
            ) from error
        except httpx.TransportError as error:
            raise EmbeddingProviderError(
                "Foundry embedding transport failed.",
                outbound_attempted=True,
            ) from error

        if response.is_error:
            if response.status_code in (408, 504):
                raise EmbeddingProviderTimeout("Foundry embedding request timed out.")
            raise _embedding_status_error(response)

        vector, request_id, input_tokens = _parse_embedding_response(
            response,
            expected_dimension=self._profile.dimension,
            expected_model=self._profile.model,
        )
        return EmbeddingProviderResult(
            vector=vector,
            profile=self._profile,
            provider=self.provider_name,
            request_id=request_id,
            input_tokens=input_tokens,
        )


def _parse_embedding_response(
    response: httpx.Response,
    *,
    expected_dimension: int,
    expected_model: str,
) -> tuple[tuple[float, ...], str | None, int | None]:
    try:
        body = response.json()
    except ValueError as error:
        raise _invalid_embedding_response() from error
    if not isinstance(body, dict):
        raise _invalid_embedding_response()

    # Fail closed unless the provider identity in the response matches the profile
    # the vector will be persisted under; never attach an unverified profile.
    model = body.get("model")
    if not isinstance(model, str) or not model.strip() or model != expected_model:
        raise _invalid_embedding_response()

    data = body.get("data")
    if not isinstance(data, list) or len(data) != 1:
        raise _invalid_embedding_response()
    element = data[0]
    if not isinstance(element, dict):
        raise _invalid_embedding_response()
    index = element.get("index")
    if type(index) is not int or index != 0:
        raise _invalid_embedding_response()
    raw_vector = element.get("embedding")
    if not isinstance(raw_vector, list) or len(raw_vector) != expected_dimension:
        raise _invalid_embedding_response()
    vector: list[float] = []
    for value in raw_vector:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise _invalid_embedding_response()
        number = float(value)
        if not math.isfinite(number):
            raise _invalid_embedding_response()
        vector.append(number)

    # The request-correlation id is optional; an embedding id is not a request id.
    request_id = next(
        (
            value
            for header in ("apim-request-id", "x-ms-request-id", "x-request-id")
            if (value := response.headers.get(header))
        ),
        None,
    )
    return tuple(vector), request_id, _parse_embedding_input_tokens(body.get("usage"))


def _parse_embedding_input_tokens(usage: object) -> int | None:
    if usage is None:
        return None
    if not isinstance(usage, dict):
        raise _invalid_embedding_response()
    prompt_tokens = _embedding_usage_count(usage.get("prompt_tokens"))
    total_tokens = _embedding_usage_count(usage.get("total_tokens"))
    # Embeddings consume input only, so an OpenAI-compatible response must report
    # total_tokens equal to prompt_tokens whenever both are present.
    if (
        prompt_tokens is not None
        and total_tokens is not None
        and total_tokens != prompt_tokens
    ):
        raise _invalid_embedding_response()
    if prompt_tokens is None:
        return None
    return prompt_tokens


def _embedding_usage_count(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or type(value) is not int or value < 0:
        raise _invalid_embedding_response()
    return value


def _embedding_status_error(response: httpx.Response) -> EmbeddingProviderError:
    return EmbeddingProviderError(
        "Foundry rejected the embedding request.",
        outbound_attempted=True,
    )


def _invalid_embedding_response() -> "EmbeddingProviderError":
    return EmbeddingProviderError(
        "Foundry returned an invalid embedding response.",
        outbound_attempted=True,
    )


def _normalize_base_url(base_url: str) -> str:
    # Reject control characters and whitespace before parsing so a crafted
    # endpoint cannot smuggle newlines into request lines or hide an authority.
    if any(
        character.isspace() or _is_forbidden_url_control(character)
        for character in base_url
    ):
        raise ValueError(
            "Foundry base URL must not contain control characters or whitespace"
        )
    normalized = base_url.rstrip("/")
    parsed = urlparse(normalized)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.params
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/").endswith("/openai/v1") is False
    ):
        raise ValueError(
            "Foundry base URL must be an absolute HTTPS /openai/v1 API root"
        )
    return normalized


def _is_forbidden_url_control(character: str) -> bool:
    """Return whether a character is a C0/C1 control or DEL code point."""
    code_point = ord(character)
    return code_point < 0x20 or 0x7F <= code_point <= 0x9F


def _messages(request: ModelProviderRequest) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    if request.system_instruction is not None:
        messages.append({"role": "system", "content": request.system_instruction})
    if request.context is not None:
        messages.append({"role": "user", "content": request.context})
    messages.append({"role": "user", "content": request.input_text})
    return messages


def _status_error(response: httpx.Response) -> FoundryProviderError:
    retry_after_ms = _retry_after_ms(response.headers)
    if response.status_code in {401, 403}:
        code = "AUTHENTICATION_FAILED"
        message = "Foundry authentication or authorization was rejected."
    elif response.status_code == 429:
        code = "THROTTLED"
        message = "Foundry request was throttled."
    elif response.status_code >= 500:
        code = "SERVICE_UNAVAILABLE"
        message = "Foundry service request failed."
    else:
        code = "REQUEST_REJECTED"
        message = "Foundry rejected the model request."
    return FoundryProviderError(
        code=code,
        message=message,
        status_code=response.status_code,
        retry_after_ms=retry_after_ms,
    )


def _retry_after_ms(headers: httpx.Headers) -> int | None:
    milliseconds = headers.get("retry-after-ms")
    if milliseconds is not None and milliseconds.isdigit():
        return int(milliseconds)
    seconds = headers.get("retry-after")
    if seconds is not None and seconds.isdigit():
        return int(seconds) * 1000
    return None


def _parse_response(
    response: httpx.Response,
    *,
    expected_model: str | None = None,
) -> tuple[str, str | None, tuple[int | None, int | None, int | None, int | None]]:
    try:
        body = response.json()
    except ValueError as error:
        raise _invalid_response() from error
    if not isinstance(body, dict):
        raise _invalid_response()
    if expected_model is not None and body.get("model") != expected_model:
        raise _invalid_response()

    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise _invalid_response()
    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        raise _invalid_response()
    message = first_choice.get("message")
    if not isinstance(message, dict):
        raise _invalid_response()
    output_text = message.get("content")
    if not isinstance(output_text, str) or not output_text.strip():
        raise _invalid_response()

    # The request-correlation id is optional; a completion id is not a request id.
    request_id = next(
        (
            value
            for header in ("apim-request-id", "x-ms-request-id", "x-request-id")
            if (value := response.headers.get(header))
        ),
        None,
    )

    return output_text, request_id, _parse_usage(body.get("usage"))


def _parse_usage(
    usage: object,
) -> tuple[int | None, int | None, int | None, int | None]:
    if usage is None:
        return None, None, None, None
    if not isinstance(usage, dict):
        raise _invalid_response()
    prompt_tokens = _optional_count(usage, "prompt_tokens")
    completion_tokens = _optional_count(usage, "completion_tokens")
    total_tokens = _optional_count(usage, "total_tokens")
    if (
        prompt_tokens is not None
        and completion_tokens is not None
        and total_tokens is not None
        and total_tokens != prompt_tokens + completion_tokens
    ):
        raise _invalid_response()

    details = usage.get("prompt_tokens_details")
    if details is None:
        cached_tokens = None
    elif isinstance(details, dict):
        cached_tokens = _optional_count(details, "cached_tokens")
    else:
        raise _invalid_response()
    if (
        cached_tokens is not None
        and prompt_tokens is not None
        and cached_tokens > prompt_tokens
    ):
        raise _invalid_response()
    return prompt_tokens, completion_tokens, total_tokens, cached_tokens


def _optional_count(values: dict[object, object], field_name: str) -> int | None:
    value = values.get(field_name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _invalid_response()
    return value


def _invalid_response() -> FoundryProviderError:
    return FoundryProviderError(
        code="INVALID_RESPONSE",
        message="Foundry returned an invalid model response.",
    )
