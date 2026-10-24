"""Seeds the facts for the Acme demo world (IMPLEMENTATION.md section 8), each at its own known_at."""

from pathlib import Path

from quota import config
from quota.engine.plans import load_plan_raw
from quota.money import fmt_usd

SEED = "seed"
SEED_START = "2025-12-01T00:00:00Z"
ROOT = Path(__file__).resolve().parents[2]
PLAN_PATH = ROOT / "plans" / "jordan.kim" / "AE-2026-H1-v3.yaml"

PEOPLE = ["priya.nair", "marcus.lee", "dana.ortiz", "morgan.diaz", "jordan.kim"]

# Q1 history: facts only, no journal entries (they predate the demo ledger). $300,000 = 100% of quota.
HISTORY = [
    # deal, account, crm/acv cents, close date, term months, countersigned
    ("birch", "Birch Health", 10_000_000, "2026-01-20", 12, "2026-01-20T18:00:00Z"),
    ("cedar", "Cedar Logistics", 12_000_000, "2026-02-14", 24, "2026-02-14T18:00:00Z"),
    ("delta", "Delta Foods", 8_000_000, "2026-03-10", 12, "2026-03-10T18:00:00Z"),
]
JORDAN_SPLITS = [{"rep": "jordan.kim", "bps": 10000}]


def _deal(deal_id, account, crm_cents, close_date):
    return {"id": deal_id, "account": account, "crm_amount_cents": crm_cents, "crm_close_date": close_date,
            "stage": "closed_won", "owner_id": "jordan.kim", "splits_json": JORDAN_SPLITS, "source": "crm"}


def _contract(deal_id, acv_cents, term, contract_date, countersigned_at, clauses, doc_ref):
    return {"id": deal_id, "deal_id": deal_id, "acv_cents": acv_cents, "term_months": term,
            "contract_date": contract_date, "countersigned_at": countersigned_at, "clauses_json": clauses,
            "doc_ref": doc_ref, "source": "esign"}


def _esign(event, at):
    return {"id": f"ENV-ACME-001-{event}", "envelope_id": "ENV-ACME-001", "deal_id": "acme", "event": event,
            "at": at}


def seed(wp, clock, emit) -> None:
    clock.advance_to(SEED_START)
    for person in PEOPLE:
        row = {"id": person, "name": config.display_name(person), "role": config.title(person), "state": None,
               "manager_id": None, "start_date": None, "end_date": None, "source": "hr"}
        if person == "jordan.kim":
            row.update(state="CA", manager_id="morgan.diaz", start_date="2024-03-01")
        wp.append_fact("people", row, SEED)
    emit(SEED, "FACT", "people: " + ", ".join(f"{config.display_name(p)} ({config.title(p)})" for p in PEOPLE)
         + "; Jordan Kim is in CA, reports to morgan.diaz, started 2024-03-01")

    clock.advance_to("2025-12-18T17:00:00Z")
    raw = load_plan_raw(PLAN_PATH)
    wp.append_fact("plans", {
        "id": raw["plan_id"], "version": raw["version"], "rep_id": raw["rep"],
        "effective_from": raw["effective"]["from"], "effective_to": raw["effective"]["to"], "config_json": raw,
        "signed_doc_ref": raw["signed_doc"], "signed_at": raw["signed_at"],
    }, SEED)
    emit(SEED, "FACT", f"plans {raw['plan_id']} v{raw['version']} {raw['rep']}: quota "
                       f"{fmt_usd(raw['quota']['amount'] * 100)}/quarter, 10% to 100%, 15% above, books on "
                       f"{raw['booking_date']}, clawback {raw['clawback']['window_months']} months, "
                       f"signed {raw['signed_at']}")

    clock.advance_to("2026-01-01T00:00:00Z")
    for month in range(1, 13):
        wp.append_fact("periods", {"period": f"2026-{month:02d}", "status": "open", "actor": SEED}, SEED)
    emit(SEED, "PERIOD", "2026-01 .. 2026-12 open")
    wp.append_fact("precedents", {
        "id": "P-0000",
        "statement": "Monthly amortization of Controller-approved commission schedules may post without "
                     "per-entry approval",
        "scope_json": {"kind": "amortization"}, "override_json": {"per_entry_approval": False},
        "approvals_json": ["priya.nair"], "effective_from": "2026-01-01", "expires_at": "2026-12-31",
    }, SEED)
    emit(SEED, "FACT", "P-0000 standing approval (priya.nair): monthly amortization of approved schedules "
                       "posts without per-entry approval, 2026-01-01..2026-12-31")

    for deal_id, account, cents, close, term, countersigned in HISTORY:
        clock.advance_to(countersigned)
        wp.append_fact("deals", _deal(deal_id, account, cents, close), SEED)
        wp.append_fact("contracts", _contract(deal_id, cents, term, close, countersigned, {},
                                              f"contracts/{deal_id}-msa.pdf"), SEED)
        emit(SEED, "FACT", f"{deal_id} ({account}) {fmt_usd(cents)}, {term} months, countersigned "
                           f"{countersigned} (Q1 history, facts only)")

    clock.advance_to("2026-03-31T22:00:00Z")
    wp.append_fact("esign_events", _esign("sent", clock.now()), SEED)
    emit(SEED, "FACT", "esign ENV-ACME-001 sent to Acme Robotics")

    clock.advance_to("2026-03-31T23:30:00Z")
    wp.append_fact("deals", _deal("acme", "Acme Robotics", 15_000_000, "2026-03-31"), SEED)
    emit(SEED, "FACT", "deals acme v1 Acme Robotics: CRM $150,000.00, close 2026-03-31, closed_won, "
                       "jordan.kim 100%")

    clock.advance_to("2026-04-01T19:40:00Z")
    wp.append_fact("esign_events", _esign("customer_signed", clock.now()), SEED)
    emit(SEED, "FACT", "esign ENV-ACME-001 customer_signed")

    clock.advance_to("2026-04-02T17:14:00Z")
    wp.append_fact("esign_events", _esign("countersigned", clock.now()), SEED)
    wp.append_fact("contracts", _contract(
        "acme", 12_000_000, 36, "2026-04-01", clock.now(),
        {"billing": "annual_upfront", "termination_for_convenience": {"window_days": 90, "refund": "pro_rata"}},
        "contracts/acme-msa.pdf#p3"), SEED)
    emit(SEED, "FACT", "esign ENV-ACME-001 countersigned; contracts acme v1: ACV $120,000.00, 36 months, "
                       "contract_date 2026-04-01, annual upfront, 90-day termination for convenience")
