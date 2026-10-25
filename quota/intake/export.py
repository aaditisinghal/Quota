"""`python -m quota export --seed 7 --out inbox/` (R6): the generator's world as the files a Controller would be
sent, with the mess real exports have, and `_answer_key.json` listing every planted item. The intake reads
these back; the round trip is checked against the generator, which is the answer key."""

import csv
import io
import json
import random
from pathlib import Path


from quota.engine.plans import load_plan_raw
from quota.intake import facts, formats, plans
from quota.ledger import queries
from quota.sim import seed_acme
from quota.sim.generator import generate

QUARTER = "2026Q1"
COUNTERSIGNER = "marcus.lee"
AMBIGUOUS_REP = "alex.chen"
HEADERS = {
    "salesforce_opportunities.csv": ["Opportunity ID", "Opportunity Name", "Account Name", "Amount", "Close Date",
                                     "Stage", "Opportunity Owner", "Split"],
    "opportunity_field_history.csv": ["Opportunity ID", "Field / Event", "Old Value", "New Value", "Edited By",
                                      "Edit Date"],
    "docusign_envelope_events.csv": ["Envelope Id", "SFDC Opp Id", "Event", "Recipient", "Role", "Timestamp (UTC)"],
    "stripe_balance_export.csv": ["id", "Created (UTC)", "Amount", "Currency", "Customer Name", "Invoice", "Type",
                                  "Status", "Metadata[opportunity_id]"],
    "contracts.csv": ["Opportunity ID", "ACV", "Term (months)", "Billing", "Clauses", "Countersigned By",
                      "Countersigned At"],
    "hr_roster.csv": ["Employee", "Email", "Title", "Manager", "State", "Start Date", "End Date"],
    "payouts_2026Q1.csv": ["Rep", "Deal", "Quarter", "Commissionable Base", "Rate", "Commission", "Paid Date"],
}
FIELD_LABELS = {"account": "Account Name", "crm_amount_cents": "Amount", "crm_close_date": "Close Date",
                "stage": "Stage", "owner_id": "Opportunity Owner", "splits_json": "Split"}
STRIPE_TYPES = {"invoice": ("invoice", "paid"), "payment": ("charge", "succeeded"), "refund": ("refund", "succeeded")}


def write_csv(path: Path, header: list[str], rows: list[list], bom=False) -> None:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    path.write_text(("﻿" if bom else "") + buffer.getvalue(), encoding="utf-8", newline="")


