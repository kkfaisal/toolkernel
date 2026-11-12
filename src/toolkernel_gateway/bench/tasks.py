"""Benchmark tasks. Each task's correct answer is computed from the generated data, so scoring is automatic.

Categories: single-call lookup, large-result filtering, multi-source joins,
text search, exploratory multi-turn analysis, a forced restart mid-task, and web tasks.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

import pandas as pd

NA = {"keep_default_na": False, "na_values": [""]}  # "NA" is North America, not a missing value


@dataclass
class Task:
    id: str
    category: str
    title: str
    turns: list[str]
    truth: Callable[[Path], Any]
    check: Callable[[str, Any], bool]
    restart_before: set[int] = field(default_factory=set)  # turn indexes; the runtime is restarted first
    needs: set[str] = field(default_factory=set)  # "web" (internet), "search" (TAVILY_API_KEY or BRAVE_API_KEY)
    added: str = ""  # tasks written after ToolKernel was tuned against the original set ("2026-11-11")


# ---------------------------------------------------------------- answer checking


def final_answer(text: str) -> str:
    """The part after the last 'FINAL ANSWER:' (or the whole text if the agent forgot the marker)."""
    idx = text.upper().rfind("FINAL ANSWER")
    return text[idx + len("FINAL ANSWER"):].lstrip(" :*\n") if idx >= 0 else text


def numbers_in(text: str) -> list[float]:
    out = []
    for m in re.finditer(r"-?\$?\d[\d,]*(?:\.\d+)?\s*(?:[kKmM](?![a-zA-Z]))?", text):
        raw = m.group(0).replace("$", "").replace(",", "").strip()
        mult = 1.0
        if raw[-1:] in "kK":
            mult, raw = 1e3, raw[:-1]
        elif raw[-1:] in "mM":
            mult, raw = 1e6, raw[:-1]
        try:
            out.append(float(raw.strip()) * mult)
        except ValueError:
            pass
    return out


def has_number(text: str, expected: float, rel: float = 0.005, abs_tol: float = 1.0) -> bool:
    return any(abs(n - expected) <= max(abs_tol, abs(expected) * rel) for n in numbers_in(text))


def has_name(text: str, name: str) -> bool:
    """Whole-name match that doesn't accept a longer product name ('Storage Node' ≠ 'Storage Node Max')."""
    return re.search(rf"(?<![\w]){re.escape(name)}(?![\w]| (?:Plus|Max)\b| \d)", text, re.I) is not None


# ---------------------------------------------------------------- data access (ground truth)


