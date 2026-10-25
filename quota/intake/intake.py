"""`python -m quota intake inbox/` (R6): read a folder of exports into a ledger, every fact with its file and row.

For each file: classify it by its headers, propose a column mapping (the model with headers and five sample
rows when it is available, else `synonyms`), and verify the mapping in code: at least 98% of the column's
values parse, the parsed type fits the field, and no two columns claim one field. A mapping that fails a check
or sits below 85% confidence is a review item and its file is not loaded. Approved mappings are saved in the
inbox (`_mappings.json`) and reused when the same headers come back, so a second run asks nothing; resolved
review items are saved beside them (`_reviews.json`).

Row-level mess is handled in code and logged: exact duplicate rows and duplicate Stripe ids are dropped, a
blank opportunity owner is held, a Stripe row without metadata is matched to its deal by invoice, and a DocuSign
row without an opportunity id goes to review. Everything that loads goes through `WritePath.append_fact`.
"""

import csv
import json
from pathlib import Path

from quota import clock as clk
from quota import config
from quota.engine import routing
from quota.intake import formats, plans, synonyms
from quota.ledger import db
from quota.ledger.write_path import WritePath

INTAKE = "system:intake"
INTAKE_AT = "2026-07-01T00:00:00Z"          # when the inbox was read (after the last fact it carries)
MIN_PARSE_RATE, MIN_CONFIDENCE = 0.98, 0.85
PRINCIPAL = {INTAKE: {"kind": "intake", "limit": None, "capabilities": frozenset({"write:fact", "post:source_entry"})}}
LOAD_ORDER = ["hr_roster", "opportunities", "contracts", "esign", "stripe"]

MAPPING_SCHEMA = {
    "type": "object",
    "properties": {"mappings": {"type": "array", "items": {
        "type": "object",
        "properties": {"source_column": {"type": "string"}, "canonical_field": {"type": "string"},
                       "transform": {"type": "string", "enum": sorted(formats.TRANSFORMS)},
                       "confidence": {"type": "number"}},
        "required": ["source_column", "canonical_field", "transform", "confidence"], "additionalProperties": False}}},
    "required": ["mappings"], "additionalProperties": False,
}
MAPPING_SYSTEM = ("You map the columns of a business export to canonical fields. Use only the fields listed. Give "
                  "a confidence from 0 to 1; below 0.85 means a person should check it. Leave out a column that "
                  "maps to nothing.")


def read_csv(path: Path) -> tuple[list[str], list[list[str]], list[str]]:
    """(headers, rows, raw lines): the BOM is dropped; raw lines are kept for the trace's source cards."""
    text = path.read_text(encoding="utf-8-sig")
    lines = text.splitlines()
    rows = list(csv.reader(lines))
    return rows[0], rows[1:], lines[1:]


def model_mappings(llm, kind, headers, sample) -> list[dict]:
    fields = {f: t for f, (t, _, _) in synonyms.FIELDS[kind].items()}
    prompt = (f"Export kind: {kind}\nCanonical fields (field: transform): {json.dumps(fields)}\n"
              f"Headers: {json.dumps(headers)}\nSample rows:\n" + "\n".join(json.dumps(r) for r in sample))
    out = llm.complete_json(MAPPING_SYSTEM, prompt, MAPPING_SCHEMA)
    return [{**m, "proposed_by": "model"} for m in out["mappings"] if m["canonical_field"] in fields]