def export(seed: int, out_dir, reps: int = 20, world=None) -> dict:
    """Run the seed's world to its end and write its facts as an inbox. Returns the answer key. A world already
    run to its end may be passed in (it is left open)."""
    out = Path(out_dir)
    (out / "plans").mkdir(parents=True, exist_ok=True)
    owned = world is None
    if owned:
        world = generate(seed, reps=reps, out=lambda line: None)
        world.run_until(world.end_ts)
    ro, rng, key = world.ro, random.Random(f"inbox-{seed}"), {"seed": seed, "planted": []}
    try:
        people = facts.latest(ro, "people")
        names = {p["id"]: p["name"] for p in people}
        deals = facts.latest(ro, "deals")
        q2 = sorted(d["id"] for d in deals if (d["crm_close_date"] or "") >= "2026-04-01" and d["owner_id"] != "jordan.kim"
                    and d["stage"] == "closed_won")
        dup_deal, spaced_deal, blank_deal = rng.sample(q2, 3)

        def plant(file, row, kind, **detail):
            key["planted"].append({"file": file, "row": row, "kind": kind, **detail})

        # salesforce_opportunities.csv: BOM, "$150,000.00", M/D/YYYY, a duplicate, trailing spaces, a blank owner
        rows = []
        for d in deals:
            owner = names.get(d["owner_id"], d["owner_id"])
            if d["id"] == spaced_deal:
                owner += "   "
            if d["id"] == blank_deal:
                owner = ""
            row = [d["id"], f"{d['account']} - New Business", d["account"], formats.to_money(d["crm_amount_cents"]),
                   formats.to_mdy(d["crm_close_date"]), formats.to_stage(d["stage"]), owner,
                   formats.to_splits(queries.loads(d["splits_json"]), names)]
            rows.append(row)
            if d["id"] == spaced_deal:
                plant("salesforce_opportunities.csv", len(rows), "trailing_spaces_owner", deal_id=d["id"])
            if d["id"] == blank_deal:
                plant("salesforce_opportunities.csv", len(rows), "blank_owner", deal_id=d["id"], expect="held")
            if d["id"] == dup_deal:
                rows.append(list(row))
                plant("salesforce_opportunities.csv", len(rows), "duplicate_row", deal_id=d["id"], duplicate_of=len(rows) - 1)
        write_csv(out / "salesforce_opportunities.csv", HEADERS["salesforce_opportunities.csv"], rows, bom=True)

        # opportunity_field_history.csv: every CRM version as field edits, in Pacific time with no zone
        rows = []
        for d in deals:
            versions = queries.fetch_all(ro, "SELECT * FROM deals WHERE id = ? ORDER BY version", (d["id"],))
            for previous, current in zip([None] + versions, versions):
                by = names.get(current["source"].split(":", 1)[-1], current["source"].split(":", 1)[-1])
                when = formats.to_pacific(current["known_at"])
                if previous is None:
                    rows.append([d["id"], "Created.", "", "", by, when])
                    continue
                for column, label in FIELD_LABELS.items():
                    if previous[column] != current[column]:
                        rows.append([d["id"], label, previous[column] or "", current[column] or "", by, when])
        write_csv(out / "opportunity_field_history.csv", HEADERS["opportunity_field_history.csv"], rows)

        # docusign_envelope_events.csv: one row is missing its opportunity id
        esign = facts.latest(ro, "esign_events")
        orphan = rng.choice([e["id"] for e in esign if e["deal_id"] in q2 and e["deal_id"] not in (dup_deal, blank_deal)])
        rows = []
        for e in esign:
            rows.append([e["envelope_id"], "" if e["id"] == orphan else e["deal_id"], e["event"],
                         names[COUNTERSIGNER], "Fernhill countersigner", formats.to_utc_space(e["at"])])
            if e["id"] == orphan:
                plant("docusign_envelope_events.csv", len(rows), "missing_opportunity_id", esign_id=e["id"],
                      deal_id=e["deal_id"], expect="review")
        write_csv(out / "docusign_envelope_events.csv", HEADERS["docusign_envelope_events.csv"], rows)

        # stripe_balance_export.csv: decimal strings, one row without metadata, one duplicate id
        billing = facts.latest(ro, "billing")
        accounts = {d["id"]: d["account"] for d in deals}
        payments = [b["id"] for b in billing if b["kind"] == "payment"]
        no_meta, dup_id = rng.sample(payments, 2)
        rows = []
        for b in billing:
            kind, status = STRIPE_TYPES[b["kind"]]
            row = [b["id"], formats.to_utc_space(b["at"]), formats.to_decimal(b["amount_cents"]), "usd",
                   accounts.get(b["deal_id"], ""), f"in_{b['deal_id']}", kind, status,
                   "" if b["id"] == no_meta else b["deal_id"]]
            rows.append(row)
            if b["id"] == no_meta:
                plant("stripe_balance_export.csv", len(rows), "missing_metadata", billing_id=b["id"],
                      deal_id=b["deal_id"], expect="matched_by_invoice")
            if b["id"] == dup_id:
                rows.append(list(row))
                plant("stripe_balance_export.csv", len(rows), "duplicate_id", billing_id=b["id"], duplicate_of=len(rows) - 1)
        write_csv(out / "stripe_balance_export.csv", HEADERS["stripe_balance_export.csv"], rows)

        # contracts.csv: the contract in force, clauses as ;-separated text
        rows = []
        for c in facts.latest(ro, "contracts"):
            clauses = queries.loads(c["clauses_json"])
            rows.append([c["deal_id"], formats.to_money(c["acv_cents"]), c["term_months"], clauses.get("billing", ""),
                         formats.to_clauses(clauses), names[COUNTERSIGNER], c["countersigned_at"]])
        write_csv(out / "contracts.csv", HEADERS["contracts.csv"], rows)

        # hr_roster.csv: managers by full name
        rows = [[p["name"], f"{p['id']}@fernhill.example", p["role"], names.get(p["manager_id"], ""), p["state"] or "",
                 formats.to_mdy(p["start_date"]), formats.to_mdy(p["end_date"])] for p in people]
        write_csv(out / "hr_roster.csv", HEADERS["hr_roster.csv"], rows)

        # plans/<rep>.pdf: Jordan's, and one other rep's whose section 4 does not say whose signature
        template = load_plan_raw(seed_acme.PLAN_PATH)
        plans.write_pdf(plans.plan_text(template), out / "plans" / "jordan.kim.pdf")
        other = {**template, "plan_id": f"AE-2026-H1-{AMBIGUOUS_REP}", "rep": AMBIGUOUS_REP}
        plans.write_pdf(plans.plan_text(other, ambiguous_booking=True), out / "plans" / f"{AMBIGUOUS_REP}.pdf")
        plant(f"plans/{AMBIGUOUS_REP}.pdf", None, "ambiguous_section", section=4, key="booking_date",
              expect="AMBIGUOUS review item for the plan owner")

        # payouts_2026Q1.csv: what was paid for Q1, with exactly three errors on reps other than Jordan
        reps = [p["id"] for p in people if p["role"] == "AE"]
        truth = facts.payouts(ro, reps, QUARTER)
        contracts = {c["deal_id"]: c for c in facts.latest(ro, "contracts")}
        crm = {d["id"]: d["crm_amount_cents"] for d in deals}
        others = [i for i, p in enumerate(truth) if p["rep"] != "jordan.kim"]
        tcv = rng.choice([i for i in others if contracts[p_deal(truth[i])]["term_months"] > 12])
        crm_row = rng.choice([i for i in others if i != tcv
                              and crm[p_deal(truth[i])] > contracts[p_deal(truth[i])]["acv_cents"]])
        dup = rng.choice([i for i in others if i not in (tcv, crm_row)])
        rows, overpaid = [], 0
        for i, p in enumerate(truth):
            base, commission = p["base_cents"], p["commission_cents"]
            contract = contracts[p_deal(p)]
            if i == tcv:
                base, commission = base * contract["term_months"] // 12, commission * contract["term_months"] // 12
            if i == crm_row:
                ratio = crm[p_deal(p)] / contract["acv_cents"]
                base, commission = round(base * ratio), round(commission * ratio)
            row = [names[p["rep"]], p["deal"], p["quarter"], formats.to_money(base), p["rate"],
                   formats.to_money(commission), "4/15/2026"]
            rows.append(row)
            if i in (tcv, crm_row):
                kind = "TCV_NOT_ACV" if i == tcv else "CRM_NOT_CONTRACT"
                overpaid += commission - p["commission_cents"]
                plant("payouts_2026Q1.csv", len(rows), kind, rep=p["rep"], deal=p["deal"], paid_cents=commission,
                      correct_cents=p["commission_cents"])
            if i == dup:
                rows.append(list(row))
                overpaid += commission
                plant("payouts_2026Q1.csv", len(rows), "DUPLICATE_ROW", rep=p["rep"], deal=p["deal"],
                      paid_cents=commission, correct_cents=0, duplicate_of=len(rows) - 1)
        write_csv(out / "payouts_2026Q1.csv", HEADERS["payouts_2026Q1.csv"], rows)
        key["payouts"] = {"rows": len(rows), "errors": 3, "overpaid_cents": overpaid}
        key["held_deals"], key["review_esign"] = [blank_deal], [orphan]
        (out / "_answer_key.json").write_text(json.dumps(key, indent=2), encoding="utf-8")
        return key
    finally:
        if owned:
            world.close()


def p_deal(payout) -> str:
    return payout["deal"].split("+")[0]



