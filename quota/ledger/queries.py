"""Read-only queries. Every function takes a connection; `t` limits the view to rows known at or before t."""

import json

from quota import clock as clk
from quota.ledger.hashing import row_sha

VERSIONED = {"people", "deals", "contracts", "plans"}
END_OF_TIME = "9999-12-31T23:59:59Z"


def _t(t):
    return t or END_OF_TIME


def fetch_all(conn, sql, params=()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def fetch_one(conn, sql, params=()) -> dict | None:
    found = conn.execute(sql, params).fetchone()
    return dict(found) if found is not None else None


def loads(value):
    return json.loads(value) if isinstance(value, str) else value


# ---------- facts ----------

def as_of(conn, table, id, t) -> dict | None:
    """Latest version of `id` with known_at <= t (non-versioned tables: the row, if known by t)."""
    if table in VERSIONED:
        return fetch_one(conn, f"SELECT * FROM {table} WHERE id = ? AND known_at <= ? "
                               "ORDER BY version DESC LIMIT 1", (id, _t(t)))
    return fetch_one(conn, f"SELECT * FROM {table} WHERE id = ? AND known_at <= ?", (id, _t(t)))


def ids(conn, table, t=None) -> list[str]:
    return [r["id"] for r in fetch_all(conn, f"SELECT DISTINCT id FROM {table} WHERE known_at <= ? ORDER BY id",
                                       (_t(t),))]


def latest_status(conn, table, key_col, key, t=None) -> str | None:
    found = fetch_one(conn, f"SELECT status FROM {table} WHERE {key_col} = ? AND known_at <= ? "
                            "ORDER BY known_at DESC, rowid DESC LIMIT 1", (key, _t(t)))
    return found["status"] if found else None


def period_locked(conn, period, t=None) -> bool:
    return latest_status(conn, "periods", "period", period, t) == "locked"


def line_status(conn, line_id, t=None) -> str | None:
    return latest_status(conn, "commission_line_status", "line_id", line_id, t)


def je_status(conn, je_id, t=None) -> str | None:
    return latest_status(conn, "je_status", "je_id", je_id, t)


def input_ref(table: str, row: dict) -> dict:
    """The record a decision keeps for each row it read."""
    return {"table": table, "id": row["id"], "version": row.get("version"), "sha256": row_sha(row)}


def load_input(conn, ref: dict) -> dict | None:
    if ref.get("version") is not None:
        return fetch_one(conn, f"SELECT * FROM {ref['table']} WHERE id = ? AND version = ?",
                         (ref["id"], ref["version"]))
    return fetch_one(conn, f"SELECT * FROM {ref['table']} WHERE id = ?", (ref["id"],))


# ---------- journal ----------

def lines_for(conn, je_id) -> list[dict]:
    return fetch_all(conn, "SELECT * FROM je_lines WHERE je_id = ? ORDER BY line_no", (je_id,))


def journal(conn, t=None) -> list[dict]:
    entries = fetch_all(conn, "SELECT * FROM journal_entries WHERE known_at <= ? ORDER BY id", (_t(t),))
    for entry in entries:
        entry["status"] = je_status(conn, entry["id"], t)
        entry["lines"] = lines_for(conn, entry["id"])
    return entries


def trial_balance(conn, t=None) -> dict[str, int]:
    """account -> signed balance (debit positive), posted entries only, accounts with activity."""
    found = fetch_all(conn, """
        SELECT l.account AS account, SUM(l.debit_cents) - SUM(l.credit_cents) AS balance
        FROM je_lines l JOIN journal_entries j ON j.id = l.je_id
        WHERE (SELECT s.status FROM je_status s WHERE s.je_id = j.id AND s.known_at <= ?
               ORDER BY s.known_at DESC, s.rowid DESC LIMIT 1) = 'posted'
        GROUP BY l.account ORDER BY l.account""", (_t(t),))
    return {r["account"]: r["balance"] for r in found}


def memo_kind(memo: str) -> str:
    """Entry memos are written "<memo_kind>: <text>"."""
    return memo.split(":", 1)[0].strip()


# ---------- approvals and precedents ----------

def approval_decision(conn, approval_id, t=None) -> dict | None:
    """The first decision on an approval is the binding one."""
    return fetch_one(conn, "SELECT * FROM approval_decisions WHERE approval_id = ? AND known_at <= ? "
                           "ORDER BY known_at, rowid LIMIT 1", (approval_id, _t(t)))


def chain_ids(conn, root_id, t=None) -> list[str]:
    chain, current = [], root_id
    while current is not None:
        chain.append(current)
        child = fetch_one(conn, "SELECT id FROM approvals WHERE parent_id = ? AND known_at <= ? "
                                "ORDER BY id LIMIT 1", (current, _t(t)))
        current = child["id"] if child else None
    return chain


def chain_satisfied(conn, root_id, t=None) -> bool:
    """Satisfied when the last decision is `final` and every ancestor is `approved`."""
    for approval_id in chain_ids(conn, root_id, t):
        decided = approval_decision(conn, approval_id, t)
        if decided is None or decided["decision"] != "approved":
            return False
        if decided["result"] == "final":
            return True
        if decided["result"] != "over_limit_routed":
            return False
    return False


def satisfied_approval(conn, kind, subject_id, t=None) -> str | None:
    """Root id of a satisfied approval chain for (kind, subject), if any."""
    roots = fetch_all(conn, "SELECT id FROM approvals WHERE kind = ? AND subject_id = ? AND parent_id IS NULL "
                            "AND known_at <= ? ORDER BY id", (kind, subject_id, _t(t)))
    for root in roots:
        if chain_satisfied(conn, root["id"], t):
            return root["id"]
    return None


def approvals_form_satisfied_chain(conn, approval_ids, t=None) -> bool:
    approvals = [as_of(conn, "approvals", a, t) for a in approval_ids]
    if not approvals or any(a is None for a in approvals):
        return False
    roots = [a for a in approvals if a["parent_id"] is None]
    if len(roots) != 1:
        return False
    return (set(chain_ids(conn, roots[0]["id"], t)) == set(approval_ids)
            and chain_satisfied(conn, roots[0]["id"], t))


def precedents_in_force(conn, t) -> list[dict]:
    found = fetch_all(conn, "SELECT * FROM precedents WHERE known_at <= ? ORDER BY id", (t,))
    return [p for p in found if clk.in_window(p["effective_from"], p["expires_at"], t)]


def standing_precedent(conn, kind, t) -> str | None:
    """An in-force precedent whose scope is exactly {"kind": kind} (e.g. P-0000 for amortization)."""
    for p in precedents_in_force(conn, t):
        if loads(p["scope_json"]) == {"kind": kind}:
            return p["id"]
    return None


# ---------- gathering decision inputs ----------

def commission_rows(conn, rep_id, t) -> dict[str, list[dict]]:
    """Everything the calculator reads for one rep, as known at t."""
    deals = [as_of(conn, "deals", deal_id, t) for deal_id in ids(conn, "deals", t)]
    mine = [d for d in deals if d["owner_id"] == rep_id
            or any(s["rep"] == rep_id for s in json.loads(d["splits_json"]))]
    deal_ids = [d["id"] for d in mine]
    contracts = []
    for deal_id in deal_ids:
        found = fetch_one(conn, "SELECT id FROM contracts WHERE deal_id = ? AND known_at <= ? "
                                "ORDER BY version DESC LIMIT 1", (deal_id, t))
        if found:
            contracts.append(as_of(conn, "contracts", found["id"], t))
    marks = ", ".join("?" * len(deal_ids))
    esign = fetch_all(conn, f"SELECT * FROM esign_events WHERE deal_id IN ({marks}) AND known_at <= ? ORDER BY id",
                      (*deal_ids, t)) if deal_ids else []
    plans = [as_of(conn, "plans", plan_id, t) for plan_id in
             [r["id"] for r in fetch_all(conn, "SELECT DISTINCT id FROM plans WHERE rep_id = ? AND known_at <= ?",
                                         (rep_id, t))]]
    precedents = [p for p in precedents_in_force(conn, t) if loads(p["scope_json"]).get("deal_id") in deal_ids]
    return {"plans": plans, "deals": mine, "contracts": contracts, "esign_events": esign, "precedents": precedents}


def input_refs(rows: dict[str, list[dict]]) -> list[dict]:
    return [input_ref(table, row) for table, table_rows in rows.items() for row in table_rows]


# ---------- replay ----------

def _recompute(fn, rows, decision) -> dict:
    from quota.engine import calculator, disputes, entries
    subject, now = decision["subject_id"], decision["known_at"]
    if fn == "calc_commission":
        deal_id, rep_id = subject.split("/")
        return calculator.commission_decision(rows, deal_id, rep_id, now)["output"]
    if fn == "commission_accrual_entry":
        return entries.accrual_decision(rows, subject, now)["output"]
    if fn == "clawback_entry":
        return entries.clawback_decision(rows, subject, now)["output"]
    if fn == "evaluate_dispute":
        return disputes.dispute_decision(rows, subject, now)["output"]
    raise ValueError(f"no replay for fn {fn!r}")


def replay_decision(conn, decision_id) -> tuple[bool, dict, dict]:
    """Load each input at its exact (table, id, version), verify sha256, re-run the engine function
    named in output_json["fn"]. Returns (matches, stored_output, recomputed_output)."""
    decision = as_of(conn, "decisions", decision_id, None)
    if decision is None:
        raise KeyError(f"unknown decision {decision_id}")
    stored = json.loads(decision["output_json"])
    rows: dict[str, list[dict]] = {}
    for ref in json.loads(decision["inputs_json"]):
        row = load_input(conn, ref)
        if row is None or row_sha(row) != ref["sha256"]:
            return False, stored, {"error": f"input {ref['table']}:{ref['id']} v{ref.get('version')} "
                                            "is missing or altered"}
        rows.setdefault(ref["table"], []).append(row)
    recomputed = json.loads(json.dumps(_recompute(stored["fn"], rows, decision)))
    return recomputed == stored, stored, recomputed


def format_trial_balance(tb: dict[str, int], accounts: dict) -> list[str]:
    from quota.money import fmt_usd
    out = []
    for account, balance in tb.items():
        side = " Dr" if balance > 0 else " Cr" if balance < 0 else ""
        out.append(f"  {account} {accounts[account][0]:<36}{fmt_usd(abs(balance)) + side:>18}")
    dr = sum(b for b in tb.values() if b > 0)
    cr = -sum(b for b in tb.values() if b < 0)
    out.append(f"  {'Total':<41}Dr {fmt_usd(dr)} = Cr {fmt_usd(cr)}")
    return out
