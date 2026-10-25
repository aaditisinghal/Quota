"""The CFO demo flow's read model (DEMO_FLOW_TASKS D1-D5): confidence, the calibration panel, the sources rail,
the reps board, rep and contract cards, and the "Needs you" inbox. Everything is read from the ledger (and the
analyst's own hold rules); the screens compute nothing. Anything simulated says so.
"""

import json
from pathlib import Path

from quota import clock as clk
from quota import config
from quota.api import views
from quota.engine import calculator, confidence
from quota.ledger import queries
from quota.sim import pipeline

CALC_KINDS = ("commission_calc", "opening_balance")
PERSON_OF = {   # who is asked about what (DEMO_FLOW D5)
    "plan_exception": "marcus.lee", "splits_check": "marcus.lee", "plan_config": "marcus.lee",
    "commitment_review": "priya.nair", "journal_entry": "priya.nair", "payroll_file": "priya.nair",
    "integrity_check": "priya.nair", "termination_review": "priya.nair", "collection_hold": "priya.nair",
    "payroll_release": "dana.ortiz", "crm_fix": "marcus.lee",
}


def _loads(v):
    return json.loads(v) if isinstance(v, str) else (v or {})


class Flow:
    def __init__(self, session):
        self.session, self.world = session, session.world
        self.ro, self.now = session.world.ro, session.world.clock.now()
        self.analyst = session.analyst

    # ---------- lines and their decisions ----------

    def line(self, line_id):
        return queries.as_of(self.ro, "commission_lines", line_id, self.now)

    def calc_of(self, line) -> dict | None:
        """The commission decision behind a line (a correcting line's original's, for a true-up or recovery)."""
        seen = set()
        while line is not None and line["id"] not in seen:
            seen.add(line["id"])
            d = queries.as_of(self.ro, "decisions", line["decision_id"], self.now)
            if d and d["kind"] in CALC_KINDS:
                return d
            line = self.line(line["corrects_id"]) if line.get("corrects_id") else None
        return None

    def deal_flags(self, deal_id) -> list[tuple]:
        """What is known about the deal since it was booked, as (factor, value, reason)."""
        flags = []
        hold = self.analyst.commitment_holds().get(deal_id)
        if hold:
            flags.append(("integrity", confidence.INTEGRITY_CAP, f"off-contract promise ({hold['id']})"))
        for d in self.analyst.decisions(kind="backdating_check", subject_id=deal_id):
            if _loads(d["output_json"]).get("result", {}).get("backdated"):
                flags.append(("integrity", confidence.INTEGRITY_CAP, f"backdated CRM edit ({d['id']})"))
        for d in self.analyst.decisions(kind="integrity_check"):
            if deal_id in (d["explanation"] or "") or d["subject_id"] == deal_id:
                flags.append(("integrity", confidence.INTEGRITY_CAP, f"integrity flag ({d['id']})"))
        for d in self.analyst.decisions(kind="authority_check"):
            if "conflict of interest" in (d["explanation"] or "") and deal_id in json.dumps(_loads(d["output_json"])):
                flags.append(("integrity", confidence.INTEGRITY_CAP, f"approver conflict ({d['id']})"))
        if self.analyst.splits_invalid(deal_id):
            deal = self.analyst.get("deals", deal_id)
            total = sum(s["bps"] for s in _loads(deal["splits_json"]))
            flags.append(("owner_splits", 0.0, f"splits add to {total / 100:g}%, not 100%"))
        for d in self.analyst.decisions(kind="no_rule_check", subject_id=deal_id):
            flags.append(("rule_coverage", 0.0, f"NO_RULE ({d['id']})"))
        return flags

    def confidence(self, line_id) -> dict | None:
        line = self.line(line_id)
        decision = self.calc_of(line) if line else None
        if decision is None:
            return None
        out = _loads(decision["output_json"])
        stored = out.get("confidence") or confidence.stored(_loads(decision["resolutions_json"]),
                                                            _loads(decision["precedent_ids_json"]))
        now = confidence.current(stored, self.deal_flags(line["deal_id"]))
        hold = self.analyst.hold_on(line)
        return {"line": line_id, "deal": line["deal_id"], "decision": decision["id"], **now,
                "band_text": confidence.BAND_TEXT[now["band"]], "stored": stored,
                "hold": {"decision": hold["id"], "kind": hold["kind"], "reason": hold["explanation"]} if hold else None}

    # ---------- the calibration panel (D5) ----------

    def _ref(self, table, row):
        if row is None:
            return None
        if row.get("source_file"):
            return f"{row['source_file']} · row {row['source_row']}"
        return f"{table}:{row['id']}" + (f":v{row['version']}" if row.get("version") is not None else "")

    def calibration(self, line_id) -> dict | None:
        line = self.line(line_id)
        decision = self.calc_of(line) if line else None
        if decision is None:
            return None
        res = _loads(decision["resolutions_json"])
        out = _loads(decision["output_json"])
        result = out.get("result", {})
        deal = self.analyst.get("deals", line["deal_id"])
        contract_row = queries.fetch_one(self.ro, "SELECT id FROM contracts WHERE deal_id = ? AND known_at <= ? "
                                                  "ORDER BY version DESC LIMIT 1", (line["deal_id"], self.now))
        contract = self.analyst.get("contracts", contract_row["id"]) if contract_row else None
        booking, base = res.get("booking_date", {}), res.get("base", {})
        day = clk.local_date(str(booking.get("value"))) if booking.get("value") else None

        def agrees_day(v):
            return None if v is None or day is None else clk.local_date(str(v)) == day
        evidence = [
            {"source": "CRM", "field": "close date", "value": deal["crm_close_date"], "agrees": agrees_day(deal["crm_close_date"]),
             "ref": self._ref("deals", deal)},
            {"source": "CRM", "field": "amount", "value": deal["crm_amount_cents"],
             "agrees": deal["crm_amount_cents"] == base.get("value"), "ref": self._ref("deals", deal)},
        ]
        if contract:
            evidence += [
                {"source": "Contract", "field": "ACV", "value": contract["acv_cents"],
                 "agrees": contract["acv_cents"] == base.get("value"), "ref": self._ref("contracts", contract)},
                {"source": "Contract", "field": "contract date", "value": contract["contract_date"],
                 "agrees": agrees_day(contract["contract_date"]), "ref": self._ref("contracts", contract)}]
        for e in queries.fetch_all(self.ro, "SELECT * FROM esign_events WHERE deal_id = ? AND known_at <= ? ORDER BY id",
                                   (line["deal_id"], self.now)):
            evidence.append({"source": "E-signature", "field": e["event"], "value": e["at"],
                             "agrees": agrees_day(e["at"]) if e["event"] == "countersigned" else None,
                             "ref": self._ref("esign_events", e)})
        for b in queries.fetch_all(self.ro, "SELECT * FROM billing WHERE deal_id = ? AND known_at <= ? ORDER BY id",
                                   (line["deal_id"], self.now)):
            evidence.append({"source": "Billing", "field": b["kind"], "value": b["amount_cents"], "agrees": None,
                             "ref": b.get("external_ref") or self._ref("billing", b)})
        person = queries.as_of(self.ro, "people", line["rep_id"], self.now)
        if person:
            evidence.append({"source": "HR", "field": "employment", "value": "left " + person["end_date"]
                             if person["end_date"] else f"active since {person['start_date']}", "agrees": None,
                             "ref": self._ref("people", person)})
        for d in self.analyst.decisions(kind="commitment_check", subject_id=line["deal_id"]):
            for f in _loads(d["output_json"]).get("args", {}).get("findings", []):
                evidence.append({"source": "Messages", "field": f["type"], "value": f["quote"], "agrees": False,
                                 "ref": f"{f['doc_id']} ({d['id']})"})
        for row in evidence:
            row["label"] = {True: "agrees", False: "disagrees", None: "—"}[row["agrees"]]
        rules = [{"field": field, "rule": r.get("rule_id"), "chosen": {"basis": r.get("basis"), "value": r.get("value"),
                                                                      "source": r.get("source")},
                  "alternatives": [{"basis": d["basis"], "value": d["value"], "effect_cents": d.get("effect_cents"),
                                    "source": d.get("source")} for d in r.get("disagreements", [])]}
                 for field, r in res.items()]
        conf = self.confidence(line_id)
        return {"line": line_id, "deal": line["deal_id"], "rep": line["rep_id"], "decision": decision["id"],
                "evidence": evidence, "rules": rules,
                "amount": {"total_cents": result.get("amount_cents", line["amount_cents"]),
                           "credit_cents": result.get("credit_cents"), "base_portion_cents": result.get("base_portion_cents"),
                           "accel_portion_cents": result.get("accel_portion_cents"), "tier": result.get("tier"),
                           "quarter": result.get("quarter"), "prior_credit_cents": result.get("prior_credit_cents")},
                "confidence": conf, "outcome": self.outcome(line, conf)}

    def outcome(self, line, conf) -> dict:
        status = queries.line_status(self.ro, line["id"], self.now)
        files = [f["id"] for f in queries.fetch_all(self.ro, "SELECT id, lines_json FROM payroll_files WHERE known_at <= ?",
                                                    (self.now,)) if line["id"] in f["lines_json"]]
        asked = [a for a in self.needs_you_items() if line["deal_id"] in (a.get("deals") or [])]
        if conf and conf["hold"] or conf and conf["band"] == "held" or asked:
            first = asked[0] if asked else None
            return {"status": "held", "line_status": status, "files": files,
                    "text": f"Asked {config.display_name(first['person'])}: {first['question']}" if first
                    else (conf["hold"]["reason"] if conf and conf["hold"] else "held"),
                    "asked": first}
        text = f"In {files[-1]} payout file" if files else ("Approved, waiting for the next payout file"
                                                             if status == "approved" else f"Line is {status}")
        return {"status": "cleared", "line_status": status, "files": files, "text": text, "asked": None}

    # ---------- sources rail (D2) ----------

    def sources(self) -> list[dict]:
        count = lambda sql, *a: queries.fetch_one(self.ro, sql, a)["n"]
        evidence = getattr(self.world, "evidence", None)
        out = [
            {"kind": "contracts", "label": "Contracts", "count": count(
                "SELECT COUNT(DISTINCT id) n FROM contracts WHERE known_at <= ?", self.now), "via": "e-signature (PDF/CSV)",
             "last_sync": queries.fetch_one(self.ro, "SELECT MAX(known_at) t FROM contracts WHERE known_at <= ?", (self.now,))["t"]},
            {"kind": "messages", "label": "Email & Slack", "count": len(getattr(evidence, "docs", {})) if evidence else 0,
             "via": "Elastic quota-comms" if getattr(evidence, "name", "") == "elastic" else "local index (offline)",
             "last_sync": self.now},
            {"kind": "crm", "label": "CRM", "count": count("SELECT COUNT(DISTINCT id) n FROM deals WHERE known_at <= ?", self.now),
             "via": "Salesforce export", "last_sync": queries.fetch_one(
                 self.ro, "SELECT MAX(known_at) t FROM deals WHERE known_at <= ?", (self.now,))["t"]},
            {"kind": "hr", "label": "HR", "count": count("SELECT COUNT(DISTINCT id) n FROM people WHERE known_at <= ?", self.now),
             "via": self._hr_via(), "last_sync": queries.fetch_one(
                 self.ro, "SELECT MAX(known_at) t FROM people WHERE known_at <= ?", (self.now,))["t"]},
            {"kind": "stripe", "label": "Stripe", "count": count("SELECT COUNT(*) n FROM billing WHERE known_at <= ?", self.now),
             "via": "Stripe sandbox" if queries.fetch_one(self.ro, "SELECT 1 FROM billing WHERE external_ref LIKE 'in\\_1%' "
                                                                    "ESCAPE '\\' OR external_ref LIKE 'pi\\_%' ESCAPE '\\'")
             else "billing mirror (simulated; serve --stripe for the sandbox)",
             "last_sync": queries.fetch_one(self.ro, "SELECT MAX(known_at) t FROM billing WHERE known_at <= ?", (self.now,))["t"]},
        ]
        backtest = Path("out/backtest_2026Q1.json")
        if backtest.exists():
            b = json.loads(backtest.read_text(encoding="utf-8"))
            out.append({"kind": "payouts", "label": "Q1 payout spreadsheet", "count": b["rows"], "via": "payouts_2026Q1.csv",
                        "detail": b["headline"], "last_sync": None})
        return out

    @staticmethod
    def _hr_via() -> str:
        import os
        url = os.environ.get("CLONE_URL")
        if not url:
            return "HR records in the ledger (BambooHR clone not connected)"
        try:
            import urllib.request
            headers = {"Authorization": f"Bearer {os.environ['CLONE_ADMIN_TOKEN']}"} if os.environ.get("CLONE_ADMIN_TOKEN") else {}
            urllib.request.urlopen(urllib.request.Request(url + "/__clone/health", headers=headers), timeout=1)
            return "BambooHR clone API"
        except Exception:
            return "HR records in the ledger (BambooHR clone not reachable)"

    def source_rows(self, kind, q=None, limit=200) -> dict:
        q = (q or "").lower()
        if kind == "messages":
            evidence = getattr(self.world, "evidence", None)
            found = {f["doc_id"]: f for d in self.analyst.decisions(kind="commitment_check")
                     for f in _loads(d["output_json"]).get("args", {}).get("findings", [])}
            rows = [{**{k: m.get(k) for k in ("doc_id", "kind", "account", "author", "sent_at", "subject", "body")},
                     "highlight": [found[m["doc_id"]]["quote"]] if m["doc_id"] in found else [],
                     "ref": f"{m.get('source_file') or 'messages.jsonl'} · row {m.get('source_row')}"}
                    for m in (getattr(evidence, "docs", {}) or {}).values() if m["sent_at"] <= self.now]
            columns = ["doc_id", "kind", "account", "author", "sent_at", "subject", "body"]
        elif kind == "payouts":
            import csv
            path = Path("inbox/payouts_2026Q1.csv")
            raw = list(csv.DictReader(path.read_text(encoding="utf-8-sig").splitlines())) if path.exists() else []
            columns = list(raw[0]) if raw else []
            rows = [{**r, "highlight": ["Commissionable Base", "Commission"], "ref": f"payouts_2026Q1.csv · row {n}"}
                    for n, r in enumerate(raw, start=1)]
        else:
            table, highlight = {"contracts": ("contracts", ["acv_cents", "term_months", "countersigned_at", "clauses_json"]),
                                "crm": ("deals", ["crm_amount_cents", "crm_close_date", "owner_id", "splits_json"]),
                                "hr": ("people", ["role", "state", "start_date", "end_date"]),
                                "stripe": ("billing", ["kind", "amount_cents", "external_ref"])}[kind]
            data = (queries.latest_rows(self.ro, table, self.now) if table in queries.VERSIONED
                    else queries.fetch_all(self.ro, f"SELECT * FROM {table} WHERE known_at <= ? ORDER BY id", (self.now,)))
            columns = [c for c in (data[0].keys() if data else []) if c not in ("source_file", "source_row")]
            rows = [{**{c: r[c] for c in columns}, "highlight": highlight, "ref": self._ref(table, r)} for r in data]
        if q:
            rows = [r for r in rows if q in json.dumps(r, default=str).lower()]
        return {"kind": kind, "columns": columns, "total": len(rows), "rows": rows[:limit]}

    # ---------- reps board (D3), rep detail and contract cards (D4) ----------

    def reps(self) -> list[dict]:
        quarter = calculator.quarter_of(self.now)
        out = []
        for person in queries.latest_rows(self.ro, "people", self.now):
            if person["role"] != "AE":
                continue
            rep = person["id"]
            deals = pipeline.open_deals(rep)
            lines = [l for l in self.analyst.lines(rep_id=rep) if l["amount_cents"] > 0
                     and queries.line_status(self.ro, l["id"], self.now) not in ("superseded", "clawed_back")]
            pending = [l for l in lines if queries.line_status(self.ro, l["id"], self.now) in ("draft", "approved")]
            confs = [c for c in (self.confidence(l["id"]) for l in lines) if c]
            statement = views.statement(self.world, rep)
            q = next((x for x in statement["quarters"] if x["quarter"] == quarter), None)
            bands = {c["band"] for c in confs} | ({"held"} if any(c["hold"] for c in confs) else set())
            status = "Held" if "held" in bands else "Note" if "note" in bands else "Cleared"
            out.append({"rep": rep, "name": person["name"], "state": person["state"], "left": person["end_date"],
                        "pipeline": {"total_cents": sum(d["amount_cents"] for d in deals),
                                     "by_stage": pipeline.by_stage(deals), "deals": len(deals), "simulated": True},
                        "booked_quarter_cents": sum(l["credit_cents"] for l in lines if l["booking_quarter"] == quarter),
                        "quarter": quarter, "attainment_bps": q["attainment_bps"] if q else 0,
                        "pending_cents": sum(l["amount_cents"] for l in pending),
                        "lowest_confidence": min((c["score"] for c in confs), default=None), "status": status})
        rank = {"Held": 0, "Note": 1, "Cleared": 2}
        return sorted(out, key=lambda r: (rank[r["status"]], -r["pending_cents"], r["rep"]))

    def rep_detail(self, rep) -> dict | None:
        person = queries.as_of(self.ro, "people", rep, self.now)
        if person is None:
            return None
        deals = [d for d in queries.latest_rows(self.ro, "deals", self.now) if queries.credits_rep(d, rep)]
        return {"rep": rep, "name": person["name"], "title": person["role"], "state": person["state"],
                "manager": person["manager_id"], "left": person["end_date"],
                "pipeline": {"simulated": True, "stages": pipeline.STAGES, "deals": pipeline.open_deals(rep)},
                "contracts": [c for c in (self.contract(d["id"], viewer=rep) for d in deals) if c]}

    def contract(self, deal_id, viewer=None) -> dict | None:
        deal = self.analyst.get("deals", deal_id)
        versions = queries.fetch_all(self.ro, "SELECT * FROM contracts WHERE deal_id = ? AND known_at <= ? ORDER BY version",
                                     (deal_id, self.now))
        if deal is None or not versions:
            return None
        originals = [c for c in versions if not _loads(c["clauses_json"]).get("amendment")]
        base = (originals or versions)[-1]
        clauses = _loads(base["clauses_json"])
        billing = queries.fetch_all(self.ro, "SELECT * FROM billing WHERE deal_id = ? AND known_at <= ? ORDER BY id",
                                    (deal_id, self.now))
        overdue = [d for d in self.analyst.decisions(kind="collection_hold", subject_id=deal_id)]
        lines = [l for l in self.analyst.lines(deal_id=deal_id) if l["amount_cents"] > 0
                 and queries.line_status(self.ro, l["id"], self.now) not in ("superseded",)]
        main = [l for l in lines if "+" not in (self.calc_of(l) or {}).get("subject_id", "")]
        recipients = []
        for share in _loads(deal["splits_json"]):
            line = next((l for l in main if l["rep_id"] == share["rep"]), None)
            recipients.append({"kind": "rep", "who": share["rep"], "name": config.display_name(share["rep"]),
                               "share_bps": share["bps"], "paid_via": "payroll", "line": line["id"] if line else None,
                               "confidence": self.confidence(line["id"]) if line else None})
        plan = calculator.plan_for({"plans": queries.fetch_all(self.ro, "SELECT * FROM plans WHERE rep_id = ? AND "
                                                                        "known_at <= ?", (deal["owner_id"], self.now))},
                                   deal["owner_id"], base["contract_date"] or "2026-01-01") if deal["owner_id"] else None
        override = (plan.raw.get("manager_override") or {}) if plan else {}
        if override.get("manager"):
            recipients.append({"kind": "manager_override", "who": override["manager"],
                               "name": config.display_name(override["manager"]), "share_bps": None,
                               "rate_bps": round(float(override.get("rate", 0)) * 10000), "paid_via": None,
                               "note": "in the signed plan; not paid by Quota yet", "line": None, "confidence": None})
        children = []
        for c in versions:
            terms = _loads(c["clauses_json"]).get("amendment")
            if terms and terms.get("kind") == "amendment":
                key = f"{deal_id}+v{c['version']}"
                inc = [l for l in lines if (self.calc_of(l) or {}).get("subject_id", "").startswith(key)]
                children.append({"id": key, "version": c["version"], "signed_at": terms.get("signed_at"),
                                 "acv_cents": c["acv_cents"], "incremental_lines": [l["id"] for l in inc],
                                 "incremental_commission_cents": sum(l["amount_cents"] for l in inc),
                                 # who is credited on the increase (the deal's own splits: an amendment has no
                                 # separate seller in the engine)
                                 "recipients": [{"kind": "rep", "who": l["rep_id"], "name": config.display_name(l["rep_id"]),
                                                 "line": l["id"], "amount_cents": l["amount_cents"],
                                                 "confidence": self.confidence(l["id"])} for l in inc]})
        return {"id": deal_id, "customer": deal["account"], "acv_cents": base["acv_cents"], "term_months": base["term_months"],
                "billing": clauses.get("billing"), "ramp": clauses.get("ramp"),
                "clauses": {k: v for k, v in clauses.items() if k not in ("billing", "ramp", "amendment")},
                "countersigned_at": base["countersigned_at"],
                "countersigned_local": clk.local_date(base["countersigned_at"]) if base["countersigned_at"] else None,
                "paid": any(b["kind"] == "payment" for b in billing), "overdue": bool(overdue),
                "lines": [{"id": l["id"], "rep": l["rep_id"], "amount_cents": l["amount_cents"],
                           "quarter": l["booking_quarter"], "status": queries.line_status(self.ro, l["id"], self.now)}
                          for l in main], "viewer": viewer,
                "recipients": recipients, "children": children, "parent": None,
                "split_bar": [{"who": r["who"], "share_bps": r["share_bps"], "line": r["line"]} for r in recipients
                              if r["share_bps"]]}

    # ---------- Needs you (D5) ----------

    def needs_you_items(self) -> list[dict]:
        items = []
        for a in queries.pending_approvals(self.ro, self.now):
            chain, deals = self._chain(a), self._deals_of(a)
            items.append({"id": a["id"], "kind": a["kind"], "person": a["approver"],
                          "question": self._question(a), "impact_cents": a["amount_cents"], "deals": deals,
                          "evidence": self._evidence(a), "chain": chain, "answer": "approve_reject"})
        for deal_id, hold in self.analyst.commitment_holds().items():
            if not any(deal_id in i["deals"] for i in items):
                items.append({"id": hold["id"], "kind": "commitment_check", "person": PERSON_OF["commitment_review"],
                              "question": hold["explanation"], "impact_cents": None, "deals": [deal_id],
                              "evidence": [{"decision": hold["id"], "text": hold["explanation"]}], "chain": [],
                              "answer": "reply"})
        return items

    def needs_you(self) -> dict:
        items = self.needs_you_items()
        people = {}
        for item in items:
            people.setdefault(item["person"], []).append(item)
        order = ["priya.nair", "marcus.lee", "dana.ortiz", "grace.liu"]
        return {"count": len(items), "people": [{"person": p, "name": config.display_name(p), "title": config.title(p),
                                                 "items": people[p]}
                                                for p in sorted(people, key=lambda p: (order.index(p) if p in order else 9, p))]}

    def _root(self, approval):
        while approval and approval["parent_id"]:
            approval = queries.as_of(self.ro, "approvals", approval["parent_id"], self.now)
        return approval

    def _chain(self, approval) -> list[dict]:
        root, out = self._root(approval), []
        ids = queries.chain_ids(self.ro, root["id"], self.now)
        for i in ids:
            a = queries.as_of(self.ro, "approvals", i, self.now)
            decided = queries.approval_decision(self.ro, i, self.now)
            out.append({"at": a["known_at"], "text": f"{i} to {config.display_name(a['approver'])}"
                        + (f": {decided['decision']} ({decided['result']})" if decided else ": waiting")})
        for r in queries.fetch_all(self.ro, "SELECT * FROM refusals WHERE known_at <= ? ORDER BY id", (self.now,)):
            if r["subject_id"] in ids:
                out.append({"at": r["known_at"], "text": f"{r['id']} refused: {', '.join(_loads(r['reason_codes_json']))}"})
        return sorted(out, key=lambda e: e["at"])

    def _deals_of(self, approval) -> list[str]:
        subject = approval["subject_id"]
        if subject.startswith("D-"):
            d = queries.as_of(self.ro, "decisions", subject, self.now)
            return [d["subject_id"]] if d else []
        if subject.startswith("JE-"):
            je = queries.as_of(self.ro, "journal_entries", subject, self.now)
            d = queries.as_of(self.ro, "decisions", je["decision_id"], self.now) if je and je["decision_id"] else None
            line = self.line(d["subject_id"]) if d and d["subject_id"].startswith("CL-") else None
            return [line["deal_id"]] if line else []
        if subject.startswith("M-"):
            calcs = [d for d in self.analyst.decisions(kind="dispute") + self.analyst.decisions(kind="escalation")]
            lines = [self.line(d["subject_id"]) for d in calcs if d["subject_id"].startswith("CL-")]
            return sorted({l["deal_id"] for l in lines if l})
        return []

    def _evidence(self, approval) -> list[dict]:
        subject = approval["subject_id"]
        d = queries.as_of(self.ro, "decisions", subject, self.now) if subject.startswith("D-") else None
        return [{"decision": d["id"], "text": d["explanation"]}] if d else [{"ref": subject}]

    def _question(self, approval) -> str:
        # The seat's own question about this approval (the first message naming it), not a digest or a reminder.
        sent = [m["text"] for m in queries.fetch_all(
            self.ro, "SELECT text FROM messages WHERE to_principal = ? AND author LIKE 'seat:%' AND known_at <= ? AND "
                     "text LIKE ? ORDER BY id", (approval["approver"], self.now, f"%{approval['id']}%"))
            if not m["text"].startswith(("Daily digest", "Reminder"))]
        if sent:
            return sent[0]
        return {"plan_exception": "Approve this plan exception?", "journal_entry": "Approve this journal entry?",
                "payroll_file": "Approve this payout file?",
                "commitment_review": "Is this promise binding? Approve if it is not; amend the contract if it is."}.get(
            approval["kind"], f"Decide {approval['kind']} {approval['subject_id']}?")

    def answer(self, item_id, as_principal, decision, text) -> dict:
        approval = queries.as_of(self.ro, "approvals", item_id, self.now)
        world = self.world
        if approval is not None and decision in ("approve", "reject"):
            fn = world.api.approve if decision == "approve" else world.api.reject
            out = fn(as_principal, item_id, text or ("Approved" if decision == "approve" else "Rejected"))
            world.settle()
            return {"id": item_id, "ok": out is not None, "result": out,
                    "refused": None if out is not None else "the write path refused it; see the timeline"}
        world.api.message(as_principal, "seat:commissions_analyst", text or "")
        world.settle()
        return {"id": item_id, "ok": True, "result": {"replied": True}}

    # ---------- Home KPIs (D2) ----------

    def home(self) -> dict:
        lines = [l for l in queries.fetch_all(self.ro, "SELECT * FROM commission_lines WHERE known_at <= ? AND "
                                                       "amount_cents > 0", (self.now,))
                 if queries.line_status(self.ro, l["id"], self.now) in ("draft", "approved")]
        held, by_reason, cleared = 0, {}, 0
        for l in lines:
            c = self.confidence(l["id"])
            if c and (c["hold"] or c["band"] == "held"):
                held += l["amount_cents"]
                reason = c["hold"]["kind"] if c["hold"] else min(c["factors"], key=lambda f: f["value"])["name"]
                by_reason[reason] = by_reason.get(reason, 0) + 1
            else:
                cleared += 1
        return {"clock": self.now, "period": clk.period_of(self.now), "pending_cents": sum(l["amount_cents"] for l in lines),
                "pending_lines": len(lines), "cleared_pct": round(100 * cleared / len(lines), 1) if lines else 100.0,
                "held_cents": held, "held_by_reason": by_reason, "needs_you": len(self.needs_you_items())}

