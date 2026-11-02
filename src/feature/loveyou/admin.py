import base64
import binascii
import io
import warnings
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from PIL import Image


class Entry(BaseModel):
    name: str = Field(max_length=80)
    amount: str = Field(max_length=20)
    previous_name: str | None = None
    version: int | None = None


class Deletion(BaseModel):
    name: str
    version: int


class PaymentImage(BaseModel):
    data_url: str = Field(max_length=12_000_000)


def decode_image(value):
    try:
        header, encoded = value.split(',', 1)
        if header not in {'data:image/png;base64', 'data:image/jpeg;base64', 'data:image/webp;base64'}:
            raise ValueError('请上传PNG、JPEG或WebP图片。')
        data = base64.b64decode(encoded, validate=True)
        if len(data) > 8 * 1024 * 1024:
            raise ValueError('图片不能超过8 MiB。')
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.width * image.height > 20_000_000 or image.format not in {'PNG', 'JPEG', 'WEBP'}:
                    raise ValueError('图片尺寸过大或格式不支持。')
                image.load()
                output = io.BytesIO()
                image.convert('RGB').save(output, format='PNG')
        if output.tell() > 8 * 1024 * 1024:
            raise ValueError('转换后的图片超过8 MiB，请先缩小图片。')
        return output.getvalue()
    except (OSError, binascii.Error, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError('无法读取图片，请上传有效的PNG、JPEG或WebP图片。') from exc


class LoveyouAdmin:
    def __init__(self, feature):
        self.feature = feature

    def register_routes(self, app: FastAPI):
        store = self.feature.store

        @app.get('/api/loveyou')
        async def get_state():
            entries, image = store.snapshot()
            return {'entries': entries, 'has_image': image is not None}

        @app.post('/api/loveyou/entries')
        async def edit_entry(body: Entry):
            try:
                store.edit(body.name, body.amount, body.previous_name, body.version)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            return {'success': True}

        @app.post('/api/loveyou/entries/delete')
        async def delete_entry(body: Deletion):
            try:
                store.delete(body.name, body.version)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            return {'success': True}

        @app.get('/api/loveyou/image')
        async def get_image():
            _, image = store.snapshot()
            if not image:
                raise HTTPException(404, '尚未设置图片')
            return Response(image, media_type='image/png', headers={'Cache-Control': 'no-store'})

        @app.post('/api/loveyou/image')
        async def set_image(body: PaymentImage):
            import asyncio
            try:
                image = await asyncio.to_thread(decode_image, body.data_url)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            store.set_image(image)
            return {'success': True}

        @app.delete('/api/loveyou/image')
        async def delete_image():
            store.set_image(None)
            return {'success': True}

    @property
    def panel_html(self):
        return Path(__file__).with_name('panel.html').read_text(encoding='utf-8')

    @property
    def javascript(self):
        return Path(__file__).with_name('panel.js').read_text(encoding='utf-8')
