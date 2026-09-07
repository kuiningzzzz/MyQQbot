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

        @app.exception_handler(ModelError)
        async def model_error(_request, exc):
            return JSONResponse(status_code=502, content={"detail": str(exc)})

        @app.get("/api/ai/config")
        async def config():
            return {**f.config.public(), "observed_self_ids": sorted(f.identity.observed_ids), "agent_prompt": (f.workspace.root / "AGENT.md").read_text("utf-8"), "index_error": f.index.last_error, "integration_error": f.runtime.last_error}

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
                return await f.attention.query(**data.model_dump())
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc

        @app.post("/memory/integrate")
        async def integrate(data: IntegrateRequest):
            try:
                if data.mode == "full" and data.date:
                    return await f.runtime.full(data.date)
                if data.mode == "incremental":
                    snapshot = data.context_snapshot
                    if snapshot is None:
                        raise ValueError("增量整合必须提供 context_snapshot")
                    # Support the documented single-scene envelope and full snapshots.
                    if "messages" in snapshot:
                        snapshot = {str(snapshot.get("scene_type", "group")) + ":" + str(snapshot.get("scene_id", "")): {"recent_messages": snapshot["messages"]}}
                    lines = []
                    for key, scene in snapshot.items():
                        for record in scene.get("recent_messages", []):
                            if "raw_line" in record:
                                lines.append(record["raw_line"])
                                continue
                            scene_type, _, scene_id = key.partition(":")
                            qq = str(record.get("qq") or record.get("user_id") or "")
                            if not qq.isdigit() or scene_type not in {"group", "private"} or not scene_id.isdigit():
                                raise ValueError("每条快照消息需要QQ号和有效场景")
                            normalized = {"qq": qq, "nickname": str(record.get("nickname") or qq), "scene_type": scene_type, "scene_id": scene_id, "content": str(record.get("content") or ""), "is_self": qq in f.identity.ids or bool(record.get("is_self")), "timestamp": record.get("timestamp") or datetime.now(ZoneInfo(f.config.load().timezone)).isoformat()}
                            lines.append(f.workspace.format_raw(normalized, f.config.load().timezone))
                    if not lines:
                        raise ValueError("快照没有可整合的消息")
                    return await f.integration.integrate(lines, snapshot)
                raise ValueError("请选择 incremental 或 full，并提供相应参数")
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc

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
