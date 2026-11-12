"""Seeded sample data for the benchmark: a small company's files.

    sales/sales_2025.xlsx       Orders (24,000 rows), Products, Targets
    crm/customers.csv           4,000 customers
    support/tickets.jsonl       9,000 support tickets
    reports/*.md                weekly notes (one of them holds a launch date)
    hr/headcount.xlsx           a small sheet nobody needs (noise)

Same seed, same data: every task has one known right answer.
"""

from __future__ import annotations

import json
import random
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

SEED = 7
REGIONS = {"EMEA": ["Germany", "France", "UK", "Netherlands", "Spain"], "NA": ["USA", "Canada"],
           "APAC": ["Japan", "Australia", "Singapore", "India"], "LATAM": ["Brazil", "Mexico", "Chile"]}
REGION_WEIGHTS = {"EMEA": 0.32, "NA": 0.36, "APAC": 0.21, "LATAM": 0.11}
CATEGORIES = {"Hardware": (400, 4000), "Software": (100, 2500), "Services": (500, 9000),
              "Accessories": (15, 250), "Training": (200, 1800)}
REPS = {"EMEA": ["Amara Okafor", "Lukas Becker", "Sofia Rossi"], "NA": ["Jordan Lee", "Priya Raman", "Marcus Hill"],
        "APAC": ["Kenji Watanabe", "Mei Lin"], "LATAM": ["Diego Alvarez", "Camila Souza"]}
SEGMENTS = [("Enterprise", 0.18), ("Mid-Market", 0.34), ("SMB", 0.48)]
WORDS_A = ["Blue", "North", "Bright", "Iron", "Silver", "Quantum", "Green", "Atlas", "Nimbus", "Cedar", "Harbor",
           "Summit", "Pioneer", "Vertex", "Lumen", "Orbit", "Granite", "Maple", "Coral", "Falcon"]
WORDS_B = ["Logistics", "Foods", "Health", "Systems", "Labs", "Partners", "Retail", "Energy", "Media", "Works",
           "Analytics", "Motors", "Capital", "Studios", "Pharma", "Networks"]
SUFFIX = ["Inc", "GmbH", "Ltd", "SA", "Co", "Group", "AG", "LLC"]
PRODUCT_NOUNS = {"Hardware": ["Edge Server", "Storage Node", "Gateway", "Rack Unit", "Sensor Hub"],
                 "Software": ["Analytics Suite", "Backup Pro", "Insight Cloud", "SecureVault", "FlowDesk"],
                 "Services": ["Onboarding Package", "Managed Ops", "Migration Service", "Premium Support"],
                 "Accessories": ["Cable Kit", "Mount Bracket", "Power Adapter", "Carry Case"],
                 "Training": ["Admin Course", "Developer Bootcamp", "Certification Exam"]}


def _choice_w(rng: random.Random, options: dict[str, float] | list[tuple[str, float]]) -> str:
    items = list(options.items()) if isinstance(options, dict) else options
    return rng.choices([k for k, _ in items], weights=[w for _, w in items])[0]


