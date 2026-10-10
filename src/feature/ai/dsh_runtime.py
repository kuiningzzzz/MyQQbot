"""Own the official dsh CLI; a private localhost bridge carries QQ capabilities."""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import time
from pathlib import Path

import httpx

from .storage import atomic_write

DSH_VERSION = "0.2.0-rc.2"


class DshRuntime:
    def __init__(self, feature):
        self.feature = feature
        self.process = self.server = None
        self.url = ""
        self.token = secrets.token_hex(32)
        self.active = {}
        self.lock = asyncio.Lock()
        self.turn_lock = asyncio.Lock()
        self.stderr = []
        self.diagnostic_task = None
        self.last_error = ""
        self.home = feature.workspace.data_root / "dsh-home"
        self.code = Path(__file__).resolve().parents[3] / "dsh"

    async def _callback(self, reader, writer):
        try:
            async with asyncio.timeout(150):
                line = await reader.readline()
                headers = {}
                while True:
                    item = await reader.readline()
                    if item in {b"\r\n", b"\n", b""}:
                        break
                    key, value = item.decode().split(":", 1)
                    headers[key.lower()] = value.strip()
                if headers.get("authorization") != "Bearer " + self.token:
                    raise PermissionError("未授权的工具桥接请求")
                size = int(headers.get("content-length", 0))
                if not 0 < size <= 2 * 1024 * 1024 or not line.startswith(b"POST /tool "):
                    raise ValueError("工具桥接请求无效")
                payload = json.loads(await reader.readexactly(size))
                task = self.active.get(payload.get("task_id"))
                if task is None or not task["current"]():
                    raise PermissionError("任务已结束或权限已撤销")
                result = await task["handler"](payload["name"], payload.get("arguments") or {})
                data = json.dumps({"ok": True, "result": result}, ensure_ascii=False).encode()
        except Exception as exc:
            data = json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode()
        try:
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: " + str(len(data)).encode() + b"\r\n\r\n" + data)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def start(self):
        async with self.lock:
            if self.process and self.process.returncode is None:
                return
            if self.server:
                await self.stop()
            node = shutil.which("node")
            cli = self.code / "node_modules/@deepseek-ai/dsh/lib/bin.js"
            if not node or not cli.exists():
                raise RuntimeError("dsh运行时缺失：请安装Node.js 24并在dsh目录执行npm ci")
            self.server = await asyncio.start_server(self._callback, "127.0.0.1", 0)
            callback = "http://127.0.0.1:" + str(self.server.sockets[0].getsockname()[1]) + "/tool"
            profile = self.home / "profiles/qqbot"
            atomic_write(profile / "package.json", json.dumps({"name": "qqbot-profile", "private": True, "dsh": {"profile": {"bundles": []}}}))
            atomic_write(profile / "cordis.patch.yml", "[]\n")
            settings = self.feature.config.load()
            temporary = self.home / 'tmp'
            temporary.mkdir(parents=True, exist_ok=True)
            env = {**os.environ, "DSH_HOME": str(self.home), "DSH_TELEMETRY_DISABLED": "1", "QQBOT_DSH_TOKEN": self.token,
                "TMPDIR": str(temporary), "TMP": str(temporary), "TEMP": str(temporary),
                "QQBOT_CALLBACK": callback, "QQBOT_WORKSPACE": str(self.feature.workspace.root), "DEEPSEEK_API_KEY": settings.api_key,
                "DEEPSEEK_BASE_URL": settings.dsh_api_url or settings.api_url.removesuffix("/v1") + "/anthropic"}
            self.process = await asyncio.create_subprocess_exec(node, str(cli), "--profile", "qqbot", "--patch", str(self.code / "profile.yml"), cwd=self.feature.workspace.root, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            self.diagnostic_task = asyncio.create_task(self._diagnostics())
            try:
                async with asyncio.timeout(45):
                    while True:
                        line = await self.process.stdout.readline()
                        if not line:
                            raise RuntimeError("dsh启动失败：" + "\n".join(self.stderr[-8:]))
                        if line.startswith(b"QQBOT_READY "):
                            self.url = line.decode().strip().split(" ", 1)[1]
                            break
            except BaseException:
                await self.stop()
                raise

    async def _diagnostics(self):
        while self.process and self.process.stderr:
            line = await self.process.stderr.readline()
            if not line:
                return
            # Keep bounded diagnostics, never print model requests or credentials.
            self.stderr = (self.stderr + [line.decode(errors="replace").strip()])[-20:]

    async def request(self, endpoint, data, timeout=30):
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.post(self.url + endpoint, headers={"Authorization": "Bearer " + self.token}, json=data)
            response.raise_for_status()
            result = response.json()
            if not result.get("ok"):
                raise RuntimeError(result.get("error", "dsh操作失败"))
            return result.get("result", {})

    async def run(self, kind, prompt, *, task_id, handler, current, owner=False, qq="", cwd=None, system="", scene_dir="", expires_at=None):
        async with self.turn_lock:
            if not current():
                return {"cancelled": True}
            await self.start()
            self.active[task_id] = {"handler": handler, "current": current}
            try:
                # Any native read can consult the oldest retained raw record.
                # Bound the entire session (including compaction/tool output)
                # by that record's remaining lifetime, not by log file mtime.
                oldest = self.feature.journal.oldest_timestamp()
                deadline = expires_at or time.time() + 30 * 86400
                if oldest is not None:
                    deadline = min(deadline, oldest + 30 * 86400)
                return await self.request("/run", {"kind": kind, "task_id": task_id, "owner": owner, "qq": qq,
                    "prompt": prompt, "system": system, "self_ids": sorted(self.feature.identity.ids), "scene_dir": scene_dir, "cwd": str(cwd or self.feature.workspace.root),
                    "expires_at": deadline * 1000}, timeout=None)
            except BaseException:
                if self.url:
                    try:
                        await asyncio.shield(self.request("/cancel", {"task_id": task_id}))
                    except Exception:
                        pass
                raise
            finally:
                self.active.pop(task_id, None)

    async def cancel(self, task_id):
        self.active.pop(task_id, None)
        if self.url:
            await self.request("/cancel", {"task_id": task_id})

    async def stop(self):
        self.active.clear()
        if self.process and self.process.returncode is None:
            if self.url:
                try:
                    await self.request('/close', {}, timeout=10)
                except Exception:
                    pass
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 10)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        if self.diagnostic_task:
            await self.diagnostic_task
        self.process = self.server = None
        self.url = ""

    def has_session(self, kind):
        path = self.home / 'qqbot-sessions.json'
        if not path.exists():
            return False
        item = json.loads(path.read_text('utf-8')).get(kind)
        return bool(item and item.get('expires', 0) > time.time() * 1000 and any((self.home / 'sessions').glob('*/' + item['id'])))

    async def prune(self):
        async with self.turn_lock:
            manifest = self.home / 'qqbot-sessions.json'
            retention = self.home / 'qqbot-retention.json'
            data = json.loads(manifest.read_text('utf-8')) if manifest.exists() else {}
            retained = json.loads(retention.read_text('utf-8')) if retention.exists() else {}
            expired = {key for key, value in retained.items() if value <= time.time() * 1000}
            if expired:
                await self.stop()
                temporary = self.home / 'tmp'
                if temporary.is_dir() and not temporary.is_symlink():
                    shutil.rmtree(temporary)
                for identity in expired:
                    if '/' in identity or '\\' in identity or not identity.startswith(('chat-', 'memory-')):
                        raise ValueError('会话路径越界')
                    for target in (self.home / 'sessions').glob('*/' + identity):
                        if not target.is_symlink() and target.resolve().is_relative_to((self.home / 'sessions').resolve()):
                            shutil.rmtree(target)
                    retained.pop(identity)
                data = {key: value for key, value in data.items() if value['id'] not in expired}
                atomic_write(manifest, json.dumps(data))
                atomic_write(retention, json.dumps(retained))
            cutoff = time.time() - 30 * 86400
            for file in (self.feature.workspace.root / 'files').rglob('*'):
                if file.is_file() and not file.is_symlink() and file.stat().st_mtime < cutoff:
                    file.unlink()
