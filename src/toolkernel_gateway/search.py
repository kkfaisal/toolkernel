"""Tool search (spec §9.9): BM25 over names, descriptions and parameter names, with exact-name boosting."""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

_STOP = {"the", "a", "an", "of", "to", "in", "for", "and", "or", "on", "by", "with", "is", "it", "this", "that", "from"}


def tokenize(text: str) -> list[str]:
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text or "")
    words = re.findall(r"[a-z0-9]+", text.lower())
    out = []
    for w in words:
        if w in _STOP:
            continue
        out.append(w)
        if len(w) > 4 and w.endswith("s"):  # crude plural folding: issues → issue
            out.append(w[:-1])
    return out


class ToolSearch:
    k1, b = 1.4, 0.75

    def __init__(self) -> None:
        self.docs: list[tuple[str, str, Counter[str], int]] = []
        self.df: Counter[str] = Counter()
        self.avg_len = 1.0

    def build(self, index: dict[str, Any]) -> None:
        self.docs, self.df = [], Counter()
        for spy, s in index.get("servers", {}).items():
            for tpy, meta in s["tools"].items():
                tokens = (
                    tokenize(tpy) * 3
                    + tokenize(meta["name"])
                    + tokenize(spy) * 2
                    + tokenize(meta.get("description", ""))
                    + tokenize(" ".join(meta.get("params", {})))
                )
                tf = Counter(tokens)
                self.docs.append((spy, tpy, tf, len(tokens)))
                self.df.update(tf.keys())
        self.avg_len = sum(d[3] for d in self.docs) / max(1, len(self.docs))

    def search(self, query: str, server: str | None = None, limit: int = 10) -> list[tuple[str, str, float]]:
        q = tokenize(query)
        n = len(self.docs)
        qnorm = query.strip().lower().replace(" ", "_")
        results = []
        for spy, tpy, tf, length in self.docs:
            if server and server not in (spy,):
                continue
            score = 0.0
            for term in set(q):
                f = tf.get(term, 0)
                if not f:
                    continue
                idf = math.log(1 + (n - self.df[term] + 0.5) / (self.df[term] + 0.5))
                score += idf * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * length / self.avg_len))
            if qnorm in (tpy, f"{spy}.{tpy}"):
                score += 50
            elif qnorm and qnorm in tpy:
                score += 5
            if score > 0:
                results.append((spy, tpy, score))
        results.sort(key=lambda r: (-r[2], r[0], r[1]))
        return results[:limit]


def format_results(results: list[tuple[str, str, float]], index: dict[str, Any], detail: str,
                   sources: dict[tuple[str, str], str] | None = None) -> str:
    if not results:
        return "No matching tools. Try other words, or list_servers() to see what is connected."
    servers = index["servers"]
    if detail == "names":
        return ", ".join(f"{s}.{t}" for s, t, _ in results)
    lines = []
    for s, t, _ in results:
        meta = servers[s]["tools"][t]
        if detail == "full" and sources is not None:
            lines.append(sources.get((s, t), "") or f"{s}.{meta['signature']}")
        else:
            lines.append(f"{s}.{meta['signature']}: {meta['summary'] or '(no description)'}")
    tail = "\nUse: from servers.<server> import <tool>. Full signature: describe_tool(\"server.tool\")."
    return ("\n\n" if detail == "full" else "\n").join(lines) + ("" if detail == "full" else tail)
