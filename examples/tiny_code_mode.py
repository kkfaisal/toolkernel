"""Code mode in about 80 lines: an agent with one tool, `run_python`, and a persistent Python namespace.

The "tools" are ordinary Python functions placed in the namespace. The model calls them from its own code,
so big results land in Python variables and only what the code prints or returns goes back to the model.

  pip install openai
  export OPENAI_API_KEY=...
  python examples/tiny_code_mode.py "Which region has the slowest average resolution time for urgent tickets?"

No sandbox here: exec() runs on your machine. Fine for a demo, never for untrusted code (see ToolKernel for
process/container isolation).
"""
import ast
import contextlib
import csv
import io
import json
import sys
from pathlib import Path

from openai import OpenAI

DATA = Path(__file__).resolve().parent.parent / ".toolkernel" / "bench-data"
MAX_RESULT = 2_000  # characters of output the model may see per step


# 1. The tools: plain functions. In a real setup these would call MCP servers.
def read_csv(path: str) -> list[dict]:
    """Read a CSV file under the data folder into a list of row dicts."""
    with open(DATA / path, newline="") as f:
        return list(csv.DictReader(f))


def read_jsonl(path: str) -> list[dict]:
    """Read a JSON Lines file under the data folder into a list of dicts."""
    return [json.loads(line) for line in open(DATA / path)]


def list_files() -> list[str]:
    """List the data files available."""
    return sorted(str(p.relative_to(DATA)) for p in DATA.rglob("*") if p.is_file())


namespace = {"read_csv": read_csv, "read_jsonl": read_jsonl, "list_files": list_files}  # survives between steps


# 2. The one tool the model sees: run Python, return stdout plus the value of the last line, kept short.
def run_python(code: str) -> str:
    out = io.StringIO()
    try:
        tree = ast.parse(code)
        last = tree.body.pop() if tree.body and isinstance(tree.body[-1], ast.Expr) else None
        with contextlib.redirect_stdout(out):
            exec(compile(tree, "<agent>", "exec"), namespace)
            if last is not None:
                value = eval(compile(ast.Expression(last.value), "<agent>", "eval"), namespace)
                if value is not None:
                    print(repr(value))
    except Exception as e:  # noqa: BLE001
        print(f"{type(e).__name__}: {e}", file=out)
    text = out.getvalue() or "(no output)"
    if len(text) > MAX_RESULT:
        text = text[:MAX_RESULT] + f"\n[cut: {len(text) - MAX_RESULT:,} more characters. Print less.]"
    return text


TOOL = {"type": "function", "function": {
    "name": "run_python",
    "description": "Run Python. Variables persist between calls. Available functions: "
                   "list_files(), read_csv(path), read_jsonl(path). Load data into variables and compute "
                   "with code; print only small results.",
    "parameters": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}}}


# 3. The agent loop.
def ask(question: str, model: str = "gpt-5-mini") -> None:
    client = OpenAI()
    messages = [{"role": "system", "content": "Answer questions about company data by writing Python."},
                {"role": "user", "content": question}]
    tokens = 0
    for _ in range(15):
        r = client.chat.completions.create(model=model, messages=messages, tools=[TOOL])
        tokens += r.usage.total_tokens
        msg = r.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))
        if not msg.tool_calls:
            print(f"\nAnswer: {msg.content}\nTotal tokens: {tokens:,}")
            return
        for call in msg.tool_calls:
            code = json.loads(call.function.arguments)["code"]
            result = run_python(code)
            print(f"--- model ran:\n{code}\n--- it saw:\n{result}")
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})


if __name__ == "__main__":
    ask(sys.argv[1] if len(sys.argv) > 1 else
        "Which region has the slowest average resolution time for urgent support tickets?")
