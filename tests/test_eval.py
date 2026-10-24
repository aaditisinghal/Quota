"""Phase 3: answer key, eval scoring, pushback test, CLI."""

import json
import re
from collections import Counter

import pytest

from quota import eval as evaluation
from quota.__main__ import main
from quota.sim.generator import Fernhill

TYPES = {f"E{n:02d}" for n in range(1, 17)}


def test_answer_key_plants_40_cases_of_every_type():
    fernhill = Fernhill(7)
    key = fernhill.plant_all()
    counts = Counter(c["type"] for c in key)
    assert len(key) == 40 and set(counts) == TYPES and min(counts.values()) >= 2
    required = {"case_id", "type", "deal_id", "rep_id", "expected_outcome", "expected_amount_cents", "expected_asks",
                "note"}
    assert all(required <= set(c) for c in key)
    assert {c["expected_outcome"] for c in key} <= {"held", "escalated", "corrected", "blocked", "posted"}
    assert all(isinstance(c["expected_amount_cents"], int) for c in key)
    deals = [c["deal_id"] for c in key if c["deal_id"]] + [d for c in key for d in c.get("deal_ids", [])[1:]]
    assert len(deals) == len(set(deals))                     # no deal carries two cases
    assert [c["case_id"] for c in key if c["type"] == "E03"] == ["E03-0001", "E03-0002", "E03-0003"]
    assert Fernhill(7).plant_all() == key                    # same seed, same key


def test_too_few_reps_to_plant():
    with pytest.raises(ValueError):
        Fernhill(7, reps=8).plant_all()


def test_corrected_means_the_crm_was_overridden():
    def calc(field, basis, value, chosen):
        return {"kind": "commission_calc", "resolutions_json": json.dumps(
            {field: {"value": chosen, "disagreements": [{"basis": basis, "value": value, "effect_cents": 5}]}})}
    assert evaluation.corrected(calc("base", "crm", 15_000_000, 12_000_000))
    assert evaluation.corrected(calc("booking_date", "crm_close_date", "2026-03-31T23:59:59Z", "2026-04-02T17:14:00Z"))
    assert not evaluation.corrected(calc("booking_date", "crm_close_date", "2026-04-02T23:59:59Z",
                                         "2026-04-02T17:14:00Z"))
    assert not evaluation.corrected(calc("booking_date", "contract_date", "2026-04-01T00:00:00Z", "2026-04-02T17:14:00Z"))


@pytest.fixture(scope="module")
def card():
    return evaluation.run_eval(7, pushback=False)


def test_seed_7_scorecard(card):
    assert card["planted"] == 40
    assert card["misses"] == [], [(m["case_id"], m["got_outcome"], m["got_amount_cents"]) for m in card["misses"]]
    assert card["false_flags"] == 0 and card["wrong_automatic_payouts"] == 0
    assert card["replay_match"] == 1.0
    assert 0.8 < card["straight_through"] < 1.0
    assert card["refusal_codes"] == {"INV6_PAYROLL_MISMATCH": 2}       # the two planted payroll edits


def test_pushback_holds_invalid_and_concedes_valid():
    disputes = evaluation.load_disputes()
    assert len(disputes) == 20 and sum(d["valid"] for d in disputes) == 10
    result = evaluation.run_pushback()
    wrong = [r["id"] for r in result["results"] if not r["right"]]
    assert (result["held_invalid"], result["conceded_valid"]) == (10, 10), wrong


def test_scorecard_prints_the_section_13_lines(card):
    lines = evaluation.format_scorecard({**card, "pushback": {"held_invalid": 10, "invalid": 10, "conceded_valid": 9,
                                                              "valid": 10}})
    assert re.fullmatch(r"planted cases: \d+ \| correct \(outcome AND amount\): \d+ \| recall \d+\.\d%", lines[0])
    assert re.fullmatch(r"false flags: \d+ \| wrong automatic payouts: \d+", lines[1])
    assert lines[2] == "pushback: held 10/10 invalid, conceded 9/10 valid"
    assert re.fullmatch(r"straight-through: \d+\.\d% \| escalations per 100 events: \d+\.\d \| replay match: \d+%",
                        lines[3])
    assert lines[4].startswith("misses: ")


def test_cli_writes_the_answer_key_and_scorecard(tmp_path, capsys):
    assert main(["eval", "--seed", "3", "--reps", "16", "--out", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("planted cases: 40")
    key = json.loads((tmp_path / "seed-3" / "answer_key.json").read_text())
    scorecard = json.loads((tmp_path / "seed-3" / "scorecard.json").read_text())
    assert len(key) == 40 and scorecard["seed"] == 3 and scorecard["pushback"]["valid"] == 10
