from collections.abc import Iterator, Sequence
from typing import TypeVar

T = TypeVar("T")


def batched(items: Sequence[T], size: int) -> Iterator[list[T]]:
    """Yield non-empty lists containing at most ``size`` items."""
    if size <= 0:
        raise ValueError("batch size must be positive")

    for start in range(0, len(items), size):
        yield list(items[start : start + size])
