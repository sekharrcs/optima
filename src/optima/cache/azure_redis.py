"""Azure Managed Redis composition with explicit authentication ownership."""

import asyncio
import importlib
import inspect
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, cast

from azure.core.credentials import AccessToken
from azure.core.exceptions import (
    ClientAuthenticationError,
    HttpResponseError,
    ServiceRequestError,
    ServiceResponseError,
)
from azure.identity.aio import AzureCliCredential, ManagedIdentityCredential

from optima.cache.contracts import SemanticCacheEmbeddingProvider
from optima.cache.redis import (
    RedisSearchClient,
    RedisSemanticCache,
)
from optima.config import AppSettings, RedisAuthMode, RedisSemanticCacheConfiguration

AZURE_MANAGED_REDIS_PORT = 10_000
AZURE_MANAGED_REDIS_SCOPE = "https://redis.azure.com/.default"
TOKEN_REFRESH_RATIO = 0.7
DEFAULT_TOKEN_RENEWAL_ATTEMPTS = 3
MAX_TOKEN_RENEWAL_ATTEMPTS = 10
DEFAULT_TOKEN_RETRY_BACKOFF_SECONDS = 0.5
DEFAULT_TOKEN_RETRY_BACKOFF_CAP_SECONDS = 5.0
DEFAULT_TOKEN_ACQUISITION_TIMEOUT_SECONDS = 10.0
DEFAULT_TOKEN_REAUTH_TIMEOUT_SECONDS = 10.0
# Azure Cache for Redis / Azure Managed Redis guidance: send a renewed AUTH token
# at least three minutes before expiry to avoid disrupting connections.
DEFAULT_TOKEN_EXPIRY_SAFETY_MARGIN_SECONDS = 180.0

# HTTP statuses from Azure Identity that represent transient, retryable failures.
_TRANSIENT_TOKEN_HTTP_STATUS = frozenset({408, 429, 500, 502, 503, 504})

SleepCallable = Callable[[float], Awaitable[None]]
JitterCallable = Callable[[float], float]


def _attempt_fits_before_deadline(
    *,
    remaining_seconds: float,
    delay_seconds: float,
    operation_timeout_seconds: float,
    safety_margin_seconds: float,
) -> bool:
    """Return whether one delayed, bounded attempt finishes before the deadline.

    The complete next-attempt budget is the actual (jittered) retry delay plus
    the operation's bounded timeout plus the pre-expiry safety margin. An attempt
    is only safe when that whole budget still fits inside the reference token's
    remaining lifetime, so a retry can never begin yet cross the safe deadline
    while blocked in acquisition or reauthentication.
    """
    budget = delay_seconds + operation_timeout_seconds + safety_margin_seconds
    return budget <= remaining_seconds


def _is_transient_token_error(error: BaseException) -> bool:
    """Return whether a token-acquisition failure should be retried.

    Authentication and authorization failures are permanent even though
    ``ClientAuthenticationError`` subclasses ``HttpResponseError``, so they are
    classified first. Only transport faults, throttling, and transient server
    statuses are treated as retryable.
    """
    if isinstance(error, ClientAuthenticationError):
        return False
    if isinstance(error, ServiceRequestError | ServiceResponseError):
        return True
    if isinstance(error, HttpResponseError):
        return error.status_code in _TRANSIENT_TOKEN_HTTP_STATUS
    return isinstance(error, TimeoutError | ConnectionError)


def _default_token_jitter(base_seconds: float) -> float:
    """Return uniform jitter in the closed interval [0, base_seconds]."""
    return random.random() * base_seconds


class _RedisConnectionPool(Protocol):
    async def re_auth_callback(self, token: "AzureRedisToken") -> None:
        """Reauthenticate idle connections and mark active connections."""
        ...


class _AsyncAzureCredential(Protocol):
    async def get_token(self, *scopes: str, **kwargs: object) -> AccessToken:
        """Acquire an access token for the requested scopes."""
        ...

    async def close(self) -> None:
        """Release credential resources."""
        ...


class _AsyncRedisClient(RedisSearchClient, Protocol):
    @property
    def connection_pool(self) -> _RedisConnectionPool:
        """Return the pool that receives renewed access tokens."""
        ...

    async def aclose(self) -> None:
        """Close all client connections."""
        ...


TokenCallback = Callable[["AzureRedisToken"], Awaitable[None] | None]
ErrorCallback = Callable[[Exception], Awaitable[None] | None]
CredentialFactory = Callable[[], _AsyncAzureCredential]


