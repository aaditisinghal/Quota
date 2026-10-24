"""The rest of SPEC's named cast, for the served Acme world (`python -m quota serve`): Alex Chen (split conflict),
Riley Shah (backdated close date) and Taylor Brooks (terminated mid-quarter, California). The CLI demo stays
the scripted Acme story; this module only adds people, plans and calendar events to a world after the seed.

Their deals are created before the pause (2026-04-27T17:00Z), so judges can act on them at once, but are
countersigned on Apr 28, after Jordan's April commission run. A second April run on Apr 30 books them, so
Jordan's ids (CL-0001, D-0002, A-0001, A-0002, ...) stay exactly as in the script.
"""

from quota import config
from quota.engine.plans import load_plan_raw
from quota.money import fmt_usd
from quota.sim import seed_acme

SEED = "seed"
CAST = [
    # id, name, state
    ("alex.chen", "Alex Chen", "NY"),
    ("riley.shah", "Riley Shah", "TX"),
    ("taylor.brooks", "Taylor Brooks", "CA"),
]
DEALS = [
    # deal, account, created, owner, splits, CRM amount, ACV, term months, CRM close date
    ("orbit", "Orbit Freight", "2026-04-22T16:00:00Z", "alex.chen",
     [{"rep": "alex.chen", "bps": 6000}, {"rep": "riley.shah", "bps": 4000}], 8_000_000, 8_000_000, 24, "2026-04-22"),
    ("summit", "Summit Dental", "2026-04-20T16:00:00Z", "riley.shah",
     [{"rep": "riley.shah", "bps": 10000}], 6_000_000, 6_000_000, 24, "2026-04-20"),
    ("vale", "Vale Logistics", "2026-04-15T16:00:00Z", "taylor.brooks",
     [{"rep": "taylor.brooks", "bps": 10000}], 10_000_000, 10_000_000, 24, "2026-04-15"),
]
COUNTERSIGNED = "2026-04-28T18:00:00Z"
SECOND_RUN = "2026-04-30T22:30:00Z"
TAYLOR_LEAVES = "2026-05-08"


def principals() -> dict:
    return {rep_id: {**config.rep_principal(name), "name": name} for rep_id, name, _ in CAST}


def add_cast(world) -> None:
    """Call right after `seed_acme.seed`: people and signed plans now (known 2026-04-02), deals and the rest of
    their story as events on the calendar."""
    extra = principals()
    config.register_principals(extra)
    world.wp.principals.update(extra)
    wp, emit = world.wp, world.emit
    for rep_id, name, state in CAST:
        wp.append_fact("people", {"id": rep_id, "name": name, "role": "AE", "state": state, "manager_id": "morgan.diaz",
                                  "start_date": "2024-06-01", "end_date": None, "source": "hr"}, SEED)
    template = load_plan_raw(seed_acme.PLAN_PATH)
    for rep_id, _, _ in CAST:
        plan_id = f"AE-2026-H1-{rep_id}"
        raw = {**template, "plan_id": plan_id, "rep": rep_id, "signed_doc": f"plans/{rep_id}/AE-2026-H1-v3.pdf"}
        wp.append_fact("plans", {"id": plan_id, "version": raw["version"], "rep_id": rep_id,
                                 "effective_from": raw["effective"]["from"], "effective_to": raw["effective"]["to"],
                                 "config_json": raw, "signed_doc_ref": raw["signed_doc"],
                                 "signed_at": raw["signed_at"]}, SEED)
    emit(SEED, "FACT", "people: " + ", ".join(f"{name} (AE, {state})" for _, name, state in CAST)
         + f"; signed plans AE-2026-H1 v3 like Jordan's (quota {fmt_usd(template['quota']['amount'] * 100)}/quarter)")

    queue = world.queue
    for deal_id, account, created, owner, splits, crm, acv, term, close in DEALS:
        queue.schedule(created, "deal.created", {"deal_id": deal_id, "actor": owner, "fields": {
            "account": account, "crm_amount_cents": crm, "crm_close_date": close, "stage": "closed_won",
            "owner_id": owner, "splits": splits}})
        queue.schedule(COUNTERSIGNED, "contract.countersigned", {
            "deal_id": deal_id, "timestamp": COUNTERSIGNED, "acv_cents": acv, "term_months": term,
            "clauses": {"billing": "annual_upfront"}})
    queue.schedule(SECOND_RUN, "job.due", {"job": "commission_run", "period": "2026-04", "since": "2026-04"})
    # Taylor leaves mid-quarter; Vale is invoiced before and pays after, so the signed plan holds Taylor's line.
    queue.schedule("2026-05-02T18:00:00Z", "invoice.issued", {"stripe_id": "vale-inv-1", "deal_id": "vale",
                                                              "amount_cents": 10_000_000, "date": "2026-05-02"})
    queue.schedule(f"{TAYLOR_LEAVES}T17:00:00Z", "rep.terminated", {"rep_id": "taylor.brooks",
                                                                    "end_date": TAYLOR_LEAVES, "state": "CA"})
    queue.schedule("2026-05-20T17:00:00Z", "payment.received", {"stripe_id": "vale-pay-1", "deal_id": "vale",
                                                                "amount_cents": 10_000_000, "date": "2026-05-20"})
