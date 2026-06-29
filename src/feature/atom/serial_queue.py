import asyncio
from collections.abc import Awaitable, Callable
from typing import Generic, TypeVar

T = TypeVar("T")


class SerialTaskQueue(Generic[T]):
    """In-memory FIFO queue with exactly one asynchronous consumer."""

    def __init__(self, processor: Callable[[T], Awaitable[None]]):
        self.processor = processor
        self.active: T | None = None
        self.pending: list[T] = []
        self._worker: asyncio.Task[None] | None = None

    def enqueue(self, task: T) -> bool:
        was_idle = not self.busy
        self.pending.append(task)
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run())
        return was_idle

    async def _run(self) -> None:
        while self.pending:
            task = self.pending.pop(0)
            self.active = task
            try:
                await self.processor(task)
            finally:
                self.active = None

    @property
    def busy(self) -> bool:
        return self.active is not None or bool(self.pending)

    def snapshot(self) -> list[tuple[str, T]]:
        tasks: list[tuple[str, T]] = []
        if self.active is not None:
            tasks.append(("处理中", self.active))
        tasks.extend(("待处理", task) for task in self.pending)
        return tasks
