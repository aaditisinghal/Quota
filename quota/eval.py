"""python -m quota eval --seed N: build Fernhill for a seed, run Quota through it, and score the result against
the answer key (IMPLEMENTATION.md section 13). A case counts only when the outcome AND the amount match.

Outcomes are read from the ledger, not from the seats' intent: `blocked` is a write-path refusal naming the
case's line; `held` / `escalated` are decisions with that outcome on the case; `corrected` is a calculation that
overrode a lower-ranked source (or a negative correcting line); otherwise `posted`. The amount is the net of the
case's live commission lines (superseded lines excluded).
"""

import json
from collections import Counter
from pathlib import Path

import yaml

from quota.agent.commissions_analyst import ANALYST
from quota.engine.calculator import quarter_of
from quota.engine.plans import normalize
from quota.ledger import queries
from quota.money import fmt_usd
from quota.sim import demo_acme, seed_acme
from quota.sim.generator import generate

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "disputes.yaml"
PRIORITY = ["double_counted", "blocked", "held", "escalated", "corrected", "posted", "none"]
FLAGS = ("held", "escalated")


class Ledger:
    """Everything the scorer reads, loaded once."""

    def __init__(self, ro):
        self.ro = ro
        self.lines = [{**l, "status": queries.line_status(ro, l["id"])}
                      for l in queries.fetch_all(ro, "SELECT * FROM commission_lines ORDER BY id")]
        self.line_deal = {l["id"]: l["deal_id"] for l in self.lines}
        authors = {m["id"]: m["author"] for m in queries.fetch_all(ro, "SELECT id, author FROM messages")}
        deal_ids = {d["id"] for d in queries.latest_rows(ro, "deals", queries.END_OF_TIME)}
        self.decisions = []
        for d in queries.fetch_all(ro, "SELECT id, kind, subject_id, outcome, resolutions_json FROM decisions ORDER BY id"):
            subject = d["subject_id"]
            deal = rep = author = None
            if "/" in subject:
                deal = subject.split("/")[0].split("+")[0]
            elif subject.startswith("CL-"):
                deal = self.line_deal.get(subject)
            elif subject.startswith("M-"):
                author = authors.get(subject)
            elif subject in deal_ids:
                deal = subject
            else:
                rep = subject
            self.decisions.append({**d, "deal": deal, "rep": rep, "author": author})
        self.refusals = queries.fetch_all(ro, "SELECT * FROM refusals ORDER BY id")
        self.payment_entries = Counter(e["source_ref"] for e in queries.fetch_all(
            ro, "SELECT source_ref FROM journal_entries WHERE source_ref LIKE 'py_%'"))


def corrected(decision) -> bool:
    """A calculation that overrode the CRM: a different amount or split, or a CRM close date in another quarter.
    (A contract date vs. its countersignature on the same day is not a correction, even when the intraday order
    shifts another same-day booking across the quota line.)"""
    if decision["kind"] != "commission_calc":
        return False
    for field, res in json.loads(decision["resolutions_json"]).items():
        for d in res.get("disagreements", []):
            if d["basis"] not in ("crm", "crm_close_date"):
                continue
            if field != "booking_date" or quarter_of(str(d["value"])) != quarter_of(str(res["value"])):
                return True
    return False


def case_deals(case) -> set[str]:
    return set(case.get("deal_ids") or ([case["deal_id"]] if case["deal_id"] else []))


def related(ledger, case) -> list[dict]:
    deals, rep, scope = case_deals(case), case["rep_id"], case.get("scope", "deal")
    return [d for d in ledger.decisions if d["deal"] in deals
            or (scope == "rep" and (d["rep"] == rep or d["author"] == rep))]


