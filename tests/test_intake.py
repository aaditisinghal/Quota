"""NEXT_TASKS R6: export a seed as messy files, read them back, learn the plans, backtest Q1."""

import csv
import json
import shutil

import pytest
from fastapi.testclient import TestClient

from quota.engine.plans import load_plan_raw
from quota.intake import backtest, export, facts, formats, plans, synonyms
from quota.intake.intake import Intake, verify
from quota.ledger import queries
from quota.sim import seed_acme
from quota.sim.generator import generate


@pytest.fixture(scope="module")
def seed7(tmp_path_factory):
    world = generate(7, reps=20, out=lambda line: None)
    world.run_until(world.end_ts)
    inbox = tmp_path_factory.mktemp("inbox")
    key = export.export(7, inbox, world=world)
    yield world, inbox, key
    world.close()


def fresh(inbox, tmp_path):
    """A copy of the exported inbox with no saved mappings or answers."""
    copy = tmp_path / "inbox"
    shutil.copytree(inbox, copy, ignore=shutil.ignore_patterns("_mappings.json", "_reviews.json"))
    return copy


def rows_of(path):
    return list(csv.reader(path.read_text(encoding="utf-8-sig").splitlines()))


def planted(key, kind):
    return next(p for p in key["planted"] if p["kind"] == kind)


def test_export_writes_real_formats_with_the_mess_planted(seed7):
    _, inbox, key = seed7
    for name, header in export.HEADERS.items():
        assert rows_of(inbox / name)[0] == header, name
    sf = inbox / "salesforce_opportunities.csv"
    assert sf.read_bytes().startswith(b"\xef\xbb\xbf")                           # BOM
    opp = rows_of(sf)
    assert opp[1][3].startswith("$") and "," in opp[1][3] and "/" in opp[1][4]
    dup = planted(key, "duplicate_row")
    assert opp[dup["row"]] == opp[dup["duplicate_of"]]
    assert opp[planted(key, "trailing_spaces_owner")["row"]][6].endswith("   ")
    assert opp[planted(key, "blank_owner")["row"]][6] == ""
    history = rows_of(inbox / "opportunity_field_history.csv")
    assert history[1][5].endswith(("AM", "PM")) and "Z" not in history[1][5]
    assert rows_of(inbox / "docusign_envelope_events.csv")[planted(key, "missing_opportunity_id")["row"]][1] == ""
    stripe = rows_of(inbox / "stripe_balance_export.csv")
    assert stripe[planted(key, "missing_metadata")["row"]][8] == ""
    assert stripe[planted(key, "duplicate_id")["row"]] == stripe[planted(key, "duplicate_id")["duplicate_of"]]
    assert "." in stripe[1][2] and "$" not in stripe[1][2]
    assert ";" in rows_of(inbox / "contracts.csv")[1][4] or "=" in rows_of(inbox / "contracts.csv")[1][4]
    assert sorted(p.name for p in (inbox / "plans").iterdir()) == ["alex.chen.pdf", "jordan.kim.pdf"]
    errors = [p for p in key["planted"] if p["file"] == "payouts_2026Q1.csv"]
    assert sorted(p["kind"] for p in errors) == ["CRM_NOT_CONTRACT", "DUPLICATE_ROW", "TCV_NOT_ACV"]
    assert all(p["rep"] != "jordan.kim" for p in errors)
    assert json.loads((inbox / "_answer_key.json").read_text())["payouts"]["errors"] == 3


def test_round_trip_equals_the_generator_except_the_planted_holds(seed7, tmp_path):
    world, inbox, key = seed7
    intake = Intake(fresh(inbox, tmp_path))
    conn, report = intake.run()
    want, got = facts.comparable(world.ro), facts.comparable(conn)
    assert {t: got[t] - want[t] for t in want} == {t: set() for t in want}      # nothing invented
    missing = {t: {row[0] for row in want[t] - got[t]} for t in want}
    assert missing == {"deals": set(key["held_deals"]), "contracts": set(), "esign_events": set(key["review_esign"]),
                       "billing": set(), "people": set()}
    assert {i["kind"] for i in report["review_items"]} == {"BLANK_OWNER", "NO_OPPORTUNITY", "AMBIGUOUS"}
    assert any("dropped, duplicate of row" in line for line in report["log"])
    assert any("matched to" in line and "by invoice" in line for line in report["log"])
    deal = queries.latest_rows(conn, "deals", facts.END)[0]
    assert deal["source_file"] == "salesforce_opportunities.csv" and deal["source_row"] >= 1


