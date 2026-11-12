"""Unit tests: generator, output guard, static check, executor/state line, policy, naming."""

import ast
import json
import os
import sys
from pathlib import Path

import pytest

from toolkernel_gateway.generator import Generator, ServerInput, build_server
from toolkernel_gateway.guard import OutputGuard, collapse_repeats
from toolkernel_gateway.naming import dedupe, py_name
from toolkernel_gateway.policy import PolicyEngine
from toolkernel_gateway.search import ToolSearch
from toolkernel_gateway.static_check import check

sys.path.insert(0, str(Path(__file__).parents[1] / "src" / "toolkernel_gateway" / "runtime_lib"))

TOOLS = [
    {
        "name": "list-issues",
        "description": "List issues in a repository.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Repository owner."},
                "state": {"type": "string", "enum": ["open", "closed"], "default": "open"},
                "from": {"type": "string"},
                "labels": {"type": "array", "items": {"type": "string"}},
                "filter": {"$ref": "#/$defs/Filter"},
            },
            "required": ["owner"],
            "$defs": {"Filter": {"type": "object", "properties": {"since": {"type": "string"}, "per-page": {"type": "integer"}}}},
        },
        "outputSchema": {"type": "object", "title": "list_issuesOutput", "required": ["result"],
                         "properties": {"result": {"type": "array", "items": {"type": "object", "properties": {
                             "number": {"type": "integer"}, "title": {"type": "string"}}, "required": ["number", "title"]}}}},
        "annotations": {"readOnlyHint": True},
    },
    {"name": "Delete", "description": "Delete things.", "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"destructiveHint": True}},
    {"name": "class", "description": "Anything goes.", "inputSchema": {"type": "object", "additionalProperties": True}},
]


def server() -> ServerInput:
    return ServerInput(name="Git-Hub", title=None, description="A code host.", instructions="Be nice.", tools=TOOLS)


# ---------------------------------------------------------------- naming


def test_naming_rules():
    assert py_name("list-issues") == "list_issues"
    assert py_name("getUserById") == "get_user_by_id"
    assert py_name("2fa") == "s_2fa"
    assert py_name("class") == "class_"
    assert py_name("list") == "list_"  # builtin clash
    assert dedupe(["a", "a", "a"]) == ["a", "a_2", "a_3"]


# ---------------------------------------------------------------- generator


def test_generator_is_deterministic():
    files1, idx1 = build_server(server(), "git_hub")
    files2, idx2 = build_server(ServerInput(**{**server().__dict__, "tools": list(reversed(TOOLS))}), "git_hub")
    assert files1 == files2
    assert idx1["hash"] == idx2["hash"]


def test_generated_code_compiles_and_reads_well():
    files, idx = build_server(server(), "git_hub")
    for name, src in files.items():
        if name.endswith(".py"):
            ast.parse(src)
    src = files["list_issues.py"]
    assert "*," in src and "owner: str," in src
    assert 'state: Literal["open", "closed"] | None = None' in src
    assert "from_: str | None = None" in src  # keyword param renamed
    assert '"from": from_' in src  # but sent under its original name
    assert 'call_tool("Git-Hub", "list-issues"' in src  # original server/tool names on the wire
    assert "-> list[ListIssuesResultItem]" in src  # wrapped output unwrapped to the inner type
    assert "Annotations: read-only" in src
    assert "Filter" in files["_types.py"] and '"per-page"' in files["_types.py"]
    meta = idx["tools"]["list_issues"]
    assert meta["unwrap_result"] is True
    assert meta["signature"] == "list_issues(owner, state?, from_?, labels?, filter?)"
    assert "def class_(**arguments: Any) -> Any:" in files["class_.py"]


def test_generator_writes_package(tmp_path):
    gen = Generator(tmp_path)
    py, changed = gen.generate(server())
    assert py == "git_hub" and changed
    _, changed_again = gen.generate(server())
    assert not changed_again
    assert (tmp_path / "servers" / "git_hub" / "__init__.py").exists()
    assert (tmp_path / "toolkernel" / "_bridge.py").exists()
    assert "git_hub" in (tmp_path / "README.md").read_text()
    assert json.loads((tmp_path / "_index.json").read_text())["servers"]["git_hub"]["name"] == "Git-Hub"
    assert gen.tool_entry("Git-Hub.list-issues")[1] == "list_issues"
    assert gen.tool_entry("git_hub.list_issues")[1] == "list_issues"


def test_search_ranks_by_relevance(tmp_path):
    gen = Generator(tmp_path)
    gen.generate(server())
    s = ToolSearch()
    s.build(gen.index)
    hits = s.search("issues")
    assert hits[0][1] == "list_issues"
    assert s.search("delete")[0][1] == "delete"


# ---------------------------------------------------------------- guard


