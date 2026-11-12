"""Upstream manager (spec §9.2): connects to real MCP servers and keeps the sessions open."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import mcp_types as types
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, get_default_environment, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.shared.exceptions import MCPError

from .config import resolve_secrets

log = logging.getLogger("toolkernel.upstream")


class ToolUnavailable(Exception):
    pass


class UpstreamToolError(Exception):
    pass


class Upstream:
    def __init__(self, name: str, config: dict[str, Any], manager: UpstreamManager):
        self.name = name
        self.config = config
        self.manager = manager
        self.status = "disabled" if config.get("disabled") else "connecting"
        self.error: str | None = None
        self.tools: list[dict[str, Any]] = []
        self.server_title: str | None = None
        self.server_description: str | None = None
        self.server_version: str | None = None
        self.instructions: str | None = None
        self.connected_at: float | None = None
        self.calls = 0
        self.failures = 0
        self.log_path = manager.log_dir / f"{name}.log"
        self._session: ClientSession | None = None
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        if self.config.get("disabled"):
            self.status = "disabled"
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name=f"upstream:{self.name}")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._task = None
        self._ready.clear()
        self._session = None

    def _transport(self, cfg: dict[str, Any], stack: contextlib.AsyncExitStack, errlog: Any):
        if cfg.get("command"):
            env = {**get_default_environment(), **(cfg.get("env") or {})}
            cwd = cfg.get("cwd") or self.manager.default_cwd  # relative paths resolve next to mcp.json
            params = StdioServerParameters(command=cfg["command"], args=cfg.get("args") or [], env=env, cwd=cwd)
            return stdio_client(params, errlog=errlog)
        url = cfg.get("url")
        if not url:
            raise ValueError("server config needs either 'command' or 'url'")
        headers = cfg.get("headers") or {}
        kind = (cfg.get("transport") or cfg.get("type") or "").lower()
        if kind == "sse" or (not kind and url.rstrip("/").endswith("/sse")):
            return sse_client(url, headers=headers)
        return streamable_http_client(url, http_client=create_mcp_http_client(headers=headers))

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            self.status = "connecting"
            self.manager.notify(self)
            errlog = open(self.log_path, "a", buffering=1)  # noqa: SIM115
            errlog.write(f"\n--- connecting {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
            try:
                cfg = resolve_secrets(self.config, self.manager.secrets())
                async with contextlib.AsyncExitStack() as stack:
                    streams = await stack.enter_async_context(self._transport(cfg, stack, errlog))
                    session = await stack.enter_async_context(
                        ClientSession(streams[0], streams[1], message_handler=self._on_message,
                                      client_info=types.Implementation(name="toolkernel", version="0.1.0"))
                    )
                    init = await asyncio.wait_for(session.initialize(), timeout=60)
                    info = init.server_info
                    self.server_title = getattr(info, "title", None) or info.name
                    self.server_description = getattr(info, "description", None)
                    self.server_version = info.version
                    self.instructions = init.instructions
                    self._session = session
                    await self.refresh_tools()
                    self.status, self.error, self.connected_at = "ready", None, time.time()
                    self._ready.set()
                    backoff = 1.0
                    self.manager.notify(self)
                    errlog.write(f"connected · {len(self.tools)} tools\n")
                    while not self._stop.is_set():
                        with contextlib.suppress(asyncio.TimeoutError):
                            await asyncio.wait_for(self._stop.wait(), timeout=30)
                            break
                        await asyncio.wait_for(session.send_ping(), timeout=15)
            except asyncio.CancelledError:
                raise
            except BaseException as e:  # noqa: BLE001 (ExceptionGroup from task groups)
                msg = _describe(e)
                self.error = msg
                self.status = "error"
                errlog.write(f"error: {msg}\n")
                log.warning("upstream %s: %s", self.name, msg)
            finally:
                self._ready.clear()
                self._session = None
                errlog.close()
            if self._stop.is_set():
                break
            self.manager.notify(self)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _on_message(self, message: Any) -> None:
        if isinstance(message, types.ToolListChangedNotification):
            asyncio.create_task(self._refresh_and_notify())
        elif isinstance(message, Exception):
            log.debug("upstream %s transport message: %s", self.name, message)

    async def _refresh_and_notify(self) -> None:
        try:
            await self.refresh_tools()
        except Exception as e:  # noqa: BLE001
            log.warning("refresh %s failed: %s", self.name, e)

    async def refresh_tools(self) -> None:
        assert self._session is not None
        tools: list[dict[str, Any]] = []
        cursor = None
        while True:
            params = types.PaginatedRequestParams(cursor=cursor) if cursor else None
            res = await self._session.list_tools(params=params)
            tools += [t.model_dump(by_alias=True, exclude_none=True, mode="json") for t in res.tools]
            cursor = res.next_cursor
            if not cursor:
                break
        self.tools = tools
        await self.manager.on_tools_changed(self)

    # -- calls ---------------------------------------------------------------

    async def call(self, tool: str, arguments: dict[str, Any], timeout: float) -> types.CallToolResult:
        if self.status == "disabled":
            raise ToolUnavailable(f"server {self.name!r} is disabled")
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=self.manager.wait_s)
        except asyncio.TimeoutError:
            raise ToolUnavailable(f"server {self.name!r} is not ready ({self.status}: {self.error or 'connecting'})") from None
        session = self._session
        assert session is not None
        self.calls += 1
        try:
            return await asyncio.wait_for(session.call_tool(tool, arguments), timeout=timeout)
        except asyncio.TimeoutError:
            self.failures += 1
            raise ToolUnavailable(f"{self.name}.{tool} timed out after {timeout:.0f}s") from None
        except MCPError as e:
            self.failures += 1
            raise UpstreamToolError(str(e)) from None
        except Exception as e:  # noqa: BLE001
            self.failures += 1
            raise ToolUnavailable(f"{self.name}.{tool} failed: {_describe(e)}") from None

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "error": self.error,
            "tools": len(self.tools),
            "title": self.server_title,
            "version": self.server_version,
            "description": self.server_description,
            "instructions": self.instructions,
            "connected_at": self.connected_at,
            "transport": "stdio" if self.config.get("command") else "http",
            "config": _redact(self.config),
            "calls": self.calls,
            "failures": self.failures,
        }


def _describe(e: BaseException) -> str:
    if isinstance(e, BaseExceptionGroup):
        return "; ".join(_describe(x) for x in e.exceptions)
    text = str(e) or type(e).__name__
    return f"{type(e).__name__}: {text}" if type(e).__name__ not in text else text


def _redact(cfg: dict[str, Any]) -> dict[str, Any]:
    out = dict(cfg)
    for key in ("env", "headers"):
        if isinstance(out.get(key), dict):
            out[key] = {k: (v if "${" in str(v) else "••••") for k, v in out[key].items()}
    return out


class UpstreamManager:
    def __init__(self, log_dir: Path, wait_s: float,
                 on_tools_changed: Callable[[Upstream], Awaitable[None]],
                 on_status: Callable[[Upstream], None],
                 secrets: Callable[[], dict[str, str]], default_cwd: Path | None = None):
        self.log_dir = log_dir
        self.default_cwd = default_cwd
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.wait_s = wait_s
        self.upstreams: dict[str, Upstream] = {}
        self._on_tools_changed = on_tools_changed
        self._on_status = on_status
        self.secrets = secrets

    async def on_tools_changed(self, up: Upstream) -> None:
        await self._on_tools_changed(up)

    def notify(self, up: Upstream) -> None:
        self._on_status(up)

    async def apply(self, servers: dict[str, dict[str, Any]]) -> list[str]:
        """Start new servers, stop removed ones, restart changed ones. Returns removed names."""
        removed = [n for n in self.upstreams if n not in servers]
        for name in removed:
            await self.upstreams.pop(name).stop()
        for name, cfg in servers.items():
            cur = self.upstreams.get(name)
            if cur and cur.config == cfg:
                continue
            if cur:
                await cur.stop()
            up = Upstream(name, cfg, self)
            self.upstreams[name] = up
            up.start()
            self.notify(up)
        return removed

    async def restart(self, name: str) -> None:
        up = self.upstreams[name]
        await up.stop()
        up.status, up.error = "connecting", None
        up.start()

    async def stop_all(self) -> None:
        await asyncio.gather(*(u.stop() for u in self.upstreams.values()), return_exceptions=True)

    def get(self, name: str) -> Upstream | None:
        return self.upstreams.get(name)
