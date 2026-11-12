"""A read-only MCP server for local data files: Excel, CSV, JSON / JSON Lines and text.

    python -m toolkernel_gateway.mcp_servers.files --root ./bench-data

Every path is resolved inside --root; nothing outside it can be read. Tables support
column selection, simple equality filters and paging, like a well-built real server would.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import math
import os
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError  # the only exception whose message reaches the caller
from mcp_types import ToolAnnotations

ROOT = Path(os.environ.get("TOOLKERNEL_FILES_ROOT", ".")).resolve()
# Only empty cells are missing. pandas' default also turns text like "NA" (North America), "None" or "null"
# into NaN, which silently drops real rows from filters and totals.
NA = {"keep_default_na": False, "na_values": [""]}
TABLE_EXT = {".csv", ".tsv", ".xlsx", ".xlsm", ".xls", ".json", ".jsonl", ".ndjson", ".parquet"}
TEXT_EXT = {".txt", ".md", ".log", ".json", ".jsonl", ".csv", ".tsv", ".yaml", ".yml", ".html", ".xml"}
RO = ToolAnnotations(read_only_hint=True, idempotent_hint=True)

mcp = MCPServer(
    "files",
    instructions="Read-only access to a folder of data files (Excel, CSV, JSON, text). "
                 "Paths are relative to that folder. Use list_files to explore, describe_file for "
                 "columns and sheets, read_table for rows.",
)


def _resolve(path: str) -> Path:
    p = (ROOT / path).resolve()
    if p != ROOT and ROOT not in p.parents:
        raise ToolError(f"{path!r} is outside the data folder")
    if not p.exists():
        raise ToolError(f"{path!r} does not exist; list_files() shows what is there")
    return p


def _rel(p: Path) -> str:
    return str(p.relative_to(ROOT)) or "."


def _clean(v: Any) -> Any:
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if isinstance(v, (pd.Timestamp, datetime)):
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if hasattr(v, "item"):  # numpy scalars
        return v.item()
    return v


_CACHE: dict[tuple[str, str | None, float], pd.DataFrame] = {}
_LOAD_LOCK = threading.Lock()  # parse each file once, even when several calls arrive together


def _load(p: Path, sheet: str | None) -> pd.DataFrame:
    with _LOAD_LOCK:
        try:
            return _load_unlocked(p, sheet)
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001  give the caller something better than an empty message
            raise ToolError(f"could not read {_rel(p)}: {type(e).__name__}: {e}") from e


def _load_unlocked(p: Path, sheet: str | None) -> pd.DataFrame:
    key = (str(p), sheet, p.stat().st_mtime)
    if key in _CACHE:
        return _CACHE[key]
    ext = p.suffix.lower()
    if ext in (".csv", ".tsv"):
        df = pd.read_csv(p, sep="\t" if ext == ".tsv" else ",", **NA)
    elif ext in (".xlsx", ".xlsm", ".xls"):
        df = pd.read_excel(p, sheet_name=sheet if sheet is not None else 0, **NA)
    elif ext in (".jsonl", ".ndjson"):
        df = pd.read_json(p, lines=True)
    elif ext == ".json":
        data = json.loads(p.read_text())
        if isinstance(data, dict):
            data = next((v for v in data.values() if isinstance(v, list)), [data])
        df = pd.json_normalize(data)
    elif ext == ".parquet":
        df = pd.read_parquet(p)
    else:
        raise ToolError(f"{_rel(p)} is not a table ({', '.join(sorted(TABLE_EXT))})")
    _CACHE[key] = df
    return df


@mcp.tool(annotations=RO)
def list_files(path: str = ".", pattern: str = "*", recursive: bool = True) -> list[dict[str, Any]]:
    """List files under a folder. pattern is a glob on the file name, e.g. "*.xlsx"."""
    base = _resolve(path or ".")
    pattern = pattern or "*"  # an empty pattern means "everything", not "nothing"
    it = base.rglob("*") if recursive else base.glob("*")
    out = []
    for p in sorted(it):
        if p.is_file() and fnmatch.fnmatch(p.name, pattern) and not p.name.startswith("."):
            st = p.stat()
            out.append({"path": _rel(p), "size_bytes": st.st_size,
                        "modified": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                        "kind": "table" if p.suffix.lower() in TABLE_EXT else "text" if p.suffix.lower() in TEXT_EXT else "other"})
    return out


@mcp.tool(annotations=RO)
def describe_file(path: str) -> dict[str, Any]:
    """Describe a file: for tables, the sheets (Excel), row count, columns with types, and 3 sample rows."""
    p = _resolve(path)
    info: dict[str, Any] = {"path": _rel(p), "size_bytes": p.stat().st_size}
    ext = p.suffix.lower()
    if ext in (".xlsx", ".xlsm", ".xls"):
        sheets = pd.ExcelFile(p).sheet_names
        info["sheets"] = []
        for s in sheets:
            df = _load(p, s)
            info["sheets"].append({"name": s, "rows": len(df),
                                   "columns": {c: str(t) for c, t in df.dtypes.items()},
                                   "sample": [{k: _clean(v) for k, v in r.items()} for r in df.head(3).to_dict("records")]})
    elif ext in TABLE_EXT:
        df = _load(p, None)
        info.update(rows=len(df), columns={c: str(t) for c, t in df.dtypes.items()},
                    sample=[{k: _clean(v) for k, v in r.items()} for r in df.head(3).to_dict("records")])
    else:
        text = p.read_text(errors="replace")
        info.update(lines=text.count("\n") + 1, chars=len(text), preview=text[:300])
    return info


@mcp.tool(annotations=RO)
def read_table(path: str, sheet: str | None = None, columns: list[str] | None = None,
               where: dict[str, Any] | None = None, offset: int = 0, limit: int | None = None) -> dict[str, Any]:
    """Read rows from a table file (Excel sheet, CSV, JSON, JSON Lines, Parquet).

    sheet: Excel sheet name (default: the first sheet).
    columns: only return these columns.
    where: equality filters, e.g. {"region": "EMEA", "status": "open"}. A list value matches any of its items.
    offset, limit: paging. With no limit, every matching row is returned.
    Returns {"total_rows": matching row count, "rows": [...]}.
    """
    p = _resolve(path)
    df = _load(p, sheet)
    if where:
        for col, val in where.items():
            if col not in df.columns:
                raise ToolError(f"no column {col!r}; columns: {', '.join(map(str, df.columns))}")
            mask = df[col].isin(val) if isinstance(val, list) else df[col] == val
            df = df[mask]
    if columns:
        missing = [c for c in columns if c not in df.columns]
        if missing:
            raise ToolError(f"no column(s) {missing}; columns: {', '.join(map(str, df.columns))}")
        df = df[columns]
    total = len(df)
    df = df.iloc[max(0, offset):]
    if limit is not None:
        df = df.iloc[: max(0, limit)]
    rows = [{k: _clean(v) for k, v in r.items()} for r in df.to_dict("records")]
    return {"total_rows": total, "rows": rows}


@mcp.tool(annotations=RO)
def read_text(path: str, offset: int = 0, limit: int | None = None) -> str:
    """Read a text file (markdown, logs, JSON, …). offset/limit are in characters."""
    p = _resolve(path)
    text = p.read_text(errors="replace")
    return text[max(0, offset): None if limit is None else max(0, offset) + limit]


@mcp.tool(annotations=RO)
def search_text(query: str, path: str = ".", pattern: str = "*", max_results: int = 50) -> list[dict[str, Any]]:
    """Case-insensitive search for text inside files. Returns {path, line, text} matches."""
    base = _resolve(path or ".")
    pattern = pattern or "*"
    files = [base] if base.is_file() else sorted(p for p in base.rglob("*") if p.is_file())
    q = query.lower()
    out: list[dict[str, Any]] = []
    for p in files:
        if not fnmatch.fnmatch(p.name, pattern) or p.suffix.lower() not in TEXT_EXT:
            continue
        for i, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
            if q in line.lower():
                out.append({"path": _rel(p), "line": i, "text": line.strip()[:300]})
                if len(out) >= max_results:
                    return out
    return out


def main() -> None:
    global ROOT
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=os.environ.get("TOOLKERNEL_FILES_ROOT", "."))
    a = ap.parse_args()
    ROOT = Path(a.root).resolve()
    mcp.run()


if __name__ == "__main__":
    main()