def verify(mapping: list[dict], kind: str, headers: list[str], rows: list[list[str]]) -> list[dict]:
    """Each mapping with its checks: parse rate over non-blank values, the parsed type, a field claimed twice."""
    index = {h.strip(): i for i, h in enumerate(headers)}
    claimed: dict[str, int] = {}
    for m in mapping:
        claimed[m["canonical_field"]] = claimed.get(m["canonical_field"], 0) + 1
    out = []
    for m in mapping:
        column, transform = index.get(m["source_column"]), formats.TRANSFORMS.get(m["transform"])
        values = [r[column] for r in rows if column is not None and column < len(r) and r[column].strip()]
        parsed, ok = [], 0
        for value in values:
            try:
                parsed.append(transform(value))
                ok += 1
            except (ValueError, TypeError):
                pass
        rate = ok / len(values) if values else 1.0
        want = synonyms.FIELD_TYPES.get(synonyms.FIELDS[kind][m["canonical_field"]][0], str)
        type_ok = all(isinstance(v, want) or v is None for v in parsed)
        problems = [p for p, bad in (("column not found", column is None or transform is None),
                                     (f"parse rate {rate:.1%} < 98%", rate < MIN_PARSE_RATE),
                                     ("parsed type does not fit the field", not type_ok),
                                     ("two columns map to this field", claimed[m["canonical_field"]] > 1),
                                     (f"confidence {m['confidence']:.2f} < 0.85", m["confidence"] < MIN_CONFIDENCE))
                    if bad]
        out.append({**m, "parse_rate": round(rate, 4), "type_ok": type_ok, "problems": problems,
                    "status": "approved" if not problems else "review"})
    return out


