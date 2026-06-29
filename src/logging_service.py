import asyncio
import logging
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from datetime import datetime
from threading import RLock


@dataclass(frozen=True)
class LogEntry:
    id: int
    timestamp: str
    level: str
    logger: str
    message: str

    def as_dict(self) -> dict[str, int | str]:
        return asdict(self)


class ApplicationLogBuffer(logging.Handler):
    """Bounded log history with thread-safe async subscriptions."""

    def __init__(self, capacity: int = 1000, subscriber_capacity: int = 200):
        super().__init__()
        self.capacity = capacity
        self.subscriber_capacity = subscriber_capacity
        self._entries: deque[LogEntry] = deque(maxlen=capacity)
        self._subscribers: set[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = set()
        self._buffer_lock = RLock()
        self._next_id = 1
        self._exception_formatter = logging.Formatter()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.exc_info:
                message = (
                    f"{message}\n"
                    f"{self._exception_formatter.formatException(record.exc_info)}"
                )
            with self._buffer_lock:
                entry = LogEntry(
                    id=self._next_id,
                    timestamp=datetime.fromtimestamp(record.created)
                    .astimezone()
                    .isoformat(timespec="seconds"),
                    level=record.levelname,
                    logger=record.name,
                    message=message,
                )
                self._next_id += 1
                self._entries.append(entry)
                subscribers = tuple(self._subscribers)
            for loop, queue in subscribers:
                try:
                    loop.call_soon_threadsafe(self._offer, queue, entry)
                except RuntimeError:
                    pass
        except Exception:
            self.handleError(record)

    def history(self, limit: int = 500) -> list[LogEntry]:
        normalized_limit = min(max(limit, 1), self.capacity)
        with self._buffer_lock:
            return list(self._entries)[-normalized_limit:]

    async def subscribe(self, after_id: int = 0) -> AsyncIterator[LogEntry | None]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[LogEntry] = asyncio.Queue(self.subscriber_capacity)
        subscriber = (loop, queue)
        with self._buffer_lock:
            self._subscribers.add(subscriber)
            backlog = [entry for entry in self._entries if entry.id > after_id]
        try:
            for entry in backlog:
                yield entry
            while True:
                try:
                    yield await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield None
        finally:
            with self._buffer_lock:
                self._subscribers.discard(subscriber)

    @staticmethod
    def _offer(queue: asyncio.Queue, entry: LogEntry) -> None:
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        queue.put_nowait(entry)


application_logs = ApplicationLogBuffer()


def install_application_log_handler() -> None:
    root_logger = logging.getLogger()
    if application_logs not in root_logger.handlers:
        root_logger.addHandler(application_logs)