def test_field_history_reads_pacific_time_back_to_the_ledgers_utc(seed7, tmp_path):
    world, inbox, _ = seed7
    intake = Intake(fresh(inbox, tmp_path))
    intake.run()
    edits = {(r["deal_id"], r["edited_at"]) for r in intake.records["field_history"]}
    versions = {(d["id"], d["known_at"]) for d in queries.fetch_all(world.ro, "SELECT id, known_at FROM deals")}
    assert edits == versions


def test_a_second_run_reuses_the_approved_mappings(seed7, tmp_path):
    _, inbox, _ = seed7
    copy = fresh(inbox, tmp_path)
    first = Intake(copy).run()[1]
    assert {f["mapping_source"] for f in first["files"]} == {"synonyms"}
    second = Intake(copy).run()[1]
    assert {f["mapping_source"] for f in second["files"]} == {"reused"}
    assert not [i for i in second["review_items"] if i["kind"] == "MAPPING"]


def test_a_mapping_that_fails_a_check_goes_to_review():
    headers = ["Opportunity ID", "Amount", "Close Date"]
    rows = [[f"d{i}", "$1,000.00", "13/45/2026" if i < 5 else "1/2/2026"] for i in range(100)]
    mapping = [{"source_column": "Opportunity ID", "canonical_field": "deal_id", "transform": "text", "confidence": 0.99},
               {"source_column": "Amount", "canonical_field": "crm_amount_cents", "transform": "money", "confidence": 0.6},
               {"source_column": "Close Date", "canonical_field": "crm_close_date", "transform": "date_mdy",
                "confidence": 0.99},
               {"source_column": "Opportunity ID", "canonical_field": "crm_close_date", "transform": "text",
                "confidence": 0.99}]
    checked = {(m["source_column"], m["canonical_field"]): m for m in verify(mapping, "opportunities", headers, rows)}
    assert checked[("Opportunity ID", "deal_id")]["status"] == "approved"
    assert "confidence 0.60 < 0.85" in checked[("Amount", "crm_amount_cents")]["problems"]
    assert any(p.startswith("parse rate 95.0%") for p in checked[("Close Date", "crm_close_date")]["problems"])
    assert "two columns map to this field" in checked[("Opportunity ID", "crm_close_date")]["problems"]
    assert synonyms.classify(export.HEADERS["stripe_balance_export.csv"])[0] == "stripe"


def test_plan_learning_reads_jordans_plan_as_his_yaml_and_flags_the_ambiguous_one(seed7):
    _, inbox, _ = seed7
    raw = load_plan_raw(seed_acme.PLAN_PATH)
    found = plans.extract(plans.read_pdf(inbox / "plans" / "jordan.kim.pdf"))
    plan, ambiguous = plans.to_plan(found)
    assert plan == plans.yaml_terms(raw) and ambiguous == []
    assert all(t["value"] in t["quote"] and t["quote"] in plans.flat(plans.read_pdf(inbox / "plans" / "jordan.kim.pdf"))
               for t in found.values())
    _, ambiguous = plans.to_plan(plans.extract(plans.read_pdf(inbox / "plans" / "alex.chen.pdf")))
    assert [(a["key"], a["section"]) for a in ambiguous] == [("booking_date", 4)]


def test_the_models_reading_is_kept_only_where_it_is_verbatim():
    text = plans.plan_text(load_plan_raw(seed_acme.PLAN_PATH))

    class Fake:
        def complete_json(self, system, prompt, schema):
            return {"terms": [
                {"key": "quota_amount", "value": "300,000", "quote": "2. Quota. The quota is $300,000 per quarter.",
                 "section": 2},
                {"key": "pay_on", "value": "booking", "quote": "6. Payment. Commission is paid on booking.", "section": 6},
                {"key": "base_rate", "value": "12", "quote": "Commission is 10% of credited ACV", "section": 3}]}
    kept = plans.model_extract(Fake(), text)
    assert sorted(kept) == ["quota_amount"]                       # an invented quote and a value not in its quote
    assert plans.extract(text, Fake())["pay_on"]["value"] == "collection"   # the regex still reads section 6


