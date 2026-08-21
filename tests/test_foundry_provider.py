"""Tests for the Microsoft Foundry and APIM model-provider adapter."""

import asyncio
import json
from collections.abc import Callable, Sequence

import httpx
import pytest
from azure.core.credentials import AccessToken, TokenCredential

from optima.api.app import create_app
from optima.api.demo import create_demo_app
from optima.api.dependencies import build_foundry_provider_pair
from optima.config import AppSettings, FoundryAuthMode
from optima.domain.execution import ModelRole
from optima.providers import (
    ApiKeyAuthentication,
    EntraTokenAuthentication,
    FoundryModelProvider,
    FoundryProviderError,
    ModelProvider,
    ModelProviderRequest,
    ModelProviderResult,
)


class ScriptedClock:
    """Deterministic monotonic clock for provider latency assertions."""

    def __init__(self, timestamps: Sequence[float]) -> None:
        self._timestamps = iter(timestamps)

    def now(self) -> float:
        return next(self._timestamps)


class FakeTokenCredential(TokenCredential):
    """Record requested scopes and return one non-secret fake access token."""

    def __init__(self, *, failure: Exception | None = None) -> None:
        self.scopes: list[tuple[str, ...]] = []
        self.closed = False
        self._failure = failure

    def get_token(self, *scopes: str, **kwargs: object) -> AccessToken:
        self.scopes.append(scopes)
        if self._failure is not None:
            raise self._failure
        return AccessToken("fake-entra-token", 4_102_444_800)

    def close(self) -> None:
        self.closed = True


