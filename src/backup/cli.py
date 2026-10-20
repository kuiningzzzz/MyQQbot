"""Offline restore: python -m src.backup.cli restore package.zip --mode overwrite."""
import argparse
from pathlib import Path
import shutil
import sys
import uuid

from filelock import FileLock, Timeout

from .archive import inspect
from .common import BackupError
from .service import BackupStore


def main():
    parser = argparse.ArgumentParser(description='MyQQbot 离线备份/还原；请先停止机器人。包中包含密钥，请勿公开。')
    parser.add_argument('action', choices=['create', 'restore', 'verify'])
    parser.add_argument('package', nargs='?')
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--mode', choices=['overwrite', 'incremental'], default='overwrite')
    args = parser.parse_args()
    store = BackupStore(args.root)
    try:
        with FileLock(str(store.store / 'service.lock'), timeout=0):
            store.recover()
            store.cleanup_incomplete()
            if args.action == 'create':
                print(store.make_backup())
            else:
                if not args.package:
                    parser.error('需要备份包路径')
                path = Path(args.package).resolve()
                manifest = inspect(path)
                if args.action == 'verify':
                    print(f'校验通过：{manifest["created_at"]}，{len(manifest["files"])} 个文件')
                else:
                    # Never restore source or execute a command from a package.
                    name = 'import-' + uuid.uuid4().hex + '.zip'
                    target = store.packages / name
                    shutil.copyfile(path, target)
                    target.chmod(0o600)
                    store.metadata(target, manifest, imported=True)
                    safety = store.restore(name, args.mode)
                    print('还原完成；原数据备份：' + safety)
        return 0
    except (BackupError, Timeout) as error:
        print('操作失败：' + ('服务正在运行，请先停止' if isinstance(error, Timeout) else str(error)), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