def test_backtest_reproduces_q1_and_holds_the_three_spreadsheet_errors(seed7, tmp_path):
    _, inbox, key = seed7
    result = backtest.run(fresh(inbox, tmp_path), "2026Q1", tmp_path / "out")
    planted_rows = {p["row"]: p["kind"] for p in key["planted"] if p["file"] == "payouts_2026Q1.csv"}
    assert {f["row"]: f["class"] for f in result["findings"]} == planted_rows
    assert (result["rows"], result["matched"], result["errors"], result["unexplained"]) == (
        key["payouts"]["rows"], key["payouts"]["rows"] - 3, 3, 0)
    assert result["overpaid_cents"] == key["payouts"]["overpaid_cents"] > 0
    assert result["headline"] == (f"Reproduced {result['matched']} of {result['rows']} Q1 payouts; 3 spreadsheet "
                                  f"errors ({formats.to_money(result['overpaid_cents'])} overpaid).")
    assert {(f["outcome"], f["routed_to"]) for f in result["findings"]} == {("held", "priya.nair")}
    assert json.loads((tmp_path / "out" / "backtest_2026Q1.json").read_text())["headline"] == result["headline"]


def test_intake_api_and_source_cards(seed7, tmp_path):
    from quota.api.app import create_app
    from quota.api.session import Session
    world, inbox, key = seed7
    session = Session(world, "fernhill", seed=7)
    copy = fresh(inbox, tmp_path)
    session.attach_inbox(copy)
    client = TestClient(create_app(session, static_dir="no-static"))
    view = client.get("/api/intake").json()
    assert view["backtest"]["errors"] == 3 and view["backtest"]["headline"].startswith("Reproduced")
    assert {f["kind"] for f in view["files"]} >= {"opportunities", "stripe", "payouts"}
    blank = next(i for i in view["review_items"] if i["kind"] == "BLANK_OWNER")
    owner = next(d for d in facts.latest(world.ro, "deals") if d["id"] == blank["deal_id"])["owner_id"]
    after = client.post(f"/api/review/{blank['id']}", json={"resolution": {"owner": owner}}).json()
    assert blank["id"] not in {i["id"] for i in after["review_items"]}
    assert ("deals", blank["deal_id"]) in session.provenance
    assert client.post("/api/review/RV-nope", json={"resolution": {}}).status_code == 404
    calc = queries.fetch_one(world.ro, "SELECT id FROM decisions WHERE kind = 'commission_calc' ORDER BY id")
    graph = client.get(f"/trace/decision/{calc['id']}").json()
    cards = [n for n in graph["nodes"] if n["kind"] == "input" and n["table"] == "deals"]
    assert cards and cards[0]["source"]["file"] == "salesforce_opportunities.csv"
    assert cards[0]["source"]["raw"].startswith(cards[0]["snippet"]["id"] + ",")


def test_a_low_confidence_model_mapping_is_reviewed_then_saved_and_reused(seed7, tmp_path):
    from quota.intake.intake import resolve
    _, inbox, _ = seed7
    copy = fresh(inbox, tmp_path)

    class Model:       # proposes what the synonyms would, but is unsure of the Split column (as the live model was)
        def complete_json(self, system, prompt, schema):
            if "terms" in schema["properties"]:
                return {"terms": []}
            headers = json.loads(prompt.split("Headers: ")[1].split("\n")[0])
            kind = prompt.split("Export kind: ")[1].split("\n")[0]
            return {"mappings": [{k: m[k] for k in ("source_column", "canonical_field", "transform")}
                                 | {"confidence": 0.8 if m["source_column"] == "Split" else 0.95}
                                 for m in synonyms.propose(kind, headers)]}
    first = Intake(copy, Model()).run()[1]
    item = next(i for i in first["review_items"] if i["kind"] == "MAPPING")
    assert item["file"] == "salesforce_opportunities.csv" and first["loaded"]["deals"] == 0
    resolve(copy, item["id"], {"mappings": {"Split": {"canonical_field": "splits", "transform": "splits"}}})
    second = Intake(copy, Model()).run()[1]
    assert second["loaded"]["deals"] == 240 and not [i for i in second["review_items"] if i["kind"] == "MAPPING"]
    third = Intake(copy, Model()).run()[1]
    assert next(f for f in third["files"] if f["file"] == "salesforce_opportunities.csv")["mapping_source"] == "reused"
