from pathlib import Path
from fastapi import FastAPI, HTTPException
from .storage import Conflict


class TimingAdmin:
    def __init__(self, feature):
        self.feature = feature

    def register_routes(self, app: FastAPI):
        f = self.feature

        @app.get('/api/timing/tasks')
        async def listing():
            return {'tasks': [f.describe(t) for t in f.store.list()], 'enabled': f.enabled, 'error': f.last_error, 'timezone': 'Asia/Shanghai'}

        @app.post('/api/timing/tasks')
        async def create(body: dict):
            try:
                return f.save(body)
            except (ValueError, KeyError, TypeError) as exc:
                raise HTTPException(400, str(exc)) from exc

        @app.put('/api/timing/tasks/{identifier}')
        async def update(identifier: str, body: dict):
            previous = f.store.get(identifier)
            if not previous:
                raise HTTPException(404, '任务不存在')
            data = {k: v for k, v in previous.items() if k in {'kind', 'scene', 'flag', 'time', 'segments', 'prompt', 'enabled', 'delivery', 'requester', 'owner_grant', 'bot_qq', 'autonomous'}}
            expected = body.get('revision')
            if not isinstance(expected, int):
                raise HTTPException(400, '编辑时须提供revision版本号')
            data.update({k: v for k, v in body.items() if k != 'revision'})
            if 'content' in body:
                data.pop('segments', None)
            try:
                return f.save(data, identifier, expected)
            except Conflict as exc:
                raise HTTPException(409, str(exc)) from exc
            except (ValueError, KeyError, TypeError) as exc:
                raise HTTPException(400, str(exc)) from exc

        @app.delete('/api/timing/tasks/{identifier}')
        async def delete(identifier: str, revision: int):
            try:
                if not f.store.delete(identifier, expected=revision):
                    raise HTTPException(404, '任务不存在')
                f.wake.set()
                return {'success': True}
            except Conflict as exc:
                raise HTTPException(409, str(exc)) from exc

    @property
    def panel_html(self):
        return Path(__file__).with_name('panel.html').read_text('utf-8')

    @property
    def javascript(self):
        return Path(__file__).with_name('panel.js').read_text('utf-8')
