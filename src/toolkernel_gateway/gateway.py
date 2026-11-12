"""The gateway core: sessions, meta-tools, the tool bridge, approvals and events."""

from __future__ import annotations

import asyncio
import base64
import difflib
import json
import logging
import mimetypes
import re
import secrets
import shutil
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import mcp_types as types

from .config import Config, read_mcp_json, write_mcp_json
from .generator import Generator, ServerInput, first_sentence
from .guard import OutputGuard, human_bytes
from .policy import PolicyEngine
from .runtime import Runtime, RuntimeError_, RuntimeManager
from .search import ToolSearch, format_results
from .static_check import check as static_check
from .store import Store
from .upstream import ToolUnavailable, Upstream, UpstreamManager, UpstreamToolError

log = logging.getLogger("toolkernel")


class BridgeError(Exception):
    def __init__(self, kind: str, message: str, **extra: Any):
        super().__init__(message)
        self.kind, self.message, self.extra = kind, message, extra


@dataclass
class RunAccumulator:
    tool_calls: list[dict[str, Any]] = field(default_factory=list)

    @property
    def bytes_from_tools(self) -> int:
        return sum(c.get("bytes", 0) for c in self.tool_calls)


@dataclass
class Session:
    id: str
    client: str | None = None
    created: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    runtime: Runtime | None = None
    had_runtime: bool = False
    restart_reason: str | None = None
    last_state: str | None = None
    bridge_token: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    stash: dict[str, tuple[Any, float]] = field(default_factory=dict)
    current: RunAccumulator | None = None
    imported: dict[str, tuple[str, str]] = field(default_factory=dict)  # for the static check
    last_call: str = ""  # the previous meta-tool call, to notice an agent repeating itself
    repeats: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class EventBus:
    def __init__(self) -> None:
        self.subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def publish(self, kind: str, data: dict[str, Any] | None = None) -> None:
        event = {"kind": kind, "data": data or {}, "ts": time.time()}
        for q in list(self.subscribers):
            if q.full():
                continue
            q.put_nowait(event)

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=200)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self.subscribers.discard(q)


# ---------------------------------------------------------------- agent-facing text

INSTRUCTIONS = """You have a persistent Python environment (like a Jupyter notebook) with access to external tools.
- Discover tools: search_tools("query"), or in code: print(open("README.md").read()).
- Use tools in code:  from servers.<server> import <tool>   (keyword arguments only)
- The runtime's own disk is scratch space and holds none of your tools' data. Reach files, records
  and pages only through the tools (servers.<server>), even when the tool is about files.
- Variables, imports and functions PERSIST between execute_code calls. Reuse them instead of
  fetching again. Each result ends with a [state] line listing them.
- Keep large data in variables or saved/. Do NOT print raw large results; print counts,
  filtered rows, summaries, or preview(x).
- Output over ~{budget} chars is truncated; the full text is saved and readable with read_output(handle).
- The environment may be restarted (idle timeout or reset). You will be told when this happens.
  Files in saved/ survive restarts; save(x, "name") data that was expensive to collect, load("name") to get it back.
- Tool errors raise ToolError with the server's message. Helpers: preview, save, load, stash, parallel, search."""

STATELESS_INSTRUCTIONS = """You have a Python environment with access to external tools.
- Discover tools: search_tools("query"), or in code: print(open("README.md").read()).
- Use tools in code:  from servers.<server> import <tool>   (keyword arguments only)
- The runtime's own disk is scratch space and holds none of your tools' data. Reach files, records
  and pages only through the tools (servers.<server>), even when the tool is about files.
- Stateless mode: variables do NOT persist between execute_code calls. Do each task in one call,
  or keep data with save(x, "name") / load("name") or the stash dict.
- Do NOT print raw large results; print counts, filtered rows, summaries, or preview(x).
- Output over ~{budget} chars is truncated; the full text is saved and readable with read_output(handle).
- Tool errors raise ToolError with the server's message."""