@cache
def _orders(root: Path) -> pd.DataFrame:
    df = pd.read_excel(root / "sales" / "sales_2025.xlsx", sheet_name="Orders", **NA)
    df["month"] = pd.to_datetime(df["order_date"]).dt.month
    df["quarter"] = "Q" + ((df["month"] - 1) // 3 + 1).astype(str)
    return df


@cache
def _sheet(root: Path, name: str) -> pd.DataFrame:
    return pd.read_excel(root / "sales" / "sales_2025.xlsx", sheet_name=name, **NA)


@cache
def _customers(root: Path) -> pd.DataFrame:
    return pd.read_csv(root / "crm" / "customers.csv", **NA)


@cache
def _tickets(root: Path) -> pd.DataFrame:
    return pd.DataFrame([json.loads(line) for line in (root / "support" / "tickets.jsonl").read_text().splitlines()])


def t_email(root: Path) -> str:
    c = _customers(root)
    return c.loc[c.customer_id == "C01234", "email"].iloc[0]


def t_emea_q3(root: Path) -> float:
    o = _orders(root)
    return round(o[(o.region == "EMEA") & (o.quarter == "Q3")].revenue.sum(), 2)


def t_apac_category(root: Path) -> tuple[str, int]:
    o = _orders(root)
    s = o[o.region == "APAC"].groupby("category").units.sum().sort_values(ascending=False)
    return s.index[0], int(s.iloc[0])


def t_rep_vs_target(root: Path) -> tuple[str, float]:
    o, t = _orders(root), _sheet(root, "Targets")
    actual = o[o.quarter == "Q2"].groupby("sales_rep").revenue.sum()
    tq = t[t.quarter == "Q2"].set_index("sales_rep").target_revenue
    pct = ((actual / tq) - 1).sort_values(ascending=False) * 100
    return pct.index[0], round(float(pct.iloc[0]), 1)


def t_urgent_segment(root: Path) -> tuple[str, int]:
    t, c = _tickets(root), _customers(root)
    m = t[(t.status == "open") & (t.priority == "urgent")].merge(c, on="customer_id")
    s = m.segment.value_counts()
    return s.index[0], int(s.iloc[0])


def t_slowest_product(root: Path) -> str:
    t, p = _tickets(root), _sheet(root, "Products")
    closed = t[t.status == "closed"]
    g = closed.groupby("product_id").resolution_hours.agg(["count", "median"])
    g = g[g["count"] >= 20].sort_values("median", ascending=False)
    return p.set_index("product_id").name[g.index[0]]


def t_explore(root: Path) -> list[str]:
    o, c = _orders(root), _customers(root)
    region = o.region.value_counts().sort_values().index[0]
    sub = o[o.region == region]
    cat = sub.groupby("category").revenue.sum().sort_values(ascending=False).index[0]
    top = sub[sub.category == cat].groupby("customer_id").revenue.sum().sort_values(ascending=False).head(3)
    names = c.set_index("customer_id").name
    return [names[i] for i in top.index]


def t_top_customer_tickets(root: Path) -> int:
    o, t = _orders(root), _tickets(root)
    top5 = o.groupby("customer_id").revenue.sum().sort_values(ascending=False).index[:5]
    return int(t.customer_id.isin(top5).sum())


MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]


def t_best_month(root: Path) -> tuple[str, float]:
    m = _orders(root).groupby("month").revenue.sum().diff().sort_values(ascending=False)
    return MONTHS[int(m.index[0]) - 1], round(float(m.iloc[0]), 2)


def t_latam_online(root: Path) -> float:
    o = _orders(root)
    latam = o[o.region == "LATAM"]
    return round(100 * latam[latam.channel == "Online"].revenue.sum() / latam.revenue.sum(), 1)


def t_renewal_tickets(root: Path) -> tuple[int, str]:
    t, p = _tickets(root), _sheet(root, "Products")
    hits = t[t.body.str.contains("renewal", case=False)]
    top = hits.product_id.value_counts().index[0]
    return len(hits), p.set_index("product_id").name[top]


def t_online_avg(root: Path) -> float:
    o = _orders(root)
    return round(float(o[o.channel == "Online"].revenue.mean()), 2)


# ---------------------------------------------------------------- tasks

