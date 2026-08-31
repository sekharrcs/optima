"""Request-size and execution-capacity security boundaries."""

from collections.abc import Iterator
from contextlib import contextmanager
from threading import Lock

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_REQUEST_BODY_BYTES = 4 * 1024 * 1024


class ExecutionCapacityExceededError(RuntimeError):
    """Raised before work starts when one process is at execution capacity."""


class ExecutionConcurrencyLimiter:
    """Reject work beyond a fixed process-local active execution count."""

    def __init__(self, maximum_concurrency: int) -> None:
        if maximum_concurrency < 1:
            raise ValueError("maximum concurrency must be positive")
        self._maximum_concurrency = maximum_concurrency
        self._active = 0
        self._lock = Lock()

    @property
    def maximum_concurrency(self) -> int:
        """Return the immutable process-local execution limit."""
        return self._maximum_concurrency

    @property
    def active(self) -> int:
        """Return the current active count for bounded diagnostics and tests."""
        with self._lock:
            return self._active

    @contextmanager
    def acquire(self) -> Iterator[None]:
        """Reserve one execution slot or reject immediately without queueing."""
        with self._lock:
            if self._active >= self._maximum_concurrency:
                raise ExecutionCapacityExceededError(
                    "process execution capacity is exhausted"
                )
            self._active += 1
        try:
            yield
        finally:
            with self._lock:
                self._active -= 1


class RequestBodyLimitMiddleware:
    """Buffer at most one bounded HTTP body before FastAPI deserialization."""

    def __init__(self, app: ASGIApp, *, maximum_body_bytes: int) -> None:
        if maximum_body_bytes < 1:
            raise ValueError("maximum body size must be positive")
        self._app = app
        self._maximum_body_bytes = maximum_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        content_lengths = [
            value for name, value in scope["headers"] if name == b"content-length"
        ]
        if len(content_lengths) > 1:
            await self._reject(
                scope,
                receive,
                send,
                status_code=400,
                code="INVALID_CONTENT_LENGTH",
                message="The request Content-Length header is invalid",
            )
            return
        if content_lengths:
            try:
                content_length = int(content_lengths[0])
            except ValueError:
                content_length = -1
            if content_length < 0:
                await self._reject(
                    scope,
                    receive,
                    send,
                    status_code=400,
                    code="INVALID_CONTENT_LENGTH",
                    message="The request Content-Length header is invalid",
                )
                return
            if content_length > self._maximum_body_bytes:
                await self._reject_too_large(scope, receive, send)
                return

        messages: list[Message] = []
        received_bytes = 0
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] == "http.disconnect":
                break
            if message["type"] != "http.request":
                continue
            received_bytes += len(message.get("body", b""))
            if received_bytes > self._maximum_body_bytes:
                await self._reject_too_large(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        index = 0

        async def replay_receive() -> Message:
            nonlocal index
            if index < len(messages):
                message = messages[index]
                index += 1
                return message
            return await receive()

        await self._app(scope, replay_receive, send)

    async def _reject_too_large(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        await self._reject(
            scope,
            receive,
            send,
            status_code=413,
            code="REQUEST_BODY_TOO_LARGE",
            message="The request body exceeds the configured size limit",
        )

    async def _reject(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        status_code: int,
        code: str,
        message: str,
    ) -> None:
        response = JSONResponse(
            status_code=status_code,
            content={"detail": {"code": code, "message": message, "facts": {}}},
        )
        await response(scope, receive, send)
