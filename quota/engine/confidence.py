"""How sure Quota is of a commission line (DEMO_FLOW_TASKS D1), derived from evidence, never a model's
self-rating: the weakest of six factors, x 100, each with its reason.

A commission decision stores the five factors its own rows can tell (booking date, amount, owner and splits,
extraction, rule coverage) in its output, so replay reproduces them. Integrity is known only later (a backdated
edit, an off-contract promise, an approver conflict), so `current` applies it, and anything else learned since,
from the decisions about the deal. Collection is not a factor: an unpaid invoice is a hold, shown separately.
"""

from quota import clock as clk

CLEARED, NOTE = 90, 70
INTEGRITY_CAP = 0.40


def _factor(name, value, reason) -> dict:
    return {"name": name, "value": round(value, 2), "reason": reason}


def _booking(res) -> dict:
    if not res:
        return _factor("booking_date", 1.0, "no booking resolution recorded")
    chosen = clk.local_date(str(res["value"]))
    differ = [d for d in res.get("disagreements", []) if clk.local_date(str(d["value"])) != chosen]
    if not differ:
        return _factor("booking_date", 1.0, f"all sources say {chosen}")
    names = ", ".join(f"{d['basis']} {clk.local_date(str(d['value']))}" for d in differ)
    return _factor("booking_date", 0.8, f"{names} disagree with {res['basis']} {chosen}; settled by {res['rule_id']}")


def _amount(res) -> dict:
    if not res:
        return _factor("amount", 1.0, "no amount resolution recorded")
    differ = [d for d in res.get("disagreements", []) if d["value"] != res["value"]]
    if not differ:
        return _factor("amount", 1.0, "all sources agree on the base")
    return _factor("amount", 0.8, f"{', '.join(d['basis'] for d in differ)} disagree with {res['basis']}; settled by "
                                  f"{res['rule_id']}")


def _splits(res) -> dict:
    shares = (res or {}).get("value") or []
    total = sum(s.get("bps", 0) for s in shares)
    if not shares or any(not s.get("rep") for s in shares):
        return _factor("owner_splits", 0.0, "no owner on the deal")
    if total != 10000:
        return _factor("owner_splits", 0.0, f"splits add to {total / 100:g}%, not 100%")
    if len(shares) == 1:
        return _factor("owner_splits", 1.0, "one owner, 100%")
    if str(res.get("source", "")).startswith("split_records:"):
        return _factor("owner_splits", 1.0, "split approved by the VP Sales Ops, 100%")
    return _factor("owner_splits", 0.8, "split from the CRM only (no VP split record)")


def _extraction(amount_res) -> dict:
    """Commission numbers come from structured records, read verbatim; a second source agreeing is the extra."""
    differ = [d for d in (amount_res or {}).get("disagreements", []) if d["value"] != amount_res["value"]]
    if differ:
        return _factor("extraction", 0.9, f"numbers verbatim in the {amount_res['basis']}; the "
                                          f"{', '.join(d['basis'] for d in differ)} differs")
    return _factor("extraction", 1.0, "numbers verbatim in the source records, and a second source agrees")


def _coverage(precedent_ids, no_rule) -> dict:
    if no_rule:
        return _factor("rule_coverage", 0.0, f"NO_RULE: {no_rule}")
    if precedent_ids:
        return _factor("rule_coverage", 0.9, f"a saved precedent applies ({', '.join(precedent_ids)})")
    return _factor("rule_coverage", 1.0, "the signed plan and every clause are known")


def stored(resolutions: dict, precedent_ids, no_rule=None) -> dict:
    """What a commission decision records about itself (pure over its resolutions)."""
    factors = [_booking(resolutions.get("booking_date")), _amount(resolutions.get("base")),
               _splits(resolutions.get("splits")), _extraction(resolutions.get("base")),
               _coverage(list(precedent_ids or []), no_rule), _factor("integrity", 1.0, "no flags when booked")]
    return summarize(factors)


def summarize(factors) -> dict:
    score = int(round(min(f["value"] for f in factors) * 100))
    return {"score": score, "band": band(score), "factors": factors}


def band(score) -> str:
    return "cleared" if score >= CLEARED else "note" if score >= NOTE else "held"


BAND_TEXT = {"cleared": "cleared to the payout file (the Controller still signs the batch)",
             "note": "cleared with a note to the rep and the analyst queue",
             "held": "held; the right person is asked"}


def current(stored_conf: dict, flags) -> dict:
    """The stored factors with what is known now: each flag is (factor name, value, reason); a factor takes the
    lowest value it is given."""
    factors = {f["name"]: dict(f) for f in stored_conf["factors"]}
    for name, value, reason in flags:
        if value < factors[name]["value"]:
            factors[name] = _factor(name, value, reason)
    return summarize(list(factors.values()))
