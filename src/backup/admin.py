import asyncio
import os
import shutil
import uuid
from pathlib import Path

from fastapi import BackgroundTasks, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import archive
from .common import BackupError


class RestoreRequest(BaseModel):
    name: str
    mode: str
    confirmation: str


def register(app, control):
    store = control.store
    lock = asyncio.Lock()

    @app.exception_handler(BackupError)
    async def backup_error(request, error):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=409, content={'detail': str(error)})

    @app.get('/api/backups')
    async def packages():
        return {'packages': await asyncio.to_thread(store.list), 'status': store.status(),
                'supported': bool(os.environ.get('MYQQBOT_BACKUP_SUPERVISED') and control.shutdown)}

    @app.get('/api/backups/status')
    async def status():
        return store.status()

    @app.post('/api/backups')
    async def create(background_tasks: BackgroundTasks):
        async with lock:
            result = control.request('create')
        background_tasks.add_task(control.stop)
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=202, content=result)

    @app.post('/api/backups/import')
    async def upload(request: Request):
        async with lock:
            if control.requested:
                raise BackupError('服务正在维护')
            name = 'import-' + uuid.uuid4().hex + '.zip'
            target = store.packages / name
            part = target.with_suffix('.upload')
            count = 0
            try:
                with part.open('xb') as stream:
                    os.chmod(part, 0o600)
                    async for chunk in request.stream():
                        count += len(chunk)
                        if count > archive.MAX_BYTES:
                            raise HTTPException(413, '备份包不能超过 100 GiB')
                        if shutil.disk_usage(store.store).free < len(chunk) + 64 * 1024**2:
                            raise BackupError('磁盘空间不足，上传已停止')
                        await asyncio.to_thread(stream.write, chunk)
                manifest = await asyncio.to_thread(archive.inspect, part)
                os.replace(part, target)
                await asyncio.to_thread(store.metadata, target, manifest, imported=True)
                return {'name': name, 'created_at': manifest['created_at'], 'files': len(manifest['files'])}
            finally:
                part.unlink(missing_ok=True)

    @app.get('/api/backups/{name}/download')
    async def download(name: str):
        return FileResponse(store.package(name), media_type='application/zip', filename=name,
                            headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})

    @app.get('/api/backups/{name}/preview')
    async def preview(name: str):
        async with lock:
            return await asyncio.to_thread(store.preview, name)

    @app.delete('/api/backups/{name}')
    async def delete(name: str):
        async with lock:
            if control.requested:
                raise BackupError('服务正在维护')
            path = store.package(name)
            path.unlink()
            path.with_suffix('.metadata.json').unlink(missing_ok=True)
            return {'ok': True}

    @app.post('/api/backups/restore')
    async def restore(body: RestoreRequest, background_tasks: BackgroundTasks):
        if body.confirmation != ('覆盖还原' if body.mode == 'overwrite' else '增量还原'):
            raise BackupError('请先确认还原模式及影响')
        async with lock:
            # Fully validate BEFORE stopping service; validate again in the cold
            # process to defend against a changed package or incomplete upload.
            await asyncio.to_thread(store.preview, body.name)
            result = control.request('restore', name=body.name, mode=body.mode)
        background_tasks.add_task(control.stop)
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=202, content=result)


def view_html():
    return Path(__file__).with_name('panel.html').read_text('utf-8')


def javascript():
    return Path(__file__).with_name('panel.js').read_text('utf-8')
