"""Small synchronized holder for replacing cache state atomically."""

from threading import Lock
from typing import Generic, TypeVar


T = TypeVar("T")


class AtomicSnapshot(Generic[T]):
    """Publish complete immutable-by-convention values with one pointer swap."""

    def __init__(self, initial: T):
        self._value = initial
        self._lock = Lock()

    def get(self) -> T:
        with self._lock:
            return self._value

    def replace(self, value: T) -> None:
        with self._lock:
            self._value = value