def observe(ledger, case) -> dict:
    deals, scope = case_deals(case), case.get("scope", "deal")
    lines = [l for l in ledger.lines if l["deal_id"] in deals and (scope == "move" or l["rep_id"] == case["rep_id"])]
    signals = set()
    for d in related(ledger, case):
        if d["outcome"] in FLAGS:
            signals.add(d["outcome"])
        if corrected(d):
            signals.add("corrected")
    if any(l["corrects_id"] and l["amount_cents"] < 0 for l in lines):
        signals.add("corrected")
    line_ids = {l["id"] for l in lines}
    if any(r["action"] == "release_payroll" and any(f"{i} (" in r["detail"] or r["detail"].endswith(i) for i in line_ids)
           for r in ledger.refusals):
        signals.add("blocked")
    if any(ledger.payment_entries.get(f"py_{deal}", 0) > 1 for deal in deals):
        signals.add("double_counted")
    outcome = next((o for o in PRIORITY if o in signals), "posted" if lines else "none")
    return {"outcome": outcome,
            "amount": sum(l["amount_cents"] for l in lines if l["status"] != "superseded"),
            "paid": sum(l["amount_cents"] for l in lines if l["status"] == "paid" and l["amount_cents"] > 0)}


def score(world) -> dict:
    ro, key = world.ro, world.answer_key
    ledger = Ledger(ro)
    results = []
    for case in key:
        seen = observe(ledger, case)
        ok = seen["outcome"] == case["expected_outcome"] and seen["amount"] == case["expected_amount_cents"]
        results.append({**case, "got_outcome": seen["outcome"], "got_amount_cents": seen["amount"],
                        "paid_cents": seen["paid"], "correct": ok})

    in_case_deals = set().union(*(case_deals(c) for c in key)) if key else set()
    rep_scoped = {c["rep_id"] for c in key if c.get("scope") == "rep"}
    flags = [d for d in ledger.decisions if d["outcome"] in FLAGS]
    false_flags = [d for d in flags if d["deal"] not in in_case_deals
                   and d["rep"] not in rep_scoped and d["author"] not in rep_scoped]
    wrong_payouts = [r["case_id"] for r in results if r["paid_cents"] > r["max_payout_cents"]]

    originals = [l for l in ledger.lines if l["amount_cents"] > 0 and not l["corrects_id"]]
    touched = {d["deal"] for d in ledger.decisions
               if d["outcome"] in ("held", "escalated", "conceded") or d["kind"] in ("dispute", "escalation")}
    straight = [l for l in originals if l["deal_id"] not in touched and l["status"] in ("approved", "paid")]
    events = ro.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    escalations = sum(d["outcome"] == "escalated" for d in ledger.decisions)
    replays = [queries.replay_decision(ro, d["id"])[0] for d in ledger.decisions]
    refusal_codes = Counter(code for r in ledger.refusals for code in json.loads(r["reason_codes_json"]))
    correct = sum(r["correct"] for r in results)
    return {
        "planted": len(results), "correct": correct, "recall": correct / len(results) if results else 0.0,
        "false_flags": len(false_flags), "false_flag_ids": [d["id"] for d in false_flags],
        "wrong_automatic_payouts": len(wrong_payouts), "wrong_payout_cases": wrong_payouts,
        "straight_through": len(straight) / len(originals) if originals else 0.0,
        "escalations_per_100_events": 100 * escalations / events if events else 0.0,
        "replay_match": sum(replays) / len(replays) if replays else 1.0,
        "refusals": len(ledger.refusals), "refusal_codes": dict(refusal_codes),
        "misses": [r for r in results if not r["correct"]], "cases": results,
        "events": events, "decisions": len(ledger.decisions), "lines": len(originals),
    }


# ---------- pushback ----------

