"""A small, real agent: Claude (Anthropic API) + ToolKernel over MCP.

    toolkernel agent                          # chat; the runtime keeps your data between questions
    toolkernel agent "Which region grew most in Q2?" --transcript run.md

It connects to a running gateway, gives Claude the gateway's tools and instructions, and runs the
tool-use loop: every step (Claude's notes, the code it runs, what came back) is printed as it happens.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}

SYSTEM = """You are a helpful analyst with access to a company's tools through a Python runtime.
Work in small steps, check data before relying on it, and never guess numbers. When something
fails, read the error and adjust. Finish with a clear, concise answer for the user."""


def _get(block: Any, key: str, default: Any = None) -> Any:
    return block.get(key, default) if isinstance(block, dict) else getattr(block, key, default)


class Style:
    def __init__(self, enabled: bool):
        self.on = enabled

    def __call__(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.on else text


@dataclass
class Usage:
    calls: int = 0
    input: int = 0
    cache_read: int = 0
    cache_write: int = 0
    output: int = 0
    tool_calls: int = 0

    def add(self, u: Any) -> None:
        self.calls += 1
        self.input += _get(u, "input_tokens", 0) or 0
        self.cache_read += _get(u, "cache_read_input_tokens", 0) or 0
        self.cache_write += _get(u, "cache_creation_input_tokens", 0) or 0
        self.output += _get(u, "output_tokens", 0) or 0

    def line(self) -> str:
        total_in = self.input + self.cache_read + self.cache_write
        return (f"{self.calls} model calls · {self.tool_calls} tool calls · {total_in:,} input tokens "
                f"({self.cache_read:,} from cache) · {self.output:,} output tokens")


@dataclass
class Agent:
    gateway_url: str = "http://localhost:7300/mcp"
    api_key: str | None = None  # gateway API key, if it requires one
    model: str = "claude-opus-5-5"
    effort: str = "medium"
    max_steps: int = 40
    client: Any = None  # an anthropic.AsyncAnthropic (or a stand-in, for tests)
    out: Any = field(default_factory=lambda: sys.stdout)
    color: bool | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    transcript: list[str] = field(default_factory=list)

    async def __aenter__(self) -> Agent:
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        from mcp.shared._httpx_utils import create_mcp_http_client

        self.stack = contextlib.AsyncExitStack()
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        http = await self.stack.enter_async_context(create_mcp_http_client(headers=headers))
        streams = await self.stack.enter_async_context(streamable_http_client(self.gateway_url, http_client=http))
        self.session = await self.stack.enter_async_context(ClientSession(streams[0], streams[1]))
        init = await self.session.initialize()
        self.system = SYSTEM + (f"\n\n# How to use your tools\n{init.instructions}" if init.instructions else "")
        self.tools = [{"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
                      for t in (await self.session.list_tools()).tools]
        if self.client is None:
            import anthropic

            with contextlib.suppress(ImportError):  # HTTPS behind corporate TLS inspection
                import truststore

                truststore.inject_into_ssl()
            self.client = anthropic.AsyncAnthropic()
        if self.color is None:
            self.color = self.out.isatty() and not os.environ.get("NO_COLOR")
        self.s = Style(bool(self.color))
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.stack.aclose()

    # -- output ------------------------------------------------------------------

    def _print(self, text: str = "") -> None:
        print(text, file=self.out, flush=True)

    def _show_tool_call(self, name: str, args: dict[str, Any]) -> None:
        s = self.s
        if name == "execute_code":
            self._print(s("36", f"  ▸ {name}"))
            for line in str(args.get("code", "")).rstrip().splitlines():
                self._print(s("36", "    │ ") + line)
            self.transcript += [f"**▸ `{name}`**", "", "```python", str(args.get("code", "")).rstrip(), "```", ""]
        else:
            short = json.dumps(args, ensure_ascii=False)
            self._print(s("36", f"  ▸ {name}") + s("2", f" {short[:200]}"))
            self.transcript += [f"**▸ `{name}`** `{short}`", ""]

    def _show_result(self, text: str, is_error: bool) -> None:
        s = self.s
        lines = text.rstrip().splitlines()
        shown = lines[:14] + ([f"… {len(lines) - 14} more lines"] if len(lines) > 14 else [])
        for line in shown:
            self._print(s("31" if is_error else "2", "    ◂ " + line[:220]))
        self.transcript += ["```", text.rstrip(), "```", ""]

    # -- the loop ------------------------------------------------------------------

    async def _model_call(self) -> Any:
        kwargs: dict[str, Any] = dict(
            model=self.model, max_tokens=16000, system=self.system, tools=self.tools, messages=self.messages,
            thinking={"type": "adaptive"}, output_config={"effort": self.effort},
            cache_control={"type": "ephemeral"},
        )
        if self.model in FALLBACK_MODELS:
            return await self.client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
        return await self.client.messages.create(**kwargs)

    async def ask(self, question: str) -> str:
        """Run one user turn to completion and return Claude's final text."""
        s = self.s
        self.messages.append({"role": "user", "content": question})
        self.transcript += [f"## You", "", question, ""]
        final = ""
        for _ in range(self.max_steps):
            resp = await self._model_call()
            self.usage.add(resp.usage)
            content = list(resp.content)
            self.messages.append({"role": "assistant", "content": content})
            texts = [_get(b, "text", "") for b in content if _get(b, "type") == "text"]
            uses = [b for b in content if _get(b, "type") == "tool_use"]
            stop = resp.stop_reason
            if texts and uses:  # notes between steps
                note = "\n".join(t for t in texts if t.strip())
                if note:
                    self._print(s("2;3", "  " + note.replace("\n", "\n  ")))
                    self.transcript += [f"> {note}", ""]
            if stop == "refusal":
                final = "(Claude declined this request.)"
                break
            if stop == "max_tokens":
                final = "\n".join(texts) + "\n(The reply was cut off at the token limit.)"
                break
            if stop == "pause_turn":
                continue
            if stop != "tool_use" or not uses:
                final = "\n".join(texts)
                break
            results = []
            for b in uses:
                name, args = _get(b, "name"), _get(b, "input") or {}
                self._show_tool_call(name, args)
                t0 = time.perf_counter()
                try:
                    r = await self.session.call_tool(name, args)
                    text = "\n".join(_get(c, "text", "") or f"<{_get(c, 'type')}>" for c in r.content)
                    is_err = bool(r.is_error)
                except Exception as e:  # noqa: BLE001  report it to Claude; it can usually work around it
                    text, is_err = f"Tool call failed: {e}", True
                self.usage.tool_calls += 1
                self._show_result(text, is_err)
                if time.perf_counter() - t0 > 5:
                    self._print(s("2", f"    ({time.perf_counter() - t0:.0f}s; if this is waiting on an approval, "
                                       "open the Approvals page)"))
                results.append({"type": "tool_result", "tool_use_id": _get(b, "id"), "content": text, "is_error": is_err})
            self.messages.append({"role": "user", "content": results})
        else:
            final = f"(Stopped after {self.max_steps} model calls without a final answer.)"
        self._print()
        self._print(final)
        self._print()
        self._print(s("2", "  " + self.usage.line()))
        self.transcript += ["## Claude", "", final, "", f"_{self.usage.line()}_", ""]
        return final

    def save_transcript(self, path: Path) -> None:
        path.write_text(f"# Agent session · {self.model}\n\n" + "\n".join(self.transcript))


async def chat(agent: Agent, first: str | None, transcript: Path | None) -> None:
    async with agent:
        s = agent.s
        if first:
            await agent.ask(first)
        else:
            agent._print(s("1", "ToolKernel agent") + s("2", f" · {agent.model} · {agent.gateway_url}"))
            agent._print(s("2", "Ask anything about your data. Variables persist between questions. "
                                "/reset clears the runtime, /quit leaves.\n"))
            while True:
                try:
                    q = (await asyncio.to_thread(input, s("1", "you › "))).strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if not q:
                    continue
                if q in ("/quit", "/exit"):
                    break
                if q == "/reset":
                    r = await agent.session.call_tool("reset_runtime", {})
                    agent._print(s("2", r.content[0].text))
                    continue
                agent._print()
                await agent.ask(q)
        if transcript:
            agent.save_transcript(transcript)
            agent._print(s("2", f"transcript saved to {transcript}"))
