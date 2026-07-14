"""Deadline and timeout helpers shared by request and tool entrypoints."""

import asyncio
import contextvars
import functools
import inspect
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Optional, TypeVar


T = TypeVar("T")
_deadline: contextvars.ContextVar[Optional[float]] = contextvars.ContextVar(
    "intellibridge_deadline", default=None
)


class OperationTimeout(TimeoutError):
    """Raised when an operation exceeds its configured deadline."""

    def __init__(self, operation: str, timeout_seconds: float):
        self.operation = operation
        self.timeout_seconds = timeout_seconds
        super().__init__(
            f"操作 {operation} 超时（限制 {timeout_seconds:g} 秒），请稍后重试"
        )


def get_deadline() -> Optional[float]:
    """Return the current monotonic deadline, if one exists."""

    return _deadline.get()


def get_remaining_timeout(default: Optional[float] = None) -> Optional[float]:
    """Return seconds remaining in the current deadline.

    ``default`` is returned when no deadline is active. A zero value means the
    current deadline has expired.
    """

    deadline = _deadline.get()
    if deadline is None:
        return default
    return max(0.0, deadline - time.monotonic())


@contextmanager
def deadline_scope(timeout_seconds: float) -> Iterator[None]:
    """Install a deadline without interrupting synchronous Python code."""

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than zero")

    new_deadline = time.monotonic() + timeout_seconds
    current_deadline = _deadline.get()
    if current_deadline is not None:
        new_deadline = min(new_deadline, current_deadline)

    token = _deadline.set(new_deadline)
    try:
        yield
    finally:
        _deadline.reset(token)


def with_timeout(timeout_seconds: float, operation: Optional[str] = None) -> Callable:
    """Apply an async deadline and expose it to nested synchronous calls.

    Async functions are cancelled by ``asyncio.timeout``. Synchronous
    functions receive the deadline through ``get_remaining_timeout``; Python
    cannot safely terminate a running thread, so their I/O layers must consume
    the remaining timeout explicitly.
    """

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than zero")

    def decorator(func: Callable[..., T]) -> Callable[..., Any]:
        name = operation or f"{func.__module__}.{func.__qualname__}"

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> T:
                remaining = get_remaining_timeout(timeout_seconds)
                if remaining is None or remaining <= 0:
                    raise OperationTimeout(name, timeout_seconds)
                try:
                    with deadline_scope(remaining):
                        async with asyncio.timeout(remaining):
                            return await func(*args, **kwargs)
                except asyncio.TimeoutError as exc:
                    raise OperationTimeout(name, timeout_seconds) from exc

            return async_wrapper

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> T:
            with deadline_scope(timeout_seconds):
                return func(*args, **kwargs)

        return sync_wrapper

    return decorator
