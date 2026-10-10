from __future__ import annotations

import asyncio
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import Body, FastAPI, HTTPException
from pydantic import BaseModel, Field
from fastapi.responses import JSONResponse

from .client import ModelError

from .storage import atomic_write, digest


class RecordRequest(BaseModel):
    scene_type: str
    scene_id: str
    qq: str
    nickname: str
    content: str = ""
    message_type: str = "text"
    file_data: str | None = None
    filename: str = ""


class QueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    target_qq: str | None = None
    scope: str = "auto"


class IntegrateRequest(BaseModel):
    mode: str
    context_snapshot: dict | None = None
    date: str | None = None


class FileRequest(BaseModel):
    file_url: str
    file_type: str = "image"
    filename: str = ""


class MemoryFileRequest(BaseModel):
    path: str
    content: str = Field(max_length=500000)
    expected_hash: str


class AiAdmin:
    def __init__(self, feature):
        self.feature = feature

    def register_routes(self, app: FastAPI):
        f = self.feature

        @app.get("/api/ai/workspace/status")
        async def workspace_status():
            try:
                return await asyncio.wait_for(f.sandbox.request("status"), timeout=5)
            except Exception as exc:
                return {"success": False, "error": str(exc) or "工作容器忙碌或响应超时"}

        @app.exception_handler(ModelError)
        async def model_error(_request, exc):
            return JSONResponse(status_code=502, content={"detail": str(exc)})

        @app.get("/api/ai/config")
        async def config():
            return {**f.config.public(), "observed_self_ids": sorted(f.identity.observed_ids), "agent_prompt": (f.workspace.root / "AGENT.md").read_text("utf-8"), "index_error": f.index.last_error, "integration_error": f.runtime.last_error, "pending_messages": f.journal.pending_count(), "dsh_version": "0.2.0-rc.2", "workspace_path": str(f.workspace.root), "memory_skill_path": str(f.workspace.root / ".agents/skills/memory-management/SKILL.md")}

        @app.post("/api/ai/config")
        async def save_config(data: dict = Body(...)):
            try:
                changes = dict(data)
                prompt = changes.pop("agent_prompt", None)
                if prompt is not None:
                    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 100000:
                        raise ValueError("人设提示词必须为1至100000字符")
                f.config.save(changes)
                if prompt is not None:
                    await asyncio.to_thread(atomic_write, f.workspace.root / "AGENT.md", prompt)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            async with f.dsh.turn_lock:
                await f.dsh.stop()
            return await config()

        @app.get("/api/ai/files")
        async def list_files():
            return {"files": f.workspace.files()}

        @app.get("/api/ai/file")
        async def read_file(path: str):
            try:
                text = await asyncio.to_thread(f.workspace.read_memory, path)
                return {"path": path, "content": text, "hash": digest(text)}
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc

        @app.post("/api/ai/file")
        async def save_file(data: MemoryFileRequest):
            try:
                if any(data.path.startswith(f"user/{qq}/") for qq in f.identity.ids):
                    raise ValueError("不能为机器人自身创建用户画像")
                await asyncio.to_thread(f.workspace.write_memory, data.path, data.content, data.expected_hash)
                indexed = await asyncio.to_thread(f.index.sync, data.path)
                return {"success": True, "indexed": indexed, "hash": digest(data.content), "index_error": f.index.last_error}
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc

        @app.post("/api/ai/reindex")
        async def reindex():
            f.workspace.save_state("index", {"dirty": True})
            return {"success": await asyncio.to_thread(f.index.sync), "error": f.index.last_error}

        @app.get("/api/ai/search")
        async def search_config():
            return f.search_config.status

        @app.post("/api/ai/search")
        async def save_search(data: dict = Body(...)):
            try:
                f.search_config.save(**data)
            except (ValueError, TypeError) as exc:
                raise HTTPException(400, str(exc)) from exc
            return f.search_config.status

        @app.post("/memory/record")
        async def record(data: RecordRequest):
            try:
                return await f.record(**data.model_dump())
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc

        @app.post("/memory/query")
        async def query(data: QueryRequest):
            try:
                return await f.query_memory(**data.model_dump())
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc

        @app.post("/memory/integrate")
        async def integrate(data: IntegrateRequest):
            try:
                if data.mode not in {'queue', 'full'} or data.date or data.context_snapshot:
                    raise ValueError('使用mode=queue整理当前队列；旧每日/快照整合接口已移除')
                return await f.runtime.full()
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            except (RuntimeError, TimeoutError) as exc:
                f.runtime.last_error = str(exc)
                raise HTTPException(502, '整理失败，消息队列已保留；请查看运行日志') from exc

        @app.post("/memory/understand_file")
        async def understand(data: FileRequest):
            try:
                return {"description": await f.files.understand(data.file_url, data.file_type, data.filename)}
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc

    @property
    def panel_html(self):
        return Path(__file__).with_name("panel.html").read_text("utf-8")

    @property
    def javascript(self):
        return Path(__file__).with_name("panel.js").read_text("utf-8")