class FailingCloseTransport(httpx.AsyncBaseTransport):
    """Fail during shutdown after accepting no model requests."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=successful_response())

    async def aclose(self) -> None:
        raise RuntimeError("transport close failed")


def provider_request(
    role: ModelRole, *, context: str | None = None
) -> ModelProviderRequest:
    """Build one provider-independent request."""
    return ModelProviderRequest(
        run_id="run-foundry-1",
        model_role=role,
        input_text="Answer the request",
        context=context,
    )


def successful_response(**updates: object) -> dict[str, object]:
    """Build one Azure OpenAI chat-completion response."""
    response: dict[str, object] = {
        "id": "chatcmpl-provider-1",
        "choices": [{"message": {"role": "assistant", "content": "Result"}}],
        "usage": {
            "prompt_tokens": 17,
            "completion_tokens": 5,
            "total_tokens": 22,
            "prompt_tokens_details": {"cached_tokens": 3},
        },
    }
    response.update(updates)
    return response


async def generate_with_transport(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    role: ModelRole = ModelRole.SMALL,
    deployment: str = "small-deployment",
    authentication: ApiKeyAuthentication | EntraTokenAuthentication | None = None,
    context: str | None = None,
) -> ModelProviderResult:
    """Execute one provider call with an in-memory HTTP transport."""
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = FoundryModelProvider(
            base_url="https://gateway.example/openai/v1/",
            deployment_name=deployment,
            model_role=role,
            authentication=authentication or ApiKeyAuthentication("fake-api-key"),
            client=client,
            clock=ScriptedClock([10.0, 10.012]),
        )
        return await provider.generate(provider_request(role, context=context))


@pytest.mark.parametrize(
    ("role", "deployment"),
    [
        (ModelRole.SMALL, "small-deployment"),
        (ModelRole.STRONG, "strong-deployment"),
    ],
)
def test_provider_maps_role_deployment_request_and_full_usage(
    role: ModelRole,
    deployment: str,
) -> None:
    """Send the configured deployment and preserve every reported usage category."""
    captured: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=successful_response())

    result = asyncio.run(
        generate_with_transport(
            handle,
            role=role,
            deployment=deployment,
            context="Supporting context",
        )
    )

    assert len(captured) == 1
    request = captured[0]
    assert request.url == "https://gateway.example/openai/v1/chat/completions"
    assert request.headers["api-key"] == "fake-api-key"
    assert json.loads(request.content) == {
        "model": deployment,
        "messages": [
            {"role": "user", "content": "Supporting context"},
            {"role": "user", "content": "Answer the request"},
        ],
    }
    assert result.output_text == "Result"
    assert result.usage.model_role is role
    assert result.usage.deployment == deployment
    assert result.usage.request_id == "chatcmpl-provider-1"
    assert result.usage.input_tokens == 17
    assert result.usage.output_tokens == 5
    assert result.usage.provider_total_tokens == 22
    assert result.usage.cached_tokens == 3
    assert result.usage.latency_ms == 12
    assert result.usage.calculated_cost is None


def test_entra_authentication_uses_only_configured_credential_and_scope() -> None:
    """Acquire one token from the injected explicit credential and send it as bearer."""
    credential = FakeTokenCredential()
    captured: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=successful_response())

    asyncio.run(
        generate_with_transport(
            handle,
            authentication=EntraTokenAuthentication(
                credential,
                "api://optima-apim/.default",
            ),
        )
    )

    assert credential.scopes == [("api://optima-apim/.default",)]
    assert captured[0].headers["Authorization"] == "Bearer fake-entra-token"
    assert "api-key" not in captured[0].headers


def test_missing_optional_usage_remains_unavailable() -> None:
    """Do not invent token counts when a successful provider omits usage."""
    result = asyncio.run(
        generate_with_transport(
            lambda request: httpx.Response(
                200,
                json=successful_response(usage=None),
            )
        )
    )

    assert result.usage.input_tokens is None
    assert result.usage.output_tokens is None
    assert result.usage.provider_total_tokens is None
    assert result.usage.cached_tokens is None


def test_response_header_supplies_request_id_when_body_omits_it() -> None:
    """Preserve gateway request identity without fabricating an adapter identifier."""
    result = asyncio.run(
        generate_with_transport(
            lambda request: httpx.Response(
                200,
                headers={"apim-request-id": "apim-request-7"},
                json=successful_response(id=None),
            )
        )
    )

    assert result.usage.request_id == "apim-request-7"


def test_request_id_prefers_request_header_over_completion_id() -> None:
    """Record the provider request-correlation id, not the chat-completion id."""
    result = asyncio.run(
        generate_with_transport(
            lambda request: httpx.Response(
                200,
                headers={"apim-request-id": "apim-request-9"},
                json=successful_response(),
            )
        )
    )

    assert result.usage.request_id == "apim-request-9"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not-json"),
        httpx.Response(200, json={"id": "request-1", "choices": []}),
        httpx.Response(
            200,
            json=successful_response(
                usage={"prompt_tokens": -1, "completion_tokens": 1}
            ),
        ),
        httpx.Response(200, json=successful_response(id=None)),
        httpx.Response(
            200,
            json=successful_response(
                usage={
                    "prompt_tokens": 17,
                    "completion_tokens": 5,
                    "total_tokens": 99,
                }
            ),
        ),
        httpx.Response(
            200,
            json=successful_response(
                usage={"prompt_tokens": True, "completion_tokens": 5}
            ),
        ),
        httpx.Response(
            200,
            json=successful_response(
                usage={
                    "prompt_tokens": 5,
                    "completion_tokens": 5,
                    "total_tokens": 10,
                    "prompt_tokens_details": {"cached_tokens": 9},
                }
            ),
        ),
        httpx.Response(
            200,
            json=successful_response(
                choices=[{"message": {"role": "assistant", "content": "   "}}]
            ),
        ),
    ],
)
def test_provider_rejects_malformed_success_responses(
    response: httpx.Response,
) -> None:
    """Reject incomplete or invalid provider data instead of creating run evidence."""
    with pytest.raises(FoundryProviderError) as captured:
        asyncio.run(generate_with_transport(lambda request: response))

    assert captured.value.code == "INVALID_RESPONSE"


@pytest.mark.parametrize(
    ("status_code", "expected_code"),
    [
        (400, "REQUEST_REJECTED"),
        (401, "AUTHENTICATION_FAILED"),
        (403, "AUTHENTICATION_FAILED"),
        (408, "REQUEST_REJECTED"),
        (409, "REQUEST_REJECTED"),
        (429, "THROTTLED"),
        (500, "SERVICE_UNAVAILABLE"),
        (503, "SERVICE_UNAVAILABLE"),
    ],
)
def test_provider_categorizes_http_failures_without_retrying(
    status_code: int,
    expected_code: str,
) -> None:
    """Surface one safe status category after exactly one physical request."""
    request_count = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            status_code,
            headers={"retry-after-ms": "250"},
            json={"error": {"message": "credential-or-service-detail"}},
        )

    with pytest.raises(FoundryProviderError) as captured:
        asyncio.run(generate_with_transport(handle))

    assert request_count == 1
    assert captured.value.code == expected_code
    assert captured.value.status_code == status_code
    assert captured.value.retry_after_ms == 250
    assert "credential-or-service-detail" not in str(captured.value)


@pytest.mark.parametrize(
    ("transport_error", "expected_exception"),
    [
        (httpx.ReadTimeout("slow"), TimeoutError),
        (httpx.ConnectError("offline"), FoundryProviderError),
    ],
)
def test_provider_maps_transport_failures(
    transport_error: Exception,
    expected_exception: type[Exception],
) -> None:
    """Keep timeout handling compatible with the executor and categorize other I/O."""

    def fail(request: httpx.Request) -> httpx.Response:
        raise transport_error

    with pytest.raises(expected_exception):
        asyncio.run(generate_with_transport(fail))


def test_entra_failure_stops_before_model_transport() -> None:
    """Do not attempt a model request after explicit credential failure."""
    request_count = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(200, json=successful_response())

    authentication = EntraTokenAuthentication(
        FakeTokenCredential(failure=RuntimeError("secret diagnostic")),
        "api://optima-apim/.default",
    )
    with pytest.raises(FoundryProviderError) as captured:
        asyncio.run(generate_with_transport(handle, authentication=authentication))

    assert request_count == 0
    assert captured.value.code == "AUTHENTICATION_FAILED"
    assert "secret diagnostic" not in str(captured.value)


def test_provider_rejects_wrong_role_before_transport() -> None:
    """Keep conceptual role binding authoritative at the provider boundary."""
    request_count = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(200, json=successful_response())

    async def execute() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            provider = FoundryModelProvider(
                base_url="https://gateway.example/openai/v1",
                deployment_name="small-deployment",
                model_role=ModelRole.SMALL,
                authentication=ApiKeyAuthentication("fake-api-key"),
                client=client,
            )
            await provider.generate(provider_request(ModelRole.STRONG))

    with pytest.raises(ValueError, match="expected SMALL, got STRONG"):
        asyncio.run(execute())

    assert request_count == 0


def test_foundry_provider_implements_existing_protocol() -> None:
    """Keep the Azure adapter substitutable for current executor dependencies."""
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=successful_response())
        )
    )
    provider = FoundryModelProvider(
        base_url="https://gateway.example/openai/v1",
        deployment_name="small-deployment",
        model_role=ModelRole.SMALL,
        authentication=ApiKeyAuthentication("fake-api-key"),
        client=client,
    )

    assert isinstance(provider, ModelProvider)
    asyncio.run(client.aclose())


@pytest.mark.parametrize(
    "base_url",
    [
        "http://gateway.example/openai/v1",
        "https://gateway.example",
        "https://gateway.example/openai",
        "https://gateway.example/OPENAI/V1",
        "https://gateway.example/openai/v1/chat/completions",
        "https://user:password@gateway.example/openai/v1",
    ],
)
def test_provider_rejects_base_url_outside_v1_api_root(base_url: str) -> None:
    """Fail before transport when the configured URL is not the v1 API root."""
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: pytest.fail("invalid base URL must not reach transport")
        )
    )

    with pytest.raises(ValueError, match="/openai/v1 API root"):
        FoundryModelProvider(
            base_url=base_url,
            deployment_name="small-deployment",
            model_role=ModelRole.SMALL,
            authentication=ApiKeyAuthentication("fake-api-key"),
            client=client,
        )

    asyncio.run(client.aclose())


def foundry_settings(
    auth_mode: FoundryAuthMode,
    **updates: object,
) -> AppSettings:
    """Build one complete provider composition for dependency tests."""
    values: dict[str, object] = {
        "foundry_base_url": "https://gateway.example/openai/v1",
        "foundry_small_deployment": "small-deployment",
        "foundry_strong_deployment": "strong-deployment",
        "foundry_auth_mode": auth_mode,
    }
    values.update(updates)
    return AppSettings.model_validate(values)


def test_api_key_composition_creates_no_azure_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compose API-key providers without probing local or deployed identity."""
    monkeypatch.setattr(
        "optima.api.dependencies.AzureCliCredential",
        lambda: pytest.fail("Azure CLI credential must not be created"),
    )
    monkeypatch.setattr(
        "optima.api.dependencies.ManagedIdentityCredential",
        lambda **kwargs: pytest.fail("managed identity must not be created"),
    )
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=successful_response())

    pair = build_foundry_provider_pair(
        foundry_settings(FoundryAuthMode.API_KEY, foundry_api_key="fake-key"),
        transport=httpx.MockTransport(handle),
        monotonic_clock=ScriptedClock([1.0, 1.001, 2.0, 2.001]),
    )

    async def execute() -> None:
        await pair.small_provider.generate(provider_request(ModelRole.SMALL))
        await pair.strong_provider.generate(provider_request(ModelRole.STRONG))
        await pair.aclose()

    asyncio.run(execute())

    assert pair.credential is None
    assert [json.loads(request.content)["model"] for request in requests] == [
        "small-deployment",
        "strong-deployment",
    ]
    assert all(request.headers["api-key"] == "fake-key" for request in requests)


