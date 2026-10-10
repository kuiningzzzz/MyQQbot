"""File transport and fixed OCR jobs. Arbitrary commands belong to native dsh tools."""
import asyncio
import base64
import hashlib
import json
import sys
import tempfile
from pathlib import Path

from .storage import atomic_write


class LocalWorkspace:
    def __init__(self, root):
        self.root = root

    def scene_dir(self, scene):
        name = hashlib.sha256(scene.encode()).hexdigest()[:20]
        directory = self.root / "files" / name
        if directory.resolve() != directory.absolute():
            raise ValueError('会话目录不能是符号链接')
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def resolve(self, scene, value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("文件路径无效")
        directory = self.scene_dir(scene)
        if directory.resolve() != directory.absolute():
            raise ValueError("会话目录不能是符号链接")
        base = directory.resolve()
        path = Path(value)
        if not path.is_absolute():
            # Native tools use workspace-relative paths; attachment downloads
            # may also use just a filename relative to the scene directory.
            path = self.root / path if path.parts[0] == "files" else base / path
        if path.resolve() != path.absolute() or not path.resolve().is_relative_to(base):
            raise ValueError("文件必须属于当前会话files目录，不能发送服务器或记忆文件")
        if path.is_file() and path.stat().st_nlink > 1:
            raise ValueError('不能使用硬链接文件')
        return path

    async def put(self, scene, path, data):
        target = self.resolve(scene, path)
        if len(data) > 20 * 1024 * 1024:
            raise ValueError("文件超过20MiB")
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_bytes, data)
        return {"success": True, "path": str(target), "bytes": len(data)}

    async def request(self, op, scene="", **values):
        if op == "status":
            return {"success": True, "workspace": str(self.root), "mode": "dsh-native", "shell": "owner-only"}
        if op != "export":
            raise ValueError("请使用dsh原生read/write/edit；任意命令仅允许主人")
        path = self.resolve(scene, values.get("path"))
        if not path.is_file() or path.stat().st_nlink > 1 or path.stat().st_size > 20 * 1024 * 1024:
            raise ValueError("文件不存在或超过20MiB")
        data = await asyncio.to_thread(path.read_bytes)
        return {"success": True, "data": base64.b64encode(data).decode(), "bytes": len(data)}

    async def analyze(self, data, pdf=False):
        with tempfile.TemporaryDirectory(prefix="qqbot-ocr-") as directory:
            path = Path(directory) / ("input.pdf" if pdf else "input.img")
            path.write_bytes(data)
            script = Path(__file__).with_name("ocr.py")
            process = await asyncio.create_subprocess_exec(sys.executable, str(script), str(path), "10", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), 120)
                if process.returncode:
                    return {"success": False, "error": "OCR不可用，请检查Tesseract/Poppler安装"}
                return {"success": True, **json.loads(stdout)}
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
