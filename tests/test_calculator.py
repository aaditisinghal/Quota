from quota.engine import calculator, ranking
from quota.engine.calculator import Booking, calc_commission, prior_credit, quarter_of
from quota.engine.plans import load_plan, plan_from_config

from conftest import PLAN_PATH

ACME_Q2 = Booking("acme", "jordan.kim", "2026-04-02T17:14:00Z", 12_000_000, 10000)
ACME_Q1 = Booking("acme", "jordan.kim", "2026-03-31T23:59:59Z", 12_000_000, 10000)
NOW = "2026-04-27T16:00:00Z"


def test_plan_loads_in_cents_and_bps():
    plan = load_plan(PLAN_PATH)
    assert (plan.quota_cents, plan.base_rate_bps, plan.accel_rate_bps) == (30_000_000, 1000, 1500)
    assert (plan.effective_from, plan.effective_to, plan.signed_at) == ("2026-01-01", "2026-06-30",
                                                                        "2025-12-18T17:00:00Z")
    assert plan.clawback_window_months == 6 and "termination_for_convenience" in plan.clawback_triggers
    assert plan_from_config(plan.raw) == plan


def test_quarter_of():
    assert [quarter_of(t) for t in ("2026-01-01T00:00:00Z", "2026-03-31T23:59:59Z", "2026-04-02T17:14:00Z",
                                    "2026-12-31T00:00:00Z")] == ["2026Q1", "2026Q1", "2026Q2", "2026Q4"]


def test_quarters_close_in_company_time():
    # R1: 8:30 pm Pacific on March 31 is still Q1, though it is April 1 in UTC.
    assert quarter_of("2026-04-01T03:30:00Z") == "2026Q1"
    assert quarter_of("2026-04-02T17:14:00Z") == "2026Q2"              # Acme: 10:14 am Pacific on April 2
    assert quarter_of("2026-03-31") == "2026Q1"                         # plain dates are already local
    from quota import clock as clk
    assert clk.period_of("2026-05-01T05:00:00Z") == "2026-04"           # 10 pm Pacific, April 30


def test_acme_q2_base(plan):
    r = calc_commission(plan, ACME_Q2, 0)
    assert (r.quarter, r.amount_cents, r.tier) == ("2026Q2", 1_200_000, "base")
    assert (r.attainment_before_bps, r.attainment_after_bps) == (0, 4000)


def test_acme_q1_exception_accelerator(plan):
    r = calc_commission(plan, ACME_Q1, 30_000_000)
    assert (r.quarter, r.amount_cents, r.tier) == ("2026Q1", 1_800_000, "accelerator")
    assert (r.base_portion_cents, r.accel_portion_cents) == (0, 12_000_000)
    assert (r.attainment_before_bps, r.attainment_after_bps) == (10000, 14000)


def test_mixed_tier(plan):
    r = calc_commission(plan, ACME_Q2, 25_000_000)
    assert (r.base_portion_cents, r.accel_portion_cents) == (5_000_000, 7_000_000)
    assert r.amount_cents == 500_000 + 1_050_000 == 1_550_000
    assert r.tier == "mixed"


def test_split_credit(plan):
    r = calc_commission(plan, Booking("acme", "jordan.kim", ACME_Q2.booking_ts, 12_000_000, 6000), 0)
    assert r.credit_cents == 7_200_000
    assert r.amount_cents == 720_000


def test_prior_credit_same_rep_and_quarter_only():
    history = [Booking("birch", "jordan.kim", "2026-01-20T18:00:00Z", 10_000_000, 10000),
               Booking("cedar", "jordan.kim", "2026-02-14T18:00:00Z", 12_000_000, 10000),
               Booking("delta", "jordan.kim", "2026-03-10T18:00:00Z", 8_000_000, 10000),
               Booking("other", "sam.ruiz", "2026-03-11T18:00:00Z", 9_000_000, 10000)]
    assert prior_credit(history + [ACME_Q1], ACME_Q1) == 30_000_000
    assert prior_credit(history + [ACME_Q2], ACME_Q2) == 0
    assert prior_credit(history, history[1]) == 10_000_000


