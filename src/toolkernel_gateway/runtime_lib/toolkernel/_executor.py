"""A small notebook-style executor: persistent namespace, last-expression display,
top-level await, trimmed tracebacks and a `[state]` summary after every run."""

from __future__ import annotations

import ast
import asyncio
import builtins
import contextlib
import importlib
import inspect
import io
import linecache
import sys
import time
import traceback
import types
from pathlib import Path
from typing import Any

from ._helpers import Preview, _is_dataframe, preview

HIDDEN_NAMES = {"In", "Out", "exit", "quit", "get_ipython"}
STDOUT_CAP = 2_000_000
_THIS_FILE = __file__
_LIB_DIR = str(Path(__file__).parent)


class _Capture(io.TextIOBase):
    """Collects stdout/stderr up to a cap; counts what it had to drop."""

    def __init__(self, cap: int):
        self.parts: list[str] = []
        self.size = 0
        self.dropped = 0
        self.cap = cap

    def writable(self) -> bool:
        return True

    def write(self, s: str) -> int:
        room = self.cap - self.size
        if room > 0:
            self.parts.append(s[:room])
            self.size += min(len(s), room)
        self.dropped += max(0, len(s) - max(room, 0))
        return len(s)

    def text(self) -> str:
        return "".join(self.parts)


