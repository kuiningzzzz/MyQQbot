"""Single-flight, rate-limited browser re-login and automatic token recovery."""
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

from src.feature.atom import FeatureBusyError


async def terminate_tree(process):
    if os.name == 'nt':
        if process.returncode is None:
            killer = await asyncio.create_subprocess_exec('taskkill', '/PID', str(process.pid), '/T', '/F',
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await killer.wait()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()


async def browser_login(payload, timeout=180):
    process = None
    with tempfile.TemporaryDirectory(prefix='qqbot-pixiv-login-') as directory:
        environment = dict(os.environ)
        environment['PYTHONPATH'] = str(Path(__file__).resolve().parents[3]) + os.pathsep + environment.get('PYTHONPATH', '')
        options = {'start_new_session': True} if os.name != 'nt' else {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}
        try:
            process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'src.feature.pixiv.login_worker',
                cwd=directory, env=environment, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, **options)
            output, _ = await asyncio.wait_for(process.communicate(json.dumps(payload).encode('utf-8')), timeout)
            if process.returncode or len(output) > 64 * 1024:
                raise ValueError('worker failed')
            return json.loads(output)
        except TimeoutError:
            return {'ok': False, 'code': 'timeout', 'message': '登录超过 180 秒，已终止。请检查网络、账号密码及是否需要人工验证。'}
        except asyncio.CancelledError:
            raise
        except Exception:
            return {'ok': False, 'code': 'worker_failed', 'message': '无法启动或读取登录进程，请检查 Python 和浏览器依赖。'}
        finally:
            if process:
                await terminate_tree(process)


class PixivAuth:
    cooldown_seconds = 600

    def __init__(self, config, client, save_token):
        self.config, self.client, self.save_token = config, client, save_token
        self.task = None
        self.last_attempt = None
        self.state = {'status': 'idle', 'message': '尚未运行自动登录。'}

    @property
    def running(self):
        return self.task is not None and not self.task.done()

    def snapshot(self):
        try:
            account = self.config.load_credentials()
            error = ''
        except ValueError as exc:
            account, error = {}, str(exc)
        return {**self.state, 'running': self.running, 'configured': bool(account),
                'username': account.get('username', ''), 'totp_configured': bool(account.get('totp_secret')),
                'configuration_error': error}

    def ensure_idle(self):
        if self.running:
            raise FeatureBusyError('Pixiv 正在登录，请等待结果后再修改认证配置。')

    def reset(self):
        self.last_attempt = None
        self.state = {'status': 'idle', 'message': '认证配置已修改。'}

    def start(self, automatic=False):
        if self.running:
            if automatic:
                return self.task
            raise FeatureBusyError('已有 Pixiv 登录正在运行，请等待结果。')
        account = self.config.load_credentials()
        if not account:
            raise ValueError('请先保存 Pixiv 账号和密码。')
        if automatic and self.last_attempt is not None and time.monotonic() - self.last_attempt < self.cooldown_seconds:
            raise ValueError('自动登录处于 10 分钟冷却期，请在 admin 查看上次登录结果。')
        self.last_attempt = time.monotonic()
        self.state = {'status': 'running', 'message': '正在通过浏览器登录 Pixiv，最长等待 180 秒。',
                      'started_at': datetime.now(timezone.utc).isoformat()}
        self.task = asyncio.create_task(self._run({**account, 'proxy_url': self.client.proxy_url}))
        return self.task

    async def _run(self, payload):
        try:
            result = await browser_login(payload)
            if not result.get('ok'):
                self.state.update(status='failed', code=result.get('code', 'login_failed'),
                                  message=result.get('message', '登录失败。'))
                return False
            # Save first: a persistence failure must not claim successful recovery.
            self.save_token(result['refresh_token'])
            self.state.update(status='passed', message='已自动获取并保存 token，Pixiv 客户端已重新初始化。', code='')
            return True
        except asyncio.CancelledError:
            self.state.update(status='failed', code='cancelled', message='服务关闭，登录已取消。')
            raise
        except Exception:
            self.state.update(status='failed', code='save_failed', message='登录或 token 保存失败，请检查认证配置与磁盘权限。')
            return False
        finally:
            self.state['finished_at'] = datetime.now(timezone.utc).isoformat()

    async def recover(self):
        try:
            return await asyncio.shield(self.start(automatic=True))
        except ValueError:
            return False

    async def stop(self):
        if self.running:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            if self.state['status'] == 'running':
                self.state.update(status='failed', code='cancelled', message='服务关闭，登录已取消。')
