from __future__ import annotations

import asyncio
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import Body, FastAPI, HTTPException
from pydantic import BaseModel, Field
from fastapi.responses import JSONResponse

from .client import ModelError
from .dsh_runtime import DSH_VERSION

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
    target_group: str | None = None
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


class MemoryDeleteRequest(BaseModel):
    path: str
    expected_hash: str


class MemoryCreateRequest(BaseModel):
    qq: str = ''
    group_id: str = ''
    category: str
    title: str = Field(default='', max_length=100)


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
            return {**f.config.public(), "observed_self_ids": sorted(f.identity.observed_ids), "agent_prompt": (f.workspace.root / "AGENT.md").read_text("utf-8"), "index_error": f.index.last_error, "integration_error": f.runtime.last_error, "pending_messages": f.journal.pending_count(), "dsh_version": DSH_VERSION, "workspace_path": str(f.workspace.root), "memory_skill_path": str(f.workspace.root / ".agents/skills/memory-management/SKILL.md")}

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
            # Saving configuration restarts the native runtime. Revoke running
            # workers first, including their in-flight QQ callback checks.
            await f.engine.stop()
            async with f.dsh.turn_lock:
                await f.dsh.stop()
            return await config()

        @app.get("/api/ai/files")
        async def list_files():
            labels = {'PERSON': '身份与关系', 'PREFERENCE': '偏好与习惯', 'RECENTNESS': '近期状态', 'GLOSSARY': '公共知识'}
            nicknames = {}
            for scene in f.context.snapshot().values():
                for record in scene.get('recent_messages', []):
                    if not record.get('is_self'):
                        nicknames[record['qq']] = record.get('nickname') or record['qq']
            items = []
            for path in f.workspace.files():
                parts = path.split('/')
                qq = parts[1] if parts[0] == 'user' else ''
                if qq in f.identity.ids:
                    continue
                group_id = parts[1] if parts[0] == 'group' else ''
                category = 'GROUP' if group_id else 'EVENT' if len(parts) == 4 else Path(path).stem
                file = f.workspace.memory_path(path)
                try:
                    info = file.stat()
                except FileNotFoundError:
                    continue  # Agent may delete a file while the list is read.
                items.append({'path': path, 'qq': qq, 'group_id': group_id, 'nickname': nicknames.get(qq, qq), 'category': category,
                    'title': Path(path).stem if category in {'EVENT', 'GROUP'} else labels.get(category, category),
                    'updated_at': info.st_mtime, 'size': info.st_size})
            return {'files': [item['path'] for item in items], 'items': items}

        @app.get("/api/ai/file")
        async def read_file(path: str):
            try:
                text = await asyncio.to_thread(f.workspace.read_memory, path)
                return {"path": path, "content": text, "hash": digest(text), "exists": f.workspace.memory_path(path).exists()}
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

        @app.post('/api/ai/file/create')
        async def create_file(data: MemoryCreateRequest):
            labels = {'PERSON': '身份与关系', 'PREFERENCE': '偏好与习惯', 'RECENTNESS': '近期状态', 'GLOSSARY': '公共知识'}
            try:
                if data.category == 'GLOSSARY':
                    path = 'common/GLOSSARY.md'
                elif data.category == 'GROUP':
                    if not data.group_id.isascii() or not data.group_id.isdigit():
                        raise ValueError('请填写有效的群号')
                    if not data.title.strip() or '/' in data.title or '\\' in data.title or '\n' in data.title or '\r' in data.title:
                        raise ValueError('请填写不含路径分隔符或换行的主题名称')
                    path = f'group/{data.group_id}/{data.title.strip()}.md'
                else:
                    if not data.qq.isascii() or not data.qq.isdigit() or data.qq in f.identity.ids:
                        raise ValueError('请填写有效的用户QQ号，不能使用机器人账号')
                    if data.category == 'EVENT':
                        if not data.title.strip() or data.title.strip() in {'.', '..'} or '/' in data.title or '\\' in data.title:
                            raise ValueError('请填写不含路径分隔符的主题名称')
                        path = f'user/{data.qq}/memory/{data.title.strip()}.md'
                    elif data.category in {'PERSON', 'PREFERENCE', 'RECENTNESS'}:
                        path = f'user/{data.qq}/{data.category}.md'
                    else:
                        raise ValueError('记忆分类无效')
                target = f.workspace.memory_path(path)
                if target.exists():
                    raise ValueError('该记忆文件已存在，请从列表打开')
                text = '# ' + (data.title.strip() if data.category in {'EVENT', 'GROUP'} else labels[data.category]) + '\n\n'
                await asyncio.to_thread(f.workspace.write_memory, path, text, digest(''))
                f.workspace.save_state('index', {'dirty': True})
                return {'path': path, 'content': text, 'hash': digest(text)}
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc

        @app.post('/api/ai/file/delete')
        async def delete_file(data: MemoryDeleteRequest):
            try:
                await asyncio.to_thread(f.workspace.delete_memory, data.path, data.expected_hash)
                f.workspace.save_state('index', {'dirty': True})
                return {'success': True}
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc

        @app.post("/api/ai/reindex")
        async def reindex():
            f.workspace.save_state("index", {"dirty": True})
            return {"success": await asyncio.to_thread(f.index.sync), "error": f.index.last_error}

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
