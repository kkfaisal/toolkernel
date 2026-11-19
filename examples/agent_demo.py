"""End-to-end agent demo: one question, one agent loop, two ways of giving it tools.

  direct      the agent gets the files MCP server's tools and every result goes into its context
  toolkernel  the agent connects to ToolKernel (one MCP server) and answers by writing Python

The agent loop is identical in both modes. Only the MCP connection changes. At the end it prints how many
tokens each mode used and what that cost.

Setup:
  pip install anthropic openai mcp truststore     (or: uv sync --extra bench)
  export ANTHROPIC_API_KEY=...    (and/or OPENAI_API_KEY, DEEPSEEK_API_KEY)
  toolkernel serve                          (for toolkernel mode, in another terminal)

Run:
  python examples/agent_demo.py
  python examples/agent_demo.py --model gpt-5-mini --mode toolkernel "Which region had the most revenue in 2025?"
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import re
import sys
import time
from pathlib import Path

import anthropic
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

with contextlib.suppress(ImportError):  # use the OS certificate store (helps behind corporate proxies)
    import truststore

    truststore.inject_into_ssl()

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / ".toolkernel" / "bench-data"          # sample Excel / CSV / JSONL files
TOOLKERNEL_URL = "http://localhost:7300/mcp"
MAX_TURNS = 25
RESULT_CAP = 100_000  # direct mode: most MCP clients cut huge tool results, so we do too (in characters)

DEFAULT_QUESTION = "What was the total revenue for the EMEA region in Q3 2025 (July to September)?"


# ---------------------------------------------------------------------------------------------- connections

@contextlib.asynccontextmanager
async def connect(mode: str):
    """Open an MCP session. This is the only part that differs between the two modes."""
    async with contextlib.AsyncExitStack() as stack:
        if mode == "direct":
            params = StdioServerParameters(
                command=sys.executable,
                args=["-m", "toolkernel_gateway.mcp_servers.files", "--root", str(DATA)])
            read, write = await stack.enter_async_context(stdio_client(params))
        else:
            read, write, *_ = await stack.enter_async_context(streamable_http_client(TOOLKERNEL_URL))
        session = await stack.enter_async_context(ClientSession(read, write))
        init = await session.initialize()
        yield session, init.instructions or ""


# ---------------------------------------------------------------------------------------------- the models
# Each adapter takes the conversation and returns (text, tool_calls, usage). The conversation is kept in each
# provider's own format; the agent loop below doesn't need to know which one it is talking to.

class Claude:
    def __init__(self, model: str):
        self.client, self.model = anthropic.AsyncAnthropic(), model

    def start(self, system, tools, question):
        self.system, self.messages = system, [{"role": "user", "content": question}]
        self.tools = [{"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
                      for t in tools]

    async def ask(self):
        r = await self.client.messages.create(model=self.model, max_tokens=4000, system=self.system,
                                              tools=self.tools, messages=self.messages,
                                              cache_control={"type": "ephemeral"})
        self.messages.append({"role": "assistant", "content": r.content})
        u = r.usage
        usage = {"input": u.input_tokens + (u.cache_creation_input_tokens or 0),
                 "cached": u.cache_read_input_tokens or 0, "output": u.output_tokens}
        text = "".join(b.text for b in r.content if b.type == "text")
        calls = [(b.id, b.name, b.input) for b in r.content if b.type == "tool_use"]
        return text, calls, usage

    def add_results(self, results):  # [(id, text, is_error)]
        self.messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": i, "content": t, "is_error": e} for i, t, e in results]})


class OpenAICompatible:
    """OpenAI, DeepSeek, or anything else that speaks the Chat Completions API."""

    def __init__(self, model: str, base_url: str | None = None, key_env: str = "OPENAI_API_KEY"):
        import os

        import openai

        self.client = openai.AsyncOpenAI(base_url=base_url, api_key=os.environ.get(key_env))
        self.model = model

    def start(self, system, tools, question):
        self.messages = [{"role": "system", "content": system}, {"role": "user", "content": question}]
        self.tools = [{"type": "function", "function": {"name": t.name, "description": t.description or "",
                                                        "parameters": t.input_schema}} for t in tools]

    async def ask(self):
        r = await self.client.chat.completions.create(model=self.model, messages=self.messages, tools=self.tools)
        msg = r.choices[0].message
        self.messages.append(msg.model_dump(exclude_none=True))
        u = r.usage
        cached = getattr(u, "prompt_cache_hit_tokens", None)  # DeepSeek
        if cached is None:
            cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0
        usage = {"input": u.prompt_tokens - cached, "cached": cached, "output": u.completion_tokens}
        calls = [(c.id, c.function.name, json.loads(c.function.arguments or "{}")) for c in msg.tool_calls or []]
        return msg.content or "", calls, usage

    def add_results(self, results):
        for i, t, _ in results:
            self.messages.append({"role": "tool", "tool_call_id": i, "content": t})


MODELS = {  # name: (adapter factory, USD per million tokens: input, cached input, output)
    "claude-haiku-5-5": (lambda: Claude("claude-haiku-5-5"), (0.10, 0.01, 0.50)),
    "gpt-5-mini": (lambda: OpenAICompatible("gpt-5-mini"), (0.25, 0.025, 2.00)),
    "deepseek-flash": (lambda: OpenAICompatible("deepseek-flash", "https://api.deepseek.com", "DEEPSEEK_API_KEY"),
                       (0.30, 0.006, 1.20)),
}


# ---------------------------------------------------------------------------------------------- the agent

async def run_agent(mode: str, model_name: str, question: str, verbose: bool) -> dict:
    make_model, price = MODELS[model_name]
    model = make_model()
    usage = {"input": 0, "cached": 0, "output": 0}
    bytes_to_model = bytes_in_sandbox = tool_calls = 0
    started = time.time()

    async with connect(mode) as (mcp, server_instructions):
        tools = (await mcp.list_tools()).tools
        system = ("You answer questions about company data using your tools. "
                  "Don't ask the user follow-up questions: if something is ambiguous, pick the most reasonable reading and "
                  "say which one you used. Give the final answer in one short sentence.\n\n" + server_instructions)
        model.start(system, tools, question)
        print(f"\n=== {mode} · {len(tools)} tools: {', '.join(t.name for t in tools)}")

        answer = "(no answer: hit the turn limit)"
        for turn in range(1, MAX_TURNS + 1):
            try:
                text, calls, u = await model.ask()
            except Exception as e:  # e.g. the conversation no longer fits in the model's context window
                answer = f"(failed: {str(e)[:200]})"
                print(f"  turn {turn}: API error: {e}")
                break
            for k in usage:
                usage[k] += u[k]
            print(f"  turn {turn}: model read {u['input'] + u['cached']:,} tokens, wrote {u['output']:,}")
            if not calls:
                answer = text.strip()
                break

            results = []
            for call_id, name, arguments in calls:
                tool_calls += 1
                res = await mcp.call_tool(name, arguments)
                out = "\n".join(c.text for c in res.content if c.type == "text")
                for amount, unit in re.findall(r"data via tools ([\d.]+) (B|KB|MB)", out):  # ToolKernel's [meta] line
                    bytes_in_sandbox += float(amount) * {"B": 1, "KB": 1e3, "MB": 1e6}[unit]
                if len(out) > RESULT_CAP:
                    out = out[:RESULT_CAP] + f"\n[truncated: {len(out) - RESULT_CAP:,} more characters]"
                if verbose:
                    print(f"    -> {name}({json.dumps(arguments)[:300]})")
                    print("    <- " + out[:400].replace("\n", "\n       "))
                else:
                    print(f"    -> {name} returned {len(out.encode()):,} bytes to the model")
                bytes_to_model += len(out.encode())
                results.append((call_id, out, bool(res.is_error)))
            model.add_results(results)

    cost = (usage["input"] * price[0] + usage["cached"] * price[1] + usage["output"] * price[2]) / 1e6
    return {"mode": mode, "answer": answer, "turns": turn, "tool_calls": tool_calls, "usage": usage,
            "total_tokens": sum(usage.values()), "bytes_to_model": bytes_to_model, "bytes_in_sandbox": bytes_in_sandbox, "cost": cost,
            "seconds": time.time() - started}


# ---------------------------------------------------------------------------------------------- report

def _size(n: float) -> str:
    return f"{n / 1e6:.2f} MB" if n >= 1e6 else f"{n / 1e3:.1f} KB"


def report(results: list[dict]) -> None:
    for r in results:
        print(f"\n[{r['mode']}] {r['answer']}")
    print(f"\n{'':22}" + "".join(f"{r['mode']:>14}" for r in results))
    rows = [
        ("turns", lambda r: f"{r['turns']}"),
        ("tool calls", lambda r: f"{r['tool_calls']}"),
        ("tool output to model", lambda r: _size(r["bytes_to_model"])),
        ("data kept in sandbox", lambda r: _size(r["bytes_in_sandbox"]) if r["mode"] == "toolkernel" else "-"),
        ("input tokens", lambda r: f"{r['usage']['input']:,}"),
        ("cached input tokens", lambda r: f"{r['usage']['cached']:,}"),
        ("output tokens", lambda r: f"{r['usage']['output']:,}"),
        ("total tokens", lambda r: f"{r['total_tokens']:,}"),
        ("cost (USD)", lambda r: f"${r['cost']:.4f}"),
        ("time", lambda r: f"{r['seconds']:.0f} s"),
    ]
    for name, fmt in rows:
        print(f"{name:22}" + "".join(f"{fmt(r):>14}" for r in results))
    if len(results) == 2 and results[1]["total_tokens"]:
        a, b = results
        print(f"\n{b['mode']} used {a['total_tokens'] / b['total_tokens']:.1f}x fewer tokens than {a['mode']}.")


async def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("question", nargs="?", default=DEFAULT_QUESTION)
    p.add_argument("--mode", choices=["both", "direct", "toolkernel"], default="both")
    p.add_argument("--verbose", "-v", action="store_true", help="print each tool call and the start of its result")
    p.add_argument("--model", choices=list(MODELS), default="claude-haiku-5-5")
    args = p.parse_args()

    print(f"Question: {args.question}\nModel: {args.model}")
    modes = ["direct", "toolkernel"] if args.mode == "both" else [args.mode]
    report([await run_agent(m, args.model, args.question, args.verbose) for m in modes])


if __name__ == "__main__":
    asyncio.run(main())
