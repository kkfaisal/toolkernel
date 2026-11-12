"""Helpers pre-imported into every runtime: preview, save/load, stash, parallel, search."""

from __future__ import annotations

import json
import os
import pickle
from collections.abc import Callable, Iterator, MutableMapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from ._bridge import _post

DATA_DIR = Path(os.environ.get("TOOLKERNEL_DATA_DIR", "saved"))


# ---------------------------------------------------------------- preview


class Preview(str):
    """A string that shows itself without quotes, so `preview(x)` reads well as a last expression."""

    def __repr__(self) -> str:
        return str(self)


def _short(x: Any, limit: int = 160) -> str:
    try:
        r = repr(x)
    except Exception as e:  # noqa: BLE001
        r = f"<unrepresentable {type(x).__name__}: {e}>"
    return r if len(r) <= limit else r[: limit - 1] + "…"


def _is_dataframe(x: Any) -> bool:
    return type(x).__name__ == "DataFrame" and hasattr(x, "shape") and hasattr(x, "dtypes")


def preview(x: Any, n: int = 3) -> Preview:
    """Summarize the structure of `x`: type, size, keys and a few samples. Never dumps it whole."""
    lines: list[str] = []
    if _is_dataframe(x):
        rows, cols = x.shape
        lines.append(f"DataFrame {rows} rows × {cols} columns")
        cols_desc = ", ".join(f"{c} ({t})" for c, t in list(x.dtypes.astype(str).items())[:20])
        lines.append(f"columns: {cols_desc}" + (f", +{cols - 20} more" if cols > 20 else ""))
        with _pandas_width():
            head = x.head(n).to_string(max_colwidth=40)
        lines.append(head)
    elif type(x).__name__ == "Series" and hasattr(x, "dtype"):
        lines.append(f"Series[{len(x)}] dtype={x.dtype}")
        lines.append(x.head(n).to_string())
    elif isinstance(x, (list, tuple, set, frozenset)):
        items = list(x)
        kinds = sorted({type(i).__name__ for i in items[:200]})
        lines.append(f"{type(x).__name__}[{len(items)}] of {' | '.join(kinds) or 'nothing'}")
        dicts = [i for i in items[:200] if isinstance(i, dict)]
        if dicts:
            keys: dict[str, None] = {}
            for d in dicts:
                keys.update(dict.fromkeys(map(str, d.keys())))
            ks = list(keys)
            lines.append("keys: " + ", ".join(ks[:25]) + (f", +{len(ks) - 25} more" if len(ks) > 25 else ""))
        for i, item in enumerate(items[:n]):
            lines.append(f"[{i}] {_short(item)}")
        if len(items) > n:
            lines.append(f"… {len(items) - n} more")
    elif isinstance(x, dict):
        ks = list(x.keys())
        lines.append(f"dict[{len(ks)} keys]")
        lines.append("keys: " + ", ".join(map(str, ks[:25])) + (f", +{len(ks) - 25} more" if len(ks) > 25 else ""))
        for k in ks[:n]:
            lines.append(f"{k!r}: {_short(x[k])}")
    elif isinstance(x, (str, bytes)):
        s = x
        lines.append(f"{type(x).__name__}[{len(s):,} chars]" if isinstance(x, str) else f"bytes[{len(s):,}]")
        if len(s) > 400:
            lines.append(_short(s[:240], 260) + " … " + _short(s[-120:], 140))
        else:
            lines.append(_short(s, 420))
    else:
        lines.append(f"{type(x).__name__}: {_short(x, 400)}")
    return Preview("\n".join(lines))


class _pandas_width:
    def __enter__(self):
        try:
            import pandas as pd

            self._ctx = pd.option_context("display.width", 160, "display.max_columns", 12)
            self._ctx.__enter__()
        except Exception:  # noqa: BLE001
            self._ctx = None

    def __exit__(self, *a):
        if self._ctx:
            self._ctx.__exit__(*a)


# ---------------------------------------------------------------- save / load


def save(x: Any, name: str) -> str:
    """Persist `x` to saved/<name>; it survives runtime restarts. Returns the path."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for old in DATA_DIR.glob(f"{name}.*"):
        old.unlink()
    if _is_dataframe(x):
        path = DATA_DIR / f"{name}.pkl"
        x.to_pickle(path)
    else:
        try:
            text = json.dumps(x)
            path = DATA_DIR / f"{name}.json"
            path.write_text(text)
        except (TypeError, ValueError):
            path = DATA_DIR / f"{name}.pkl"
            with open(path, "wb") as f:
                pickle.dump(x, f)
    return Preview(f"saved {name} → {path}")


def load(name: str) -> Any:
    """Load something stored with save(x, name)."""
    for ext in ("json", "pkl"):
        path = DATA_DIR / f"{name}.{ext}"
        if path.exists():
            if ext == "json":
                return json.loads(path.read_text())
            try:
                import pandas as pd

                return pd.read_pickle(path)
            except ImportError:
                with open(path, "rb") as f:
                    return pickle.load(f)
    saved = sorted(p.stem for p in DATA_DIR.glob("*.*")) if DATA_DIR.exists() else []
    raise FileNotFoundError(f"nothing saved as {name!r}. Saved: {', '.join(saved) or 'nothing yet'}")


# ---------------------------------------------------------------- stash


class _Stash(MutableMapping[str, Any]):
    """Small key-value store kept in the gateway. Survives restarts; values must be JSON."""

    def __getitem__(self, key: str) -> Any:
        res = _post("/bridge/stash", {"op": "get", "key": key})
        if not res.get("found"):
            raise KeyError(key)
        return res["value"]

    def __setitem__(self, key: str, value: Any) -> None:
        json.dumps(value)  # fail early, in the user's code
        _post("/bridge/stash", {"op": "set", "key": key, "value": value})

    def __delitem__(self, key: str) -> None:
        if not _post("/bridge/stash", {"op": "del", "key": key}).get("found"):
            raise KeyError(key)

    def keys_list(self) -> list[str]:
        return _post("/bridge/stash", {"op": "keys"})["keys"]

    def __iter__(self) -> Iterator[str]:
        return iter(self.keys_list())

    def __len__(self) -> int:
        return len(self.keys_list())

    def __repr__(self) -> str:
        return f"stash(keys={self.keys_list()})"


stash = _Stash()


# ---------------------------------------------------------------- parallel / search


def parallel(fn: Callable[..., Any], kwargs_list: list[dict[str, Any]], max_workers: int = 8,
             return_exceptions: bool = False) -> list[Any]:
    """Call fn(**kw) for each kw concurrently; results keep the input order.

    With return_exceptions=True, failed calls give their exception instead of raising.
    """
    def one(kw: dict[str, Any]) -> Any:
        try:
            return fn(**kw)
        except Exception as e:  # noqa: BLE001
            if return_exceptions:
                return e
            raise

    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, 32))) as pool:
        return list(pool.map(one, kwargs_list))


def search(query: str, server: str | None = None, detail: str = "summary", limit: int = 10) -> Preview:
    """Same as the search_tools meta-tool, from code."""
    res = _post("/bridge/search", {"query": query, "server": server, "detail": detail, "limit": limit})
    return Preview(res["text"])
