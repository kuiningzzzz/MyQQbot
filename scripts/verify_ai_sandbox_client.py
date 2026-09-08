"""Mounted with only src/feature/ai/sandbox.py as /client.py in a test container."""
import asyncio
import base64

from client import SandboxClient


async def main():
    client = SandboxClient()
    assert (await client.request("status"))["success"]
    scene = "group:9876543220"
    assert (await client.put(scene, "input.txt", "跨容器读写".encode()))["success"]
    assert (await client.request("run", scene, command="cp input.txt output.txt"))["success"]
    assert (await client.request("read", scene, path="output.txt"))["content"] == "跨容器读写"
    exported = await client.request("export", scene, path="output.txt")
    assert base64.b64decode(exported["data"]).decode() == "跨容器读写"
    print("PASS: actual async bot client -> readonly Unix socket mount -> isolated workspace -> export")


asyncio.run(main())
