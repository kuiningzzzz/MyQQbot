"""Bounded, isolated live checks for optional external-service features."""
import asyncio
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


class DiagnosticRequest(BaseModel):
    sample_id: str = Field(default='', max_length=20, pattern=r'^[0-9]*$')

    @field_validator('sample_id')
    @classmethod
    def positive_id(cls, value):
        if value and int(value) == 0:
            raise ValueError('测试作品ID必须大于0')
        return value


class DiagnosticFailure(RuntimeError):
    def __init__(self, code, message, hint):
        super().__init__(message)
        self.code, self.hint = code, hint


def safe_error(error, secrets=()):
    text = str(error)
    from urllib.parse import quote, quote_plus
    for secret in secrets:
        if secret:
            for value in (secret, quote(secret, safe=''), quote_plus(secret)):
                text = text.replace(value, '[已隐藏]')
    text = re.sub(r'(https?://)[^/\s@]+@', r'\1[已隐藏]@', text)
    text = re.sub(r'''(?i)((?:refresh_token|access_token|client_secret|authorization|password|token)["']?\s*[:=]\s*["']?)[^\s,"'}&]+''', r'\1[已隐藏]', text)
    return ' '.join(text.split())[:500]


class DiagnosticReport:
    """The worker publishes progress atomically, including from SDK threads."""
    def __init__(self, path, data, secrets=()):
        self.path, self.data, self.secrets = path, data, secrets
        self.lock = threading.RLock()
        self.started = time.monotonic()
        self.flush()

    def flush(self):
        with self.lock:
            self.data['duration_ms'] = round((time.monotonic() - self.started) * 1000)
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(json.dumps(self.data, ensure_ascii=False), 'utf-8')
            os.replace(temporary, self.path)

    def step(self, key, status='running', message='', **extra):
        with self.lock:
            check = next(item for item in self.data['checks'] if item['id'] == key)
            check.update(status=status, message=safe_error(message, self.secrets), **extra)
            self.flush()

    def require_passed(self, key):
        check = next(item for item in self.data['checks'] if item['id'] == key)
        if check['status'] != 'passed':
            raise DiagnosticFailure('incomplete_download', '图片下载未完整成功，不能继续判定 PDF 可用。', '先解决图片下载阶段的问题后重新测试。')

    def fail(self, error):
        with self.lock:
            current = next((item for item in self.data['checks'] if item['status'] == 'running'), None)
            current = current or next((item for item in self.data['checks'] if item['status'] == 'pending'), self.data['checks'][-1])
            detail = safe_error(error, self.secrets)
            lower = (type(error).__name__ + ' ' + str(error)).lower()
            code = getattr(error, 'code', 'check_failed')
            hint = getattr(error, 'hint', '检查该阶段的配置和服务状态后重试；单个作品失败不代表整个服务不可用，可更换测试ID。')
            if isinstance(error, ImportError):
                code, hint = 'dependency_missing', '重新安装或更新 requirements.txt 中该功能的依赖，然后重启服务。'
            elif not isinstance(error, DiagnosticFailure):
                if any(word in lower for word in ('timeout', 'timed out', '超时', 'curl: (28)')):
                    code, hint = 'timeout', '检查服务器网络及代理是否可达，稍后重试。'
                elif any(word in lower for word in ('invalid_grant', 'invalid refresh', 'invalid_refresh', 'oauth', 'authentication required')):
                    code, hint = 'auth_failed', '重新获取 Pixiv refresh token，在当前管理页面保存后重试。'
                elif any(word in lower for word in ('proxy', 'connection', 'connect', 'resolve host', 'name resolution', 'dns', 'ssl', 'certificate')):
                    code, hint = 'network_error', '检查服务器的 DNS、网络、代理及目标站点访问限制；Pixiv 使用 PIXIV_PROXY 环境配置。'
                elif current['id'] == 'auth':
                    code, hint = 'auth_failed', '检查 Pixiv OAuth 返回原因；若 token 已失效，重新获取并保存后测试。'
                elif current['id'] == 'image':
                    code, hint = 'image_download_failed', '检查图片 CDN、作品访问权限及图片格式；确认文件未被拦截页面替换。'
                elif current['id'] == 'pdf':
                    code, hint = 'pdf_export_failed', '检查 jmcomic、img2pdf 依赖和磁盘写入权限，更新依赖后重试。'
            current.update(status='failed', code=code, message=detail or type(error).__name__, hint=hint)
            for item in self.data['checks']:
                if item['status'] in {'pending', 'running'}:
                    item.update(status='skipped', message='前序检查未通过，未执行。')
            self.data.update(status='failed', finished_at=datetime.now(timezone.utc).isoformat())
            self.flush()

    def finish(self):
        if any(item['status'] != 'passed' for item in self.data['checks']):
            raise DiagnosticFailure('incomplete_check', '测试未完整执行，不能判定可用。', '请重新运行测试。')
        self.data.update(status='passed', finished_at=datetime.now(timezone.utc).isoformat())
        self.flush()


