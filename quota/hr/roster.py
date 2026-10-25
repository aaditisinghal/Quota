"""`python -m quota export-roster --world acme+fernhill --seed 7 --out out/hr_roster.json` (CUA_TASKS C1).

The HR system is the company's source of truth for people and base pay, so the BambooHR clone is seeded with
Quota's cast: the 20 Fernhill reps (Jordan, Alex, Riley and Taylor among them), the Sales manager, VP Sales Ops,
Controller, Payroll manager and CFO, and four outside partners and agents. Each person carries the rows HR keeps:
compensation, employment status, a few bonuses, and the Q1 2026 commissions HR recorded. Those are copied from
`payouts_2026Q1.csv`, so the three spreadsheet errors (R6) are in HR too.
"""

import csv
import hashlib
import json
from pathlib import Path

from quota.intake import formats

STAFF = [   # (id, first, last, title, department, reports to, hire date, state)
    ("morgan.diaz", "Morgan", "Diaz", "Sales Manager", "Sales", "marcus.lee", "2022-09-12", "CA"),
    ("marcus.lee", "Marcus", "Lee", "VP Sales Ops", "Sales Operations", None, "2021-03-01", "CA"),
    ("priya.nair", "Priya", "Nair", "Controller", "Finance", "grace.liu", "2021-06-14", "CA"),
    ("dana.ortiz", "Dana", "Ortiz", "Payroll Manager", "Finance", "priya.nair", "2022-01-10", "CA"),
    ("grace.liu", "Grace", "Liu", "Chief Financial Officer", "Finance", None, "2020-11-02", "CA"),
]
STAFF_PAY = {"morgan.diaz": 165_000, "marcus.lee": 210_000, "priya.nair": 190_000, "dana.ortiz": 115_000,
             "grace.liu": 260_000}
PARTNERS = [   # (id, first, last, title, email)
    ("northstar.partners", "Northstar", "Partners LLC", "Channel Partner", "payouts@northstar-partners.example"),
    ("ana.ruiz", "Ana", "Ruiz (agent)", "Independent Sales Agent", "ana.ruiz@agents.example"),
    ("brightline.referrals", "Brightline", "Referral Co.", "Referral Partner", "ap@brightline-referrals.example"),
    ("kai.moreno", "Kai", "Moreno (agent)", "Independent Sales Agent", "kai.moreno@agents.example"),
]
TERMINATED = {"taylor.brooks": "2026-05-08"}           # the Acme cast: Taylor leaves mid-quarter (CA)
BONUS_REPS = 3


def _stable(text: str, low: int, high: int) -> int:
    """A deterministic number in [low, high] from a name (no random state to keep in sync)."""
    return low + int(hashlib.sha256(text.encode()).hexdigest(), 16) % (high - low + 1)


def _payouts(path) -> list[dict]:
    rows = list(csv.DictReader(Path(path).read_text(encoding="utf-8-sig").splitlines()))
    return [{"rep_name": r["Rep"], "deal": r["Deal"], "quarter": r["Quarter"], "date": formats.date_mdy(r["Paid Date"]),
             "amount_cents": formats.money(r["Commission"]), "base_cents": formats.money(r["Commissionable Base"]),
             "row": n} for n, r in enumerate(rows, start=1)]


def build(seed: int = 7, inbox="inbox") -> dict:
    from quota.sim.generator import Fernhill
    payouts_path = Path(inbox) / "payouts_2026Q1.csv"
    if not payouts_path.exists():
        from quota.intake.export import export
        export(seed, inbox)
    fernhill = Fernhill(seed)
    payouts = _payouts(payouts_path)
    people = []
    for rep in sorted(fernhill.reps, key=lambda r: r.id):
        first, last = rep.name.split(" ", 1)
        hire = "2024-03-01"
        status = [{"effectiveDate": hire, "status": "Full-Time", "comment": "Hire"}]
        if rep.id in TERMINATED:
            status.append({"effectiveDate": TERMINATED[rep.id], "status": "Terminated", "comment": "Resigned"})
        commission = [{"date": p["date"], "amount_cents": p["amount_cents"],
                       "comment": f"{p['deal']} {p['quarter']} (payouts_2026Q1.csv row {p['row']})"}
                      for p in payouts if p["rep_name"] == rep.name]
        people.append({"key": rep.id, "firstName": first, "lastName": last, "workEmail": f"{rep.id}@fernhill.example",
                       "jobTitle": "Account Executive", "department": "Sales", "reportsTo": "morgan.diaz",
                       "hireDate": hire, "state": rep.state, "payType": "Salary", "employmentStatus": status,
                       "terminationDate": TERMINATED.get(rep.id),
                       "compensation": [{"effectiveDate": hire, "payRate": _stable(rep.id, 70, 135) * 1000,
                                         "payType": "Salary", "payPer": "Year", "paySchedule": "Twice a month"}],
                       "commission": commission, "bonus": []})
    for person in sorted(people, key=lambda p: p["key"])[:BONUS_REPS]:
        person["bonus"].append({"date": "2026-03-31", "amount_cents": 250_000, "reason": "Performance",
                                "comment": "Q1 2026 team target"})
    for key, first, last, title, dept, boss, hire, state in STAFF:
        people.append({"key": key, "firstName": first, "lastName": last, "workEmail": f"{key}@fernhill.example",
                       "jobTitle": title, "department": dept, "reportsTo": boss, "hireDate": hire, "state": state,
                       "payType": "Salary", "terminationDate": None,
                       "employmentStatus": [{"effectiveDate": hire, "status": "Full-Time", "comment": "Hire"}],
                       "compensation": [{"effectiveDate": hire, "payRate": STAFF_PAY[key], "payType": "Salary",
                                         "payPer": "Year", "paySchedule": "Twice a month"}],
                       "commission": [], "bonus": []})
    for key, first, last, title, email in PARTNERS:
        people.append({"key": key, "firstName": first, "lastName": last, "workEmail": email, "jobTitle": title,
                       "department": "Partners", "reportsTo": "marcus.lee", "hireDate": "2025-07-01", "state": "CA",
                       "payType": "Contract", "terminationDate": None,
                       "employmentStatus": [{"effectiveDate": "2025-07-01", "status": "Contractor",
                                             "comment": "Partner agreement"}],
                       "compensation": [], "commission": [], "bonus": []})
    return {"source": "quota", "seed": seed, "world": "acme+fernhill",
            "departments": ["Sales", "Finance", "Sales Operations", "Partners"], "people": people}


def write(roster: dict, out) -> Path:
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(roster, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    return path
