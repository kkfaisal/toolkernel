"""Benchmark harness: run every task under every configuration with a real agent loop, and measure.

Configurations:
  direct      every upstream MCP tool given to the model directly; results capped like Claude Code does
  toolkernel  ToolKernel, stateful runtime (the default)
  stateless   ToolKernel with a fresh runtime per execute_code (typical of other code-mode tools)
  guard_off   ToolKernel stateful with the output guard effectively disabled
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
import secrets
import socket
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .tasks import Task, final_answer

# Estimated $ per million tokens: (input, output, cache read). Cache writes are billed at 1.25× input.
# Claude rows checked against platform.claude.com/docs/en/about-claude/pricing on 2026-11-11.
PRICES = {
    "claude-fable-5-1": (10.0, 50.0, 0.25),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0, 0.10),
    "claude-sonnet-5": (2.0, 10.0, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    # From the providers' pricing pages, 2026-11-11 (DeepSeek: peak-hour rates, the higher of its two).
    "gpt-5-mini": (0.25, 2.00, 0.025),
    "gpt-5.4-mini": (0.75, 4.50, 0.075),
    "gpt-5.4-nano": (0.20, 1.25, 0.02),
    "deepseek-flash": (0.30, 1.20, 0.006),
    "deepseek-v4-pro": (1.32, 3.96, 0.044),
}
# Models whose rates rise for long prompts: (threshold in input tokens per request, multiplier on every rate).
LONG_PROMPT = {"claude-haiku-5-5": (100_000, 5.0)}
# Which API serves a model, by name prefix: (provider, OpenAI-compatible base URL, key variable).
PROVIDERS = [("claude-", ("anthropic", None, "ANTHROPIC_API_KEY")),
             ("deepseek-", ("deepseek", "https://api.deepseek.com", "DEEPSEEK_API_KEY"))]


def provider(model: str) -> tuple[str, str | None, str]:
    return next((p for prefix, p in PROVIDERS if model.startswith(prefix)), ("openai", None, "OPENAI_API_KEY"))
# Models that accept the server-side refusal fallback (`fallbacks: "default"`).
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}

SYSTEM = """You are a careful data analyst working for a company. You have tools to read the company's data \
files and to use the web. Use them to answer the user's questions accurately; never guess numbers.

