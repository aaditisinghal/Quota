"""Whole-ledger invariant checks. Each returns list[Violation]; empty means OK."""

import json
from dataclasses import dataclass

from quota import clock as clk
from quota.ledger import queries


@dataclass(frozen=True)
class Violation:
    code: str
    subject: str
    detail: str


def entry_balanced(lines) -> bool:
    """INV1: >= 2 lines, each exactly one of debit/credit > 0, debits == credits."""
    if len(lines) < 2:
        return False
    for line in lines:
        debit, credit = line.get("debit_cents", 0), line.get("credit_cents", 0)
        if not (isinstance(debit, int) and isinstance(credit, int)) or debit < 0 or credit < 0:
            return False
        if (debit > 0) == (credit > 0):
            return False
    return sum(l.get("debit_cents", 0) for l in lines) == sum(l.get("credit_cents", 0) for l in lines)


def _posted(conn):
    """(entry, posted_known_at) for every posting."""
    rows = queries.fetch_all(conn, "SELECT j.*, s.known_at AS posted_at FROM journal_entries j "
                                   "JOIN je_status s ON s.je_id = j.id WHERE s.status = 'posted' ORDER BY j.id")
    return rows


def check_inv1(conn) -> list[Violation]:
    out = []
    for entry in queries.fetch_all(conn, "SELECT id FROM journal_entries ORDER BY id"):
        if not entry_balanced(queries.lines_for(conn, entry["id"])):
            out.append(Violation("INV1", entry["id"], "entry does not balance"))
    return out


def check_inv2(conn) -> list[Violation]:
    out = []
    for entry in _posted(conn):
        if queries.period_locked(conn, entry["period"], entry["posted_at"]):
            out.append(Violation("INV2", entry["id"], f"posted into locked period {entry['period']}"))
    for line in queries.fetch_all(conn, "SELECT * FROM commission_lines ORDER BY id"):
        if queries.period_locked(conn, clk.period_of(line["known_at"]), line["known_at"]):
            out.append(Violation("INV2", line["id"], "commission line written into a locked period"))
    return out


def check_inv4(conn) -> list[Violation]:
    out = []
    for entry in queries.fetch_all(conn, "SELECT * FROM journal_entries ORDER BY id"):
        if entry["source_ref"] is None and (
                entry["decision_id"] is None or queries.as_of(conn, "decisions", entry["decision_id"], None) is None):
            out.append(Violation("INV4", entry["id"], "non-source entry without a decision"))
    for line in queries.fetch_all(conn, "SELECT * FROM commission_lines ORDER BY id"):
        if queries.as_of(conn, "decisions", line["decision_id"], None) is None:
            out.append(Violation("INV4", line["id"], "commission line without a decision"))
    for decision in queries.fetch_all(conn, "SELECT * FROM decisions ORDER BY id"):
        if not json.loads(decision["inputs_json"]):
            out.append(Violation("INV4", decision["id"], "decision with empty inputs"))
    return out


def check_inv5(conn) -> list[Violation]:
    out = []
    for d in queries.fetch_all(conn, "SELECT d.*, a.requested_by, a.approver FROM approval_decisions d "
                                     "JOIN approvals a ON a.id = d.approval_id ORDER BY d.rowid"):
        if d["decided_by"] == d["requested_by"]:
            out.append(Violation("INV5", d["approval_id"], "self-approval"))
        if d["decided_by"] != d["approver"]:
            out.append(Violation("INV5", d["approval_id"], f"decided by {d['decided_by']}, not {d['approver']}"))
    for entry in _posted(conn):
        at = entry["posted_at"]
        if entry["created_by"].startswith("seat:") and entry["source_ref"] is None:
            if (queries.satisfied_approval(conn, "journal_entry", entry["id"], at) is None
                    and queries.standing_precedent(conn, queries.memo_kind(entry["memo"]), at) is None):
                out.append(Violation("INV5", entry["id"], "seat entry posted without approval"))
        elif queries.memo_kind(entry["memo"]) == "payroll_payment":
            if queries.satisfied_approval(conn, "payroll_file", entry["source_ref"], at) is None:
                out.append(Violation("INV5", entry["id"], f"payroll {entry['source_ref']} released unapproved"))
    return out