class Gateway:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.data_dir = cfg.data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.events = EventBus()
        self.store = Store(self.data_dir / "toolkernel.db")
        self.generator = Generator(self.data_dir / "wrappers", cfg["generator"]["max_type_depth"])
        self.search_index = ToolSearch()
        self.search_index.build(self.generator.index)
        self.policy = PolicyEngine(cfg["policies"])
        out = cfg["output"]
        self.guard = OutputGuard(out["budget_chars"], out["head_ratio"])
        self.runtimes = RuntimeManager(cfg["runtime"], out, self.generator.root, self.data_dir / "sessions",
                                       cfg["server"]["bridge_port"])
        self.upstreams = UpstreamManager(self.data_dir / "logs", cfg["upstreams"]["upstream_wait_s"],
                                         self._on_tools_changed, self._on_status, self._secrets,
                                         default_cwd=cfg.mcp_json.parent)
        self.sessions: dict[str, Session] = {}
        self.by_token: dict[str, Session] = {}
        self.approvals: dict[str, dict[str, Any]] = {}
        self._approval_futures: dict[str, asyncio.Future[bool]] = {}
        self._tasks: list[asyncio.Task[Any]] = []
        self._mcp_mtime = 0.0
        self.started = time.time()

    # -- lifecycle -----------------------------------------------------------------

    async def start(self) -> None:
        await self.runtimes.cleanup_orphans()
        await self.reload_mcp_json(force=True)
        self._tasks.append(asyncio.create_task(self._reaper(), name="reaper"))
        if self.cfg["upstreams"]["watch"]:
            self._tasks.append(asyncio.create_task(self._watch_mcp_json(), name="watch-mcp-json"))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for s in self.sessions.values():
            if s.runtime:
                await self.runtimes.stop(s.runtime)
        await self.upstreams.stop_all()
        await self.runtimes.close()

    def _secrets(self) -> dict[str, str]:
        p = self.data_dir / "secrets.json"
        try:
            return json.loads(p.read_text()) if p.exists() else {}
        except json.JSONDecodeError:
            return {}

    async def reload_mcp_json(self, force: bool = False) -> None:
        path = self.cfg.mcp_json
        mtime = path.stat().st_mtime if path.exists() else 0.0
        if not force and mtime == self._mcp_mtime:
            return
        self._mcp_mtime = mtime
        try:
            servers = read_mcp_json(path)
        except (json.JSONDecodeError, OSError) as e:
            log.error("could not read %s: %s", path, e)
            self.events.publish("config_error", {"message": f"{path.name}: {e}"})
            return
        removed = await self.upstreams.apply(servers)
        for name in removed:
            self.generator.remove(name)
        if removed:
            self.search_index.build(self.generator.index)
        # Drop generated packages for servers no longer configured at all.
        for py, entry in list(self.generator.index["servers"].items()):
            if entry["name"] not in servers:
                self.generator.remove(entry["name"])
        self.events.publish("servers", {})

    async def _watch_mcp_json(self) -> None:
        while True:
            await asyncio.sleep(2)
            try:
                await self.reload_mcp_json()
            except Exception as e:  # noqa: BLE001
                log.warning("mcp.json reload failed: %s", e)

    async def _reaper(self) -> None:
        idle = self.cfg["runtime"]["idle_timeout_s"]
        while True:
            await asyncio.sleep(min(30, max(1, idle / 4)))
            now = time.time()
            for s in list(self.sessions.values()):
                rt = s.runtime
                if rt and not s.lock.locked() and now - rt.last_used > idle:
                    await self.runtimes.stop(rt)
                    s.runtime = None
                    s.restart_reason = f"idle timeout ({_duration(idle)} without use)"
                    self.events.publish("session", {"id": s.id})
            for s in self.sessions.values():
                s.stash = {k: v for k, v in s.stash.items() if v[1] > now}

    async def _on_tools_changed(self, up: Upstream) -> None:
        description = up.server_description
        if not description and up.instructions:
            description = first_sentence(up.instructions, 200)
        server = ServerInput(name=up.name, title=up.server_title, description=description,
                             instructions=up.instructions, tools=up.tools)
        py, changed = await asyncio.to_thread(self.generator.generate, server)
        self.search_index.build(self.generator.index)
        if changed:
            log.info("generated servers.%s (%d tools)", py, len(up.tools))
        self.events.publish("servers", {"name": up.name})

    def _on_status(self, up: Upstream) -> None:
        self.events.publish("servers", {"name": up.name, "status": up.status})

    # -- server admin ----------------------------------------------------------------

    def add_server(self, name: str, config: dict[str, Any]) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name):
            raise ValueError("Server names may use letters, digits, '-', '_' and '.'.")
        if not (config.get("command") or config.get("url")):
            raise ValueError("A server needs either a \"command\" (stdio) or a \"url\" (HTTP).")
        servers = read_mcp_json(self.cfg.mcp_json)
        servers[name] = config
        write_mcp_json(self.cfg.mcp_json, servers)

    def remove_server(self, name: str) -> None:
        servers = read_mcp_json(self.cfg.mcp_json)
        servers.pop(name, None)
        write_mcp_json(self.cfg.mcp_json, servers)

    def set_server_enabled(self, name: str, enabled: bool) -> None:
        servers = read_mcp_json(self.cfg.mcp_json)
        if name not in servers:
            raise KeyError(name)
        if enabled:
            servers[name].pop("disabled", None)
        else:
            servers[name]["disabled"] = True
        write_mcp_json(self.cfg.mcp_json, servers)

    # -- sessions --------------------------------------------------------------------

    def session(self, sid: str, client: str | None = None) -> Session:
        s = self.sessions.get(sid)
        if s is None:
            s = Session(id=sid, client=client)
            self.sessions[sid] = s
            self.by_token[s.bridge_token] = s
            self.events.publish("session", {"id": sid})
        s.last_seen = time.time()
        if client and not s.client:
            s.client = client
        self.store.touch_session(sid, client)
        return s

    async def force_restart(self, sid: str, reason: str) -> bool:
        """Stop a session's runtime as an idle reap would; the next call gets a restart notice."""
        s = self.sessions.get(sid)
        if s is None:
            return False
        async with s.lock:
            if s.runtime:
                await self.runtimes.stop(s.runtime)
                s.runtime = None
                s.restart_reason = reason
                self.events.publish("session", {"id": s.id})
        return True

    async def _ensure_runtime(self, s: Session) -> tuple[Runtime | None, str | None]:
        """Returns (runtime, restart notice). Starts a runtime if needed."""
        notice = None
        stateless = self.cfg["runtime"]["state"] == "stateless"
        if s.runtime and not s.runtime.alive():
            await self.runtimes.stop(s.runtime)
            s.runtime = None
            s.restart_reason = s.restart_reason or "the runtime process exited (crash or out of memory)"
        if stateless and s.runtime:
            await self.runtimes.stop(s.runtime)
            s.runtime = None
        if s.runtime is None:
            if s.had_runtime and s.restart_reason and not stateless:
                notice = self._restart_notice(s, s.restart_reason)
            s.runtime = await self.runtimes.start(s.id, s.bridge_token)
            s.had_runtime = True
            s.restart_reason = None
            self.events.publish("session", {"id": s.id})
        return s.runtime, notice

    def _restart_notice(self, s: Session, reason: str) -> str:
        files = self._data_files(s)
        keys = list(s.stash)
        kept = f"Files in saved/ are kept: {', '.join(files[:10]) + (f' +{len(files) - 10} more' if len(files) > 10 else '') if files else 'none'}"
        if keys:
            kept += f" · stash keys: {', '.join(keys[:10])}"
        return f"[runtime] Restarted since your last call (reason: {reason}). All variables were cleared.\n{kept}"

    def _data_files(self, s: Session) -> list[str]:
        d = self.runtimes.workdir(s.id) / "saved"
        return sorted(p.name for p in d.iterdir() if not p.name.startswith((".", "_"))) if d.exists() else []

    # -- meta-tools ------------------------------------------------------------------

    def instructions(self) -> str:
        tmpl = STATELESS_INSTRUCTIONS if self.cfg["runtime"]["state"] == "stateless" else INSTRUCTIONS
        return tmpl.format(budget=self.cfg["output"]["budget_chars"])

    def server_names_line(self) -> str:
        names = list(self.generator.index["servers"])
        cap = self.cfg["agent_interface"]["max_servers_in_description"]
        if not names:
            return "Connected servers: none yet (check list_servers())."
        shown = ", ".join(names[:cap])
        if len(names) > cap:
            shown += f", +{len(names) - cap} more, see list_servers()"
        return f"Connected servers: {shown}."

    def meta_tools(self) -> list[dict[str, Any]]:
        stateless = self.cfg["runtime"]["state"] == "stateless"
        persist = ("Variables do NOT persist between calls (stateless mode); use save()/load() or stash."
                   if stateless else "Variables, imports and functions persist between calls; reuse them instead of refetching.")
        budget = self.cfg["output"]["budget_chars"]
        tools = [
            {
                "name": "execute_code",
                "description": (
                    "Run Python in your persistent sandboxed runtime, which has every connected MCP server as an "
                    "importable package: `from servers.<server> import <tool>` then call it with keyword arguments. "
                    f"{persist} Keep large data in variables; print only counts, summaries or preview(x). "
                    f"Output over ~{budget} chars is truncated and saved (read_output). "
                    "Results end with a [state] line. Helpers: preview, save, load, stash, parallel, search. "
                    + self.server_names_line()
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string", "description": "Python source. The last expression's value is shown."},
                        "timeout_s": {"type": "number", "description": "Optional time limit in seconds."},
                    },
                    "required": ["code"],
                },
            },
            {
                "name": "search_tools",
                "description": "Find tools across connected servers by keywords. detail: names | summary (default) | full.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "server": {"type": "string", "description": "Only search this server."},
                        "detail": {"type": "string", "enum": ["names", "summary", "full"]},
                        "limit": {"type": "integer"},
                    },
                    "required": ["query"],
                },
                "annotations": {"readOnlyHint": True},
            },
            {
                "name": "describe_tool",
                "description": "Full Python signature, docstring and usage for one tool, e.g. describe_tool(\"github.list_issues\").",
                "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
                "annotations": {"readOnlyHint": True},
            },
            {
                "name": "list_servers",
                "description": "Connected servers with a one-line description and tool count.",
                "inputSchema": {"type": "object", "properties": {}},
                "annotations": {"readOnlyHint": True},
            },
            {
                "name": "read_output",
                "description": "Page through output that was truncated and saved. Use the handle from the truncation note.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"handle": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}},
                    "required": ["handle"],
                },
                "annotations": {"readOnlyHint": True},
            },
            {
                "name": "reset_runtime",
                "description": "Restart the Python runtime, clearing variables. Files in saved/ are kept unless wipe=true.",
                "inputSchema": {"type": "object", "properties": {"wipe": {"type": "boolean"}}},
            },
        ]
        if self.cfg["agent_interface"]["hybrid_call_tool"]:
            tools.insert(4, {
                "name": "call_tool",
                "description": "Make one direct tool call without writing code, e.g. call_tool(\"github.get_issue\", {...}). "
                               "Large results are kept in the runtime as `_last` and previewed. Prefer execute_code for multi-step work.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"name": {"type": "string", "description": "server.tool"}, "arguments": {"type": "object"}},
                    "required": ["name"],
                },
            })
        return tools

    async def dispatch(self, session_id: str, tool: str, args: dict[str, Any], client: str | None = None) -> tuple[str, bool]:
        """Run a meta-tool. Returns (text for the model, is_error)."""
        s = self.session(session_id, client)
        handlers = {
            "execute_code": lambda: self.execute_code(s, str(args.get("code", "")), args.get("timeout_s")),
            "search_tools": lambda: self.search_tools(s, **_pick(args, "query", "server", "detail", "limit")),
            "describe_tool": lambda: self.describe_tool(s, str(args.get("name", ""))),
            "list_servers": lambda: self.list_servers(s),
            "call_tool": lambda: self.call_tool(s, str(args.get("name", "")), args.get("arguments") or {}),
            "read_output": lambda: self.read_output(s, str(args.get("handle", "")), args.get("offset") or 0, args.get("limit")),
            "reset_runtime": lambda: self.reset_runtime(s, bool(args.get("wipe"))),
        }
        if tool == "call_tool" and not self.cfg["agent_interface"]["hybrid_call_tool"]:
            handlers.pop("call_tool")
        if tool not in handlers:
            return f"Unknown tool {tool!r}. Available: {', '.join(handlers)}.", True
        try:
            text, is_error = await handlers[tool]()
        except RuntimeError_ as e:
            return f"[error]\nThe runtime could not start: {e}", True
        key = tool + json.dumps(args, sort_keys=True, default=str)
        s.repeats = s.repeats + 1 if key == s.last_call else 0
        s.last_call = key
        if s.repeats >= 2 and tool != "reset_runtime":  # the same call three or more times in a row: say so
            text += (f"\n[note] This is the same {tool} call as your last {s.repeats}, and the answer has not changed. "
                     "Use what you have: write code with the tools listed above, or try something different.")
        return text, is_error

    async def _record(self, s: Session, tool: str, input_: dict[str, Any], output: str, *, code: str = "",
                      sandbox: str = "", state: str = "", duration_ms: int = 0, acc: RunAccumulator | None = None,
                      is_error: bool = False, restarted: bool = False, spill: str | None = None) -> None:
        acc = acc or RunAccumulator()
        run_id = self.store.add_run(
            session_id=s.id, ts=time.time(), tool=tool, input=input_, code=code, output=output,
            sandbox=sandbox[:1_000_000], state=state, duration_ms=duration_ms, tool_calls=acc.tool_calls,
            bytes_from_tools=acc.bytes_from_tools, bytes_returned=len(output.encode()), is_error=int(is_error),
            restarted=int(restarted), spill=spill,
        )
        self.events.publish("run", {"id": run_id, "session_id": s.id, "tool": tool})

    def _with_meta(self, body: str, ms: int, acc: RunAccumulator) -> str:
        """Append the [meta] line. 'returned' is the exact size of the final text, meta line included."""
        calls = acc.tool_calls
        parts = [f"exec {ms}ms"]
        if calls:
            counts = Counter(c["tool"] for c in calls)
            detail = ", ".join(f"{t} ×{n}" for t, n in counts.most_common(4))
            if len(counts) > 4:
                detail += ", …"
            parts.append(f"tool calls {len(calls)} ({detail})")
            parts.append(f"data via tools {human_bytes(acc.bytes_from_tools)}")
        else:
            parts.append("tool calls 0")
        base = body + "\n[meta] " + " · ".join(parts) + " · returned "
        size = len(base.encode())
        for _ in range(3):  # the label's own length can change the total; settle it
            text = base + human_bytes(size)
            if len(text.encode()) == size:
                break
            size = len(text.encode())
        return text

    async def execute_code(self, s: Session, code: str, timeout_s: Any = None) -> tuple[str, bool]:
        t0 = time.perf_counter()
        limit = self.cfg["runtime"]["exec_timeout_s"]
        timeout = min(float(timeout_s), limit) if isinstance(timeout_s, (int, float)) and timeout_s > 0 else limit
        acc = RunAccumulator()
        stateless = self.cfg["runtime"]["state"] == "stateless"
        spill_dir = self.runtimes.workdir(s.id) / ".outputs"
        notice = None
        if s.runtime is None or stateless:
            s.imported.clear()  # a fresh runtime has no earlier imports
        problem = static_check(code, self.generator.index, s.imported) if self.cfg["runtime"]["static_check"] else None
        if problem:
            raw = {"error": problem}
            state = s.last_state
        else:
            async with s.lock:
                rt, notice = await self._ensure_runtime(s)
                s.current = acc
                try:
                    raw = await self.runtimes.exec(rt, code, timeout)
                finally:
                    s.current = None
                if raw.get("crashed"):
                    s.runtime = None
                    s.restart_reason = "execution timed out" if raw.get("timed_out") else "the runtime crashed (possibly out of memory)"
            state = raw.get("state") or s.last_state
            if stateless:
                state = "[state] stateless mode: variables do not persist; use save()/load() or stash"
            elif raw.get("crashed"):
                state = "[state] (lost: the runtime will restart on your next call)"
            s.last_state = state
        stdout = raw.get("stdout") or ""
        if raw.get("stdout_dropped"):
            stdout += f"\n… [{raw['stdout_dropped']:,} more chars of output were discarded by the runtime]"
        sections = [("stdout", stdout), ("result", raw.get("result") or ""), ("error", raw.get("error") or "")]
        if not any(b for _, b in sections):
            sections = [("result", "(ran with no output)")]
        guarded = self.guard.render(sections, spill_dir, notice=notice,
                                    state=state if self.cfg["output"]["state_line"]["enabled"] else None)
        ms = raw.get("duration_ms") or int((time.perf_counter() - t0) * 1000)
        text = self._with_meta(guarded.text, ms, acc)
        sandbox = "\n".join(f"[{k}]\n{v}" for k, v in sections if v)
        await self._record(s, "execute_code", {"timeout_s": timeout_s} if timeout_s else {}, text, code=code,
                           sandbox=sandbox, state=state or "", duration_ms=int((time.perf_counter() - t0) * 1000),
                           acc=acc, is_error=bool(raw.get("error")), restarted=bool(notice), spill=guarded.handle)
        return text, bool(raw.get("error")) and not raw.get("stdout") and not raw.get("result")

    async def call_tool(self, s: Session, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        found = self.generator.tool_entry(name)
        if not found:
            text = self._unknown_tool(name)
            await self._record(s, "call_tool", {"name": name, "arguments": arguments}, text, is_error=True)
            return text, True
        spy, tpy, meta = found
        server = self.generator.index["servers"][spy]["name"]
        payload = json.dumps(json.dumps(arguments))
        code = (
            "import json as __tk_json\n"
            "from toolkernel._bridge import call_tool as __tk_call\n"
            f"_last = __tk_call({server!r}, {meta['name']!r}, __tk_json.loads({payload}), _caller='direct')\n"
            "_last"
        )
        t0 = time.perf_counter()
        acc = RunAccumulator()
        spill_dir = self.runtimes.workdir(s.id) / ".outputs"
        async with s.lock:
            rt, notice = await self._ensure_runtime(s)
            s.current = acc
            try:
                raw = await self.runtimes.exec(rt, code, self.cfg["runtime"]["exec_timeout_s"])
            finally:
                s.current = None
            if raw.get("crashed"):
                s.runtime = None
                s.restart_reason = "the runtime crashed"
        result = (raw.get("result") or "").replace("`_`", "`_last`").replace("`data = _`", "`data = _last`")
        error = raw.get("error") or ""
        if error:
            error = re.sub(r"^Traceback \(most recent call last\):\n(  .*\n)*", "", error)
        sections = [("result", result), ("error", error)]
        if not any(b for _, b in sections):
            sections = [("result", "None")]
        guarded = self.guard.render(sections, spill_dir, notice=notice)
        ms = int((time.perf_counter() - t0) * 1000)
        text = self._with_meta(guarded.text, ms, acc)
        await self._record(s, "call_tool", {"name": name, "arguments": arguments}, text, code=code,
                           sandbox="\n".join(f"[{k}]\n{v}" for k, v in sections if v), duration_ms=ms, acc=acc,
                           is_error=bool(error), restarted=bool(notice), spill=guarded.handle)
        return text, bool(error)

    def _unknown_tool(self, name: str) -> str:
        names = [f"{spy}.{tpy}" for spy, e in self.generator.index["servers"].items() for tpy in e["tools"]]
        close = difflib.get_close_matches(name, names, n=5, cutoff=0.4)
        hint = f" Did you mean: {', '.join(close)}?" if close else " Try search_tools(\"…\")."
        return f"No tool named {name!r} (use \"server.tool\").{hint}"

    async def search_tools(self, s: Session, query: str = "", server: str | None = None, detail: str | None = None,
                           limit: int | None = None) -> tuple[str, bool]:
        detail = detail if detail in ("names", "summary", "full") else "summary"
        limit = int(limit or (5 if detail == "full" else 10))
        if server and server not in self.generator.index["servers"]:
            match = [p for p, e in self.generator.index["servers"].items() if e["name"] == server]
            server = match[0] if match else server
        results = self.search_index.search(query, server, limit)
        sources = None
        if detail == "full":
            sources = {(sp, tp): self._describe_source(sp, tp) for sp, tp, _ in results}
        text = format_results(results, self.generator.index, detail, sources)
        guarded = self.guard.render([("result", text)], self.runtimes.workdir(s.id) / ".outputs")
        out = guarded.text.removeprefix("[result]\n")
        await self._record(s, "search_tools", {"query": query, "server": server, "detail": detail, "limit": limit}, out)
        return out, False

    def _describe_source(self, spy: str, tpy: str) -> str:
        src = self.generator.tool_source(spy, tpy)
        idx = src.find("def ")
        body = src[idx:] if idx >= 0 else src
        body = re.sub(r"\n    return call_tool\(.*\)\n?$", "", body.rstrip()) + "\n    ..."
        return f"# servers.{spy}.{tpy}\n{body}"

    async def describe_tool(self, s: Session, name: str) -> tuple[str, bool]:
        found = self.generator.tool_entry(name)
        if not found:
            text = self._unknown_tool(name)
            await self._record(s, "describe_tool", {"name": name}, text, is_error=True)
            return text, True
        spy, tpy, meta = found
        types_ = self.generator.index["servers"][spy].get("types", {})
        parts = [self._describe_source(spy, tpy)]
        used = [t for t in meta.get("used_types", []) if t in types_]
        if used:
            parts.append("# types (servers.%s._types)\n" % spy + "\n\n".join(types_[t] for t in used))
        req = [p for p, m in meta["params"].items() if m["required"]]
        example = ", ".join(f"{p}=..." for p in req)
        parts.append(f"# usage\nfrom servers.{spy} import {tpy}\nresult = {tpy}({example})")
        text = "\n\n".join(parts)
        guarded = self.guard.render([("result", text)], self.runtimes.workdir(s.id) / ".outputs")
        out = guarded.text.removeprefix("[result]\n")
        await self._record(s, "describe_tool", {"name": name}, out)
        return out, False

    async def list_servers(self, s: Session) -> tuple[str, bool]:
        lines = []
        index = self.generator.index["servers"]
        for up in self.upstreams.upstreams.values():
            py = next((p for p, e in index.items() if e["name"] == up.name), None)
            desc = index[py]["description"] if py else (up.server_description or "")
            label = py or up.name
            status = up.status if up.status != "ready" else f"{len(up.tools)} tools"
            line = f"{label} ({status}): {first_sentence(desc, 120) or '(no description)'}"
            if up.status == "error" and up.error:
                line += f" [error: {up.error[:120]}]"
            lines.append(line)
        text = "\n".join(lines) if lines else "No servers are configured."
        text += "\nMore: search_tools(\"…\", server=\"<name>\") or open(\"servers/<name>/README.md\")."
        await self._record(s, "list_servers", {}, text)
        return text, False

    async def read_output(self, s: Session, handle: str, offset: int = 0, limit: int | None = None) -> tuple[str, bool]:
        text = self.guard.page(self.runtimes.workdir(s.id) / ".outputs", handle, int(offset or 0),
                               int(limit) if limit else None)
        await self._record(s, "read_output", {"handle": handle, "offset": offset, "limit": limit}, text)
        return text, text.startswith("No saved output")

    async def reset_runtime(self, s: Session, wipe: bool = False) -> tuple[str, bool]:
        async with s.lock:
            if s.runtime:
                await self.runtimes.stop(s.runtime)
                s.runtime = None
            s.restart_reason = None
            s.last_state = None
            wd = self.runtimes.workdir(s.id)
            if wipe:
                for p in (wd / "saved").iterdir():
                    shutil.rmtree(p) if p.is_dir() else p.unlink()
                s.stash.clear()
        files = self._data_files(s)
        text = "[runtime] Reset. All variables were cleared."
        text += " saved/ and the stash were wiped." if wipe else (
            f" Files in saved/ are kept: {', '.join(files[:10]) or 'none'}" +
            (f" · stash keys: {', '.join(list(s.stash)[:10])}" if s.stash else ""))
        await self._record(s, "reset_runtime", {"wipe": wipe}, text)
        self.events.publish("session", {"id": s.id})
        return text, False

    # -- bridge ------------------------------------------------------------------------

    def session_for_token(self, token: str) -> Session | None:
        return self.by_token.get(token)

    async def bridge_call(self, s: Session, server: str, tool: str, arguments: dict[str, Any], caller: str) -> Any:
        up = self.upstreams.get(server)
        if up is None:
            raise BridgeError("ToolUnavailable", f"server {server!r} is not configured")
        entry = None
        spy = next((p for p, e in self.generator.index["servers"].items() if e["name"] == server), None)
        if spy:
            entry = next(((tpy, m) for tpy, m in self.generator.index["servers"][spy]["tools"].items()
                          if m["name"] == tool), None)
        annotations = entry[1]["annotations"] if entry else {}
        decision = self.policy.evaluate(server, tool, annotations, caller=caller, session_id=s.id,
                                        aliases=(spy, entry[0]) if spy and entry else None)
        record: dict[str, Any] = {"tool": f"{spy or server}.{entry[0] if entry else tool}",
                                  "args": _clip_json(arguments, 600), "caller": caller, "ts": time.time()}
        acc = s.current
        if acc is not None:
            acc.tool_calls.append(record)
        if decision.action == "deny":
            record.update(ok=False, error=f"denied: {decision.reason}", bytes=0, ms=0)
            raise BridgeError("ToolDenied", f"{server}.{tool} was blocked by policy ({decision.reason})")
        if decision.action == "approve":
            ok, why = await self._await_approval(s, server, tool, arguments, decision.reason, annotations)
            record["approval"] = "approved" if ok else why
            if not ok:
                record.update(ok=False, error=why, bytes=0, ms=0)
                raise BridgeError("ToolDenied", f"{server}.{tool} was not approved ({why})")
        t0 = time.perf_counter()
        try:
            res = await up.call(tool, arguments, self.cfg["upstreams"]["call_timeout_s"])
        except ToolUnavailable as e:
            record.update(ok=False, error=str(e), bytes=0, ms=int((time.perf_counter() - t0) * 1000))
            raise BridgeError("ToolUnavailable", str(e)) from None
        except UpstreamToolError as e:
            record.update(ok=False, error=str(e), bytes=0, ms=int((time.perf_counter() - t0) * 1000))
            raise BridgeError("ToolError", str(e), server=server, tool=tool) from None
        record.update(ms=int((time.perf_counter() - t0) * 1000), bytes=_payload_bytes(res), ok=not res.is_error,
                      preview=_raw_preview(res, 3000))
        if res.is_error:
            msg = " ".join(c.text for c in res.content if isinstance(c, types.TextContent)) or "tool reported an error"
            record["error"] = msg[:500]
            raise BridgeError("ToolError", msg, server=server, tool=tool)
        meta = entry[1] if entry else {}
        value, warning = self._normalize(s, res, meta, server, tool)
        if warning:
            record["warning"] = warning
        return value

    def _normalize(self, s: Session, res: types.CallToolResult, meta: dict[str, Any], server: str, tool: str
                   ) -> tuple[Any, str | None]:
        warning = None
        if res.structured_content is not None:
            value: Any = res.structured_content
            if meta.get("unwrap_result") and isinstance(value, dict) and "result" in value:
                value = value["result"]
            warning = self._validate(server, tool, res.structured_content)
            return value, warning
        items: list[Any] = []
        texts: list[str] = []
        for c in res.content:
            if isinstance(c, types.TextContent):
                texts.append(c.text)
                items.append(c.text)
            elif isinstance(c, (types.ImageContent, types.AudioContent)):
                items.append(self._save_media(s, c.data, c.mime_type))
            elif isinstance(c, types.EmbeddedResource):
                r = c.resource
                if isinstance(r, types.TextResourceContents):
                    items.append({"__tk__": "resource", "uri": str(r.uri), "mime_type": r.mime_type, "text": r.text})
                else:
                    ref = self._save_media(s, r.blob, r.mime_type or "application/octet-stream")
                    items.append({"__tk__": "resource", "uri": str(r.uri), "mime_type": r.mime_type, "path": ref["path"]})
            else:
                d = c.model_dump(mode="json", by_alias=True, exclude_none=True)
                if d.get("type") == "resource_link":
                    items.append({"__tk__": "resource", "uri": d.get("uri"), "mime_type": d.get("mimeType")})
                else:
                    items.append(d)
        if not items:
            return None, None
        if len(items) == 1 and len(texts) == 1:
            return _maybe_json(texts[0]), None
        if len(texts) == len(items):
            parsed = [_maybe_json(t) for t in texts]
            if all(not isinstance(p, str) for p in parsed):
                return parsed, None
            return texts, None
        return items if len(items) > 1 else items[0], None

    def _validate(self, server: str, tool: str, value: Any) -> str | None:
        mode = self.cfg["generator"]["validate_output"]
        if mode == "off":
            return None
        up = self.upstreams.get(server)
        schema = next((t.get("outputSchema") for t in (up.tools if up else []) if t["name"] == tool), None)
        if not schema:
            return None
        try:
            import jsonschema

            jsonschema.validate(value, schema)
        except ImportError:
            return None
        except jsonschema.ValidationError as e:
            msg = f"output did not match the declared schema: {e.message[:200]}"
            if mode == "strict":
                raise BridgeError("ToolError", msg, server=server, tool=tool) from None
            log.warning("%s.%s %s", server, tool, msg)
            return msg
        return None

    def _save_media(self, s: Session, data_b64: str, mime: str) -> dict[str, Any]:
        d = self.runtimes.workdir(s.id) / "saved" / "_media"
        d.mkdir(parents=True, exist_ok=True)
        ext = mimetypes.guess_extension(mime or "") or ".bin"
        name = f"{int(time.time())}-{secrets.token_hex(3)}{ext}"
        (d / name).write_bytes(base64.b64decode(data_b64))
        return {"__tk__": "media", "path": f"saved/_media/{name}", "mime_type": mime}

    async def _await_approval(self, s: Session, server: str, tool: str, arguments: dict[str, Any], reason: str,
                              annotations: dict[str, Any]) -> tuple[bool, str]:
        aid = secrets.token_hex(4)
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self.approvals[aid] = {
            "id": aid, "session_id": s.id, "client": s.client, "server": server, "tool": tool,
            "arguments": arguments, "reason": reason, "annotations": annotations,
            "created": time.time(), "status": "pending",
        }
        self._approval_futures[aid] = fut
        self.events.publish("approval", {"id": aid})
        timeout = self.cfg["policies"].get("approval_timeout_s", 300)
        try:
            ok = await asyncio.wait_for(fut, timeout=timeout)
            return ok, "approved" if ok else "declined by a person"
        except asyncio.TimeoutError:
            self.approvals[aid]["status"] = "expired"
            return False, f"no one responded within {_duration(timeout)}"
        finally:
            self._approval_futures.pop(aid, None)
            self.events.publish("approval", {"id": aid})

    def decide(self, aid: str, approve: bool) -> bool:
        fut = self._approval_futures.get(aid)
        if not fut or fut.done():
            return False
        self.approvals[aid]["status"] = "approved" if approve else "declined"
        self.approvals[aid]["decided"] = time.time()
        fut.set_result(approve)
        return True

    def stash_op(self, s: Session, op: str, key: str | None, value: Any) -> dict[str, Any]:
        now = time.time()
        s.stash = {k: v for k, v in s.stash.items() if v[1] > now}
        if op == "keys":
            return {"keys": list(s.stash)}
        if not key:
            raise BridgeError("ToolError", "stash key is required", server="stash", tool=op)
        if op == "get":
            hit = s.stash.get(key)
            return {"found": hit is not None, "value": hit[0] if hit else None}
        if op == "del":
            return {"found": s.stash.pop(key, None) is not None}
        if op == "set":
            size = sum(len(json.dumps(v[0])) for k, v in s.stash.items() if k != key) + len(json.dumps(value))
            if size > self.cfg["stash"]["max_bytes"]:
                raise BridgeError("ToolDenied", f"stash is full (limit {human_bytes(self.cfg['stash']['max_bytes'])}); "
                                                "use save() for large data")
            s.stash[key] = (value, now + self.cfg["stash"]["ttl_s"])
            return {"ok": True}
        raise BridgeError("ToolError", f"unknown stash op {op!r}", server="stash", tool=op)

    def bridge_search(self, query: str, server: str | None, detail: str, limit: int) -> str:
        results = self.search_index.search(query, server, limit)
        sources = {(sp, tp): self._describe_source(sp, tp) for sp, tp, _ in results} if detail == "full" else None
        return format_results(results, self.generator.index, detail if detail in ("names", "summary", "full") else "summary", sources)

    # -- views for the UI ------------------------------------------------------------------

    def session_view(self, s: Session) -> dict[str, Any]:
        rt = s.runtime
        return {
            "id": s.id, "client": s.client, "created": s.created, "last_seen": s.last_seen,
            "runtime": {"driver": rt.driver, "started": rt.started, "last_used": rt.last_used} if rt else None,
            "pending_restart": s.restart_reason, "state": s.last_state, "stash_keys": list(s.stash),
            "files": self._data_files(s),
        }


def _pick(d: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {k: d[k] for k in keys if k in d and d[k] is not None}


def _maybe_json(text: str) -> Any:
    t = text.strip()
    if t[:1] in "{[" or t in ("true", "false", "null") or re.fullmatch(r"-?\d+(\.\d+)?", t or "x"):
        try:
            return json.loads(t)
        except json.JSONDecodeError:
            return text
    return text


def _payload_bytes(res: types.CallToolResult) -> int:
    n = 0
    for c in res.content:
        if isinstance(c, types.TextContent):
            n += len(c.text.encode())
        elif isinstance(c, (types.ImageContent, types.AudioContent)):
            n += len(c.data) * 3 // 4
        else:
            n += len(json.dumps(c.model_dump(mode="json")).encode())
    if res.structured_content is not None:
        n = max(n, len(json.dumps(res.structured_content).encode()))
    return n


def _raw_preview(res: types.CallToolResult, limit: int) -> str:
    if res.structured_content is not None:
        text = json.dumps(res.structured_content, indent=1)
    else:
        parts = []
        for c in res.content:
            if isinstance(c, types.TextContent):
                parts.append(c.text)
            else:
                parts.append(f"<{c.type} content>")
        text = "\n".join(parts)
    return text if len(text) <= limit else text[:limit] + f"\n… ({len(text) - limit:,} more chars)"


def _clip_json(value: Any, limit: int) -> Any:
    text = json.dumps(value, default=str)
    if len(text) <= limit:
        return value
    return text[:limit] + "…"


def _duration(seconds: float) -> str:
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{int(seconds // 3600)} h"
    if seconds >= 60:
        return f"{int(seconds // 60)} min"
    return f"{int(seconds)} s"