When you have the answer to the current question, reply with a short explanation and end with one line:
FINAL ANSWER: <answer>"""


@dataclass
class Config:
    name: str
    kind: str  # direct | toolkernel
    label: str
    overrides: dict[str, Any] = field(default_factory=dict)


CONFIGS: dict[str, Config] = {
    "direct": Config("direct", "direct", "Direct MCP (all tools, results capped at ~25k tokens)"),
    "toolkernel": Config("toolkernel", "toolkernel", "ToolKernel (stateful)"),
    "stateless": Config("stateless", "toolkernel", "ToolKernel (stateless)", {"runtime": {"state": "stateless"}}),
    "guard_off": Config("guard_off", "toolkernel", "ToolKernel (stateful, output guard off)", {
        "output": {"budget_chars": 2_000_000,
                   "preview_threshold": {"list": 10**9, "dict": 10**9, "str": 10**9, "dataframe_rows": 10**9}}}),
}


@dataclass
class Result:
    run_id: str
    config: str
    model: str
    task: str
    category: str
    repeat: int
    success: bool = False
    answer: str = ""
    expected: str = ""
    error: str | None = None
    input_tokens: int = 0  # all input, cached or not
    uncached_input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    peak_context_tokens: int = 0
    api_calls: int = 0
    tool_calls: int = 0
    repeated_fetches: int = 0
    bytes_from_tools: int = 0
    chars_to_model: int = 0
    wall_s: float = 0.0
    cost_usd: float = 0.0
    fallback_used: bool = False
    simulated: bool = False
    excluded: bool = False  # the provider kept refusing for load (rate limits); says nothing about the configuration


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _servers(data_dir: Path, web: bool) -> dict[str, dict[str, Any]]:
    servers = {"files": {"command": sys.executable,
                         "args": ["-m", "toolkernel_gateway.mcp_servers.files", "--root", str(data_dir)]}}
    if web:
        env = {k: f"${{env:{k}}}" for k in ("TAVILY_API_KEY", "BRAVE_API_KEY", "SSL_CERT_FILE") if os.environ.get(k)}
        servers["web"] = {"command": sys.executable, "args": ["-m", "toolkernel_gateway.mcp_servers.web"], "env": env}
    return servers


def _text_of(result: Any) -> str:
    parts = []
    for c in result.content:
        parts.append(getattr(c, "text", None) or f"<{c.type} content>")
    return "\n".join(parts)


def _repeats(calls: list[tuple[str, str]]) -> int:
    seen: set[tuple[str, str]] = set()
    n = 0
    for c in calls:
        if c in seen:
            n += 1
        seen.add(c)
    return n


# ---------------------------------------------------------------- tool backends


class DirectBackend:
    """The agent talks to every upstream server itself, as an MCP client with many servers would."""

    def __init__(self, data_dir: Path, web: bool, cap_chars: int):
        self.data_dir, self.web, self.cap = data_dir, web, cap_chars
        self.stack = contextlib.AsyncExitStack()
        self.sessions: dict[str, Any] = {}
        self.tool_defs: list[dict[str, Any]] = []
        self.instructions = ""

    async def start(self) -> None:
        from mcp.client.session import ClientSession
        from mcp.client.stdio import StdioServerParameters, get_default_environment, stdio_client

        from ..config import resolve_secrets

        notes = []
        for name, cfg in _servers(self.data_dir, self.web).items():
            cfg = resolve_secrets(cfg, {})
            params = StdioServerParameters(command=cfg["command"], args=cfg["args"],
                                           env={**get_default_environment(), **cfg.get("env", {})})
            r, w = await self.stack.enter_async_context(stdio_client(params, errlog=open(os.devnull, "w")))
            s = await self.stack.enter_async_context(ClientSession(r, w))
            init = await s.initialize()
            if init.instructions:
                notes.append(f"{name}: {init.instructions}")
            self.sessions[name] = s
            for t in (await s.list_tools()).tools:
                self.tool_defs.append({"name": f"{name}__{t.name}", "description": t.description or "",
                                       "input_schema": t.input_schema})
        self.instructions = "\n".join(notes)

    async def stop(self) -> None:
        await self.stack.aclose()

    async def open_task(self) -> DirectTask:
        return DirectTask(self)


class DirectTask:
    def __init__(self, b: DirectBackend):
        self.b = b
        self.calls: list[tuple[str, str]] = []
        self.bytes_from_tools = 0

    async def call(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        server, _, tool = name.partition("__")
        s = self.b.sessions.get(server)
        if s is None:
            return f"Unknown tool {name}", True
        self.calls.append((name, json.dumps(args, sort_keys=True)))
        try:
            res = await s.call_tool(tool, args)
        except Exception as e:  # noqa: BLE001
            return f"Error: {e}", True
        text = _text_of(res)
        self.bytes_from_tools += len(text.encode())
        if len(text) > self.b.cap:
            more = len(text) - self.b.cap
            text = text[: self.b.cap] + (f"\n\n[Output truncated by the client: {more:,} more characters were not shown. "
                                         "Use filters, fewer columns or paging to get less data.]")
        return text, bool(res.is_error)

    async def restart(self) -> None:  # nothing to restart: there is no runtime
        pass

    def repeated(self) -> int:
        return _repeats(self.calls)

    async def close(self) -> None:
        pass


class GatewayBackend:
    """A ToolKernel gateway in this process; each task gets its own MCP connection (and runtime)."""

    def __init__(self, data_dir: Path, web: bool, overrides: dict[str, Any], work: Path, driver: str):
        self.data_dir, self.web, self.overrides, self.work, self.driver = data_dir, web, overrides, work, driver
        self.instructions = ""
        self.tool_defs: list[dict[str, Any]] = []

    async def start(self) -> None:
        from ..app import serve
        from ..config import _merge, load_config
        from ..gateway import Gateway

        self.work.mkdir(parents=True, exist_ok=True)
        mcp_json = self.work / "mcp.json"
        mcp_json.write_text(json.dumps({"mcpServers": _servers(self.data_dir, self.web)}))
        port = _free_port()
        base = {"server": {"port": port, "bridge_port": _free_port()}, "data_dir": str(self.work / "gw"),
                "upstreams": {"config_file": str(mcp_json), "watch": False},
                "runtime": {"driver": self.driver}}
        self.gw = Gateway(load_config(None, _merge(base, self.overrides)))
        self.task = asyncio.create_task(serve(self.gw))
        self.url = f"http://127.0.0.1:{port}/mcp"
        want = 2 if self.web else 1
        for _ in range(300):
            ups = self.gw.upstreams.upstreams.values() if hasattr(self.gw, "upstreams") else []
            if len(ups) == want and all(u.status == "ready" for u in ups):
                break
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError("gateway upstreams did not become ready: " +
                               ", ".join(f"{u.name}={u.status} {u.error or ''}" for u in self.gw.upstreams.upstreams.values()))
        probe = await self.open_task()
        self.instructions, self.tool_defs = probe.instructions, probe.tool_defs
        await probe.close()

    async def stop(self) -> None:
        self.gw.request_shutdown()  # graceful: lets uvicorn and the MCP session manager wind down
        try:
            await asyncio.wait_for(self.task, 15)
        except BaseException:  # noqa: BLE001
            self.task.cancel()
            with contextlib.suppress(BaseException):
                await self.task

    async def open_task(self) -> GatewayTask:
        t = GatewayTask(self)
        await t.connect()
        return t


class GatewayTask:
    def __init__(self, b: GatewayBackend):
        self.b = b
        self.stack = contextlib.AsyncExitStack()
        self.sid: str | None = None

    async def connect(self) -> None:
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        from mcp.shared._httpx_utils import create_mcp_http_client

        async def remember_session(response: Any) -> None:  # this connection's own gateway session
            self.sid = self.sid or response.headers.get("mcp-session-id")

        http = await self.stack.enter_async_context(create_mcp_http_client())
        http.event_hooks["response"].append(remember_session)
        streams = await self.stack.enter_async_context(streamable_http_client(self.b.url, http_client=http))
        self.s = await self.stack.enter_async_context(ClientSession(streams[0], streams[1]))
        init = await self.s.initialize()
        self.instructions = init.instructions or ""
        self.tool_defs = [{"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
                          for t in (await self.s.list_tools()).tools]

    async def call(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        res = await self.s.call_tool(name, args)
        return _text_of(res), bool(res.is_error)

    async def restart(self) -> None:
        if self.sid:
            await self.b.gw.force_restart(self.sid, "restarted by the benchmark to test recovery")

    def _runs(self) -> list[dict[str, Any]]:
        return self.b.gw.store.runs(self.sid, limit=10_000) if self.sid else []

    def repeated(self) -> int:
        calls = [(c["tool"], json.dumps(c.get("args"), sort_keys=True)) for r in reversed(self._runs())
                 for c in r["tool_calls"]]
        return _repeats(calls)

    @property
    def bytes_from_tools(self) -> int:
        return sum(r["bytes_from_tools"] or 0 for r in self._runs())

    async def close(self) -> None:
        with contextlib.suppress(BaseException):
            await self.stack.aclose()


# ---------------------------------------------------------------- models


@dataclass
class Step:
    content: list[Any]  # what to append to the conversation (SDK blocks or dicts)
    dumped: list[dict[str, Any]]  # JSON-safe copy for transcripts
    stop_reason: str
    usage: dict[str, int]
    fallback_used: bool = False


def _trust_os_certificates() -> None:
    """Use the OS certificate store, so HTTPS works behind corporate TLS inspection."""
    if getattr(_trust_os_certificates, "done", False):
        return
    with contextlib.suppress(ImportError):
        import truststore

        truststore.inject_into_ssl()
    _trust_os_certificates.done = True  # type: ignore[attr-defined]


class AnthropicLLM:
    def __init__(self, model: str, effort: str):
        import anthropic

        _trust_os_certificates()
        self.anthropic = anthropic
        self.client = anthropic.AsyncAnthropic(max_retries=4)
        self.model, self.effort = model, effort

    async def step(self, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]) -> Step:
        kwargs: dict[str, Any] = dict(
            model=self.model, max_tokens=16000, system=system, tools=tools, messages=messages,
            thinking={"type": "adaptive"}, output_config={"effort": self.effort},
            cache_control={"type": "ephemeral"},
        )
        if self.model in FALLBACK_MODELS:
            resp = await self.client.beta.messages.create(betas=["server-side-fallback-2026-07-01"],
                                                          fallbacks="default", **kwargs)
        else:
            resp = await self.client.messages.create(**kwargs)
        u = resp.usage
        usage = {"input": u.input_tokens or 0, "cache_read": u.cache_read_input_tokens or 0,
                 "cache_write": u.cache_creation_input_tokens or 0, "output": u.output_tokens or 0}
        fallback = any(getattr(it, "type", "") == "fallback_message" for it in (getattr(u, "iterations", None) or []))
        return Step(list(resp.content), [b.model_dump(mode="json", exclude_none=True) for b in resp.content],
                    resp.stop_reason or "", usage, fallback)


class OpenAILLM:
    """OpenAI Chat Completions behind the same step() interface. The conversation is kept in the harness's
    (Anthropic-shaped) format and translated on every call, so run_task doesn't care which provider it talks to."""

    def __init__(self, model: str, effort: str, base_url: str | None = None, key_env: str = "OPENAI_API_KEY"):
        import openai

        _trust_os_certificates()
        self.openai = openai
        self.client = openai.AsyncOpenAI(max_retries=4, base_url=base_url, api_key=os.environ.get(key_env))
        self.model, self.effort = model, effort
        # OpenAI-compatible APIs (DeepSeek, …) don't take reasoning_effort; OpenAI models that don't are found on
        # the first 400.
        self.send_effort = base_url is None

    @staticmethod
    def _messages(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for m in messages:
            c = m["content"]
            if isinstance(c, str):
                out.append({"role": m["role"], "content": c})
            elif m["role"] == "assistant":
                text = "\n".join(b["text"] for b in c if b.get("type") == "text") or None
                calls = [{"id": b["id"], "type": "function",
                          "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {})}}
                         for b in c if b.get("type") == "tool_use"]
                out.append({"role": "assistant", "content": text, **({"tool_calls": calls} if calls else {})})
            else:  # tool results
                out += [{"role": "tool", "tool_call_id": b["tool_use_id"],
                         "content": ("ERROR: " if b.get("is_error") else "") + b["content"]} for b in c]
        return out

    async def step(self, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]) -> Step:
        kwargs: dict[str, Any] = dict(
            model=self.model, max_completion_tokens=16000, messages=self._messages(system, messages),
            tools=[{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                                     "parameters": t["input_schema"]}} for t in tools],
        )
        if self.send_effort:
            try:
                resp = await self.client.chat.completions.create(reasoning_effort=self.effort, **kwargs)
            except self.openai.BadRequestError as e:
                if "reasoning_effort" not in str(e) and "reasoning.effort" not in str(e):
                    raise
                self.send_effort = False
                resp = await self.client.chat.completions.create(**kwargs)
        else:
            resp = await self.client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        msg = choice.message
        blocks: list[dict[str, Any]] = []
        if msg.content:
            blocks.append({"type": "text", "text": msg.content})
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_unparsed_arguments": tc.function.arguments}
            blocks.append({"type": "tool_use", "id": tc.id, "name": tc.function.name, "input": args})
        stop = {"tool_calls": "tool_use", "length": "max_tokens", "content_filter": "refusal"}.get(
            choice.finish_reason or "", "end_turn")
        if getattr(msg, "refusal", None):
            stop = "refusal"
        if stop == "end_turn" and any(b["type"] == "tool_use" for b in blocks):
            stop = "tool_use"
        u = resp.usage
        cached = (u.prompt_tokens_details.cached_tokens or 0) if u and u.prompt_tokens_details else 0
        cached = cached or (getattr(u, "prompt_cache_hit_tokens", 0) or 0)  # DeepSeek reports it here
        usage = {"input": (u.prompt_tokens if u else 0) - cached, "cache_read": cached, "cache_write": 0,
                 "output": u.completion_tokens if u else 0}
        return Step(blocks, blocks, stop, usage)