def test_booking_resolution_prefers_countersignature(acme):
    res = ranking.resolve_booking(acme["deal"], acme["contract"], acme["esign"], [], NOW)
    assert (res.value, res.source, res.rule_id, res.basis) == (
        "2026-04-02T17:14:00Z", "contracts:acme:v1", "R-BOOKING-01", "countersignature")
    by_basis = {d["basis"]: d for d in res.disagreements}
    assert by_basis["crm_close_date"]["value"] == "2026-03-31T23:59:59Z"
    assert by_basis["crm_close_date"]["source"] == "deals:acme:v1"
    assert by_basis["contract_date"]["value"] == "2026-04-01T00:00:00Z"
    assert "countersignature" not in by_basis   # the e-sign event agrees, so it is not a disagreement


def test_booking_resolution_precedent_overrides(acme):
    res = ranking.resolve_booking(acme["deal"], acme["contract"], acme["esign"], [acme["p0001"]],
                                  "2026-04-29T16:00:00Z")
    assert (res.value, res.source, res.basis) == ("2026-03-31T23:59:59Z", "precedent:P-0001", "precedent")
    assert {d["value"] for d in res.disagreements} == {"2026-04-02T17:14:00Z", "2026-04-01T00:00:00Z"}
    # Not yet in force, and expired after 2026-06-30: countersignature wins again.
    for now in ("2026-04-29T15:59:59Z", "2026-07-01T00:00:00Z"):
        assert ranking.resolve_booking(acme["deal"], acme["contract"], acme["esign"], [acme["p0001"]],
                                       now).value == "2026-04-02T17:14:00Z"
    assert ranking.resolve_booking(acme["deal"], acme["contract"], acme["esign"], [acme["p0001"]],
                                   "2026-06-30T23:20:00Z").source == "precedent:P-0001"


def test_base_resolution_prefers_contract(acme, plan):
    res = ranking.resolve_base(acme["deal"], acme["contract"], plan, [], NOW)
    assert (res.value, res.source, res.rule_id) == (12_000_000, "contracts:acme:v1", "R-AMOUNT-01")
    res = ranking.with_effects(res, lambda base: calc_commission(
        plan, Booking("acme", "jordan.kim", ACME_Q2.booking_ts, base, 10000), 0).amount_cents)
    assert len(res.disagreements) == 1
    assert res.disagreements[0]["source"] == "deals:acme:v1"
    assert res.disagreements[0]["value"] == 15_000_000
    assert res.disagreements[0]["effect_cents"] == 300000


def test_splits_resolution_prefers_vp_record(acme):
    record = {"deal_id": "acme", "source": "split_records:SR-1",
              "splits": [{"rep": "jordan.kim", "bps": 6000}, {"rep": "sam.ruiz", "bps": 4000}]}
    assert ranking.resolve_splits(acme["deal"], []).value == [{"bps": 10000, "rep": "jordan.kim"}]
    res = ranking.resolve_splits(acme["deal"], [record])
    assert (res.source, res.rule_id, ranking.split_bps(res, "jordan.kim")) == ("split_records:SR-1", "R-OWNER-01",
                                                                               6000)


def test_commission_decision_from_rows(acme_rows, acme):
    d = calculator.commission_decision(acme_rows, "acme", "jordan.kim", NOW)
    assert d["result"].amount_cents == 1_200_000 and d["result"].quarter == "2026Q2"
    effects = {x["basis"]: x["effect_cents"] for x in d["resolutions"]["booking_date"]["disagreements"]}
    assert effects == {"contract_date": 0, "crm_close_date": 600_000}
    assert d["resolutions"]["base"]["disagreements"][0]["effect_cents"] == 300_000
    assert d["output"]["args"]["prior_credit_cents"] == 0

    with_precedent = {**acme_rows, "precedents": [acme["p0001"]]}
    d = calculator.commission_decision(with_precedent, "acme", "jordan.kim", "2026-04-29T16:00:00Z")
    assert (d["result"].quarter, d["result"].prior_credit_cents, d["result"].tier, d["result"].amount_cents) == (
        "2026Q1", 30_000_000, "accelerator", 1_800_000)
    assert d["precedent_ids"] == ["P-0001"]
