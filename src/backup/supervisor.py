"""Keep the admin restart transparent; snapshots run with no bot process alive."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

from filelock import FileLock, Timeout

from .archive import ENV_NAMES
from .common import BackupError, read_json
from .service import BackupStore

MAINTENANCE_EXIT = 75


def child_environment(store):
    env = dict(os.environ)
    from dotenv import dotenv_values
    # Keep normal deployment precedence: explicit system/Docker environment wins.
    for file in ('.env', '.env.host'):
        for key, value in dotenv_values(store.root / file).items():
            if value is not None:
                env.setdefault(key, value)
    saved = read_json(store.data / '.deployment-environment.json', {})
    if not isinstance(saved, dict) or set(saved) - ENV_NAMES or any(not isinstance(v, str) for v in saved.values()):
        raise BackupError('还原的环境配置无效')
    if (store.data / '.deployment-environment.json').exists():
        # A restored snapshot is the complete effective project environment,
        # including the absence of a setting. Do not leak current-deployment
        # keys back into a bot which did not have them at the snapshot time.
        for key in ENV_NAMES:
            env.pop(key, None)
        env.update(saved)
    env['MYQQBOT_BACKUP_SUPERVISED'] = '1'
    return env


def run():
    store = BackupStore(Path.cwd())
    try:
        lock = FileLock(str(store.store / 'service.lock'), timeout=0)
        lock.acquire()
    except Timeout:
        print('MyQQbot 已有受管理进程正在使用此 data 目录', file=sys.stderr)
        return 1
    child = None
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True
        if child is not None and child.poll() is None:
            # Forward gracefully; never run the pending operation after an
            # externally requested shutdown or a forcibly killed runtime.
            child.terminate()

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        recovered = store.recover()
        if store.pending.exists():
            store.pending.unlink()
            if not recovered:
                store.set_status('failed', '上次操作未完成，已取消；原数据保留')
        while not stopping:
            # A rollback can itself be interrupted or fail (e.g. locked files).
            # Never initialize bot features while an install journal remains.
            store.recover()
            store.cleanup_incomplete()
            child = subprocess.Popen([sys.executable, '-m', 'src.main', '--runtime'], cwd=store.root, env=child_environment(store))
            code = child.wait()
            if stopping:
                return 0
            if code != MAINTENANCE_EXIT or not store.pending.exists():
                return code
            # Child's asyncio.run has finished all executor threads, so no
            # Python/background task can mutate a DB during capture/restore.
            store.execute_pending()
        return 0
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            child.wait()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        lock.release()
