"""Run via docker compose exec -T ai-sandbox python /opt/worker/selftest.py.
Uses only synthetic files in reserved test scenes; never contacts QQ.
"""
import base64
import io
import json
import socket

from PIL import Image, ImageDraw, ImageFont


def rpc(op, scene="group:9876543210", **args):
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(150)
        sock.connect("/control/sandbox.sock")
        with sock.makefile("rwb") as pipe:
            pipe.write(json.dumps({**args, "op": op, "scene": scene}).encode() + b"\n")
            pipe.flush()
            return json.loads(pipe.readline())


def run(command, **args):
    return rpc("run", command=command, **args)


def main():
    assert rpc("status")["success"]
    assert int(run("id -u")["output"].strip()) >= 10000
    assert run("printf hello > report.txt")["success"]
    assert rpc("read", path="report.txt")["content"] == "hello"
    assert base64.b64decode(rpc("export", path="report.txt")["data"]) == b"hello"
    for path in ("../etc/passwd", "/etc/passwd", "a/../../etc/passwd"):
        assert not rpc("export", path=path)["success"]
    assert run("ln -s /etc/passwd escaped.txt")["success"]
    assert not rpc("export", path="escaped.txt")["success"]
    assert not rpc("put", path="escaped.txt", data="eA==")["success"]
    assert not run("echo bad > /etc/sandbox-escape-check")["success"]
    assert not run("ls /app/data")["success"]
    assert not run("ls /control")["success"]
    assert not run("test -e /var/run/docker.sock")["success"]
    assert not run("python -c 'import socket; socket.create_connection((\"1.1.1.1\",443),1)'")["success"]
    assert rpc("put", scene="private:9876543211", path="private.txt", data="c2VjcmV0")["success"]
    assert not run("cat /workspace/private-9876543211/private.txt")["success"]
    assert not run("python -c 'open(\"huge.bin\",\"wb\").truncate(22*1024*1024)'")["success"]
    assert run("sleep 5", timeout=1)["timed_out"]
    assert run("python -c 'print(\"x\"*100000)' ")["truncated"]
    assert run("setsid sleep 300 > /dev/null 2>&1 &")["success"]
    assert run("echo after-background-cleanup")["success"]
    print("PASS: file operations, unprivileged UID, traversal/symlink rejection, readonly root, no secrets/socket/network, scene isolation, file/output/time limits", flush=True)
    image = Image.new("RGB", (1400, 450), "white")
    font = ImageFont.truetype("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 60)
    ImageDraw.Draw(image).text((60, 60), "订单编号 12345\n合计 88.50 元", font=font, fill="black", spacing=30)
    for format in ("PNG", "PDF"):
        buffer = io.BytesIO()
        image.save(buffer, format=format)
        result = rpc("analyze", scene="private:0", data=base64.b64encode(buffer.getvalue()).decode(), pdf=format == "PDF")
        assert result["success"], result
        text = result["pages"][0]["text"]
        assert "12345" in text and "88.50" in text, text
        assert result["pages"][0].get("image")
        print(f"PASS: {format} OCR: {text.strip()}", flush=True)


if __name__ == "__main__":
    main()