TASKS: list[Task] = [
    Task("lookup_email", "lookup", "Single lookup",
         ["What is the email address of customer C01234?"],
         t_email, lambda a, t: t.lower() in a.lower()),
    Task("emea_q3_revenue", "large-filter", "Filter 24k orders",
         ["What was the total revenue from orders in the EMEA region in Q3 2025 (July to September)? "
          "Use the sales workbook. Give the amount in dollars, rounded to the nearest dollar."],
         t_emea_q3, lambda a, t: has_number(a, t)),
    Task("apac_top_category", "large-filter", "Group and rank",
         ["In APAC, which product category sold the most units in 2025, and how many units was that?"],
         t_apac_category, lambda a, t: t[0].lower() in a.lower() and has_number(a, t[1], rel=0, abs_tol=0.5)),
    Task("rep_vs_target", "join", "Join two sheets",
         ["Which sales rep beat their Q2 2025 revenue target by the largest percentage? "
          "Give the rep's name and the percentage above target."],
         t_rep_vs_target, lambda a, t: has_name(a, t[0])),
    Task("urgent_by_segment", "join", "Join two files",
         ["Looking at support tickets that are still open with urgent priority: which customer segment has the "
          "most of them, and how many?"],
         t_urgent_segment, lambda a, t: t[0].lower() in a.lower() and has_number(a, t[1], rel=0, abs_tol=0.5)),
    Task("slowest_product", "join", "Aggregate tickets",
         ["Which product has the longest median resolution time for closed support tickets? Only consider "
          "products with at least 20 closed tickets. Give the product's name."],
         t_slowest_product, lambda a, t: has_name(a, t)),
    Task("lighthouse_date", "text-search", "Search the reports",
         ["According to the weekly reports, when is Project Lighthouse scheduled to launch?"],
         lambda root: "2026-03-18",
         lambda a, t: bool(re.search(r"march\s+18(th)?,?\s+2026|18\s+march\s+2026|2026-03-18", a, re.I))),
    Task("explore_region", "exploratory", "Three-step analysis",
         ["How many orders were placed in each region in 2025?",
          "For the region with the fewest orders, which product category brought in the most revenue?",
          "In that region and category, who are the top 3 customers by revenue? Give their company names."],
         t_explore, lambda a, t: all(has_name(a, n) for n in t)),
    Task("restart_recovery", "restart", "Recover after restart",
         ["Load all 2025 orders and tell me the average revenue per order.",
          "Now give me the same average, but only for orders from the Online channel."],
         t_online_avg, lambda a, t: has_number(a, t, rel=0.002, abs_tol=0.5), restart_before={1}),
    Task("web_python_year", "web", "Read a long web page",
         ["Using the web page https://en.wikipedia.org/wiki/Python_(programming_language), in which year was "
          "Python first released?"],
         lambda root: 1991, lambda a, t: "1991" in a, needs={"web"}),
    Task("web_search", "web", "Search the web",
         ["Search the web: who co-founded Brave Software (the company behind the Brave browser) and is its CEO?"],
         lambda root: "Brendan Eich", lambda a, t: "eich" in a.lower(), needs={"web", "search"}),
    # Written after the fixes that the tasks above surfaced, so they measure ToolKernel on work it wasn't tuned for.
    Task("top_customer_tickets", "join", "Top customers' tickets",
         ["Take the 5 customers who brought in the most revenue in 2025. How many support tickets have those 5 "
          "customers opened in total (any status)?"],
         t_top_customer_tickets,  # "the top 5 customers" mustn't count as the answer
         lambda a, t: has_number(re.sub(r"(?i)(top|those|the)\s+5\b|\b5\s+(customers|companies)", "", a), t,
                                 rel=0, abs_tol=0.5), added="2026-11-11"),
    Task("best_month", "large-filter", "Month-over-month change",
         ["Which month of 2025 had the largest increase in total revenue compared with the month before? "
          "Give the month and the size of the increase in dollars."],
         t_best_month, lambda a, t: t[0].lower() in a.lower() and has_number(a, t[1]), added="2026-11-11"),
    Task("latam_online_share", "large-filter", "Share of revenue",
         ["What percentage of LATAM's 2025 revenue came through the Online channel? Round to one decimal place."],
         t_latam_online, lambda a, t: has_number(a, t, rel=0, abs_tol=0.15), added="2026-11-11"),
    Task("renewal_tickets", "text-search", "Search ticket text",
         ["How many support tickets mention renewal in their text, and which product comes up most often among "
          "those tickets? Give the count and the product name."],
         t_renewal_tickets, lambda a, t: has_number(a, t[0], rel=0, abs_tol=0.5) and has_name(a, t[1]),
         added="2026-11-11"),
    Task("web_rust_sponsor", "web", "Search, then answer",
         ["Search the web: which organization originally sponsored the development of the Rust programming "
          "language?"],
         lambda root: "Mozilla", lambda a, t: "mozilla" in a.lower(), needs={"web", "search"}, added="2026-11-11"),
]


def get_tasks(selector: str = "all") -> list[Task]:
    if selector in ("all", ""):
        return list(TASKS)
    wanted = {s.strip() for s in selector.split(",")}
    picked = [t for t in TASKS if t.id in wanted or t.category in wanted]
    unknown = wanted - {t.id for t in picked} - {t.category for t in picked}
    if unknown:
        raise SystemExit(f"unknown task(s) or category: {', '.join(sorted(unknown))}. "
                         f"Known: {', '.join(t.id for t in TASKS)}")
    return picked