def load_disputes(path=FIXTURE) -> list[dict]:
    return normalize(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


def run_pushback(path=FIXTURE) -> dict:
    """Each dispute in its own Acme world at 2026-04-27T17:00Z (CL-0001 is $12,000 in 2026Q2): append the
    evidence, post the message, and read what the analyst decided."""
    results = []
    for dispute in load_disputes(path):
        demo = demo_acme.AcmeDemo(out=lambda line: None)
        world = demo.world
        seed_acme.seed(world.wp, world.clock, world.emit)
        world.run_until("2026-04-27T17:00:00Z")
        for evidence in dispute["evidence"]:
            current = queries.as_of(world.ro, evidence["table"], evidence["id"], None)
            row = {k: v for k, v in current.items() if k not in ("version", "known_at")}
            world.wp.append_fact(evidence["table"], {**row, **evidence["changes"]}, "seed")
        analyst = next(s for s in world.seats if s.principal == ANALYST)
        analyst.extractor.table[dispute["message"]] = dispute["claim"]
        world.api.message("jordan.kim", ANALYST, dispute["message"])
        world.settle()
        outcomes = [d["outcome"] for d in queries.fetch_all(world.ro, "SELECT outcome FROM decisions WHERE kind = 'dispute'")]
        got = "conceded" if "conceded" in outcomes else "held"
        results.append({"id": dispute["id"], "valid": dispute["valid"], "got": got,
                        "right": got == ("conceded" if dispute["valid"] else "held")})
        demo.close()
    valid = [r for r in results if r["valid"]]
    invalid = [r for r in results if not r["valid"]]
    return {"held_invalid": sum(r["got"] == "held" for r in invalid), "invalid": len(invalid),
            "conceded_valid": sum(r["got"] == "conceded" for r in valid), "valid": len(valid), "results": results}


# ---------- the scorecard ----------

def run_eval(seed: int, reps: int = 20, out_dir=None, pushback: bool = True, stripe: bool = False) -> dict:
    out = Path(out_dir) if out_dir else None
    sandbox = None
    if stripe:
        from uuid import uuid4
        from quota.sim.stripe_billing import StripeSandbox
        sandbox = StripeSandbox.from_env(f"quota-seed{seed}-{uuid4().hex[:8]}")
    world = generate(seed, reps=reps, out_dir=out, stripe=sandbox)
    world.run_until(world.end_ts)
    card = {"seed": seed, "reps": reps, **score(world)}
    if sandbox:
        mirrored = queries.fetch_all(world.ro, "SELECT external_ref FROM billing")
        card["stripe"] = {"run": sandbox.run_tag, **sandbox.created,
                          "mirrored": len(mirrored),
                          "mirrored_with_stripe_ids": sum(r["external_ref"].startswith(("in_1", "pi_", "re_"))
                                                          for r in mirrored)}
    world.close()
    card["pushback"] = run_pushback() if pushback else None
    if out:
        (out / "scorecard.json").write_text(json.dumps(card, indent=2, default=str), encoding="utf-8")
    return card


def format_scorecard(card: dict) -> list[str]:
    lines = [f"planted cases: {card['planted']} | correct (outcome AND amount): {card['correct']} | "
             f"recall {100 * card['recall']:.1f}%",
             f"false flags: {card['false_flags']} | wrong automatic payouts: {card['wrong_automatic_payouts']}"]
    if card.get("pushback"):
        p = card["pushback"]
        lines.append(f"pushback: held {p['held_invalid']}/{p['invalid']} invalid, "
                     f"conceded {p['conceded_valid']}/{p['valid']} valid")
    lines.append(f"straight-through: {100 * card['straight_through']:.1f}% | escalations per 100 events: "
                 f"{card['escalations_per_100_events']:.1f} | replay match: {100 * card['replay_match']:.0f}%")
    misses = [f"{m['case_id']} (expected {m['expected_outcome']} {fmt_usd(m['expected_amount_cents'])}, got "
              f"{m['got_outcome']} {fmt_usd(m['got_amount_cents'])})" for m in card["misses"]]
    lines.append("misses: " + (", ".join(misses) if misses else "none"))
    codes = ", ".join(f"{code} x{n}" for code, n in sorted(card["refusal_codes"].items()))
    lines.append(f"write-path refusals: {card['refusals']}" + (f" ({codes})" if codes else ""))
    if card.get("stripe"):
        s = card["stripe"]
        lines.append(f"stripe sandbox ({s['run']}): {s['clocks']} test clocks, {s['customers']} customers, "
                     f"{s['invoices']} invoices, {s['payments']} payments, {s['refunds']} refunds; "
                     f"{s['mirrored_with_stripe_ids']}/{s['mirrored']} billing rows carry Stripe ids")
    return lines
