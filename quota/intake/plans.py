"""Plan learning (R6): the signed plan as a PDF with numbered sections, read back into plan terms.

The model reads the text and returns {key: {value, quote, section}}; every value must appear verbatim in its
quote and every quote verbatim in the PDF's text, or the key falls back to the section's regex. Offline, the
regexes are the reader. A section the text leaves open (whose "signature date"?) is an AMBIGUOUS review item for
the plan owner, never a guess.
"""

import re
from pathlib import Path

from quota import config

AMBIGUOUS_BOOKING = "A deal is booked on the signature date."


def plan_text(raw: dict, ambiguous_booking: bool = False) -> str:
    base, accel = (round(r["rate"] * 100) for r in raw["rates"])
    claw = raw["clawback"]
    triggers = ", ".join(t.replace("_", " ") for t in claw["triggers"])
    booking = (AMBIGUOUS_BOOKING if ambiguous_booking else
               "A deal is booked on the date the contract is countersigned by Fernhill.")
    return "\n".join([
        f"FERNHILL SALES COMPENSATION PLAN {raw['plan_id']} version {raw['version']}",
        f"Participant: {config.display_name(raw['rep'])} ({raw['rep']})",
        "",
        f"1. Term. This plan is effective from {raw['effective']['from']} to {raw['effective']['to']}.",
        f"2. Quota. The quota is ${raw['quota']['amount']:,} per quarter.",
        f"3. Rates. Commission is {base}% of credited ACV up to 100% of quota and {accel}% of credited ACV "
        f"above 100% of quota.",
        f"4. Booking date. {booking}",
        "5. Commissionable base. Commission is calculated on year-one ACV.",
        f"6. Payment. Commission is paid on collection. Commission is held once a customer is "
        f"{raw['collection_hold_days']} days past due.",
        f"7. Clawback. If the customer leaves within {claw['window_months']} months of booking ({triggers}), the "
        f"full commission is recovered.",
        "8. Advances. Commissions are advances until earned: the customer has paid and the clawback window has "
        "elapsed. An unearned advance is recovered from future advances.",
        f"9. Signature. Signed by the participant on {raw['signed_at']}.",
    ])


def write_pdf(text: str, path: Path) -> Path:
    from fpdf import FPDF
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    for line in text.split("\n"):
        pdf.multi_cell(0, 6, line or " ", new_x="LMARGIN", new_y="NEXT")
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(path))
    return path


def read_pdf(path: Path) -> str:
    from pypdf import PdfReader
    return "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)


def flat(text: str) -> str:
    return " ".join(text.split())


# ---------- the offline reader: one regex per section ----------

SECTION = re.compile(r"(\d+)\. ([A-Z][a-z]+(?: [a-z]+)*)\. (.*?)(?= \d+\. [A-Z][a-z]+(?: [a-z]+)*\. |$)")
PATTERNS = {   # key -> (section, regex whose group 1 is the value)
    "effective_from": (1, r"effective from (\d{4}-\d{2}-\d{2})"),
    "effective_to": (1, r"to (\d{4}-\d{2}-\d{2})"),
    "quota_amount": (2, r"quota is \$([\d,]+) per quarter"),
    "base_rate": (3, r"Commission is (\d+)% of credited ACV up to"),
    "accel_rate": (3, r"and (\d+)% of credited ACV above"),
    "booking_date": (4, r"booked on the (date the contract is countersigned by Fernhill|signature date)"),
    "commissionable_base": (5, r"calculated on (year-one ACV)"),
    "pay_on": (6, r"paid on (collection)"),
    "collection_hold_days": (6, r"(\d+) days past due"),
    "clawback_window_months": (7, r"within (\d+) months of booking"),
    "clawback_triggers": (7, r"of booking \(([^)]*)\)"),
    "clawback_basis": (7, r"the (full) commission is recovered"),
    "earned_when": (8, r"(the customer has paid and the clawback window has elapsed)"),
    "chargeback_method": (8, r"recovered from (future advances)"),
    "signed_at": (9, r"Signed by the participant on (\S+Z)"),
}


def sections(text: str) -> dict[int, str]:
    return {int(m.group(1)): m.group(0) for m in SECTION.finditer(flat(text))}


def regex_extract(text: str) -> dict:
    found, by_section = {}, sections(text)
    for key, (section, pattern) in PATTERNS.items():
        quote = by_section.get(section, "")
        m = re.search(pattern, quote)
        if m:
            found[key] = {"value": m.group(1), "quote": quote, "section": section, "read_by": "regex"}
    return found


EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {"terms": {"type": "array", "items": {
        "type": "object", "properties": {"key": {"type": "string", "enum": sorted(PATTERNS)},
                                         "value": {"type": "string"}, "quote": {"type": "string"},
                                         "section": {"type": "integer"}},
        "required": ["key", "value", "quote", "section"], "additionalProperties": False}}},
    "required": ["terms"], "additionalProperties": False,
}
EXTRACT_SYSTEM = ("You read a sales compensation plan. For each key you can find, return the value exactly as "
                  "written in the plan, the sentence it comes from copied verbatim, and its section number. Skip "
                  "a key the text does not state; never infer one.")


def _bare(value: str) -> str:
    return re.sub(r"[^0-9a-z]", "", value.lower())


def model_extract(llm, text: str) -> dict:
    """The model's reading, kept only where the value is in its quote and the quote is in the text. The value is
    then put in the section's canonical form ("$300,000 per quarter" -> "300,000"); a reading the section's
    pattern does not confirm is dropped, and the regex's reading stands."""
    out = llm.complete_json(EXTRACT_SYSTEM, f"Keys: {', '.join(sorted(PATTERNS))}\n\nPlan:\n{flat(text)}",
                            EXTRACT_SCHEMA)
    body, kept = flat(text), {}
    for term in out["terms"]:
        quote = flat(term["quote"])
        if not (term["value"] in quote and quote in body):
            continue
        m = re.search(PATTERNS[term["key"]][1], quote)
        if m and _bare(m.group(1)) in _bare(term["value"]):
            kept[term["key"]] = {"value": m.group(1), "quote": quote, "section": term["section"], "read_by": "model"}
    return kept


def extract(text: str, llm=None) -> dict:
    """{key: {value, quote, section, read_by}}: the model's verified reading, the regex for anything else."""
    found = regex_extract(text)
    if llm is not None:
        from quota.agent.llm import LLMError, LLMUnavailable
        try:
            found.update(model_extract(llm, text))
        except (LLMError, LLMUnavailable):
            pass
    return found


# ---------- extracted terms as plan YAML ----------

def to_plan(found: dict) -> tuple[dict, list[dict]]:
    """(plan terms in the YAML's shape, AMBIGUOUS items). Only what was read; nothing defaulted."""
    v = {k: t["value"] for k, t in found.items()}
    ambiguous = []
    booking = v.get("booking_date")
    if booking == "signature date":
        ambiguous.append({"key": "booking_date", "section": found["booking_date"]["section"],
                          "quote": found["booking_date"]["quote"],
                          "question": "Section 4 says a deal books on \"the signature date\" without saying whose: "
                                      "the customer's signature or Fernhill's countersignature?"})
        booking = None
    triggers = [t.strip().replace(" ", "_") for t in v.get("clawback_triggers", "").split(",") if t.strip()]
    plan = {
        "effective": {"from": v.get("effective_from"), "to": v.get("effective_to")},
        "signed_at": v.get("signed_at"),
        "quota": {"period": "quarter", "amount": int(v["quota_amount"].replace(",", ""))} if "quota_amount" in v else None,
        "booking_date": "countersignature" if booking == "date the contract is countersigned by Fernhill" else booking,
        "commissionable_base": "year1_acv" if v.get("commissionable_base") == "year-one ACV" else None,
        "rates": [{"tier": "base", "up_to_attainment": 1.00, "rate": int(v["base_rate"]) / 100},
                  {"tier": "accelerator", "above_attainment": 1.00, "rate": int(v["accel_rate"]) / 100}]
        if "base_rate" in v and "accel_rate" in v else None,
        "pay_on": v.get("pay_on"),
        "collection_hold_days": int(v["collection_hold_days"]) if "collection_hold_days" in v else None,
        "clawback": {"window_months": int(v["clawback_window_months"]), "triggers": triggers,
                     "basis": v.get("clawback_basis")} if "clawback_window_months" in v else None,
        "earned_when": ["customer_paid", "clawback_window_elapsed"] if "earned_when" in v else None,
        "chargeback_method": "future_advances" if v.get("chargeback_method") == "future advances" else None,
    }
    return plan, ambiguous


PLAN_KEYS = ["effective", "signed_at", "quota", "booking_date", "commissionable_base", "rates", "pay_on",
             "collection_hold_days", "clawback", "earned_when", "chargeback_method"]


def yaml_terms(raw: dict) -> dict:
    """The same keys from a plan YAML, for comparison."""
    return {k: (str(raw[k]) if k == "signed_at" else
                {kk: str(vv) for kk, vv in raw[k].items()} if k == "effective" else raw[k]) for k in PLAN_KEYS}