@dataclass(frozen=True)
class AzureRedisToken:
    """Redis-compatible access token with a validated configured object ID."""

    value: str = field(repr=False)
    object_id: str
    expires_at_ms: float
    received_at_ms: float

    def is_expired(self) -> bool:
        """Return whether the access token has reached its expiry."""
        return self.ttl() <= 0

    def ttl(self) -> float:
        """Return remaining token lifetime in milliseconds."""
        return self.expires_at_ms - _utc_now_ms()

    def try_get(self, key: str) -> str | None:
        """Expose only the Redis AUTH object-ID claim."""
        return self.object_id if key == "oid" else None

    def get_value(self) -> str:
        """Return the secret access-token value for Redis AUTH."""
        return self.value

    def get_expires_at_ms(self) -> float:
        """Return the token expiry as Unix epoch milliseconds."""
        return self.expires_at_ms

    def get_received_at_ms(self) -> float:
        """Return local acquisition time as Unix epoch milliseconds."""
        return self.received_at_ms


class AzureRedisCredentialProvider:
    """Acquire and renew Redis credentials from one explicit Azure credential."""

    def __init__(
        self,
        credential_factory: CredentialFactory,
        object_id: str,
        *,
        scope: str = AZURE_MANAGED_REDIS_SCOPE,
        max_renewal_attempts: int = DEFAULT_TOKEN_RENEWAL_ATTEMPTS,
        retry_backoff_seconds: float = DEFAULT_TOKEN_RETRY_BACKOFF_SECONDS,
        retry_backoff_cap_seconds: float = DEFAULT_TOKEN_RETRY_BACKOFF_CAP_SECONDS,
        acquisition_timeout_seconds: float = (
            DEFAULT_TOKEN_ACQUISITION_TIMEOUT_SECONDS
        ),
        reauth_timeout_seconds: float = DEFAULT_TOKEN_REAUTH_TIMEOUT_SECONDS,
        expiry_safety_margin_seconds: float = (
            DEFAULT_TOKEN_EXPIRY_SAFETY_MARGIN_SECONDS
        ),
        sleep: SleepCallable = asyncio.sleep,
        jitter: JitterCallable = _default_token_jitter,
    ) -> None:
        if not 1 <= max_renewal_attempts <= MAX_TOKEN_RENEWAL_ATTEMPTS:
            raise ValueError(
                "max_renewal_attempts must be between 1 and "
                f"{MAX_TOKEN_RENEWAL_ATTEMPTS}"
            )
        if retry_backoff_seconds <= 0 or retry_backoff_cap_seconds <= 0:
            raise ValueError("token retry backoff seconds must be positive")
        if acquisition_timeout_seconds <= 0:
            raise ValueError("token acquisition timeout must be positive")
        if reauth_timeout_seconds <= 0:
            raise ValueError("token reauthentication timeout must be positive")
        if expiry_safety_margin_seconds < 0:
            raise ValueError("token expiry safety margin must not be negative")
        self._credential_factory = credential_factory
        self._credential: _AsyncAzureCredential | None = None
        self._object_id = object_id
        self._scope = scope
        self._max_renewal_attempts = max_renewal_attempts
        self._retry_backoff_seconds = retry_backoff_seconds
        self._retry_backoff_cap_seconds = retry_backoff_cap_seconds
        self._acquisition_timeout_seconds = acquisition_timeout_seconds
        self._reauth_timeout_seconds = reauth_timeout_seconds
        self._expiry_safety_margin_seconds = expiry_safety_margin_seconds
        self._sleep = sleep
        self._jitter = jitter
        self._token: AzureRedisToken | None = None
        self._on_next: TokenCallback | None = None
        self._on_error: ErrorCallback | None = None
        self._refresh_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    async def get_credentials_async(self) -> tuple[str, str]:
        """Return current credentials and start one background renewal task."""
        async with self._lock:
            if self._closed:
                raise RuntimeError("Redis credential provider is closed")
            if self._token is None or self._token.is_expired():
                self._token = await self._request_token()
            if self._refresh_task is None or self._refresh_task.done():
                self._refresh_task = asyncio.create_task(self._renew_tokens())
            return self._object_id, self._token.value

    def on_next(self, callback: TokenCallback) -> None:
        """Set the callback invoked after each successful token renewal."""
        self._on_next = callback

    def on_error(self, callback: ErrorCallback) -> None:
        """Set the callback invoked after a failed token renewal."""
        self._on_error = callback

    def is_streaming(self) -> bool:
        """Return whether token renewal is active."""
        return self._refresh_task is not None and not self._refresh_task.done()

    async def aclose(self) -> None:
        """Cancel renewal and close the selected Azure credential once."""
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            task = self._refresh_task
            self._refresh_task = None
            credential = self._credential
            self._credential = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if credential is not None:
            await credential.close()

    async def _request_token(self) -> AzureRedisToken:
        received_at_ms = _utc_now_ms()
        credential = self._credential
        if credential is None:
            credential = self._credential_factory()
            self._credential = credential
        # Bound acquisition so a hung credential endpoint cannot stall renewal
        # indefinitely; a timeout is transient and eligible for retry.
        async with asyncio.timeout(self._acquisition_timeout_seconds):
            access_token = await credential.get_token(self._scope)
        return _build_redis_token(
            access_token,
            object_id=self._object_id,
            received_at_ms=received_at_ms,
        )

    async def _renew_tokens(self) -> None:
        while not self._closed:
            token = self._token
            if token is None:
                return
            remaining_seconds = token.ttl() / 1000
            if remaining_seconds <= 0:
                await self._report_error(
                    RuntimeError("Azure Redis access token expired")
                )
                return
            await self._sleep(self._initial_refresh_delay(remaining_seconds))
            renewed = await self._renew_with_retries()
            if renewed is None:
                return
            # Publish the renewed token only after the pool has accepted it, so a
            # failed reauthentication never advertises an undelivered token.
            delivered = await self._deliver_renewed_token(renewed)
            if not delivered:
                return
            self._token = renewed

    def _initial_refresh_delay(self, remaining_seconds: float) -> float:
        """Schedule the first refresh early enough for the bounded workflow.

        The refresh wakes at the sooner of the proactive refresh ratio or the
        moment that still reserves one full acquisition, one full
        reauthentication, and the safety margin before expiry. A short-lived
        token whose ratio point would already violate that reserve is refreshed
        immediately.
        """
        required_lead_seconds = (
            self._expiry_safety_margin_seconds
            + self._acquisition_timeout_seconds
            + self._reauth_timeout_seconds
        )
        ratio_delay = remaining_seconds * TOKEN_REFRESH_RATIO
        lead_delay = remaining_seconds - required_lead_seconds
        return max(0.0, min(ratio_delay, lead_delay))

    async def _renew_with_retries(self) -> "AzureRedisToken | None":
        attempt = 0
        while not self._closed:
            try:
                renewed = await self._request_token()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                attempt += 1
                current = self._token
                if (
                    not _is_transient_token_error(error)
                    or attempt >= self._max_renewal_attempts
                    or current is None
                    or current.is_expired()
                ):
                    await self._report_error(error)
                    return None
                delay = self._retry_delay(attempt)
                if not _attempt_fits_before_deadline(
                    remaining_seconds=current.ttl() / 1000,
                    delay_seconds=delay,
                    operation_timeout_seconds=self._acquisition_timeout_seconds,
                    safety_margin_seconds=self._expiry_safety_margin_seconds,
                ):
                    # The full next-attempt budget would outlast the current
                    # token; stop before a retry can cross the safe deadline.
                    await self._report_error(error)
                    return None
                await self._sleep(delay)
                continue
            if renewed.is_expired():
                await self._report_error(
                    RuntimeError("Azure Redis returned an expired access token")
                )
                return None
            return renewed
        return None

    async def _deliver_renewed_token(self, renewed: "AzureRedisToken") -> bool:
        """Reauthenticate the pool with bounded retries; report on final failure.

        Live connections keep using the current token until reauthentication
        succeeds, so each retry's full budget is measured against the current
        token's deadline, not the renewed token's later expiry.
        """
        if self._on_next is None:
            return True
        attempt = 0
        while not self._closed:
            try:
                async with asyncio.timeout(self._reauth_timeout_seconds):
                    await _maybe_await(self._on_next(renewed))
                return True
            except asyncio.CancelledError:
                raise
            except Exception as error:
                attempt += 1
                current = self._token
                delay = self._retry_delay(attempt)
                if (
                    attempt >= self._max_renewal_attempts
                    or current is None
                    or current.is_expired()
                    or not _attempt_fits_before_deadline(
                        remaining_seconds=current.ttl() / 1000,
                        delay_seconds=delay,
                        operation_timeout_seconds=self._reauth_timeout_seconds,
                        safety_margin_seconds=self._expiry_safety_margin_seconds,
                    )
                ):
                    await self._report_error(error)
                    return False
                await self._sleep(delay)
        return False

    def _retry_delay(self, attempt: int) -> float:
        exponential = self._retry_backoff_seconds * (1 << (attempt - 1))
        capped = min(exponential, self._retry_backoff_cap_seconds)
        return capped + self._jitter(capped)

    async def _report_error(self, error: Exception) -> None:
        if self._on_error is not None:
            await _maybe_await(self._on_error(error))