class Executor:
    def __init__(self, thresholds: dict[str, int] | None = None, state_items: int = 15, state_chars: int = 300):
        self.thresholds = {"list": 20, "dict": 50, "str": 2000, "dataframe_rows": 20, **(thresholds or {})}
        self.state_items, self.state_chars = state_items, state_chars
        self.ns: dict[str, Any] = {"__name__": "__main__", "__builtins__": builtins}
        exec("from toolkernel import *", self.ns)  # noqa: S102
        self.preloaded = {k: id(v) for k, v in self.ns.items()}
        self.recency: dict[str, int] = {}
        self.tick = 0
        self.cells = 0
        self.loop = asyncio.new_event_loop()
        self.server_stamps: dict[str, int] = {}
        self.executing = False

    # -- module freshness ------------------------------------------------------

    def _refresh_servers(self) -> None:
        """Pick up regenerated wrapper packages without a restart."""
        importlib.invalidate_caches()
        root = Path("servers")
        for name in [m for m in sys.modules if m.startswith("servers.") and m.count(".") == 1]:
            init = root / name.split(".", 1)[1] / "__init__.py"
            try:
                stamp = init.stat().st_mtime_ns
            except FileNotFoundError:
                stamp = -1
            if self.server_stamps.get(name) not in (None, stamp):
                for m in [m for m in sys.modules if m == name or m.startswith(name + ".")]:
                    del sys.modules[m]
            self.server_stamps[name] = stamp

    # -- run -------------------------------------------------------------------

    def run(self, code: str) -> dict[str, Any]:
        self.cells += 1
        fname = f"<cell-{self.cells}>"
        linecache.cache[fname] = (len(code), None, code.splitlines(True), fname)
        self._refresh_servers()
        before = {k: id(v) for k, v in self.ns.items()}
        cap = _Capture(STDOUT_CAP)
        result_text: str | None = None
        error: str | None = None
        started = time.perf_counter()
        self.executing = True
        try:
            with contextlib.redirect_stdout(cap), contextlib.redirect_stderr(cap):
                value = self._exec(code, fname)
            if value is not None:
                self.ns["_"] = value
                result_text = self._display(value)
        except KeyboardInterrupt:
            error = "KeyboardInterrupt: execution was interrupted (time limit reached). Variables set before this point are kept."
        except SystemExit as e:
            error = f"SystemExit({e.code}) is ignored here; the runtime keeps running."
        except BaseException as e:  # noqa: BLE001
            error = self._format_error(e)
        finally:
            self.executing = False
            for name in sys.modules:
                if name.startswith("servers.") and name.count(".") == 1 and name not in self.server_stamps:
                    init = Path("servers") / name.split(".", 1)[1] / "__init__.py"
                    self.server_stamps[name] = init.stat().st_mtime_ns if init.exists() else -1
        self._track(before)
        return {
            "stdout": cap.text(),
            "stdout_dropped": cap.dropped,
            "result": result_text,
            "error": error,
            "state": self.state_line(),
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }

    def _exec(self, code: str, fname: str) -> Any:
        flags = ast.PyCF_ALLOW_TOP_LEVEL_AWAIT
        tree = ast.parse(code, fname)
        last = tree.body.pop() if tree.body and isinstance(tree.body[-1], ast.Expr) else None
        if tree.body:
            self._maybe_await(eval(compile(tree, fname, "exec", flags=flags), self.ns))  # noqa: S307
        if last is None:
            return None
        expr = ast.Expression(body=last.value)
        ast.copy_location(expr, last)
        return self._maybe_await(eval(compile(expr, fname, "eval", flags=flags), self.ns))  # noqa: S307

    def _maybe_await(self, value: Any) -> Any:
        if inspect.iscoroutine(value):
            return self.loop.run_until_complete(value)
        return value

    # -- display -----------------------------------------------------------------

    def _display(self, value: Any) -> str:
        t = self.thresholds
        large = (
            (isinstance(value, (list, tuple, set, frozenset)) and len(value) > t["list"])
            or (isinstance(value, dict) and len(value) > t["dict"])
            or (isinstance(value, str) and not isinstance(value, Preview) and len(value) > t["str"])
            or (_is_dataframe(value) and value.shape[0] > t["dataframe_rows"])
        )
        if not large:
            try:
                text = value.to_string() if _is_dataframe(value) else repr(value)
            except Exception as e:  # noqa: BLE001
                text = f"<repr failed: {e}>"
            if len(text) <= t["str"] or isinstance(value, Preview):
                return text
        return ("(large value, showing a preview. The full value is in the Python variable `_`: use it in your next "
                "execute_code, e.g. `data = _`. It is not a read_output handle.)\n" + preview(value))

    def _format_error(self, e: BaseException) -> str:
        frames = [
            f for f in traceback.extract_tb(e.__traceback__)
            if f.filename != _THIS_FILE
            and not f.filename.startswith(_LIB_DIR)
            and "/servers/" not in f.filename
            and "/toolkernel/" not in f.filename
        ]
        head = "Traceback (most recent call last):\n" + "".join(traceback.format_list(frames)) if frames else ""
        return head + "".join(traceback.format_exception_only(type(e), e)).rstrip()

    # -- state ---------------------------------------------------------------------

    def _track(self, before: dict[str, int]) -> None:
        for name in list(self.recency):
            if name not in self.ns:
                del self.recency[name]
        for name, value in self.ns.items():
            if before.get(name) != id(value):
                self.tick += 1
                self.recency[name] = self.tick

    def _visible(self, name: str, value: Any) -> bool:
        if name.startswith("_") or name in HIDDEN_NAMES:
            return False
        if self.preloaded.get(name) == id(value):
            return False
        return not isinstance(value, (types.ModuleType, types.FunctionType, types.BuiltinFunctionType, type,
                                      types.MethodType))

    @staticmethod
    def describe(value: Any) -> str:
        tn = type(value).__name__
        if _is_dataframe(value):
            return f"DataFrame[{value.shape[0]}×{value.shape[1]}]"
        if tn == "ndarray" and hasattr(value, "shape"):
            return "ndarray[" + "×".join(map(str, value.shape)) + "]"
        if isinstance(value, (str, bytes, list, tuple, dict, set, frozenset)) or tn == "Series":
            try:
                return f"{tn}[{len(value)}]"
            except Exception:  # noqa: BLE001
                return tn
        return tn

    def state_items_list(self) -> list[tuple[str, str]]:
        names = sorted(
            (n for n, v in self.ns.items() if self._visible(n, v)),
            key=lambda n: -self.recency.get(n, 0),
        )
        return [(n, self.describe(self.ns[n])) for n in names]

    def state_line(self) -> str:
        items = self.state_items_list()
        if not items:
            return "[state] (no variables yet)"
        parts: list[str] = []
        length = len("[state] ")
        for i, (name, desc) in enumerate(items):
            piece = f"{name}: {desc}"
            more = len(items) - i
            if i >= self.state_items or length + len(piece) + 3 > self.state_chars - 12:
                parts.append(f"+{more} more")
                break
            parts.append(piece)
            length += len(piece) + 3
        return "[state] " + " · ".join(parts)