class Intake:
    def __init__(self, inbox, llm=None):
        self.inbox, self.llm = Path(inbox), llm
        self.saved = self._load_json("_mappings.json")
        self.resolved = self._load_json("_reviews.json")
        self.files, self.review_items, self.log = [], [], []
        self.records: dict[str, list[dict]] = {}       # kind -> mapped rows (with file, row, raw)
        self.raw: dict[tuple[str, int], str] = {}      # (file, row) -> the CSV line, for source cards
        self.plans: dict[str, dict] = {}

    def _load_json(self, name) -> dict:
        path = self.inbox / name
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def _save(self) -> None:
        (self.inbox / "_mappings.json").write_text(json.dumps(self.saved, indent=2), encoding="utf-8")
        (self.inbox / "_reviews.json").write_text(json.dumps(self.resolved, indent=2), encoding="utf-8")

    def review(self, item_id, kind, routed_to, question, **detail) -> None:
        if item_id in self.resolved:
            return
        self.review_items.append({"id": item_id, "kind": kind, "routed_to": routed_to, "question": question,
                                  **detail})

    # ---------- files and mappings ----------

    def map_file(self, path: Path) -> None:
        headers, rows, raw = read_csv(path)
        kind, score = synonyms.classify(headers)
        entry = {"file": path.name, "kind": kind, "classified_share": round(score, 3), "rows": len(rows)}
        self.files.append(entry)
        if kind is None:
            self.review(f"RV-{path.stem}-kind", "UNKNOWN_FILE", routing.owner("material_flag"),
                        f"{path.name}: no known export has these headers", file=path.name, headers=headers)
            return
        saved = self.saved.get(path.name)
        if saved and saved["headers"] == headers and saved["kind"] == kind:
            mapping, entry["mapping_source"] = saved["mappings"], "reused"
        else:
            proposed = None
            if self.llm is not None:
                from quota.agent.llm import LLMError, LLMUnavailable
                try:
                    proposed = model_mappings(self.llm, kind, headers, rows[:5])
                except (LLMError, LLMUnavailable):
                    proposed = None
            proposed = proposed or synonyms.propose(kind, headers)
            for column, fix in self.resolved.get(f"RV-{path.stem}-mapping", {}).get("mappings", {}).items():
                proposed = [m for m in proposed if m["source_column"] != column] + [
                    {"source_column": column, **fix, "confidence": 1.0, "proposed_by": "review"}]
            mapping = verify(proposed, kind, headers, rows)
            entry["mapping_source"] = mapping[0]["proposed_by"] if mapping else "none"
            missing = [f for f, (_, req, _) in synonyms.FIELDS[kind].items()
                       if req and f not in {m["canonical_field"] for m in mapping if m["status"] == "approved"}]
            if missing or any(m["status"] == "review" for m in mapping):
                self.review(f"RV-{path.stem}-mapping", "MAPPING", routing.owner("material_flag"),
                            f"{path.name}: check the mapping before it loads", file=path.name,
                            missing_fields=missing, mappings=[m for m in mapping if m["status"] == "review"])
                entry["mappings"], entry["loaded"] = mapping, False
                return
            self.saved[path.name] = {"kind": kind, "headers": headers, "mappings": mapping}
        entry["mappings"], entry["loaded"] = mapping, True
        index = {h.strip(): i for i, h in enumerate(headers)}
        out = self.records.setdefault(kind, [])
        for n, (row, line) in enumerate(zip(rows, raw), start=1):
            self.raw[(path.name, n)] = line
            record = {"_file": path.name, "_row": n, "_raw": tuple(row)}
            for m in mapping:
                value = row[index[m["source_column"]]] if index[m["source_column"]] < len(row) else ""
                try:
                    record[m["canonical_field"]] = formats.TRANSFORMS[m["transform"]](value) if value.strip() else None
                except (ValueError, TypeError):
                    record[m["canonical_field"]] = None
            out.append(record)

    def read_plans(self) -> None:
        for pdf in sorted((self.inbox / "plans").glob("*.pdf")):
            rep = pdf.stem
            found = plans.extract(plans.read_pdf(pdf), self.llm)
            plan, ambiguous = plans.to_plan(found)
            self.plans[rep] = {"file": f"plans/{pdf.name}", "terms": found, "plan": plan}
            for item in ambiguous:
                self.review(f"RV-plan-{rep}-{item['key']}", "AMBIGUOUS", routing.owner("plan_config"),
                            item["question"], file=f"plans/{pdf.name}", rep=rep, section=item["section"],
                            quote=item["quote"], key=item["key"])

    # ---------- rows ----------

    def dedupe(self, kind, key=None) -> list[dict]:
        seen, out = {}, []
        for r in self.records.get(kind, []):
            k = r["_raw"] if key is None else r[key]
            if k in seen:
                self.log.append(f"{r['_file']} row {r['_row']}: dropped, duplicate of row {seen[k]}"
                                + ("" if key is None else f" ({key} {k})"))
                continue
            seen[k] = r["_row"]
            out.append(r)
        return out

    def rows_for_load(self) -> dict[str, list[tuple[dict, dict]]]:
        """table -> [(fact row, record)] after the row-level rules."""
        people = self.dedupe("hr_roster")
        by_name = {p["name"]: p["email"].split("@")[0] for p in people}
        out = {"people": [], "deals": [], "contracts": [], "esign_events": [], "billing": []}
        for p in people:
            out["people"].append(({"id": p["email"].split("@")[0], "name": p["name"], "role": p["role"],
                                   "state": p["state"] or None, "manager_id": by_name.get(p["manager"]) if p["manager"]
                                   else None, "start_date": p["start_date"], "end_date": p["end_date"],
                                   "source": "hr"}, p))
        for d in self.dedupe("opportunities"):
            if not d["owner"]:
                fix = self.resolved.get(f"RV-{Path(d['_file']).stem}-{d['_row']}-owner", {})
                if not fix.get("owner"):
                    self.review(f"RV-{Path(d['_file']).stem}-{d['_row']}-owner", "BLANK_OWNER", routing.owner("splits"),
                                f"{d['deal_id']} ({d['account']}) has no opportunity owner; who owns it?",
                                file=d["_file"], row=d["_row"], deal_id=d["deal_id"])
                    self.log.append(f"{d['_file']} row {d['_row']}: held, blank owner on {d['deal_id']}")
                    continue
                d = {**d, "owner": config.display_name(fix["owner"]) if fix["owner"] not in by_name.values()
                     else next(n for n, i in by_name.items() if i == fix["owner"])}
            out["deals"].append(({"id": d["deal_id"], "account": d["account"], "crm_amount_cents": d["crm_amount_cents"],
                                  "crm_close_date": d["crm_close_date"], "stage": d["stage"],
                                  "owner_id": by_name.get(d["owner"], d["owner"]),
                                  "splits_json": [{"rep": by_name.get(s["name"], s["name"]), "bps": s["bps"]}
                                                  for s in d["splits"]], "source": "crm"}, d))
        for c in self.dedupe("contracts"):
            out["contracts"].append(({"id": c["deal_id"], "deal_id": c["deal_id"], "acv_cents": c["acv_cents"],
                                      "term_months": c["term_months"], "contract_date": clk.local_date(c["countersigned_at"]),
                                      "countersigned_at": c["countersigned_at"], "clauses_json": c["clauses"],
                                      "doc_ref": f"contracts/{c['deal_id']}-msa.pdf", "source": "esign"}, c))
        for e in self.dedupe("esign"):
            if not e["deal_id"]:
                fix = self.resolved.get(f"RV-{Path(e['_file']).stem}-{e['_row']}-deal", {})
                if not fix.get("deal_id"):
                    self.review(f"RV-{Path(e['_file']).stem}-{e['_row']}-deal", "NO_OPPORTUNITY",
                                routing.owner("splits"), f"Envelope {e['envelope_id']} ({e['event']}) names no "
                                f"opportunity; which deal is it?", file=e["_file"], row=e["_row"],
                                envelope_id=e["envelope_id"])
                    self.log.append(f"{e['_file']} row {e['_row']}: to review, no opportunity id")
                    continue
                e = {**e, "deal_id": fix["deal_id"]}
            out["esign_events"].append(({"id": f"{e['envelope_id']}-{e['event']}", "envelope_id": e["envelope_id"],
                                         "deal_id": e["deal_id"], "event": e["event"], "at": e["at"]}, e))
        stripe = self.dedupe("stripe", key="id")
        invoices = {s["id"]: s["deal_id"] for s in stripe if s["kind"] == "invoice" and s["deal_id"]}
        kinds = {"invoice": "invoice", "charge": "payment", "refund": "refund"}
        for s in stripe:
            deal = s["deal_id"]
            if not deal:
                deal = invoices.get(s["invoice"])
                self.log.append(f"{s['_file']} row {s['_row']}: no metadata; matched to {deal} by invoice {s['invoice']}")
            out["billing"].append(({"id": s["id"], "kind": kinds[s["kind"]], "deal_id": deal,
                                    "amount_cents": s["amount_cents"], "at": s["at"], "external_ref": s["id"]}, s))
        return out

    # ---------- the run ----------

    def run(self, db_path=None):
        """Classify, map, verify and load. Returns (connection to the intake ledger, report)."""
        for path in sorted(self.inbox.glob("*.csv")):
            self.map_file(path)
        self.read_plans()
        uri = db_path or db.memory_uri(f"quota-intake-{id(self)}")
        conn = db.connect(uri)
        db.init_db(conn)
        wp = WritePath(conn, clk.SimClock(INTAKE_AT), {**config.principals(), **PRINCIPAL})
        loaded = {}
        for table, rows in self.rows_for_load().items():
            for fact, record in rows:
                wp.append_fact(table, {**fact, "source_file": record["_file"], "source_row": record["_row"]}, INTAKE)
            loaded[table] = len(rows)
        self._save()
        self.conn, self.loaded = conn, loaded
        self.report_data = self.report(loaded)
        return conn, self.report_data

    def provenance(self) -> dict:
        """(table, id) -> {file, row, raw} for every fact the inbox carried."""
        out = {}
        for table, rows in self.rows_for_load().items():
            for fact, record in rows:
                out[(table, fact["id"])] = {"file": record["_file"], "row": record["_row"],
                                            "raw": self.raw.get((record["_file"], record["_row"]))}
        return out

    def report(self, loaded) -> dict:
        return {"files": [{k: v for k, v in f.items()} for f in self.files], "loaded": loaded,
                "review_items": self.review_items, "log": self.log,
                "history_rows": len(self.records.get("field_history", [])),
                "plans": {rep: {"file": p["file"], "plan": p["plan"],
                                "terms": {k: {kk: vv for kk, vv in t.items()} for k, t in p["terms"].items()}}
                          for rep, p in self.plans.items()}}


def resolve(inbox, item_id, resolution: dict) -> dict:
    """POST /api/review/{id}: save the answer; the next intake run applies it (a mapping fix is then saved as an
    approved mapping, so the run after that asks nothing)."""
    path = Path(inbox) / "_reviews.json"
    resolved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    resolved[item_id] = resolution
    path.write_text(json.dumps(resolved, indent=2), encoding="utf-8")
    return {"id": item_id, "resolution": resolution}