def make_llm(model: str, effort: str) -> Any:
    name, base_url, key_env = provider(model)
    return AnthropicLLM(model, effort) if name == "anthropic" else OpenAILLM(model, effort, base_url, key_env)


class ScriptedLLM:
    """No model calls: a fixed policy that makes one realistic tool call per question, then answers with the
    known truth. It exercises every part of the harness for free; token counts are estimated (chars / 4)."""

    def __init__(self, task: Task, truth: Any, kind: str):
        self.task, self.truth, self.kind = task, truth, kind

    def _call(self) -> tuple[str, dict[str, Any]]:
        files = {"lookup": ("crm/customers.csv", None), "text-search": None, "web": None}
        cat = self.task.category
        if cat == "web":
            url = "https://en.wikipedia.org/wiki/Python_(programming_language)"
            return ("web__fetch_url", {"url": url}) if self.kind == "direct" else (
                "execute_code", {"code": f"from servers.web import fetch_url\npage = fetch_url(url={url!r})\n"
                                         "len(page['text'])"})
        if cat == "text-search":
            return ("files__search_text", {"query": "Lighthouse"}) if self.kind == "direct" else (
                "execute_code", {"code": "from servers.files import search_text\nsearch_text(query='Lighthouse')"})
        path, sheet = files.get(cat) or ("sales/sales_2025.xlsx", "Orders")
        if self.kind == "direct":
            return "files__read_table", {"path": path, **({"sheet": sheet} if sheet else {})}
        sheet_arg = f", sheet={sheet!r}" if sheet else ""
        return "execute_code", {"code": "from servers.files import read_table\n"
                                        f"rows = read_table(path={path!r}{sheet_arg})['rows']\nlen(rows)"}

    async def step(self, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]) -> Step:
        context = len(system) + len(json.dumps(tools)) + len(json.dumps(messages, default=str))
        usage = {"input": context // 4, "cache_read": 0, "cache_write": 0, "output": 60}
        last = messages[-1]["content"]
        answered_tool = isinstance(last, list) and any(b.get("type") == "tool_result" for b in last)
        if not answered_tool:
            name, args = self._call()
            block = {"type": "tool_use", "id": "toolu_" + secrets.token_hex(6), "name": name, "input": args}
            return Step([block], [block], "tool_use", usage)
        t = self.truth
        text = ", ".join(map(str, t)) if isinstance(t, (list, tuple)) else str(t)
        block = {"type": "text", "text": f"Here is what I found.\nFINAL ANSWER: {text}"}
        return Step([block], [block], "end_turn", usage)


# ---------------------------------------------------------------- the loop


def _cost(model: str, usage: dict[str, int]) -> float:
    i, o, cr = PRICES.get(model, (0, 0, 0))
    limit, mult = LONG_PROMPT.get(model, (None, 1.0))
    if limit is not None and usage["input"] + usage["cache_read"] + usage["cache_write"] > limit:
        i, o, cr = i * mult, o * mult, cr * mult
    return (usage["input"] * i + usage["cache_write"] * i * 1.25 + usage["cache_read"] * cr + usage["output"] * o) / 1e6


def _is_load_error(e: BaseException) -> bool:
    """Rate limits and overload: the provider's capacity, not the request. Worth waiting out."""
    status = getattr(e, "status_code", None)
    return status in (429, 503, 529) or "rate limit" in str(e).lower() or "overloaded" in str(e).lower()


async def _step_with_backoff(llm: Any, *args: Any) -> Step:
    for attempt in range(8):  # the SDKs already retried a few times; this waits out per-minute limits
        try:
            return await llm.step(*args)
        except Exception as e:  # noqa: BLE001
            if not _is_load_error(e) or attempt == 7:
                raise
            await asyncio.sleep(min(90.0, 10.0 * 2**attempt) * (0.75 + random.random() / 2))
    raise AssertionError("unreachable")


async def run_task(task: Task, truth: Any, conn: Any, llm: Any, system: str, tools: list[dict[str, Any]],
                   res: Result, transcript: list[dict[str, Any]], max_steps: int, max_input_tokens: int) -> None:
    messages: list[dict[str, Any]] = []
    last_text = ""
    started = time.perf_counter()
    try:
        for ti, prompt in enumerate(task.turns):
            if ti in task.restart_before:
                await conn.restart()
                transcript.append({"event": "runtime restarted by the harness"})
            messages.append({"role": "user", "content": prompt})
            transcript.append({"role": "user", "content": prompt})
            for _ in range(max_steps):
                step = await _step_with_backoff(llm, system, tools, messages)
                u = step.usage
                res.api_calls += 1
                total_in = u["input"] + u["cache_read"] + u["cache_write"]
                res.input_tokens += total_in
                res.uncached_input_tokens += u["input"]
                res.cache_read_tokens += u["cache_read"]
                res.cache_write_tokens += u["cache_write"]
                res.output_tokens += u["output"]
                res.peak_context_tokens = max(res.peak_context_tokens, total_in)
                res.cost_usd += _cost(res.model, u)
                res.fallback_used |= step.fallback_used
                messages.append({"role": "assistant", "content": step.content})
                transcript.append({"role": "assistant", "content": step.dumped, "stop_reason": step.stop_reason,
                                   "usage": u})
                texts = [b.get("text", "") for b in step.dumped if b.get("type") == "text"]
                if texts:
                    last_text = "\n".join(texts)
                if step.stop_reason == "refusal":
                    raise RuntimeError("the model declined (refusal)")
                if step.stop_reason == "max_tokens":
                    raise RuntimeError("the response hit max_tokens")
                uses = [b for b in step.dumped if b.get("type") == "tool_use"]
                if step.stop_reason != "tool_use" or not uses:
                    break
                outs = await asyncio.gather(*(conn.call(b["name"], b.get("input") or {}) for b in uses))
                results = []
                for b, (text, is_err) in zip(uses, outs):
                    res.tool_calls += 1
                    res.chars_to_model += len(text)
                    results.append({"type": "tool_result", "tool_use_id": b["id"], "content": text, "is_error": is_err})
                    transcript.append({"tool": b["name"], "input": b.get("input"), "is_error": is_err,
                                       "result_chars": len(text), "result_head": text[:1500]})
                messages.append({"role": "user", "content": results})
                if res.input_tokens > max_input_tokens:
                    raise RuntimeError(f"stopped: task used more than {max_input_tokens:,} input tokens")
            else:
                raise RuntimeError(f"stopped: more than {max_steps} model calls for one question")
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        res.excluded = _is_load_error(e)
        if "prompt is too long" in msg or "too many tokens" in msg.lower():
            msg = "context overflow: " + msg[:200]
        res.error = msg[:500]
    res.wall_s = round(time.perf_counter() - started, 2)
    res.answer = final_answer(last_text)[:2000]
    res.success = res.error is None and task.check(res.answer, truth)
    res.repeated_fetches = conn.repeated()
    res.bytes_from_tools = conn.bytes_from_tools


@dataclass
class Plan:
    data_dir: Path
    out_dir: Path
    configs: list[str]
    models: list[str]
    tasks: list[Task]
    repeats: int = 1
    effort: str = "medium"
    llm: str = "api"  # api (each model's own provider) | scripted
    driver: str = "local"
    direct_cap_chars: int = 100_000
    max_steps: int = 30
    max_input_tokens: int = 3_000_000
    max_cost: float | None = None
    concurrency: int = 2


# Errors that fail every request until the account changes; retrying the remaining tasks only floods the report.
ACCOUNT_ERRORS = ("credit balance is too low", "reached your specified api usage limits", "authentication_error",
                  "permission_error", "invalid x-api-key", "insufficient_quota", "invalid_api_key", "insufficient balance",
                  "incorrect api key")


async def run(plan: Plan, log=print) -> Path:
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
    out = plan.out_dir / run_id
    (out / "transcripts").mkdir(parents=True, exist_ok=True)
    web = any("web" in t.needs for t in plan.tasks)
    meta = {"run_id": run_id, "started": time.time(), "configs": {c: asdict(CONFIGS[c]) for c in plan.configs},
            "models": plan.models, "tasks": [t.id for t in plan.tasks], "repeats": plan.repeats, "effort": plan.effort,
            "llm": plan.llm, "driver": plan.driver, "direct_cap_chars": plan.direct_cap_chars,
            "data_dir": str(plan.data_dir), "status": "running"}
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    truths = {t.id: t.truth(plan.data_dir) for t in plan.tasks}
    results_f = open(out / "results.jsonl", "a")
    spent = 0.0
    stop = False
    stop_reason = "spending limit"
    try:
        for model in plan.models:
            for cname in plan.configs:
                if stop:
                    break
                cfg = CONFIGS[cname]
                log(f"\n▸ {model} · {cfg.label}")
                backend = (DirectBackend(plan.data_dir, web, plan.direct_cap_chars) if cfg.kind == "direct" else
                           GatewayBackend(plan.data_dir, web, cfg.overrides, out / "work" / f"{model}-{cname}", plan.driver))
                await backend.start()
                system = SYSTEM + (f"\n\n# Notes from your tools\n{backend.instructions}" if backend.instructions else "")
                sem = asyncio.Semaphore(max(1, plan.concurrency))

                async def one(task: Task, rep: int) -> None:
                    nonlocal spent, stop, stop_reason
                    async with sem:
                        if stop:
                            return
                        res = Result(run_id, cname, model, task.id, task.category, rep,
                                     expected=json.dumps(truths[task.id], default=str), simulated=plan.llm == "scripted")
                        transcript: list[dict[str, Any]] = []
                        conn = await backend.open_task()
                        llm = (ScriptedLLM(task, truths[task.id], cfg.kind) if plan.llm == "scripted"
                               else make_llm(model, plan.effort))
                        try:
                            await run_task(task, truths[task.id], conn, llm, system, backend.tool_defs if cfg.kind == "direct"
                                           else conn.tool_defs, res, transcript, plan.max_steps, plan.max_input_tokens)
                        finally:
                            await conn.close()
                        spent += res.cost_usd
                        results_f.write(json.dumps(asdict(res)) + "\n")
                        results_f.flush()
                        (out / "transcripts" / f"{model}--{cname}--{task.id}--{rep}.json").write_text(
                            json.dumps({"result": asdict(res), "system": system, "transcript": transcript}, indent=1, default=str))
                        mark = "✓" if res.success else "✗"
                        log(f"  {mark} {task.id:20s} in {res.input_tokens:>9,} tok · peak {res.peak_context_tokens:>8,} · "
                            f"{res.api_calls:>2} calls · ${res.cost_usd:.3f}" + (f" · {res.error[:70]}" if res.error else ""))
                        if not stop and res.error and any(e in res.error.lower() for e in ACCOUNT_ERRORS):
                            stop, stop_reason = True, "API account error"
                            log(f"  ! the API refused the request for an account reason; stopping:\n    {res.error}")
                        if plan.max_cost is not None and spent >= plan.max_cost and not stop:
                            stop = True
                            log(f"  ! spending limit reached (${spent:.2f}); stopping")

                try:
                    await asyncio.gather(*(one(t, r) for t in plan.tasks for r in range(plan.repeats)))
                finally:
                    await backend.stop()
        meta["status"] = f"stopped ({stop_reason})" if stop else "done"
    except BaseException:
        meta["status"] = "failed"
        raise
    finally:
        meta["finished"] = time.time()
        meta["spent_usd"] = round(spent, 4)
        (out / "meta.json").write_text(json.dumps(meta, indent=2))
        results_f.close()
    return out
