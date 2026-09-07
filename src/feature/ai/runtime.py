from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .storage import period_date, digest

logger = logging.getLogger(__name__)


class MemoryRuntime:
    """Single background writer; durable source logs are the recovery queue."""

    def __init__(self, workspace, config, integration):
        self.workspace, self.config, self.integration = workspace, config, integration
        self.pending = None
        self.worker = self.scheduler = None
        self.last_error = ""
        self.running = False
        self.state_lock = asyncio.Lock()
        self.enabled = True

    def set_enabled(self, enabled: bool):
        self.enabled = enabled
        if not enabled:
            self.pending = None
            for task in (self.worker, self.scheduler):
                if task and not task.done():
                    task.cancel()
        elif self.running and (self.scheduler is None or self.scheduler.done() or self.scheduler.cancelling()):
            self.scheduler = asyncio.create_task(self._schedule())

    def trigger(self, snapshot: dict) -> None:
        if not self.enabled or not self.config.load().memory_auto_learn:
            return
        self.pending = snapshot
        if self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self._drain())

    async def _drain(self):
        while self.pending is not None and self.enabled and self.config.load().memory_auto_learn:
            snapshot, self.pending = self.pending, None
            lines = [record["raw_line"] for scene in snapshot.values() for record in scene.get("recent_messages", []) if "raw_line" in record]
            try:
                await self.integration.integrate(lines, snapshot)
                self.last_error = ""
            except Exception as exc:
                self.last_error = str(exc)
                logger.exception("增量整合失败；原始记录保留，后续全量任务可恢复")

    async def full(self, date: str) -> dict:
        datetime.fromisoformat(date)
        now = datetime.now(ZoneInfo(self.config.load().timezone))
        if date >= period_date(now):
            raise ValueError("只能整合已结束的03:00至次日03:00周期")
        lines = await asyncio.to_thread(self.workspace.read_raw, date)
        result = await self.integration.integrate(lines)
        async with self.state_lock:
            state = self.workspace.load_state("schedule", {"completed": [], "last_compaction": ""})
            state["completed"] = sorted(set(state["completed"]) | {date})
            state.setdefault("completed_hashes", {})[date] = digest("\n".join(lines))
            await asyncio.to_thread(self.workspace.save_state, "schedule", state)
        return result

    async def tick(self, now: datetime | None = None):
        settings = self.config.load()
        if not self.enabled or not settings.memory_auto_learn:
            return
        now = now or datetime.now(ZoneInfo(settings.timezone))
        current = period_date(now)
        state = self.workspace.load_state("schedule", {"completed": [], "last_compaction": ""})
        for path in sorted((self.workspace.root / "raw").glob("????-??-??.md")):
            if path.stem < current:
                lines = await asyncio.to_thread(self.workspace.read_raw, path.stem)
                if state.get("completed_hashes", {}).get(path.stem) != digest("\n".join(lines)):
                    await self.full(path.stem)
        state = self.workspace.load_state("schedule", {"completed": [], "last_compaction": ""})
        last = state.get("last_compaction")
        if not last or (datetime.fromisoformat(current) - datetime.fromisoformat(last)).days >= settings.vector_merge_threshold:
            await self.integration.compact()
            async with self.state_lock:
                state = self.workspace.load_state("schedule", {"completed": [], "last_compaction": ""})
                state["last_compaction"] = current
                await asyncio.to_thread(self.workspace.save_state, "schedule", state)
        cutoff = (datetime.fromisoformat(current) - timedelta(days=settings.raw_retention_days)).date().isoformat()
        for path in (self.workspace.root / "raw").glob("????-??-??.md"):
            if path.stem < cutoff and path.stem in state["completed"]:
                lines = await asyncio.to_thread(self.workspace.read_raw, path.stem)
                if state.get("completed_hashes", {}).get(path.stem) == digest("\n".join(lines)):
                    path.unlink()  # Never remove late arrivals awaiting integration.
        # Dedup receipts need only cover the retained raw logs.
        retained = set()
        for path in (self.workspace.root / "raw").glob("????-??-??.md"):
            retained.update(digest(line) for line in await asyncio.to_thread(self.workspace.read_raw, path.stem))
        async with self.integration.lock:
            receipts = set(self.workspace.load_state("integrated_lines", []))
            await asyncio.to_thread(self.workspace.save_state, "integrated_lines", sorted(receipts & retained))

    async def start(self):
        self.running = True
        self.scheduler = asyncio.create_task(self._schedule())

    async def _schedule(self):
        while self.running:
            try:
                await self.tick()
                self.last_error = ""
            except Exception as exc:
                self.last_error = str(exc)
                logger.exception("记忆日任务失败，将在下次调度重试")
            await asyncio.sleep(60)

    async def stop(self):
        self.running = False
        tasks = [task for task in (self.worker, self.scheduler) if task and not task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