def test_guard_budget_and_spill(tmp_path):
    g = OutputGuard(budget=1000)
    big = "\n".join(f"row {i}: " + "x" * 40 for i in range(2000))
    out = g.render([("stdout", big), ("error", "Boom at the end")], tmp_path, state="[state] a: int")
    assert len(out.text) <= 1000
    assert out.handle and (tmp_path / f"{out.handle}.txt").read_text().count("row ") == 2000
    assert "row 0:" in out.text and "row 1999:" in out.text  # head and tail survive
    assert "Boom at the end" in out.text and out.text.endswith("[state] a: int")
    page = g.page(tmp_path, out.handle, 0, 500)
    assert page.startswith("[stdout]\nrow 0") and "next: read_output" in page


def test_guard_small_output_untouched(tmp_path):
    out = OutputGuard(4000).render([("result", "(87, 12)")], tmp_path)
    assert out.text == "[result]\n(87, 12)" and out.handle is None


def test_collapse_repeats():
    assert collapse_repeats("a\nb\nb\nb\nc") == "a\nb (×3)\nc"
    assert collapse_repeats("a\na") == "a\na"


# ---------------------------------------------------------------- static check


def index_for(tmp_path):
    gen = Generator(tmp_path)
    gen.generate(server())
    return gen.index


def test_static_check(tmp_path):
    idx = index_for(tmp_path)
    assert check("x = (", idx).startswith("SyntaxError")
    assert "Did you mean: list_issues" in check("from servers.git_hub import list_isues", idx)
    assert "No server named 'gitlab'" in check("from servers.gitlab import x", idx)
    assert "no argument 'statee'" in check("from servers.git_hub import list_issues\nlist_issues(owner='a', statee='open')", idx)
    assert "keyword arguments only" in check("from servers.git_hub import list_issues\nlist_issues('a')", idx)
    assert check("from servers.git_hub import list_issues\nlist_issues(owner='a', state='open')", idx) is None
    assert check("from servers.git_hub import class_\nclass_(anything=1)", idx) is None


def test_static_check_remembers_earlier_imports(tmp_path):
    idx = index_for(tmp_path)
    known: dict = {}
    assert check("from servers.git_hub import list_issues", idx, known) is None
    assert "no argument 'status'" in check("list_issues(owner='a', status='x')", idx, known)


# ---------------------------------------------------------------- executor


@pytest.fixture
def executor(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TOOLKERNEL_DATA_DIR", str(tmp_path / "data"))
    from toolkernel._executor import Executor

    return Executor()


def test_executor_state_and_display(executor):
    r = executor.run("issues = list(range(87))\nstale = issues[:12]\nlen(stale)")
    assert r["result"] == "12" and r["error"] is None
    assert r["state"] == "[state] stale: list[12] · issues: list[87]"
    r = executor.run("issues")
    assert r["result"].startswith("(large value, showing a preview") and "list[87] of int" in r["result"]
    r = executor.run("import json\ndef f(): pass\n_hidden = 1\nx = 'hi'")
    assert "json" not in r["state"] and "f:" not in r["state"] and "_hidden" not in r["state"]
    assert r["state"].startswith("[state] x: str[2]")


def test_executor_errors_and_await(executor):
    r = executor.run("for i in range(3):\n    print(i)\n1/0")
    assert r["stdout"] == "0\n1\n2\n"
    assert "ZeroDivisionError" in r["error"] and "<cell-1>" in r["error"] and "_executor" not in r["error"]
    r = executor.run("import asyncio\nawait asyncio.sleep(0)\n41 + 1")
    assert r["result"] == "42"
    r = executor.run("raise SystemExit(3)")
    assert "ignored" in r["error"]


def test_preview_dataframe(executor):
    r = executor.run("import pandas as pd\ndf = pd.DataFrame({'a': range(100), 'b': ['x'] * 100})\ndf")
    assert "DataFrame 100 rows × 2 columns" in r["result"]
    assert "df: DataFrame[100×2]" in r["state"]


# ---------------------------------------------------------------- policy


def test_policy():
    p = PolicyEngine({"default": "allow", "rules": [
        {"match": "github.delete_*", "action": "deny"},
        {"match": "*", "when": {"annotation": "destructiveHint"}, "action": "approve"},
        {"match": "crm.*", "rate_limit": {"calls": 2, "per": "minute"}},
        {"match": "warehouse.query", "callers": ["code"]},
    ]})
    assert p.evaluate("github", "delete_repo", {}).action == "deny"
    assert p.evaluate("slack", "post", {"destructiveHint": True}).action == "approve"
    assert p.evaluate("slack", "list", {"readOnlyHint": True}).action == "allow"
    assert [p.evaluate("crm", "x", {}, session_id="s").action for _ in range(3)] == ["allow", "allow", "deny"]
    assert p.evaluate("warehouse", "query", {}, caller="direct").action == "deny"
    assert p.evaluate("warehouse", "query", {}, caller="code").action == "allow"