def generate(out: Path, seed: int = SEED) -> dict[str, int]:
    rng = random.Random(seed)
    out.mkdir(parents=True, exist_ok=True)
    for sub in ("sales", "crm", "support", "reports", "hr"):
        (out / sub).mkdir(exist_ok=True)

    # -- products
    products = []
    pid = 1
    for cat, nouns in PRODUCT_NOUNS.items():
        lo, hi = CATEGORIES[cat]
        for noun in nouns:
            for tier in ("", " Plus", " Max") if cat in ("Hardware", "Software") else ("", " Plus"):
                products.append({"product_id": f"P{pid:03d}", "name": f"{noun}{tier}", "category": cat,
                                 "list_price": round(rng.uniform(lo, hi), 2),
                                 "launch_date": (date(2021, 1, 1) + timedelta(days=rng.randint(0, 1400))).isoformat()})
                pid += 1
    prod_df = pd.DataFrame(products)

    # -- customers
    used: set[str] = set()
    customers = []
    for i in range(1, 4001):
        name = f"{rng.choice(WORDS_A)} {rng.choice(WORDS_B)} {rng.choice(SUFFIX)}"
        if name in used:  # 2,560 base names for 4,000 customers: number the repeats, like real registries
            k = 2
            while f"{name} {k}" in used:
                k += 1
            name = f"{name} {k}"
        used.add(name)
        region = _choice_w(rng, REGION_WEIGHTS)
        slug = name.split()[0].lower() + name.split()[1].lower()
        customers.append({"customer_id": f"C{i:05d}", "name": name, "segment": _choice_w(rng, SEGMENTS),
                          "region": region, "country": rng.choice(REGIONS[region]),
                          "signup_date": (date(2019, 1, 1) + timedelta(days=rng.randint(0, 2100))).isoformat(),
                          "account_manager": rng.choice(REPS[region]),
                          "email": f"ops@{slug}{i % 97}.example.com"})
    cust_df = pd.DataFrame(customers)
    by_region: dict[str, list[dict]] = {r: [c for c in customers if c["region"] == r] for r in REGIONS}

    # -- orders
    orders = []
    start = date(2025, 1, 1)
    for n in range(24000):
        region = _choice_w(rng, REGION_WEIGHTS)
        cust = rng.choice(by_region[region])
        prod = rng.choice(products)
        units = rng.choice([1, 1, 1, 2, 2, 3, 4, 5, 8, 10, 20]) if prod["category"] != "Accessories" else rng.randint(5, 60)
        discount = rng.choice([0, 0, 0, 0.05, 0.1, 0.15, 0.2])
        d = start + timedelta(days=rng.randint(0, 364))
        orders.append({"order_id": f"O{100001 + n}", "order_date": d.isoformat(), "region": region,
                       "country": cust["country"], "customer_id": cust["customer_id"],
                       "product_id": prod["product_id"], "category": prod["category"], "units": units,
                       "unit_price": prod["list_price"], "discount": discount,
                       "revenue": round(units * prod["list_price"] * (1 - discount), 2),
                       "sales_rep": cust["account_manager"],
                       "channel": rng.choice(["Online", "Online", "Partner", "Direct"])})
    ord_df = pd.DataFrame(orders).sort_values(["order_date", "order_id"]).reset_index(drop=True)

    # -- targets: per rep per quarter, around their actual revenue so some beat it and some miss
    # (targets are computed from the in-memory frame, so "NA" is never mistaken for a missing value here)
    ord_df["_q"] = "Q" + ((pd.to_datetime(ord_df["order_date"]).dt.month - 1) // 3 + 1).astype(str)
    actual = ord_df.groupby(["sales_rep", "_q"])["revenue"].sum()
    targets = []
    for region, reps in REPS.items():
        for rep in reps:
            for q in ("Q1", "Q2", "Q3", "Q4"):
                targets.append({"region": region, "sales_rep": rep, "quarter": q,
                                "target_revenue": round(actual[(rep, q)] * rng.uniform(0.8, 1.2), -3)})
    ord_df = ord_df.drop(columns="_q")
    with pd.ExcelWriter(out / "sales" / "sales_2025.xlsx") as xw:
        ord_df.to_excel(xw, sheet_name="Orders", index=False)
        prod_df.to_excel(xw, sheet_name="Products", index=False)
        pd.DataFrame(targets).to_excel(xw, sheet_name="Targets", index=False)
    cust_df.to_csv(out / "crm" / "customers.csv", index=False)

    # -- support tickets
    issues = ["cannot log in", "sync is slow", "license key rejected", "dashboard shows wrong totals",
              "device not detected", "export fails", "billing question", "upgrade failed", "API timeout",
              "certificate expired", "missing data after migration", "training access"]
    with open(out / "support" / "tickets.jsonl", "w") as f:
        for i in range(1, 9001):
            cust = rng.choice(customers)
            prod = rng.choice(products)
            created = datetime(2025, 1, 1) + timedelta(minutes=rng.randint(0, 525_000))
            status = rng.choices(["closed", "open", "pending"], weights=[0.72, 0.18, 0.10])[0]
            weights = {"Enterprise": [0.15, 0.35, 0.3, 0.2], "Mid-Market": [0.25, 0.4, 0.25, 0.1], "SMB": [0.35, 0.4, 0.18, 0.07]}
            priority = rng.choices(["low", "normal", "high", "urgent"], weights=weights[cust["segment"]])[0]
            issue = rng.choice(issues)
            # some products are genuinely slower to fix
            base = {"Hardware": 40, "Software": 18, "Services": 30, "Accessories": 10, "Training": 8}[prod["category"]]
            slow = 2.2 if prod["name"] in ("Storage Node Max", "SecureVault Plus") else 1.0
            hours = round(rng.lognormvariate(0, 0.6) * base * slow, 1) if status == "closed" else None
            body = (f"Customer reports that {prod['name']} {issue}. " +
                    " ".join(rng.choice(["Steps were attempted twice.", "Happens after the latest update.",
                                         "Several users affected.", "Workaround: restart the service.",
                                         "Logs attached in the portal.", "Customer is waiting on a fix before renewal."])
                             for _ in range(rng.randint(2, 6))))
            f.write(json.dumps({"ticket_id": f"T{i:05d}", "customer_id": cust["customer_id"],
                                "created_at": created.isoformat(timespec="minutes"), "priority": priority,
                                "status": status, "product_id": prod["product_id"],
                                "subject": f"{prod['name']}: {issue}", "body": body,
                                "resolution_hours": hours}) + "\n")

    # -- reports
    topics = ["Pipeline review", "Hiring update", "Infra costs", "Partner channel", "Support backlog",
              "Pricing experiments", "Security review", "Roadmap sync"]
    for week in range(1, 31):
        d = date(2025, 6, 2) + timedelta(weeks=week)
        lines = [f"# Weekly update {d.isoformat()}", ""]
        for t in rng.sample(topics, 4):
            lines += [f"## {t}", "", " ".join(rng.choice([
                "Numbers are in line with plan.", "We saw a small dip in EMEA.", "APAC continues to grow.",
                "Two deals slipped to next quarter.", "The team is focused on renewals.",
                "No major incidents this week.", "Costs came in under budget.", "We need more partner training."])
                for _ in range(4)), ""]
        if week == 9:
            lines += ["## Project Lighthouse", "",
                      "Codename for the new analytics bundle. Scope is still being discussed; no date yet.", ""]
        if week == 23:
            lines += ["## Project Lighthouse", "",
                      "Leadership approved the plan. Project Lighthouse is scheduled to launch on March 18, 2026, "
                      "starting with Enterprise customers in EMEA and NA.", ""]
        (out / "reports" / f"weekly-{d.isoformat()}.md").write_text("\n".join(lines))

    # -- noise
    hr = pd.DataFrame([{"team": t, "headcount": rng.randint(4, 40), "open_roles": rng.randint(0, 6)}
                       for t in ["Sales", "Support", "Engineering", "Marketing", "Finance", "People"]])
    hr.to_excel(out / "hr" / "headcount.xlsx", index=False)
    (out / "README.md").write_text(
        "# Company data\n\n- sales/sales_2025.xlsx: orders, products and quarterly targets for 2025\n"
        "- crm/customers.csv: customer accounts\n- support/tickets.jsonl: support tickets\n"
        "- reports/: weekly management notes\n- hr/headcount.xlsx: team sizes\n")
    return {"orders": len(ord_df), "customers": len(cust_df), "tickets": 9000, "products": len(prod_df)}