def check_inv6(conn) -> list[Violation]:
    out = []
    for payroll in queries.fetch_all(conn, "SELECT * FROM payroll_files ORDER BY id"):
        lines = json.loads(payroll["lines_json"])
        if payroll["total_cents"] != sum(l["amount_cents"] for l in lines):
            out.append(Violation("INV6", payroll["id"], "total != sum of lines"))
        for pl in lines:
            line = queries.as_of(conn, "commission_lines", pl["line_id"], None)
            if line is None or line["amount_cents"] != pl["amount_cents"]:
                out.append(Violation("INV6", payroll["id"], f"{pl['line_id']} amount mismatch"))
    return out


def check_inv7(conn) -> list[Violation]:
    from quota.engine import ranking
    out = []
    for line in queries.fetch_all(conn, "SELECT * FROM commission_lines ORDER BY id"):
        deal = queries.as_of(conn, "deals", line["deal_id"], line["known_at"])
        if deal is None or ranking.splits_total(ranking.resolve_splits(deal, [])) != 10000:
            out.append(Violation("INV7", line["id"], f"splits on {line['deal_id']} do not sum to 10000"))
    return out


def _gl(tb, account):
    return tb.get(account, 0)


def tie_outs(conn, t=None) -> list[dict]:
    """Subledger to GL: each pair is computed from different records, so they agree only if the books do."""
    tb = queries.trial_balance(conn, t)
    lines = [{**l, "status": queries.line_status(conn, l["id"], t)}
             for l in queries.fetch_all(conn, "SELECT * FROM commission_lines WHERE known_at <= ?", (t or queries.END_OF_TIME,))]
    payable = sum(l["amount_cents"] for l in lines if l["amount_cents"] > 0 and l["status"] == "approved")
    recoverable = sum(-l["amount_cents"] for l in lines if l["amount_cents"] < 0 and l["status"] == "approved")
    amortized: dict[str, int] = {}
    in_transit = 0
    for entry in queries.journal(conn, t):
        kind = queries.memo_kind(entry["memo"])
        for l in entry["lines"]:
            if kind == "amortization" and entry["status"] == "posted" and l["account"] == "6110":
                amortized[l["subledger_ref"]] = amortized.get(l["subledger_ref"], 0) + l["debit_cents"]
            if kind in ("clawback", "void_reversal", "true_up") and entry["status"] != "posted" and l["account"] == "1350":
                in_transit += l["credit_cents"]
    schedules = queries.fetch_all(conn, "SELECT * FROM amortization_schedules WHERE known_at <= ?",
                                  (t or queries.END_OF_TIME,))
    cancelled = {s["id"][:-2] for s in schedules if s["status"] == "cancelled"}
    remaining = sum(s["total_cents"] - amortized.get(s["line_id"], 0) for s in schedules
                    if s["status"] == "active" and s["id"] not in cancelled)
    billing = queries.fetch_all(conn, "SELECT kind, amount_cents FROM billing WHERE known_at <= ?", (t or queries.END_OF_TIME,))
    receivable = sum(b["amount_cents"] if b["kind"] == "invoice" else -b["amount_cents"]
                     for b in billing if b["kind"] in ("invoice", "payment"))
    pairs = [
        ("Deferred commissions (1350)", "unamortized schedules + recoveries awaiting approval", _gl(tb, "1350"),
         remaining + in_transit),
        ("Commissions payable (2150)", "approved, unpaid commission lines", -_gl(tb, "2150"), payable),
        ("Commission advances recoverable (1290)", "approved recovery lines", _gl(tb, "1290"), recoverable),
        ("Accounts receivable (1200)", "invoices less payments in billing", _gl(tb, "1200"), receivable),
    ]
    return [{"name": n, "subledger": s, "gl_cents": g, "subledger_cents": v, "ok": g == v} for n, s, g, v in pairs]


def check_inv3(conn) -> list[Violation]:
    """SPEC invariant 3: every subledger ties to its GL account (commissions payable, deferred commissions,
    recoveries due from reps, receivables)."""
    return [Violation("INV3", t["name"], f"GL {t['gl_cents']} != subledger {t['subledger_cents']} ({t['subledger']})")
            for t in tie_outs(conn) if not t["ok"]]


CHECKS = {"INV1": check_inv1, "INV2": check_inv2, "INV3": check_inv3, "INV4": check_inv4, "INV5": check_inv5,
          "INV6": check_inv6, "INV7": check_inv7}


def check_all(conn) -> dict[str, list[Violation]]:
    return {name: fn(conn) for name, fn in CHECKS.items()}


def summary_line(results: dict[str, list[Violation]]) -> str:
    return "INVARIANTS  " + "  ".join(f"{name} {'OK' if not v else f'FAIL({len(v)})'}"
                                      for name, v in results.items())
