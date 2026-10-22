"""Own the official dsh CLI; a private localhost bridge carries QQ capabilities."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import time
from pathlib import Path

import httpx

from .storage import atomic_write
from .dsh_upgrade import DSH_VERSION, RuntimeUpgrade

logger = logging.getLogger(__name__)


class DshRuntime:
    def __init__(self, feature):
        self.feature = feature
        self.process = self.server = None
        self.url = ""
        self.token = secrets.token_hex(32)
        self.active = {}
        self.lock = asyncio.Lock()
        self.turn_lock = asyncio.Lock()
        self.kind_locks = {'chat': asyncio.Lock(), 'memory': asyncio.Lock()}
        self.stderr = []
        self.diagnostic_task = None
        self.last_error = ""
        self.home = feature.workspace.data_root / "dsh-home"
        self.code = Path(__file__).resolve().parents[3] / "dsh"
        self.upgrade = RuntimeUpgrade(feature.workspace.data_root)

    async def _callback(self, reader, writer):
        try:
            async with asyncio.timeout(150) as budget:
                line = await reader.readline()
                headers = {}
                while True:
                    item = await reader.readline()
                    if item in {b"\r\n", b"\n", b""}:
                        break
                    key, value = item.decode().split(":", 1)
                    headers[key.lower()] = value.strip()
                authorization = headers.get('authorization', '')
                native = secrets.compare_digest(authorization.encode(), ('Bearer ' + self.token).encode())
                size = int(headers.get("content-length", 0))
                if not 0 < size <= 2 * 1024 * 1024 or not line.startswith(b"POST /tool "):
                    raise ValueError("工具桥接请求无效")
                payload = json.loads(await reader.readexactly(size))
                task = self.active.get(payload.get("task_id"))
                capability = task.get('sticker_capability') if task else None
                if not native and not (payload.get('name') == '__stickers' and capability and
                        task.get('expires_at', 0) > time.time() and
                        secrets.compare_digest(authorization.encode(), ('Bearer ' + capability).encode())):
                    raise PermissionError('未授权的工具桥接请求')
                if payload.get('name') == 'integrate_memory':
                    budget.reschedule(None)
                # Completion is idempotent, including after a host-side revoke.
                if task is None and payload.get('name') != '__finish':
                    raise PermissionError("任务已结束或权限已撤销")
                name, args = payload['name'], payload.get('arguments') or {}
                if task is None:
                    result = {'success': True}
                elif name == '__revoke' and task.get('revoke'):
                    task['revoke']()
                    result = {'success': True}
                elif name == '__finish' and task.get('finish'):
                    self.active.pop(payload['task_id'], None)
                    await task['finish'](args)
                    result = {'success': True}
                else:
                    if not task['current']():
                        raise PermissionError('任务已结束或权限已撤销')
                    if name == '__spawn' and task.get('spawn'):
                        child = await task['spawn'](args['task_id'], args['title'])
                        child.update(owner=task.get('owner', False), expires_at=task.get('expires_at', 0),
                                     sticker_capability=secrets.token_hex(32) if task.get('owner') else '')
                        self.active[args['task_id']] = child
                        result = {'context': child['context'], 'sticker_capability': child['sticker_capability']}
                    else:
                        result = await task['handler'](name, args)
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
            installed = json.loads((cli.parents[1] / 'package.json').read_text('utf-8'))['version']
            if installed != DSH_VERSION:
                raise RuntimeError(f"dsh依赖版本不符：需要 {DSH_VERSION}，实际 {installed}；请在dsh目录执行npm ci")
            self.upgrade.prepare()
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
            env.pop('DASHSCOPE_API_KEY', None)  # Cloud audio belongs to the host, never to shell tools.
            env['QQBOT_BROWSER_EXECUTABLE_PATH'] = os.getenv('AI_BROWSER_EXECUTABLE_PATH', '')
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
                            self.upgrade.complete()
                            break
            except BaseException:
                await self.stop()
                raise

    async def _diagnostics(self):
        process = self.process
        while process and process.stderr:
            line = await process.stderr.readline()
            if not line:
                break
            # Keep bounded diagnostics, never print model requests or credentials.
            entry = line.decode(errors="replace").strip()
            self.stderr = (self.stderr + [entry])[-20:]
            if entry.startswith('QQBOT_TOOL_SURFACE '):
                # Names and host-issued identifiers only; never log prompts,
                # tool arguments, chat content or model credentials here.
                logger.info('dsh工具注入：%s', entry.removeprefix('QQBOT_TOOL_SURFACE '))
            elif entry.startswith('QQBOT_ACTIVATION_ERROR '):
                # The bridge emits a bounded, credential-redacted exception,
                # not the model input. systemd/application logs retain it even
                # after the temporary fork's physical log is deleted.
                logger.error('dsh分支失败：%s', entry.removeprefix('QQBOT_ACTIVATION_ERROR '))
        if process:
            await process.wait()
        if self.process is process:
            # A crashed native process cannot call __finish. Release its input
            # files and notify the original scene; never silently restart work.
            children = [(identifier, task) for identifier, task in self.active.items() if task.get('finish')]
            for identifier, task in children:
                if self.active.pop(identifier, None) is task:
                    await task['finish']({'state': 'failed', 'output': '后台执行进程意外退出，任务已中断，未自动重做。请检查已有文件后重新发起。'})

    async def request(self, endpoint, data, timeout=30):
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.post(self.url + endpoint, headers={"Authorization": "Bearer " + self.token}, json=data)
            response.raise_for_status()
            result = response.json()
            if not result.get("ok"):
                raise RuntimeError(result.get("error", "dsh操作失败"))
            return result.get("result", {})

    def limits(self):
        """单轮任务的 (请求超时, 总截止时间)；模型服务过载时用于让队列继续。"""
        try:
            value = int(self.feature.config.load().dsh_timeout_seconds)
        except Exception:
            value = 600
        timeout = float(max(30, min(3600, value)))
        return timeout, timeout + 15.0

    async def acquire_gate(self, kind):
        """有界地等待同类回合，超时即判定上一回合泄漏并强制复位运行时。"""
        lock = self.kind_locks.setdefault(kind, asyncio.Lock())
        wait = self.limits()[1] * 2
        try:
            await asyncio.wait_for(lock.acquire(), timeout=wait)
            return lock
        except TimeoutError:
            self.last_error = '等待回合锁超时，已强制复位 dsh 运行时'
            logger.error('等待 %s 回合超过 %.0f 秒仍未释放，判定上一回合泄漏，强制复位 dsh 运行时', kind, wait)
            await self.recover_stalled(kind)
            lock = self.kind_locks[kind]
            await lock.acquire()
            return lock

    async def recover_stalled(self, kind=None):
        """终止卡死的 dsh 内核并换发新锁，让排队的回合重新开始。"""
        try:
            await self.stop()
        except Exception:
            logger.exception('复位 dsh 运行时失败')
        if isinstance(kind, str):
            self.kind_locks[kind] = asyncio.Lock()
        else:
            for name in list(self.kind_locks):
                self.kind_locks[name] = asyncio.Lock()
        self.turn_lock = asyncio.Lock()

    async def run(self, kind, prompt, *, task_id, handler, current, owner=False, qq="", cwd=None, system="", scene_dir="", group_id="", expires_at=None, role=None, worker_system='', spawn=None, delivery=False, revoke=None):
        role = role or ('main' if kind == 'chat' else 'memory')
        # Root conversations are ordered independently; native forks never
        # hold a root turn lock while doing their potentially long work.
        gate = None if role == 'scheduled' else await self.acquire_gate(kind)
        try:
            if not current():
                return {"cancelled": True}
            turn_timeout, turn_deadline = self.limits()
            async with self.turn_lock:
                await self.start()
                capability = secrets.token_hex(32) if kind == 'chat' and owner and role in {'worker', 'scheduled'} else ''
                self.active[task_id] = {"handler": handler, "current": current, 'spawn': spawn, 'revoke': revoke,
                                        'owner': owner, 'sticker_capability': capability}
            request_task = monitor = None
            try:
                # Any native read can consult the oldest retained raw record.
                # Bound the entire session (including compaction/tool output)
                # by that record's remaining lifetime, not by log file mtime.
                oldest = self.feature.journal.oldest_timestamp()
                deadline = expires_at or time.time() + 30 * 86400
                if oldest is not None:
                    deadline = min(deadline, oldest + 30 * 86400)
                self.active[task_id]['expires_at'] = deadline
                endpoint = '/maintenance' if kind == 'memory' else '/scheduled' if role == 'scheduled' else '/run'
                request_task = asyncio.create_task(self.request(endpoint, {"kind": kind, "role": role, 'worker_system': worker_system,
                    'delivery': delivery, "task_id": task_id, "owner": owner, "qq": qq,
                    'sticker_capability': capability,
                    'browser_enabled': self.feature.config.load().browser_enabled,
                    "prompt": prompt, "system": system, "self_ids": sorted(self.feature.identity.ids), "scene_dir": scene_dir, "group_id": group_id, "cwd": str(cwd or self.feature.workspace.root),
                    "expires_at": deadline * 1000}, timeout=turn_timeout))
                async def watch():
                    while not request_task.done():
                        if not current():
                            request_task.cancel()
                            return
                        await asyncio.sleep(.25)
                monitor = asyncio.create_task(watch())
                try:
                    return await asyncio.wait_for(request_task, timeout=turn_deadline)
                except TimeoutError as exc:
                    # 模型服务长时间无响应时不能占着队列：取消本轮并让上层
                    # 记录失败，后续消息继续处理。
                    request_task.cancel()
                    self.last_error = 'dsh回合超时'
                    raise RuntimeError('dsh回合超时：超过 %.0f 秒未返回，已取消本轮' % turn_timeout) from exc
            except BaseException as exc:
                if revoke:
                    revoke()
                if self.url:
                    try:
                        await asyncio.shield(self.request("/cancel", {"task_id": task_id}))
                    except Exception:
                        pass
                if isinstance(exc, asyncio.CancelledError) and not current() and not asyncio.current_task().cancelling():
                    return {'cancelled': True}
                raise
            finally:
                if monitor:
                    monitor.cancel()
                    await asyncio.gather(monitor, return_exceptions=True)
                self.active.pop(task_id, None)
        finally:
            if gate is not None:
                gate.release()

    async def cancel(self, task_id):
        task = self.active.pop(task_id, None)
        if task and task.get('revoke'):
            task['revoke']()
        if task and task.get('finish'):
            await task['finish']({'task_id': task_id, 'state': 'cancelled', 'output': ''})
        if self.url:
            await self.request("/cancel", {"task_id": task_id})

    async def stop(self):
        for task in self.active.values():
            if task.get('revoke'):
                task['revoke']()
        children = [task for task in self.active.values() if task.get('finish')]
        self.active.clear()
        for task in children:
            try:
                await asyncio.wait_for(task['finish']({'state': 'cancelled', 'output': ''}), timeout=5)
            except Exception:
                logger.exception('后台任务收尾失败')
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
                try:
                    await asyncio.wait_for(self.process.wait(), 5)
                except TimeoutError:
                    logger.error('dsh进程未能退出，复位流程继续')
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        if self.diagnostic_task:
            # 诊断任务在读 stderr；进程异常时它可能永不返回，不能让它拖住复位。
            try:
                await asyncio.wait_for(asyncio.shield(self.diagnostic_task), timeout=5)
            except TimeoutError:
                self.diagnostic_task.cancel()
                await asyncio.gather(self.diagnostic_task, return_exceptions=True)
            except Exception:
                pass
        self.process = self.server = None
        self.url = ""

    def has_session(self, kind):
        path = self.home / 'qqbot-sessions.json'
        if not path.exists():
            return False
        item = json.loads(path.read_text('utf-8')).get(kind)
        if not item or item.get('expires', 0) <= time.time() * 1000:
            return False
        folders = list((self.home / 'sessions').glob('*/' + item['id']))
        if not folders:
            return False
        cwd = item.get('cwd')
        if not isinstance(cwd, str):
            # This read-only check runs before startup's upgrade backup. Let
            # the native bridge persist the inferred field after that backup;
            # otherwise Python would reset its cursor and replay old input.
            for folder in folders:
                try:
                    with (folder / 'session.v4.jsonl').open(encoding='utf-8') as stream:
                        header = json.loads(stream.readline(65536))
                    if isinstance(header, dict) and header.get('type') == 'session' and header.get('id') == item['id']:
                        cwd = header.get('cwd')
                        break
                except (OSError, ValueError):
                    continue
        # Only an actual directory move requires re-seeding retained context.
        return isinstance(cwd, str) and Path(cwd) == self.feature.workspace.root

    async def prune(self):
        if self.turn_lock.locked():
            # 有回合正持有锁（或锁已泄漏）；不要在这里排队等待，交给看门狗处理。
            logger.error('dsh 清理跳过：回合锁被占用')
            return
        async with self.turn_lock:
            if not self.process or self.process.returncode is not None:
                self.upgrade.prepare()
            self.upgrade.prune()
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
                    if not re.fullmatch(r'(?:chat-|memory-)?[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', identity):
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
