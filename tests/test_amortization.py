from dataclasses import replace

from quota.engine import entries
from quota.engine.amortization import amortized_through, build_schedule, capitalize, remaining_after
from quota.ledger.invariants import entry_balanced

LINE = {"id": "CL-0002", "rep_id": "jordan.kim", "deal_id": "acme", "amount_cents": 1_800_000}


def _lines(entry):
    return [(l["account"], l["debit_cents"], l["credit_cents"]) for l in entry["lines"]]


def test_schedule_36_even():
    schedule = build_schedule(1_800_000, 36, "2026-04")
    assert len(schedule) == 36
    assert {cents for _, cents in schedule} == {50_000}
    assert (schedule[0][0], schedule[-1][0]) == ("2026-04", "2029-03")


def test_schedule_remainder_last():
    assert [c for _, c in build_schedule(1_000_000, 3, "2026-04")] == [333_333, 333_333, 333_334]


def test_amortized_through_and_remaining():
    schedule = build_schedule(1_800_000, 36, "2026-04")
    assert amortized_through(schedule, "2026-06") == 150_000
    assert remaining_after(schedule, "2026-06") == 1_650_000
    assert amortized_through(schedule, "2026-03") == 0


def test_short_terms_are_expensed():
    assert not capitalize(12) and capitalize(24) and capitalize(36)


def test_clawback_recoverable(plan):
    assert entries.is_recoverable(plan, "2026-03-31", "2026-06-30", "termination_for_convenience")
    entry = entries.clawback_entry(LINE, 1_800_000, 150_000, "2026-06-30", recoverable=True)
    assert _lines(entry) == [("1290", 1_800_000, 0), ("1350", 0, 1_650_000), ("6110", 0, 150_000)]
    assert entry["memo_kind"] == "clawback" and entry_balanced(entry["lines"])


def test_clawback_not_recoverable_when_unsigned(plan):
    unsigned = replace(plan, signed_at=None)
    assert not entries.is_recoverable(unsigned, "2026-03-31", "2026-06-30", "termination_for_convenience")
    entry = entries.clawback_entry(LINE, 1_800_000, 150_000, "2026-06-30", recoverable=False)
    assert _lines(entry) == [("6110", 1_650_000, 0), ("1350", 0, 1_650_000)]


def test_clawback_window_and_trigger(plan):
    assert entries.is_recoverable(plan, "2026-03-31", "2026-09-30", "churn")
    assert not entries.is_recoverable(plan, "2026-03-31", "2026-10-01", "churn")
    assert not entries.is_recoverable(plan, "2026-03-31", "2026-06-30", "downgrade")
    assert not entries.is_recoverable(replace(plan, clawback_window_months=None), "2026-03-31", "2026-06-30", "churn")


def test_builders_balance_and_use_section_6_5_accounts():
    payroll = {"id": "PR-2026-05", "total_cents": 1_800_000}
    built = {
        "invoice": entries.invoice_entry("acme", 12_000_000, "2026-04-02"),
        "payment": entries.payment_entry("acme", 12_000_000, "2026-04-15"),
        "revenue": entries.revenue_entry("acme", 1_000_000, "2026-04-30"),
        "refund": entries.refund_entry("acme", 9_000_000, "2026-06-30"),
        "accrual": entries.commission_accrual_entry(LINE, "2026-04-30"),
        "expensed": entries.commission_accrual_entry(LINE, "2026-04-30", capitalized=False),
        "amortization": entries.amortization_entry(LINE, 50_000, "2026-04-30"),
        "payroll": entries.payroll_payment_entry(payroll, "2026-05-15"),
    }
    accounts = {k: [(l["account"], "Dr" if l["debit_cents"] else "Cr") for l in e["lines"]] for k, e in built.items()}
    assert accounts == {
        "invoice": [("1200", "Dr"), ("2400", "Cr")], "payment": [("1000", "Dr"), ("1200", "Cr")],
        "revenue": [("2400", "Dr"), ("4000", "Cr")], "refund": [("2400", "Dr"), ("1000", "Cr")],
        "accrual": [("1350", "Dr"), ("2150", "Cr")], "expensed": [("6115", "Dr"), ("2150", "Cr")],
        "amortization": [("6110", "Dr"), ("1350", "Cr")], "payroll": [("2150", "Dr"), ("1000", "Cr")],
    }
    assert all(entry_balanced(e["lines"]) for e in built.values())
    assert built["amortization"]["memo"].startswith("amortization: ")