@dataclass
class RedisSemanticCacheResources:
    """Own one Redis client and its optional Azure credential provider."""

    cache: RedisSemanticCache
    client: _AsyncRedisClient = field(repr=False)
    credential_provider: AzureRedisCredentialProvider | None = field(
        default=None,
        repr=False,
    )
    _closed: bool = field(default=False, init=False, repr=False)

    async def aclose(self) -> None:
        """Stop renewal, close Redis, and close Azure Identity at most once."""
        if self._closed:
            return
        self._closed = True
        try:
            if self.credential_provider is not None:
                await self.credential_provider.aclose()
        finally:
            await self.client.aclose()


def build_redis_semantic_cache_resources(
    settings: AppSettings,
    embedding_provider: SemanticCacheEmbeddingProvider,
) -> RedisSemanticCacheResources:
    """Compose one TLS client from complete settings without auth fallback."""
    configuration = settings.redis_semantic_cache_configuration()
    if configuration is None:
        raise ValueError("Redis semantic-cache settings are not configured")

    embedding_profile = configuration.embedding_profile()
    if embedding_provider.profile != embedding_profile:
        raise ValueError(
            "embedding provider profile does not match the Redis cache profile"
        )

    credential_provider: AzureRedisCredentialProvider | None = None
    password: str | None = None
    if configuration.auth_mode is RedisAuthMode.ACCESS_KEY:
        if configuration.access_key is None:
            raise AssertionError("validated access-key configuration requires a key")
        password = configuration.access_key.get_secret_value()
    else:
        if configuration.object_id is None:
            raise AssertionError("validated Entra configuration requires an object ID")
        if configuration.auth_mode is RedisAuthMode.AZURE_CLI:
            credential_factory: CredentialFactory = AzureCliCredential
        else:

            def credential_factory() -> _AsyncAzureCredential:
                return ManagedIdentityCredential(
                    client_id=configuration.managed_identity_client_id,
                )

        credential_provider = AzureRedisCredentialProvider(
            credential_factory,
            configuration.object_id,
            max_renewal_attempts=configuration.token_renewal_attempts,
            retry_backoff_seconds=configuration.token_retry_backoff_seconds,
            retry_backoff_cap_seconds=configuration.token_retry_backoff_cap_seconds,
            acquisition_timeout_seconds=(
                configuration.token_acquisition_timeout_seconds
            ),
            reauth_timeout_seconds=configuration.token_reauth_timeout_seconds,
            expiry_safety_margin_seconds=(
                configuration.token_expiry_safety_margin_seconds
            ),
        )

    client = _create_redis_client(
        configuration,
        password=password,
        credential_provider=credential_provider,
    )
    if credential_provider is not None:
        credential_provider.on_next(client.connection_pool.re_auth_callback)
        credential_provider.on_error(_ignore_token_renewal_error)
    cache = RedisSemanticCache(
        client,
        embedding_provider,
        index_name=configuration.index_name,
        embedding_profile=embedding_profile,
        timeout_seconds=configuration.timeout_seconds,
    )
    return RedisSemanticCacheResources(
        cache=cache,
        client=client,
        credential_provider=credential_provider,
    )