@pytest.mark.parametrize(
    ("auth_mode", "settings_updates", "expected_client_id"),
    [
        (
            FoundryAuthMode.AZURE_CLI,
            {"foundry_token_scope": "api://optima-apim/.default"},
            None,
        ),
        (
            FoundryAuthMode.MANAGED_IDENTITY,
            {"foundry_token_scope": "api://optima-apim/.default"},
            None,
        ),
        (
            FoundryAuthMode.MANAGED_IDENTITY,
            {
                "foundry_token_scope": "api://optima-apim/.default",
                "foundry_managed_identity_client_id": "managed-client-id",
            },
            "managed-client-id",
        ),
    ],
)
def test_entra_composition_creates_only_selected_credential(
    monkeypatch: pytest.MonkeyPatch,
    auth_mode: FoundryAuthMode,
    settings_updates: dict[str, str],
    expected_client_id: str | None,
) -> None:
    """Select the configured local or deployed identity without chain fallback."""
    created: list[tuple[str, str | None]] = []
    credential = FakeTokenCredential()

    def cli_credential() -> FakeTokenCredential:
        created.append(("AZURE_CLI", None))
        return credential

    def managed_credential(*, client_id: str | None) -> FakeTokenCredential:
        created.append(("MANAGED_IDENTITY", client_id))
        return credential

    monkeypatch.setattr(
        "optima.api.dependencies.AzureCliCredential",
        cli_credential,
    )
    monkeypatch.setattr(
        "optima.api.dependencies.ManagedIdentityCredential",
        managed_credential,
    )
    pair = build_foundry_provider_pair(
        foundry_settings(auth_mode, **settings_updates),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=successful_response())
        ),
        monotonic_clock=ScriptedClock([1.0, 1.001]),
    )

    asyncio.run(pair.small_provider.generate(provider_request(ModelRole.SMALL)))
    asyncio.run(pair.aclose())

    assert created == [(auth_mode.value, expected_client_id)]
    assert pair.credential is credential
    assert credential.scopes == [("api://optima-apim/.default",)]


