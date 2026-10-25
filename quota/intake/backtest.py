"""`python -m quota backtest --quarter 2026Q1` (R6): recompute every row of the quarter's payout spreadsheet from
the facts the intake loaded, and say which rows it reproduces and why the others differ.

Each row is `match`, or one of the spreadsheet's known mistakes: `TCV_NOT_ACV` (paid on total contract value),
`CRM_NOT_CONTRACT` (paid on the CRM amount instead of the signed contract), `DUPLICATE_ROW` (the same row
twice), else `unexplained`. Every error is a held finding for the Controller; nothing is paid or clawed back.
"""

import json
from pathlib import Path

from quota.engine import routing
from quota.intake import facts
from quota.intake.intake import Intake
from quota.ledger import queries
from quota.money import fmt_usd


def classify(rows, truth, contracts, deals, by_name) -> list[dict]:
    expected = {(t["rep"], t["deal"]): t for t in truth}
    seen, out = {}, []
    for r in rows:
        rep = by_name.get(r["rep"], r["rep"])
        key, paid, base = (rep, r["deal"]), r["commission_cents"], r["base_cents"]
        t = expected.get(key)
        result = {"file": r["_file"], "row": r["_row"], "rep": rep, "deal": r["deal"], "paid_cents": paid,
                  "base_cents": base, "correct_cents": t["commission_cents"] if t else None,
                  "correct_base_cents": t["base_cents"] if t else None}
        deal_id = r["deal"].split("+")[0]
        contract, deal = contracts.get(deal_id), deals.get(deal_id)
        bps = next((s["bps"] for s in queries.loads(deal["splits_json"]) if s["rep"] == rep), 0) if deal else 0
        if key in seen and seen[key]["_raw"] == r["_raw"]:
            kind, correct = "DUPLICATE_ROW", 0
            result["duplicate_of"] = seen[key]["_row"]
        elif t is None:
            kind, correct = "unexplained", None
        elif base == t["base_cents"] and paid == t["commission_cents"]:
            kind, correct = "match", paid
        elif contract and contract["term_months"] > 12 and base == t["base_cents"] * contract["term_months"] // 12:
            kind, correct = "TCV_NOT_ACV", t["commission_cents"]
        elif deal and base == deal["crm_amount_cents"] * bps // 10000 != t["base_cents"]:
            kind, correct = "CRM_NOT_CONTRACT", t["commission_cents"]
        else:
            kind, correct = "unexplained", t["commission_cents"]
        seen.setdefault(key, r)
        out.append({**result, "class": kind, "correct_cents": correct,
                    "overpaid_cents": paid - correct if correct is not None and kind != "match" else 0})
    return out


def run(inbox="inbox", quarter="2026Q1", out_dir="out", llm=None, intake=None) -> dict:
    intake = intake or Intake(inbox, llm)
    conn, report = intake.run() if not hasattr(intake, "conn") else (intake.conn, None)
    people = queries.latest_rows(conn, "people", facts.END)
    by_name = {p["name"]: p["id"] for p in people}
    rows = [r for r in intake.records.get("payouts", []) if r["quarter"] == quarter]
    truth = facts.payouts(conn, [p["id"] for p in people if p["role"] == "AE"], quarter)
    contracts = {c["deal_id"]: c for c in queries.latest_rows(conn, "contracts", facts.END)}
    deals = {d["id"]: d for d in queries.latest_rows(conn, "deals", facts.END)}
    results = classify(rows, truth, contracts, deals, by_name)
    errors = [r for r in results if r["class"] not in ("match", "unexplained")]
    unexplained = [r for r in results if r["class"] == "unexplained"]
    overpaid = sum(r["overpaid_cents"] for r in errors)
    matched = sum(1 for r in results if r["class"] == "match")
    controller = routing.owner("material_flag")
    findings = [{"id": f"BT-{quarter}-{r['row']:04d}", "outcome": "held", "routed_to": controller, **r,
                 "text": f"{r['file']} row {r['row']}: {r['class']} on {r['deal']} ({r['rep']}): paid "
                         f"{fmt_usd(r['paid_cents'])}, the signed contract pays {fmt_usd(r['correct_cents'])}"}
                for r in errors]
    headline = (f"Reproduced {matched} of {len(results)} {quarter[-2:]} payouts; {len(errors)} spreadsheet errors "
                f"({fmt_usd(overpaid)} overpaid)." + (f" {len(unexplained)} unexplained." if unexplained else ""))
    out = {"quarter": quarter, "rows": len(results), "matched": matched, "errors": len(errors),
           "unexplained": len(unexplained), "overpaid_cents": overpaid, "headline": headline,
           "findings": findings, "results": results}
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    (Path(out_dir) / f"backtest_{quarter}.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out