def _create_redis_client(
    configuration: RedisSemanticCacheConfiguration,
    *,
    password: str | None,
    credential_provider: AzureRedisCredentialProvider | None,
) -> _AsyncRedisClient:
    """Import redis-py lazily and create one TLS RESP2 client with no retries."""
    redis_type = importlib.import_module("redis.asyncio").Redis
    retry_type = importlib.import_module("redis.asyncio.retry").Retry
    no_backoff_type = importlib.import_module("redis.backoff").NoBackoff

    client = redis_type(
        host=configuration.host,
        port=AZURE_MANAGED_REDIS_PORT,
        password=password,
        credential_provider=credential_provider,
        ssl=True,
        ssl_cert_reqs="required",
        ssl_check_hostname=True,
        socket_timeout=configuration.timeout_seconds,
        socket_connect_timeout=configuration.timeout_seconds,
        max_connections=configuration.max_connections,
        decode_responses=False,
        protocol=2,
        legacy_responses=True,
        retry=retry_type(no_backoff_type(), 0),
        retry_on_timeout=False,
    )
    return cast(_AsyncRedisClient, client)


def _build_redis_token(
    access_token: AccessToken,
    *,
    object_id: str,
    received_at_ms: float,
) -> AzureRedisToken:
    """Validate Azure Identity output before exposing it to Redis AUTH."""
    if not access_token.token:
        raise RuntimeError("Azure Redis returned an empty access token")
    expires_at_ms = float(access_token.expires_on * 1000)
    if expires_at_ms <= received_at_ms:
        raise RuntimeError("Azure Redis returned an expired access token")
    return AzureRedisToken(
        value=access_token.token,
        object_id=object_id,
        expires_at_ms=expires_at_ms,
        received_at_ms=received_at_ms,
    )


async def _maybe_await(result: Awaitable[None] | None) -> None:
    if inspect.isawaitable(result):
        await result


async def _ignore_token_renewal_error(error: Exception) -> None:
    """Avoid leaking Azure SDK details from a background callback."""
    del error


def _utc_now_ms() -> float:
    return datetime.now(UTC).timestamp() * 1000
