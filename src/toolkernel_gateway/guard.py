"""Output guard (spec §12): everything returned to the model passes through here.

Budget, repeated-line collapse, head+tail truncation, spill to file + read_output paging.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from pathlib import Path


def human_bytes(n: int | float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def collapse_repeats(text: str, min_run: int = 3) -> str:
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        j = i
        while j + 1 < len(lines) and lines[j + 1] == lines[i]:
            j += 1
        run = j - i + 1
        if run >= min_run and lines[i].strip():
            out.append(f"{lines[i]} (×{run})")
        else:
            out.extend(lines[i : j + 1])
        i = j + 1
    return "\n".join(out)


def _snap_head(text: str, n: int) -> str:
    cut = text.rfind("\n", max(0, n - 80), n)
    return text[: cut if cut > 0 else n]


def _snap_tail(text: str, n: int) -> str:
    start = len(text) - n
    nl = text.find("\n", start, start + 80)
    return text[(nl + 1) if nl >= 0 else start :]


@dataclass
class Guarded:
    text: str
    handle: str | None
    full_chars: int


class OutputGuard:
    def __init__(self, budget: int = 4000, head_ratio: float = 0.7):
        self.budget = budget
        self.head_ratio = head_ratio

    def _truncate(self, body: str, alloc: int, handle: str) -> str:
        if len(body) <= alloc:
            return body
        omitted_guess = len(body) - alloc
        marker = f"\n… [{omitted_guess:,} chars omitted · full output: read_output(\"{handle}\")] …\n"
        keep = max(120, alloc - len(marker))
        head = _snap_head(body, int(keep * self.head_ratio))
        tail = _snap_tail(body, keep - len(head))
        omitted = len(body) - len(head) - len(tail)
        marker = f"\n… [{omitted:,} chars omitted · full output: read_output(\"{handle}\")] …\n"
        return head.rstrip("\n") + marker + tail.lstrip("\n")

    def render(self, sections: list[tuple[str, str]], spill_dir: Path, *, notice: str | None = None,
               state: str | None = None, reserve: int = 200) -> Guarded:
        """sections: [(label, body)] e.g. [("stdout", …), ("result", …), ("error", …)]. Empty bodies are dropped."""
        sections = [(label, body) for label, body in sections if body]
        full = "\n".join(f"[{label}]\n{body}" for label, body in sections)
        bodies = [collapse_repeats(body) for _, body in sections]
        fixed = len(notice or "") + len(state or "") + reserve + sum(len(label) + 4 for label, _ in sections)
        avail = max(400, self.budget - fixed)
        handle = None
        if sum(map(len, bodies)) > avail:
            handle = "o" + secrets.token_hex(3)
            spill_dir.mkdir(parents=True, exist_ok=True)
            (spill_dir / f"{handle}.txt").write_text(full)
            order = sorted(range(len(bodies)), key=lambda i: len(bodies[i]))
            remaining, allocs = avail, [0] * len(bodies)
            for pos, i in enumerate(order):
                share = remaining // (len(bodies) - pos)
                allocs[i] = min(len(bodies[i]), share)
                remaining -= allocs[i]
            bodies = [self._truncate(b, a, handle) for b, a in zip(bodies, allocs)]
        parts = []
        if notice:
            parts.append(notice)
        parts += [f"[{label}]\n{body}" for (label, _), body in zip(sections, bodies)]
        if state:
            parts.append(state)
        return Guarded("\n".join(parts), handle, len(full))

    def page(self, spill_dir: Path, handle: str, offset: int = 0, limit: int | None = None) -> str:
        handle = handle.strip().strip('"').removesuffix(".txt")
        path = spill_dir / f"{handle}.txt"
        if not handle.isalnum() or not path.exists():
            known = sorted(p.stem for p in spill_dir.glob("*.txt")) if spill_dir.exists() else []
            return (f"No saved output named {handle!r}. Saved outputs: {', '.join(known[-10:]) or 'none'}. "
                    "(Names like `_last` are Python variables in the runtime: use them in execute_code.)")
        text = path.read_text()
        limit = min(limit or (self.budget - 200), self.budget - 200)
        offset = max(0, offset)
        chunk = text[offset : offset + limit]
        end = offset + len(chunk)
        footer = f"[read_output] chars {offset:,}–{end:,} of {len(text):,}"
        if end < len(text):
            footer += f" · next: read_output(\"{handle}\", offset={end})"
        return chunk + "\n" + footer
