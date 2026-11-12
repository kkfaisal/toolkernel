"""A deterministic GitHub-like MCP server with seeded data, for demos and tests."""

import random
from datetime import datetime, timedelta, timezone
from typing import Literal, TypedDict

from mcp.server import MCPServer
from mcp_types import ToolAnnotations

mcp = MCPServer("github", instructions="Mock GitHub. Repositories: acme/api, acme/web.")

_rng = random.Random(7)
_NOW = datetime(2026, 11, 11, tzinfo=timezone.utc)
_LABELS = ["bug", "feature", "docs", "perf", "security"]
_PEOPLE = ["ana", "bo", "chen", "dee", "eli", "fay"]


class Issue(TypedDict):
    number: int
    title: str
    state: Literal["open", "closed"]
    labels: list[str]
    assignee: str | None
    updated_at: str
    body: str


ISSUES: dict[str, list[Issue]] = {}
for repo in ("acme/api", "acme/web"):
    rows: list[Issue] = []
    for n in range(1, 141):
        labels = _rng.sample(_LABELS, _rng.choice([1, 1, 2]))
        rows.append(Issue(
            number=n,
            title=f"{_rng.choice(['Fix', 'Improve', 'Investigate', 'Add', 'Remove'])} "
                  f"{_rng.choice(['login', 'billing', 'search', 'export', 'cache', 'webhooks', 'rate limits'])} "
                  f"{_rng.choice(['timeout', 'flow', 'handling', 'endpoint', 'docs', 'regression'])}",
            state="open" if _rng.random() < 0.62 else "closed",
            labels=labels,
            assignee=_rng.choice(_PEOPLE + [None]),
            updated_at=(_NOW - timedelta(days=_rng.randint(0, 90), hours=_rng.randint(0, 23))).isoformat().replace("+00:00", "Z"),
            body=("Steps to reproduce:\n" + "\n".join(f"{i}. step {i}" for i in range(1, 6)) + "\n") * 6,
        ))
    ISSUES[repo] = rows


def _repo(owner: str, repo: str) -> list[Issue]:
    key = f"{owner}/{repo}"
    if key not in ISSUES:
        raise ValueError(f"repository {key} not found")
    return ISSUES[key]


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
def list_issues(owner: str, repo: str, state: Literal["open", "closed", "all"] = "open",
                labels: list[str] | None = None, per_page: int = 30) -> list[Issue]:
    """List issues in a GitHub repository.

    owner: Repository owner. repo: Repository name. state: Filter by state.
    labels: Only issues with all of these labels. per_page: Results per page (max 100).
    """
    rows = [i for i in _repo(owner, repo) if state == "all" or i["state"] == state]
    if labels:
        rows = [i for i in rows if all(lb in i["labels"] for lb in labels)]
    return rows[: max(1, min(per_page, 100))]


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
def get_issue(owner: str, repo: str, number: int) -> Issue:
    """Get one issue by number, including its full body."""
    for i in _repo(owner, repo):
        if i["number"] == number:
            return i
    raise ValueError(f"issue #{number} not found")


@mcp.tool(annotations=ToolAnnotations(destructive_hint=True))
def create_issue(owner: str, repo: str, title: str, body: str = "", labels: list[str] | None = None) -> Issue:
    """Create a new issue."""
    rows = _repo(owner, repo)
    issue = Issue(number=len(rows) + 1, title=title, state="open", labels=labels or [], assignee=None,
                  updated_at=_NOW.isoformat().replace("+00:00", "Z"), body=body)
    rows.append(issue)
    return issue


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True), structured_output=False)
def search_code(query: str, limit: int = 20) -> str:
    """Search code across repositories. Returns plain text matches (path:line: snippet)."""
    out = []
    for i in range(limit):
        out.append(f"src/{_rng.choice(['auth', 'billing', 'search'])}/mod{i}.py:{10 + i}: ...{query}...")
    return "\n".join(out)


if __name__ == "__main__":
    mcp.run()