def test_pair_closes_credential_when_http_client_close_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Release the selected identity resource even when transport cleanup fails."""
    credential = FakeTokenCredential()
    monkeypatch.setattr(
        "optima.api.dependencies.AzureCliCredential",
        lambda: credential,
    )
    pair = build_foundry_provider_pair(
        foundry_settings(
            FoundryAuthMode.AZURE_CLI,
            foundry_token_scope="api://optima-apim/.default",
        ),
        transport=FailingCloseTransport(),
    )

    with pytest.raises(RuntimeError, match="transport close failed"):
        asyncio.run(pair.aclose())

    assert credential.closed is True


def test_default_api_and_demo_creation_do_not_create_azure_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep import and explicit local paths free from Azure credential probing."""
    monkeypatch.setattr(
        "optima.api.dependencies.AzureCliCredential",
        lambda: pytest.fail("default app must not create Azure CLI credential"),
    )
    monkeypatch.setattr(
        "optima.api.dependencies.ManagedIdentityCredential",
        lambda **kwargs: pytest.fail("default app must not create managed identity"),
    )

    assert create_app() is not None
    assert create_demo_app() is not None


def test_composition_requires_explicit_foundry_settings() -> None:
    """Reject cloud composition when the optional configuration is absent."""
    with pytest.raises(ValueError, match="not configured"):
        build_foundry_provider_pair(AppSettings())
