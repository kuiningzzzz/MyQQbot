"""Linux-only, single-threaded broker. NEVER run this on the host.

The Compose sandbox has no network, host bind mounts, Docker socket or secrets.
Only this root broker sees /control; all parsing and shell commands run as a
different unprivileged UID per scene. Rootfs and worker code are read-only.
"""
from __future__ import annotations

import base64
import json
import os
import re
import selectors
import shutil
import signal
import socketserver
import stat
import subprocess
import time
import uuid
from pathlib import Path, PurePosixPath

MAX_FILE = 20 * 1024 * 1024
MAX_FRAME = 30 * 1024 * 1024
ROOT = Path("/workspace")
USERS = {}


def scene_home(scene):
    if not re.fullmatch(r"(?:group|private):[0-9]{1,20}", scene):
        raise ValueError("Invalid scene")
    if scene not in USERS:
        if len(USERS) >= 200:
            raise ValueError("工作区数量达到上限，请重启sandbox清空临时文件")
        USERS[scene] = 10000 + len(USERS)
    uid = USERS[scene]
    home = ROOT / scene.replace(":", "-")
    if not home.exists():
        home.mkdir(mode=0o700)
        os.chown(home, uid, uid)
    if home.is_symlink() or not home.is_dir():
        raise ValueError("Invalid workspace")
    return home, uid


def parts(path):
    if not isinstance(path, str) or not path or len(path) > 500 or "\\" in path or "\x00" in path:
        raise ValueError("工作路径必须为相对路径")
    p = PurePosixPath(path)
    if p.is_absolute() or any(s in {"", ".", ".."} for s in path.split("/")):
        raise ValueError("禁止越出工作目录")
    return p.parts


def open_file(home, path, uid, write=False):
    """dirfd traversal prevents symlink escapes and race-based path replacement."""
    names = parts(path)
    fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in names[:-1]:
            if write:
                try:
                    os.mkdir(name, 0o700, dir_fd=fd)
                    os.chown(name, uid, uid, dir_fd=fd, follow_symlinks=False)
                except FileExistsError:
                    pass
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        flags = os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_WRONLY | os.O_CREAT if write else os.O_RDONLY)
        leaf = os.open(names[-1], flags, 0o600, dir_fd=fd)
        info = os.fstat(leaf)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            os.close(leaf)
            raise ValueError("仅允许普通文件，禁止链接和设备")
        if write:
            os.fchown(leaf, uid, uid)
            os.ftruncate(leaf, 0)
        return os.fdopen(leaf, "wb" if write else "rb")
    finally:
        os.close(fd)


def kill_user(uid):
    # Kill detached/setsid descendants as well as the original process group.
    for _ in range(20):
        found = False
        for item in Path("/proc").iterdir():
            if not item.name.isdigit():
                continue
            try:
                if item.stat().st_uid == uid:
                    os.kill(int(item.name), signal.SIGKILL)
                    found = True
            except (FileNotFoundError, ProcessLookupError):
                pass
        if not found:
            return
        time.sleep(0.01)


def execute(home, uid, argv, timeout, output_limit=64000):
    import resource
    def limits():
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_FILE, MAX_FILE))
        resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_CPU, (timeout, timeout + 1))
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": str(home), "TMPDIR": str(home), "PYTHONDONTWRITEBYTECODE": "1", "OMP_THREAD_LIMIT": "1"}
    process = subprocess.Popen(argv, cwd=home, env=env, user=uid, group=uid, extra_groups=[], preexec_fn=limits, start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    output = bytearray()
    timed_out = truncated = False
    start = time.monotonic()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                if time.monotonic() - start > timeout:
                    timed_out = True
                    break
                for key, _ in selector.select(0.1):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fd)
                        continue
                    output.extend(chunk[:max(0, output_limit - len(output))])
                    if len(output) >= output_limit:
                        truncated = True
                        break
                if truncated:
                    break
    finally:
        kill_user(uid)
        process.wait(timeout=5)
        process.stdout.close()
    return {"success": process.returncode == 0 and not timed_out and not truncated, "exit_code": process.returncode, "timed_out": timed_out, "truncated": truncated, "output": output.decode("utf-8", errors="replace")}


