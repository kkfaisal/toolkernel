"""Policy engine (spec §13.1): allow / deny / approve, annotation conditions, caller restrictions, rate limits."""

from __future__ import annotations

import fnmatch
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any

_PER = {"second": 1, "minute": 60, "hour": 3600}


@dataclass
class Decision:
    action: str  # allow | deny | approve
    reason: str = ""
    rule: int | None = None


class PolicyEngine:
    def __init__(self, cfg: dict[str, Any]):
        self.default = cfg.get("default", "allow")
        self.rules: list[dict[str, Any]] = list(cfg.get("rules") or [])
        self._hits: dict[tuple[int, str], deque[float]] = defaultdict(deque)

    @staticmethod
    def _matches(rule: dict[str, Any], names: list[str], annotations: dict[str, Any], caller: str) -> bool:
        pattern = rule.get("match", "*")
        if not any(fnmatch.fnmatchcase(n, pattern) for n in names):
            return False
        when = rule.get("when") or {}
        ann = when.get("annotation")
        if ann and not annotations.get(ann):
            return False
        return True

    def evaluate(self, server: str, tool: str, annotations: dict[str, Any], *, caller: str = "code",
                 session_id: str = "", aliases: tuple[str, str] | None = None) -> Decision:
        names = [f"{server}.{tool}"]
        if aliases:
            names.append(f"{aliases[0]}.{aliases[1]}")
        decision: Decision | None = None
        now = time.monotonic()
        for i, rule in enumerate(self.rules):
            if not self._matches(rule, names, annotations, caller):
                continue
            callers = rule.get("callers")
            if callers and caller not in callers:
                return Decision("deny", f"{names[0]} can only be called from {' or '.join(callers)}", i)
            rl = rule.get("rate_limit")
            if rl:
                window = _PER.get(rl.get("per", "minute"), 60)
                q = self._hits[(i, session_id)]
                while q and now - q[0] > window:
                    q.popleft()
                if len(q) >= int(rl.get("calls", 60)):
                    return Decision("deny", f"rate limit: {rl.get('calls')} calls per {rl.get('per', 'minute')}", i)
                q.append(now)
            if decision is None and rule.get("action"):
                decision = Decision(rule["action"], f"rule {i + 1} ({rule.get('match', '*')})", i)
        return decision or Decision(self.default, "default")