def run_worker(probe):
    payload = json.loads(sys.stdin.read())
    path = Path(os.environ['QQBOT_DIAGNOSTIC_REPORT'])
    report = DiagnosticReport(path, payload['report'], (payload.get('refresh_token', ''), payload.get('proxy_url', '')))
    try:
        probe(payload, report, path.parent / 'sample')
        report.finish()
    except Exception as error:
        report.fail(error)


class DiagnosticRunner:
    """One job per feature. A hard process deadline also stops stuck SDK calls."""
    def __init__(self, module, checks, timeout=180):
        self.module, self.checks, self.timeout = module, checks, timeout
        self.task = self.process = self.report_path = None
        self.data = {'status': 'idle', 'checks': []}

    @property
    def running(self):
        return self.task is not None and not self.task.done()

    def snapshot(self):
        if self.report_path and self.report_path.is_file():
            try:
                if self.report_path.stat().st_size <= 64 * 1024:
                    updated = json.loads(self.report_path.read_text('utf-8'))
                    if updated.get('run_id') == self.data.get('run_id'):
                        self.data = updated
            except (OSError, ValueError):
                pass  # Atomic replace may briefly contend on Windows.
        return {**self.data, 'running': self.running, 'timeout_seconds': self.timeout}

    def start(self, payload):
        if self.running:
            from src.feature.atom.errors import FeatureBusyError
            raise FeatureBusyError('已有可用性测试正在运行，请等待结果')
        self.report_path = None
        self.data = {
            'run_id': uuid4().hex, 'status': 'running',
            'started_at': datetime.now(timezone.utc).isoformat(), 'duration_ms': 0,
            'checks': [{'id': key, 'label': label, 'status': 'pending', 'message': ''} for key, label in self.checks],
        }
        self.task = asyncio.create_task(self._execute({**payload, 'report': self.data}))
        return self.snapshot()

    def _failed(self, code, message):
        self.snapshot()
        current = next((c for c in self.data['checks'] if c['status'] == 'running'), None)
        current = current or next((c for c in self.data['checks'] if c['status'] == 'pending'), self.data['checks'][-1])
        current.update(status='failed', code=code, message=message, hint='检查网络/代理和功能依赖后重试。')
        for check in self.data['checks']:
            if check['status'] in {'pending', 'running'}:
                check.update(status='skipped', message='测试提前结束，未执行。')
        self.data.update(status='failed', finished_at=datetime.now(timezone.utc).isoformat())

    async def _execute(self, payload):
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix='qqbot-diagnostic-') as directory:
            self.report_path = Path(directory) / 'report.json'
            try:
                environment = {**os.environ, 'QQBOT_DIAGNOSTIC_REPORT': str(self.report_path)}
                source_root = str(Path(__file__).resolve().parents[3])
                environment['PYTHONPATH'] = source_root + os.pathsep + environment.get('PYTHONPATH', '')
                # Credentials travel over stdin, never argv, reports or SDK logs.
                self.process = await asyncio.create_subprocess_exec(
                    sys.executable, '-m', self.module, cwd=directory, env=environment,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await asyncio.wait_for(self.process.communicate(json.dumps(payload, ensure_ascii=False).encode('utf-8')), self.timeout)
                result = self.snapshot()
                if self.process.returncode or result['status'] not in {'passed', 'failed'}:
                    self._failed('worker_failed', '测试进程异常退出，未取得完整结果。请检查依赖与运行环境。')
            except TimeoutError:
                self._failed('timeout', f'测试超过 {self.timeout} 秒，已终止测试进程。')
            except asyncio.CancelledError:
                self._failed('cancelled', '服务关闭，测试已取消。')
                raise
            except Exception:
                self._failed('worker_failed', '无法启动测试进程，请检查 Python 运行环境及临时目录权限。')
            finally:
                if self.process and self.process.returncode is None:
                    self.process.kill()
                    await self.process.wait()
                self.report_path = None
                self.process = None
                self.data['duration_ms'] = round((time.monotonic() - started) * 1000)
        # TemporaryDirectory cleans images/PDFs after the worker has stopped.

    async def stop(self):
        if self.running:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            if self.data['status'] == 'running':
                self._failed('cancelled', '服务关闭，测试已取消。')