def dispatch(request):
    if request.get("op") == "status":
        return {"success": True, "isolation": "docker-no-network", "temporary": True, "max_file_bytes": MAX_FILE}
    home, uid = scene_home(request.get("scene", ""))
    kill_user(uid)
    op = request.get("op")
    if op == "put":
        data = base64.b64decode(request["data"], validate=True)
        if len(data) > MAX_FILE:
            raise ValueError("文件超过20MiB")
        with open_file(home, request["path"], uid, True) as file:
            file.write(data)
        return {"success": True, "path": request["path"], "bytes": len(data)}
    if op in {"read", "export"}:
        with open_file(home, request["path"], uid) as file:
            data = file.read(MAX_FILE + 1)
        if len(data) > MAX_FILE:
            raise ValueError("文件超过20MiB")
        if op == "export":
            return {"success": True, "data": base64.b64encode(data).decode(), "bytes": len(data)}
        offset = max(0, int(request.get("offset", 0)))
        text = data.decode("utf-8-sig", errors="replace")
        return {"success": True, "content": text[offset:offset + 20000], "offset": offset, "total_chars": len(text), "truncated": len(text) > offset + 20000}
    if op == "list":
        rows = []
        for parent, dirs, files in os.walk(home, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not Path(parent, d).is_symlink())
            for name in sorted(files):
                path = Path(parent, name)
                if not path.is_symlink() and path.is_file():
                    rows.append({"path": path.relative_to(home).as_posix(), "bytes": path.stat().st_size})
                    if len(rows) >= 300:
                        return {"success": True, "files": rows, "truncated": True}
        return {"success": True, "files": rows, "truncated": False}
    if op == "run":
        command = request.get("command")
        if not isinstance(command, str) or not command.strip() or len(command) > 16000:
            raise ValueError("命令必须为1至16000字符")
        return execute(home, uid, ["/bin/sh", "-c", command], min(60, max(1, int(request.get("timeout", 30)))))
    if op == "analyze":
        data = base64.b64decode(request["data"], validate=True)
        if len(data) > MAX_FILE:
            raise ValueError("文件超过20MiB")
        job = home / (".ocr-" + uuid.uuid4().hex)
        job.mkdir(mode=0o700)
        os.chown(job, uid, uid)
        name = "input.pdf" if request.get("pdf") else "input.image"
        with open_file(job, name, uid, True) as file:
            file.write(data)
        try:
            response = execute(job, uid, ["python", "-I", "/opt/worker/analyze.py", name, str(min(10, max(1, int(request.get("pages", 10)))))], 120, 12 * 1024 * 1024)
            if not response["success"]:
                raise ValueError("OCR失败或超时：" + response["output"][-1000:])
            return {"success": True, **json.loads(response["output"])}
        finally:
            if job.is_symlink():
                job.unlink()
            else:
                shutil.rmtree(job)
    raise ValueError("Unsupported sandbox operation")


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(150)
        try:
            raw = self.rfile.readline(MAX_FRAME + 1)
            if len(raw) > MAX_FRAME or not raw.endswith(b"\n"):
                raise ValueError("RPC frame too large")
            response = dispatch(json.loads(raw))
        except Exception as exc:
            response = {"success": False, "error": str(exc)[:1500]}
        self.wfile.write(json.dumps(response, ensure_ascii=False).encode() + b"\n")


def main():
    if os.name != "posix" or os.getuid() != 0 or not Path("/.dockerenv").exists():
        raise SystemExit("Sandbox broker must run inside its dedicated Docker container")
    ROOT.mkdir(exist_ok=True)
    os.chmod(ROOT, 0o711)
    os.chmod("/dev/shm", 0o700)
    control = Path("/control")
    control.mkdir(exist_ok=True)
    os.chmod(control, 0o700)
    socket = control / "sandbox.sock"
    socket.unlink(missing_ok=True)
    with socketserver.UnixStreamServer(str(socket), Handler) as server:
        os.chmod(socket, 0o600)
        server.serve_forever()


if __name__ == "__main__":
    main()
