"""Network relay for isolated Docker runtimes: `python -m toolkernel._relay`.

Runtime containers sit on an internal Docker network with no route out. This relay is the
only container on both that network and the default one. It does exactly two things:

- inbound  (port 8890, published to the host's 127.0.0.1): the gateway sends HTTP requests with an
  `X-Runtime-Target: <container name>` header; they are piped to that runtime's port 8888.
- outbound (port 7301, internal network only): runtimes reach the gateway's bridge through here,
  and nowhere else.
"""

from __future__ import annotations

import asyncio
import os
import re

BRIDGE_TARGET = os.environ.get("TOOLKERNEL_RELAY_BRIDGE", "host.docker.internal:7301")
TARGET_RE = re.compile(rb"^x-runtime-target:\s*(toolkernel-rt-[a-z0-9_-]{1,80})\s*$", re.I | re.M)


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


async def _join(client_r, client_w, host: str, port: int, head: bytes = b"") -> None:
    try:
        up_r, up_w = await asyncio.open_connection(host, port)
    except OSError:
        client_w.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await client_w.drain()
        client_w.close()
        return
    if head:
        up_w.write(head)
    await asyncio.gather(_pipe(client_r, up_w), _pipe(up_r, client_w))


async def inbound(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        head = await reader.readuntil(b"\r\n\r\n")
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        writer.close()
        return
    m = TARGET_RE.search(head)
    if not m:
        writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()
        return
    await _join(reader, writer, m.group(1).decode(), 8888, head)


async def outbound(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    host, port = BRIDGE_TARGET.rsplit(":", 1)
    await _join(reader, writer, host, int(port))


async def main() -> None:
    a = await asyncio.start_server(inbound, "0.0.0.0", 8890)
    b = await asyncio.start_server(outbound, "0.0.0.0", 7301)
    print(f"toolkernel relay ready · inbound :8890 · outbound :7301 → {BRIDGE_TARGET}", flush=True)
    async with a, b:
        await asyncio.gather(a.serve_forever(), b.serve_forever())


if __name__ == "__main__":
    asyncio.run(main())
