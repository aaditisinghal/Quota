"""The offline fallback for mapping proposals: each export kind's canonical fields, the transform that reads
them, and the header spellings seen for them. The model proposes mappings when it is available; these are
what the intake falls back to (and what it checks a proposal's field names against)."""

# kind -> canonical field -> (transform, required, header synonyms)
FIELDS = {
    "opportunities": {
        "deal_id": ("text", True, ["Opportunity ID", "Opp ID", "Id"]),
        "name": ("text", False, ["Opportunity Name", "Name"]),
        "account": ("text", True, ["Account Name", "Account"]),
        "crm_amount_cents": ("money", True, ["Amount", "Opportunity Amount"]),
        "crm_close_date": ("date_mdy", False, ["Close Date", "Closed Date"]),
        "stage": ("stage", True, ["Stage", "Stage Name"]),
        "owner": ("person", True, ["Opportunity Owner", "Owner", "Owner Name"]),
        "splits": ("splits", True, ["Split", "Splits", "Opportunity Split"]),
    },
    "field_history": {
        "deal_id": ("text", True, ["Opportunity ID", "Opp ID"]),
        "field": ("text", True, ["Field / Event", "Field", "Event"]),
        "old_value": ("text", False, ["Old Value", "Original Value"]),
        "new_value": ("text", False, ["New Value"]),
        "edited_by": ("text", True, ["Edited By", "User"]),
        "edited_at": ("datetime_pacific", True, ["Edit Date", "Date"]),
    },
    "esign": {
        "envelope_id": ("text", True, ["Envelope Id", "Envelope ID"]),
        "deal_id": ("text", True, ["SFDC Opp Id", "Opportunity ID", "Salesforce Opportunity Id"]),
        "event": ("text", True, ["Event", "Status"]),
        "recipient": ("text", False, ["Recipient", "Recipient Name"]),
        "role": ("text", False, ["Role", "Recipient Role"]),
        "at": ("datetime_utc", True, ["Timestamp (UTC)", "Timestamp", "Completed (UTC)"]),
    },
    "stripe": {
        "id": ("text", True, ["id", "ID"]),
        "at": ("datetime_utc", True, ["Created (UTC)", "Created"]),
        "amount_cents": ("decimal_money", True, ["Amount", "Gross"]),
        "currency": ("text", True, ["Currency"]),
        "customer": ("text", False, ["Customer Name", "Customer"]),
        "invoice": ("text", False, ["Invoice", "Invoice ID"]),
        "kind": ("text", True, ["Type", "Reporting Category"]),
        "status": ("text", False, ["Status"]),
        "deal_id": ("text", False, ["Metadata[opportunity_id]", "opportunity_id (metadata)"]),
    },
    "contracts": {
        "deal_id": ("text", True, ["Opportunity ID", "Opp ID"]),
        "acv_cents": ("money", True, ["ACV", "Annual Contract Value"]),
        "term_months": ("int", True, ["Term (months)", "Term"]),
        "billing": ("text", False, ["Billing", "Billing Frequency"]),
        "clauses": ("clauses", True, ["Clauses", "Special Terms"]),
        "countersigned_by": ("text", False, ["Countersigned By"]),
        "countersigned_at": ("datetime_utc", True, ["Countersigned At", "Countersigned (UTC)"]),
    },
    "hr_roster": {
        "name": ("person", True, ["Employee", "Employee Name", "Name"]),
        "email": ("text", True, ["Email", "Work Email"]),
        "role": ("text", True, ["Title", "Job Title"]),
        "manager": ("text", False, ["Manager", "Manager Name"]),
        "state": ("text", False, ["State", "Work State"]),
        "start_date": ("date_mdy", False, ["Start Date", "Hire Date"]),
        "end_date": ("date_mdy", False, ["End Date", "Termination Date"]),
    },
    "payouts": {
        "rep": ("person", True, ["Rep", "Sales Rep"]),
        "deal": ("text", True, ["Deal", "Opportunity ID"]),
        "quarter": ("text", True, ["Quarter"]),
        "base_cents": ("money", True, ["Commissionable Base", "Base"]),
        "rate": ("text", True, ["Rate"]),
        "commission_cents": ("money", True, ["Commission", "Payout"]),
        "paid_date": ("date_mdy", False, ["Paid Date", "Pay Date"]),
    },
}

# What each canonical field's value must be once read (the "types fit" check).
FIELD_TYPES = {"money": int, "decimal_money": int, "int": int, "splits": list, "clauses": dict}


def norm(header: str) -> str:
    return " ".join(header.replace("﻿", "").strip().lower().split())


def classify(headers: list[str]) -> tuple[str | None, float]:
    """The export kind whose known headers cover the most of this file's headers (and the share covered)."""
    seen = {norm(h) for h in headers}
    best, score = None, 0.0
    for kind, fields in FIELDS.items():
        known = {norm(s) for _, _, synonyms in fields.values() for s in synonyms}
        required = {f for f, (_, req, _) in fields.items() if req}
        hit = {f for f, (_, _, synonyms) in fields.items() if any(norm(s) in seen for s in synonyms)}
        share = len(seen & known) / len(seen) if seen else 0.0
        if required <= hit and share > score:
            best, score = kind, share
    return best, score


def propose(kind: str, headers: list[str]) -> list[dict]:
    """Synonym mappings: an exact (case- and space-insensitive) header match is confident."""
    out = []
    for header in headers:
        for field, (transform, _, synonyms) in FIELDS[kind].items():
            if norm(header) in {norm(s) for s in synonyms}:
                out.append({"source_column": header.replace("﻿", "").strip(), "canonical_field": field,
                            "transform": transform, "confidence": 0.99, "proposed_by": "synonyms"})
                break
    return out
