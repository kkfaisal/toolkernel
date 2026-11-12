"""Benchmark: data generation, ground truth, answer checking, the files server, and a scripted harness run."""

import json
import os
import sys
from pathlib import Path

import pytest

from toolkernel_gateway.bench.data import generate
from toolkernel_gateway.bench.harness import Plan, run
from toolkernel_gateway.bench.report import load, markdown, summarize
from toolkernel_gateway.bench.tasks import TASKS, final_answer, get_tasks, has_name, has_number


@pytest.fixture(scope="module")
def data(tmp_path_factory):
    root = tmp_path_factory.mktemp("bench-data")
    generate(root)
    return root


def test_data_is_deterministic(tmp_path, data):
    generate(tmp_path)
    assert (tmp_path / "crm" / "customers.csv").read_bytes() == (data / "crm" / "customers.csv").read_bytes()
    assert (tmp_path / "support" / "tickets.jsonl").read_bytes() == (data / "support" / "tickets.jsonl").read_bytes()
    truths = {t.id: t.truth(data) for t in TASKS}
    assert truths["lighthouse_date"] == "2026-03-18" and len(truths["explore_region"]) == 3


def test_checkers():
    assert final_answer("blah\nFINAL ANSWER: $15,960,537") == "$15,960,537"
    assert has_number("about $15.96M", 15_960_536.65, rel=0.001)
    assert has_number("15,960,537", 15_960_536.65)
    assert not has_number("15,000,000", 15_960_536.65)
    assert has_name("The slowest is Storage Node Max.", "Storage Node Max")
    assert not has_name("The slowest is Storage Node Max.", "Storage Node")
    assert not has_name("Atlas Pharma AG 2", "Atlas Pharma AG")


def test_files_server_keeps_na_region(data, monkeypatch):
    from toolkernel_gateway.mcp_servers import files

    monkeypatch.setattr(files, "ROOT", data.resolve())
    rows = files.read_table(path="sales/sales_2025.xlsx", sheet="Orders", where={"region": "NA"}, limit=2)
    assert rows["total_rows"] > 8000 and rows["rows"][0]["region"] == "NA"  # not NaN
    assert files.describe_file("sales/sales_2025.xlsx")["sheets"][2]["name"] == "Targets"
    assert files.search_text("Lighthouse", "reports")[0]["path"].startswith("reports/")
    with pytest.raises(files.ToolError, match="outside"):
        files.read_text("../../etc/passwd")
    # An empty pattern lists everything (models often pass "" for "no filter").
    assert len(files.list_files(path="", pattern="")) == len(files.list_files())


async def test_scripted_run_end_to_end(tmp_path, data):
    plan = Plan(data_dir=data, out_dir=tmp_path, configs=["direct", "toolkernel"], models=["claude-opus-5-5"],
                tasks=get_tasks("lookup_email,emea_q3_revenue,restart_recovery"), llm="scripted", concurrency=3)
    out = await run(plan, log=lambda *a: None)
    meta, rows = load(out)
    assert meta["status"] == "done" and len(rows) == 6
    assert all(r["success"] for r in rows), [r["error"] for r in rows]
    by = {(r["config"], r["task"]): r for r in rows}
    # Direct mode pushes the (capped) raw table at the model; ToolKernel keeps it in the runtime.
    assert by[("direct", "emea_q3_revenue")]["chars_to_model"] > 90_000
    assert by[("toolkernel", "emea_q3_revenue")]["chars_to_model"] < 1_000
    # The harness really restarted the runtime, and the agent was told.
    t = json.loads((out / "transcripts" / "claude-opus-5-5--toolkernel--restart_recovery--0.json").read_text())
    results = [e["result_head"] for e in t["transcript"] if "tool" in e]
    assert results[1].startswith("[runtime] Restarted since your last call")
    s = summarize(meta, rows)
    assert "Plumbing check, not a benchmark" in markdown(s)


class FakeOpenAI:
    """Plays an OpenAI model that doesn't take reasoning_effort: one execute_code call, then the answer."""

    calls: list = []

    def __init__(self, **kw):
        from types import SimpleNamespace as NS

        self.chat = NS(completions=NS(create=self.create))

    async def create(self, **kw):
        import httpx
        import openai
        from types import SimpleNamespace as NS

        FakeOpenAI.calls.append(kw)
        if "reasoning_effort" in kw:
            req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
            raise openai.BadRequestError("Unsupported parameter: 'reasoning_effort'", body=None,
                                         response=httpx.Response(400, request=req))
        usage = NS(prompt_tokens=120, completion_tokens=30, prompt_tokens_details=NS(cached_tokens=20))
        last = kw["messages"][-1]
        if last["role"] == "user":
            code = ("from servers.files import read_table\n"
                    "read_table(path='crm/customers.csv', where={'customer_id': 'C01234'})['rows'][0]['email']")
            tc = NS(id="call_1", function=NS(name="execute_code", arguments=json.dumps({"code": code})))
            msg = NS(content=None, tool_calls=[tc], refusal=None)
            return NS(choices=[NS(message=msg, finish_reason="tool_calls")], usage=usage)
        import re

        email = re.search(r"[\w.+-]+@[\w.-]+", last["content"]).group(0)
        msg = NS(content=f"Found it.\nFINAL ANSWER: {email}", tool_calls=None, refusal=None)
        return NS(choices=[NS(message=msg, finish_reason="stop")], usage=usage)


async def test_openai_adapter_runs_a_task(tmp_path, data, monkeypatch):
    import openai

    monkeypatch.setattr(openai, "AsyncOpenAI", FakeOpenAI)
    FakeOpenAI.calls = []
    plan = Plan(data_dir=data, out_dir=tmp_path, configs=["toolkernel"], models=["some-openai-model"],
                tasks=get_tasks("lookup_email"), llm="openai")
    out = await run(plan, log=lambda *a: None)
    _, rows = load(out)
    assert rows[0]["success"], rows[0]["error"]
    assert rows[0]["api_calls"] == 2 and rows[0]["cache_read_tokens"] == 40
    # Tools went over in OpenAI's shape, and the tool result came back as a role=tool message.
    first, last = FakeOpenAI.calls[1], FakeOpenAI.calls[-1]
    assert first["tools"][0]["type"] == "function" and first["tools"][0]["function"]["name"] == "execute_code"
    assert "reasoning_effort" not in last and last["messages"][-1]["role"] == "tool"
    assert last["messages"][-2]["tool_calls"][0]["id"] == "call_1"
